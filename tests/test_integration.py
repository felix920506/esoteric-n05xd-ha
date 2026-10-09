"""Home Assistant integration tests against the fake unit."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.components.media_player import (
    ATTR_INPUT_SOURCE,
    ATTR_INPUT_SOURCE_LIST,
    ATTR_MEDIA_POSITION,
    ATTR_MEDIA_REPEAT,
    ATTR_MEDIA_SHUFFLE,
    ATTR_MEDIA_TRACK,
    ATTR_MEDIA_VOLUME_LEVEL,
    MediaPlayerEntityFeature,
)
from homeassistant.components.media_player import (
    DOMAIN as MP_DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.esoteric.const import (
    CONF_BAUDRATE,
    CONF_CONNECTION,
    CONF_MODEL,
    CONF_RESPONSE_TIMEOUT,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.esoteric.coordinator import UNSUPPORTED_RECHECK

from .fake_device import FakeEsoteric

MP = "media_player.esoteric_n_05xd"


@pytest.fixture
async def device():
    fake = FakeEsoteric()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture(autouse=True)
def _fast_keys():
    with patch("custom_components.esoteric.coordinator.KEY_SETTLE", 0.01):
        yield


async def _setup(hass: HomeAssistant, device: FakeEsoteric, model: str = "n_05xd"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Esoteric N-05XD",
        unique_id=device.url,
        data={CONF_CONNECTION: device.url, CONF_MODEL: model, CONF_BAUDRATE: 9600},
        options={CONF_SCAN_INTERVAL: 10, CONF_RESPONSE_TIMEOUT: 0.3},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _mp(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        MP_DOMAIN, service, {ATTR_ENTITY_ID: MP, **data}, blocking=True
    )


# --- config flow ---------------------------------------------------------------


async def test_config_flow(hass: HomeAssistant, device: FakeEsoteric) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CONNECTION: f"tcp://127.0.0.1:{device.port}",
            CONF_MODEL: "n_05xd",
            CONF_BAUDRATE: "9600",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Esoteric N-05XD"
    assert result["data"] == {
        CONF_CONNECTION: device.url,  # normalized
        CONF_MODEL: "n_05xd",
        CONF_BAUDRATE: 9600,
    }
    await hass.async_block_till_done()

    # Same port again is rejected.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CONNECTION: device.url, CONF_MODEL: "n_05xd", CONF_BAUDRATE: "9600"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("connection", "baudrate", "errors"),
    [
        ("http://x:1", "9600", {CONF_CONNECTION: "invalid_connection"}),
        ("socket://127.0.0.1:1", "9600", {"base": "cannot_connect"}),
        ("/dev/does-not-exist", "9600", {"base": "cannot_connect"}),
        ("socket://127.0.0.1:1", "fast", {CONF_BAUDRATE: "invalid_baudrate"}),
    ],
)
async def test_config_flow_errors(
    hass: HomeAssistant, connection: str, baudrate: str, errors: dict
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CONNECTION: connection, CONF_MODEL: "n_05xd", CONF_BAUDRATE: baudrate},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors


async def test_config_flow_port_busy(hass: HomeAssistant) -> None:
    """Single-client servers accept then reset when the port is taken."""
    device = FakeEsoteric(single_client=True)
    await device.start()
    _, other = await asyncio.open_connection("127.0.0.1", device.port)
    try:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CONNECTION: device.url, CONF_MODEL: "n_05xd", CONF_BAUDRATE: "9600"},
        )
        assert result["errors"] == {"base": "port_busy"}
    finally:
        other.close()
        await device.stop()


async def test_options_flow(hass: HomeAssistant, device: FakeEsoteric) -> None:
    entry = await _setup(hass, device)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 30, CONF_RESPONSE_TIMEOUT: 2.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.client.response_timeout == 2.0


# --- runtime -------------------------------------------------------------------


async def test_setup_and_state(hass: HomeAssistant, device: FakeEsoteric) -> None:
    entry = await _setup(hass, device)
    assert entry.state is ConfigEntryState.LOADED

    state = hass.states.get(MP)
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_INPUT_SOURCE] == "NET"
    assert state.attributes[ATTR_MEDIA_VOLUME_LEVEL] == 0.3
    assert state.attributes[ATTR_MEDIA_TRACK] == 3
    assert state.attributes[ATTR_MEDIA_POSITION] == 83
    assert state.attributes[ATTR_MEDIA_REPEAT] == "off"
    assert state.attributes[ATTR_MEDIA_SHUFFLE] is False

    assert hass.states.get("sensor.esoteric_n_05xd_sample_rate").state == "44.1kHz"
    assert hass.states.get("sensor.esoteric_n_05xd_codec").state == "FLAC"
    assert hass.states.get("number.esoteric_n_05xd_volume").state == "30.0"
    # N-05XD is a network player: no CD-only entities.
    assert hass.states.get("sensor.esoteric_n_05xd_media") is None
    # Confirmed on site: no digital output, no upconversion (DOC-MISMATCH-01/02).
    assert hass.states.get("sensor.esoteric_n_05xd_digital_output") is None
    assert hass.states.get("sensor.esoteric_n_05xd_upconversion") is None
    assert hass.states.get("button.esoteric_n_05xd_dimmer") is not None
    assert hass.states.get("button.esoteric_n_05xd_tray") is None


async def test_controls(hass: HomeAssistant, device: FakeEsoteric) -> None:
    await _setup(hass, device)
    device.received.clear()

    await _mp(hass, "volume_set", volume_level=0.424)
    assert device.values["VOLUME"] == "42.5"  # rounded to 0.5 steps
    await _mp(hass, "volume_up")
    assert device.values["VOLUME"] == "43.0"
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: "number.esoteric_n_05xd_volume", "value": 100},
        blocking=True,
    )
    assert device.values["VOLUME"] == "100.0"

    await _mp(hass, "media_pause")
    assert hass.states.get(MP).state == STATE_PAUSED

    await hass.services.async_call(
        "button",
        "press",
        {ATTR_ENTITY_ID: "button.esoteric_n_05xd_dimmer"},
        blocking=True,
    )
    assert "@KEY 5A" in device.received


async def test_n05xd_repeat_shuffle_read_only(
    hass: HomeAssistant, device: FakeEsoteric
) -> None:
    """Confirmed on site: N-05XD rejects the keys but reports the modes."""
    device.values["REPEAT"] = "ALL"
    device.values["PMODE"] = "SHUFFLE"
    await _setup(hass, device)
    attrs = hass.states.get(MP).attributes
    assert not attrs["supported_features"] & MediaPlayerEntityFeature.REPEAT_SET
    assert not attrs["supported_features"] & MediaPlayerEntityFeature.SHUFFLE_SET
    assert attrs[ATTR_MEDIA_REPEAT] == "all"
    assert attrs[ATTR_MEDIA_SHUFFLE] is True


async def test_rejected_key_is_reported(
    hass: HomeAssistant, device: FakeEsoteric
) -> None:
    """A NAK reaches the user as an error with a hint about state/menu."""
    await _setup(hass, device)
    with (
        patch.object(device, "_key", lambda code: False),
        pytest.raises(HomeAssistantError, match="current state"),
    ):
        await hass.services.async_call(
            "button",
            "press",
            {ATTR_ENTITY_ID: "button.esoteric_n_05xd_dimmer"},
            blocking=True,
        )


async def test_repeat_shuffle_on_cd_player(hass: HomeAssistant) -> None:
    device = FakeEsoteric(toggle_keys=True)
    await device.start()
    try:
        await _setup(hass, device, model="k_05xd")
        await _mp(hass, "repeat_set", repeat="one")
        assert device.values["REPEAT"] == "1"
        assert "@KEY 47" in device.received
        assert hass.states.get(MP).attributes[ATTR_MEDIA_REPEAT] == "one"
        await _mp(hass, "shuffle_set", shuffle=True)
        assert device.values["PMODE"] == "SHUFFLE"
        assert "@KEY 1F" in device.received
    finally:
        await device.stop()


async def test_select_source_by_stepping(
    hass: HomeAssistant, device: FakeEsoteric
) -> None:
    """N-05XD rejects @INPUT; the known input list is stepped with KEY 20."""
    entry = await _setup(hass, device)
    coordinator = entry.runtime_data
    assert hass.states.get(MP).attributes[ATTR_INPUT_SOURCE_LIST] == device.inputs

    await _mp(hass, "select_source", source="COAX1")
    assert device.values["INPUT"] == "COAX1"
    assert hass.states.get(MP).attributes[ATTR_INPUT_SOURCE] == "COAX1"
    assert "@INPUT COAX1" not in device.received
    # Wrapping around the end of the list.
    await _mp(hass, "select_source", source="NET")
    assert device.values["INPUT"] == "NET"

    # An input the list didn't know about is learned when seen.
    device.inputs.insert(1, "HDMI")
    device._key("20")  # noqa: SLF001
    await coordinator.async_refresh()
    assert coordinator.learned_inputs[-1] == "HDMI"


async def test_select_source_direct(hass: HomeAssistant, device: FakeEsoteric) -> None:
    device.direct_input = True
    device.inputs = ["XLR1", "XLR2", "ESLA1", "RCA1"]
    device.values["INPUT"] = "XLR1"
    await _setup(hass, device, model="grandioso_c1x")
    mp = "media_player.esoteric_n_05xd"
    assert "ESLA1" in hass.states.get(mp).attributes[ATTR_INPUT_SOURCE_LIST]
    await hass.services.async_call(
        MP_DOMAIN,
        "select_source",
        {ATTR_ENTITY_ID: mp, ATTR_INPUT_SOURCE: "ESLA1"},
        blocking=True,
    )
    assert "@INPUT ESLA1" in device.received
    assert device.values["INPUT"] == "ESLA1"


async def test_power_and_standby(hass: HomeAssistant, device: FakeEsoteric) -> None:
    """N-05XD: standby reports "@INPUT OFF"; POWER ON replies 0x83, not ACK."""
    device.values["PMODE"] = "SHUFFLE"
    entry = await _setup(hass, device)
    coordinator = entry.runtime_data
    await _mp(hass, "turn_off")
    assert device.power is False
    assert hass.states.get(MP).state == STATE_OFF

    await coordinator.async_refresh()
    assert hass.states.get(MP).state == STATE_OFF
    assert hass.states.get("sensor.esoteric_n_05xd_codec").state == STATE_UNAVAILABLE
    # Standby defaults (PMODE CONTINUE) don't overwrite the real values.
    assert coordinator.data.value("PMODE") == "SHUFFLE"
    device.received.clear()
    await coordinator.async_refresh()
    assert device.received == ["@?INPUT"]

    # Someone presses the power button: noticed by polling.
    device.power = True
    await coordinator.async_refresh()
    assert hass.states.get(MP).state == STATE_PLAYING
    device.power = False
    await coordinator.async_refresh()
    assert hass.states.get(MP).state == STATE_OFF

    # DOC-MISMATCH-06: no ACK for POWER ON, but it is not an error.
    with patch("custom_components.esoteric.coordinator.POWER_SETTLE", 0.01):
        await _mp(hass, "turn_on")
    assert device.power is True
    assert hass.states.get(MP).state == STATE_PLAYING


async def test_power_on_right_after_off_is_retried(
    hass: HomeAssistant, device: FakeEsoteric
) -> None:
    """DOC-MISMATCH-08: POWER ON during shutdown is ACKed but ignored."""
    device.power_on_lockout = 0.3
    await _setup(hass, device)
    with patch("custom_components.esoteric.coordinator.POWER_SETTLE", 0.1):
        await _mp(hass, "turn_off")
        await _mp(hass, "turn_on")
    assert device.power is True
    assert device.received.count("@POWER ON") > 1
    assert hass.states.get(MP).state == STATE_PLAYING
    assert hass.states.get(MP).attributes[ATTR_INPUT_SOURCE] == "NET"


async def test_power_on_gives_up(hass: HomeAssistant, device: FakeEsoteric) -> None:
    device.power_on_lockout = 60
    await _setup(hass, device)
    with (
        patch("custom_components.esoteric.coordinator.POWER_SETTLE", 0.05),
        patch("custom_components.esoteric.coordinator.POWER_ON_RETRY_WINDOW", 0.3),
    ):
        await _mp(hass, "turn_off")
        with pytest.raises(HomeAssistantError, match="stayed in standby"):
            await _mp(hass, "turn_on")
    assert hass.states.get(MP).state == STATE_OFF


@pytest.mark.parametrize("mode", ["nak", "silent"])
async def test_standby_without_input_off(hass: HomeAssistant, mode: str) -> None:
    """Units that NAK or ignore requests in standby are also seen as off."""
    device = FakeEsoteric()
    device.standby_mode = mode
    device.power_on_reply = b"\x06"
    await device.start()
    try:
        entry = await _setup(hass, device, model="k_05xd")
        device.power = False
        await entry.runtime_data.async_refresh()
        assert hass.states.get(MP).state == STATE_OFF
        # Only a few requests are probed in standby, not the whole list.
        device.received.clear()
        await entry.runtime_data.async_refresh()
        assert len(device.received) == 3
        await _mp(hass, "turn_on")
        await entry.runtime_data.async_refresh()
        assert hass.states.get(MP).state == STATE_PLAYING
    finally:
        await device.stop()


async def test_power_on_failure_is_reported(
    hass: HomeAssistant, device: FakeEsoteric
) -> None:
    """No ACK and the unit still reads as off: that is an error."""
    entry = await _setup(hass, device)
    await _mp(hass, "turn_off")
    device.power_on_reply = b""
    with (
        patch.object(device, "_on_line", lambda line, writer: None),
        patch("custom_components.esoteric.coordinator.POWER_SETTLE", 0.01),
        pytest.raises(HomeAssistantError),
    ):
        await _mp(hass, "turn_on")
    assert entry.runtime_data.data.power is False


async def test_unsupported_request_is_dropped(
    hass: HomeAssistant, device: FakeEsoteric
) -> None:
    device.unsupported.add("CODEC")
    entry = await _setup(hass, device)
    coordinator = entry.runtime_data
    for _ in range(3):
        await coordinator.async_refresh()
    assert "CODEC" in coordinator.data.unsupported
    assert hass.states.get("sensor.esoteric_n_05xd_codec").state == STATE_UNAVAILABLE
    device.received.clear()
    await coordinator.async_refresh()
    assert "@?CODEC" not in device.received

    # Retried now and then, in case it only answers in some states.
    device.unsupported.discard("CODEC")
    coordinator._polls = UNSUPPORTED_RECHECK - 1  # noqa: SLF001
    await coordinator.async_refresh()
    assert "CODEC" not in coordinator.data.unsupported
    assert hass.states.get("sensor.esoteric_n_05xd_codec").state == "FLAC"


async def test_push_updates_from_other_clients(
    hass: HomeAssistant, device: FakeEsoteric
) -> None:
    await _setup(hass, device)
    _, other = await asyncio.open_connection("127.0.0.1", device.port)
    device.values["VOLUME"] = "55.0"
    other.write(b"@?VOLUME\r")
    await other.drain()
    async with asyncio.timeout(3):
        while hass.states.get("number.esoteric_n_05xd_volume").state != "55.0":
            await asyncio.sleep(0.01)
    other.close()


async def test_connection_loss(hass: HomeAssistant, device: FakeEsoteric) -> None:
    await _setup(hass, device)
    await device.drop_clients()
    async with asyncio.timeout(3):
        while hass.states.get(MP).state != STATE_UNAVAILABLE:
            await asyncio.sleep(0.01)
    # Reconnects and refreshes by itself.
    async with asyncio.timeout(5):
        while hass.states.get(MP).state != STATE_PLAYING:
            await asyncio.sleep(0.05)


async def test_setup_retry_when_unreachable(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_CONNECTION: "socket://127.0.0.1:1",
            CONF_MODEL: "n_05xd",
            CONF_BAUDRATE: 9600,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.esoteric.CONNECT_TIMEOUT", 0.2):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_send_command_service(hass: HomeAssistant, device: FakeEsoteric) -> None:
    entry = await _setup(hass, device)

    async def send(command: str):
        return await hass.services.async_call(
            DOMAIN,
            "send_command",
            {"config_entry_id": entry.entry_id, "command": command},
            blocking=True,
            return_response=True,
        )

    assert await send("?FS") == {"result": "response", "response": "@FS 44.1kHz"}
    assert await send("KEY 5A") == {"result": "ack", "response": None}
    assert await send("@NONSENSE") == {"result": "nak", "response": None}


async def test_unload(hass: HomeAssistant, device: FakeEsoteric) -> None:
    entry = await _setup(hass, device)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    async with asyncio.timeout(3):
        while device.client_count:
            await asyncio.sleep(0.01)


async def test_all_models_set_up(hass: HomeAssistant, device: FakeEsoteric) -> None:
    """Every model's entities can be created (translations, features)."""
    from custom_components.esoteric.models import MODELS  # noqa: PLC0415

    strings = json.loads(
        (
            Path(__file__).parent.parent / "custom_components/esoteric/strings.json"
        ).read_text()
    )["entity"]
    registry = er.async_get(hass)
    for model_id in MODELS:
        entry = await _setup(hass, device, model=model_id)
        assert entry.state is ConfigEntryState.LOADED, model_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.domain == MP_DOMAIN:
                continue
            assert entity.translation_key in strings[entity.domain], entity.entity_id
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()


