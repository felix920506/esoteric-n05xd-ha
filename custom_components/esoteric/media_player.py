"""Media player entity."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import STANDBY_INPUT, EsotericConfigEntry, EsotericCoordinator
from .entity import EsotericEntity
from .models import REQ_INPUT, REQ_PMODE, REQ_PSTS, REQ_REPEAT, Category

PARALLEL_UPDATES = 1

PLAYBACK_STATES = {
    "PLAY": MediaPlayerState.PLAYING,
    "PAUSE": MediaPlayerState.PAUSED,
    "STOP": MediaPlayerState.IDLE,
}
REPEAT_TO_HA = {"OFF": RepeatMode.OFF, "ALL": RepeatMode.ALL, "1": RepeatMode.ONE}
HA_TO_REPEAT = {mode: value for value, mode in REPEAT_TO_HA.items()}
SHUFFLE = "SHUFFLE"
# @PSTS PLAY <track> <min> <sec> <TE|TR|DE|DR>; only TE is a track position.
TRACK_ELAPSED = "TE"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EsotericConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the media player."""
    async_add_entities([EsotericMediaPlayer(entry.runtime_data)])


class EsotericMediaPlayer(EsotericEntity, MediaPlayerEntity):
    """The unit as a media player."""

    _attr_name = None
    _attr_media_content_type = MediaType.MUSIC

    def __init__(self, coordinator: EsotericCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "media_player")
        model = coordinator.model
        features = MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF
        if model.playback:
            features |= (
                MediaPlayerEntityFeature.PLAY
                | MediaPlayerEntityFeature.PAUSE
                | MediaPlayerEntityFeature.STOP
                | MediaPlayerEntityFeature.NEXT_TRACK
                | MediaPlayerEntityFeature.PREVIOUS_TRACK
            )
            if model.repeat_shuffle_keys:
                # The N-05XD rejects these keys (DOC-MISMATCH-03 / -04); its
                # repeat and shuffle state is still shown, read-only.
                features |= (
                    MediaPlayerEntityFeature.REPEAT_SET
                    | MediaPlayerEntityFeature.SHUFFLE_SET
                )
        if model.volume:
            features |= (
                MediaPlayerEntityFeature.VOLUME_SET
                | MediaPlayerEntityFeature.VOLUME_STEP
            )
        if model.direct_inputs or model.input_next_code:
            features |= MediaPlayerEntityFeature.SELECT_SOURCE
        self._attr_supported_features = features
        if model.categories & {Category.AMP, Category.PHONO}:
            self._attr_device_class = MediaPlayerDeviceClass.RECEIVER

    @property
    def _state(self):
        return self.coordinator.data

    @property
    def _playback(self) -> tuple[str, ...]:
        return self._state.args(REQ_PSTS) if self.coordinator.model.playback else ()

    @property
    def state(self) -> MediaPlayerState:
        """Current state."""
        if self._state.power is False:
            return MediaPlayerState.OFF
        if playback := self._playback:
            return PLAYBACK_STATES.get(playback[0].upper(), MediaPlayerState.ON)
        return MediaPlayerState.ON

    @property
    def media_track(self) -> int | None:
        """Track number."""
        try:
            return int(self._playback[1])
        except (IndexError, ValueError):
            return None

    @property
    def media_position(self) -> int | None:
        """Elapsed time within the track."""
        playback = self._playback
        if len(playback) < 5 or playback[4] != TRACK_ELAPSED:
            return None
        try:
            return int(playback[2]) * 60 + int(playback[3])
        except ValueError:
            return None

    @property
    def media_position_updated_at(self) -> datetime | None:
        """When the position was read."""
        if self.media_position is None:
            return None
        return self._state.received_at.get(REQ_PSTS)

    @property
    def volume_level(self) -> float | None:
        """Volume 0..1."""
        volume, steps = self.coordinator.model.volume, self._state.volume
        if volume is None or steps is None:
            return None
        return min(max(steps / volume.maximum, 0.0), 1.0)

    @property
    def source(self) -> str | None:
        """Current input."""
        source = self._state.value(REQ_INPUT)
        return None if source == STANDBY_INPUT else source

    @property
    def source_list(self) -> list[str] | None:
        """Selectable inputs."""
        model = self.coordinator.model
        if model.direct_inputs:
            return list(model.direct_inputs)
        if model.input_next_code:
            return list(self.coordinator.learned_inputs)
        return None

    @property
    def repeat(self) -> RepeatMode | None:
        """Repeat mode."""
        return REPEAT_TO_HA.get((self._state.value(REQ_REPEAT) or "").upper())

    @property
    def shuffle(self) -> bool | None:
        """Shuffle on/off."""
        mode = self._state.value(REQ_PMODE)
        return None if mode is None else mode.upper() == SHUFFLE

    async def async_turn_on(self) -> None:
        """Power on."""
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self) -> None:
        """Standby."""
        await self.coordinator.async_set_power(False)

    async def _playback_key(self, attr: str) -> None:
        playback = self.coordinator.model.playback
        assert playback is not None
        await self.coordinator.async_press(getattr(playback, attr))
        await self.coordinator.async_refresh_key(REQ_PSTS)

    async def async_media_play(self) -> None:
        """Play."""
        await self._playback_key("play")

    async def async_media_pause(self) -> None:
        """Pause."""
        await self._playback_key("pause")

    async def async_media_stop(self) -> None:
        """Stop."""
        await self._playback_key("stop")

    async def async_media_next_track(self) -> None:
        """Next track."""
        await self._playback_key("next")

    async def async_media_previous_track(self) -> None:
        """Previous track."""
        await self._playback_key("previous")

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        """Press REPEAT until the requested mode is reached."""
        playback = self.coordinator.model.playback
        assert playback is not None
        target = HA_TO_REPEAT[repeat]
        await self.coordinator.async_cycle(
            playback.repeat, REQ_REPEAT, lambda msg: msg.value.upper() == target, 3
        )

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Press SHUFFLE until the requested state is reached."""
        playback = self.coordinator.model.playback
        assert playback is not None
        await self.coordinator.async_cycle(
            playback.shuffle,
            REQ_PMODE,
            lambda msg: (msg.value.upper() == SHUFFLE) == shuffle,
            3,
        )

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume 0..1."""
        model_volume = self.coordinator.model.volume
        assert model_volume is not None
        await self.coordinator.async_set_volume(volume * model_volume.maximum)

    async def async_volume_up(self) -> None:
        """One step up."""
        await self.coordinator.async_step_volume(up=True)

    async def async_volume_down(self) -> None:
        """One step down."""
        await self.coordinator.async_step_volume(up=False)

    async def async_select_source(self, source: str) -> None:
        """Select an input."""
        await self.coordinator.async_select_input(source)
