"""Optional network (UPnP / OpenHome) side of network players.

The N-05XD's network module is an OpenHome renderer. It adds what RS-232
lacks: track metadata and artwork, duration/position, seeking, and setting
repeat / shuffle (which the N-05XD rejects over RS-232). RS-232 stays the
primary interface: the network module is switched off in standby (no ping,
no SSDP, no HTTP), and only comes up ~30 s after POWER ON.

Change notifications use UPnP eventing (GENA). The unit sends the full
state of a service right after subscribing; if that first event doesn't
arrive (e.g. a firewall between VLANs blocks the callback), we fall back to
polling.

No Home Assistant imports here so the module can be tested in isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpSessionRequester
from async_upnp_client.client import UpnpDevice, UpnpService, UpnpStateVariable
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.exceptions import UpnpError, UpnpResponseError
from defusedxml import ElementTree

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 23000
OPENHOME = "urn:av-openhome-org:service:{}:1"
REQUIRED_SERVICES = ("Transport", "Info", "Time")
OPTIONAL_SERVICES = ("Product", "Playlist")
# The unit sends each service's state right after SUBSCRIBE.
INITIAL_EVENT_TIMEOUT = 5.0
SUBSCRIPTION_TIMEOUT = 1800
REQUEST_TIMEOUT = 5
# Time events arrive every second while playing; only notify listeners when
# the position jumps (seek, new track) rather than on every tick.
POSITION_TOLERANCE = 2

_DIDL = {
    "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
}


class NetworkError(Exception):
    """The network side is unreachable or misbehaved."""


def description_url(address: str) -> str:
    """Turn ``host``, ``host:port`` or a full URL into a description URL."""
    address = address.strip()
    if not address:
        raise ValueError("Empty network address")
    if "://" not in address:
        address = f"http://{address}"
    parts = urlsplit(address)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Invalid network address {address!r}")
    port = parts.port or DEFAULT_PORT
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}:{port}{parts.path or '/'}"


async def async_probe(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
    """Check that ``url`` is an Esoteric renderer; return (udn, friendly name).

    Raises NetworkError if unreachable, ValueError if it is something else.
    """
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            response = await session.get(url)
            response.raise_for_status()
            text = await response.text()
    except (aiohttp.ClientError, OSError, TimeoutError) as err:
        raise NetworkError(f"{url}: {err!r}") from err
    try:
        root = ElementTree.fromstring(text)
    except Exception as err:  # noqa: BLE001
        raise ValueError(f"{url} is not a UPnP device description") from err
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    device = root.find("d:device", ns)
    if device is None:
        raise ValueError(f"{url} is not a UPnP device description")

    def text_of(tag: str) -> str:
        return (device.findtext(f"d:{tag}", "", ns) or "").strip()

    if "ESOTERIC" not in text_of("manufacturer").upper():
        raise ValueError(f"{url} is not an Esoteric device")
    return text_of("UDN"), text_of("friendlyName")


@dataclass
class TrackInfo:
    """Metadata of the current track, from Info's DIDL-Lite."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    image_url: str | None = None


def parse_didl(metadata: str, base_url: str) -> TrackInfo:
    """Extract title / artist / album / artwork from DIDL-Lite."""
    if not metadata:
        return TrackInfo()
    try:
        root = ElementTree.fromstring(metadata)
    except Exception:  # noqa: BLE001 - malformed metadata from the stream
        _LOGGER.debug("Unparsable DIDL-Lite: %.200s", metadata)
        return TrackInfo()
    item = root.find("didl:item", _DIDL)
    if item is None:
        return TrackInfo()

    def text(path: str) -> str | None:
        node = item.find(path, _DIDL)
        return node.text.strip() if node is not None and node.text else None

    # Servers differ: minidlna puts the track artist in dc:creator and the
    # album artist in a plain upnp:artist; others tag upnp:artist roles.
    plain, album_artist = None, None
    for node in item.findall("upnp:artist", _DIDL):
        name = (node.text or "").strip()
        role = node.get("role")
        if not name:
            continue
        if role == "AlbumArtist":
            album_artist = album_artist or name
        elif role in (None, "Performer"):
            plain = plain or name
    artist = text("dc:creator") or plain
    if album_artist is None and plain and plain != artist:
        album_artist = plain
    image = text("upnp:albumArtURI")
    return TrackInfo(
        title=text("dc:title"),
        artist=artist,
        album=text("upnp:album"),
        album_artist=album_artist,
        image_url=urljoin(base_url, image) if image else None,
    )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class NetworkState:
    """Everything the network side has told us."""

    transport_state: str | None = None
    repeat: bool | None = None
    shuffle: bool | None = None
    can_repeat: bool = False
    can_shuffle: bool = False
    can_seek: bool = False
    can_pause: bool = False
    stream_id: int = 0
    track: TrackInfo = field(default_factory=TrackInfo)
    track_uri: str | None = None
    codec: str | None = None
    duration: int | None = None
    position: int | None = None
    position_at: float | None = None  # time.time() when position was read
    source_index: int | None = None


