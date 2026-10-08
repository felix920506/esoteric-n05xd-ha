"""Config flow."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.helpers.service_info.ssdp import (
    ATTR_UPNP_FRIENDLY_NAME,
    ATTR_UPNP_UDN,
    SsdpServiceInfo,
)

from .const import (
    CONF_BAUDRATE,
    CONF_CALLBACK_URL,
    CONF_CONNECTION,
    CONF_EVENT_PORT,
    CONF_MODEL,
    CONF_NETWORK,
    CONF_NETWORK_UDN,
    CONF_NETWORK_URL,
    CONF_RESPONSE_TIMEOUT,
    CONF_SCAN_INTERVAL,
    CONNECT_TIMEOUT,
    DEFAULT_BAUDRATE,
    DEFAULT_EVENT_PORT,
    DEFAULT_RESPONSE_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import EsotericConfigEntry
from .models import MODELS, Category
from .network import NetworkError, async_probe, description_url
from .protocol import ConnectionDroppedError, EsotericClient
from .transport import InvalidUrlError, parse_url

_LOGGER = logging.getLogger(__name__)

BAUDRATES = ["9600", "19200", "38400", "57600", "115200"]


def _connection_schema(defaults: dict[str, Any], *, with_name: bool) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if with_name:
        fields[vol.Optional(CONF_NAME, default=defaults.get(CONF_NAME, ""))] = (
            TextSelector()
        )
    fields.update(
        {
            vol.Required(
                CONF_CONNECTION, default=defaults.get(CONF_CONNECTION, vol.UNDEFINED)
            ): TextSelector(),
            vol.Required(
                CONF_MODEL, default=defaults.get(CONF_MODEL, "n_05xd")
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(MODELS),
                    translation_key=CONF_MODEL,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_BAUDRATE,
                default=str(defaults.get(CONF_BAUDRATE, DEFAULT_BAUDRATE)),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=BAUDRATES,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_NETWORK,
                description={
                    "suggested_value": defaults.get(
                        CONF_NETWORK, defaults.get(CONF_NETWORK_URL)
                    )
                },
            ): TextSelector(),
        }
    )
    return vol.Schema(fields)


async def _validate(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalize the input and try to open the connection(s)."""
    errors: dict[str, str] = {}
    data = {
        CONF_CONNECTION: user_input[CONF_CONNECTION].strip(),
        CONF_MODEL: user_input[CONF_MODEL],
    }
    try:
        data[CONF_BAUDRATE] = int(user_input[CONF_BAUDRATE])
        if data[CONF_BAUDRATE] <= 0:
            raise ValueError
    except ValueError:
        errors[CONF_BAUDRATE] = "invalid_baudrate"
        return data, errors
    try:
        spec = parse_url(data[CONF_CONNECTION])
    except InvalidUrlError:
        errors[CONF_CONNECTION] = "invalid_connection"
        return data, errors
    data[CONF_CONNECTION] = str(spec)

    client = EsotericClient(spec, data[CONF_BAUDRATE], connect_timeout=CONNECT_TIMEOUT)
    try:
        await client.check_connection()
    except ConnectionDroppedError as err:
        # Accepted, then closed straight away: usually a single-client
        # serial server whose port is already in use.
        _LOGGER.debug("%s dropped the connection: %r", spec, err)
        errors["base"] = "port_busy"
    except (OSError, TimeoutError) as err:
        _LOGGER.debug("Cannot connect to %s: %r", spec, err)
        errors["base"] = "cannot_connect"
    finally:
        await client.stop()
    if not errors:
        await _validate_network(hass, user_input, data, errors)
    return data, errors


