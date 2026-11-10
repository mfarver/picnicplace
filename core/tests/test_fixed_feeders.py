"""Strips of tape and trays of parts held on the bed (fixed feeders).

Parts come out in order, along each row and then row by row; each pick (taken or not)
moves on to the next one, and when none are left the operator refills it. The run tests
use the demo board from ``test_run``, with one part moved from its Photon feeder to a
strip or tray.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.feeders.setup import FixedFeeder
from picnicplace.machine.profile import Point
from tests.conftest import Session, connect, fast_config
from tests.test_run import check_landed, lumen, ok, set_up  # noqa: F401 (fixture)


def tray(**update: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "label": "SOIC tray",
        "kind": "tray",
        "part_id": "EX-MCU-8",
        "first": {"x": 100, "y": 250},
        "step": {"x": 10, "y": 0},
        "columns": 4,
        "rows": 3,
        "row_step": {"x": 0, "y": -12},
        "part_angle": 90,
        "pick_height": -26.0,
    }
    return base | update


def test_positions_go_along_each_row_then_down_the_rows() -> None:
    f = FixedFeeder.model_validate(tray())
    assert f.capacity == 12 and f.remaining == 12
    assert f.position(0) == Point(x=100, y=250)
    assert f.position(3) == Point(x=130, y=250)
    assert f.position(4) == Point(x=100, y=238)
    assert f.position(11) == Point(x=130, y=226)
    assert f.corners() == [f.position(i) for i in (0, 3, 8, 11)]


def test_add_teach_refill_and_remove(lumen: TestClient) -> None:  # noqa: F811
    with connect(lumen) as s:
        r = s.post("/api/v1/feeders/fixed", tray(step={"x": 0, "y": 0}))
        assert r.status_code == 201, r.json()
        assert r.json()["id"] == "T1" and r.json()["next"] == {"x": 100, "y": 250}
        assert s.post("/api/v1/feeders/fixed", tray(label="")).json()["id"] == "T2"

        # Teach by where the parts are: the first, the end of the first row, the last.
        teach = "/api/v1/feeders/fixed/T1/teach"
        s.home()
        r = s.put(teach, {"which": "first", "at": {"x": 101, "y": 251}})
        assert r.status_code == 200, r.json()
        r = s.put(teach, {"which": "row_end", "at": {"x": 131.3, "y": 251.3}})
        assert r.json()["feeder"]["step"] == {"x": 10.1, "y": 0.1}
        r = s.put(teach, {"which": "last", "at": {"x": 131.3, "y": 227.3}})
        feeder = r.json()["feeder"]
        assert feeder["row_step"] == pytest.approx({"x": 0, "y": -12})
        assert r.json()["angle"] == 90

        # Refill with some already used.
        r = s.post("/api/v1/feeders/fixed/T1/refill", {"picked": 5})
        assert r.json()["remaining"] == 7
        assert s.post("/api/v1/feeders/fixed/T1/refill", {"picked": 13}).status_code == 422

        state = lumen.get("/api/v1/feeders").json()
        assert [f["id"] for f in state["fixed"]] == ["T1", "T2"]
        assert s.put("/api/v1/feeders/fixed/X9", tray()).status_code == 422
        assert lumen.delete("/api/v1/feeders/fixed/T2").status_code == 200
        assert [f["id"] for f in lumen.get("/api/v1/feeders").json()["fixed"]] == ["T1"]

        # A tray has no sprocket holes to find.
        assert s.post("/api/v1/feeders/fixed/T1/pocket").status_code == 422


def move_to_bed(client: TestClient, s: Session, part_id: str, feeder: dict[str, Any]) -> None:
    """Take a part's Photon feeder off it and hold the part on the bed instead."""
    for slot in client.get("/api/v1/feeders").json()["slots"]:
        a = slot["feeder"] and slot["feeder"]["assignment"]
        if a and a["part_id"] == part_id:
            assert client.delete(f"/api/v1/feeders/assignments/{slot['feeder']['uuid']}")
    r = s.post("/api/v1/feeders/fixed", feeder)
    assert r.status_code == 201, r.json()


