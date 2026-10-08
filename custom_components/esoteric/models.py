"""Per-model capabilities, from the RS-232C command table rev 1.5."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Category(StrEnum):
    """Command groups in the command table."""

    COMMON = "common"
    CD = "cd"
    NETWORK = "network"
    AMP = "amp"
    PHONO = "phono"


@dataclass(frozen=True)
class KeyCommand:
    """A ``@KEY <code>`` remote-control key."""

    key: str  # entity translation key / unique id suffix
    code: str
    enabled_default: bool = True

    @property
    def command(self) -> str:
        """The command body."""
        return f"KEY {self.code}"


@dataclass(frozen=True)
class PlaybackKeys:
    """Playback key codes."""

    play: str
    pause: str
    stop: str
    previous: str
    next: str
    repeat: str
    shuffle: str


# 1-5-1-1 Common
COMMON_KEYS = (KeyCommand("dimmer", "5A"),)

# 1-5-1-2 CD player
CD_KEYS = (
    KeyCommand("tray", "00"),
    KeyCommand("rewind", "40", enabled_default=False),
    KeyCommand("fast_forward", "41", enabled_default=False),
    *(
        KeyCommand(f"digit_{digit}", code, enabled_default=False)
        for digit, code in zip(
            range(10),
            ("04", "05", "06", "07", "0D", "0E", "0F", "15", "16", "17"),
            strict=True,
        )
    ),
    KeyCommand("plus_10", "4F", enabled_default=False),
    KeyCommand("clear", "4B", enabled_default=False),
    KeyCommand("play_mode", "1E"),
    KeyCommand("play_area", "49"),
    KeyCommand("mode", "1D"),
    KeyCommand("display", "42"),
)
CD_TRANSPORT = PlaybackKeys(
    play="01",
    pause="02",
    stop="03",
    previous="0B",
    next="0C",
    repeat="47",
    shuffle="1F",
)

# 1-5-1-3 AMP
AMP_KEYS = (
    KeyCommand("input_previous", "43"),
    KeyCommand("input_next", "44"),
    KeyCommand("mute", "1C"),
    KeyCommand("setup_menu", "14", enabled_default=False),
    KeyCommand("input_esla", "32", enabled_default=False),
    KeyCommand("input_xlr", "33", enabled_default=False),
    KeyCommand("input_rca", "34", enabled_default=False),
    KeyCommand("input_phono", "35", enabled_default=False),
    KeyCommand("input_option", "36", enabled_default=False),
    KeyCommand("output", "37"),
)
AMP_VOLUME_UP = "12"
AMP_VOLUME_DOWN = "13"

# 1-5-1-4 Network / DAC
NETWORK_KEYS = (
    KeyCommand("input_next", "20"),
    KeyCommand("menu", "21", enabled_default=False),
    KeyCommand("cursor_left", "22", enabled_default=False),
    KeyCommand("cursor_right", "23", enabled_default=False),
)
NETWORK_TRANSPORT = PlaybackKeys(
    play="26",
    pause="27",
    stop="28",
    previous="24",
    next="25",
    repeat="29",  # not accepted by the N-05XD (DOC-MISMATCH-03)
    shuffle="2A",  # not accepted by the N-05XD (DOC-MISMATCH-04)
)

# 1-5-1-5 Phono EQ
PHONO_KEYS = (
    KeyCommand("subsonic", "60"),
    KeyCommand("mono", "61"),
    KeyCommand("output", "62"),
    KeyCommand("load_preset", "63"),
    KeyCommand("low_limit", "69", enabled_default=False),
    KeyCommand("turnover", "6B", enabled_default=False),
    KeyCommand("roll_off", "6A", enabled_default=False),
    KeyCommand("eq_previous", "6D"),
    KeyCommand("eq_next", "6C"),
    KeyCommand("output_xlr", "6E", enabled_default=False),
    KeyCommand("output_rca", "6F", enabled_default=False),
    KeyCommand("output_opt", "70", enabled_default=False),
    KeyCommand("cartridge_gain_down", "72"),
    KeyCommand("cartridge_gain_up", "71"),
    KeyCommand("mm_mc", "73"),
    KeyCommand("cap_imp", "74", enabled_default=False),
    KeyCommand("led_off", "75", enabled_default=False),
    KeyCommand("output_gain", "76", enabled_default=False),
    KeyCommand("gain_down", "78"),
    KeyCommand("gain_up", "77"),
    KeyCommand("demag", "79"),
)

KEYS_BY_CATEGORY: dict[Category, tuple[KeyCommand, ...]] = {
    Category.COMMON: COMMON_KEYS,
    Category.CD: CD_KEYS,
    Category.AMP: AMP_KEYS,
    Category.NETWORK: NETWORK_KEYS,
    Category.PHONO: PHONO_KEYS,
}

# Request commands (1-5-2). Each is sent as "@?<KEY>" and answered "@<KEY> ...".
REQ_INPUT = "INPUT"
REQ_AOUT = "AOUT"
REQ_DOUT = "DOUT"  # not on N-05XD (DOC-MISMATCH-01, resolved)
REQ_MEDIA = "MEDIA"
REQ_PSTS = "PSTS"  # DOC-MISMATCH-07 on N-05XD (AirPlay only)
REQ_PMODE = "PMODE"
REQ_REPEAT = "REPEAT"
REQ_UPCONV = "UPCONV"  # not on N-05XD (DOC-MISMATCH-02, resolved)
REQ_FS = "FS"  # "NON" while idle (DOC-MISMATCH-05, resolved)
REQ_MQA = "MQA"
REQ_CODEC = "CODEC"  # empty while idle (DOC-MISMATCH-05, resolved)
REQ_VOLUME = "VOLUME"
REQ_OUTPUT = "OUTPUT"
REQ_EQ = "EQ"
REQ_GAIN = "GAIN"
REQ_CGAIN = "CGAIN"

COMMON_REQUESTS = (REQ_INPUT, REQ_AOUT, REQ_DOUT)
CD_REQUESTS = (REQ_MEDIA, REQ_PSTS, REQ_PMODE, REQ_REPEAT, REQ_UPCONV, REQ_FS, REQ_MQA)
NETWORK_REQUESTS = (
    REQ_PSTS,
    REQ_PMODE,
    REQ_REPEAT,
    REQ_UPCONV,
    REQ_FS,
    REQ_CODEC,
    REQ_MQA,
    REQ_VOLUME,
)
AMP_REQUESTS = (REQ_VOLUME,)
PHONO_REQUESTS = (REQ_OUTPUT, REQ_EQ, REQ_GAIN, REQ_CGAIN)

ALL_REQUESTS = tuple(
    dict.fromkeys(
        COMMON_REQUESTS + CD_REQUESTS + NETWORK_REQUESTS + AMP_REQUESTS + PHONO_REQUESTS
    )
)


@dataclass(frozen=True)
class Volume:
    """Volume range in device steps."""

    maximum: float
    step: float
    # True: volume up/down via KEY 12/13. False: via "@VOLUME <n>".
    use_keys: bool


@dataclass(frozen=True)
class ModelInfo:
    """What a model supports."""

    model_id: str
    name: str
    categories: frozenset[Category]
    requests: tuple[str, ...]
    volume: Volume | None = None
    # Inputs accepted by "@INPUT <name>" (empty: no direct input select).
    direct_inputs: tuple[str, ...] = ()
    # Inputs in INPUT+ order, for models that select by stepping.
    known_inputs: tuple[str, ...] = ()
    # Whether the REPEAT / SHUFFLE keys can be used to set those modes.
    repeat_shuffle_keys: bool = True
    # Flip to True once verified against real hardware.
    tested: bool = False
    is_generic: bool = False
    keys: tuple[KeyCommand, ...] = field(init=False)

    def __post_init__(self) -> None:
        """Collect the key commands for this model's categories."""
        keys: dict[str, KeyCommand] = {}
        for category in Category:
            if category in self.categories:
                for key in KEYS_BY_CATEGORY[category]:
                    keys.setdefault(key.key, key)
        object.__setattr__(self, "keys", tuple(keys.values()))

    @property
    def playback(self) -> PlaybackKeys | None:
        """Playback key codes, for players."""
        if Category.CD in self.categories:
            return CD_TRANSPORT
        if Category.NETWORK in self.categories:
            return NETWORK_TRANSPORT
        return None

    @property
    def input_next_code(self) -> str | None:
        """Key code that steps to the next input, if any."""
        if Category.AMP in self.categories:
            return "44"
        if Category.NETWORK in self.categories:
            return "20"
        return None


