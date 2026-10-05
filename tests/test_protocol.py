"""Protocol and transport tests (no Home Assistant needed)."""

from __future__ import annotations

import asyncio
import os
import struct
import tty

import pytest

from custom_components.esoteric.protocol import (
    AckEvent,
    CommandRejectedError,
    EsotericClient,
    FrameParser,
    Message,
    NotConnectedError,
    ResponseTimeoutError,
    build_command,
    parse_line,
)
from custom_components.esoteric.transport import (
    DO,
    IAC,
    OPT_COM_PORT,
    SB,
    SE,
    WONT,
    ConnectionSpec,
    InvalidUrlError,
    Rfc2217Transport,
    SerialPortTransport,
    TcpTransport,
    create_transport,
    parse_url,
)

from .fake_device import FakeEsoteric


async def _until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


# --- framing -----------------------------------------------------------------


def test_parse_line() -> None:
    assert parse_line("@INPUT DISC") == Message("INPUT", ("DISC",), "@INPUT DISC")
    assert parse_line("@MQA MQA. 192kHz").args == ("MQA.", "192kHz")
    assert parse_line("\x00junk@FS 44.1kHz").value == "44.1kHz"
    # Echoed host commands and garbage are not status.
    assert parse_line("@?INPUT") is None
    assert parse_line("@KEY 01") is None
    assert parse_line("@POWER ON") is None
    assert parse_line("no frame") is None
    assert parse_line("@") is None


def test_frame_parser_interleaving() -> None:
    parser = FrameParser()
    events = parser.feed(b"\x06@VOLU")
    assert events == [AckEvent(True)]
    events = parser.feed(b"ME 23.5\r\n\x15@PSTS PLAY 3 1 23 TE\r")
    assert events == [
        Message("VOLUME", ("23.5",), "@VOLUME 23.5"),
        AckEvent(False),
        Message("PSTS", ("PLAY", "3", "1", "23", "TE"), "@PSTS PLAY 3 1 23 TE"),
    ]


def test_frame_parser_recovers_from_lost_cr() -> None:
    parser = FrameParser()
    assert parser.feed(b"@INPUT US@INPUT XLR\r") == [
        Message("INPUT", ("XLR",), "@INPUT XLR")
    ]
    parser.feed(b"x" * 1000)
    assert parser.feed(b"\r@FS 48kHz\r") == [Message("FS", ("48kHz",), "@FS 48kHz")]


def test_build_command() -> None:
    assert build_command("KEY 01") == b"@KEY 01\r"
    assert build_command("@?INPUT") == b"@?INPUT\r"
    with pytest.raises(ValueError):
        build_command("")
    with pytest.raises(ValueError):
        build_command("VOLUME 1\r@POWER OFF")


# --- connection strings --------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("socket://10.0.0.5:4001", ConnectionSpec("socket", "10.0.0.5", 4001)),
        ("tcp://host:23", ConnectionSpec("socket", "host", 23)),
        ("10.0.0.5:4001", ConnectionSpec("socket", "10.0.0.5", 4001)),
        ("RFC2217://h:2217", ConnectionSpec("rfc2217", "h", 2217)),
        ("/dev/ttyUSB0", ConnectionSpec("serial", path="/dev/ttyUSB0")),
        ("COM3", ConnectionSpec("serial", path="COM3")),
        ("serial:///dev/ttyS1", ConnectionSpec("serial", path="/dev/ttyS1")),
    ],
)
def test_parse_url(url: str, expected: ConnectionSpec) -> None:
    assert parse_url(url) == expected
    # str() round-trips
    assert parse_url(str(expected)) == expected


@pytest.mark.parametrize(
    "url", ["", "http://h:1", "socket://h", "socket://:1", "serial://", "h:notaport"]
)
def test_parse_url_invalid(url: str) -> None:
    with pytest.raises(InvalidUrlError):
        parse_url(url)


def test_create_transport_backends() -> None:
    assert type(create_transport("socket://h:1", 9600)) is TcpTransport
    assert type(create_transport("rfc2217://h:1", 9600)) is Rfc2217Transport
    assert type(create_transport("/dev/ttyUSB0", 9600)) is SerialPortTransport


# --- RFC 2217 ------------------------------------------------------------------


def test_rfc2217_negotiation() -> None:
    transport = Rfc2217Transport(parse_url("rfc2217://h:1"), 9600)
    payload, replies = transport.process_incoming(
        bytes([IAC, DO, OPT_COM_PORT, ord("@"), IAC, IAC, IAC, DO, 24, IAC, DO, 24])
    )
    assert payload == b"@\xff"
    assert replies.startswith(transport.settings_payload())
    assert struct.pack("!I", 9600) in transport.settings_payload()
    # Unknown option refused exactly once.
    assert replies.endswith(bytes([IAC, WONT, 24]))
    assert replies.count(bytes([IAC, WONT, 24])) == 1
    # A repeated DO COM-PORT does not resend settings (no loops).
    _, replies = transport.process_incoming(bytes([IAC, DO, OPT_COM_PORT]))
    assert replies == b""
    # Server confirmation, split across reads.
    transport.process_incoming(bytes([IAC, SB, OPT_COM_PORT, 101, 0, 0]))
    payload, _ = transport.process_incoming(bytes([0x25, 0x80, IAC, SE]) + b"\x06")
    assert payload == b"\x06"
    assert transport.server_settings == {"baudrate": 9600}


