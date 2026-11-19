# pyright: reportMissingImports=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Generate picnicplace-test-board.kicad_pcb, the PicnicPlace test board.

Run with KiCad's bundled Python (it needs the pcbnew module and KiCad's footprint
libraries), from anywhere:

    $KICAD_PYTHON hardware/test-board/make_test_board.py

The board is all targets: nothing on it is soldered. Parts are placed on clear
double-sided tape laid over it, the top camera measures each against the copper ring
around it, and the parts are picked up again, so one board serves for test after test.
Every target is here, so change the list and run it again rather than editing the board
by hand. The design is described in docs/hardware/test-board.md.

Design coordinates, in millimeters: the origin is the board's front-left corner, the
corner pushed against the datum's pins; +X right, +Y toward the back of the machine.
KiCad's Y runs down the page, so the script flips Y.
"""

from __future__ import annotations

import json
import math
import os
import sys

import pcbnew

LIBS = os.environ.get(
    "KICAD_FOOTPRINTS", "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"
)
HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "picnicplace-test-board"
REVISION = "B"
MM = pcbnew.FromMM
PAGE_ORIGIN = (60.0, 140.0)  # where design (0, 0) sits on KiCad's page

SIZE = (120.0, 72.0)
"""Wide enough to rest on both of the datum's X pins, at 20 and 105 mm."""
CORNER_R = 1.0

# Three fiducials, asymmetric: a board loaded turned round shows a dot where none should
# be, and is caught instead of placed wrong.
FIDUCIALS = [(4.0, 4.0), (116.0, 4.0), (4.0, 68.0)]
FIDUCIAL_FP = ("Fiducial", "Fiducial_1mm_Mask3mm")

RING_GAP = 0.3
"""How far the ring stands off the part's body and pads."""
RING_W = 0.15
RING_MASK = 0.4
"""The mask opening over a ring: its width, so the ring is bare and bright."""
SILK_W = 0.12
SILK_GAP = 0.15
"""Between the ring's mask opening and the silkscreen outline, which cannot print on bare
copper."""
REF_SIZE = 0.8
REF_W = 0.15
REF_GAP = 0.4
"""Each target's reference (R1, U3), printed this far beyond its silkscreen outline: above
it, or for the circle, outside it. Far enough from the part that the top camera's check
of a placement, which looks some 1 mm about the part, does not take it for an end cap."""
POLARIZED = {"TEST-LED0805", "TEST-SOD123"}
"""Two-pad parts with a pin 1 (the cathode): marked with a bar at that end. Parts with
more pads get a dot at the corner nearest pin 1; resistors and capacitors get neither."""

ANGLES = (0.0, 90.0, 180.0, -90.0, 45.0)

# One row per kind of part: label, library, footprint, part ID, row's y, angles. The part
# IDs are generic: any part of that kind and size will do for a test.
SMALL_X = (14.0, 22.0, 30.0, 38.0, 46.0)
ROWS = [
    ("0402", "Resistor_SMD", "R_0402_1005Metric", "TEST-R0402", 11.0, ANGLES),
    ("0603", "Resistor_SMD", "R_0603_1608Metric", "TEST-R0603", 18.0, ANGLES),
    ("0805", "Capacitor_SMD", "C_0805_2012Metric", "TEST-C0805", 25.0, ANGLES),
    ("1206", "Resistor_SMD", "R_1206_3216Metric", "TEST-R1206", 32.0, ANGLES[:4]),
    ("LED", "LED_SMD", "LED_0805_2012Metric", "TEST-LED0805", 39.0, ANGLES[:4]),
    ("SOD-123", "Diode_SMD", "D_SOD-123", "TEST-SOD123", 46.0, ANGLES[:4]),
    ("SOT-23", "Package_TO_SOT_SMD", "SOT-23", "TEST-SOT23", 54.0, ANGLES),
    ("SOT-23-6", "Package_TO_SOT_SMD", "SOT-23-6", "TEST-SOT23-6", 62.0, ANGLES[:4]),
]
# The larger parts, one by one: library, footprint, part ID, x, y, angle.
LARGE = [
    ("Package_SO", "SOIC-8_3.9x4.9mm_P1.27mm", "TEST-SOIC8", 62.0, 14.0, 0.0),
    ("Package_SO", "SOIC-8_3.9x4.9mm_P1.27mm", "TEST-SOIC8", 78.0, 14.0, 90.0),
    ("Package_SO", "SOIC-8_3.9x4.9mm_P1.27mm", "TEST-SOIC8", 62.0, 30.0, 180.0),
    ("Package_SO", "SOIC-8_3.9x4.9mm_P1.27mm", "TEST-SOIC8", 78.0, 30.0, 45.0),
    ("Package_DFN_QFN", "QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm", "TEST-QFN16", 60.0, 44.0, 0.0),
    ("Package_DFN_QFN", "QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm", "TEST-QFN16", 70.0, 44.0, 90.0),
    ("Package_DFN_QFN", "QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm", "TEST-QFN16", 80.0, 44.0, 45.0),
    ("Package_DFN_QFN", "QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm", "TEST-QFN16", 60.0, 54.0, 180.0),
    ("Package_DFN_QFN", "QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm", "TEST-QFN16", 70.0, 54.0, -90.0),
    ("Package_DFN_QFN", "QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm", "TEST-QFN32", 51.0, 39.0, 0.0),
]
# A circle of 0603s, one every 30 degrees, each turned to point along its radius: an
# offset that changes with the part's turn (nozzle runout, the wrong center of rotation)
# traces a sine wave round it.
CIRCLE = ("Resistor_SMD", "R_0603_1608Metric", "TEST-R0603")
CIRCLE_CENTER = (104.0, 30.0)
CIRCLE_R = 8.5
"""The parts' centers, from the circle's center."""
CIRCLE_REF_R = 12.0
CIRCLE_ANGLES = tuple(30.0 * k for k in range(12))

