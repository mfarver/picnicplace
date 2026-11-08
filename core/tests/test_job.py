"""Jobs: panels from KiKit, placing a job on the machine, the checks, and the endpoints."""

import math
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from picnicplace.board.kicad import read_kicad_pcb
from picnicplace.board.panel import Panel, find_copies, to_panel
from picnicplace.job.layout import find_location, layout, machine_placements, overlaps
from picnicplace.job.models import Job, MachineLocation
from picnicplace.library.models import Footprint
from picnicplace.machine.profile import Point, Rect, load_profile

DATA = Path(__file__).parent / "data" / "kicad"
ORIGIN = (100.0, 80.0)  # the demo board's top-left corner in KiCad
LUMEN = load_profile("lumenpnp-v4.1")  # +X points left, +Y to the front


def demo() -> tuple[Job, list[Footprint]]:
    result = read_kicad_pcb((DATA / "picnic-demo.kicad_pcb").read_text(), "picnic-demo")
    return Job.for_board(result.board), result.footprints


def demo_panel() -> Panel:
    board = read_kicad_pcb((DATA / "picnic-demo.kicad_pcb").read_text()).board
    panel_board = read_kicad_pcb((DATA / "picnic-demo-panel.kicad_pcb").read_text()).board
    panel, warnings = find_copies(board, panel_board)
    assert warnings == []
    return panel


def test_panel_copies() -> None:
    panel = demo_panel()
    assert (panel.size.x, panel.size.y) == (124.0, 101.9992)
    assert [(c.x, c.y, c.rotation) for c in panel.copies] == [
        (0.0, 52.9995, 0.0),
        (64.0, 52.9995, 0.0),
        (60.0, 48.9995, 180.0),
        (124.0, 48.9995, 180.0),
    ]
    assert sorted((f.ref, f.side) for f in panel.fiducials) == [
        (f"KiKit_FID_{s}_{n}", side)
        for s, side in (("B", "bottom"), ("T", "top"))
        for n in (1, 2, 3)
    ]


def test_side_up_follows_the_parts() -> None:
    job, _ = demo()
    assert job.side_up == "top"  # parts on both sides: top first
    only_bottom = job.board.model_copy(
        update={"placements": [p for p in job.board.placements if p.side == "bottom"]}
    )
    assert Job.for_board(only_bottom).side_up == "bottom"


def test_machine_coordinates() -> None:
    job, _ = demo()
    at = MachineLocation(x=100, y=50)
    placed = {p.ref: p for p in machine_placements(job, LUMEN, at)}
    # +X left and +Y front: board right and back are machine -X and -Y.
    assert (placed["R1"].x, placed["R1"].y, placed["R1"].rotation) == (88.0, 20.0, 0.0)
    assert "U3" not in placed  # on the bottom, facing down

    turned = {
        p.ref: p for p in machine_placements(job, LUMEN, at.model_copy(update={"rotation": 90}))
    }
    assert (turned["R1"].x, turned["R1"].y) == pytest.approx((130.0, 38.0))
    assert turned["R1"].rotation == 90.0 and turned["R2"].rotation == 180.0

    bottom = job.model_copy(update={"side_up": "bottom"})
    flipped = {p.ref: p for p in machine_placements(bottom, LUMEN, at)}
    # U3 at (48, 20), 180 degrees on the bottom: flipped, it is at (12, 20), 0 degrees.
    assert (flipped["U3"].x, flipped["U3"].y, flipped["U3"].rotation) == (88.0, 30.0, 0.0)
    assert set(flipped) == {"C2", "C3", "U3", "FID4"}


def _kicad_pads() -> dict[tuple[str, str], list[tuple[float, float]]]:
    """KiCad's own pad positions in the board frame, from the IPC-D-356 export."""
    pads: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for line in (DATA / "picnic-demo.d356").read_text().splitlines():
        m = re.match(r"^327.{14}\s*(\S+)\s*-(\S+)\s.*?A0\dX([+-]?\d+)Y([+-]?\d+)", line)
        if m:
            x = int(m.group(3)) * 0.00254 - ORIGIN[0]
            y = int(m.group(4)) * 0.00254 + ORIGIN[1] + 40.0
            pads.setdefault((m.group(1), m.group(2)), []).append((x, y))
    return pads


