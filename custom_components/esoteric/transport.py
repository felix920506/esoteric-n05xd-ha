"""Pluggable byte transports.

A connection string selects a backend:

* ``socket://host:port`` (alias ``tcp://``, or bare ``host:port``) - raw TCP
  to a network serial server. 9600 8N1 must be configured on the server.
* ``rfc2217://host:port`` - Telnet COM Port Control (RFC 2217). The client
  tells the server which baud rate / framing to use.
* ``/dev/ttyUSB0``, ``COM3`` or ``serial:///dev/ttyUSB0`` - a local serial
  port (USB adapter etc.), via ``pyserial-asyncio-fast``.

New backends register themselves with :func:`register_backend`.

This module intentionally has no Home Assistant imports so it can be tested
in isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

_LOGGER = logging.getLogger(__name__)

SCHEME_SOCKET = "socket"
SCHEME_RFC2217 = "rfc2217"
SCHEME_SERIAL = "serial"
_SCHEME_ALIASES = {"tcp": SCHEME_SOCKET}
_WINDOWS_PORT = re.compile(r"^(?:\\\\\.\\)?COM\d+$", re.IGNORECASE)

READ_CHUNK = 1024


class InvalidUrlError(ValueError):
    """The connection string could not be parsed."""


@dataclass(frozen=True)
class ConnectionSpec:
    """A parsed connection string."""

    scheme: str
    host: str | None = None
    port: int | None = None
    path: str | None = None

    @property
    def is_network(self) -> bool:
        """Whether this goes over TCP."""
        return self.host is not None

    def __str__(self) -> str:
        if self.path is not None:
            return self.path
        host = f"[{self.host}]" if self.host and ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


def parse_url(url: str) -> ConnectionSpec:
    """Parse a connection string into a :class:`ConnectionSpec`."""
    url = url.strip()
    if not url:
        raise InvalidUrlError("Empty connection string")
    if url.startswith("/") or _WINDOWS_PORT.match(url):
        return ConnectionSpec(SCHEME_SERIAL, path=url)
    if "://" not in url:
        url = f"{SCHEME_SOCKET}://{url}"
    scheme, rest = url.split("://", 1)
    scheme = _SCHEME_ALIASES.get(scheme.lower(), scheme.lower())
    if scheme not in _BACKENDS:
        raise InvalidUrlError(f"Unsupported scheme {scheme!r}")
    if scheme == SCHEME_SERIAL:
        if not rest:
            raise InvalidUrlError("Missing serial device path")
        return ConnectionSpec(SCHEME_SERIAL, path=rest)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as err:
        raise InvalidUrlError(str(err)) from err
    if not parts.hostname:
        raise InvalidUrlError("Missing host")
    if port is None:
        raise InvalidUrlError("Missing port")
    return ConnectionSpec(scheme, host=parts.hostname, port=port)


class Transport:
    """A bidirectional byte stream built on asyncio streams.

    Subclasses implement :meth:`_open_streams`; framing-aware backends
    (RFC 2217) also override :meth:`read` / :meth:`write`.
    """

    def __init__(self, spec: ConnectionSpec, baudrate: int) -> None:
        """Initialize."""
        self.spec = spec
        self.baudrate = baudrate
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _open_streams(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise NotImplementedError

    async def open(self) -> None:
        """Open the connection."""
        self._reader, self._writer = await self._open_streams()

    async def read(self) -> bytes:
        """Read payload bytes. Raises ConnectionError on EOF."""
        if self._reader is None:
            raise ConnectionError("Not connected")
        data = await self._reader.read(READ_CHUNK)
        if not data:
            raise ConnectionError(f"Connection to {self.spec} closed")
        return data

    async def write(self, data: bytes) -> None:
        """Write payload bytes."""
        await self._write_raw(data)

    async def _write_raw(self, data: bytes) -> None:
        if self._writer is None or self._writer.is_closing():
            raise ConnectionError("Not connected")
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        """Close the connection."""
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), 2)


TransportFactory = Callable[[ConnectionSpec, int], Transport]
_BACKENDS: dict[str, TransportFactory] = {}


def register_backend(scheme: str, factory: TransportFactory) -> None:
    """Register a transport backend for a URL scheme."""
    _BACKENDS[scheme] = factory


def create_transport(url: str | ConnectionSpec, baudrate: int) -> Transport:
    """Create the transport matching the connection string."""
    spec = url if isinstance(url, ConnectionSpec) else parse_url(url)
    return _BACKENDS[spec.scheme](spec, baudrate)


class TcpTransport(Transport):
    """Raw TCP to a network serial server (``socket://``)."""

    async def _open_streams(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection(self.spec.host, self.spec.port)
        if (sock := writer.get_extra_info("socket")) is not None:
            _enable_keepalive(sock)
        return reader, writer


def _enable_keepalive(sock: socket.socket) -> None:
    """Detect half-open connections to the serial server."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        elif hasattr(socket, "TCP_KEEPALIVE"):  # macOS
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 30)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except OSError as err:  # pragma: no cover - platform specific
        _LOGGER.debug("Could not enable TCP keepalive: %s", err)


class SerialPortTransport(Transport):
    """A local serial port (``/dev/ttyUSB0``, ``COM3``) at <baudrate> 8N1."""

    async def _open_streams(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # Imported lazily: only needed for local ports, and lets the TCP
        # backends be used/tested without pyserial installed.
        import serial  # noqa: PLC0415
        import serial_asyncio_fast  # noqa: PLC0415

        try:
            return await serial_asyncio_fast.open_serial_connection(
                url=self.spec.path,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        except serial.SerialException as err:
            raise OSError(str(err)) from err


# Telnet / RFC 2217 constants
IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240

OPT_BINARY = 0
OPT_SGA = 3
OPT_COM_PORT = 44

# Client -> server COM-PORT-OPTION sub-commands (server replies add 100)
CPO_SET_BAUDRATE = 1
CPO_SET_DATASIZE = 2
CPO_SET_PARITY = 3
CPO_SET_STOPSIZE = 4
CPO_SET_CONTROL = 5
CPO_SERVER_OFFSET = 100

PARITY_NONE = 1
STOPSIZE_1 = 1
CONTROL_NO_FLOW = 1
NEGOTIATION_TIMEOUT = 3.0

# Options we offer / accept in both directions.
_SUPPORTED_OPTIONS = frozenset({OPT_BINARY, OPT_SGA, OPT_COM_PORT})


class _TelnetState:
    """Parser states."""

    DATA = object()
    IAC = object()
    NEGOTIATE = object()
    SUB = object()
    SUB_IAC = object()


class Rfc2217Transport(TcpTransport):
    """Telnet RFC 2217: TCP plus client-side baud rate / 8N1 configuration."""

    def __init__(self, spec: ConnectionSpec, baudrate: int) -> None:
        """Initialize."""
        super().__init__(spec, baudrate)
        self._state = _TelnetState.DATA
        self._command = 0
        self._sub = bytearray()
        self._settings_sent = False
        self._refused: set[tuple[int, int]] = set()
        self.server_settings: dict[str, int] = {}
        self.com_port_refused = False
        self._early_payload = bytearray()

    async def open(self) -> None:
        """Open the connection and negotiate the serial settings."""
        await super().open()
        self._state = _TelnetState.DATA
        self._settings_sent = False
        self._refused.clear()
        self.server_settings.clear()
        self.com_port_refused = False
        self._early_payload.clear()
        await self._write_raw(
            bytes(
                [IAC, WILL, OPT_BINARY, IAC, DO, OPT_BINARY]
                + [IAC, WILL, OPT_SGA, IAC, DO, OPT_SGA]
                + [IAC, WILL, OPT_COM_PORT]
            )
        )
        # Don't let commands go out before the baud rate is set: wait for the
        # server to accept (and ideally confirm) COM-PORT-OPTION.
        try:
            async with asyncio.timeout(NEGOTIATION_TIMEOUT):
                while not (self.com_port_refused or "baudrate" in self.server_settings):
                    data = await super().read()
                    payload, replies = self.process_incoming(data)
                    self._early_payload += payload
                    if replies:
                        await self._write_raw(replies)
        except TimeoutError:
            if self._settings_sent:
                _LOGGER.debug("%s did not confirm the baud rate", self.spec)
                return
            _LOGGER.warning(
                "%s did not confirm RFC 2217 settings; is it really an RFC 2217 "
                "server? Continuing anyway",
                self.spec,
            )

    async def read(self) -> bytes:
        """Read payload bytes with telnet commands removed."""
        if self._early_payload:
            payload, self._early_payload = bytes(self._early_payload), bytearray()
            return payload
        while True:
            data = await super().read()
            payload, replies = self.process_incoming(data)
            if replies:
                await self._write_raw(replies)
            if payload:
                return payload

    async def write(self, data: bytes) -> None:
        """Write payload bytes, escaping IAC."""
        await self._write_raw(data.replace(bytes([IAC]), bytes([IAC, IAC])))

    def settings_payload(self) -> bytes:
        """COM-PORT-OPTION sub-negotiations for baudrate and 8N1."""

        def sub(cmd: int, value: bytes) -> bytes:
            value = value.replace(bytes([IAC]), bytes([IAC, IAC]))
            return bytes([IAC, SB, OPT_COM_PORT, cmd]) + value + bytes([IAC, SE])

        return (
            sub(CPO_SET_BAUDRATE, struct.pack("!I", self.baudrate))
            + sub(CPO_SET_DATASIZE, bytes([8]))
            + sub(CPO_SET_PARITY, bytes([PARITY_NONE]))
            + sub(CPO_SET_STOPSIZE, bytes([STOPSIZE_1]))
            + sub(CPO_SET_CONTROL, bytes([CONTROL_NO_FLOW]))
        )

    def process_incoming(self, data: bytes) -> tuple[bytes, bytes]:
        """Split incoming bytes into (payload, bytes to send back)."""
        payload = bytearray()
        replies = bytearray()
        for byte in data:
            state = self._state
            if state is _TelnetState.DATA:
                if byte == IAC:
                    self._state = _TelnetState.IAC
                else:
                    payload.append(byte)
            elif state is _TelnetState.IAC:
                if byte == IAC:
                    payload.append(IAC)
                    self._state = _TelnetState.DATA
                elif byte in (WILL, WONT, DO, DONT):
                    self._command = byte
                    self._state = _TelnetState.NEGOTIATE
                elif byte == SB:
                    self._sub.clear()
                    self._state = _TelnetState.SUB
                else:  # NOP, GA, etc.
                    self._state = _TelnetState.DATA
            elif state is _TelnetState.NEGOTIATE:
                replies += self._negotiate(self._command, byte)
                self._state = _TelnetState.DATA
            elif state is _TelnetState.SUB:
                if byte == IAC:
                    self._state = _TelnetState.SUB_IAC
                else:
                    self._sub.append(byte)
            elif state is _TelnetState.SUB_IAC:
                if byte == SE:
                    self._handle_sub(bytes(self._sub))
                    self._state = _TelnetState.DATA
                else:  # IAC IAC inside sub-negotiation
                    self._sub.append(byte)
                    self._state = _TelnetState.SUB
        return bytes(payload), bytes(replies)

    def _negotiate(self, command: int, option: int) -> bytes:
        if option == OPT_COM_PORT:
            if command == DO and not self._settings_sent:
                self._settings_sent = True
                _LOGGER.debug(
                    "%s accepted COM-PORT-OPTION, setting %s 8N1",
                    self.spec,
                    self.baudrate,
                )
                return self.settings_payload()
            if command == DONT:
                self.com_port_refused = True
                _LOGGER.warning(
                    "%s refused RFC 2217 COM-PORT-OPTION; configure %s 8N1 on "
                    "the serial server and use socket:// instead",
                    self.spec,
                    self.baudrate,
                )
            return b""
        if option in _SUPPORTED_OPTIONS:
            # Either an answer to our own request or a request we already
            # agreed to; never reply, to avoid negotiation loops.
            return b""
        # Refuse anything else, once per (command, option).
        if command in (DO, WILL) and (command, option) not in self._refused:
            self._refused.add((command, option))
            return bytes([IAC, WONT if command == DO else DONT, option])
        return b""

    def _handle_sub(self, sub: bytes) -> None:
        if len(sub) < 3 or sub[0] != OPT_COM_PORT:
            return
        names = {
            CPO_SET_BAUDRATE: "baudrate",
            CPO_SET_DATASIZE: "datasize",
            CPO_SET_PARITY: "parity",
            CPO_SET_STOPSIZE: "stopsize",
            CPO_SET_CONTROL: "control",
        }
        cmd, value = sub[1] - CPO_SERVER_OFFSET, sub[2:]
        if cmd not in names:
            return
        number = struct.unpack("!I", value[:4])[0] if len(value) >= 4 else value[0]
        self.server_settings[names[cmd]] = number
        _LOGGER.debug("%s confirmed %s=%s", self.spec, names[cmd], number)


register_backend(SCHEME_SOCKET, TcpTransport)
register_backend(SCHEME_RFC2217, Rfc2217Transport)
register_backend(SCHEME_SERIAL, SerialPortTransport)
