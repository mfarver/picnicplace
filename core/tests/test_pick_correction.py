"""Keeping picks centered: with the top camera before each pick, or by learning each
feeder's true pick place from what the bottom camera sees of its picks.

The simulator puts each uncalibrated slot's pockets up to half a millimeter from where
the profile says (slot 3: -0.4, +0.2 mm), and strips and trays 0.15, -0.1 mm from where
their setup says, so there is something to learn.
"""

import math
from typing import Any

import pytest
from fastapi.testclient import TestClient

from picnicplace.feeders.service import SIM_FIXED_ERROR, sim_slot_error
from tests.conftest import Session, connect
from tests.test_fixed_feeders import move_to_bed, tray
from tests.test_placement import lumen, ok, set_up  # noqa: F401 (fixture)
from tests.test_run import check_landed
from tests.test_run import lumen as lumen_run  # noqa: F401 (fixture)
from tests.test_run import set_up as set_up_job


def load(client: TestClient, nozzle: str) -> dict[str, Any]:
    return next(x for x in client.get("/api/v1/placement").json()["loads"] if x["nozzle"] == nozzle)


def slot_state(client: TestClient, slot_id: str) -> dict[str, Any]:
    slots = client.get("/api/v1/feeders").json()["slots"]
    return next(x for x in slots if x["slot"]["id"] == slot_id)