LOGO = os.path.join(HERE, "..", "logo", "picnicplace-icon-outline.json")
LOGO_CENTER = (104.0, 58.5)
LOGO_H = 17.0
"""The PicnicPlace logo, in silkscreen above the circle."""

PREFIX = {"TEST-C0805": "C", "TEST-LED0805": "D", "TEST-SOD123": "D", "TEST-SOT23": "Q"}
"""Reference prefixes, by part ID; any other is R if a resistor, else U."""


def at(x: float, y: float) -> pcbnew.VECTOR2I:
    """A design point on KiCad's page."""
    return pcbnew.VECTOR2I(MM(PAGE_ORIGIN[0] + x), MM(PAGE_ORIGIN[1] - y))


def segment(
    parent: pcbnew.BOARD | pcbnew.FOOTPRINT,
    layer: int,
    a: pcbnew.VECTOR2I,
    b: pcbnew.VECTOR2I,
    width: float,
) -> None:
    shape = pcbnew.PCB_SHAPE(parent)
    shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
    shape.SetStart(a)
    shape.SetEnd(b)
    shape.SetLayer(layer)
    shape.SetWidth(MM(width))
    parent.Add(shape)


def outline(board: pcbnew.BOARD) -> None:
    w, h = SIZE
    r = CORNER_R
    segment(board, pcbnew.Edge_Cuts, at(r, 0), at(w - r, 0), 0.05)
    segment(board, pcbnew.Edge_Cuts, at(w, r), at(w, h - r), 0.05)
    segment(board, pcbnew.Edge_Cuts, at(w - r, h), at(r, h), 0.05)
    segment(board, pcbnew.Edge_Cuts, at(0, h - r), at(0, r), 0.05)
    k = 1 - 0.5**0.5  # an arc's midpoint, in from the corner
    for (cx, cy), (sx, sy), (mx, my), (ex, ey) in [
        ((r, r), (0, r), (r * k, r * k), (r, 0)),
        ((w - r, r), (w - r, 0), (w - r * k, r * k), (w, r)),
        ((w - r, h - r), (w, h - r), (w - r * k, h - r * k), (w - r, h)),
        ((r, h - r), (r, h), (r * k, h - r * k), (0, h - r)),
    ]:
        del cx, cy
        arc = pcbnew.PCB_SHAPE(board)
        arc.SetShape(pcbnew.SHAPE_T_ARC)
        arc.SetArcGeometry(at(sx, sy), at(mx, my), at(ex, ey))
        arc.SetLayer(pcbnew.Edge_Cuts)
        arc.SetWidth(MM(0.05))
        board.Add(arc)


def load(library: str, name: str) -> pcbnew.FOOTPRINT:
    fp = pcbnew.FootprintLoad(os.path.join(LIBS, f"{library}.pretty"), name)
    if fp is None:
        sys.exit(f"footprint {library}:{name} not found under {LIBS}")
    return fp


def part_box(fp: pcbnew.FOOTPRINT) -> tuple[int, int, int, int]:
    """The part with its leads, before it is turned: its body (the fabrication outline)
    and its pads together, so a ring around it clears the pads."""
    boxes = [p.GetBoundingBox() for p in fp.Pads()]
    boxes += [
        item.GetBoundingBox()
        for item in fp.GraphicalItems()
        if item.GetLayer() == pcbnew.F_Fab and isinstance(item, pcbnew.PCB_SHAPE)
    ]
    if not boxes:
        sys.exit(f"{fp.GetFPID().GetLibItemName()} has no pads or fabrication outline")
    return (
        min(b.GetLeft() for b in boxes),
        min(b.GetTop() for b in boxes),
        max(b.GetRight() for b in boxes),
        max(b.GetBottom() for b in boxes),
    )


