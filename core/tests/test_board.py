"""Reading KiCad boards: S-expressions, placements, footprints, and BOM part numbers."""

import math
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from picnicplace.board.kicad import read_bom, read_kicad_pcb
from picnicplace.board.models import normalize_angle, placed_frame
from picnicplace.board.sexpr import SexprError, parse

DATA = Path(__file__).parent / "data" / "kicad"
DEMO = DATA / "picnic-demo.kicad_pcb"
ORIGIN = (100.0, 80.0)  # the demo board's top-left corner in KiCad (make_demo_board.py)
SIZE = (60.0, 40.0)


def test_sexpr() -> None:
    root = parse('(a 1 "two words" (b x) (b "q\\"uote") (c (d 3.5)))')
    assert root.name == "a" and root.values == ["1", "two words"]
    assert [b.values for b in root.all("b")] == [["x"], ['q"uote']]
    c = root.first("c")
    assert c is not None and c.floats("d") == [3.5]
    assert root.value("b") == "x" and root.value("missing") is None
    for bad in ("(a (b)", "(a))", "x", "", "(a) (b)"):
        with pytest.raises(SexprError):
            parse(bad)


def test_demo_board() -> None:
    result = read_kicad_pcb(DEMO.read_text(), "picnic-demo")
    board = result.board
    assert result.warnings == []
    assert (board.size.x, board.size.y) == SIZE and board.thickness == 1.6
    by_ref = {p.ref: p for p in board.placements}
    assert list(by_ref)[:4] == ["C1", "C2", "C3", "FID1"]  # natural order

    r2 = by_ref["R2"]
    assert (r2.x, r2.y, r2.rotation, r2.side) == (18.0, 30.0, 90.0, "top")
    assert r2.part_id == "EX-R0603-10K" and r2.enabled

    u3 = by_ref["U3"]
    assert (u3.x, u3.y, u3.rotation, u3.side) == (48.0, 20.0, 180.0, "bottom")
    assert placed_frame(u3, board.size.x) == (12.0, 20.0, 0.0)

    assert (by_ref["R3"].enabled, by_ref["R3"].note) == (False, "do not populate")
    assert (by_ref["R4"].part_id, by_ref["R4"].note) == (None, "no part number")
    assert (by_ref["J1"].enabled, by_ref["J1"].note) == (False, "through-hole: fitted by hand")
    fiducials = [p for p in board.placements if p.kind == "fiducial"]
    assert [(f.ref, f.side) for f in fiducials] == [
        ("FID1", "top"),
        ("FID2", "top"),
        ("FID3", "top"),
        ("FID4", "bottom"),
    ]
    assert board.sides() == {"top", "bottom"}

    footprints = {f.id: f for f in result.footprints}
    sot = footprints["SOT-23-6"]
    pad1 = next(p for p in sot.pads if p.name == "1")
    assert (pad1.x, pad1.y) == (-1.1375, 0.95)  # pin 1 top left, as in the library
    assert sot.body is not None and (sot.body.x, sot.body.y) == (1.6, 2.9)
    assert footprints["PinHeader_1x03_P2.54mm_Vertical"].pads == []

    parts = {p.id: p for p in result.parts}
    assert parts["EX-SWITCH-1"].footprint_id == "SOT-23-6"
    assert parts["EX-SWITCH-1"].manufacturer == "Example Parts Co."
    assert "EX-R0603-1K" in parts  # the DNP part is still a part


def test_pads_land_where_kicad_puts_them() -> None:
    """Every pad, placed from the library footprint and the placement as the machine
    will place it, lands on KiCad's own pad position (from its IPC-D-356 export)."""
    truth: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for line in (DATA / "picnic-demo.d356").read_text().splitlines():
        m = re.match(r"^327.{14}\s*(\S+)\s*-(\S+)\s.*?A0\dX([+-]?\d+)Y([+-]?\d+)", line)
        if m:  # units of 0.0001 inch, +Y up
            xy = (int(m.group(3)) * 0.00254, int(m.group(4)) * 0.00254)
            truth.setdefault((m.group(1), m.group(2)), []).append(xy)

    result = read_kicad_pcb(DEMO.read_text())
    width = result.board.size.x
    footprints = {f.id: f for f in result.footprints}
    checked = 0
    for placement in result.board.placements:
        x0, y0, angle = placed_frame(placement, width)
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        for pad in footprints[placement.footprint_id].pads:
            if not pad.name:
                continue  # fiducial pads have no number, and no D-356 line
            x = x0 + c * pad.x - s * pad.y
            y = y0 + s * pad.x + c * pad.y
            if placement.side == "bottom":
                x = width - x  # back to the top view
            kicad = (x + ORIGIN[0], y - SIZE[1] - ORIGIN[1])
            near = [
                math.hypot(kicad[0] - a, kicad[1] - b) for a, b in truth[placement.ref, pad.name]
            ]
            assert min(near) < 0.005, (placement.ref, pad.name, kicad)
            checked += 1
    assert checked == 34  # 4 resistors, 3 capacitors, 2 SOT-23-6, 1 SOIC-8


def test_bom_overrides_the_board() -> None:
    bom = read_bom((DATA / "picnic-demo-bom.csv").read_text())
    assert bom["R1"] == bom["R2"] == "EX-R0603-10K-B" and bom["C3"] == "EX-C0603-100N"
    result = read_kicad_pcb(DEMO.read_text(), bom=bom)
    by_ref = {p.ref: p for p in result.board.placements}
    assert by_ref["R2"].part_id == "EX-R0603-10K-B"
    assert (by_ref["R4"].part_id, by_ref["R4"].enabled) == ("EX-R0603-4K7", True)
    assert by_ref["U1"].part_id == "EX-SWITCH-1"  # not in the BOM: from the board
    with pytest.raises(ValueError, match="needs a reference column"):
        read_bom("Item,Qty\n1,2\n")


def test_normalize_angle() -> None:
    assert [normalize_angle(a) for a in (0, 180, -180, 270, -90, 540, 359.5)] == [
        0,
        180,
        180,
        -90,
        -90,
        180,
        -0.5,
    ]


def test_import_endpoint(client: TestClient) -> None:
    body = {
        "kicad_pcb": DEMO.read_text(),
        "name": "picnic-demo",
        "bom_csv": (DATA / "picnic-demo-bom.csv").read_text(),
        "add_to_library": True,
    }
    r = client.post("/api/v1/boards/import/kicad", json=body)
    assert r.status_code == 200, r.json()
    result = r.json()
    assert result["board"]["name"] == "picnic-demo"
    assert len(result["board"]["placements"]) == 15
    assert "part:EX-SWITCH-1" in result["library_report"]["added"]
    assert client.get("/api/v1/library/footprints/SOT-23-6").status_code == 200

    # Importing again changes nothing in the library.
    again = client.post("/api/v1/boards/import/kicad", json=body).json()["library_report"]
    assert again["added"] == [] and again["conflicts"] == []

    bad = client.post("/api/v1/boards/import/kicad", json={"kicad_pcb": "(kicad_sch)"})
    assert bad.status_code == 400
