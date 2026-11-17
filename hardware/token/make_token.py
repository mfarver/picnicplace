# pyright: reportMissingImports=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Generate the PicnicPlace calibration token and a panel of them to order.

Run with KiCad's bundled Python (it needs the pcbnew module and KiKit), from anywhere:

    $KICAD_PYTHON hardware/token/make_token.py

It writes two boards: picnicplace-token.kicad_pcb, one token, and from it, with KiKit,
picnicplace-token-panel.kicad_pcb, the panel to order. Every dimension is here, so change
the numbers and run it again rather than editing either board by hand. The design is
described in docs/hardware/calibration-token.md.

The token is a small round board that a nozzle picks up, turns and puts down in stop 2
of the setup wizard (nozzle offsets). Its top is copper under solder mask with a round
opening in the middle: the opening's bare (ENIG) copper is flat for a small tip to seal
on, and bright for the top camera to find. Silkscreen dots on its back hold it a little
off the board, so air gets under it and it lets go when it is lifted.

Design coordinates, in millimeters: the token's center is (0, 0); +Y is up. KiCad's Y
runs down the page, so the script flips Y.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "picnicplace-token"
REVISION = "A"
MM = pcbnew.FromMM
PAGE_ORIGIN = (100.0, 100.0)  # where design (0, 0) sits on KiCad's page

TOKEN_D = 5.0
THICKNESS = 0.8
OPENING_D = 2.5
"""The mask opening in the middle. Stop 2 fits the N045 and N08 (faces 1.0 and 1.5 mm
across), whose faces sit inside it on flat copper, clear of the step at the mask's edge."""
COPPER_D = 3.6
"""The copper, top and back alike so the token stays flat. It stops 0.7 mm in from the
edge, clear of the mouse bites (their holes reach 0.4 mm in) and of the routed edge."""
VENT_DOT_D = 0.4
VENT_PITCH = 1.0
VENT_REACH = 1.9
"""Silkscreen dots on the back, on a square grid, as far as this from the center."""

# The panel: 12 x 12 tokens on a 100 x 100 mm panel, with rails all round.
ROWS = COLS = 12
ROUTE = 2.0
"""The gap between tokens and to the rails: the router bit's diameter."""
RAIL = 7.0
TAB_W = 1.5
BITE_D = 0.5
BITE_PITCH = 0.75
BITE_IN = 0.15
"""How far the mouse bites' holes sit into the token, so the stub a tab leaves when it
breaks off stays within the token's round edge and the token lies flat."""


def at(x: float, y: float) -> pcbnew.VECTOR2I:
    """A design point on KiCad's page."""
    return pcbnew.VECTOR2I(MM(PAGE_ORIGIN[0] + x), MM(PAGE_ORIGIN[1] - y))


def disc(parent: pcbnew.BOARD | pcbnew.FOOTPRINT, layer: int, d: float, filled: bool) -> None:
    shape = pcbnew.PCB_SHAPE(parent)
    shape.SetShape(pcbnew.SHAPE_T_CIRCLE)
    shape.SetCenter(at(0, 0))
    shape.SetEnd(at(d / 2, 0))
    shape.SetLayer(layer)
    shape.SetWidth(MM(0.0 if filled else 0.05))
    shape.SetFilled(filled)
    parent.Add(shape)


def dot(fp: pcbnew.FOOTPRINT, layer: int, x: float, y: float, d: float) -> None:
    shape = pcbnew.PCB_SHAPE(fp)
    shape.SetShape(pcbnew.SHAPE_T_CIRCLE)
    shape.SetCenter(at(x, y))
    shape.SetEnd(at(x + d / 2, y))
    shape.SetLayer(layer)
    shape.SetWidth(MM(0.0))
    shape.SetFilled(True)
    fp.Add(shape)


def token() -> pcbnew.BOARD:
    board = pcbnew.BOARD()
    settings = board.GetDesignSettings()
    settings.SetBoardThickness(MM(THICKNESS))
    settings.SetAuxOrigin(at(0, 0))
    settings.SetGridOrigin(at(0, 0))
    disc(board, pcbnew.Edge_Cuts, TOKEN_D, filled=False)

    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("TOK1")
    fp.SetValue("CalibrationToken_5mm")
    fp.Reference().SetVisible(False)
    fp.Value().SetVisible(False)
    fp.SetAttributes(
        pcbnew.FP_EXCLUDE_FROM_POS_FILES | pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_BOARD_ONLY
    )
    board.Add(fp)
    fp.SetPosition(at(0, 0))

    # The top: one pad of copper, its mask opening drawn in to the middle by a negative
    # margin, so the opening is centered on the copper by construction.
    pad = pcbnew.PAD(fp)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(MM(COPPER_D), MM(COPPER_D)))
    pad.SetLayerSet(pad.SMDMask().RemoveLayer(pcbnew.F_Paste))
    pad.SetLocalSolderMaskMargin(MM(-(COPPER_D - OPENING_D) / 2))
    fp.Add(pad)
    pad.SetPosition(at(0, 0))

    # The back: the same copper under unbroken mask, and the vent dots printed over it.
    dot(fp, pcbnew.B_Cu, 0, 0, COPPER_D)
    n = int(VENT_REACH / VENT_PITCH)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            x, y = i * VENT_PITCH, j * VENT_PITCH
            if (x * x + y * y) ** 0.5 + VENT_DOT_D / 2 <= VENT_REACH:
                dot(fp, pcbnew.B_SilkS, x, y, VENT_DOT_D)
    return board


def panel(source: str, path: str) -> None:
    """The panel, with KiKit: the tokens in a grid, each held by four tabs, one to each
    neighbor or to the rail, broken off at mouse bites."""
    command = [
        sys.executable,
        "-m",
        "kikit.ui",
        "panelize",
        "--layout",
        f"grid; rows: {ROWS}; cols: {COLS}; space: {ROUTE}mm",
        "--tabs",
        f"fixed; width: {TAB_W}mm; hcount: 1; vcount: 1",
        "--cuts",
        f"mousebites; drill: {BITE_D}mm; spacing: {BITE_PITCH}mm; offset: {BITE_IN}mm",
        "--framing",
        f"frame; width: {RAIL}mm; space: {ROUTE}mm; cuts: none",
        "--post",
        f"millradius: {ROUTE / 2}mm",
        "--text",
        "simple; text: PicnicPlace calibration tokens   rev "
        + REVISION
        + "   CERN-OHL-S-2.0; anchor: mt; voffset: "
        + f"{RAIL / 2}mm; width: 1.5mm; height: 1.5mm; thickness: 0.25mm; layer: F.SilkS",
        source,
        path,
    ]
    subprocess.run(command, check=True)


def main() -> None:
    source = os.path.join(HERE, f"{NAME}.kicad_pcb")
    if not pcbnew.SaveBoard(source, token()):
        sys.exit(f"could not save {source}")
    print(source)
    out = os.path.join(HERE, f"{NAME}-panel.kicad_pcb")
    panel(source, out)
    print(out)


if __name__ == "__main__":
    main()
