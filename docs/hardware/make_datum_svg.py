"""Plan view of the PicnicPlace datum (concept, rough dimensions), as an SVG.

Run from the repository root: python3 docs/hardware/make_datum_svg.py \
    > docs/hardware/picnicplace-datum-plan.svg

Board coordinates in millimeters: the origin is the inside corner of the L, where a board's
front-left corner sits; +X right, +Y toward the back (up on the drawing).
"""

import sys

S = 5.0  # px per mm
X0, Y0 = -62.0, 95.0  # top-left of the drawing, in mm (Y up)
W_MM, H_MM = 222.0, 200.0

LEG = 30.0  # width of each leg
LONG = 150.0  # overall X
SHORT = 75.0  # overall Y
PRINT_MARGIN = 4.0  # the 3D-printed holder beyond the PCB
LEDGE = 10.0  # the holder's ledge under the board's edges

INK = "#1d2430"
MUTED = "#6b7480"
COPPER = "#b8741a"
PCB = "#2f6d4f"
PCB_FILL = "#e6f0ea"
PRINT_FILL = "#eef0f3"
BOARD = "#3a6fb0"
PIN = "#4a4f57"


def px(x: float, y: float) -> tuple[float, float]:
    return ((x - X0) * S, (Y0 - y) * S)


out: list[str] = []


def add(s: str) -> None:
    out.append(s)


def poly(points: list[tuple[float, float]], **attrs: str) -> None:
    pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in (px(x, y) for x, y in points))
    extra = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    add(f'<polygon points="{pts}" {extra}/>')


def rect(x0: float, y0: float, x1: float, y1: float, **attrs: str) -> None:
    poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], **attrs)


def circle(x: float, y: float, r: float, **attrs: str) -> None:
    cx, cy = px(x, y)
    extra = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r * S:.1f}" {extra}/>')


def line(x0: float, y0: float, x1: float, y1: float, **attrs: str) -> None:
    a, b = px(x0, y0)
    c, d = px(x1, y1)
    extra = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    add(f'<line x1="{a:.1f}" y1="{b:.1f}" x2="{c:.1f}" y2="{d:.1f}" {extra}/>')


def text(
    x: float, y: float, s: str, size: float = 11, anchor: str = "middle", **attrs: str
) -> None:
    a, b = px(x, y)
    extra = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    add(
        f'<text x="{a:.1f}" y="{b:.1f}" font-size="{size}" text-anchor="{anchor}" {extra}>'
        f"{s}</text>"
    )


def dim_h(x0: float, x1: float, y: float, label: str) -> None:
    line(x0, y, x1, y, stroke=MUTED, stroke_width="1", marker_start="url(#arr)",
         marker_end="url(#arr)")
    text((x0 + x1) / 2, y + 1.2, label, size=11, fill=MUTED)


def dim_v(y0: float, y1: float, x: float, label: str) -> None:
    line(x, y0, x, y1, stroke=MUTED, stroke_width="1", marker_start="url(#arr)",
         marker_end="url(#arr)")
    a, b = px(x - 1.5, (y0 + y1) / 2)
    add(
        f'<text x="{a:.1f}" y="{b:.1f}" font-size="11" text-anchor="middle" fill="{MUTED}" '
        f'transform="rotate(-90 {a:.1f} {b:.1f})">{label}</text>'
    )


add(
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W_MM * S:.0f}" height="{H_MM * S:.0f}" '
    f'viewBox="0 0 {W_MM * S:.0f} {H_MM * S:.0f}" font-family="Helvetica, Arial, sans-serif">'
)
add(
    '<defs><marker id="arr" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="7" '
    'markerHeight="7" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" '
    f'fill="{MUTED}"/></marker></defs>'
)
add(f'<rect width="100%" height="100%" fill="#ffffff"/>')

text(X0 + 4, Y0 - 6, "PicnicPlace datum: plan view (concept, rough dimensions, mm)", size=16,
     anchor="start", fill=INK, font_weight="bold")
