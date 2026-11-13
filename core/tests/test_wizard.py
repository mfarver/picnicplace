"""The setup wizard: where each page stands, the machine's stage, and the limits the
machine service holds it to until it is calibrated."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.calibration import CalibrationStore
from picnicplace.config import Config
from picnicplace.machine.service import MachineService
from picnicplace.wizard import LIMITED_SPEED
from tests.conftest import Session, connect, fast_config, operation_done, receive_until
from tests.test_placement import set_up

BRING_UP = ("probe", "camera", "slow", "home", "jog", "nozzles", "outputs", "stop", "keep_outs")


def limited_config(data_dir: Path, **machine: object) -> Config:
    return fast_config(
        machine={"sim_calibration_limits": True, **machine},
        data={"dir": str(data_dir)},
        presence={"heartbeat_timeout_s": 300.0},
    )


def wizard(client: TestClient) -> dict[str, Any]:
    r = client.get("/api/v1/wizard")
    assert r.status_code == 200, r.json()
    return r.json()


def pages(client: TestClient) -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in wizard(client)["pages"]}


def machine(client: TestClient) -> MachineService:
    return client.app.state.ctx.machine  # type: ignore[attr-defined,no-any-return]


def finish_bring_up(s: Session) -> None:
    steps = {step: {"status": "done"} for step in BRING_UP}
    r = s.put("/api/v1/bring-up", {"steps": steps, "points": []})
    assert r.status_code == 200, r.json()


def measure_everything(client: TestClient) -> None:
    """Stand-ins for what the wizard's measuring pages record, without the minutes it
    takes to measure them in the simulator (their own tests do that)."""
    store: CalibrationStore = machine(client).calibration
    for line in ("homing_fiducial", "top_camera_scale", "bottom_camera", "nozzle_offsets"):
        store.record(line, f"{line} measured.")
    store.record("vacuum", "Vacuum levels measured.")


def test_a_new_machine_is_limited(tmp_path: Path) -> None:
    """Nothing done: picks and runs are refused, saying what is still to do; teaching
    feeders waits for homing and the top camera; moves are capped at a quarter of full
    speed. Homing and the wizard's own steps go ahead."""
    config = limited_config(tmp_path, profile="lumenpnp-v4.1")
    with TestClient(create_app(config)) as client, connect(client) as s:
        state = wizard(client)
        assert state["stage"] == "limited" and state["enforced"]
        assert state["next_page"] == "connect"
        assert [p["number"] for p in state["pages"]] == list(range(1, 17))
        tips = pages(client)["nozzle_tips"]
        assert tips["status"] == "todo" and "Say which tip is on Left nozzle" in tips["detail"]
        assert pages(client)["nozzle_runout"]["status"] == "not_built"

        set_up(client, s, calibrate=False)
        assert s.state()["speed_scale"] == LIMITED_SPEED

        r = s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N1"})
        assert r.status_code == 409, r.json()
        body = r.json()
        assert body["code"] == "calibration_required"
        assert "picks and placements wait until the machine is calibrated" in body["detail"]
        assert "continue the setup wizard (next: Connect, then " in body["detail"]

        limit = machine(client).limits
        assert limit is not None
        teach = limit("rail_calibrate")
        assert teach.refused and "homing and the top camera" in teach.refused
        assert limit("job_run").refused
        assert limit("visual_home").refused is None
        assert limit("discard").refused is None


