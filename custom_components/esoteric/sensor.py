"""Status sensors, one per request command the model supports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EsotericConfigEntry, EsotericCoordinator
from .entity import EsotericEntity
from .models import (
    REQ_AOUT,
    REQ_CGAIN,
    REQ_CODEC,
    REQ_DOUT,
    REQ_EQ,
    REQ_FS,
    REQ_GAIN,
    REQ_INPUT,
    REQ_MEDIA,
    REQ_MQA,
    REQ_OUTPUT,
    REQ_PMODE,
    REQ_PSTS,
    REQ_REPEAT,
    REQ_UPCONV,
)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EsotericSensorDescription(SensorEntityDescription):
    """Maps a request command to a sensor."""

    request: str
    # Which argument is the state; None means all arguments joined.
    arg: int | None = None


SENSORS = (
    EsotericSensorDescription(key="input", request=REQ_INPUT),
    EsotericSensorDescription(key="analog_output", request=REQ_AOUT),
    EsotericSensorDescription(key="digital_output", request=REQ_DOUT),
    EsotericSensorDescription(key="media", request=REQ_MEDIA, arg=0),
    EsotericSensorDescription(
        key="playback_status", request=REQ_PSTS, entity_registry_enabled_default=False
    ),
    EsotericSensorDescription(key="play_mode", request=REQ_PMODE),
    EsotericSensorDescription(
        key="repeat", request=REQ_REPEAT, entity_registry_enabled_default=False
    ),
    EsotericSensorDescription(key="upconversion", request=REQ_UPCONV),
    EsotericSensorDescription(key="sample_rate", request=REQ_FS),
    EsotericSensorDescription(key="codec", request=REQ_CODEC),
    EsotericSensorDescription(key="mqa", request=REQ_MQA),
    EsotericSensorDescription(key="phono_output", request=REQ_OUTPUT),
    EsotericSensorDescription(key="eq_curve", request=REQ_EQ),
    EsotericSensorDescription(key="gain", request=REQ_GAIN),
    EsotericSensorDescription(key="cartridge_gain", request=REQ_CGAIN),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EsotericConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for the requests this model answers."""
    coordinator = entry.runtime_data
    requests = set(coordinator.model.requests)
    async_add_entities(
        EsotericSensor(coordinator, description)
        for description in SENSORS
        if description.request in requests
    )


class EsotericSensor(EsotericEntity, SensorEntity):
    """The last value of one ``@?<KEY>`` request."""

    entity_description: EsotericSensorDescription

    def __init__(
        self, coordinator: EsotericCoordinator, description: EsotericSensorDescription
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_translation_key = description.key
        if description.request in (REQ_FS, REQ_MQA, REQ_CODEC, REQ_UPCONV):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        """Unavailable in standby or when the model turns out not to answer."""
        state = self.coordinator.data
        return (
            super().available
            and self.powered_on
            and self.entity_description.request not in state.unsupported
        )

    @property
    def native_value(self) -> str | None:
        """The value."""
        args = self.coordinator.data.args(self.entity_description.request)
        if not args:
            return None
        if (index := self.entity_description.arg) is not None:
            return args[index] if index < len(args) else None
        return " ".join(args)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Extra detail for the media sensor and the raw line."""
        message = self.coordinator.data.messages.get(self.entity_description.request)
        if message is None:
            return None
        attrs: dict[str, Any] = {"raw": message.raw}
        if self.entity_description.request == REQ_MEDIA and len(message.args) >= 4:
            # @MEDIA SACD 12 34 56 -> 12 tracks, 34m56s
            try:
                attrs["total_tracks"] = int(message.args[1])
                attrs["total_time"] = int(message.args[2]) * 60 + int(message.args[3])
            except ValueError:
                pass
        return attrs
