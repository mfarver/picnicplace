"""The calibration checklist: what it says before and after measuring, what a crash makes
out of date, and what it stops in a run."""

from pathlib import Path

from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.calibration import (
    CalibrationStore,
    build_checklist,
    run_issues,
)
from tests.conftest import connect, fast_config
from tests.test_vision import home, visual_config


def lines(client: TestClient) -> dict[str, dict[str, object]]:
    r = client.get("/api/v1/calibration")
    assert r.status_code == 200, r.json()
    return {line["id"]: line for line in r.json()["lines"]}


def test_measuring_fills_in_the_checklist(tmp_path: Path) -> None:
    config = visual_config(tmp_path, sim_home_error=(0.3, -0.2))
    with TestClient(create_app(config)) as client, connect(client) as s:
        before = lines(client)
        assert list(before) == [
            "machine_frame",
            "top_camera_orientation",
            "homing_fiducial",
            "top_camera_scale",
            "bottom_camera",
            "nozzle_offsets",
            "nozzle_runout",
            "heights",
            "vacuum",
            "feeder_rails",
            "strips_and_trays",
            "boards",
        ]
        assert [line["number"] for line in before.values()] == list(range(1, 13))
        assert before["machine_frame"]["status"] == "needed"
        assert before["machine_frame"]["action"] == "home"
        assert before["homing_fiducial"]["status"] == "needed"
        assert before["top_camera_scale"]["status"] == "assumed"
        assert before["boards"]["status"] == "every_run"

        assert home(s)["status"] == "succeeded"
        assert s.wait(s.post("/api/v1/machine/camera-scale"))["status"] == "succeeded"
        after = lines(client)
        assert after["machine_frame"]["status"] == "done"
        assert after["machine_frame"]["measured_at"] is not None
        fiducial = after["homing_fiducial"]
        assert fiducial["status"] == "done" and "0.36" in str(fiducial["detail"])
        assert fiducial["warning"] is None
        scale = after["top_camera_scale"]
        assert scale["status"] == "done" and scale["warning"] is None
        assert "mm/pixel" in str(scale["detail"])

    # Kept across restarts, in the data directory.
    assert (config.data.calibration_path).is_file()
    with TestClient(create_app(config)) as client:
        again = lines(client)
        assert again["machine_frame"]["status"] == "needed"  # homing is per session
        assert again["homing_fiducial"]["status"] == "done"
        assert again["top_camera_scale"]["status"] == "done"


def test_a_crash_makes_measurements_out_of_date(tmp_path: Path) -> None:
    store = CalibrationStore(tmp_path / "calibration.json")
    store.record("homing_fiducial", "found it", {"off": 0.2})
    store.record("top_camera_scale", "measured", {"x": 0.05, "y": 0.05, "profile": 0.05})
    store.mark_stale(("homing_fiducial", "top_camera_scale", "nozzle_offsets"), "Z lost steps")
    record = store.get()
    assert record.lines["homing_fiducial"].stale == "Z lost steps"
    assert "nozzle_offsets" not in record.lines  # never measured: nothing to mark

    config = fast_config(machine={"profile": "lumenpnp-v4.1"})
    profile = config.machine.machine_profile
    checklist = build_checklist(profile, True, record, None)
    assert checklist.line("homing_fiducial").status == "stale"
    issues = run_issues(checklist, aligns=True)
    assert [severity for severity, _ in issues] == ["warning", "warning"]
    assert "line 3, homing fiducial, is out of date (Z lost steps)" in issues[0][1]

    # Measuring again puts it right.
    store.record("homing_fiducial", "found it again", {"off": 0.1})
    checklist = build_checklist(profile, True, store.get(), None)
    assert checklist.line("homing_fiducial").status == "done"


def test_values_worth_a_look_are_flagged(tmp_path: Path) -> None:
    store = CalibrationStore(None)
    store.record("homing_fiducial", "found it", {"off": 1.6})
    store.record("top_camera_scale", "measured", {"x": 0.055, "y": 0.055, "profile": 0.05})
    profile = fast_config(machine={"profile": "lumenpnp-v4.1"}).machine.machine_profile
    checklist = build_checklist(profile, True, store.get(), None)
    assert "1.60 mm off" in (checklist.line("homing_fiducial").warning or "")
    assert "10% from" in (checklist.line("top_camera_scale").warning or "")


def test_a_run_needs_the_bottom_camera_focused() -> None:
    profile = fast_config(machine={"profile": "lumenpnp-v4.1"}).machine.machine_profile
    for camera in profile.cameras:
        if camera.looking == "up":
            camera.focus_height = None
    checklist = build_checklist(profile, True, CalibrationStore(None).get(), None)
    line = checklist.line("bottom_camera")
    assert line.status == "needed" and "focus_height" in line.detail
    [(severity, message)] = run_issues(checklist, aligns=True)
    assert severity == "error" and message.startswith("calibration line 5, bottom camera")
    assert run_issues(checklist, aligns=False) == []  # nothing to align: no matter
