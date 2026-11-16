"""Mockups of the control panel's screens, as SVGs, at the reference display's size
(1024 x 600). They illustrate docs/hardware/control-panel.md; keep the two in step.

Run from the repository root: python3 docs/hardware/make_panel_svg.py

It writes docs/hardware/panel-ready.svg, panel-running.svg, panel-paused.svg and
panel-tip-change.svg. The colors are the Studio's dark theme (studio/src/styles/tokens.css).
"""

from pathlib import Path
from xml.sax.saxutils import escape

W, H = 1024, 600
OUT = Path(__file__).parent

BG = "#121418"
SURFACE = "#1a1d23"
RAISED = "#22262e"
BORDER = "#2c313a"
BORDER_STRONG = "#3a404b"
TEXT = "#e7e9ee"
MUTED = "#8b93a1"
FAINT = "#5c6370"
ACCENT = "#2fbf86"
ACCENT_TEXT = "#08130e"
ACCENT_SOFT = "#173a2d"
WARN = "#f2b84b"
WARN_SOFT = "#3a3122"
DANGER = "#ef4d55"
DANGER_SOFT = "#3a1f23"

SANS = "Inter, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
MONO = "'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace"

TOP = 56
"""The status bar's height."""
BAR = 104
"""The action bar's height, at the bottom."""
LEFT_W = 330
"""The position column's width."""
GAP = 16


class Svg:
    def __init__(self, title: str) -> None:
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
            f'width="{W}" height="{H}" role="img" aria-label="{escape(title)}">',
            f"<title>{escape(title)}</title>",
            f'<rect width="{W}" height="{H}" fill="{BG}"/>',
        ]

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill: str,
        stroke: str | None = None,
        r: float = 10,
        width: float = 1.5,
    ) -> None:
        line = f' stroke="{stroke}" stroke-width="{width}"' if stroke else ""
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}"{line}/>'
        )

    def text(
        self,
        x: float,
        y: float,
        s: str,
        size: float = 18,
        fill: str = TEXT,
        weight: int = 400,
        anchor: str = "start",
        mono: bool = False,
    ) -> None:
        family = MONO if mono else SANS
        self.parts.append(
            f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(s)}</text>'
        )

    def chip(self, x: float, s: str, color: str, soft: str) -> float:
        """A status chip in the top bar, from its left edge; returns its right edge."""
        w = 36 + len(s) * 9.2
        self.rect(x, 12, w, 32, soft, r=16)
        self.parts.append(f'<circle cx="{x + 15}" cy="28" r="5" fill="{color}"/>')
        self.text(x + 27, 34, s, 15, TEXT, 500)
        return x + w

    def button(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        label: str,
        kind: str = "accent",
        sub: str | None = None,
        enabled: bool = True,
    ) -> None:
        fill, stroke, ink = {
            "accent": (ACCENT, None, ACCENT_TEXT),
            "warn": (WARN, None, ACCENT_TEXT),
            "danger": (DANGER_SOFT, DANGER, DANGER),
            "plain": (RAISED, BORDER_STRONG, TEXT),
        }[kind]
        if not enabled:
            fill, stroke, ink = RAISED, BORDER, FAINT
        self.rect(x, y, w, h, fill, stroke, r=12, width=2)
        mid = y + h / 2 + (0 if sub else 9)
        self.text(x + w / 2, mid - (4 if sub else 0), label, 26, ink, 700, "middle")
        if sub:
            self.text(x + w / 2, mid + 22, sub, 14, ink, 500, "middle")

    def save(self, name: str) -> None:
        self.parts.append("</svg>")
        (OUT / name).write_text("\n".join(self.parts) + "\n")


def top_bar(svg: Svg, chips: list[tuple[str, str, str]], right: str, right_color: str) -> None:
    svg.rect(0, 0, W, TOP, SURFACE, r=0)
    svg.parts.append(f'<line x1="0" y1="{TOP}" x2="{W}" y2="{TOP}" stroke="{BORDER}"/>')
    svg.text(GAP, 36, "PicnicPlace", 20, TEXT, 700)
    svg.text(140, 36, "LumenPnP v4", 15, MUTED, 500)
    x = 260.0
    for s, color, soft in chips:
        x = svg.chip(x, s, color, soft) + 8
    svg.text(W - GAP, 36, right, 18, right_color, 700, "end")


