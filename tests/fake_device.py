"""A simulated Esoteric unit behind a network serial server.

Like a shared-port serial server, everything the unit sends (status lines,
ACK, NAK) is copied to every connected client.
"""

from __future__ import annotations

import asyncio
import struct
import time

ACK = b"\x06"
NAK = b"\x15"

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
COM_PORT = 44


# What a real N-05XD reports in standby.
STANDBY_VALUES = {
    "INPUT": "OFF",
    "AOUT": "OFF",
    "PSTS": "STOP 0 0 00 TE",
    "PMODE": "CONTINUE",
    "REPEAT": "OFF",
    "FS": "NON",
    "CODEC": "",
    "MQA": "NON",
}


class FakeEsoteric:
    """Simulated unit; defaults mirror a real N-05XD's observed behavior."""

    def __init__(
        self,
        *,
        rfc2217: bool = False,
        broadcast: bool = True,
        # As a real N-05XD answered while stopped on NET:
        # DOUT -> DOC-MISMATCH-01, UPCONV -> DOC-MISMATCH-02.
        unsupported: tuple[str, ...] = ("MEDIA", "DOUT", "UPCONV"),
        direct_input: bool = False,
        toggle_keys: bool = False,
        single_client: bool = False,
    ) -> None:
        """Initialize."""
        self.rfc2217 = rfc2217
        self.broadcast = broadcast
        self.direct_input = direct_input
        self.toggle_keys = toggle_keys
        self.single_client = single_client
        self.power = True
        # Standby behavior. "report": answer with defaults and "@INPUT OFF"
        # and accept commands, as a real N-05XD does. "nak": NAK everything.
        # "silent": answer nothing.
        self.standby_mode = "report"
        # A real N-05XD answers POWER ON with 0x83 instead of ACK
        # (DOC-MISMATCH-06).
        self.power_on_reply = b"\x83"
        # A real N-05XD ACKs but ignores POWER ON for ~6-8 s after POWER OFF
        # (DOC-MISMATCH-08).
        self.power_on_lockout = 0.0
        self._off_at = 0.0
        self.inputs = [
            "NET",
            "Bluetooth",
            "LINE1",
            "LINE2",
            "XLR",
            "COAX1",
            "COAX2",
            "OPT1",
            "OPT2",
            "USB",
        ]
        self.values = {
            "INPUT": "NET",
            "AOUT": "XLR",
            "DOUT": "OFF",
            "PSTS": "PLAY 3 1 23 TE",
            "PMODE": "CONTINUE",
            "REPEAT": "OFF",
            "UPCONV": "OFF",
            "FS": "44.1kHz",
            "CODEC": "FLAC",
            "MQA": "NON",
            "VOLUME": "30.0",
        }
        self.unsupported = set(unsupported)
        self.received: list[str] = []
        self.baudrate: int | None = None
        self.silent = False
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> int:
        """Listen on a random port."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    @property
    def url(self) -> str:
        """Connection string."""
        scheme = "rfc2217" if self.rfc2217 else "socket"
        return f"{scheme}://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        """Shut down."""
        await self.drop_clients()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def drop_clients(self) -> None:
        """Close every client connection (simulates a server reboot)."""
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()

    @property
    def client_count(self) -> int:
        """Connected clients."""
        return len(self._writers)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self.single_client and self._writers:
            # Like the real STE serial server: accept, then reset.
            writer.transport.abort()
            return
        self._writers.append(writer)
        buffer = bytearray()
        try:
            while data := await reader.read(1024):
                if self.rfc2217:
                    data = self._telnet(data, writer)
                for byte in data:
                    if byte == 0x0D:
                        self._on_line(buffer.decode(), writer)
                        buffer.clear()
                    else:
                        buffer.append(byte)
        except (ConnectionError, OSError):
            pass
        finally:
            if writer in self._writers:
                self._writers.remove(writer)

    def _telnet(self, data: bytes, writer: asyncio.StreamWriter) -> bytes:
        out = bytearray()
        i = 0
        while i < len(data):
            byte = data[i]
            if byte != IAC:
                out.append(byte)
                i += 1
                continue
            cmd = data[i + 1]
            if cmd == IAC:
                out.append(IAC)
                i += 2
            elif cmd in (WILL, WONT, DO, DONT):
                opt = data[i + 2]
                if cmd == WILL and opt == COM_PORT:
                    writer.write(bytes([IAC, DO, COM_PORT]))
                    # Also try an option the client must refuse.
                    writer.write(bytes([IAC, DO, 24]))
                i += 3
            elif cmd == SB:
                end = data.index(bytes([IAC, SE]), i)
                sub = data[i + 2 : end]
                if sub[0] == COM_PORT and sub[1] == 1:
                    self.baudrate = struct.unpack("!I", sub[2:6])[0]
                    writer.write(
                        bytes([IAC, SB, COM_PORT, 101]) + sub[2:6] + bytes([IAC, SE])
                    )
                i = end + 2
            else:
                i += 2
        return bytes(out)

    def send(self, data: bytes, writer: asyncio.StreamWriter | None = None) -> None:
        """Emit bytes from the unit."""
        if self.rfc2217:
            data = data.replace(bytes([IAC]), bytes([IAC, IAC]))
        targets = self._writers if self.broadcast or writer is None else [writer]
        for target in targets:
            target.write(data)

    def _on_line(self, line: str, writer: asyncio.StreamWriter) -> None:
        self.received.append(line)
        if self.silent:
            return
        if not line.startswith("@"):
            self.send(NAK, writer)
            return
        body = line[1:]
        if body.startswith("?"):
            key = body[1:]
            if not self.power and self.standby_mode != "report":
                if self.standby_mode == "nak":
                    self.send(NAK, writer)
                return
            if not self.power and key in STANDBY_VALUES:
                self.send(f"@{key} {STANDBY_VALUES[key]}\r".encode(), writer)
            elif key in self.values and key not in self.unsupported:
                self.send(f"@{key} {self.values[key]}\r".encode(), writer)
            else:
                self.send(NAK, writer)
            return
        command, _, arg = body.partition(" ")
        if command == "POWER" and arg in ("ON", "OFF"):
            if arg == "OFF":
                self._off_at = time.monotonic()
            elif time.monotonic() - self._off_at < self.power_on_lockout:
                self.send(ACK, writer)
                return
            self.power = arg == "ON"
            if self.power:
                self.send(self.power_on_reply, writer)
                return
        elif not self.power and self.standby_mode != "report":
            self.send(NAK, writer)
            return
        elif command == "VOLUME":
            self.values["VOLUME"] = arg
        elif command == "INPUT":
            if not self.direct_input or arg not in self.inputs:
                self.send(NAK, writer)
                return
            self.values["INPUT"] = arg
        elif command == "KEY":
            if not self._key(arg):
                self.send(NAK, writer)
                return
        else:
            self.send(NAK, writer)
            return
        self.send(ACK, writer)

    def _key(self, code: str) -> bool:
        """Apply a key; False means the unit NAKs it.

        A real N-05XD NAKed KEY 29 / 2A (DOC-MISMATCH-03 / -04) while stopped
        on NET; ``toggle_keys=True`` simulates the documented behavior.
        """
        if code in ("29", "2A", "47", "1F") and not self.toggle_keys:
            return False
        if code == "20":
            index = self.inputs.index(self.values["INPUT"])
            self.values["INPUT"] = self.inputs[(index + 1) % len(self.inputs)]
        elif code in ("29", "47"):
            order = ["OFF", "ALL", "1"]
            self.values["REPEAT"] = order[(order.index(self.values["REPEAT"]) + 1) % 3]
        elif code in ("2A", "1F"):
            self.values["PMODE"] = (
                "SHUFFLE" if self.values["PMODE"] == "CONTINUE" else "CONTINUE"
            )
        elif code in ("27", "02"):
            self.values["PSTS"] = "PAUSE 3 1 23 TE"
        elif code in ("26", "01"):
            self.values["PSTS"] = "PLAY 3 1 23 TE"
        return True
