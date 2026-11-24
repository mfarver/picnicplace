"""Generate the control panel's enclosure: STEP and STL files to print.

Run with CadQuery (2.4 or later), from anywhere:

    python hardware/panel-enclosure/make_enclosure.py

For a test fit, give it the makers' STEP models (not kept in the repository): Waveshare's
model of the board and TE's of the E-stop. It then reports any part that overlaps them
and writes the whole as out/panel-fit.step:

    python hardware/panel-enclosure/make_enclosure.py --board ESP32-S3-Touch-LCD-7B.stp \\
        --estop c-2416049-1-a1-3d.stp

The panel is the Waveshare ESP32-S3-Touch-LCD-7B in portrait, with a 22 mm E-stop (TE
ALCOSWITCH 2416049-1) below it. It stands just right of a LumenPnP near its front, facing
the operator, leaning back, its bottom edge on the table, and its left side bolted to the
machine's lower right 2020 rail by a flat bracket. (On the left there is no room in front
of the machine's controller.) Every dimension is here: change it and
run the script again rather than editing the models. Values marked MEASURE are not yet
checked on the real parts. The design is described in docs/hardware/control-panel.md.

Parts, each sized for a 220 x 220 x 220 mm printer (a Flashforge Adventurer 5M Pro):
  - shell: the screen's housing, its front lip holding the glass; printed face down;
  - back: the shell's back cover, with posts screwed to the board's standoffs; printed flat;
  - pod: the E-stop's housing below the shell; printed on its back;
  - pod-back: the pod's back cover;
  - bracket: the plate from the shell's left side to the rail.

Coordinates, in millimeters, for each part as it stands upright before leaning back: +X
right (away from the machine), +Y up, +Z toward the operator. The screen's face is Z = 0.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import cadquery as cq

HERE = Path(__file__).resolve().parent

# The board, from Waveshare's STEP model and drawing (ESP32-S3-Touch-LCD-7B, 2025-10-25),
# in portrait as it stands here: the board's native USB-C ports at the bottom. Positions
# are from the glass's center, +X right and +Y up as seen from the front.
GLASS_W = 110.76
GLASS_H = 192.96
GLASS_CORNER_R = 7.88
OPENING = (-48.55, 52.49, -80.64, 84.24)
"""The opening in the glass's black print (left, right, bottom, top): 101.0 x 164.9 mm,
not centered on the glass."""
WINDOW_MARGIN = 1.0
"""How far the shell's window is cut beyond the print's opening, so the lip never shades
it; at least LIP_MIN of the lip still lies on the glass."""
LIP_MIN = 2.0
FRAME_MOUNTS = [(-33.81, -61.61), (31.85, -61.61), (-33.81, 64.59), (31.85, 64.59)]
"""The four M3 holes in the board's metal frame (65.65 x 126.2 mm apart)."""
MOUNT_FACE = 8.5
"""The glass's face to the frame's mounting face."""
STANDOFF = 8.0
"""The M3 standoffs that come with the board, screwed into the frame's holes."""
BOARD_SPACE = 31.0
"""The glass's face to the back cover: the board's connectors, with plugs in them, reach
about 22 mm behind the glass, and their wires need room to bend."""
USB_PORTS = (-16.0, 8.0, -55.0)
"""The two USB-C ports (left edge, right edge, their mouths' height), facing down."""
USB_SLOT = (28.0, 18.0)
"""The opening down from the shell into the pod, for the USB-C plug and cable and the
E-stop's wires (across, deep)."""

# The enclosure.
WALL = 2.4
LIP = 2.0
"""The front lip's thickness, over the glass's border."""
CLEAR = 0.4
"""Around the glass, so it drops in."""
BACK = 2.4
SCREW_BOSS_D = 7.0
INSERT_D = 4.0
"""Holes for M3 heat-set inserts (most M3 inserts take a 4.0 mm hole)."""
M3_CLEAR = 3.4
M3_HEAD_D = 6.0
CORNER_R = 4.0

