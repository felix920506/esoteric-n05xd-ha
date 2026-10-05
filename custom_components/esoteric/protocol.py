"""Esoteric RS-232C protocol client (command table rev 1.5).

Frames:
    host -> unit   ``@<COMMAND>[<SP><ARG>...]<CR>``   e.g. ``@KEY 01\\r``
    host -> unit   ``@?<KEY><CR>``                    e.g. ``@?INPUT\\r``
    unit -> host   ``0x06`` (ACK) / ``0x15`` (NAK)    for normal commands
    unit -> host   ``@<KEY><SP><VALUE>...<CR>``       for requests

Shared serial servers
---------------------
Some serial servers let several TCP clients share one serial port and copy
everything the unit sends to every client. This client is written so that it
behaves sensibly in that environment:

* Every status line received is parsed and published to listeners, no matter
  who asked for it, so other clients' requests keep our state fresh for free.
* A request completes when *any* line with the requested key arrives; a reply
  triggered by another client carries the same information.
* ACK / NAK carry no identifier. A bare ACK/NAK with nothing of ours in
  flight is dropped. One that arrives while we wait may belong to another
  client; we accept that ambiguity rather than block on it.
* Lines that look like *commands* (``@?INPUT``, ``@KEY 01``), as echoed by some
  servers, are ignored.

No Home Assistant imports here so the module can be tested in isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .transport import ConnectionSpec, Transport, create_transport, parse_url

_LOGGER = logging.getLogger(__name__)

START = b"@"
CR = 0x0D
LF = 0x0A
ACK = 0x06
NAK = 0x15

DEFAULT_BAUDRATE = 9600
DEFAULT_RESPONSE_TIMEOUT = 1.0
# Spec: wait at least 20 ms after the ACK before sending the next command.
COMMAND_INTERVAL = 0.03
MAX_LINE = 256
RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0

# Keys that only ever appear as host -> unit commands. If a server echoes
# client traffic we must not mistake these for status.
_COMMAND_ONLY_KEYS = frozenset({"KEY", "POWER"})


class EsotericError(Exception):
    """Base error."""


class NotConnectedError(EsotericError):
    """The serial server connection is down."""


class ConnectionDroppedError(EsotericError):
    """The server accepted the connection and closed it straight away."""


class CommandRejectedError(EsotericError):
    """The unit answered NAK."""


class ResponseTimeoutError(EsotericError):
    """The unit did not answer in time."""


@dataclass(frozen=True)
class Message:
    """A status line from the unit, e.g. ``@PSTS PLAY 3 1 23 TE``."""

    key: str
    args: tuple[str, ...]
    raw: str

    @property
    def value(self) -> str:
        """All arguments joined with spaces."""
        return " ".join(self.args)


@dataclass(frozen=True)
class AckEvent:
    """ACK (0x06) or NAK (0x15) byte."""

    ack: bool


Event = Message | AckEvent


def parse_line(line: str) -> Message | None:
    """Parse one CR-terminated line (without the CR).

    Returns None for blank lines, garbage and echoed host commands.
    """
    start = line.find("@")
    if start < 0:
        return None
    body = line[start + 1 :].strip()
    if not body or body.startswith("?"):
        return None
    key, *args = body.split()
    if key in _COMMAND_ONLY_KEYS:
        return None
    return Message(key=key, args=tuple(args), raw=line[start:].strip())


class FrameParser:
    """Turn a byte stream into ACK/NAK events and status messages."""

    def __init__(self) -> None:
        """Initialize."""
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Event]:
        """Consume bytes and return the complete events found."""
        events: list[Event] = []
        for byte in data:
            if byte == ACK:
                events.append(AckEvent(True))
            elif byte == NAK:
                events.append(AckEvent(False))
            elif byte == CR:
                if self._buffer:
                    line = self._buffer.decode("ascii", errors="replace")
                    self._buffer.clear()
                    if (message := parse_line(line)) is not None:
                        events.append(message)
            elif byte == LF:
                continue
            elif len(self._buffer) >= MAX_LINE:
                _LOGGER.debug("Discarding over-long line %r", bytes(self._buffer))
                self._buffer.clear()
            elif byte == START[0] and self._buffer:
                # A new frame started before the previous one ended (lost CR,
                # or interleaved garbage); drop the partial frame.
                _LOGGER.debug("Discarding partial frame %r", bytes(self._buffer))
                self._buffer[:] = START
            else:
                self._buffer.append(byte)
        return events


def build_command(command: str) -> bytes:
    """Frame a command. Accepts ``KEY 01``, ``@KEY 01`` or ``?INPUT``."""
    command = command.strip()
    if command.startswith("@"):
        command = command[1:]
    if not command or not command.isascii() or "\r" in command:
        raise ValueError(f"Invalid command {command!r}")
    return b"@" + command.encode("ascii") + b"\r"


@dataclass
class _Pending:
    """What the in-flight command is waiting for."""

    key: str | None  # None -> waiting for ACK/NAK
    future: asyncio.Future[Message | None]


@dataclass
class ClientStats:
    """Counters, exposed through diagnostics."""

    connects: int = 0
    disconnects: int = 0
    acks: int = 0
    naks: int = 0
    stray_acks: int = 0
    timeouts: int = 0
    messages: int = 0
    last_error: str | None = None
    last_message_at: float | None = None
    extra: dict[str, object] = field(default_factory=dict)


class EsotericClient:
    """Connection to one Esoteric unit (network serial server or local port)."""

    def __init__(
        self,
        url: str | ConnectionSpec,
        baudrate: int = DEFAULT_BAUDRATE,
        *,
        response_timeout: float = DEFAULT_RESPONSE_TIMEOUT,
        connect_timeout: float = 10.0,
    ) -> None:
        """Initialize."""
        self.url = url if isinstance(url, ConnectionSpec) else parse_url(url)
        self.baudrate = baudrate
        self.response_timeout = response_timeout
        self.connect_timeout = connect_timeout
        self.stats = ClientStats()
        self._transport: Transport | None = None
        self._parser = FrameParser()
        self._lock = asyncio.Lock()
        self._pending: _Pending | None = None
        self._last_exchange = 0.0
        self._listeners: list[Callable[[Message], None]] = []
        self._connection_listeners: list[Callable[[bool], None]] = []
        self._runner: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._stopping = False

    @property
    def connected(self) -> bool:
        """Whether the TCP connection to the serial server is up."""
        return self._connected.is_set()

    def add_listener(self, callback: Callable[[Message], None]) -> Callable[[], None]:
        """Call ``callback`` for every status line received."""
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def add_connection_listener(
        self, callback: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Call ``callback(connected)`` whenever the connection state changes."""
        self._connection_listeners.append(callback)
        return lambda: self._connection_listeners.remove(callback)

    async def connect(self) -> None:
        """Connect once, without starting the background reconnect loop.

        Used by the config flow to validate the URL.
        """
        transport = create_transport(self.url, self.baudrate)
        await asyncio.wait_for(transport.open(), self.connect_timeout)
        self._transport = transport

    async def check_connection(self, linger: float = 0.5) -> None:
        """Connect, then make sure the server doesn't drop us right away.

        Serial servers that allow only one client per port often accept the
        TCP connection and reset it immediately when the port is in use.
        Raises ConnectionDroppedError in that case. Closes the connection.
        """
        await self.connect()
        assert self._transport is not None
        try:
            await asyncio.wait_for(self._transport.read(), linger)
        except TimeoutError:
            pass  # still connected, nothing sent: fine
        except OSError as err:
            raise ConnectionDroppedError(str(err)) from err
        finally:
            await self._close()

    async def start(self) -> None:
        """Start the background connect / read / reconnect loop."""
        self._stopping = False
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._run(), name=f"esoteric {self.url}")

    async def wait_connected(self, timeout: float) -> bool:
        """Wait until connected; return False on timeout."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        """Stop the loop and close the connection."""
        self._stopping = True
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None
        await self._close()

    async def _close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()

    async def _run(self) -> None:
        delay = RECONNECT_MIN
        while not self._stopping:
            try:
                await self.connect()
            except (OSError, TimeoutError) as err:
                self.stats.last_error = f"connect: {err!r}"
                _LOGGER.debug("Connecting to %s failed: %r", self.url, err)
            else:
                delay = RECONNECT_MIN
                self.stats.connects += 1
                _LOGGER.info("Connected to %s", self.url)
                self._set_connected(True)
                try:
                    await self._read_loop()
                except (OSError, ConnectionError) as err:
                    self.stats.last_error = f"read: {err!r}"
                    _LOGGER.warning("Lost connection to %s: %s", self.url, err)
                finally:
                    self.stats.disconnects += 1
                    self._set_connected(False)
                    await self._close()
            if self._stopping:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    def _set_connected(self, connected: bool) -> None:
        if connected:
            self._parser = FrameParser()
            self._connected.set()
        else:
            self._connected.clear()
            if self._pending and not self._pending.future.done():
                self._pending.future.set_exception(NotConnectedError("Connection lost"))
        for callback in list(self._connection_listeners):
            try:
                callback(connected)
            except Exception:
                _LOGGER.exception("Error in connection listener")

    async def _read_loop(self) -> None:
        while True:
            assert self._transport is not None
            data = await self._transport.read()
            _LOGGER.debug("%s <- %r", self.url, data)
            for event in self._parser.feed(data):
                self._dispatch(event)

    def _dispatch(self, event: Event) -> None:
        pending = self._pending
        if pending is not None and pending.future.done():
            pending = None
        if isinstance(event, AckEvent):
            if pending is None:
                self.stats.stray_acks += 1
                return
            if not event.ack:
                self.stats.naks += 1
                pending.future.set_exception(CommandRejectedError("NAK"))
            elif pending.key is None:
                self.stats.acks += 1
                pending.future.set_result(None)
            else:
                # An ACK while we wait for a status line belongs to another
                # client's normal command.
                self.stats.stray_acks += 1
            return

        self.stats.messages += 1
        self.stats.last_message_at = time.monotonic()
        if pending is not None and pending.key == event.key:
            pending.future.set_result(event)
        for callback in list(self._listeners):
            try:
                callback(event)
            except Exception:
                _LOGGER.exception("Error in message listener")

    async def _exchange(self, command: str, key: str | None) -> Message | None:
        frame = build_command(command)
        async with self._lock:
            if not self.connected or self._transport is None:
                raise NotConnectedError(f"Not connected to {self.url}")
            wait = self._last_exchange + COMMAND_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            future: asyncio.Future[Message | None] = (
                asyncio.get_running_loop().create_future()
            )
            self._pending = _Pending(key, future)
            try:
                _LOGGER.debug("%s -> %r", self.url, frame)
                await self._transport.write(frame)
                return await asyncio.wait_for(future, self.response_timeout)
            except TimeoutError as err:
                self.stats.timeouts += 1
                raise ResponseTimeoutError(
                    f"No response to {command!r} from {self.url}"
                ) from err
            except OSError as err:
                raise NotConnectedError(str(err)) from err
            finally:
                self._pending = None
                self._last_exchange = time.monotonic()

    async def send_command(self, command: str) -> None:
        """Send a normal command (e.g. ``KEY 01``) and wait for ACK.

        Raises CommandRejectedError on NAK and ResponseTimeoutError if
        nothing arrives.
        """
        await self._exchange(command, None)

    async def request(self, key: str) -> Message:
        """Send ``@?<key>`` and return the matching status line."""
        key = key.lstrip("@?")
        message = await self._exchange(f"?{key}", key)
        assert message is not None
        return message

    async def send_raw(self, command: str) -> Message | None:
        """Send a command or request given as text.

        ``?INPUT`` / ``@?INPUT`` returns the status line; anything else
        returns None once ACKed.
        """
        body = command.strip().lstrip("@")
        if body.startswith("?"):
            return await self.request(body[1:])
        await self.send_command(body)
        return None
