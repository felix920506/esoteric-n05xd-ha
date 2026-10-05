"""Remote-control keys (``@KEY xx``) as buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EsotericConfigEntry, EsotericCoordinator
from .entity import EsotericEntity
from .models import KeyCommand

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EsotericConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one button per key the model documents."""
    coordinator = entry.runtime_data
    async_add_entities(
        EsotericKeyButton(coordinator, key) for key in coordinator.model.keys
    )


class EsotericKeyButton(EsotericEntity, ButtonEntity):
    """Press one remote-control key."""

    def __init__(self, coordinator: EsotericCoordinator, key: KeyCommand) -> None:
        """Initialize."""
        super().__init__(coordinator, f"key_{key.key}")
        self._key = key
        self._attr_translation_key = f"key_{key.key}"
        self._attr_entity_registry_enabled_default = key.enabled_default

    async def async_press(self) -> None:
        """Send the key."""
        await self.coordinator.async_press(self._key.code)
