"""Polling coordinator plus push updates from the serial stream."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .models import AMP_VOLUME_DOWN, AMP_VOLUME_UP, REQ_INPUT, REQ_VOLUME, ModelInfo
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
STORAGE_VERSION = 1

type EsotericConfigEntry = ConfigEntry[EsotericCoordinator]


@dataclass
class EsotericState:
    """Everything we know about the unit."""

    power: bool | None = None
    messages: dict[str, Message] = field(default_factory=dict)
    received_at: dict[str, datetime] = field(default_factory=dict)
    unsupported: set[str] = field(default_factory=set)

    def value(self, key: str) -> str | None:
        """Joined arguments of the last ``@<key> ...`` line."""
        message = self.messages.get(key)
        return message.value if message else None

    def args(self, key: str) -> tuple[str, ...]:
        """Arguments of the last ``@<key> ...`` line."""
        message = self.messages.get(key)
        return message.args if message else ()

    @property
    def volume(self) -> float | None:
        """Volume in device steps."""
        try:
            return float(self.args(REQ_VOLUME)[0])
        except (IndexError, ValueError):
            return None


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
        self._store: Store[list[str]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.inputs"
        )
        self.learned_inputs: list[str] = list(model.known_inputs)
        self._polls = 0

    async def async_start(self) -> None:
        """Hook up listeners and start the connection loop."""
        for name in await self._store.async_load() or []:
            if name not in self.learned_inputs:
                self.learned_inputs.append(name)
        self._unsubs.append(self.client.add_listener(self._on_message))
        self._unsubs.append(self.client.add_connection_listener(self._on_connection))
        await self.client.start()

    async def async_stop(self) -> None:
        """Disconnect."""
        while self._unsubs:
            self._unsubs.pop()()
        await self.client.stop()

    @callback
    def _on_message(self, message: Message) -> None:
        state = self.data
        state.messages[message.key] = message
        state.received_at[message.key] = dt_util.utcnow()
        state.unsupported.discard(message.key)
        self._strikes.pop(message.key, None)
        if message.key == REQ_INPUT and message.value:
            self._learn_input(message.value)
        self.async_update_listeners()

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
        self._store.async_delay_save(lambda: self.learned_inputs, 5)

    async def _async_update_data(self) -> EsotericState:
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
                await self.client.request(key)
            except CommandRejectedError:
                rejected.append(key)
                failures += 1
            except ResponseTimeoutError:
                failures += 1
            except NotConnectedError as err:
                raise UpdateFailed(str(err)) from err
            else:
                answered = True
            if not answered and failures >= STANDBY_PROBES:
                break

        if answered:
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
            # No power query exists; a unit that answers nothing is assumed
            # to be in standby (to be confirmed on real hardware).
            state.power = False
        return state

    # Commands -----------------------------------------------------------

    async def async_send(self, command: str) -> None:
        """Send a normal command, translating errors for the UI."""
        try:
            await self.client.send_command(command)
        except CommandRejectedError as err:
            raise HomeAssistantError(
                f"{self.model.name} rejected command {command!r} (NAK). It may "
                "not be available in the unit's current state (e.g. source) "
                "or may be disabled in its menu settings"
            ) from err
        except EsotericError as err:
            raise HomeAssistantError(
                f"Sending {command!r} to {self.model.name} failed: {err}"
            ) from err

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
        await self.async_send("POWER ON" if on else "POWER OFF")
        self.data.power = on
        self.async_update_listeners()

    async def async_set_volume(self, steps: float) -> None:
        """Set the volume in device steps."""
        volume = self.model.volume
        if volume is None:
            raise HomeAssistantError(f"{self.model.name} has no volume control")
        steps = round(round(steps / volume.step) * volume.step, 1)
        steps = min(max(steps, 0.0), volume.maximum)
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
