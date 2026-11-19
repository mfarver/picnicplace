# pyright: reportMissingImports=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Generate picnicplace-datum.kicad_pcb, the PicnicPlace datum PCB.

Run with KiCad's bundled Python (it needs the pcbnew module), from anywhere:

    $KICAD_PYTHON hardware/datum/make_datum.py

Every dimension is here, so change the numbers and run it again rather than editing the
board by hand (silkscreen touch-ups in KiCad are fine, but are lost on the next run). The
design is described in docs/hardware/picnicplace-datum.md.

Design coordinates, in millimeters: the origin is where a board's front-left corner sits,
the corner the locating pins define; +X right, +Y toward the back of the machine. KiCad's
Y runs down the page, so the script flips Y, and puts the origin at KiCad's grid origin.
"""

from __future__ import annotations

import json
import os
import sys

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "picnicplace-datum"
REVISION = "C"
MM = pcbnew.FromMM
PAGE_ORIGIN = (60.0, 120.0)  # where design (0, 0) sits on KiCad's page

# The L: two 30 mm legs, 150 x 75 mm overall.
LEG = 30.0
LONG = 150.0
SHORT = 75.0

# Locating pins: 3 x 10 mm dowels (h6 or m6). The PCB's inside edges run through the pin
# centers, so each pin sits in a half-hole drilled before the edge is routed (the drill
# sets where the pin is, to drill accuracy). A pin touches the board's edge line, so its
# center is one radius from it, and the PCB's inside edges stand that far back from the
# board.
PIN_D = 3.0
PIN_R = PIN_D / 2
INSIDE = -PIN_R  # the PCB's inside edges, along y = INSIDE and x = INSIDE
PIN_HOLE_D = 3.1
"""The half-holes, drawn larger than the pins: fabs finish unplated holes to about
+/-0.05 mm, and m6 pins are up to 3.008 mm, so a 3.0 mm hole could leave a pin riding on
the notch's corners, some 0.3 mm out of place. Each hole's center is moved from the
pin's toward the board by the difference of their radii, so a pin against the back of its
notch is where it is drawn, and the notch is a little less than half a hole. Revision A
drew them at 3.0 mm, centered on the pins."""
HOLE_BACK = PIN_HOLE_D / 2 - PIN_R
PINS_X = (10.0, 40.0, 105.0)
"""Pins along the X edge (y = INSIDE). A board 43 mm or more along X rests on the first two,
30 mm apart; one 108 mm or more also reaches the third, and rests on the two of the three
that stand farthest out (they are drilled in line to some 0.05 mm, so it turns at most a
few hundredths of a degree from one to another). Revision B had two, at 20 and 105 mm, and
a board shorter than 105 mm along X could turn on the first."""
PINS_Y = (12.0,)
"""One pin along the Y edge (x = INSIDE): any board 15 mm or more along Y reaches it.
Revision B had it at 32 mm."""

# Fiducials: 1 mm copper dots with 6 mm mask openings. The homing dot is in the corner
# square; the X and Y fiducials are 120 mm and 45 mm from it, for axis scale and square.
FID_D = 1.0
FID_MASK_D = 6.0
HOMING = (-15.0, -15.0)
FID_X = (105.0, -15.0)
FID_Y = (-15.0, 30.0)

AIM_RING = (-15.0, 8.0)  # copper ring for aiming the nozzles
AIM_RING_D = 8.0
AIM_RING_W = 0.6

BARE = (4.0, -26.0, 28.0, -4.0)  # nothing printed: the nozzle-offset disc or token
BARE_BOX_GAP = 0.3
"""The silkscreen box around the bare area stands this far outside it, so nothing is printed
where the disc or token lies."""
SILK_W = 0.15
Z_PAD = (34.0, -20.0, 46.0, -8.0)  # bare copper the nozzles touch to find their height
SCALE_GRID = (52.0, -20.0)  # first dot of an 11 x 11 grid, 1 mm pitch
SCALE_DOT_D = 0.5
LABEL_AREA = (68.0, -22.0, 82.0, -8.0)  # name, revision, license
LOGO = os.path.join(HERE, "..", "logo", "picnicplace-icon-outline.json")
LOGO_CENTER = (92.0, -15.0)
LOGO_H = 13.0
"""The PicnicPlace logo, in silkscreen between the label and the X fiducial."""

SCREW_D = 3.2  # M3 clearance, PCB to the 3D-printed holder (heat-set inserts)
SCREWS = [(-26.0, -26.0), (-26.0, 41.0), (91.0, -26.0), (116.0, -26.0)]


def at(x: float, y: float) -> pcbnew.VECTOR2I:
    """A design point on KiCad's page."""
    return pcbnew.VECTOR2I(MM(PAGE_ORIGIN[0] + x), MM(PAGE_ORIGIN[1] - y))


