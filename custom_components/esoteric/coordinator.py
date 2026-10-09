"""Polling coordinator plus push updates from the serial stream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .models import AMP_VOLUME_DOWN, AMP_VOLUME_UP, REQ_INPUT, REQ_VOLUME, ModelInfo
from .network import EsotericNetwork, NetworkError
from .protocol import (
    CommandRejectedError,
    EsotericClient,
    EsotericError,
    Message,
    NotConnectedError,
    ResponseTimeoutError,
)

_LOGGER = logging.getLogger(__name__)

# A request must be NAKed this many polls in a row (while the unit answers
# other requests) before we stop asking. More than one, because on a shared
# serial server a NAK may belong to another client.
UNSUPPORTED_STRIKES = 3
# Unsupported requests are retried every this many polls; some (e.g. UPCONV
# on the N-05XD) may only be answered in certain states.
UNSUPPORTED_RECHECK = 30
# Requests to try before concluding the unit is in standby.
STANDBY_PROBES = 3
# Time for the unit to act on a key before we read the result back.
KEY_SETTLE = 0.4
# A real N-05XD keeps answering in standby, reporting "@INPUT OFF".
STANDBY_INPUT = "OFF"
# Time to let the unit report its new power state before reading it back.
POWER_SETTLE = 1.0
# How long to keep re-sending POWER ON while the unit is still shutting down.
POWER_ON_RETRY_WINDOW = 15.0
STORAGE_VERSION = 1
# DOC-MISMATCH-09: a real N-05XD set to 100.0 reports "@VOLUME 10.0". Read
# 10.0 as the maximum when the last known volume was this close to it; the
# unit only steps by 0.5.
TRUNCATED_VOLUME = 10.0
NEAR_MAX_VOLUME = 95.0
# Retry backoff for the network side, which needs ~30 s after POWER ON.
NETWORK_RETRY_MIN = 10.0
NETWORK_RETRY_MAX = 30.0

type EsotericConfigEntry = ConfigEntry[EsotericCoordinator]


@dataclass
class EsotericState:
    """Everything we know about the unit."""

    power: bool | None = None
    messages: dict[str, Message] = field(default_factory=dict)
    received_at: dict[str, datetime] = field(default_factory=dict)
    unsupported: set[str] = field(default_factory=set)
    # Effective volume in device steps (after the 100.0 fix-up below).
    volume: float | None = None

    def value(self, key: str) -> str | None:
        """Joined arguments of the last ``@<key> ...`` line."""
        message = self.messages.get(key)
        return message.value if message else None

    def args(self, key: str) -> tuple[str, ...]:
        """Arguments of the last ``@<key> ...`` line."""
        message = self.messages.get(key)
        return message.args if message else ()


class EsotericCoordinator(DataUpdateCoordinator[EsotericState]):
    """Owns the client and the unit's state."""

    config_entry: EsotericConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EsotericConfigEntry,
        client: EsotericClient,
        model: ModelInfo,
        scan_interval: int,
        network: EsotericNetwork | None = None,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.url}",
            update_interval=timedelta(seconds=scan_interval),
            always_update=True,
        )
        self.client = client
        self.model = model
        self.data = EsotericState()
        self._strikes: dict[str, int] = {}
        self._unsubs: list[Callable[[], None]] = []
        # {"inputs": [...], "volume": float}; version 1 data was a bare list
        # of inputs, which is still accepted.
        self._store: Store[dict[str, Any] | list[str]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.inputs"
        )
        self._volume_hint: float | None = None
        self.learned_inputs: list[str] = list(model.known_inputs)
        self._polls = 0
        self.network = network
        if network is not None:
            network.on_update = self.async_update_listeners
        self._network_task: asyncio.Task[None] | None = None
        self._network_backoff = NETWORK_RETRY_MIN
        self._network_retry_at = 0.0

    async def async_start(self) -> None:
        """Hook up listeners and start the connection loop."""
        stored = await self._store.async_load() or {}
        if isinstance(stored, list):
            stored = {"inputs": stored}
        for name in stored.get("inputs", []):
            if name not in self.learned_inputs:
                self.learned_inputs.append(name)
        self._volume_hint = stored.get("volume")
        self._unsubs.append(self.client.add_listener(self._on_message))
        self._unsubs.append(self.client.add_connection_listener(self._on_connection))
        await self.client.start()

    async def async_stop(self) -> None:
        """Disconnect."""
        while self._unsubs:
            self._unsubs.pop()()
        if (task := self._network_task) is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.network is not None:
            await self.network.async_disconnect()
        await self.client.stop()

    @callback
    def schedule_network_upkeep(self, *, now: bool = False) -> None:
        """Connect/refresh/disconnect the network side in the background."""
        if self.network is None:
            return
        if self._network_task is not None and not self._network_task.done():
            return
        if now:
            self._network_retry_at = 0.0
        self._network_task = self.config_entry.async_create_background_task(
            self.hass, self._async_network_upkeep(), "esoteric network"
        )

    async def _async_network_upkeep(self) -> None:
        network = self.network
        assert network is not None
        # The network module is off in standby, so only try while on.
        want = bool(self.data.power) and self.client.connected
        try:
            if want and not network.connected:
                if time.monotonic() < self._network_retry_at:
                    return
                await network.async_connect()
            elif want:
                await network.async_refresh()
            elif network.connected:
                await network.async_disconnect()
            self._network_backoff = NETWORK_RETRY_MIN
        except NetworkError as err:
            _LOGGER.debug("Network side not available: %s", err)
            self._network_retry_at = time.monotonic() + self._network_backoff
            self._network_backoff = min(self._network_backoff * 2, NETWORK_RETRY_MAX)
        except Exception:
            # Never let the background task die with an unretrieved error.
            _LOGGER.exception("Unexpected error on the network side")
            self._network_retry_at = time.monotonic() + NETWORK_RETRY_MAX

    @callback
    def _on_message(self, message: Message) -> None:
        state = self.data
        state.messages[message.key] = message
        state.received_at[message.key] = dt_util.utcnow()
        state.unsupported.discard(message.key)
        self._strikes.pop(message.key, None)
        if message.key == REQ_INPUT and message.value:
            if message.value == STANDBY_INPUT:
                if state.power is not False:
                    state.power = False
                    self.schedule_network_upkeep(now=True)
            else:
                state.power = True
                self._learn_input(message.value)
        elif message.key == REQ_VOLUME:
            self._on_volume(message)
        self.async_update_listeners()

    def _on_volume(self, message: Message) -> None:
        try:
            steps = float(message.args[0])
        except (IndexError, ValueError):
            return
        volume = self.model.volume
        if (
            volume is not None
            and volume.maximum >= 100
            and steps == TRUNCATED_VOLUME
            and self._volume_hint is not None
            and self._volume_hint >= NEAR_MAX_VOLUME
        ):
            steps = volume.maximum
        self.data.volume = steps
        if steps != self._volume_hint:
            self._volume_hint = steps
            self._save()

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {"inputs": self.learned_inputs, "volume": self._volume_hint}, 5
        )

    @callback
    def _on_connection(self, connected: bool) -> None:
        if connected:
            self.config_entry.async_create_background_task(
                self.hass, self.async_request_refresh(), "esoteric refresh"
            )
        else:
            self.last_update_success = False
            self.async_update_listeners()

    def _learn_input(self, name: str) -> None:
        if self.model.direct_inputs or name in self.learned_inputs:
            return
        self.learned_inputs.append(name)
        self._save()

    async def _async_update_data(self) -> EsotericState:
        try:
            return await self._async_poll_serial()
        finally:
            self.schedule_network_upkeep()

    async def _async_poll_serial(self) -> EsotericState:
        if not self.client.connected:
            raise UpdateFailed(f"Not connected to {self.client.url}")
        state = self.data
        self._polls += 1
        recheck = self._polls % UNSUPPORTED_RECHECK == 0
        pending = [
            r for r in self.model.requests if recheck or r not in state.unsupported
        ]
        answered = False
        rejected: list[str] = []
        failures = 0
        for key in pending:
            try:
                message = await self.client.request(key)
            except CommandRejectedError:
                rejected.append(key)
                failures += 1
            except ResponseTimeoutError:
                failures += 1
            except NotConnectedError as err:
                raise UpdateFailed(str(err)) from err
            else:
                answered = True
                if key == REQ_INPUT and message.value == STANDBY_INPUT:
                    # In standby the unit answers with defaults (e.g. PMODE
                    # CONTINUE); don't overwrite the real values with them.
                    return state
            if not answered and failures >= STANDBY_PROBES:
                break

        if answered:
            if state.value(REQ_INPUT) != STANDBY_INPUT:
                state.power = True
            for key in rejected:
                self._strikes[key] = self._strikes.get(key, 0) + 1
                if self._strikes[key] >= UNSUPPORTED_STRIKES:
                    _LOGGER.info(
                        "%s does not answer @?%s; polling it less often",
                        self.model.name,
                        key,
                    )
                    state.unsupported.add(key)
        else:
            # No power query exists. Units that report "@INPUT OFF" are
            # handled above; one that answers nothing is assumed to be in
            # standby (behavior of models other than the N-05XD unknown).
            state.power = False
        return state

    # Commands -----------------------------------------------------------

    async def async_send(self, command: str) -> None:
        """Send a normal command, translating errors for the UI."""
        try:
            await self.client.send_command(command)
        except EsotericError as err:
            raise self._command_error(command, err) from err

    def _command_error(self, command: str, err: EsotericError) -> HomeAssistantError:
        if isinstance(err, CommandRejectedError):
            return HomeAssistantError(
                f"{self.model.name} rejected command {command!r} (NAK). It may "
                "not be available in the unit's current state (e.g. source) "
                "or may be disabled in its menu settings"
            )
        return HomeAssistantError(
            f"Sending {command!r} to {self.model.name} failed: {err}"
        )

    async def async_press(self, code: str) -> None:
        """Press a remote-control key."""
        await self.async_send(f"KEY {code}")

    async def async_refresh_key(self, key: str) -> Message | None:
        """Re-read one status value, ignoring failures."""
        try:
            return await self.client.request(key)
        except EsotericError as err:
            _LOGGER.debug("Re-reading %s failed: %s", key, err)
            return None

    async def async_set_power(self, on: bool) -> None:
        """Power on / standby."""
        command = "POWER ON" if on else "POWER OFF"
        acked = await self._send_power(command)
        if on:
            await self._confirm_power_on(command, acked)
        elif acked is not True:
            raise self._command_error(command, acked)
        self.data.power = on
        self.async_update_listeners()
        self.schedule_network_upkeep(now=True)

    async def _send_power(self, command: str) -> ResponseTimeoutError | bool:
        """Send POWER ON/OFF; return True on ACK or the timeout error."""
        try:
            await self.client.send_command(command)
        except ResponseTimeoutError as err:
            # DOC-MISMATCH-06: a real N-05XD answers a POWER ON that it acts
            # on with 0x83 instead of ACK (0x06).
            return err
        except EsotericError as err:
            raise self._command_error(command, err) from err
        return True

    async def _confirm_power_on(
        self, command: str, acked: ResponseTimeoutError | bool
    ) -> None:
        """Make sure the unit really left standby, re-sending if needed.

        DOC-MISMATCH-08: a real N-05XD ACKs but ignores POWER ON for about
        6-8 s after POWER OFF. Units that report "@INPUT OFF" in standby
        tell us whether it worked; for others we can only trust the ACK.
        """
        deadline = time.monotonic() + POWER_ON_RETRY_WINDOW
        while True:
            await asyncio.sleep(POWER_SETTLE)
            message = await self.async_refresh_key(REQ_INPUT)
            if message is None or message.value != STANDBY_INPUT:
                if message is None and acked is not True:
                    raise self._command_error(command, acked)
                return
            if time.monotonic() > deadline:
                raise HomeAssistantError(
                    f"{self.model.name} stayed in standby after {command!r}"
                )
            _LOGGER.debug(
                "%s still in standby, re-sending %s", self.model.name, command
            )
            await self._send_power(command)

    async def async_set_volume(self, steps: float) -> None:
        """Set the volume in device steps."""
        volume = self.model.volume
        if volume is None:
            raise HomeAssistantError(f"{self.model.name} has no volume control")
        steps = round(round(steps / volume.step) * volume.step, 1)
        steps = min(max(steps, 0.0), volume.maximum)
        # What we asked for decides how a truncated "10.0" reply is read.
        self._volume_hint = steps
        await self.async_send(f"VOLUME {steps:.1f}")
        await self.async_refresh_key(REQ_VOLUME)

    async def async_step_volume(self, up: bool) -> None:
        """Volume up / down by one device step."""
        volume = self.model.volume
        if volume is None:
            raise HomeAssistantError(f"{self.model.name} has no volume control")
        if volume.use_keys:
            await self.async_press(AMP_VOLUME_UP if up else AMP_VOLUME_DOWN)
            await self.async_refresh_key(REQ_VOLUME)
            return
        current = self.data.volume
        if current is None:
            message = await self.async_refresh_key(REQ_VOLUME)
            current = self.data.volume
            if message is None or current is None:
                raise HomeAssistantError("Current volume is unknown")
        await self.async_set_volume(current + (volume.step if up else -volume.step))

    async def async_select_input(self, name: str) -> None:
        """Select an input, directly if supported, else by stepping."""
        if self.model.direct_inputs:
            await self.async_send(f"INPUT {name}")
            await self.async_refresh_key(REQ_INPUT)
            return
        code = self.model.input_next_code
        if code is None:
            raise HomeAssistantError(f"{self.model.name} cannot change input")
        presses = max(len(self.learned_inputs), 8)
        if not await self.async_cycle(
            code, REQ_INPUT, lambda msg: msg.value == name, presses
        ):
            raise HomeAssistantError(f"Input {name!r} not reached")

    async def async_cycle(
        self,
        code: str,
        key: str,
        done: Callable[[Message], bool],
        max_presses: int,
    ) -> bool:
        """Press a toggle key until the ``key`` status satisfies ``done``."""
        message = self.data.messages.get(key) or await self.async_refresh_key(key)
        for _ in range(max_presses):
            if message is not None and done(message):
                return True
            await self.async_press(code)
            await asyncio.sleep(KEY_SETTLE)
            message = await self.async_refresh_key(key)
        return message is not None and done(message)
