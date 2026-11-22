"""Tape guessed from a footprint, for a part the library has no tape for.

Most parts come in the tape EIA-481 makes usual for their footprint: an 0603 resistor in
8 mm paper tape at 4 mm, a SOT-23 in 8 mm embossed tape at 4 mm, a SOIC-8 in 12 mm
embossed tape at 8 mm. KiCad's footprint names say the footprint (``R_0603_1608Metric``,
``SOT-23-6``, ``QFN-16-1EP_3x3mm_P0.5mm``), as do IPC-7351 names (``RESC1608X55``), so a
short table of rules gives a new part its tape's width, pitch and kind before anyone has
looked at a reel. A footprint the rules do not know is guessed from its body's size.

What is guessed is marked ``estimated``, with the rule in its source, so the plan and the
load list can say so: check it against the reel. Which way round a part lies in its tape
is guessed only for parts that read the same turned 180 degrees (resistors, capacitors,
inductors): a diode's, a transistor's or an IC's depends on its maker, and is left for the
operator to give (the plan warns of it).
"""

import re
from dataclasses import dataclass

from picnicplace.library.models import Footprint, Tape, TapeKind, standard_tape


@dataclass(frozen=True)
class TapeGuess:
    kind: TapeKind
    width: float
    pitch: float | None
    rotation: float | None
    rule: str
    """What the guess is from, for the tape's source."""


# Chip sizes by their imperial code: tape width, pitch.
CHIP_TAPE: dict[str, tuple[float, float]] = {
    "01005": (8, 2),
    "0201": (8, 2),
    "0402": (8, 2),
    "0603": (8, 4),
    "0805": (8, 4),
    "1206": (8, 4),
    "1210": (8, 4),
    "1812": (12, 8),
    "2010": (12, 4),
    "2512": (12, 4),
}
METRIC_TO_IMPERIAL = {
    "0402": "01005",
    "0603": "0201",
    "1005": "0402",
    "1608": "0603",
    "2012": "0805",
    "3216": "1206",
    "3225": "1210",
    "4532": "1812",
    "5025": "2010",
    "6332": "2512",
}
PAPER_UP_TO = {"R": "1206", "C": "0603"}
"""The largest chip of each kind usually in paper tape; larger, or thick ones, are in
embossed tape, and other kinds vary."""
CHIP_ORDER = list(CHIP_TAPE)

# Packages by name, with their usual tape: width, pitch.
PACKAGES: list[tuple[re.Pattern[str], float, float, str]] = [
    (re.compile(r"^SOT-?(23|323|353|363|416|523|563|723|883)\b|^SC-?70|^SC-?59"), 8, 4, "SOT-23"),
    (re.compile(r"^SOD-?(123|323|523|923)"), 8, 4, "SOD"),
    (re.compile(r"^SOT-?89"), 12, 8, "SOT-89"),
    (re.compile(r"^SOT-?223"), 12, 8, "SOT-223"),
    (re.compile(r"^TO-?252|^DPAK"), 16, 8, "TO-252"),
    (re.compile(r"^TO-?263|^D2PAK"), 24, 16, "TO-263"),
    (re.compile(r"^SMA\b|^D_SMA|^DO-214AC"), 12, 4, "SMA"),
    (re.compile(r"^SMB\b|^D_SMB|^DO-214AA"), 12, 8, "SMB"),
    (re.compile(r"^SMC\b|^D_SMC|^DO-214AB"), 16, 8, "SMC"),
]
LEADED = re.compile(r"^(SOIC|SOP|SO|TSSOP|MSOP|VSSOP|SSOP|HTSSOP|QSOP)-?(\d+)")
LEADLESS = re.compile(
    r"^(QFN|DFN|VQFN|WQFN|UQFN|TQFN|WSON|VSON|SON|USON|LGA|TDFN|UDFN|QFP|LQFP|TQFP)"
)
SIZE = re.compile(r"_(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)mm")
CHIP = re.compile(r"^(R|C|L|D|LED|CP|Fuse|FB)_(\d{4,5})(?:_|$)")
IPC_CHIP = re.compile(r"^(RESC|CAPC|INDC|DIOC|LEDC|FUSC)(\d{4})X")
IPC_KINDS = {"RESC": "R", "CAPC": "C", "INDC": "L", "DIOC": "D", "LEDC": "LED", "FUSC": "Fuse"}
SYMMETRIC_CHIPS = {"R", "C", "L", "Fuse", "FB"}