# The E-stop: TE ALCOSWITCH 2416049-1 (PBES22, illuminated), from TE's datasheet and model.
ESTOP_HOLE = 22.5
"""22.3 mm +0.4/-0."""
ESTOP_KEY = (3.3, 1.8)
"""The anti-rotation ring's notch at the top of the hole: 3.2 mm wide, to 24.1 mm across."""
ESTOP_FACE = 3.0
"""The pod's face, where the switch mounts: 1.2 to 4.5 mm with the anti-rotation ring."""
ESTOP_BEHIND = 45.7
"""The switch's body and contact blocks behind the panel's face (45.7 +/- 1)."""
ESTOP_X = 25.0
"""The switch's center, right of the pod's: the USB cable drops past its left side."""
ESTOP_KNOB_D = 40.0
ESTOP_KNOB_OUT = 33.0
"""How far the mushroom stands out in front of the face, released."""
POD_H = 64.0
POD_DEPTH = ESTOP_FACE + ESTOP_BEHIND + 1.0 + 6.0
"""The face to the pod's back cover: the switch, its tolerance, and room for the screw
terminals' wires, which leave the contact blocks sideways."""
POD_SCREW_X = 46.0
"""The two screws from the pod up into the shell, either side of the USB opening."""
POD_CABLES = (18.0, 10.0)
"""Out of the pod's back: the USB cable (its far plug passes through) and the cable from
the E-stop's power pole to the machine's 24 V."""

# How it stands.
LEAN = 45.0
"""Degrees back from vertical: the screen faces up at 45 degrees, between a standing
operator's eyes (about 55 degrees up from the panel) and a sitting one's (about 35)."""
RAIL_HEIGHT = 130.0
"""The lower rail's center (its T-slot) above the table: 130 mm on a LumenPnP v4 standing
on its feet (measured 2026-11-23)."""
RAIL_SLOT_M5 = 5.4
RAIL_HOLE_PITCH = 40.0
"""Between the bracket's two M5 screws, along the rail."""
BRACKET_T = 5.0

SHELL_W = GLASS_W + 2 * (CLEAR + WALL)
SHELL_H = GLASS_H + 2 * (CLEAR + WALL)
SHELL_D = LIP + BOARD_SPACE


def window() -> tuple[float, float, float, float]:
    """The shell's window (left, right, bottom, top)."""
    left, right, bottom, top = OPENING
    gx, gy = GLASS_W / 2 - LIP_MIN, GLASS_H / 2 - LIP_MIN
    m = WINDOW_MARGIN
    return max(left - m, -gx), min(right + m, gx), max(bottom - m, -gy), min(top + m, gy)


def rounded_box(w: float, h: float, d: float, r: float) -> cq.Workplane:
    """A box from Z = 0 to -d, centered in X and Y, its edges along Z rounded."""
    return cq.Workplane("XY").rect(w, h).extrude(-d).edges("|Z").fillet(r)


def corner_points(inset: float) -> list[tuple[float, float]]:
    x, y = SHELL_W / 2 - inset, SHELL_H / 2 - inset
    return [(-x, -y), (x, -y), (-x, y), (x, y)]


CORNER_INSET = WALL + SCREW_BOSS_D / 2 - 1.0


def usb_slot(z_from: float) -> cq.Workplane:
    """The opening between the shell's bottom and the pod's top, through a wall at
    Y = -SHELL_H / 2 from ``z_from`` (+1 up, -1 down)."""
    left, right, _ = USB_PORTS
    w, d = USB_SLOT
    return (
        cq.Workplane("XZ", origin=((left + right) / 2, -SHELL_H / 2, -LIP - 4.0 - d / 2))
        .rect(w, d)
        .extrude(-WALL * z_from)
    )


