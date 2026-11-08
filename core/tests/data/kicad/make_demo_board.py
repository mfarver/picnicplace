# pyright: reportMissingImports=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Regenerate picnic-demo.kicad_pcb, a made-up board for the KiCad import tests.

Run with KiCad's bundled Python (it needs the pcbnew module and KiCad's footprint
libraries), then regenerate the IPC-D-356 file the tests check pad positions against:

    $KICAD_PYTHON make_demo_board.py
    kicad-cli pcb export ipcd356 -o picnic-demo.d356 picnic-demo.kicad_pcb

The part numbers are invented (EX-...). The board covers what the importer must get
right: both sides, every quarter turn and an odd angle, a through-hole part, a
do-not-populate part, a part with no part number, and fiducials on both sides.
"""

import os
import sys

import pcbnew

LIBS = os.environ.get(
    "KICAD_FOOTPRINTS", "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"
)
HERE = os.path.dirname(os.path.abspath(__file__))
MM = pcbnew.FromMM

# ref, library, footprint, x, y, angle, side, value, part number, flags
PARTS = [
    ("R1", "Resistor_SMD", "R_0603_1608Metric", 12, 10, 0, "top", "10k", "EX-R0603-10K", ""),
    ("R2", "Resistor_SMD", "R_0603_1608Metric", 18, 10, 90, "top", "10k", "EX-R0603-10K", ""),
    ("R3", "Resistor_SMD", "R_0603_1608Metric", 24, 10, 0, "top", "1k", "EX-R0603-1K", "dnp"),
    ("R4", "Resistor_SMD", "R_0603_1608Metric", 30, 10, -90, "top", "4.7k", "", ""),
    ("C1", "Capacitor_SMD", "C_0805_2012Metric", 12, 18, 180, "top", "1uF", "EX-C0805-1U", ""),
    ("C2", "Capacitor_SMD", "C_0603_1608Metric", 40, 30, 0, "bottom", "100nF", "EX-C0603-100N", ""),
    (
        "C3",
        "Capacitor_SMD",
        "C_0603_1608Metric",
        44,
        30,
        90,
        "bottom",
        "100nF",
        "EX-C0603-100N",
        "",
    ),
    ("U1", "Package_TO_SOT_SMD", "SOT-23-6", 20, 22, -90, "top", "EX-SWITCH", "EX-SWITCH-1", ""),
    ("U2", "Package_SO", "SOIC-8_3.9x4.9mm_P1.27mm", 32, 22, 45, "top", "EX-MCU", "EX-MCU-8", ""),
    ("U3", "Package_TO_SOT_SMD", "SOT-23-6", 48, 20, 180, "bottom", "EX-SWITCH", "EX-SWITCH-1", ""),
    (
        "J1",
        "Connector_PinHeader_2.54mm",
        "PinHeader_1x03_P2.54mm_Vertical",
        52,
        8,
        0,
        "top",
        "Header",
        "EX-HDR-3",
        "",
    ),
    ("FID1", "Fiducial", "Fiducial_1mm_Mask2mm", 4, 4, 0, "top", "Fiducial", "", ""),
    ("FID2", "Fiducial", "Fiducial_1mm_Mask2mm", 56, 36, 0, "top", "Fiducial", "", ""),
    ("FID3", "Fiducial", "Fiducial_1mm_Mask2mm", 4, 36, 0, "top", "Fiducial", "", ""),
    ("FID4", "Fiducial", "Fiducial_1mm_Mask2mm", 56, 4, 0, "bottom", "Fiducial", "", ""),
]
ORIGIN = (100.0, 80.0)  # where the board's top-left corner sits on KiCad's page
SIZE = (60.0, 40.0)


def main() -> None:
    board = pcbnew.BOARD()
    ox, oy = ORIGIN
    corners = [(0, 0), (SIZE[0], 0), (SIZE[0], SIZE[1]), (0, SIZE[1])]
    for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1], strict=True):
        line = pcbnew.PCB_SHAPE(board)
        line.SetShape(pcbnew.SHAPE_T_SEGMENT)
        line.SetStart(pcbnew.VECTOR2I(MM(ox + x1), MM(oy + y1)))
        line.SetEnd(pcbnew.VECTOR2I(MM(ox + x2), MM(oy + y2)))
        line.SetLayer(pcbnew.Edge_Cuts)
        line.SetWidth(MM(0.05))
        board.Add(line)
    for ref, lib, name, x, y, angle, side, value, part, flags in PARTS:
        fp = pcbnew.FootprintLoad(os.path.join(LIBS, f"{lib}.pretty"), name)
        if fp is None:
            sys.exit(f"no footprint {lib}:{name}")
        fp.SetFPID(pcbnew.LIB_ID(lib, name))
        fp.SetReference(ref)
        fp.SetValue(value)
        if part:
            fp.SetField("MP", part)
            fp.SetField("MF", "Example Parts Co.")
        board.Add(fp)
        fp.SetPosition(pcbnew.VECTOR2I(MM(ox + x), MM(oy + y)))
        if side == "bottom":
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
        fp.SetOrientationDegrees(angle)
        if "dnp" in flags:
            fp.SetDNP(True)
    board.Save(os.path.join(HERE, "picnic-demo.kicad_pcb"))


if __name__ == "__main__":
    main()

# The panel, made with KiKit (installed into KiCad's Python):
#
#   $KICAD_PYTHON -m kikit.ui panelize \
#       --layout 'grid; rows: 2; cols: 2; space: 4mm; rotation: 0deg; alternation: rows' \
#       --tabs 'fixed; width: 3mm' --cuts 'mousebites; drill: 0.5mm; spacing: 1mm' \
#       --framing 'railstb; width: 6mm; space: 3mm' \
#       --fiducials '3fid; hoffset: 6mm; voffset: 3mm; coppersize: 1mm; opening: 2mm' \
#       --post 'millradius: 1mm' picnic-demo.kicad_pcb picnic-demo-panel.kicad_pcb