@pytest.mark.parametrize("side_up", ["top", "bottom"])
@pytest.mark.parametrize("turn", [0.0, 90.0, 210.0])
def test_every_pad_lands_on_the_machine(side_up: str, turn: float) -> None:
    """The whole chain, for every copy in the panel: each part placed at its machine
    position and angle puts every pad where KiCad's design has it, carried to the
    machine by the same panel, flip, and location."""
    job, footprints = demo()
    job = job.model_copy(update={"panel": demo_panel(), "side_up": side_up})
    at = MachineLocation(x=300, y=120, rotation=turn)
    shapes = {f.id: f for f in footprints}
    kicad = _kicad_pads()
    reference = {p.ref: p for p in job.board.placements}
    xs, ys = LUMEN.x_sign, LUMEN.y_sign

    def design_to_machine(copy_index: int, x: float, y: float) -> tuple[float, float]:
        px, py = to_panel(job.panel.copies[copy_index], x, y)
        if side_up == "bottom":
            px = job.panel.size.x - px
        c, s = math.cos(math.radians(turn)), math.sin(math.radians(turn))
        vx, vy = c * px - s * py, s * px + c * py
        return at.x + xs * vx, at.y + ys * vy

    checked = 0
    for placed in machine_placements(job, LUMEN, at):
        if placed.kind != "part" or placed.copy_id is None:
            continue
        index = int(placed.copy_id) - 1
        assert reference[placed.ref].side == side_up
        c, s = math.cos(math.radians(placed.rotation)), math.sin(math.radians(placed.rotation))
        for pad in shapes[placed.footprint_id].pads:
            view = (c * pad.x - s * pad.y, s * pad.x + c * pad.y)
            machine = (placed.x + xs * view[0], placed.y + ys * view[1])
            truth = [design_to_machine(index, *xy) for xy in kicad[placed.ref, pad.name]]
            assert min(math.dist(machine, t) for t in truth) < 0.005, (placed, pad.name)
            checked += 1
    assert checked == (24 if side_up == "top" else 10) * 4  # pads per copy, 4 copies


def test_checks_and_finding_a_place() -> None:
    job, _ = demo()
    location = find_location(job, LUMEN)
    assert location is not None
    placed = job.model_copy(update={"location": location})
    result = layout(placed, LUMEN)
    assert result.ok and result.issues == []
    assert len(result.outline) == 4

    # Over the datum board: refused. The front-left corner is front and left of it,
    # so the board reaches back and right over it.
    datum = next(k for k in LUMEN.keep_outs if k.id == "datum")
    on_datum = MachineLocation(x=datum.area.x_max + 10, y=datum.area.y_max + 5)
    bad = layout(job.model_copy(update={"location": on_datum}), LUMEN)
    assert not bad.ok
    assert any("datum" in i.message for i in bad.issues)

    # Out of reach of a nozzle at the side of the bed.
    area = LUMEN.placement_area
    assert area is not None
    edge = MachineLocation(x=area.x_min + 20, y=60)
    far = layout(job.model_copy(update={"location": edge}), LUMEN)
    assert any("cannot reach" in i.message for i in far.issues)

    assert not layout(job, LUMEN).ok  # no location yet


def test_a_found_place_passes_the_checks() -> None:
    # The panel's rail fiducials are near its edge; rounding the location must not push
    # them out of the camera's reach.
    job, _ = demo()
    for side in ("top", "bottom"):
        panel_job = job.model_copy(update={"panel": demo_panel(), "side_up": side})
        location = find_location(panel_job, LUMEN)
        assert location is not None
        result = layout(panel_job.model_copy(update={"location": location}), LUMEN)
        assert result.ok, result.issues


def test_too_big_to_fit_anywhere() -> None:
    job, _ = demo()
    big = job.model_copy(
        update={
            "panel": job.panel.model_copy(
                update={"size": job.board.size.model_copy(update={"x": 300, "y": 300})}
            )
        }
    )
    assert find_location(big, LUMEN) is None


def test_overlaps() -> None:
    square = [Point(x=0, y=0), Point(x=2, y=0), Point(x=2, y=2), Point(x=0, y=2)]
    assert overlaps(square, Rect(x_min=1, y_min=1, x_max=3, y_max=3))
    assert not overlaps(square, Rect(x_min=2, y_min=0, x_max=3, y_max=1))  # touching
    diamond = [Point(x=0, y=-2), Point(x=2, y=0), Point(x=0, y=2), Point(x=-2, y=0)]
    assert not overlaps(diamond, Rect(x_min=1.1, y_min=1.1, x_max=3, y_max=3))
    assert overlaps(diamond, Rect(x_min=0.9, y_min=0.9, x_max=3, y_max=3))


def test_job_endpoints(client: TestClient) -> None:
    imported = client.post(
        "/api/v1/boards/import/kicad",
        json={
            "kicad_pcb": (DATA / "picnic-demo.kicad_pcb").read_text(),
            "panel_pcb": (DATA / "picnic-demo-panel.kicad_pcb").read_text(),
            "name": "picnic-demo",
        },
    ).json()
    job = imported["job"]
    assert len(job["panel"]["copies"]) == 4 and job["location"] is None

    # The panel's fiducials are near its edges: the found place keeps them in view.
    r = client.post("/api/v1/jobs/find-location", json=job)
    assert r.status_code == 200, r.json()
    job["location"] = r.json()
    checked = client.post("/api/v1/jobs/layout", json=job).json()
    assert (
        checked["ok"] and len(checked["placements"]) == 4 * 11 + 3
    )  # top side, plus the panel's fiducials

    assert client.put("/api/v1/jobs/picnic-demo", json=job).status_code == 200
    [summary] = client.get("/api/v1/jobs").json()
    assert summary["id"] == "picnic-demo" and summary["copies"] == 4 and summary["placed"]
    assert client.get("/api/v1/jobs/picnic-demo").json()["location"] == job["location"]
    assert client.get("/api/v1/jobs/../etc").status_code in (404, 422)
    assert client.delete("/api/v1/jobs/picnic-demo").status_code == 204
    assert client.get("/api/v1/jobs/picnic-demo").status_code == 404

    job["panel"]["size"] = {"x": 900, "y": 900}
    assert client.post("/api/v1/jobs/find-location", json=job).status_code == 409