def _by_size(longest: float, shortest: float) -> tuple[float, float]:
    """The usual tape for a part of this body size (mm)."""
    if longest <= 3.4 and shortest <= 1.8:
        return 8, 4
    if longest <= 5.6:
        return 12, 8
    if longest <= 7.6:
        return 16, 12
    if longest <= 10.6:
        return 24, 16
    return 32, 24


def _chip(kind: str, code: str) -> TapeGuess | None:
    if code not in CHIP_TAPE:
        return None
    width, pitch = CHIP_TAPE[code]
    paper_to = PAPER_UP_TO.get(kind)
    tape_kind: TapeKind = "unknown"
    if paper_to is not None:
        paper = CHIP_ORDER.index(code) <= CHIP_ORDER.index(paper_to)
        tape_kind = "paper" if paper else "embossed"
    rotation = 90.0 if kind in SYMMETRIC_CHIPS else None
    return TapeGuess(tape_kind, width, pitch, rotation, f"{code} chips")


def guess_tape(footprint: Footprint) -> TapeGuess | None:
    """The tape a part on this footprint usually comes in, by the footprint's name, else
    its body's size; None if neither says."""
    name = footprint.id
    m = CHIP.match(name)
    if m:
        kind, code = m.group(1), m.group(2)
        # KiCad names give the imperial code first; a metric one (1608) is converted.
        code = code if code in CHIP_TAPE else METRIC_TO_IMPERIAL.get(code, code)
        return _chip(kind, code)
    m = IPC_CHIP.match(name)
    if m:
        code = METRIC_TO_IMPERIAL.get(m.group(2))
        return _chip(IPC_KINDS[m.group(1)], code) if code else None
    for pattern, width, pitch, rule in PACKAGES:
        if pattern.match(name):
            return TapeGuess("embossed", width, pitch, None, rule)
    size = SIZE.search(name)
    m = LEADED.match(name)
    if m:
        # By pin count: a long package lies along the tape, so its length sets the pitch
        # less than its width does. A wide-body SOIC (7.5 mm) takes a wider tape.
        pins = int(m.group(2))
        wide = size is not None and min(float(size.group(1)), float(size.group(2))) >= 7.0
        if wide:
            width, pitch = (16, 12) if pins <= 20 else (24, 12)
        elif m.group(1) in ("SOIC", "SO", "SOP"):
            width, pitch = (12, 8) if pins <= 8 else (16, 8)
        else:
            width, pitch = (12, 8) if pins <= 20 else (16, 8)
        return TapeGuess("embossed", width, pitch, None, f"{m.group(1)}-{pins}")
    if LEADLESS.match(name) and size:
        a, b = float(size.group(1)), float(size.group(2))
        longest, shortest = max(a, b), min(a, b)
        width, pitch = (8, 4) if longest <= 2.6 else _by_size(longest, max(shortest, 2.0))
        return TapeGuess("embossed", width, pitch, None, f"{a:g} x {b:g} mm leadless parts")
    if footprint.body is not None and len(footprint.pads) >= 2:
        a, b = footprint.body.x, footprint.body.y
        width, pitch = _by_size(max(a, b), min(a, b))
        return TapeGuess("unknown", width, pitch, None, f"a {a:g} x {b:g} mm body")
    return None


def guessed_tape(footprint: Footprint) -> Tape | None:
    """The tape record for ``guess_tape``, marked estimated."""
    guess = guess_tape(footprint)
    if guess is None:
        return None
    tape = standard_tape(guess.kind, guess.width, guess.pitch, guess.rotation)
    # A record of its own, so a part given a tape checked on a real reel is told apart.
    return tape.model_copy(
        update={
            "id": f"{tape.id}-guessed",
            "name": f"{tape.name} (guessed)",
            "basis": "estimated",
            "source": f"guessed from the footprint ({guess.rule}, as EIA-481 tapes usually "
            "are): check it against the reel",
        }
    )
