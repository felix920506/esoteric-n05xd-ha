"""Media player entity."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import STANDBY_INPUT, EsotericConfigEntry, EsotericCoordinator
from .entity import EsotericEntity
from .models import NET_INPUT, REQ_INPUT, REQ_PMODE, REQ_PSTS, REQ_REPEAT, Category
from .network import EsotericNetwork, NetworkError

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
# OpenHome transport states in which Info/Time describe what is playing. When
# stopped, Info still holds the previous track.
NETWORK_STATES = {
    "Playing": MediaPlayerState.PLAYING,
    "Paused": MediaPlayerState.PAUSED,
    "Buffering": MediaPlayerState.BUFFERING,
}
NETWORK_ACTIVE = tuple(NETWORK_STATES)


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
        self._base_features = features
        if model.categories & {Category.AMP, Category.PHONO}:
            self._attr_device_class = MediaPlayerDeviceClass.RECEIVER

    @property
    def _state(self):
        return self.coordinator.data

    @property
    def _net(self) -> EsotericNetwork | None:
        """The network side, while connected and the unit is on NET."""
        network = self.coordinator.network
        if network is None or not network.connected:
            return None
        if self._state.value(REQ_INPUT) != NET_INPUT:
            return None
        return network

    @property
    def _net_active(self) -> EsotericNetwork | None:
        """The network side, while it is the one playing."""
        network = self._net
        if network is None or network.state.transport_state not in NETWORK_ACTIVE:
            return None
        return network

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Features; repeat/shuffle/seek come from the network side if any."""
        features = self._base_features
        if (net := self._net) is not None:
            if net.state.can_repeat:
                features |= MediaPlayerEntityFeature.REPEAT_SET
            if net.state.can_shuffle:
                features |= MediaPlayerEntityFeature.SHUFFLE_SET
            if net.state.can_seek and self._net_active is not None:
                features |= MediaPlayerEntityFeature.SEEK
        return features

    @property
    def media_title(self) -> str | None:
        """Track title (network side only)."""
        net = self._net_active
        return net.state.track.title if net else None

    @property
    def media_artist(self) -> str | None:
        """Artist (network side only)."""
        net = self._net_active
        return net.state.track.artist if net else None

    @property
    def media_album_name(self) -> str | None:
        """Album (network side only)."""
        net = self._net_active
        return net.state.track.album if net else None

    @property
    def media_album_artist(self) -> str | None:
        """Album artist (network side only)."""
        net = self._net_active
        return net.state.track.album_artist if net else None

    @property
    def media_image_url(self) -> str | None:
        """Artwork (network side only); proxied by Home Assistant."""
        net = self._net_active
        return net.state.track.image_url if net else None

    @property
    def media_duration(self) -> int | None:
        """Track length (network side only)."""
        net = self._net_active
        return net.state.duration if net else None

    @property
    def _playback(self) -> tuple[str, ...]:
        return self._state.args(REQ_PSTS) if self.coordinator.model.playback else ()

    @property
    def state(self) -> MediaPlayerState:
        """Current state."""
        if self._state.power is False:
            return MediaPlayerState.OFF
        if (net := self._net_active) is not None:
            # Pushed by the network player; RS-232 PSTS is only polled.
            return NETWORK_STATES[net.state.transport_state]
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
        if (net := self._net_active) is not None and net.state.position is not None:
            return net.state.position
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
        if (net := self._net_active) is not None and net.state.position_at:
            return datetime.fromtimestamp(net.state.position_at, UTC)
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
        if (net := self._net) is not None and net.state.repeat is not None:
            return RepeatMode.ALL if net.state.repeat else RepeatMode.OFF
        return REPEAT_TO_HA.get((self._state.value(REQ_REPEAT) or "").upper())

    @property
    def shuffle(self) -> bool | None:
        """Shuffle on/off."""
        if (net := self._net) is not None and net.state.shuffle is not None:
            return net.state.shuffle
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
        """Set repeat over the network if possible, else press REPEAT."""
        if (net := self._net) is not None and net.state.can_repeat:
            if repeat is RepeatMode.ONE:
                raise ServiceValidationError(
                    "Repeat one is not supported; use repeat all or off"
                )
            await self._network_call(net.async_set_repeat(repeat is RepeatMode.ALL))
            return
        playback = self.coordinator.model.playback
        if playback is None or not self.coordinator.model.repeat_shuffle_keys:
            raise HomeAssistantError(
                "Repeat can only be set over the network connection on this model"
            )
        target = HA_TO_REPEAT[repeat]
        await self.coordinator.async_cycle(
            playback.repeat, REQ_REPEAT, lambda msg: msg.value.upper() == target, 3
        )

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Set shuffle over the network if possible, else press SHUFFLE."""
        if (net := self._net) is not None and net.state.can_shuffle:
            await self._network_call(net.async_set_shuffle(shuffle))
            return
        playback = self.coordinator.model.playback
        if playback is None or not self.coordinator.model.repeat_shuffle_keys:
            raise HomeAssistantError(
                "Shuffle can only be set over the network connection on this model"
            )
        await self.coordinator.async_cycle(
            playback.shuffle,
            REQ_PMODE,
            lambda msg: (msg.value.upper() == SHUFFLE) == shuffle,
            3,
        )

    async def async_media_seek(self, position: float) -> None:
        """Seek (network side only)."""
        net = self._net_active
        if net is None:
            raise HomeAssistantError("Seeking needs the network connection")
        await self._network_call(net.async_seek(position))

    async def _network_call(self, call) -> None:
        try:
            await call
        except NetworkError as err:
            raise HomeAssistantError(str(err)) from err

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