def shell() -> cq.Workplane:
    """The screen's housing: a lip in front over the glass's border, walls, bosses for the
    back cover's screws, inserts for the pod and the bracket, and the opening down into
    the pod."""
    body = rounded_box(SHELL_W, SHELL_H, SHELL_D, CORNER_R)
    pocket = (
        cq.Workplane("XY")
        .workplane(offset=-LIP)
        .rect(GLASS_W + 2 * CLEAR, GLASS_H + 2 * CLEAR)
        .extrude(-(SHELL_D - LIP))
    )
    left, right, bottom, top = window()
    opening = (
        cq.Workplane("XY")
        .center((left + right) / 2, (bottom + top) / 2)
        .rect(right - left, top - bottom)
        .extrude(-LIP)
    )
    body = body.cut(pocket).cut(opening)
    # Corner bosses for the back cover, outside the glass's rounded corners and behind it.
    bosses = (
        cq.Workplane("XY")
        .workplane(offset=-LIP - 3.0)
        .pushPoints(corner_points(CORNER_INSET))
        .circle(SCREW_BOSS_D / 2)
        .extrude(-(SHELL_D - LIP - 3.0))
    )
    holes = (
        cq.Workplane("XY")
        .workplane(offset=-SHELL_D)
        .pushPoints(corner_points(CORNER_INSET))
        .circle(INSERT_D / 2)
        .extrude(8.0)
    )
    body = body.union(bosses).cut(holes)
    # The bottom wall: two inserts for the pod, either side of the opening into it.
    bottom_y = -SHELL_H / 2
    for x in (-POD_SCREW_X, POD_SCREW_X):
        boss = (
            cq.Workplane("XZ", origin=(x, bottom_y + WALL, -SHELL_D / 2))
            .circle(SCREW_BOSS_D / 2)
            .extrude(-6.0)
        )
        hole = (
            cq.Workplane("XZ", origin=(x, bottom_y, -SHELL_D / 2))
            .circle(INSERT_D / 2)
            .extrude(-(WALL + 6.0))
        )
        body = body.union(boss).cut(hole)
    body = body.cut(usb_slot(+1))
    # The left side wall, toward the machine: two inserts for the bracket.
    left_x = -SHELL_W / 2
    for y in (-SHELL_H / 4, SHELL_H / 4):
        boss = (
            cq.Workplane("YZ", origin=(left_x + WALL, y, -SHELL_D / 2))
            .circle(SCREW_BOSS_D / 2)
            .extrude(6.0)
        )
        hole = (
            cq.Workplane("YZ", origin=(left_x, y, -SHELL_D / 2))
            .circle(INSERT_D / 2)
            .extrude(WALL + 6.0)
        )
        body = body.union(boss).cut(hole)
    return body


def back() -> cq.Workplane:
    """The shell's back cover. Four posts reach the ends of the board's standoffs, and M3
    screws through them hold the board, pressing its glass against the shell's lip."""
    plate = (
        cq.Workplane("XY")
        .workplane(offset=-SHELL_D)
        .rect(SHELL_W, SHELL_H)
        .extrude(-BACK)
        .edges("|Z")
        .fillet(CORNER_R)
    )
    standoff_end = LIP + MOUNT_FACE + STANDOFF
    posts = (
        cq.Workplane("XY")
        .workplane(offset=-SHELL_D)
        .pushPoints(FRAME_MOUNTS)
        .circle(3.5)
        .extrude(SHELL_D - standoff_end)
    )
    plate = plate.union(posts)
    screws = (
        cq.Workplane("XY")
        .workplane(offset=-SHELL_D - BACK)
        .pushPoints(FRAME_MOUNTS + corner_points(CORNER_INSET))
        .circle(M3_CLEAR / 2)
        .extrude(SHELL_D)
    )
    return plate.cut(screws)


def pod_center_y() -> float:
    return -SHELL_H / 2 - POD_H / 2