def test_a_wide_part_from_a_strip(lumen: TestClient) -> None:  # noqa: F811
    """U2 on 24 mm tape, which no Photon feeder takes, held in a strip on the bed. The
    run finds the strip's next pocket from its sprocket holes."""
    with connect(lumen) as s:
        set_up(lumen, s)
        library = lumen.app.state.ctx.library  # type: ignore[attr-defined]
        mcu = library.find_part("EX-MCU-8")
        wide = mcu.tape.model_copy(update={"width": 24.0, "pitch": 12.0})
        library.put_part(mcu.model_copy(update={"tape": wide}))
        strip = {
            "label": "24 mm holder",
            "kind": "strip",
            "part_id": "EX-MCU-8",
            "first": {"x": 150, "y": 250},
            "step": {"x": -12, "y": 0},
            "columns": 5,
        }
        move_to_bed(lumen, s, "EX-MCU-8", strip)

        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        u2 = next(i for i in plan["items"] if i["ref"] == "U2")
        assert (u2["status"], u2["slot_id"]) == ("pending", "T1")
        assert [i["ref"] for i in plan["items"] if i["status"] == "pending"][-1] == "U2"

        op = ok(s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        assert check_landed(lumen, lumen.get("/api/v1/run").json()) == 4
        t1 = lumen.get("/api/v1/feeders").json()["fixed"][0]
        assert t1["remaining"] == 4 and t1["next"] == {"x": 138, "y": 250}
        # The simulated strip really is 0.15 mm right of and 0.1 mm in front of where it
        # was taught; the tape points right (+X), so the holes are behind (+Y).
        ok(s.wait(s.post("/api/v1/feeders/fixed/T1/pocket")))
        pocket = lumen.get("/api/v1/feeders").json()["fixed"][0]["pocket"]
        assert (pocket["along"], pocket["across"]) == pytest.approx((0.15, -0.1), abs=0.05)


def empty_tray_run(client: TestClient, s: Session) -> dict[str, Any]:
    """Run the demo job with C1's part from a tray that is empty: the run pauses."""
    set_up(client, s)
    move_to_bed(
        client,
        s,
        "EX-C0805-1U",
        tray(part_id="EX-C0805-1U", columns=2, rows=1, part_angle=0, pick_height=None),
    )
    assert s.post("/api/v1/feeders/fixed/T1/refill", {"picked": 2}).json()["remaining"] == 0

    plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
    warnings = [i["message"] for i in plan["issues"] if i["severity"] == "warning"]
    assert (
        "T1 has 0 parts left and this job takes 1: the run will pause for you to refill it"
        in warnings
    )

    op = s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True}))
    assert op["status"] == "failed"
    assert op["error"].startswith("T1 (SOIC tray) is empty after 2 parts: refill it")
    run = client.get("/api/v1/run").json()
    assert run["status"] == "paused" and run["placed"] == 2
    assert run["error"] == op["error"]
    return op


def test_an_empty_tray_pauses_the_run_until_refilled(lumen: TestClient) -> None:  # noqa: F811
    with connect(lumen) as s:
        op = empty_tray_run(lumen, s)
        # Out of the operator's way: no park location is set, so the back of the machine.
        assert op["error"].endswith("The head moved to the back (no park location is set).")
        machine = lumen.get("/api/v1/machine").json()
        profile = lumen.app.state.ctx.machine.profile  # type: ignore[attr-defined]
        y = next(a for a in profile.axes if a.name == "Y")
        assert machine["position"]["Y"] == pytest.approx(y.max)

        assert s.post("/api/v1/feeders/fixed/T1/refill", {}).json()["remaining"] == 2
        op = ok(s.wait(s.post("/api/v1/run/resume")))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        assert check_landed(lumen, lumen.get("/api/v1/run").json()) == 4
        assert lumen.get("/api/v1/feeders").json()["fixed"][0]["remaining"] == 1


def test_an_empty_tray_parks_the_head() -> None:
    park = {
        "id": "park",
        "label": "Park position",
        "kind": "park",
        "position": {"x": 400, "y": 380},
    }
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "locations": [park]},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        op = empty_tray_run(client, s)
        assert op["error"].endswith("The head moved to the park position.")
        position = client.get("/api/v1/machine").json()["position"]
        assert (position["X"], position["Y"]) == pytest.approx((400, 380))
        # Nozzles up, out of the way too.
        profile = client.app.state.ctx.machine.profile  # type: ignore[attr-defined]
        for nozzle in profile.nozzles:
            assert position[nozzle.z_axis] == pytest.approx(0)


def test_recycle_into_a_tray(lumen: TestClient) -> None:  # noqa: F811
    """A part put back in a tray is the next one picked: the count goes back one."""
    with connect(lumen) as s:
        set_up(lumen, s)
        move_to_bed(lumen, s, "EX-MCU-8", tray())
        ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "T1", "nozzle_id": "N1"})))
        assert lumen.get("/api/v1/feeders").json()["fixed"][0]["remaining"] == 11
        op = ok(s.wait(s.post("/api/v1/placement/recycle", {"nozzle_id": "N1"})))
        assert op["message"].endswith("T1 (SOIC tray) picks it again next.")
        assert lumen.get("/api/v1/feeders").json()["fixed"][0]["remaining"] == 12
        op = ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "T1", "nozzle_id": "N2"})))
        assert "number 1 from T1" in op["message"]
