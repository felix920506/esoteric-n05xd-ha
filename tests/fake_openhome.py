"""A simulated N-05XD network module (OpenHome renderer).

Serves the real description/SCPD XML captured from an N-05XD
(tests/fixtures/n05xd_upnp), answers the SOAP actions the integration uses,
and sends GENA events like the real unit: the full state right after
SUBSCRIBE, then changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import re
from html import escape
from pathlib import Path

import aiohttp
from aiohttp import web

FIXTURES = Path(__file__).parent / "fixtures" / "n05xd_upnp"

DIDL = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
    '<item id="1" parentID="0" restricted="1"><dc:title>{title}</dc:title>'
    "<upnp:artist>{artist}</upnp:artist>"
    '<upnp:artist role="AlbumArtist">Various Artists</upnp:artist>'
    "<upnp:album>{album}</upnp:album>"
    "<upnp:albumArtURI>/art/{title}.jpg</upnp:albumArtURI>"
    "<upnp:class>object.item.audioItem.musicTrack</upnp:class></item></DIDL-Lite>"
)


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes")


class FakeOpenHome:
    """Simulated network side of an N-05XD."""

    def __init__(self) -> None:
        """Initialize."""
        self.send_events = True
        # Like the real unit with a callback outside its subnet.
        self.reject_subscriptions = False
        self.transport_state = "Playing"
        # Transport.Repeat is a string on the real unit ("true"/"false").
        self.repeat = "false"
        self.shuffle = False
        self.stream_id = 7
        self.title, self.artist, self.album = "Vivid Theory", "Arcaea", "Memories"
        self.duration = 245
        self.seconds = 30
        self.source_index = 0
        # AirPlay (source 2 on the real unit) reports all three as False.
        self.can_modes = True
        # Strings Transport.SetRepeat honors; others are ACKed but ignored.
        self.transport_repeat_values = {"true": "true", "false": "false"}
        self.calls: list[str] = []
        self.subscriptions: dict[str, tuple[str, str]] = {}  # sid -> (svc, url)
        self._sids = itertools.count(1)
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None
        self.port = 0

    @property
    def url(self) -> str:
        """Description URL."""
        return f"http://127.0.0.1:{self.port}/"

    async def start(self) -> None:
        """Listen on a random port."""
        app = web.Application()
        app.router.add_get("/", self._description)
        app.router.add_route("*", "/{service}/{udn}/{kind}.xml", self._service)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        """Shut down (like the unit going to standby)."""
        if self._session:
            await self._session.close()
        if self._runner:
            await self._runner.cleanup()

    # HTTP --------------------------------------------------------------

    async def _description(self, request: web.Request) -> web.Response:
        return web.Response(
            text=(FIXTURES / "description.xml").read_text(), content_type="text/xml"
        )

    async def _service(self, request: web.Request) -> web.Response:
        service, kind = request.match_info["service"], request.match_info["kind"]
        if kind == "scpd":
            return web.Response(
                text=(FIXTURES / f"{service}.xml").read_text(), content_type="text/xml"
            )
        if kind == "control":
            return await self._soap(service, request)
        if request.method == "SUBSCRIBE":
            return self._subscribe(service, request)
        if request.method == "UNSUBSCRIBE":
            self.subscriptions.pop(request.headers.get("SID", ""), None)
            return web.Response()
        return web.Response(status=405)

    def _subscribe(self, service: str, request: web.Request) -> web.Response:
        if self.reject_subscriptions:
            return web.Response(status=412)
        sid = request.headers.get("SID")
        if sid is None:  # new subscription (renewals carry SID)
            sid = f"uuid:sub-{next(self._sids)}"
            callback = request.headers["CALLBACK"].strip("<>")
            self.subscriptions[sid] = (service, callback)
            if self.send_events:
                asyncio.get_running_loop().call_later(
                    0.05, lambda: asyncio.ensure_future(self._notify(sid))
                )
        return web.Response(headers={"SID": sid, "TIMEOUT": "Second-1800"})

    async def _soap(self, service: str, request: web.Request) -> web.Response:
        action = request.headers["SOAPACTION"].strip('"').split("#")[1]
        body = await request.text()
        args = {
            k: v
            for k, v in re.findall(r"<(\w+)>([^<]*)</\1>", body)
            if not k.startswith(action)
        }
        self.calls.append(f"{service}.{action}")
        out = self.handle(service, action, args)
        if out is None:
            return web.Response(
                status=500, text="<s:Envelope>Invalid Action</s:Envelope>"
            )
        ns = f"urn:av-openhome-org:service:{service}:1"
        values = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in out.items())
        return web.Response(
            content_type="text/xml",
            text=(
                '<?xml version="1.0"?><s:Envelope '
                'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                f'<s:Body><u:{action}Response xmlns:u="{ns}">{values}'
                f"</u:{action}Response></s:Body></s:Envelope>"
            ),
        )

    # Behavior ----------------------------------------------------------

    def evented(self, service: str) -> dict[str, object]:
        """Evented variables of a service, as the real unit sends them."""
        if service == "Transport":
            return {
                "TransportState": self.transport_state,
                "Repeat": self.repeat,
                "Shuffle": int(self.shuffle),
                "CanRepeat": int(self.can_modes),
                "CanShuffle": int(self.can_modes),
                "CanSeek": int(self.can_modes),
                "CanPause": 1,
                "StreamId": self.stream_id,
            }
        if service == "Info":
            return {
                "Uri": "http://server/track.flac",
                "Metadata": self.metadata,
                "Duration": self.duration,
                "CodecName": "FLAC",
            }
        if service == "Time":
            return {"Duration": self.duration, "Seconds": self.seconds}
        if service == "Product":
            return {"SourceIndex": self.source_index, "Standby": 0}
        if service == "Playlist":
            return {"Repeat": int(_bool(self.repeat)), "Shuffle": int(self.shuffle)}
        return {}

    @property
    def metadata(self) -> str:
        """DIDL-Lite of the current track."""
        return DIDL.format(title=self.title, artist=self.artist, album=self.album)

    def handle(self, service: str, action: str, args: dict[str, str]):
        """Return the out-arguments of an action, or None if unknown."""
        state = self.evented(service)
        match (service, action):
            case ("Transport", "TransportState"):
                return {"State": self.transport_state}
            case ("Transport", "ModeInfo"):
                return {k: state[k] for k in ("CanRepeat", "CanShuffle")} | {
                    "CanSkipNext": 1,
                    "CanSkipPrevious": 1,
                }
            case ("Transport", "StreamInfo"):
                return {
                    "StreamId": self.stream_id,
                    "CanSeek": int(self.can_modes),
                    "CanPause": 1,
                }
            case ("Transport", "Repeat"):
                return {"Repeat": self.repeat}
            case ("Transport", "Shuffle"):
                return {"Shuffle": int(self.shuffle)}
            case ("Transport", "SetRepeat"):
                if (
                    value := self.transport_repeat_values.get(args["Repeat"])
                ) is not None:
                    self._change(repeat=value)
                return {}
            case ("Playlist", "SetRepeat"):
                self._change(repeat="true" if _bool(args["Value"]) else "false")
                return {}
            case ("Transport", "SetShuffle"):
                self._change(shuffle=_bool(args["Shuffle"]))
                return {}
            case ("Transport", "SeekSecondAbsolute"):
                assert int(args["StreamId"]) == self.stream_id
                self._change(seconds=int(args["SecondAbsolute"]))
                return {}
            case ("Info", "Track"):
                return {"Uri": state["Uri"], "Metadata": state["Metadata"]}
            case ("Info", "Details"):
                return {
                    "Duration": self.duration,
                    "BitRate": 0,
                    "BitDepth": 16,
                    "SampleRate": 44100,
                    "Lossless": 1,
                    "CodecName": "FLAC",
                }
            case ("Time", "Time"):
                return {
                    "TrackCount": 1,
                    "Duration": self.duration,
                    "Seconds": self.seconds,
                }
            case ("Product", "SourceIndex"):
                return {"Value": self.source_index}
        return None

    def _change(self, **values) -> None:
        for key, value in values.items():
            setattr(self, key, value)
        if self.send_events:
            for sid in list(self.subscriptions):
                asyncio.ensure_future(self._notify(sid))

    def change(self, **values) -> None:
        """Change state as if from the front panel or another app."""
        self._change(**values)

    async def _notify(self, sid: str) -> None:
        if sid not in self.subscriptions or self._session is None:
            return
        service, callback = self.subscriptions[sid]
        props = "".join(
            f"<e:property><{k}>{escape(str(v))}</{k}></e:property>"
            for k, v in self.evented(service).items()
        )
        body = (
            '<?xml version="1.0"?><e:propertyset '
            f'xmlns:e="urn:schemas-upnp-org:event-1-0">{props}</e:propertyset>'
        )
        with contextlib.suppress(aiohttp.ClientError):
            await self._session.request(
                "NOTIFY",
                callback,
                data=body,
                headers={
                    "NT": "upnp:event",
                    "NTS": "upnp:propchange",
                    "SID": sid,
                    "SEQ": "0",
                    "Content-Type": 'text/xml; charset="utf-8"',
                },
            )