class EsotericNetwork:
    """OpenHome control and eventing for one unit."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        listen_host: str | None = None,
        listen_port: int = 0,
        callback_url: str | None = None,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        """Initialize. ``url`` is the device description URL."""
        self.url = url
        self.state = NetworkState()
        self.connected = False
        self.events = False
        self.udn: str | None = None
        self._requester = AiohttpSessionRequester(
            session, with_sleep=True, timeout=REQUEST_TIMEOUT
        )
        self._listen = (listen_host or "0.0.0.0", listen_port)
        self._callback_url = callback_url
        # Called on any change; the coordinator sets this.
        self.on_update: Callable[[], None] = on_update or (lambda: None)
        self._device: UpnpDevice | None = None
        self._services: dict[str, UpnpService] = {}
        self._server: AiohttpNotifyServer | None = None
        self._initial: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._subscribed_at = 0.0

    @property
    def callback_url(self) -> str | None:
        """URL the unit sends events to, once listening."""
        return self._server.callback_url if self._server else None

    # Connection ---------------------------------------------------------

    async def async_connect(self) -> None:
        """Read the device description, then subscribe (or poll)."""
        async with self._lock:
            if self.connected:
                return
            try:
                await self._async_connect()
            except NetworkError:
                await self._async_teardown()
                raise
            except Exception as err:  # noqa: BLE001 - always clean up
                await self._async_teardown()
                raise NetworkError(f"{self.url}: {err!r}") from err

    async def _async_connect(self) -> None:
        factory = UpnpFactory(self._requester, non_strict=True)
        device = await factory.async_create_device(self.url)
        services = {}
        for name in REQUIRED_SERVICES + OPTIONAL_SERVICES:
            if (service := device.service(OPENHOME.format(name))) is not None:
                services[name] = service
            elif name in REQUIRED_SERVICES:
                raise NetworkError(f"{self.url} has no OpenHome {name} service")
        self._device, self._services = device, services
        self.udn = device.udn
        self.state = NetworkState()

        try:
            await self._async_subscribe()
        except UpnpResponseError as err:
            if err.status == HTTPStatus.PRECONDITION_FAILED:
                # Seen on a real N-05XD: it only accepts callbacks on its own
                # subnet and answers 412 otherwise.
                _LOGGER.warning(
                    "%s refused event subscriptions for %s (HTTP 412); it only "
                    "accepts callback addresses on its own subnet. Polling "
                    "instead. For push updates, give Home Assistant an address "
                    "on the unit's subnet, or set the event callback URL to a "
                    "proxy there",
                    self.url,
                    self.callback_url,
                )
            else:
                _LOGGER.warning(
                    "Could not subscribe to events from %s (%s); polling instead",
                    self.url,
                    err,
                )
            await self._async_stop_events()
        except (UpnpError, aiohttp.ClientError, OSError) as err:
            _LOGGER.warning(
                "Could not subscribe to events from %s (%s); polling instead",
                self.url,
                err,
            )
            await self._async_stop_events()
        if not self.events:
            await self._async_poll()
        self.connected = True
        _LOGGER.info(
            "Connected to %s (%s)", self.url, "events" if self.events else "polling"
        )
        self.on_update()

    async def _async_subscribe(self) -> None:
        self._server = AiohttpNotifyServer(
            self._requester, source=self._listen, callback_url=self._callback_url
        )
        await self._server.async_start_server()
        handler = self._server.event_handler
        self._initial = {name: asyncio.Event() for name in self._services}
        for service in self._services.values():
            service.on_event = self._on_event
            await handler.async_subscribe(
                service, timeout=timedelta(seconds=SUBSCRIPTION_TIMEOUT)
            )
        self._subscribed_at = time.monotonic()
        try:
            async with asyncio.timeout(INITIAL_EVENT_TIMEOUT):
                for event in self._initial.values():
                    await event.wait()
        except TimeoutError:
            _LOGGER.warning(
                "No events from %s reached %s; polling instead. If Home "
                "Assistant is on another VLAN or behind NAT, allow the unit to "
                "reach that address or set the event callback URL/port",
                self.url,
                self.callback_url,
            )
            await self._async_stop_events()
            return
        self.events = True

    async def async_disconnect(self) -> None:
        """Unsubscribe and forget the device (e.g. the unit went to standby)."""
        async with self._lock:
            was_connected = self.connected
            await self._async_teardown()
        if was_connected:
            self.on_update()

    async def _async_teardown(self) -> None:
        self.connected = False
        await self._async_stop_events()
        self._device = None
        self._services = {}

    async def _async_stop_events(self) -> None:
        self.events = False
        server, self._server = self._server, None
        if server is None:
            return
        # The unit is probably already off; nothing to clean up there.
        with contextlib.suppress(UpnpError, aiohttp.ClientError, OSError, TimeoutError):
            await server.event_handler.async_unsubscribe_all()
        # aiohttp waits up to 10 s for connections the sender keeps alive
        # after a NOTIFY; drop them so standby/unload doesn't stall.
        http_server = getattr(server, "_aiohttp_server", None)
        for connection in list(getattr(http_server, "connections", ())):
            connection.force_close()
        await server.async_stop_server()

    async def async_refresh(self) -> None:
        """Keep the connection healthy: poll or renew subscriptions.

        Raises NetworkError (and disconnects) if the unit stopped answering.
        """
        if not self.connected:
            return
        try:
            if self.events and self._server is not None:
                if time.monotonic() - self._subscribed_at > SUBSCRIPTION_TIMEOUT / 2:
                    await self._server.event_handler.async_resubscribe_all()
                    self._subscribed_at = time.monotonic()
                # Cheap liveness check; events alone don't tell us the unit
                # is gone.
                await self._call("Transport", "TransportState")
            else:
                await self._async_poll()
        except (UpnpError, aiohttp.ClientError, OSError, TimeoutError) as err:
            await self.async_disconnect()
            raise NetworkError(f"{self.url}: {err!r}") from err
        self.on_update()

    # State --------------------------------------------------------------

    async def _async_poll(self) -> None:
        values: dict[str, Any] = {}
        values["TransportState"] = (await self._call("Transport", "TransportState"))[
            "State"
        ]
        values.update(await self._call("Transport", "ModeInfo"))
        values.update(await self._call("Transport", "StreamInfo"))
        values.update({"Repeat": (await self._call("Transport", "Repeat"))["Repeat"]})
        values.update(
            {"Shuffle": (await self._call("Transport", "Shuffle"))["Shuffle"]}
        )
        values.update(await self._call("Info", "Track"))
        details = await self._call("Info", "Details")
        values["CodecName"] = details.get("CodecName")
        time_values = await self._call("Time", "Time")
        values["Seconds"] = time_values.get("Seconds")
        values["Duration"] = time_values.get("Duration")
        if "Product" in self._services:
            values.update(
                {"SourceIndex": (await self._call("Product", "SourceIndex"))["Value"]}
            )
        self._apply(values)

    def _on_event(
        self, service: UpnpService, variables: list[UpnpStateVariable]
    ) -> None:
        changed = self._apply({var.name: var.value for var in variables})
        name = service.service_type.split(":")[-2]
        if (event := self._initial.get(name)) is not None:
            event.set()
        if changed and self.connected:
            self.on_update()

    def _apply(self, values: dict[str, Any]) -> bool:
        """Merge variables into the state; return whether anything changed."""
        state = self.state
        before = (
            state.transport_state,
            state.repeat,
            state.shuffle,
            state.can_repeat,
            state.can_shuffle,
            state.can_seek,
            state.track_uri,
            state.track,
            state.duration,
            state.codec,
            state.source_index,
        )
        if "TransportState" in values:
            state.transport_state = str(values["TransportState"])
        for name, attr in (
            ("Repeat", "repeat"),
            ("Shuffle", "shuffle"),
            ("CanRepeat", "can_repeat"),
            ("CanShuffle", "can_shuffle"),
            ("CanSeek", "can_seek"),
            ("CanPause", "can_pause"),
        ):
            if name in values and values[name] is not None:
                setattr(state, attr, _as_bool(values[name]))
        if values.get("StreamId") is not None:
            state.stream_id = int(values["StreamId"])
        if "Metadata" in values or "Uri" in values:
            state.track_uri = values.get("Uri", state.track_uri) or None
            state.track = parse_didl(values.get("Metadata") or "", self.url)
        if "CodecName" in values:
            state.codec = values["CodecName"] or None
        if values.get("SourceIndex") is not None:
            state.source_index = int(values["SourceIndex"])
        if values.get("Duration") is not None:
            state.duration = int(values["Duration"]) or None
        jumped = False
        if values.get("Seconds") is not None:
            seconds = int(values["Seconds"])
            expected = self.position_now()
            jumped = expected is None or abs(expected - seconds) > POSITION_TOLERANCE
            state.position, state.position_at = seconds, time.time()
        after = (
            state.transport_state,
            state.repeat,
            state.shuffle,
            state.can_repeat,
            state.can_shuffle,
            state.can_seek,
            state.track_uri,
            state.track,
            state.duration,
            state.codec,
            state.source_index,
        )
        return jumped or before != after

    def position_now(self) -> float | None:
        """Extrapolated position while playing."""
        state = self.state
        if state.position is None or state.position_at is None:
            return None
        if state.transport_state != "Playing":
            return state.position
        return state.position + time.time() - state.position_at

    # Actions ------------------------------------------------------------

    async def _call(self, service: str, action: str, **args: Any) -> dict[str, Any]:
        if service not in self._services:
            raise NetworkError(f"{self.url} has no OpenHome {service} service")
        return await self._services[service].action(action).async_call(**args)

    async def _act(self, service: str, action: str, **args: Any) -> dict[str, Any]:
        """Call an action, mapping transport errors to NetworkError."""
        if not self.connected:
            raise NetworkError(f"Not connected to {self.url}")
        try:
            return await self._call(service, action, **args)
        except (UpnpError, aiohttp.ClientError, OSError, TimeoutError) as err:
            raise NetworkError(f"{action} failed: {err}") from err

    async def async_set_repeat(self, repeat: bool) -> None:
        """Repeat on/off.

        Transport.Repeat is declared as a string ("true"/"false" observed).
        If that doesn't take, fall back to the Playlist service (boolean;
        the unit only accepted "1" there).
        """
        await self._act("Transport", "SetRepeat", Repeat="true" if repeat else "false")
        current = await self._act("Transport", "Repeat")
        if _as_bool(current.get("Repeat")) != repeat and "Playlist" in self._services:
            _LOGGER.debug("Transport.SetRepeat ignored; using Playlist.SetRepeat")
            await self._act("Playlist", "SetRepeat", Value=repeat)
            current = await self._act("Transport", "Repeat")
        self._apply({"Repeat": current.get("Repeat")})
        self.on_update()

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Shuffle on/off."""
        await self._act("Transport", "SetShuffle", Shuffle=shuffle)
        current = await self._act("Transport", "Shuffle")
        self._apply({"Shuffle": current.get("Shuffle")})
        self.on_update()

    async def async_seek(self, seconds: float) -> None:
        """Seek within the current track."""
        await self._act(
            "Transport",
            "SeekSecondAbsolute",
            StreamId=self.state.stream_id,
            SecondAbsolute=max(0, int(seconds)),
        )
        self._apply({"Seconds": int(seconds)})
        self.on_update()
