"""The calibration checklist: what it says before and after measuring, what a crash makes
out of date, and what it stops in a run."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.calibration import (
    CalibrationStore,
    apply_measurements,
    build_checklist,
    nozzle_key,
    run_issues,
)
from picnicplace.config import Config
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
    store.record("top_camera_scale", "measured", {"x": 0.06, "y": 0.06, "profile": 0.05})
    profile = fast_config(machine={"profile": "lumenpnp-v4.1"}).machine.machine_profile
    checklist = build_checklist(profile, True, store.get(), None)
    assert "1.60 mm off" in (checklist.line("homing_fiducial").warning or "")
    assert "20% from" in (checklist.line("top_camera_scale").warning or "")


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


def test_the_measured_top_camera_scale_is_used(tmp_path: Path) -> None:
    """The simulated top camera really sees 5% more than the profile says. Measuring its
    scale finds that, and vision uses the measured scale from then on."""
    config = visual_config(tmp_path, sim_home_error=(0.3, -0.2), sim_top_camera_scale=1.05)
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        design = client.get("/api/v1/machine/description").json()["cameras"][0]
        op = s.wait(s.post("/api/v1/machine/camera-scale"))
        assert op["status"] == "succeeded", op
        top = client.get("/api/v1/machine/description").json()["cameras"][0]
        assert top["field_of_view"]["x"] == pytest.approx(
            design["field_of_view"]["x"] * 1.05, rel=0.01
        )
        assert top["field_of_view"]["y"] == pytest.approx(
            design["field_of_view"]["y"] * 1.05, rel=0.01
        )
        line = lines(client)["top_camera_scale"]
        assert line["status"] == "done" and line["warning"] is None  # 5% is not alarming

        # Visual homing now measures the homing error with the true scale.
        op = s.wait(s.post("/api/v1/machine/visual-home"))
        assert op["status"] == "succeeded", op

    # Used again after a restart.
    with TestClient(create_app(config)) as client:
        top = client.get("/api/v1/machine/description").json()["cameras"][0]
        assert top["field_of_view"]["x"] == pytest.approx(
            design["field_of_view"]["x"] * 1.05, rel=0.01
        )


def test_calibrate_the_bottom_camera() -> None:
    """The simulated bottom camera is 0.8 mm left of and 0.5 mm behind the profile's
    place, and in focus 0.4 mm lower: calibrating finds all three."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_bottom_camera_error": (-0.8, 0.5, -0.4)},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert lines(client)["bottom_camera"]["action"] == "bottom_camera"
        design = next(
            c
            for c in client.get("/api/v1/machine/description").json()["cameras"]
            if c["looking"] == "up"
        )
        assert home(s)["status"] == "succeeded"
        op = s.wait(s.post("/api/v1/machine/bottom-camera", {}))
        assert op["status"] == "succeeded", op
        bottom = next(
            c
            for c in client.get("/api/v1/machine/description").json()["cameras"]
            if c["looking"] == "up"
        )
        assert bottom["position"]["x"] == pytest.approx(design["position"]["x"] - 0.8, abs=0.03)
        assert bottom["position"]["y"] == pytest.approx(design["position"]["y"] + 0.5, abs=0.03)
        assert bottom["focus_height"] == pytest.approx(design["focus_height"] - 0.4, abs=0.06)
        assert bottom["field_of_view"]["x"] == pytest.approx(design["field_of_view"]["x"], rel=0.01)
        line = lines(client)["bottom_camera"]
        assert line["status"] == "done" and line["warning"] is None
        assert "mm from the profile's place" in str(line["detail"])
        # The nozzles are back up, and nothing is held.
        assert s.state()["z_safe"]


def test_the_bottom_camera_search_needs_a_focus_height(tmp_path: Path) -> None:
    """Without a focus height to start from, the search could drive the nozzle into the
    camera: it refuses, saying what to set."""
    with TestClient(create_app(fast_config())) as client, connect(client) as s:
        s.home()
        op = s.wait(s.post("/api/v1/machine/bottom-camera", {}))
        assert op["status"] == "failed" and "focus_height" in op["error"]


