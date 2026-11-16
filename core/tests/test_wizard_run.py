"""Running the setup wizard's stops: one Start each, the machine doing every step it can on
its own, in the simulator set up like the first real LumenPnP it ran on."""

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from tests.conftest import Session, connect, fast_config

FIDUCIAL = (217.0, 106.0)
"""The LumenPnP datum board's center dot."""


def run(s: Session, stop: int) -> dict[str, Any]:
    return s.wait(s.post("/api/v1/wizard/run", {"stop": stop}))


def wizard(client: TestClient) -> dict[str, Any]:
    return client.get("/api/v1/wizard").json()


def pages(client: TestClient) -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in wizard(client)["pages"]}


def test_three_stops_calibrate_the_machine(tmp_path: Path) -> None:
    """Not homed, with an N045 on the left nozzle and an N08 on the right (none said; a
    LumenPnP comes with one tip of each size), a bottom camera turned a quarter turn that
    sees 3.7 times the profile's view, and a paper disc lying on the datum board: stop 1
    homes, homes visually, measures both cameras and names the N08 (the N045's end is too
    few pixels across to name, so the owner says it); stop 2 finds the disc
    and measures each nozzle; confirming the discard and park spots finishes it."""
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_calibration_limits": True,
            "sim_home_error": (0.3, -0.2),
            "sim_tips": {"N1": "N045", "N2": "N08"},
            "sim_bottom_camera_scale": 3.7,
            "sim_bottom_camera_turn": 90.0,
            "sim_calibration_disc": (FIDUCIAL[0] + 14.0, FIDUCIAL[1] + 6.0),
        },
        data={"dir": str(tmp_path)},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with (
        TestClient(create_app(config, pinned=frozenset({"machine.profile"}))) as client,
        connect(client) as s,
    ):
        assert wizard(client)["next_stop"] == 1
        # The disc stop needs the machine homed, which the first stop does.
        r = s.post("/api/v1/wizard/run", {"stop": 2})
        assert r.status_code == 422 and "home the machine first" in r.json()["detail"]

        op = run(s, 1)
        assert op["status"] == "succeeded", (op, wizard(client)["run"])
        steps = {step["id"]: step for step in wizard(client)["run"]["steps"]}
        # Homing homed visually (the profile asks for it), so it is not done twice.
        assert "Visual homing found" in str(steps["home"]["message"]), steps
        assert steps["visual_home"]["message"] == "done when homing", steps
        assert [steps[i]["status"] for i in ("home", "visual_home", "camera_scale")] == [
            "done",
            "skipped",
            "done",
        ], steps
        assert steps["bottom_camera"]["status"] == "done", steps
        said = str(steps["tips"]["message"])
        # Not named wrong: said to be like none of the tips, or too like an N08 to tell.
        assert "Left nozzle: its end looked" in said and "say which it is" in said, said
        assert "Right nozzle: N08" in said, said
        nozzles = {n["id"]: n for n in client.get("/api/v1/machine/description").json()["nozzles"]}
        assert (nozzles["N1"]["tip"], nozzles["N2"]["tip"]) == (None, "N08")
        assert s.put("/api/v1/machine/nozzles/N1/tip", {"tip_id": "N045"}).status_code == 200
        status = {p["id"]: p["status"] for p in wizard(client)["pages"]}
        for page in ("first_motion", "bed", "visual_homing", "top_camera_scale"):
            assert status[page] == "done", (page, status)
        assert status["bottom_camera"] == status["nozzle_tips"] == "done", status
        assert wizard(client)["next_stop"] in (1, 2), wizard(client)["next_stop"]

        # Start again skips what is done: homing (homed), visual homing (done since),
        # the cameras and the tips; Run again (redo) measures them again.
        op = run(s, 1)
        assert op["status"] == "succeeded", (op, wizard(client)["run"])
        steps = {step["id"]: step["status"] for step in wizard(client)["run"]["steps"]}
        for step in ("home", "visual_home", "camera_scale", "bottom_camera", "tips"):
            assert steps[step] == "skipped", steps
        op = s.wait(s.post("/api/v1/wizard/run", {"stop": 1, "redo": True}))
        assert op["status"] == "succeeded", (op, wizard(client)["run"])
        steps = {step["id"]: step["status"] for step in wizard(client)["run"]["steps"]}
        assert steps["home"] == "skipped", steps
        for step in ("visual_home", "camera_scale", "bottom_camera", "tips"):
            assert steps[step] == "done", steps

        op = run(s, 2)
        assert op["status"] == "succeeded", (op, wizard(client)["run"])
        assert pages(client)["nozzle_offsets"]["status"] == "done"

        r = s.put("/api/v1/wizard/pages/discard_park", {"status": "done"})
        assert r.status_code == 200, r.json()
        state = wizard(client)
        left = [p["id"] for p in state["pages"] if p["status"] == "todo" and not p["optional"]]
        assert left == [] and state["stage"] == "calibrated", left
        # The simulated feeders are not at the rail's ends: the rails were left for later,
        # which does not hold calibration back.
        assert state["run"] is not None
        feeders = pages(client)["feeders"]
        assert feeders["optional"] and feeders["status"] == "todo"