text(X0 + 4, Y0 - 11, "Origin at the inside corner of the L: a board's front-left corner. "
     "+X right, +Y toward the back of the machine.", size=11, anchor="start", fill=MUTED)

# The board it holds (part of one, dashed): up to A4 / Letter.
rect(0, 0, 130, 70, fill="none", stroke=BOARD, stroke_width="1.5", stroke_dasharray="8 5")
text(85, 40, "Board or panel (up to A4 / Letter)", size=13, fill=BOARD)
text(85, 34, "pushed into the corner, against the three pins", size=11, fill=BOARD)
line(130, 70, 140, 70, stroke=BOARD, stroke_width="1.5", stroke_dasharray="3 4")
line(130, 0, 140, 0, stroke=BOARD, stroke_width="1.5", stroke_dasharray="3 4")

# The 3D-printed holder: the L grown by a margin, with a ledge under the board's edges.
m = PRINT_MARGIN
L_outline = [
    (-LEG, -LEG), (LONG - LEG, -LEG), (LONG - LEG, 0), (0, 0), (0, SHORT - LEG),
    (-LEG, SHORT - LEG),
]
holder = [
    (-LEG - m, -LEG - m), (LONG - LEG + m, -LEG - m), (LONG - LEG + m, LEDGE),
    (LEDGE, LEDGE), (LEDGE, SHORT - LEG + m), (-LEG - m, SHORT - LEG + m),
]
poly(holder, fill=PRINT_FILL, stroke=MUTED, stroke_width="1.2", stroke_dasharray="5 3")

# The datum PCB.
poly(L_outline, fill=PCB_FILL, stroke=PCB, stroke_width="2")

# Edge relief: the PCB's inside edges are cut back 0.5 mm between the pins, so only the
# pins touch the board (drawn exaggerated).
relief = "#c9473b"
line(0, -1.2, 120, -1.2, stroke=relief, stroke_width="1", stroke_dasharray="2 3")
line(-1.2, 0, -1.2, 45, stroke=relief, stroke_width="1", stroke_dasharray="2 3")

# Locating pins: 3 mm dowels, tangent to the board's edge lines (two on X, one on Y).
for x, y in [(20, -1.5), (105, -1.5), (-1.5, 32)]:
    circle(x, y, 1.5, fill=PIN, stroke="none")
text(20, -5.5, "pin", size=10, fill=PIN)
text(105, -5.5, "pin", size=10, fill=PIN)
text(-5.5, 32, "pin", size=10, fill=PIN, anchor="end")


def fiducial(x: float, y: float, label: str, below: str = "") -> None:
    circle(x, y, 3.0, fill="#ffffff", stroke=PCB, stroke_width="1")  # mask opening
    circle(x, y, 0.5, fill=COPPER, stroke="none")  # 1 mm copper dot
    text(x, y - 5.5, label, size=10, fill=INK)
    if below:
        text(x, y - 8.5, below, size=10, fill=MUTED)


# Homing fiducial in the corner square: everything is measured from here.
fiducial(-15, -15, "Homing dot", "1 mm, 6 mm clear")
# Far fiducials: 120 mm along X and 45 mm along Y from the homing dot. Measured with the
# camera, they give X and Y scale and whether the gantry is square.
fiducial(105, -15, "X fiducial", "120 mm from the dot")
fiducial(-15, 30, "Y fiducial", "")
text(-15, 22, "45 mm from", size=10, fill=MUTED)
text(-15, 19, "the dot", size=10, fill=MUTED)

# Aiming ring (as the LumenPnP datum's): a copper ring for aiming the nozzles.
circle(-15, 8, 4.0, fill="none", stroke=COPPER, stroke_width=f"{0.6 * S:.1f}")
text(-15, 1.0, "Aiming ring", size=10, fill=INK)