def edge_line(board: pcbnew.BOARD, a: tuple[float, float], b: tuple[float, float]) -> None:
    shape = pcbnew.PCB_SHAPE(board)
    shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
    shape.SetStart(at(*a))
    shape.SetEnd(at(*b))
    shape.SetLayer(pcbnew.Edge_Cuts)
    shape.SetWidth(MM(0.05))
    board.Add(shape)


def edge_arc(
    board: pcbnew.BOARD,
    a: tuple[float, float],
    mid: tuple[float, float],
    b: tuple[float, float],
) -> None:
    shape = pcbnew.PCB_SHAPE(board)
    shape.SetShape(pcbnew.SHAPE_T_ARC)
    shape.SetArcGeometry(at(*a), at(*mid), at(*b))
    shape.SetLayer(pcbnew.Edge_Cuts)
    shape.SetWidth(MM(0.05))
    board.Add(shape)


def outline(board: pcbnew.BOARD) -> None:
    """The L, with a half-hole notch at each pin, counterclockwise from the outer corner."""
    right = LONG - LEG
    top = SHORT - LEG
    r = PIN_HOLE_D / 2
    back = HOLE_BACK
    half = (r * r - back * back) ** 0.5  # half the notch's width on the edge line
    # Each notch's center is ``back`` from the pin's, toward the board (less negative).
    # The outside: front edge, right end, then up the X leg's inside edge to the notches.
    edge_line(board, (-LEG, -LEG), (right, -LEG))
    edge_line(board, (right, -LEG), (right, INSIDE))
    x = right
    for px in sorted(PINS_X, reverse=True):
        edge_line(board, (x, INSIDE), (px + half, INSIDE))
        edge_arc(board, (px + half, INSIDE), (px, INSIDE + back - r), (px - half, INSIDE))
        x = px - half
    edge_line(board, (x, INSIDE), (INSIDE, INSIDE))
    y = INSIDE
    for py in sorted(PINS_Y):
        edge_line(board, (INSIDE, y), (INSIDE, py - half))
        edge_arc(board, (INSIDE, py - half), (INSIDE + back - r, py), (INSIDE, py + half))
        y = py + half
    edge_line(board, (INSIDE, y), (INSIDE, top))
    edge_line(board, (INSIDE, top), (-LEG, top))
    edge_line(board, (-LEG, top), (-LEG, -LEG))


def footprint(board: pcbnew.BOARD, ref: str, name: str, x: float, y: float) -> pcbnew.FOOTPRINT:
    """An empty footprint, kept out of the BOM and the placement files: nothing on the
    datum is assembled."""
    fp = pcbnew.FOOTPRINT(board)
    fp.SetFPID(pcbnew.LIB_ID("", name))
    fp.SetReference(ref)
    fp.SetValue(name)
    fp.Reference().SetVisible(False)
    fp.Value().SetVisible(False)
    fp.SetAttributes(
        pcbnew.FP_EXCLUDE_FROM_POS_FILES | pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_BOARD_ONLY
    )
    board.Add(fp)
    fp.SetPosition(at(x, y))
    return fp