async def test_diagnostics(hass: HomeAssistant, device: FakeEsoteric) -> None:
    """Diagnostics must be JSON serializable (the download button)."""
    from custom_components.esoteric.diagnostics import (  # noqa: PLC0415
        async_get_config_entry_diagnostics,
    )

    entry = await _setup(hass, device)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    json.dumps(diag)
    assert diag["power"] is True
    assert diag["last_messages"]["INPUT"] == "@INPUT NET"
    assert diag["network"] is None


async def test_volume_100_reported_as_10(hass: HomeAssistant, device: FakeEsoteric):
    """DOC-MISMATCH-09: at 100.0 the unit answers "@VOLUME 10.0"."""
    entry = await _setup(hass, device)
    number = "number.esoteric_n_05xd_volume"

    async def set_volume(value: float) -> None:
        await hass.services.async_call(
            "number",
            "set_value",
            {ATTR_ENTITY_ID: number, "value": value},
            blocking=True,
        )

    await set_volume(100)
    assert device.values["VOLUME"] == "100.0"
    assert hass.states.get(number).state == "100.0"
    assert hass.states.get(MP).attributes[ATTR_MEDIA_VOLUME_LEVEL] == 1.0
    await entry.runtime_data.async_refresh()  # polling keeps it at 100
    assert hass.states.get(number).state == "100.0"

    # Knob from 100 down one step, then back up: still read correctly.
    device.values["VOLUME"] = "99.5"
    await entry.runtime_data.async_refresh()
    assert hass.states.get(number).state == "99.5"
    device.values["VOLUME"] = "100.0"
    await entry.runtime_data.async_refresh()
    assert hass.states.get(number).state == "100.0"

    # Volume down from 100 uses the real value.
    await _mp(hass, "volume_down")
    assert device.values["VOLUME"] == "99.5"

    # A genuine 10.0, set from HA or stepped to, stays 10.0.
    await set_volume(10)
    assert hass.states.get(number).state == "10.0"
    await entry.runtime_data.async_refresh()
    assert hass.states.get(number).state == "10.0"
    device.values["VOLUME"] = "10.5"
    await entry.runtime_data.async_refresh()
    device.values["VOLUME"] = "10.0"
    await entry.runtime_data.async_refresh()
    assert hass.states.get(number).state == "10.0"


async def test_volume_100_survives_restart(hass: HomeAssistant, device: FakeEsoteric):
    entry = await _setup(hass, device)
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: "number.esoteric_n_05xd_volume", "value": 100},
        blocking=True,
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()  # stored
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("number.esoteric_n_05xd_volume").state == "100.0"


async def test_old_input_storage_format(
    hass: HomeAssistant, device: FakeEsoteric, hass_storage
) -> None:
    """Version 1 stored a bare list of learned inputs."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Esoteric F-01",
        unique_id=device.url,
        data={CONF_CONNECTION: device.url, CONF_MODEL: "f_01", CONF_BAUDRATE: 9600},
    )
    hass_storage[f"{DOMAIN}.{entry.entry_id}.inputs"] = {
        "version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}.inputs",
        "data": ["PHONO", "LINE 1"],
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.learned_inputs[:2] == ["PHONO", "LINE 1"]
