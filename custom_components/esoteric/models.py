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
    repeat="29",  # DOC-MISMATCH-03 on N-05XD
    shuffle="2A",  # DOC-MISMATCH-04 on N-05XD
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
REQ_DOUT = "DOUT"  # DOC-MISMATCH-01 on N-05XD
REQ_MEDIA = "MEDIA"
REQ_PSTS = "PSTS"
REQ_PMODE = "PMODE"
REQ_REPEAT = "REPEAT"
REQ_UPCONV = "UPCONV"  # DOC-MISMATCH-02 on N-05XD
REQ_FS = "FS"  # DOC-MISMATCH-05 on N-05XD
REQ_MQA = "MQA"
REQ_CODEC = "CODEC"  # DOC-MISMATCH-05 on N-05XD
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
# Differences between the command table (rev 1.5) and a real N-05XD, seen
# remotely on 2026-10-05 while the unit was STOPPED on input NET. The unit's
# menu settings were unknown and these may be state dependent, so the code keeps
# the documented behavior. Grep for the tag; confirm each one on site.
#
#   DOC-MISMATCH-01  @?DOUT   (common request)     -> NAK
#   DOC-MISMATCH-02  @?UPCONV (network request)    -> NAK
#   DOC-MISMATCH-03  @KEY 29  (network REPEAT)     -> NAK
#   DOC-MISMATCH-04  @KEY 2A  (network SHUFFLE)    -> NAK
#   DOC-MISMATCH-05  @?CODEC  -> "@CODEC" with no value;
#                    @?FS     -> "@FS NON" (not a sampling frequency)
#
# Matches the table but worth noting: direct "@INPUT <name>" is NAKed (the
# table documents it only for C1X / C1X solo / E1), as are the CD-group
# REPEAT/SHUFFLE codes KEY 47 / KEY 1F.

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
            # Implemented as documented. Where the real unit disagreed, see
            # the DOC-MISMATCH-xx notes above; confirm on site before changing.
            _requests(COMMON_REQUESTS, NETWORK_REQUESTS),
            volume=Volume(maximum=100.0, step=0.5, use_keys=False),
            known_inputs=N05XD_INPUTS,
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