def _requests(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(req for group in groups for req in group))


_C, _CD, _N, _A, _P = (
    Category.COMMON,
    Category.CD,
    Category.NETWORK,
    Category.AMP,
    Category.PHONO,
)

C1X_INPUTS = ("XLR1", "XLR2", "XLR3", "ESLA1", "ESLA2", "ESLA3", "RCA1", "RCA2")
# Differences between the command table (rev 1.5) and a real N-05XD.
# 2026-10-05: remote, unit stopped on NET. 2026-10-07: on site, webcam on the
# front panel, including AirPlay playback. The unit's menu settings may be
# setup specific and some behavior is source/state dependent, so the code
# keeps the documented behavior unless noted. Grep for the tag.
#
#   DOC-MISMATCH-01  RESOLVED: @?DOUT (common request) -> NAK idle and
#                    playing; the N-05XD has no digital output. Not polled.
#   DOC-MISMATCH-02  RESOLVED: @?UPCONV (network request) -> NAK idle and
#                    playing; no upconversion setting in the N-05XD's menu.
#                    Not polled.
#   DOC-MISMATCH-03  RESOLVED: @KEY 29 (network REPEAT) -> NAK in every state
#                    tried: idle, AirPlay, and the unit's own (OpenHome)
#                    playlist playing over DLNA with repeat active. KEY 47 /
#                    1F / 1E (CD codes) too. @?REPEAT does follow changes
#                    made by other controllers, so repeat is read-only.
#   DOC-MISMATCH-04  RESOLVED: @KEY 2A (network SHUFFLE), as -03; @?PMODE
#                    follows changes made elsewhere. Shuffle is read-only.
#   DOC-MISMATCH-05  RESOLVED: "@CODEC" with no value and "@FS NON" only while
#                    nothing plays; AirPlay gave "@CODEC AAC", "@FS 44.1kHz".
#   DOC-MISMATCH-06  @POWER ON -> 0x83 instead of ACK (0x06) every time it
#                    actually powers on. Handled: readback decides.
#   DOC-MISMATCH-07  @?PSTS stays "PLAY 0 0 00 TE" while AirPlay is paused
#                    (the display icon too); no track/time for AirPlay.
#                    AirPlay only: over DLNA PAUSE, track and time are right.
#                    Shown as reported.
#   DOC-MISMATCH-08  @POWER ON within ~6-8 s of POWER OFF is ACKed (0x06) but
#                    ignored; the unit stays in standby. Once it acts on POWER
#                    ON it replies 0x83 (see -06). Handled: re-sent until
#                    INPUT is no longer OFF.
#
# Not in the table, observed on the N-05XD:
#   * Standby: every request is still answered, "@INPUT OFF", "@AOUT OFF" and
#     defaults (PMODE CONTINUE, REPEAT OFF); commands are ACKed. "INPUT OFF"
#     is used as the power state.
#   * POWER OFF: ACK in ~30 ms. POWER ON: about 30 s of "Initialize" before
#     the unit is ready, although it answers requests at once.
#   * MENU (KEY 21) steps through setup items rather than toggling the menu;
#     < / > (KEY 22/23) change the shown setting immediately. Menu closes on
#     its own after a few seconds.
#   * Dimmer (KEY 5A) cycles brightness levels, shown as "DIMMER n".
#   * Direct "@INPUT <name>" is NAKed (the table documents it only for C1X /
#     C1X solo / E1), as are the CD-group REPEAT/SHUFFLE codes KEY 47 / 1F.

