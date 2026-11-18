"""Generate the datum's 3D-printed holder, and a coupon for sizing its pin holes.

Run with CadQuery (2.4 or later), from anywhere:

    python hardware/datum/make_holder.py

It writes STEP and STL files to out/ next to it. Every dimension is here: change it and
run the script again rather than editing the models. Values marked MEASURE are not yet
checked on a real machine. The design is described in docs/hardware/picnicplace-datum.md.

The holder sits on the staging plate under the datum PCB. Its top carries the PCB and,
on a 10 mm ledge inside the L, the edges of the board being placed, level with the
LumenPnP's board holders. It grips the three locating pins, holds the PCB with four M3
screws into heat-set inserts, and screws to the staging plate with three M3 screws,
counterbored in the ledge.

Print it flat on its bottom. Before the holder, print the coupon: it has the pin hole in
four sizes, and the one a pin presses into firmly, by hand or with a gentle tap, is the
size for PIN_GRIP_D.

Design coordinates match make_datum.py: millimeters, the origin where a board's
front-left corner sits, +X right, +Y toward the back of the machine, and Z up from the
staging plate.
"""

from __future__ import annotations

from pathlib import Path

import cadquery as cq

HERE = Path(__file__).resolve().parent

# From make_datum.py (keep in step): the L, the pins and the PCB's screws.
LEG = 30.0
LONG = 150.0
SHORT = 75.0
PCB_T = 1.6
PIN_R = 1.5
INSIDE = -PIN_R
PINS = [(20.0, INSIDE), (105.0, INSIDE), (INSIDE, 32.0)]
PCB_SCREWS = [(-26.0, -26.0), (-26.0, 41.0), (91.0, -26.0), (116.0, -26.0)]

# The holder.
TOP = 8.4
"""MEASURE: the LumenPnP board holders' top above the staging plate (from published
figures). The holder's top is level with it, so a board on the ledge and the datum PCB
beside it are at the same height."""
MARGIN = 4.0
"""How far the holder reaches beyond the PCB's outside edges."""
LEDGE = 10.0
"""How far the holder reaches in under the board being placed, from the board's edges."""
CORNER_R = 3.0

# The pins: 3 x 10 mm dowels, h6 or m6, rounded or chamfered end up.
PIN_L = 10.0
PIN_GRIP = 6.0
"""How deep the holder grips each pin. The pin's foot rests on the hole's floor, so its top
stands PIN_L - PIN_GRIP - PCB_T = 2.4 mm above a 1.6 mm board's top."""
PIN_GRIP_D = 2.9
"""MEASURE: the printed pin hole, which most printers make a little small. Size it with
the coupon."""
VENT_D = 1.2
"""A hole from each pin hole's floor through the bottom, so pressing a pin in does not
trap air, and a pin can be pushed out from below."""

# Screws.
INSERT_D = 4.0
"""Holes for M3 heat-set inserts (most take a 4.0 mm hole)."""
INSERT_DEPTH = 6.0
M3_CLEAR = 3.4
M3_HEAD_D = 6.2
M3_HEAD_H = 3.4
"""A counterbore for an M3 socket head (5.5 mm across, 3 mm tall), below the ledge's
top."""
PLATE_SCREWS = [(35.0, 5.0), (95.0, 5.0), (5.0, 35.0)]
"""Holder to the staging plate, in the ledge, 30 mm apart: v4.1's 15 mm checkerboard and
v4.2's 30 mm grid both have holes 30 mm apart. MEASURE: where the datum goes on the plates
sets these against the plates' holes."""

# The coupon.
COUPON_SIZES = [2.8, 2.9, 3.0, 3.1]
COUPON_PITCH = 9.0


def outline() -> list[tuple[float, float]]:
    """The holder's outline: the PCB's L grown by MARGIN outside, and reaching LEDGE in
    under the board."""
    m = MARGIN
    right, top = LONG - LEG + m, SHORT - LEG + m
    return [
        (-LEG - m, -LEG - m),
        (right, -LEG - m),
        (right, LEDGE),
        (LEDGE, LEDGE),
        (LEDGE, top),
        (-LEG - m, top),
    ]


def pin_hole(body: cq.Workplane, x: float, y: float, d: float) -> cq.Workplane:
    """A blind pin hole PIN_GRIP deep from the top, with its vent through the bottom."""
    hole = (
        cq.Workplane("XY")
        .workplane(offset=TOP - PIN_GRIP)
        .center(x, y)
        .circle(d / 2)
        .extrude(PIN_GRIP)
    )
    vent = cq.Workplane("XY").center(x, y).circle(VENT_D / 2).extrude(TOP)
    return body.cut(hole).cut(vent)


def holder() -> cq.Workplane:
    body = cq.Workplane("XY").polyline(outline()).close().extrude(TOP)
    body = body.edges("|Z").fillet(CORNER_R)
    for x, y in PINS:
        body = pin_hole(body, x, y, PIN_GRIP_D)
    inserts = (
        cq.Workplane("XY")
        .workplane(offset=TOP - INSERT_DEPTH)
        .pushPoints(PCB_SCREWS)
        .circle(INSERT_D / 2)
        .extrude(INSERT_DEPTH)
    )
    screws = cq.Workplane("XY").pushPoints(PLATE_SCREWS).circle(M3_CLEAR / 2).extrude(TOP)
    heads = (
        cq.Workplane("XY")
        .workplane(offset=TOP - M3_HEAD_H)
        .pushPoints(PLATE_SCREWS)
        .circle(M3_HEAD_D / 2)
        .extrude(M3_HEAD_H)
    )
    return body.cut(inserts).cut(screws).cut(heads)


def coupon() -> cq.Workplane:
    """A small block with the pin hole in each of COUPON_SIZES, as deep as the holder's,
    each labeled on the top."""
    n = len(COUPON_SIZES)
    w, h = COUPON_PITCH * n, 14.0
    body = cq.Workplane("XY").box(w, h, TOP, centered=(True, True, False))
    for i, d in enumerate(COUPON_SIZES):
        x = (i - (n - 1) / 2) * COUPON_PITCH
        body = pin_hole(body, x, -2.5, d)
        label = (
            cq.Workplane("XY")
            .workplane(offset=TOP)
            .center(x, 3.5)
            .text(f"{d:.1f}", 3.0, -0.6, halign="center", valign="center")
        )
        body = body.cut(label)
    return body


def main() -> None:
    out = HERE / "out"
    out.mkdir(exist_ok=True)
    for name, make in (("holder", holder), ("pin-coupon", coupon)):
        part = make()
        bb = part.val().BoundingBox()
        cq.exporters.export(part, str(out / f"picnicplace-datum-{name}.step"))
        cq.exporters.export(part, str(out / f"picnicplace-datum-{name}.stl"), tolerance=0.05)
        print(f"{name}: {bb.xlen:.1f} x {bb.ylen:.1f} x {bb.zlen:.1f} mm")
    above = PIN_L - PIN_GRIP - PCB_T
    print(f"the pins stand {above:.1f} mm above a {PCB_T} mm board's top")
    cq.exporters.export(
        holder(),
        str(out / "picnicplace-datum-holder.svg"),
        opt={"width": 900, "height": 600, "projectionDir": (-0.4, -0.8, 0.6)},
    )


if __name__ == "__main__":
    main()