def pod() -> cq.Workplane:
    """The E-stop's housing, below the shell: its face flush with the screen's, its top
    screwed to the shell's bottom. Leaning back, the panel stands on the pod's back bottom
    edge."""
    top = -SHELL_H / 2
    cy = pod_center_y()
    outer = (
        cq.Workplane("XY")
        .center(0, cy)
        .rect(SHELL_W, POD_H)
        .extrude(-POD_DEPTH)
        .edges("|Z")
        .fillet(CORNER_R)
    )
    inner = (
        cq.Workplane("XY")
        .workplane(offset=-ESTOP_FACE)
        .center(0, cy)
        .rect(SHELL_W - 2 * WALL, POD_H - 2 * WALL)
        .extrude(-POD_DEPTH)
    )
    body = outer.cut(inner)
    hole = cq.Workplane("XY").center(ESTOP_X, cy).circle(ESTOP_HOLE / 2).extrude(-ESTOP_FACE)
    key = (
        cq.Workplane("XY")
        .center(ESTOP_X, cy + ESTOP_HOLE / 2 + ESTOP_KEY[1] / 2 - 0.5)
        .rect(ESTOP_KEY[0], ESTOP_KEY[1] + 1.0)
        .extrude(-ESTOP_FACE)
    )
    body = body.cut(hole).cut(key)
    # The top wall: screws up into the shell's inserts, and the opening into the shell.
    for x in (-POD_SCREW_X, POD_SCREW_X):
        screw = cq.Workplane("XZ", origin=(x, top, -SHELL_D / 2)).circle(M3_CLEAR / 2).extrude(WALL)
        body = body.cut(screw)
    return body.cut(usb_slot(-1))


def pod_back() -> cq.Workplane:
    """The pod's back cover, pressed into the pod's back: a slot for the USB cable, its far
    plug passing through, and a hole for the cable from the E-stop's power pole."""
    cy = pod_center_y()
    plate = (
        cq.Workplane("XY")
        .workplane(offset=-POD_DEPTH)
        .center(0, cy)
        .rect(SHELL_W - 2 * WALL - 0.4, POD_H - 2 * WALL - 0.4)
        .extrude(-BACK)
    )
    usb = (
        cq.Workplane("XY")
        .workplane(offset=-POD_DEPTH - BACK)
        .center(-SHELL_W / 4 - 6, cy)
        .slot2D(POD_CABLES[0], POD_CABLES[1])
        .extrude(BACK)
    )
    power = (
        cq.Workplane("XY")
        .workplane(offset=-POD_DEPTH - BACK)
        .center(-8.0, cy)
        .circle(POD_CABLES[1] / 2)
        .extrude(BACK)
    )
    return plate.cut(usb).cut(power)


def to_table(y: float, z: float) -> tuple[float, float]:
    """A point on the panel's side (upright coordinates) as it stands leaned back: its
    height above the table, and how far in front of the pod's back bottom edge (+) or
    behind it (-). The panel leans back about that edge, which stands on the table."""
    lean = math.radians(LEAN)
    ry, rz = y - (-SHELL_H / 2 - POD_H), z + POD_DEPTH
    return ry * math.cos(lean) + rz * math.sin(lean), -ry * math.sin(lean) + rz * math.cos(lean)


def bracket_points() -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """The bracket's screw holes, as (height, forward) on the table: two M3 into the
    shell's left side, and two M5 into T-nuts in the rail's outer slot, at its height."""
    shell_holes = [to_table(y, -SHELL_D / 2) for y in (-SHELL_H / 4, SHELL_H / 4)]
    forward = sum(f for _, f in shell_holes) / 2
    rail_holes = [
        (RAIL_HEIGHT, forward + RAIL_HOLE_PITCH / 2),
        (RAIL_HEIGHT, forward - RAIL_HOLE_PITCH / 2),
    ]
    return shell_holes, rail_holes