def hole(board: pcbnew.BOARD, ref: str, name: str, x: float, y: float, d: float) -> None:
    fp = footprint(board, ref, name, x, y)
    pad = pcbnew.PAD(fp)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(MM(d), MM(d)))
    pad.SetDrillSize(pcbnew.VECTOR2I(MM(d), MM(d)))
    pad.SetLayerSet(pad.UnplatedHoleMask())
    fp.Add(pad)
    pad.SetPosition(at(x, y))


def copper_pad(
    fp: pcbnew.FOOTPRINT,
    x: float,
    y: float,
    w: float,
    h: float,
    mask_margin: float,
    round_: bool,
) -> None:
    """Bare copper with its own mask opening, on no net."""
    pad = pcbnew.PAD(fp)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE if round_ else pcbnew.PAD_SHAPE_RECT)
    pad.SetSize(pcbnew.VECTOR2I(MM(w), MM(h)))
    pad.SetLayerSet(pad.SMDMask())
    pad.SetLocalSolderMaskMargin(MM(mask_margin))
    fp.Add(pad)
    pad.SetPosition(at(x, y))


def fiducial(board: pcbnew.BOARD, ref: str, xy: tuple[float, float]) -> None:
    fp = footprint(board, ref, "Fiducial_1mm_Mask6mm", *xy)
    copper_pad(fp, *xy, FID_D, FID_D, (FID_MASK_D - FID_D) / 2, round_=True)


def circle(
    parent: pcbnew.FOOTPRINT,
    layer: int,
    xy: tuple[float, float],
    d: float,
    width: float,
    filled: bool,
) -> None:
    shape = pcbnew.PCB_SHAPE(parent)
    shape.SetShape(pcbnew.SHAPE_T_CIRCLE)
    shape.SetCenter(at(*xy))
    shape.SetEnd(at(xy[0] + d / 2, xy[1]))
    shape.SetLayer(layer)
    shape.SetWidth(MM(width))
    shape.SetFilled(filled)
    parent.Add(shape)


def rect(
    parent: pcbnew.BOARD | pcbnew.FOOTPRINT,
    layer: int,
    box: tuple[float, ...],
    width: float = 0.1,
) -> None:
    x0, y0, x1, y1 = box
    shape = pcbnew.PCB_SHAPE(parent)
    shape.SetShape(pcbnew.SHAPE_T_RECT)
    shape.SetStart(at(x0, y1))
    shape.SetEnd(at(x1, y0))
    shape.SetLayer(layer)
    shape.SetWidth(MM(width))
    shape.SetFilled(False)
    parent.Add(shape)


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


def text(
    board: pcbnew.BOARD,
    s: str,
    x: float,
    y: float,
    size: float = 1.2,
    layer: int = pcbnew.F_SilkS,
    angle: float = 0.0,
) -> None:
    t = pcbnew.PCB_TEXT(board)
    t.SetText(s)
    t.SetPosition(at(x, y))
    t.SetLayer(layer)
    t.SetTextSize(pcbnew.VECTOR2I(MM(size), MM(size)))
    t.SetTextThickness(MM(size * 0.15))
    t.SetTextAngle(pcbnew.EDA_ANGLE(angle, pcbnew.DEGREES_T))
    board.Add(t)