def offsets_config(**machine: object) -> Config:
    fiducial = (217.0, 106.0)  # the LumenPnP datum board's center
    return fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_calibration_disc": (fiducial[0], fiducial[1] - 11.5),
            **machine,
        },
        presence={"heartbeat_timeout_s": 300.0},
    )


def test_calibrate_the_nozzle_offsets() -> None:
    """The simulated nozzles are off from the profile's offsets, and the datum board is
    0.3 mm higher than the profile says. Each nozzle sniffs for the board, then turns a
    paper disc lying on it: that finds both nozzles, and the board's height."""
    errors = {"N1": (0.4, -0.25), "N2": (-0.15, 0.3)}
    config = offsets_config(sim_nozzle_offset_error=errors, sim_datum_height_error=0.3)
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert lines(client)["nozzle_offsets"]["action"] == "nozzle_offsets"
        design = client.get("/api/v1/machine/description").json()["nozzles"]
        assert home(s)["status"] == "succeeded"
        disc = config.machine.sim_calibration_disc
        assert disc is not None
        cam = client.get("/api/v1/machine/description").json()["cameras"][0]["position"]
        s.wait(
            s.post(
                "/api/v1/machine/move",
                {"position": {"X": disc[0] - cam["x"], "Y": disc[1] - cam["y"]}},
            )
        )
        op = s.wait(s.post("/api/v1/machine/nozzle-offsets", {}))
        assert op["status"] == "succeeded", op
        nozzles = client.get("/api/v1/machine/description").json()["nozzles"]
        for before, after in zip(design, nozzles, strict=True):
            dx, dy = errors[after["id"]]
            assert after["offset"]["x"] == pytest.approx(before["offset"]["x"] + dx, abs=0.03)
            assert after["offset"]["y"] == pytest.approx(before["offset"]["y"] + dy, abs=0.03)
        line = lines(client)["nozzle_offsets"]
        assert line["status"] == "done" and line["warning"] is None
        assert "touching the datum board at Z -27.2" in str(line["detail"])
        assert s.state()["z_safe"]

    # Used again after a restart.
    with TestClient(create_app(config)) as client:
        nozzles = client.get("/api/v1/machine/description").json()["nozzles"]
        assert nozzles[0]["offset"]["x"] == pytest.approx(design[0]["offset"]["x"] + 0.4, abs=0.03)


def test_the_bottom_camera_moves_with_its_nozzle(tmp_path: Path) -> None:
    """The bottom camera's place was found with N1's tip: when N1's offset is measured
    later, the camera's place moves with it."""
    store = CalibrationStore(None)
    profile = fast_config(machine={"profile": "lumenpnp-v4.1"}).machine.machine_profile
    n1 = profile.nozzles[0]
    store.record(
        "bottom_camera",
        "found",
        {
            "x": 200.0,
            "y": 150.0,
            "focus": -3.5,
            "scale_x": 0.03,
            "scale_y": 0.03,
            "width": 1280,
            "height": 720,
            nozzle_key("N1", "x"): n1.offset.x,
            nozzle_key("N1", "y"): n1.offset.y,
        },
    )
    store.record(
        "nozzle_offsets",
        "measured",
        {nozzle_key("N1", "x"): n1.offset.x + 0.5, nozzle_key("N1", "y"): n1.offset.y - 0.2},
    )
    apply_measurements(profile, store.get())
    bottom = next(c for c in profile.cameras if c.looking == "up")
    assert bottom.position.x == pytest.approx(200.5)
    assert bottom.position.y == pytest.approx(149.8)


def test_sniffing_stops_short_of_crashing() -> None:
    """With no board where the profile says (here, 3 mm lower than its height), the
    nozzle stops 1.5 mm below that height (or at the bottom of its travel) and says so."""
    config = offsets_config(sim_datum_height_error=-3.0)
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        disc = config.machine.sim_calibration_disc
        assert disc is not None
        cam = client.get("/api/v1/machine/description").json()["cameras"][0]["position"]
        s.wait(
            s.post(
                "/api/v1/machine/move",
                {"position": {"X": disc[0] - cam["x"], "Y": disc[1] - cam["y"]}},
            )
        )
        op = s.wait(s.post("/api/v1/machine/nozzle-offsets", {"nozzle_ids": ["N1"]}))
        assert op["status"] == "failed" and "did not seal" in op["error"]
        assert s.state()["z_safe"]