def bracket() -> cq.Workplane:
    """The plate from the shell's left side to the rail, drawn as it stands: Y up from
    the table, Z forward, X its thickness. Printed flat. Its outline is the hull around its
    four holes, so it fits the rail wherever it is: below the shell's screws or between
    them."""
    shell_holes, rail_holes = bracket_points()
    margin = 9.0
    # On the "YZ" workplane, local x is the height (Y) and local y is forward (Z).
    outline = cq.Sketch().push(shell_holes + rail_holes).circle(margin).reset().hull()
    plate = cq.Workplane("YZ").placeSketch(outline).extrude(BRACKET_T)
    holes = [(p, M3_CLEAR) for p in shell_holes] + [(p, RAIL_SLOT_M5) for p in rail_holes]
    for (h, f), d in holes:
        plate = plate.cut(cq.Workplane("YZ").center(h, f).circle(d / 2).extrude(BRACKET_T))
    return plate


PARTS = {
    "shell": shell,
    "back": back,
    "pod": pod,
    "pod-back": pod_back,
    "bracket": bracket,
}


def standing(part: cq.Workplane) -> cq.Workplane:
    """A panel part as it stands, leaned back on the pod's back bottom edge: Y up from the
    table, Z forward from that edge."""
    return part.translate((0, SHELL_H / 2 + POD_H, POD_DEPTH)).rotate((0, 0, 0), (1, 0, 0), -LEAN)


BOARD_STEP_ORIGIN = (1.02, 0.8)
"""In Waveshare's STEP model: the glass's center across (its Z) and its face (its Y)."""
ESTOP_STEP_FACE = -32.59
"""In TE's STEP model: the panel's face along the switch's axis (its Z)."""


def board_model(path: Path) -> cq.Workplane:
    """Waveshare's model of the board, placed: its X (long) becomes our Y, its Z our X,
    and its Y (out of the glass) our Z, with the glass's face against the lip."""
    model = cq.importers.importStep(str(path))
    turned = model.rotate((0, 0, 0), (1, 1, 1), 120)
    across, face = BOARD_STEP_ORIGIN
    return turned.translate((-across, 0, -face - LIP))


def to_board_coordinates(part: cq.Workplane) -> cq.Workplane:
    """A part moved into the coordinates of Waveshare's model, undoing board_model()."""
    across, face = BOARD_STEP_ORIGIN
    return part.translate((across, 0, face + LIP)).rotate((0, 0, 0), (1, 1, 1), -120)


def estop_model(path: Path) -> cq.Workplane:
    """TE's model of the switch, placed in the pod's hole, its face on the pod's face."""
    model = cq.importers.importStep(str(path))
    return model.translate((ESTOP_X, pod_center_y(), -ESTOP_STEP_FACE))


def interference(part: cq.Workplane, model: cq.Workplane) -> float:
    """The volume a part and a model share, in cubic millimeters."""
    shape = part.val()
    pb = shape.BoundingBox()
    total = 0.0
    for solid in model.solids().vals():
        sb = solid.BoundingBox()
        if (
            sb.xmax < pb.xmin
            or sb.xmin > pb.xmax
            or sb.ymax < pb.ymin
            or sb.ymin > pb.ymax
            or sb.zmax < pb.zmin
            or sb.zmin > pb.zmax
        ):
            continue
        total += shape.intersect(solid).Volume()
    return total


def fit_assembly(made: dict[str, cq.Workplane], models: list[cq.Workplane]) -> cq.Assembly:
    """The parts and the makers' models together, upright, colored, for a test fit."""
    fit = cq.Assembly(name="panel-fit")
    colors = {"shell": "gray30", "back": "gray50", "pod": "gray30", "pod-back": "gray50"}
    for name, color in colors.items():
        fit.add(made[name], name=name, color=cq.Color(color))
    for i, model in enumerate(models):
        fit.add(model, name=f"model-{i}", color=cq.Color("goldenrod"))
    return fit