def main() -> None:
    board = pcbnew.BOARD()
    board.GetDesignSettings().SetAuxOrigin(at(0, 0))
    board.GetDesignSettings().SetGridOrigin(at(0, 0))
    outline(board)

    for i, px in enumerate(PINS_X, start=1):
        hole(board, f"P{i}", "LocatingPin_3mm", px, INSIDE + HOLE_BACK, PIN_HOLE_D)
    for i, py in enumerate(PINS_Y, start=len(PINS_X) + 1):
        hole(board, f"P{i}", "LocatingPin_3mm", INSIDE + HOLE_BACK, py, PIN_HOLE_D)
    for i, (x, y) in enumerate(SCREWS, start=1):
        hole(board, f"H{i}", "Hole_M3", x, y, SCREW_D)

    fiducial(board, "FID1", HOMING)
    fiducial(board, "FID2", FID_X)
    fiducial(board, "FID3", FID_Y)

    ring = footprint(board, "AIM1", "AimingRing_8mm", *AIM_RING)
    circle(ring, pcbnew.F_Cu, AIM_RING, AIM_RING_D, AIM_RING_W, filled=False)
    circle(ring, pcbnew.F_Mask, AIM_RING, AIM_RING_D + AIM_RING_W + 2.0, 0.0, filled=True)

    zpad = footprint(
        board, "ZPAD1", "TouchPad_12mm", (Z_PAD[0] + Z_PAD[2]) / 2, (Z_PAD[1] + Z_PAD[3]) / 2
    )
    copper_pad(
        zpad,
        (Z_PAD[0] + Z_PAD[2]) / 2,
        (Z_PAD[1] + Z_PAD[3]) / 2,
        Z_PAD[2] - Z_PAD[0],
        Z_PAD[3] - Z_PAD[1],
        0.0,
        round_=False,
    )

    gx, gy = SCALE_GRID
    grid = footprint(board, "SCALE1", "ScaleTarget_10mm", gx + 5, gy + 5)
    for i in range(11):
        for j in range(11):
            circle(grid, pcbnew.F_Cu, (gx + i, gy + j), SCALE_DOT_D, 0.0, filled=True)
            circle(grid, pcbnew.F_Mask, (gx + i, gy + j), SCALE_DOT_D + 0.1, 0.0, filled=True)

    # The bare area stays bare: a silkscreen box just outside it shows where the disc or
    # token goes, and a fabrication-layer outline marks the area itself.
    rect(board, pcbnew.F_Fab, BARE)
    g = BARE_BOX_GAP + SILK_W / 2
    rect(board, pcbnew.F_SilkS, (BARE[0] - g, BARE[1] - g, BARE[2] + g, BARE[3] + g), SILK_W)
    text(
        board, "disc / token: keep bare", (BARE[0] + BARE[2]) / 2, BARE[3] - 1.5, 0.8, pcbnew.F_Fab
    )

    # Silkscreen.
    lx = (LABEL_AREA[0] + LABEL_AREA[2]) / 2
    text(board, "PicnicPlace", lx, -11, 1.5)
    text(board, "datum", lx, -13.5, 1.5)
    text(board, f"rev {REVISION}", lx, -16, 1.2)
    text(board, "CERN-OHL-S-2.0", lx, -19.5, 0.8)
    text(board, "token", (BARE[0] + BARE[2]) / 2, BARE[1] - 2.2, 1.0)
    logo(board)
    text(board, "Z touch", (Z_PAD[0] + Z_PAD[2]) / 2, Z_PAD[1] - 1.5, 1.0)
    text(board, "scale 1 mm", gx + 5, gy - 1.5, 1.0)
    text(board, "home", HOMING[0], HOMING[1] - 4.5, 1.0)
    text(board, "X 120", FID_X[0], FID_X[1] - 4.5, 1.0)
    text(board, "Y 45", FID_Y[0], FID_Y[1] - 4.5, 1.0)
    text(board, "aim", AIM_RING[0], AIM_RING[1] + 6.5, 1.0)
    text(board, "+X  board edge", 60, INSIDE - 3.0, 1.0)
    text(board, "+Y  board edge", INSIDE - 3.0, 30, 1.0, angle=90)

    path = os.path.join(HERE, f"{NAME}.kicad_pcb")
    if not pcbnew.SaveBoard(path, board):
        sys.exit(f"could not save {path}")
    print(path)


if __name__ == "__main__":
    main()