# The input that plays from the network module (OpenHome renderer).
NET_INPUT = "NET"

# Read from a real N-05XD by pressing INPUT+ (KEY 20).
N05XD_INPUTS = (
    "NET",
    "Bluetooth",
    "LINE1",
    "LINE2",
    "XLR",
    "COAX1",
    "COAX2",
    "OPT1",
    "OPT2",
    "USB",
)
E1_INPUTS = ("XLR1", "XLR2", "XLR3", "RCA", "OPT")

MODELS: dict[str, ModelInfo] = {
    model.model_id: model
    for model in (
        ModelInfo(
            "n_05xd",
            "N-05XD",
            frozenset({_C, _N}),
            # As documented, except where a DOC-MISMATCH-xx above was
            # confirmed on site: no DOUT (-01) or UPCONV (-02); repeat and
            # shuffle are read-only (-03/-04).
            tuple(
                req
                for req in _requests(COMMON_REQUESTS, NETWORK_REQUESTS)
                if req not in (REQ_DOUT, REQ_UPCONV)
            ),
            volume=Volume(maximum=100.0, step=0.5, use_keys=False),
            known_inputs=N05XD_INPUTS,
            repeat_shuffle_keys=False,
            tested=True,
        ),
        ModelInfo(
            "k_01xd",
            "K-01XD",
            frozenset({_C, _CD}),
            _requests(COMMON_REQUESTS, CD_REQUESTS),
        ),
        ModelInfo(
            "k_03xd",
            "K-03XD",
            frozenset({_C, _CD}),
            _requests(COMMON_REQUESTS, CD_REQUESTS),
        ),
        ModelInfo(
            "k_05xd",
            "K-05XD",
            frozenset({_C, _CD}),
            _requests(COMMON_REQUESTS, CD_REQUESTS),
        ),
        ModelInfo(
            "grandioso_c1x",
            "Grandioso C1X",
            frozenset({_C, _A}),
            _requests((REQ_INPUT, REQ_AOUT), AMP_REQUESTS),
            volume=Volume(maximum=99.9, step=0.1, use_keys=True),
            direct_inputs=C1X_INPUTS,
        ),
        ModelInfo(
            "grandioso_c1x_solo",
            "Grandioso C1X solo",
            frozenset({_C, _A}),
            _requests((REQ_INPUT, REQ_AOUT), AMP_REQUESTS),
            volume=Volume(maximum=99.9, step=0.1, use_keys=True),
            direct_inputs=C1X_INPUTS,
        ),
        ModelInfo(
            "grandioso_e1",
            "Grandioso E1",
            frozenset({_C, _P}),
            _requests((REQ_INPUT,), PHONO_REQUESTS),
            direct_inputs=E1_INPUTS,
        ),
        ModelInfo(
            "f_01",
            "F-01",
            frozenset({_C, _A}),
            _requests((REQ_INPUT, REQ_AOUT), AMP_REQUESTS),
            volume=Volume(maximum=99.9, step=0.1, use_keys=True),
        ),
        ModelInfo(
            "f_02",
            "F-02",
            frozenset({_C, _A}),
            _requests((REQ_INPUT, REQ_AOUT), AMP_REQUESTS),
            volume=Volume(maximum=99.9, step=0.1, use_keys=True),
        ),
        ModelInfo(
            "generic",
            "Other / generic",
            frozenset({_C}),
            ALL_REQUESTS,
            is_generic=True,
        ),
    )
}