@pytest.mark.parametrize("stop", [1, 2])
def test_only_the_client_in_control_runs_a_stop(stop: int) -> None:
    config = fast_config(machine={"profile": "lumenpnp-v4.1"})
    with TestClient(create_app(config)) as client, connect(client) as s:
        s.home()
        with connect(client, "other") as other:
            assert other.post("/api/v1/wizard/run", {"stop": stop}).status_code == 409


def test_pause_holds_a_run_and_stop_ends_it() -> None:
    """Pause (there is no software emergency stop in the Studio: Marlin would halt until
    power-cycled) holds the run before its next move, refuses anything new, and Resume
    carries on; Stop ends the run, and the machine stays homed and running."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_calibration_limits": True},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        s.home()
        started = s.post("/api/v1/wizard/run", {"stop": 1})
        assert started.status_code == 202, started.json()
        op_url = f"/api/v1/operations/{started.json()['id']}"

        def machine() -> dict[str, Any]:
            return client.get("/api/v1/machine").json()

        def where() -> tuple[float, float]:
            position = machine()["position"]
            return position["X"], position["Y"]

        with connect(client, "other") as other:
            # Anyone may pause, as anyone could press the emergency stop.
            assert other.post("/api/v1/machine/pause").status_code == 200
            assert machine()["paused"]
            # Only the client in control resumes.
            assert other.post("/api/v1/machine/resume").status_code in (403, 409)
        time.sleep(0.3)  # the move under way finishes
        held = where()
        time.sleep(0.5)
        assert where() == held and client.get(op_url).json()["status"] == "running"
        r = s.post("/api/v1/machine/jog", {"axis": "X", "distance": 1.0})
        assert r.status_code == 409 and "paused" in r.json()["detail"], r.json()

        assert s.post("/api/v1/machine/resume").status_code == 200
        assert not machine()["paused"]
        s.post("/api/v1/machine/pause")
        assert s.post("/api/v1/machine/stop").status_code == 200
        op = s.wait(started)
        assert op["status"] == "failed" and "stopped" in op["error"], op
        state = machine()
        assert state["homed"] and not state["estop"] and not state["busy"], state
        assert not state["paused"], state
        steps = wizard(client)["run"]["steps"]
        assert "stopped" in [step["status"] for step in steps], steps
        # Nothing runs now: Stop does nothing, and the machine moves again.
        assert s.post("/api/v1/machine/stop").status_code == 200
        jog = s.wait(s.post("/api/v1/machine/jog", {"axis": "X", "distance": 1.0}))
        assert jog["status"] == "succeeded"
