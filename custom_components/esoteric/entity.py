"""Base entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import EsotericCoordinator


class EsotericEntity(CoordinatorEntity[EsotericCoordinator]):
    """An entity belonging to one Esoteric unit."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EsotericCoordinator, key: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            model=coordinator.model.name,
            name=entry.title,
        )

    @property
    def available(self) -> bool:
        """Unavailable while the serial link is down."""
        return super().available and self.coordinator.client.connected

    @property
    def powered_on(self) -> bool:
        """Whether the unit is believed to be on."""
        return self.coordinator.data.power is not False