def position(svg: Svg, x: float, y: float, z: float, a: float, b: float, tips: str) -> None:
    """The position column: X, Y and Z large, the nozzles' angles and tips below."""
    top, bottom = TOP + GAP, H - BAR - GAP
    svg.rect(GAP, top, LEFT_W - GAP, bottom - top, SURFACE, BORDER)
    svg.text(GAP + 20, top + 32, "POSITION  (mm)", 13, MUTED, 600)
    for i, (axis, value) in enumerate((("X", x), ("Y", y), ("Z", z))):
        row = top + 92 + i * 70
        svg.text(GAP + 20, row, axis, 30, MUTED, 600)
        svg.text(LEFT_W - 20, row, f"{value:8.2f}", 44, TEXT, 600, "end", mono=True)
    line = top + 286
    svg.parts.append(
        f'<line x1="{GAP + 20}" y1="{line}" x2="{LEFT_W - 20}" y2="{line}" stroke="{BORDER}"/>'
    )
    for i, (name, angle) in enumerate((("Left C", a), ("Right C", b))):
        row = line + 36 + i * 32
        svg.text(GAP + 20, row, name, 17, MUTED, 500)
        svg.text(LEFT_W - 20, row, f"{angle:7.1f}°", 22, TEXT, 500, "end", mono=True)
    svg.text(GAP + 20, bottom - 18, tips, 15, MUTED, 500)


def task_list(
    svg: Svg,
    title: str,
    progress: str,
    done: float,
    rows: list[tuple[str, str, str, str, str]],
    current: int,
    current_color: str = ACCENT,
    current_soft: str = ACCENT_SOFT,
) -> None:
    """The job's task list: placements in order, the one under way highlighted."""
    x0, top, bottom = LEFT_W + GAP, TOP + GAP, H - BAR - GAP
    w = W - GAP - x0
    svg.rect(x0, top, w, bottom - top, SURFACE, BORDER)
    svg.text(x0 + 20, top + 34, title, 20, TEXT, 700)
    svg.text(x0 + w - 20, top + 34, progress, 16, MUTED, 500, "end")
    svg.rect(x0 + 20, top + 48, w - 40, 8, RAISED, r=4)
    svg.rect(x0 + 20, top + 48, (w - 40) * done, 8, ACCENT, r=4)
    cols = (x0 + 24, x0 + 112, x0 + 236, x0 + 352)
    head = top + 86
    for cx, s in zip(cols, ("REF", "PART", "FOOTPRINT", "FEEDER"), strict=True):
        svg.text(cx, head, s, 12, MUTED, 600)
    svg.text(x0 + w - 24, head, "STATUS", 12, MUTED, 600, "end")
    for i, (ref, part, footprint, feeder, status) in enumerate(rows):
        y = head + 12 + i * 42
        if i == current:
            svg.rect(x0 + 10, y, w - 20, 40, current_soft, current_color, r=8, width=2)
        ink = TEXT if i >= current else MUTED
        weight = 700 if i == current else 500
        for cx, s in zip(cols, (ref, part, footprint, feeder), strict=True):
            svg.text(cx, y + 27, s, 18, ink, weight, mono=cx == cols[0])
        color = {
            "Placed": ACCENT,
            "Skipped": FAINT,
            "Waiting": FAINT,
        }.get(status, current_color if i == current else MUTED)
        svg.text(x0 + w - 24, y + 27, status, 17, color, 600, "end")


ROWS_DONE = [
    ("C3", "100 nF", "C_0402", "Slot 2", "Placed"),
    ("C4", "100 nF", "C_0402", "Slot 2", "Placed"),
    ("R5", "10 kΩ", "R_0402", "Slot 3", "Placed"),
    ("R6", "10 kΩ", "R_0402", "Slot 3", "Placed"),
]
ROWS_AFTER = [
    ("R8", "4.7 kΩ", "R_0402", "Slot 4", "Waiting"),
    ("D1", "Green", "LED_0603", "Slot 6", "Waiting"),
    ("D2", "Green", "LED_0603", "Slot 6", "Waiting"),
]
TIPS = "Tips: left N045, right N08"