def target(
    board: pcbnew.BOARD,
    ref: str,
    library: str,
    name: str,
    part: str,
    x: float,
    y: float,
    angle: float,
    ref_at: tuple[float, float] | None = None,
) -> None:
    """A part's footprint, with no paste, a bare copper ring around it, and outside the
    ring a silkscreen outline with the pin 1 mark and its reference, above it or at
    ``ref_at``. The library's own silkscreen would cross the ring and is replaced; the 3D
    model is dropped, as nothing is soldered."""
    fp = load(library, name)
    fp.SetReference(ref)
    fp.SetValue(part)
    fp.SetField("MP", part)
    board.Add(fp)
    fp.SetPosition(at(x, y))
    for pad in fp.Pads():
        pad.SetLayerSet(pad.GetLayerSet().RemoveLayer(pcbnew.F_Paste))
    for item in list(fp.GraphicalItems()):
        if item.GetLayer() == pcbnew.F_SilkS:
            fp.Remove(item)
    fp.Models().clear()
    x0, y0, x1, y1 = part_box(fp)
    g = MM(RING_GAP + RING_W / 2)
    corners = [
        pcbnew.VECTOR2I(x0 - g, y0 - g),
        pcbnew.VECTOR2I(x1 + g, y0 - g),
        pcbnew.VECTOR2I(x1 + g, y1 + g),
        pcbnew.VECTOR2I(x0 - g, y1 + g),
    ]
    for a, b in zip(corners, corners[1:] + corners[:1]):  # noqa: B905 (Python 3.9)
        segment(fp, pcbnew.F_Cu, a, b, RING_W)
        segment(fp, pcbnew.F_Mask, a, b, RING_MASK)
    silk_outline(fp, (x0, y0, x1, y1), g + MM(RING_MASK / 2 + SILK_GAP + SILK_W / 2), part)
    fp.SetOrientationDegrees(angle)
    ref = fp.Reference()
    ref.SetLayer(pcbnew.F_SilkS)
    ref.SetTextSize(pcbnew.VECTOR2I(MM(REF_SIZE), MM(REF_SIZE)))
    ref.SetTextThickness(MM(REF_W))
    ref.SetTextAngleDegrees(0.0)
    if ref_at is None:
        silk = [i.GetBoundingBox() for i in fp.GraphicalItems() if i.GetLayer() == pcbnew.F_SilkS]
        top = min(b.GetTop() for b in silk)
        ref.SetPosition(pcbnew.VECTOR2I(fp.GetPosition().x, top - MM(REF_GAP + REF_SIZE / 2)))
    else:
        ref.SetPosition(at(*ref_at))
    ref.SetVisible(True)
    fp.Value().SetVisible(False)
    fp.GetField("MP").SetVisible(False)


def silk_outline(fp: pcbnew.FOOTPRINT, box: tuple[int, int, int, int], off: int, part: str) -> None:
    """The silkscreen outline, ``off`` outside the part's box, and its pin 1 mark. Works
    before the footprint is turned."""
    x0, y0, x1, y1 = box[0] - off, box[1] - off, box[2] + off, box[3] + off
    corners = [
        pcbnew.VECTOR2I(x0, y0),
        pcbnew.VECTOR2I(x1, y0),
        pcbnew.VECTOR2I(x1, y1),
        pcbnew.VECTOR2I(x0, y1),
    ]
    for a, b in zip(corners, corners[1:] + corners[:1]):  # noqa: B905 (Python 3.9)
        segment(fp, pcbnew.F_SilkS, a, b, SILK_W)
    pads = [p for p in fp.Pads() if p.GetNumber() == "1"]
    if not pads or not (part in POLARIZED or len(fp.Pads()) > 2):
        return
    one = pads[0].GetPosition()
    center = fp.GetPosition()
    step = MM(SILK_W * 2.5)
    if part in POLARIZED:
        # A second line just outside the outline, along pin 1's end.
        if abs(one.x - center.x) >= abs(one.y - center.y):
            x = x0 - step if one.x < center.x else x1 + step
            segment(fp, pcbnew.F_SilkS, pcbnew.VECTOR2I(x, y0), pcbnew.VECTOR2I(x, y1), SILK_W)
        else:
            y = y0 - step if one.y < center.y else y1 + step
            segment(fp, pcbnew.F_SilkS, pcbnew.VECTOR2I(x0, y), pcbnew.VECTOR2I(x1, y), SILK_W)
        return
    # A dot just outside the outline's corner nearest pin 1.
    x = x0 - step if one.x < center.x else x1 + step
    y = y0 - step if one.y < center.y else y1 + step
    dot = pcbnew.PCB_SHAPE(fp)
    dot.SetShape(pcbnew.SHAPE_T_CIRCLE)
    dot.SetCenter(pcbnew.VECTOR2I(x, y))
    dot.SetEnd(pcbnew.VECTOR2I(x + MM(0.2), y))
    dot.SetLayer(pcbnew.F_SilkS)
    dot.SetWidth(MM(0.0))
    dot.SetFilled(True)
    fp.Add(dot)