# --- client against the fake unit ---------------------------------------------


@pytest.fixture
async def device():
    fake = FakeEsoteric()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def client(device: FakeEsoteric):
    esoteric = EsotericClient(device.url, response_timeout=0.3)
    await esoteric.start()
    assert await esoteric.wait_connected(3)
    yield esoteric
    await esoteric.stop()


async def test_request_and_command(client: EsotericClient, device: FakeEsoteric):
    seen: list[Message] = []
    client.add_listener(seen.append)
    message = await client.request("VOLUME")
    assert message.value == "30.0"
    await client.send_command("VOLUME 42.5")
    assert device.values["VOLUME"] == "42.5"
    assert (await client.send_raw("?VOLUME")).value == "42.5"
    assert await client.send_raw("@KEY 5A") is None
    assert [m.value for m in seen] == ["30.0", "42.5"]
    assert device.received == [
        "@?VOLUME",
        "@VOLUME 42.5",
        "@?VOLUME",
        "@KEY 5A",
    ]


async def test_nak_and_timeout(client: EsotericClient, device: FakeEsoteric):
    with pytest.raises(CommandRejectedError):
        await client.request("MEDIA")
    with pytest.raises(CommandRejectedError):
        await client.send_command("BOGUS")
    device.silent = True
    with pytest.raises(ResponseTimeoutError):
        await client.request("INPUT")
    assert client.stats.timeouts == 1


async def test_shared_port_other_client_traffic(
    client: EsotericClient, device: FakeEsoteric
):
    """Another client's replies update us; its ACK/NAK do not confuse us."""
    seen: list[Message] = []
    client.add_listener(seen.append)
    _, other = await asyncio.open_connection("127.0.0.1", device.port)
    await _until(lambda: device.client_count == 2)

    other.write(b"@?CODEC\r@KEY 5A\r@?MEDIA\r")
    await other.drain()
    await _until(lambda: len(device.received) == 3)
    await _until(lambda: client.stats.stray_acks == 2)
    assert [m.raw for m in seen] == ["@CODEC FLAC"]

    # Our traffic still works afterwards.
    assert (await client.request("INPUT")).value == "NET"
    other.close()


async def test_reply_triggered_by_other_client_completes_request(
    client: EsotericClient, device: FakeEsoteric
):
    device.silent = True
    task = asyncio.create_task(client.request("FS"))
    await _until(lambda: device.received == ["@?FS"])
    # Another client's request produces the same line; that answers ours.
    device.send(b"@FS 96kHz\r")
    assert (await task).value == "96kHz"


async def test_echoing_server_is_tolerated(
    client: EsotericClient, device: FakeEsoteric
):
    """Servers that echo client data back must not break matching."""
    seen: list[Message] = []
    client.add_listener(seen.append)
    device.send(b"@?INPUT\r@KEY 01\r@POWER OFF\r")
    assert (await client.request("INPUT")).value == "NET"
    assert [m.key for m in seen] == ["INPUT"]


async def test_reconnects(client: EsotericClient, device: FakeEsoteric):
    states: list[bool] = []
    client.add_connection_listener(states.append)
    await device.drop_clients()
    await _until(lambda: states == [False])
    with pytest.raises(NotConnectedError):
        await client.request("INPUT")
    await _until(lambda: states == [False, True], timeout=5)
    assert (await client.request("INPUT")).value == "NET"


async def test_rfc2217_end_to_end():
    device = FakeEsoteric(rfc2217=True)
    await device.start()
    client = EsotericClient(device.url, 9600, response_timeout=0.5)
    try:
        await client.start()
        assert await client.wait_connected(3)
        assert (await client.request("VOLUME")).value == "30.0"
        assert device.baudrate == 9600
        assert client._transport.server_settings == {"baudrate": 9600}  # noqa: SLF001
    finally:
        await client.stop()
        await device.stop()


async def test_local_serial_port_backend():
    """The serial backend works against a real tty (a pseudo-terminal)."""
    master, slave = os.openpty()
    tty.setraw(master)
    path = os.ttyname(slave)
    client = EsotericClient(path, 9600, response_timeout=1)
    loop = asyncio.get_running_loop()
    received = bytearray()

    def on_readable() -> None:
        received.extend(os.read(master, 1024))
        if received.endswith(b"\r"):
            assert bytes(received) == b"@?INPUT\r"
            os.write(master, b"@INPUT XLR\r")
            received.clear()

    loop.add_reader(master, on_readable)
    try:
        await client.start()
        assert await client.wait_connected(3)
        assert (await client.request("INPUT")).value == "XLR"
    finally:
        loop.remove_reader(master)
        await client.stop()
        os.close(master)
        os.close(slave)
