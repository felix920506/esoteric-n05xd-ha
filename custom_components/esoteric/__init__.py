"""Esoteric audio equipment over RS-232C."""

from __future__ import annotations

import asyncio
import socket
from ipaddress import ip_address
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.components.network import async_get_source_ip
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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_COMMAND,
    ATTR_CONFIG_ENTRY_ID,
    CONF_BAUDRATE,
    CONF_CALLBACK_URL,
    CONF_CONNECTION,
    CONF_EVENT_PORT,
    CONF_MODEL,
    CONF_NETWORK_URL,
    CONF_RESPONSE_TIMEOUT,
    CONF_SCAN_INTERVAL,
    CONNECT_TIMEOUT,
    DEFAULT_BAUDRATE,
    DEFAULT_EVENT_PORT,
    DEFAULT_RESPONSE_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_SEND_COMMAND,
)
from .coordinator import EsotericConfigEntry, EsotericCoordinator
from .models import MODELS
from .network import EsotericNetwork
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
        await _async_create_network(hass, entry),
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


async def _async_create_network(
    hass: HomeAssistant, entry: EsotericConfigEntry
) -> EsotericNetwork | None:
    """Set up the optional OpenHome side, if a network address is configured."""
    if not (url := entry.data.get(CONF_NETWORK_URL)):
        return None
    return EsotericNetwork(
        async_get_clientsession(hass),
        url,
        listen_host=await _async_listen_host(hass, urlsplit(url).hostname or ""),
        listen_port=int(entry.options.get(CONF_EVENT_PORT, DEFAULT_EVENT_PORT)),
        callback_url=entry.options.get(CONF_CALLBACK_URL) or None,
    )


async def _async_listen_host(hass: HomeAssistant, host: str) -> str | None:
    """Local address that routes to the unit, for the event listener.

    Matters with several interfaces/VLANs. None (all interfaces) if unknown;
    never fails setup, since the network side is optional.
    """
    try:
        ip_address(host)
    except ValueError:
        try:  # resolve without blocking the event loop
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, None, family=socket.AF_INET
            )
        except OSError:
            return None
        host = infos[0][4][0]
    try:
        return await async_get_source_ip(hass, target_ip=host)
    except HomeAssistantError:
        return None