def assembly(models: list[cq.Workplane]) -> cq.Workplane:
    """Everything as it stands beside the rail, with the rail and the table, for a look.
    ``models`` are the board and switch models, if given; otherwise simple stand-ins."""
    if not models:
        glass = cq.Workplane("XY").box(GLASS_W, GLASS_H, 1.0).translate((0, 0, -LIP - 0.5))
        knob = (
            cq.Workplane("XY")
            .circle(ESTOP_KNOB_D / 2)
            .extrude(ESTOP_KNOB_OUT)
            .translate((ESTOP_X, pod_center_y(), 0))
        )
        models = [glass, knob]
    parts = [standing(p) for p in [shell(), back(), pod(), pod_back(), *models]]
    parts.append(bracket().translate((-SHELL_W / 2 - BRACKET_T, 0, 0)))
    rail = (
        cq.Workplane("XY")
        .box(20, 20, 400)
        .translate((-SHELL_W / 2 - BRACKET_T - 10, RAIL_HEIGHT, -100))
    )
    table = cq.Workplane("XY").box(400, 2, 450).translate((0, -1, -100))
    result = cq.Workplane("XY")
    for p in [*parts, rail, table]:
        result = result.add(p)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--board", type=Path, help="Waveshare's STEP model, for a test fit")
    parser.add_argument("--estop", type=Path, help="TE's STEP model of the E-stop")
    args = parser.parse_args()
    out = HERE / "out"
    out.mkdir(exist_ok=True)
    made = {}
    for name, make in PARTS.items():
        part = made[name] = make()
        bb = part.val().BoundingBox()
        cq.exporters.export(part, str(out / f"panel-{name}.step"))
        cq.exporters.export(part, str(out / f"panel-{name}.stl"), tolerance=0.05)
        print(f"{name}: {bb.xlen:.1f} x {bb.ylen:.1f} x {bb.zlen:.1f} mm")
    left, right, bottom, top = window()
    print(f"window: {right - left:.1f} x {top - bottom:.1f} mm")
    models = []
    if args.board:
        models.append(board_model(args.board))
    if args.estop:
        models.append(estop_model(args.estop))
    for model in models:
        for name, part in made.items():
            if name == "bracket":
                continue
            v = interference(part, model)
            if v > 0.01:
                print(f"  {name} and a model overlap by {v:.1f} cubic mm")
    if models:
        # The board's model grows tenfold when written again as STEP, so the whole fit is
        # written as glTF to look at, and the enclosure alone as STEP in Waveshare's
        # coordinates, to open in CAD over their original model.
        fit_assembly(made, models).export(str(out / "panel-fit.glb"))
        in_board = cq.Assembly(name="panel-in-board-coordinates")
        for name in ("shell", "back", "pod", "pod-back"):
            in_board.add(to_board_coordinates(made[name]), name=name)
        if args.estop:
            in_board.add(to_board_coordinates(estop_model(args.estop)), name="estop")
        in_board.export(str(out / "panel-in-board-coordinates.step"))
    whole = assembly(models)
    name = "panel-fit" if models else "panel-assembly"
    if not models:
        cq.exporters.export(whole, str(out / f"{name}.step"))
    views = {"front": (-0.6, 0.35, 1.0), "side": (1.0, 0.0, 0.0), "above": (-0.3, 1.0, 0.6)}
    for view, direction in views.items():
        cq.exporters.export(
            whole,
            str(out / f"{name}-{view}.svg"),
            opt={"width": 900, "height": 700, "projectionDir": direction, "showHidden": False},
        )
    lean = math.radians(LEAN)
    total = SHELL_H + POD_H
    print(
        f"leaning {LEAN:g} degrees back: the top is {total * math.cos(lean):.0f} mm above the "
        f"table and {total * math.sin(lean):.0f} mm behind the front edge; the screen's "
        f"center is {(POD_H + SHELL_H / 2) * math.cos(lean):.0f} mm up"
    )


if __name__ == "__main__":
    main()