def ready() -> None:
    svg = Svg("Control panel, ready: the machine needs homing before the job can start")
    top_bar(
        svg,
        [("Connected", ACCENT, ACCENT_SOFT), ("Not homed", WARN, WARN_SOFT)],
        "Ready",
        MUTED,
    )
    position(svg, 0, 0, 0, 0, 0, TIPS)
    x0, top, bottom = LEFT_W + GAP, TOP + GAP, H - BAR - GAP
    w = W - GAP - x0
    svg.rect(x0, top, w, bottom - top, SURFACE, BORDER)
    svg.text(x0 + 20, top + 34, "Blinky rev B", 20, TEXT, 700)
    svg.text(x0 + w - 20, top + 34, "48 placements · 6 feeders", 16, MUTED, 500, "end")
    svg.text(x0 + 20, top + 80, "Before the machine moves", 15, MUTED, 600)
    steps = [
        ("1", "Home the machine", "Its position is not known since it was switched on."),
        ("2", "Check the board", "The board is on the staging plate, against its stops."),
        ("3", "Start", "The job starts with the board's fiducials."),
    ]
    for i, (n, head, body) in enumerate(steps):
        y = top + 100 + i * 66
        color = WARN if i == 0 else FAINT
        svg.parts.append(f'<circle cx="{x0 + 38}" cy="{y + 22}" r="16" fill="{RAISED}"/>')
        svg.text(x0 + 38, y + 28, n, 17, color, 700, "middle")
        svg.text(x0 + 68, y + 18, head, 19, TEXT, 600)
        svg.text(x0 + 68, y + 42, body, 15, MUTED, 400)
    # The homing check: the same confirmation as the Studio's Home.
    cy = bottom - 84
    svg.rect(x0 + 20, cy, w - 40, 68, WARN_SOFT, WARN, r=10)
    svg.rect(x0 + 38, cy + 20, 28, 28, BG, WARN, r=6, width=2)
    svg.text(x0 + 80, cy + 30, "Both nozzles are level and raised", 18, TEXT, 600)
    svg.text(x0 + 80, cy + 52, "Homing drags a low nozzle across the machine.", 14, MUTED, 400)
    bar(svg)
    svg.button(GAP, H - BAR + 12, 300, BAR - 28, "Home", "plain", "tick the check first", False)
    svg.button(332, H - BAR + 12, 360, BAR - 28, "Start", "accent", "home first", False)
    svg.button(708, H - BAR + 12, W - GAP - 708, BAR - 28, "Stop", "danger", enabled=False)
    svg.save("panel-ready.svg")


def bar(svg: Svg) -> None:
    svg.rect(0, H - BAR, W, BAR, SURFACE, r=0)
    svg.parts.append(f'<line x1="0" y1="{H - BAR}" x2="{W}" y2="{H - BAR}" stroke="{BORDER}"/>')


def running() -> None:
    svg = Svg(
        "Control panel, running a job: the position, the task list with the placement "
        "under way highlighted, and Pause"
    )
    top_bar(
        svg,
        [
            ("Connected", ACCENT, ACCENT_SOFT),
            ("Homed", ACCENT, ACCENT_SOFT),
            ("Panel has control", ACCENT, ACCENT_SOFT),
        ],
        "Running",
        ACCENT,
    )
    position(svg, 142.37, 88.15, -27.24, 90.0, 0.0, TIPS)
    rows = [*ROWS_DONE[1:], ("R7", "10 kΩ", "R_0402", "Slot 3", "Placing · left"), *ROWS_AFTER]
    task_list(svg, "Blinky rev B", "21 of 48 placed · 6:12 left", 21 / 48, rows, 3)
    bar(svg)
    svg.button(GAP, H - BAR + 12, 660, BAR - 28, "Pause", "warn", "after the parts on the nozzles")
    svg.button(692, H - BAR + 12, W - GAP - 692, BAR - 28, "Stop", "danger")
    svg.save("panel-running.svg")