def logo(board: pcbnew.BOARD) -> None:
    """The logo, from the outlines hardware/logo/make_logo_outline.py traced from its
    picture: filled silkscreen polygons, each with its holes."""
    with open(LOGO) as f:
        art = json.load(f)
    scale = LOGO_H
    left = LOGO_CENTER[0] - art["aspect"] * scale / 2
    top = LOGO_CENTER[1] + scale / 2

    def chain(points: list[list[float]]) -> pcbnew.SHAPE_LINE_CHAIN:
        line = pcbnew.SHAPE_LINE_CHAIN()
        for u, v in points:
            line.Append(at(left + u * scale, top - v * scale))
        line.SetClosed(True)
        return line

    for polygon in art["polygons"]:
        shapes = pcbnew.SHAPE_POLY_SET()
        shapes.AddOutline(chain(polygon["outline"]))
        for hole_points in polygon["holes"]:
            shapes.AddHole(chain(hole_points))
        shapes.Fracture()
        shape = pcbnew.PCB_SHAPE(board)
        shape.SetShape(pcbnew.SHAPE_T_POLY)
        shape.SetPolyShape(shapes)
        shape.SetLayer(pcbnew.F_SilkS)
        shape.SetWidth(MM(0.0))
        shape.SetFilled(True)
        board.Add(shape)


def text(board: pcbnew.BOARD, s: str, x: float, y: float, size: float = 1.0) -> None:
    t = pcbnew.PCB_TEXT(board)
    t.SetText(s)
    t.SetPosition(at(x, y))
    t.SetLayer(pcbnew.F_SilkS)
    t.SetTextSize(pcbnew.VECTOR2I(MM(size), MM(size)))
    t.SetTextThickness(MM(size * 0.15))
    board.Add(t)


def main() -> None:
    board = pcbnew.BOARD()
    board.GetDesignSettings().SetAuxOrigin(at(0, 0))
    board.GetDesignSettings().SetGridOrigin(at(0, 0))
    outline(board)

    for i, (x, y) in enumerate(FIDUCIALS, start=1):
        fp = load(*FIDUCIAL_FP)
        fp.SetReference(f"FID{i}")
        fp.Reference().SetVisible(False)
        board.Add(fp)
        fp.SetPosition(at(x, y))

    counts: dict[str, int] = {}

    def next_ref(part: str) -> str:
        prefix = PREFIX.get(part, "R" if part.startswith("TEST-R") else "U")
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}{counts[prefix]}"

    for label, library, name, part, y, angles in ROWS:
        text(board, label, 6.5, y, 0.9)
        for x, angle in zip(SMALL_X, angles):  # noqa: B905 (Python 3.9)
            target(board, next_ref(part), library, name, part, x, y, angle)
    for library, name, part, x, y, angle in LARGE:
        target(board, next_ref(part), library, name, part, x, y, angle)
    cx, cy = CIRCLE_CENTER
    for angle in CIRCLE_ANGLES:
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        x, y = cx + CIRCLE_R * c, cy + CIRCLE_R * s
        ref_at = (cx + CIRCLE_REF_R * c, cy + CIRCLE_REF_R * s)
        target(board, next_ref(CIRCLE[2]), *CIRCLE, x, y, angle, ref_at)
    text(board, "0603 circle", cx, cy, 0.9)
    logo(board)

    text(board, "PicnicPlace test board", 72.0, 66.0, 1.4)
    text(board, f"rev {REVISION}   CERN-OHL-S-2.0", 72.0, 63.0, 0.9)
    text(board, "nothing here is soldered", 72.0, 60.5, 0.9)

    path = os.path.join(HERE, f"{NAME}.kicad_pcb")
    if not pcbnew.SaveBoard(path, board):
        sys.exit(f"could not save {path}")
    print(path)


if __name__ == "__main__":
    main()
