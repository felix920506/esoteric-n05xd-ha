"""Home Assistant tests for RS-232 + network (OpenHome) control combined."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.components.media_player import (
    ATTR_MEDIA_ALBUM_NAME,
    ATTR_MEDIA_ARTIST,
    ATTR_MEDIA_DURATION,
    ATTR_MEDIA_POSITION,
    ATTR_MEDIA_REPEAT,
    ATTR_MEDIA_SHUFFLE,
    ATTR_MEDIA_TITLE,
    MediaPlayerEntityFeature,
)
from homeassistant.components.media_player import (
    DOMAIN as MP_DOMAIN,
)
from homeassistant.config_entries import SOURCE_SSDP, SOURCE_USER
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.service_info.ssdp import (
    ATTR_UPNP_FRIENDLY_NAME,
    ATTR_UPNP_MANUFACTURER,
    ATTR_UPNP_UDN,
    SsdpServiceInfo,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.esoteric.const import (
    CONF_BAUDRATE,
    CONF_CONNECTION,
    CONF_EVENT_PORT,
    CONF_MODEL,
    CONF_NETWORK,
    CONF_NETWORK_UDN,
    CONF_NETWORK_URL,
    DOMAIN,
)

from .fake_device import FakeEsoteric
from .fake_openhome import FakeOpenHome

MP = "media_player.esoteric_n_05xd"
UDN = "uuid:4c494e4e-0000-0000-0000-000000000001"


@pytest.fixture
async def device():
    fake = FakeEsoteric()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def openhome():
    fake = FakeOpenHome()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture(autouse=True)
def _fast():
    with (
        patch("custom_components.esoteric.coordinator.KEY_SETTLE", 0.01),
        patch("custom_components.esoteric.coordinator.POWER_SETTLE", 0.05),
        patch("custom_components.esoteric.coordinator.NETWORK_RETRY_MIN", 0.0),
        # Events go to the address routing to the unit; that's localhost here.
        patch(
            "custom_components.esoteric.async_get_source_ip", return_value="127.0.0.1"
        ),
    ):
        yield


async def _until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def _setup(hass, device, openhome) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Esoteric N-05XD",
        unique_id=device.url,
        data={
            CONF_CONNECTION: device.url,
            CONF_MODEL: "n_05xd",
            CONF_BAUDRATE: 9600,
            CONF_NETWORK_URL: openhome.url,
            CONF_NETWORK_UDN: UDN,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    network = entry.runtime_data.network
    await _until(lambda: network.connected)
    await hass.async_block_till_done()
    return entry


async def _mp(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        MP_DOMAIN, service, {ATTR_ENTITY_ID: MP, **data}, blocking=True
    )


async def test_metadata_and_controls(hass, device, openhome) -> None:
    await _setup(hass, device, openhome)
    attrs = hass.states.get(MP).attributes
    assert attrs[ATTR_MEDIA_TITLE] == "Vivid Theory"
    assert attrs[ATTR_MEDIA_ARTIST] == "Arcaea"
    assert attrs[ATTR_MEDIA_ALBUM_NAME] == "Memories"
    assert attrs[ATTR_MEDIA_DURATION] == 245
    assert attrs[ATTR_MEDIA_POSITION] == 30
    assert "entity_picture" in attrs
    features = attrs["supported_features"]
    for feature in (
        MediaPlayerEntityFeature.REPEAT_SET,
        MediaPlayerEntityFeature.SHUFFLE_SET,
        MediaPlayerEntityFeature.SEEK,
    ):
        assert features & feature

    # Repeat/shuffle: rejected over RS-232 on the N-05XD, fine over OpenHome.
    await _mp(hass, "repeat_set", repeat="all")
    assert openhome.repeat == "true"
    assert hass.states.get(MP).attributes[ATTR_MEDIA_REPEAT] == "all"
    await _mp(hass, "shuffle_set", shuffle=True)
    assert openhome.shuffle is True
    assert hass.states.get(MP).attributes[ATTR_MEDIA_SHUFFLE] is True
    assert "@KEY 29" not in device.received
    with pytest.raises(ServiceValidationError):
        await _mp(hass, "repeat_set", repeat="one")

    await _mp(hass, "media_seek", seek_position=100)
    assert openhome.seconds == 100

    # Pushed changes (another app) show up without polling.
    openhome.change(title="Glow")
    await _until(lambda: hass.states.get(MP).attributes.get(ATTR_MEDIA_TITLE) == "Glow")


async def test_state_follows_network_player(hass, device, openhome) -> None:
    """Play state is pushed by the network player; RS-232 PSTS lags."""
    await _setup(hass, device, openhome)
    device.values["PSTS"] = "STOP 0 0 00 TE"
    openhome.change(transport_state="Paused")
    await _until(lambda: hass.states.get(MP).state == "paused")
    openhome.change(transport_state="Buffering")
    await _until(lambda: hass.states.get(MP).state == "buffering")


async def test_network_only_on_net_input(hass, device, openhome) -> None:
    entry = await _setup(hass, device, openhome)
    device.values["INPUT"] = "USB"
    await entry.runtime_data.async_refresh()
    attrs = hass.states.get(MP).attributes
    assert ATTR_MEDIA_TITLE not in attrs
    assert not attrs["supported_features"] & MediaPlayerEntityFeature.REPEAT_SET


async def test_no_stale_metadata_when_stopped(hass, device, openhome) -> None:
    await _setup(hass, device, openhome)
    openhome.change(transport_state="Stopped")
    await _until(lambda: ATTR_MEDIA_TITLE not in hass.states.get(MP).attributes)
    attrs = hass.states.get(MP).attributes
    assert not attrs["supported_features"] & MediaPlayerEntityFeature.SEEK
    # Modes can still be set while stopped.
    assert attrs["supported_features"] & MediaPlayerEntityFeature.SHUFFLE_SET


async def test_standby_drops_and_power_on_restores_network(
    hass, device, openhome
) -> None:
    entry = await _setup(hass, device, openhome)
    network = entry.runtime_data.network
    await _mp(hass, "turn_off")
    await _until(lambda: not network.connected)
    await _until(lambda: not openhome.subscriptions)
    assert ATTR_MEDIA_TITLE not in hass.states.get(MP).attributes

    await _mp(hass, "turn_on")
    await _until(lambda: network.connected)
    await hass.async_block_till_done()
    assert hass.states.get(MP).attributes[ATTR_MEDIA_TITLE] == "Vivid Theory"


async def test_network_down_keeps_rs232_working(hass, device, openhome) -> None:
    entry = await _setup(hass, device, openhome)
    await openhome.stop()
    await entry.runtime_data.async_refresh()
    await _until(lambda: not entry.runtime_data.network.connected)
    await hass.async_block_till_done()
    state = hass.states.get(MP)
    assert state.state == "playing"  # from RS-232
    assert ATTR_MEDIA_TITLE not in state.attributes


# --- config flow ----------------------------------------------------------


async def _user_flow(hass, data):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def test_config_flow_with_network(hass, device, openhome) -> None:
    result = await _user_flow(
        hass,
        {
            CONF_CONNECTION: device.url,
            CONF_MODEL: "n_05xd",
            CONF_BAUDRATE: "9600",
            CONF_NETWORK: f"127.0.0.1:{openhome.port}",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_NETWORK_URL] == openhome.url
    assert result["data"][CONF_NETWORK_UDN] == UDN
    assert CONF_NETWORK not in result["data"]


@pytest.mark.parametrize(
    ("model", "network", "error"),
    [
        ("n_05xd", "http://", "invalid_network"),
        ("n_05xd", "127.0.0.1:1", "network_unreachable"),
        ("grandioso_c1x", "127.0.0.1:1", "network_not_supported"),
    ],
)
async def test_config_flow_network_errors(hass, device, model, network, error):
    result = await _user_flow(
        hass,
        {
            CONF_CONNECTION: device.url,
            CONF_MODEL: model,
            CONF_BAUDRATE: "9600",
            CONF_NETWORK: network,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NETWORK: error}


def _ssdp(url: str) -> SsdpServiceInfo:
    return SsdpServiceInfo(
        ssdp_usn=f"{UDN}::upnp:rootdevice",
        ssdp_st="upnp:rootdevice",
        ssdp_location=url,
        ssdp_udn=UDN,
        upnp={
            ATTR_UPNP_UDN: UDN,
            ATTR_UPNP_MANUFACTURER: "ESOTERIC COMPANY",
            ATTR_UPNP_FRIENDLY_NAME: "ESOTERIC N-05XD:ESOTERIC",
        },
    )


async def test_ssdp_discovery(hass, device, openhome) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_SSDP}, data=_ssdp(openhome.url)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    schema = {str(k): k for k in result["data_schema"].schema}
    assert schema[CONF_NETWORK].description["suggested_value"] == openhome.url
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CONNECTION: device.url,
            CONF_MODEL: "n_05xd",
            CONF_BAUDRATE: "9600",
            CONF_NETWORK: openhome.url,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ESOTERIC N-05XD"
    assert result["data"][CONF_NETWORK_UDN] == UDN


async def test_ssdp_follows_address_change(hass, device, openhome) -> None:
    entry = await _setup(hass, device, openhome)
    new_url = "http://127.0.0.1:1/"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_SSDP}, data=_ssdp(new_url)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_NETWORK_URL] == new_url
    await hass.async_block_till_done()


async def test_options_show_event_settings(hass, device, openhome) -> None:
    entry = await _setup(hass, device, openhome)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_EVENT_PORT in {str(k) for k in result["data_schema"].schema}
