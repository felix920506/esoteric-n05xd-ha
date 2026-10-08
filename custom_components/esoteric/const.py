"""Constants for the Esoteric integration."""

from typing import Final

DOMAIN: Final = "esoteric"
MANUFACTURER: Final = "Esoteric"

CONF_CONNECTION: Final = "connection"
CONF_MODEL: Final = "model"
CONF_BAUDRATE: Final = "baudrate"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_RESPONSE_TIMEOUT: Final = "response_timeout"
# Optional network (UPnP / OpenHome) side of network players.
CONF_NETWORK: Final = "network"  # user input: host, host:port or URL
CONF_NETWORK_URL: Final = "network_url"  # device description URL
CONF_NETWORK_UDN: Final = "network_udn"
CONF_EVENT_PORT: Final = "event_port"
CONF_CALLBACK_URL: Final = "callback_url"

DEFAULT_BAUDRATE: Final = 9600
DEFAULT_SCAN_INTERVAL: Final = 10
DEFAULT_RESPONSE_TIMEOUT: Final = 1.0
DEFAULT_EVENT_PORT: Final = 0  # any free port
CONNECT_TIMEOUT: Final = 10.0

SERVICE_SEND_COMMAND: Final = "send_command"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_COMMAND: Final = "command"