async def _validate_network(
    hass: HomeAssistant,
    user_input: dict[str, Any],
    data: dict[str, Any],
    errors: dict[str, str],
) -> None:
    """Check the optional network address and store its description URL."""
    if not (address := (user_input.get(CONF_NETWORK) or "").strip()):
        return
    if Category.NETWORK not in MODELS[data[CONF_MODEL]].categories:
        errors[CONF_NETWORK] = "network_not_supported"
        return
    try:
        url = description_url(address)
    except ValueError:
        errors[CONF_NETWORK] = "invalid_network"
        return
    try:
        udn, _ = await async_probe(async_get_clientsession(hass), url)
    except NetworkError as err:
        # The network module is off in standby.
        _LOGGER.debug("Network side not reachable: %s", err)
        errors[CONF_NETWORK] = "network_unreachable"
        return
    except ValueError as err:
        _LOGGER.debug("Not an Esoteric network player: %s", err)
        errors[CONF_NETWORK] = "network_not_esoteric"
        return
    data[CONF_NETWORK_URL] = url
    data[CONF_NETWORK_UDN] = udn


class EsotericConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up an Esoteric unit."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._discovered: dict[str, Any] = {}

    async def async_step_ssdp(
        self, discovery_info: SsdpServiceInfo
    ) -> ConfigFlowResult:
        """A network player announced itself; ask for its RS-232 connection."""
        url = discovery_info.ssdp_location
        udn = discovery_info.upnp.get(ATTR_UPNP_UDN) or discovery_info.ssdp_udn
        friendly = discovery_info.upnp.get(ATTR_UPNP_FRIENDLY_NAME, "")
        for entry in self._async_current_entries(include_ignore=False):
            if udn and entry.data.get(CONF_NETWORK_UDN) == udn:
                # Known unit; follow DHCP address changes.
                if url and entry.data.get(CONF_NETWORK_URL) != url:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_NETWORK_URL: url}
                    )
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(udn)
        self._abort_if_unique_id_configured()
        model = next(
            (
                model.model_id
                for model in MODELS.values()
                if Category.NETWORK in model.categories
                and model.name.upper() in friendly.upper()
            ),
            "n_05xd",
        )
        name = friendly.split(":")[0] or MODELS[model].name
        self._discovered = {CONF_NETWORK: url, CONF_MODEL: model, CONF_NAME: name}
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the connection and model."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(
                    data[CONF_CONNECTION], raise_on_progress=False
                )
                self._abort_if_unique_id_configured()
                title = (user_input.get(CONF_NAME) or "").strip() or (
                    f"Esoteric {MODELS[data[CONF_MODEL]].name}"
                )
                return self.async_create_entry(title=title, data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(
                user_input or self._discovered, with_name=True
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change connection, model or baud rate."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate(self.hass, user_input)
            if not errors:
                if any(
                    other.entry_id != entry.entry_id
                    and other.unique_id == data[CONF_CONNECTION]
                    for other in self._async_current_entries(include_ignore=False)
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry, unique_id=data[CONF_CONNECTION], data=data
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(
                user_input or dict(entry.data), with_name=False
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: EsotericConfigEntry) -> EsotericOptionsFlow:
        """Options."""
        return EsotericOptionsFlow()


class EsotericOptionsFlow(OptionsFlowWithReload):
    """Polling and timeout options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=2,
                            max=3600,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_RESPONSE_TIMEOUT,
                        default=options.get(
                            CONF_RESPONSE_TIMEOUT, DEFAULT_RESPONSE_TIMEOUT
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0.2,
                            max=10,
                            step=0.1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    **self._network_options(options),
                }
            ),
        )

    def _network_options(self, options: Mapping[str, Any]) -> dict[Any, Any]:
        """Event listener options, when a network address is configured."""
        if not self.config_entry.data.get(CONF_NETWORK_URL):
            return {}
        return {
            vol.Required(
                CONF_EVENT_PORT,
                default=options.get(CONF_EVENT_PORT, DEFAULT_EVENT_PORT),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=65535, step=1, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_CALLBACK_URL,
                description={"suggested_value": options.get(CONF_CALLBACK_URL)},
            ): TextSelector(),
        }
