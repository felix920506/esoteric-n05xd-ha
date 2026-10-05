"""Volume in native device steps."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EsotericConfigEntry, EsotericCoordinator
from .entity import EsotericEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EsotericConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the volume number for models with volume control."""
    coordinator = entry.runtime_data
    if coordinator.model.volume is not None:
        async_add_entities([EsotericVolume(coordinator)])


class EsotericVolume(EsotericEntity, NumberEntity):
    """Volume as shown on the unit (e.g. 0-100.0 in 0.5 steps on the N-05XD)."""

    _attr_translation_key = "volume"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0.0

    def __init__(self, coordinator: EsotericCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "volume")
        volume = coordinator.model.volume
        assert volume is not None
        self._attr_native_max_value = volume.maximum
        self._attr_native_step = volume.step

    @property
    def available(self) -> bool:
        """Unavailable in standby."""
        return super().available and self.powered_on

    @property
    def native_value(self) -> float | None:
        """Volume in steps."""
        return self.coordinator.data.volume

    async def async_set_native_value(self, value: float) -> None:
        """Set the volume."""
        await self.coordinator.async_set_volume(value)
