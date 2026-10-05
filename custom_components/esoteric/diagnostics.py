"""Diagnostics, useful when reporting issues with untested models."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import EsotericConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EsotericConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    client = coordinator.client
    state = coordinator.data
    transport = client._transport  # noqa: SLF001
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "model": coordinator.model.name,
        "model_tested": coordinator.model.tested,
        "connected": client.connected,
        "client_stats": asdict(client.stats),
        "rfc2217_server_settings": getattr(transport, "server_settings", None),
        "rfc2217_refused": getattr(transport, "com_port_refused", None),
        "power": state.power,
        "unsupported_requests": sorted(state.unsupported),
        "last_messages": {key: msg.raw for key, msg in state.messages.items()},
        "learned_inputs": coordinator.learned_inputs,
    }
