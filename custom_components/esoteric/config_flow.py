"""Config flow."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
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
)
from .coordinator import EsotericConfigEntry
from .models import MODELS
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
        }
    )
    return vol.Schema(fields)


async def _validate(
    user_input: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalize the input and try to open the connection."""
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
    return data, errors


class EsotericConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up an Esoteric unit."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the connection and model."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate(user_input)
            if not errors:
                await self.async_set_unique_id(data[CONF_CONNECTION])
                self._abort_if_unique_id_configured()
                title = (user_input.get(CONF_NAME) or "").strip() or (
                    f"Esoteric {MODELS[data[CONF_MODEL]].name}"
                )
                return self.async_create_entry(title=title, data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(user_input or {}, with_name=True),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change connection, model or baud rate."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate(user_input)
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
                }
            ),
        )
