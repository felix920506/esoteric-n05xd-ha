"""Tests for the OpenHome network side against a simulated N-05XD."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from custom_components.esoteric import network as network_module
from custom_components.esoteric.network import (
    EsotericNetwork,
    NetworkError,
    async_probe,
    description_url,
    parse_didl,
)

from .fake_openhome import FakeOpenHome


async def _until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def fake():
    device = FakeOpenHome()
    await device.start()
    yield device
    await device.stop()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
async def net(fake: FakeOpenHome, session: aiohttp.ClientSession):
    updates: list[int] = []
    network = EsotericNetwork(
        session, fake.url, listen_host="127.0.0.1", on_update=lambda: updates.append(1)
    )
    network.updates = updates  # type: ignore[attr-defined]
    yield network
    await network.async_disconnect()


def test_description_url() -> None:
    assert description_url("172.20.0.120") == "http://172.20.0.120:23000/"
    assert description_url("172.20.0.120:1234") == "http://172.20.0.120:1234/"
    assert description_url(" http://h:1/desc.xml ") == "http://h:1/desc.xml"
    for bad in ("", "ftp://h", "http://"):
        with pytest.raises(ValueError):
            description_url(bad)


def test_parse_didl() -> None:
    from .fake_openhome import DIDL

    track = parse_didl(DIDL.format(title="T", artist="A", album="B"), "http://u:1/")
    assert (track.title, track.artist, track.album) == ("T", "A", "B")
    assert track.album_artist == "Various Artists"
    assert track.image_url == "http://u:1/art/T.jpg"
    assert parse_didl("", "http://u/").title is None
    # minidlna: track artist in dc:creator, album artist in a plain upnp:artist.
    minidlna = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"><item id="1">'
        "<dc:title>T</dc:title><dc:creator>Track Artist</dc:creator>"
        "<upnp:artist>Album Artist</upnp:artist><upnp:album>B</upnp:album>"
        "<upnp:albumArtURI>http://s/a.jpg</upnp:albumArtURI></item></DIDL-Lite>"
    )
    track = parse_didl(minidlna, "http://u/")
    assert (track.artist, track.album_artist) == ("Track Artist", "Album Artist")
    assert track.image_url == "http://s/a.jpg"
    assert parse_didl("<not xml", "http://u/").title is None


async def test_probe(fake: FakeOpenHome, session: aiohttp.ClientSession) -> None:
    udn, name = await async_probe(session, fake.url)
    assert udn == "uuid:4c494e4e-0000-0000-0000-000000000001"
    assert name == "ESOTERIC N-05XD:ESOTERIC"
    with pytest.raises(ValueError):
        await async_probe(session, fake.url + "Transport/x/scpd.xml")
    with pytest.raises(NetworkError):
        await async_probe(session, "http://127.0.0.1:1/")


async def test_connect_with_events(fake: FakeOpenHome, net: EsotericNetwork) -> None:
    await net.async_connect()
    assert net.connected and net.events
    assert len(fake.subscriptions) == 5
    state = net.state
    assert state.transport_state == "Playing"
    assert (state.track.title, state.track.artist) == ("Vivid Theory", "Arcaea")
    assert state.track.image_url == f"{fake.url}art/Vivid Theory.jpg"
    assert (state.duration, state.position, state.codec) == (245, 30, "FLAC")
    assert state.repeat is False and state.shuffle is False
    assert state.can_repeat and state.can_shuffle and state.can_seek
    # No polling needed when events work.
    assert "Info.Track" not in fake.calls

    # Changes made elsewhere arrive as events.
    net.updates.clear()  # type: ignore[attr-defined]
    fake.change(title="Glow", transport_state="Paused")
    await _until(lambda: net.state.track.title == "Glow")
    assert net.state.transport_state == "Paused"
    assert net.updates  # type: ignore[attr-defined]


async def test_position_ticks_do_not_spam(fake: FakeOpenHome, net: EsotericNetwork):
    await net.async_connect()
    net.updates.clear()  # type: ignore[attr-defined]
    fake.change(seconds=31)  # one-second tick while playing
    await _until(lambda: net.state.position == 31)
    assert not net.updates  # type: ignore[attr-defined]
    fake.change(seconds=200)  # seek / jump
    await _until(lambda: net.state.position == 200)
    assert net.updates  # type: ignore[attr-defined]


async def test_falls_back_to_polling(
    fake: FakeOpenHome, net: EsotericNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events blocked (e.g. firewall between VLANs): poll instead."""
    monkeypatch.setattr(network_module, "INITIAL_EVENT_TIMEOUT", 0.3)
    fake.send_events = False
    await net.async_connect()
    assert net.connected and not net.events
    assert not fake.subscriptions  # cleaned up
    assert net.state.track.title == "Vivid Theory"
    assert net.state.transport_state == "Playing"
    fake.change(title="Glow", transport_state="Paused")
    await net.async_refresh()
    assert net.state.track.title == "Glow"
    assert net.state.transport_state == "Paused"


async def test_subscription_refused_412(
    fake: FakeOpenHome, net: EsotericNetwork, caplog: pytest.LogCaptureFixture
) -> None:
    """Seen on the real unit with Home Assistant on another subnet."""
    fake.reject_subscriptions = True
    await net.async_connect()
    assert net.connected and not net.events
    assert net.callback_url is None  # listener cleaned up
    assert "only accepts callback addresses on its own subnet" in caplog.text
    assert net.state.track.title == "Vivid Theory"  # polled


async def test_set_repeat_shuffle_seek(fake: FakeOpenHome, net: EsotericNetwork):
    await net.async_connect()
    await net.async_set_repeat(True)
    assert fake.repeat == "true" and net.state.repeat is True
    assert "Playlist.SetRepeat" not in fake.calls
    await net.async_set_shuffle(True)
    assert fake.shuffle is True and net.state.shuffle is True
    await net.async_seek(120.6)
    assert fake.seconds == 120 and net.state.position == 120


async def test_set_repeat_falls_back_to_playlist(
    fake: FakeOpenHome, net: EsotericNetwork
) -> None:
    """If Transport.SetRepeat("true") is ignored, use Playlist.SetRepeat."""
    fake.transport_repeat_values = {}
    await net.async_connect()
    await net.async_set_repeat(True)
    assert "Playlist.SetRepeat" in fake.calls
    assert fake.repeat == "true" and net.state.repeat is True


async def test_unit_goes_away(fake: FakeOpenHome, net: EsotericNetwork) -> None:
    await net.async_connect()
    await fake.stop()
    with pytest.raises(NetworkError):
        await net.async_refresh()
    assert not net.connected
    with pytest.raises(NetworkError):
        await net.async_set_shuffle(True)


async def test_unreachable(session: aiohttp.ClientSession) -> None:
    network = EsotericNetwork(session, "http://127.0.0.1:1/", listen_host="127.0.0.1")
    with pytest.raises(NetworkError):
        await network.async_connect()
    assert not network.connected