# Bare area for the nozzle-offset disc or token: nothing printed, flat solder mask.
rect(4, -26, 28, -4, fill="#ffffff", stroke=INK, stroke_width="1", stroke_dasharray="3 2")
text(16, -13, "Disc / token", size=10, fill=INK)
text(16, -16.5, "24 x 22 bare", size=10, fill=MUTED)

# Copper pad the nozzles touch to find their height (ENIG, flat).
rect(34, -20, 46, -8, fill=COPPER, stroke="none", opacity="0.85")
text(40, -24, "Z touch pad", size=10, fill=INK)
text(40, -27, "12 x 12", size=10, fill=MUTED)

# Camera scale target: a 10 x 10 mm grid of 0.5 mm dots at 1 mm pitch.
for i in range(11):
    for j in range(11):
        circle(52 + i, -20 + j, 0.18, fill=COPPER, stroke="none")
text(57, -24, "Scale target", size=10, fill=INK)
text(57, -27, "10 mm dot grid", size=10, fill=MUTED)

# Spare space: label, QR code with the datum's revision.
rect(68, -22, 82, -8, fill="none", stroke=MUTED, stroke_width="1")
text(75, -15, "QR / rev", size=9, fill=MUTED)

# M3 screws holding the PCB to the holder (heat-set inserts in the print).
for x, y in [(-26, -26), (-26, 41), (91, -26), (116, -26)]:
    circle(x, y, 1.6, fill="#ffffff", stroke=INK, stroke_width="1")
    circle(x, y, 2.8, fill="none", stroke=INK, stroke_width="0.6")
# Holder to the staging plate: M3 on a 30 mm grid (v4.1's 15 mm checkerboard and v4.2's
# 30 mm grid both have these), counterbored, under the PCB.
# In the ledge, reachable with no board in place.
for x, y in [(35, 5), (95, 5), (5, 35)]:
    circle(x, y, 1.6, fill="none", stroke=MUTED, stroke_width="1")
    circle(x, y, 3.0, fill="none", stroke=MUTED, stroke_width="0.8")

# Dimensions.
dim_h(-LEG, LONG - LEG, -LEG - 10, "150")
dim_v(-LEG, SHORT - LEG, -LEG - 10, "75")
dim_h(-LEG, 0, SHORT - LEG + 8, "30")
dim_v(-LEG, 0, LONG - LEG + 9, "30")
dim_h(-15, 105, -LEG - 16, "120 (dot to X fiducial)")

# Origin marker.
line(0, 0, 8, 0, stroke=INK, stroke_width="1.5")
line(0, 0, 0, 8, stroke=INK, stroke_width="1.5")
text(9.5, -0.8, "+X", size=10, fill=INK, anchor="start")
text(0, 9.5, "+Y", size=10, fill=INK)
text(3, 2.2, "0,0", size=10, fill=INK, anchor="start")

# Legend and notes.
lx, ly = -58, -62
notes = [
    ("PCB", "Datum PCB, 1.6 mm, ENIG", PCB),
    ("print", "3D-printed holder (dashed): top level with", MUTED),
    ("", "the LumenPnP board holders; a 10 mm ledge", MUTED),
    ("", "supports the board's edges, so a 1.6 mm board's", MUTED),
    ("", "top is level with the datum's", MUTED),
    ("pin", "3 mm dowels press-fit in plated holes; the", PIN),
    ("", "edges between them cut back 0.5 mm (red)", PIN),
    ("scr", "M3: PCB to holder (black); holder to the staging", INK),
    ("", "plate in the ledge, 30 mm apart (gray, counterbored)", INK),
    ("cu", "Copper: fiducials, ring, Z pad, scale dots", COPPER),
]
for i, (_, s, color) in enumerate(notes):
    col, row = divmod(i, 5)
    text(lx + col * 105, ly - row * 4.6, s, size=11, fill=color, anchor="start")

add("</svg>")
sys.stdout.write("\n".join(out) + "\n")