def test_finishing_the_wizard_lifts_the_limits(tmp_path: Path) -> None:
    """Each page done in turn: the stage moves to calibrated, picks are allowed and moves
    run at full speed. A crash then puts it out of date: runs are paused, and a single
    pick goes ahead with a warning naming what is out of date. Confirmed pages are kept
    across a restart."""
    config = limited_config(tmp_path)
    with TestClient(create_app(config)) as client, connect(client) as s:
        s.home()
        finish_bring_up(s)
        assert wizard(client)["next_page"] == "visual_homing"

        store: CalibrationStore = machine(client).calibration
        store.record("homing_fiducial", "Found the datum board's center dot.")
        store.record("top_camera_scale", "0.0107 mm/pixel.")
        # Teaching feeders opens once homing and the top camera are done.
        limit = machine(client).limits
        assert limit is not None and limit("rail_calibrate").refused is None
        assert limit("pick").refused

        measure_everything(client)
        assert wizard(client)["next_page"] == "discard_park"
        # Strips and trays are optional; skipping them is kept. Required pages cannot be.
        r = s.put("/api/v1/wizard/pages/discard_park", {"status": "skipped"})
        assert r.status_code == 422 and "cannot be skipped" in r.json()["detail"]
        r = s.put("/api/v1/wizard/pages/strips_trays", {"status": "skipped"})
        assert r.status_code == 200, r.json()
        assert wizard(client)["stage"] == "limited"

        r = s.put("/api/v1/wizard/pages/discard_park", {"status": "done"})
        assert r.status_code == 200, r.json()
        state = r.json()
        assert state["stage"] == "calibrated" and state["next_page"] is None
        assert pages(client)["done"]["status"] == "done"
        machine(client)._limited_cache = (float("-inf"), False)  # pyright: ignore[reportPrivateUsage]
        assert s.state()["speed_scale"] == 1.0
        assert limit("pick").refused is None and limit("pick").warning is None

        # A crash knocks the nozzle offsets out of date.
        store.mark_stale(("nozzle_offsets", "vacuum"), "a crash (Z lost steps)")
        state = wizard(client)
        assert state["stage"] == "out_of_date" and state["next_page"] == "nozzle_offsets"
        assert pages(client)["nozzle_offsets"]["stale"] == "a crash (Z lost steps)"
        run = limit("job_run")
        assert run.refused and "out of date (nozzle offsets, heights and vacuum)" in run.refused
        pick = limit("pick")
        assert pick.refused is None and pick.warning
        # Measuring again goes ahead without a warning.
        assert (
            limit("nozzle_offsets") == limit("visual_home") and not limit("nozzle_offsets").warning
        )

    # Only the client holding control confirms pages; confirmed pages are kept.
    with TestClient(create_app(config)) as client:
        assert pages(client)["discard_park"]["status"] == "done"
        assert pages(client)["strips_trays"]["status"] == "skipped"
        assert [p["id"] for p in wizard(client)["pages"] if p["status"] == "todo"] == []
        with connect(client) as s, connect(client, "other") as other:
            assert s.put("/api/v1/wizard/pages/discard_park", {"status": "todo"}).status_code == 200
            assert pages(client)["discard_park"]["status"] == "todo"
            r = other.put("/api/v1/wizard/pages/discard_park", {"status": "done"})
            assert r.status_code == 409, r.json()


def test_a_single_pick_warns_while_out_of_date(tmp_path: Path) -> None:
    """Out of date after a crash, a pick still runs, and its result begins with a
    warning naming what is out of date."""
    with TestClient(create_app(limited_config(tmp_path))) as client, connect(client) as s:
        s.home()
        finish_bring_up(s)
        measure_everything(client)
        assert s.put("/api/v1/wizard/pages/discard_park", {"status": "done"}).status_code == 200
        machine(client).calibration.mark_stale(("nozzle_offsets",), "a crash (Z lost steps)")

        async def work() -> str:
            return "Picked."

        assert client.portal is not None
        op = client.portal.call(machine(client).start_task, "pick", work)
        done = receive_until(s.ws, operation_done(op.id))["operation"]
        assert done["status"] == "succeeded", done
        assert done["message"] == (
            "Warning: calibration is out of date (nozzle offsets, heights and vacuum). Picked."
        )


def test_the_simulator_is_not_limited_unless_asked(tmp_path: Path) -> None:
    config = fast_config(data={"dir": str(tmp_path)})
    with TestClient(create_app(config)) as client, connect(client) as s:
        state = wizard(client)
        assert state["stage"] == "limited" and not state["enforced"]
        assert machine(client).limits is None
        s.home()
        assert s.state()["speed_scale"] == pytest.approx(config.machine.speed_scale)