def paused() -> None:
    svg = Svg("Control panel, paused on a problem: the core's message and Resume")
    top_bar(
        svg,
        [
            ("Connected", ACCENT, ACCENT_SOFT),
            ("Homed", ACCENT, ACCENT_SOFT),
            ("Panel has control", ACCENT, ACCENT_SOFT),
        ],
        "Paused",
        WARN,
    )
    position(svg, 96.40, 212.80, 0.0, 0.0, 0.0, TIPS)
    rows = [*ROWS_DONE[2:], ("R7", "10 kΩ", "R_0402", "Slot 3", "Picking"), ROWS_AFTER[0]]
    task_list(svg, "Blinky rev B", "20 of 48 placed", 20 / 48, rows, 2, WARN, WARN_SOFT)
    # The message, over the bottom of the list: in the core's words, as the Studio shows it.
    x0 = LEFT_W + GAP
    y = H - BAR - GAP - 120
    svg.rect(x0 + 10, y, W - GAP - x0 - 20, 108, WARN_SOFT, WARN, r=10, width=2)
    svg.text(x0 + 30, y + 34, "Paused: picked nothing from slot 3", 20, WARN, 700)
    svg.text(
        x0 + 30,
        y + 64,
        "N1 picked nothing from slot 3: the vacuum reads 0.31, not a part.",
        16,
        TEXT,
        400,
    )
    svg.text(x0 + 30, y + 88, "Check the pocket and the pick height, then resume.", 16, TEXT, 400)
    bar(svg)
    svg.button(GAP, H - BAR + 12, 660, BAR - 28, "Resume", "accent", "carry on from R7")
    svg.button(692, H - BAR + 12, W - GAP - 692, BAR - 28, "Stop", "danger")
    svg.save("panel-paused.svg")


def tip_change() -> None:
    svg = Svg(
        "Control panel, asking for a tip change: the head is at the front, and the "
        "operator says which tips they fitted"
    )
    top_bar(
        svg,
        [
            ("Connected", ACCENT, ACCENT_SOFT),
            ("Homed", ACCENT, ACCENT_SOFT),
            ("Panel has control", ACCENT, ACCENT_SOFT),
        ],
        "Waiting for you",
        WARN,
    )
    position(svg, 216.50, 44.45, 0.0, 0.0, 0.0, "Tips: left N24, right N08")
    x0, top, bottom = LEFT_W + GAP, TOP + GAP, H - BAR - GAP
    w = W - GAP - x0
    svg.rect(x0, top, w, bottom - top, SURFACE, WARN, width=2)
    svg.text(x0 + 24, top + 40, "Change the left nozzle's tip", 24, TEXT, 700)
    svg.text(
        x0 + 24,
        top + 72,
        "The N24 tip is too big for the paper disc. The head is at",
        16,
        MUTED,
        400,
    )
    svg.text(x0 + 24, top + 94, "the front: fit a smaller tip, then say which one.", 16, MUTED, 400)
    svg.text(x0 + 24, top + 140, "LEFT NOZZLE  ·  now N24", 13, MUTED, 600)
    tips = ["N045", "N08", "N14"]
    for i, tip in enumerate(tips):
        bx = x0 + 24 + i * 150
        chosen = tip == "N045"
        svg.rect(
            bx,
            top + 156,
            136,
            72,
            ACCENT_SOFT if chosen else RAISED,
            ACCENT if chosen else BORDER_STRONG,
            r=12,
            width=2,
        )
        svg.text(bx + 68, top + 194, tip, 24, TEXT, 700, "middle")
        size = {"N045": "0.45 mm", "N08": "0.8 mm", "N14": "1.4 mm"}[tip]
        svg.text(bx + 68, top + 216, size, 13, MUTED, 500, "middle")
    svg.text(x0 + 24, top + 270, "RIGHT NOZZLE  ·  N08, keep it", 13, MUTED, 600)
    svg.text(x0 + 24, top + 300, "The N08 is already fitted, and fits the disc.", 16, MUTED, 400)
    bar(svg)
    svg.button(GAP, H - BAR + 12, 660, BAR - 28, "Fitted", "accent", "the left nozzle has N045")
    svg.button(692, H - BAR + 12, W - GAP - 692, BAR - 28, "Stop", "danger")
    svg.save("panel-tip-change.svg")


if __name__ == "__main__":
    ready()
    running()
    paused()
    tip_change()
