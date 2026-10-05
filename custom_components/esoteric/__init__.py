"""Esoteric audio equipment over RS-232C."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_COMMAND,
    ATTR_CONFIG_ENTRY_ID,
    CONF_BAUDRATE,
    CONF_CONNECTION,
    CONF_MODEL,
    CONF_RESPONSE_TIMEOUT,
    CONF_SCAN_INTERVAL,
    CONNECT_TIMEOUT,
    DEFAULT_BAUDRATE,
    DEFAULT_RESPONSE_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_SEND_COMMAND,
)
from .coordinator import EsotericConfigEntry, EsotericCoordinator
from .models import MODELS
from .protocol import CommandRejectedError, EsotericClient, EsotericError

PLATFORMS = [Platform.BUTTON, Platform.MEDIA_PLAYER, Platform.NUMBER, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SEND_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_COMMAND): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-wide services."""

    async def send_command(call: ServiceCall) -> ServiceResponse:
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        entry: EsotericConfigEntry | None = hass.config_entries.async_get_entry(
            entry_id
        )
        if (
            entry is None
            or entry.domain != DOMAIN
            or not hasattr(entry, "runtime_data")
        ):
            raise ServiceValidationError(f"Esoteric entry {entry_id} is not loaded")
        try:
            message = await entry.runtime_data.client.send_raw(call.data[ATTR_COMMAND])
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except CommandRejectedError:
            return {"result": "nak", "response": None}
        except EsotericError as err:
            raise HomeAssistantError(str(err)) from err
        if message is None:
            return {"result": "ack", "response": None}
        return {"result": "response", "response": message.raw}

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_COMMAND,
        send_command,
        schema=SEND_COMMAND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EsotericConfigEntry) -> bool:
    """Connect to the unit."""
    model = MODELS[entry.data[CONF_MODEL]]
    client = EsotericClient(
        entry.data[CONF_CONNECTION],
        entry.data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
        response_timeout=entry.options.get(
            CONF_RESPONSE_TIMEOUT, DEFAULT_RESPONSE_TIMEOUT
        ),
        connect_timeout=CONNECT_TIMEOUT,
    )
    coordinator = EsotericCoordinator(
        hass,
        entry,
        client,
        model,
        entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    await coordinator.async_start()
    if not await client.wait_connected(CONNECT_TIMEOUT):
        error = client.stats.last_error
        await coordinator.async_stop()
        raise ConfigEntryNotReady(f"Cannot connect to {client.url}: {error}")
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        await coordinator.async_stop()
        raise

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EsotericConfigEntry) -> bool:
    """Disconnect."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
    return unloaded
