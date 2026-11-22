"""What a board's footprints say before anyone has looked at a reel: the body the bottom
camera looks for, even when the footprint draws its leads on F.Fab, and the tape a part
on it usually comes in."""

import pytest

from picnicplace.board.kicad import library_footprint
from picnicplace.board.sexpr import parse
from picnicplace.library.models import Footprint, Pad, Size
from picnicplace.library.tape_rules import guess_tape, guessed_tape


def line(x0: float, y0: float, x1: float, y1: float) -> str:
    return f'(fp_line (start {x0} {y0}) (end {x1} {y1}) (layer "F.Fab"))'


def pad(name: str, x: float, y: float, w: float, h: float) -> str:
    return f'(pad "{name}" smd rect (at {x} {y}) (size {w} {h}) (layers "F.Cu"))'


def sot23_6(with_leads: bool) -> str:
    """A SOT-23-6 (a 1.6 x 2.9 mm body, leads out the long sides) as a vendor tool draws
    it: with each lead on F.Fab too, over its pad, when ``with_leads``."""
    shapes = [line(-0.8, -1.45, 0.8, -1.45), line(0.8, -1.45, 0.8, 1.45)]
    shapes += [line(0.8, 1.45, -0.8, 1.45), line(-0.8, 1.45, -0.8, -1.45)]
    pads = []
    for n, (x, y) in enumerate(
        [(-1.25, -0.95), (-1.25, 0), (-1.25, 0.95), (1.25, 0.95), (1.25, 0), (1.25, -0.95)], 1
    ):
        pads.append(pad(str(n), x, y, 1.1, 0.6))
        if with_leads:
            # The lead's outline, from the body's edge out over the pad.
            sign = 1 if x > 0 else -1
            shapes += [
                line(sign * 0.82, y - 0.2, sign * 1.42, y - 0.2),
                line(sign * 1.42, y - 0.2, sign * 1.42, y + 0.2),
                line(sign * 1.42, y + 0.2, sign * 0.82, y + 0.2),
            ]
    return f'(footprint "Mine:DBV0006A" (at 0 0) {" ".join(shapes + pads)})'


def read(text: str, name: str) -> tuple[Footprint, list[str]]:
    warnings: list[str] = []
    return library_footprint(parse(text), name, "top", warnings, "U1"), warnings


def test_leads_drawn_on_fab_are_left_out_of_the_body() -> None:
    """On a first real board the bottom camera could not find two SOT-23-6s: their
    footprint drew the leads on F.Fab, so the part looked for was 2.84 mm wide, not 1.6."""
    plain, warnings = read(sot23_6(with_leads=False), "SOT-23-6-plain")
    assert plain.body == Size(x=1.6, y=2.9) and warnings == []

    leaded, warnings = read(sot23_6(with_leads=True), "DBV0006A")
    assert leaded.body == Size(x=1.6, y=2.9)
    assert len(warnings) == 1
    assert "draws its leads on F.Fab too" in warnings[0]
    assert "taken as 1.6 x 2.9 mm, not 2.84 x 2.9" in warnings[0]


def test_a_body_drawn_over_two_pads_is_kept() -> None:
    """A power inductor's body covers its two pads, and its sides lie over them: that is
    its body, not leads."""
    shapes = [line(-4, -4, 4, -4), line(4, -4, 4, 4), line(4, 4, -4, 4), line(-4, 4, -4, -4)]
    pads = [pad("1", -3.2, 0, 2.2, 8.4), pad("2", 3.2, 0, 2.2, 8.4)]
    inductor, warnings = read(f'(footprint "L" (at 0 0) {" ".join(shapes + pads)})', "L_8x8")
    assert inductor.body == Size(x=8, y=8) and warnings == []


def footprint(name: str, body: tuple[float, float] | None = None) -> Footprint:
    pads = [Pad(name=n, x=x, y=0, width=0.5, height=0.5) for n, x in (("1", -1), ("2", 1))]
    size = Size(x=body[0], y=body[1]) if body else None
    return Footprint(id=name, pads=pads, body=size)


@pytest.mark.parametrize(
    ("name", "kind", "width", "pitch", "rotation"),
    [
        ("R_0603_1608Metric", "paper", 8, 4, 90),
        ("R_0402_1005Metric", "paper", 8, 2, 90),
        ("C_0805_2012Metric", "embossed", 8, 4, 90),
        ("C_0603_1608Metric_Pad1.08x0.95mm_HandSolder", "paper", 8, 4, 90),
        ("L_1210_3225Metric", "unknown", 8, 4, 90),
        ("RESC1608X55N", "paper", 8, 4, 90),
        ("D_0603_1608Metric", "unknown", 8, 4, None),
        ("LED_0805_2012Metric", "unknown", 8, 4, None),
        ("SOT-23", "embossed", 8, 4, None),
        ("SOT-23-6", "embossed", 8, 4, None),
        ("SOD-123", "embossed", 8, 4, None),
        ("SOT-223-3_TabPin2", "embossed", 12, 8, None),
        ("SOIC-8_3.9x4.9mm_P1.27mm", "embossed", 12, 8, None),
        ("SOIC8-N_MC_MCH", "embossed", 12, 8, None),
        ("SOIC-16_3.9x9.9mm_P1.27mm", "embossed", 16, 8, None),
        ("SOIC-16W_7.5x10.3mm_P1.27mm", "embossed", 16, 12, None),
        ("TSSOP-14_4.4x5mm_P0.65mm", "embossed", 12, 8, None),
        ("TSSOP-20", "embossed", 12, 8, None),
        ("QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm", "embossed", 12, 8, None),
        ("DFN-6-1EP_2x2mm_P0.65mm_EP1x1.6mm", "embossed", 8, 4, None),
        ("QFN-48-1EP_7x7mm_P0.5mm_EP5.6x5.6mm", "embossed", 16, 12, None),
    ],
)
def test_a_tape_is_guessed_from_the_footprints_name(
    name: str, kind: str, width: float, pitch: float, rotation: float | None
) -> None:
    guess = guess_tape(footprint(name))
    assert guess is not None, name
    assert (guess.kind, guess.width, guess.pitch, guess.rotation) == (
        kind,
        width,
        pitch,
        rotation,
    )


def test_a_footprint_by_another_name_is_guessed_from_its_body() -> None:
    """TI's DBV0006A (a SOT-23-6, by its own name) and D8 (a SOIC-8), with their bodies
    read from F.Fab."""
    sot = guess_tape(footprint("DBV0006A_M", (1.6, 2.9)))
    soic = guess_tape(footprint("D8_TEX", (3.9, 4.9)))
    assert sot is not None and (sot.width, sot.pitch, sot.rotation) == (8, 4, None)
    assert soic is not None and (soic.width, soic.pitch) == (12, 8)
    assert guess_tape(footprint("Mystery")) is None


def test_a_guessed_tape_is_a_record_of_its_own_marked_estimated() -> None:
    tape = guessed_tape(footprint("R_0603_1608Metric"))
    assert tape is not None
    assert tape.id == "8mm-paper-p4-r90-guessed" and tape.name.endswith("(guessed)")
    assert tape.basis == "estimated" and "check it against the reel" in tape.source