def pick_and_align(s: Session, source: str, nozzle: str, how: str, feed: bool = True) -> str:
    pick = {"slot_id": source, "nozzle_id": nozzle, "feed": feed, "pick_correction": how}
    ok(s.wait(s.post("/api/v1/placement/pick", pick)))
    return str(ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": nozzle})))["message"])


def miss(client: TestClient, nozzle: str) -> float:
    a = load(client, nozzle)["alignment"]
    return math.hypot(a["dx"], a["dy"])


def test_the_bottom_camera_learns_where_a_slots_parts_are(lumen: TestClient) -> None:  # noqa: F811
    error = sim_slot_error(3)
    with connect(lumen) as s:
        set_up(lumen, s, slots=("3",), calibrate=False)
        assert lumen.get("/api/v1/placement").json()["pick_correction"] == "top"
        base = slot_state(lumen, "3")["pick"]

        # Nothing learned yet: the first pick goes where the setup says, and misses.
        message = pick_and_align(s, "3", "N1", "bottom", feed=False)
        assert load(lumen, "N1")["picked_at"] == base
        assert miss(lumen, "N1") == pytest.approx(math.hypot(error.x, error.y), abs=0.05)
        learned = slot_state(lumen, "3")["learned"]
        assert learned is not None and learned["samples"] == 1
        assert learned["feeder"] == slot_state(lumen, "3")["feeder"]["uuid"]
        assert (learned["offset"]["x"], learned["offset"]["y"]) == pytest.approx(
            (error.x, error.y), abs=0.05
        )
        assert "Slot 3's parts lie -0.4" in message

        # The next pick goes where the parts really are.
        pick_and_align(s, "3", "N2", "bottom")
        picked = load(lumen, "N2")["picked_at"]
        assert picked["x"] == pytest.approx(base["x"] + learned["offset"]["x"], abs=1e-6)
        assert picked["y"] == pytest.approx(base["y"] + learned["offset"]["y"], abs=1e-6)
        assert miss(lumen, "N2") < 0.06
        misses = slot_state(lumen, "3")["learned"]["misses"]["bottom"]
        assert misses["count"] == 2 and misses["last"] < 0.06


def test_the_top_camera_finds_the_pocket_and_the_bottom_camera_keeps_score(
    lumen: TestClient,  # noqa: F811
) -> None:
    with connect(lumen) as s:
        set_up(lumen, s, slots=("3",), calibrate=False)
        pick_and_align(s, "3", "N1", "top", feed=False)
        held = load(lumen, "N1")
        assert held["correction"] == "top" and held["picked_at"] != held["base"]
        assert miss(lumen, "N1") < 0.06
        learned = slot_state(lumen, "3")["learned"]
        # Learned all the same, ready for a switch to the bottom camera.
        error = sim_slot_error(3)
        assert (learned["offset"]["x"], learned["offset"]["y"]) == pytest.approx(
            (error.x, error.y), abs=0.06
        )
        assert set(learned["misses"]) == {"top"}

        # With neither, the pick goes where the setup says, and misses again.
        pick_and_align(s, "3", "N2", "off")
        assert load(lumen, "N2")["picked_at"] == load(lumen, "N2")["base"]
        assert miss(lumen, "N2") > 0.3
        assert set(slot_state(lumen, "3")["learned"]["misses"]) == {"top", "off"}


def test_teaching_or_forgetting_starts_learning_again(lumen: TestClient) -> None:  # noqa: F811
    with connect(lumen) as s:
        set_up(lumen, s, slots=("3",), calibrate=False)
        pick_and_align(s, "3", "N1", "bottom", feed=False)
        assert slot_state(lumen, "3")["learned"] is not None
        r = s.delete("/api/v1/feeders/learned/3")
        assert r.status_code == 200, r.json()
        assert slot_state(lumen, "3")["learned"] is None

        pick_and_align(s, "3", "N2", "bottom")
        assert slot_state(lumen, "3")["learned"] is not None
        base = slot_state(lumen, "3")["pick"]
        assert s.put("/api/v1/feeders/slots/3/pick", {"at": base}).status_code == 200
        assert slot_state(lumen, "3")["learned"] is None
        r = s.delete("/api/v1/feeders/learned/99")
        assert r.status_code == 422


def test_a_tray_learns_from_the_bottom_camera(lumen_run: TestClient) -> None:  # noqa: F811
    """Trays have no sprocket holes, so even with the top camera chosen, their picks move
    by what the bottom camera saw."""
    client = lumen_run
    with connect(client) as s:
        set_up_job(client, s)
        move_to_bed(
            client,
            s,
            "EX-C0805-1U",
            tray(part_id="EX-C0805-1U", columns=4, rows=1, part_angle=0, pick_height=None),
        )
        pick_and_align(s, "T1", "N1", "top")
        first = load(client, "N1")
        assert first["correction"] == "bottom" and first["picked_at"] == first["base"]
        learned = client.get("/api/v1/feeders").json()["fixed"][0]["learned"]
        assert (learned["offset"]["x"], learned["offset"]["y"]) == pytest.approx(
            (SIM_FIXED_ERROR.x, SIM_FIXED_ERROR.y), abs=0.05
        )
        pick_and_align(s, "T1", "N2", "top")
        assert load(client, "N2")["picked_at"] != load(client, "N2")["base"]
        assert miss(client, "N2") < 0.06

        # Moving the tray starts again; renaming it does not.
        f = client.get("/api/v1/feeders").json()["fixed"][0]["feeder"]
        assert s.put("/api/v1/feeders/fixed/T1", f | {"label": "0805s"}).status_code == 200
        assert client.get("/api/v1/feeders").json()["fixed"][0]["learned"] is not None
        moved = f | {"first": {"x": f["first"]["x"] + 1, "y": f["first"]["y"]}}
        assert s.put("/api/v1/feeders/fixed/T1", moved).status_code == 200
        assert client.get("/api/v1/feeders").json()["fixed"][0]["learned"] is None


def test_a_run_with_the_bottom_camera_only(lumen_run: TestClient) -> None:  # noqa: F811
    client = lumen_run
    with connect(client) as s:
        set_up_job(client, s)
        body = {"job_id": "demo", "skip_blocked": True, "pick_correction": "bottom"}
        op = ok(s.wait(s.post("/api/v1/run/start", body)))
        run = client.get("/api/v1/run").json()
        assert run["pick_correction"] == "bottom"
        assert check_landed(client, run) >= 4, op
        slots = client.get("/api/v1/feeders").json()["slots"]
        assert all(x["pocket"] is None for x in slots)  # the top camera never looked
        assert any(x["learned"] is not None for x in slots)
