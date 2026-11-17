"""The calibration checklist: what it says before and after measuring, what a crash makes
out of date, and what it stops in a run."""

import math
import re
import time
from pathlib import Path
from typing import Any

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
from picnicplace.machine.nozzle_offsets import (
    DISC_FIT_MM,
    NozzleOffsetError,
    search_disc,
    search_spots,
)
from picnicplace.machine.service import APPROACH_SPEED
from picnicplace.machine.tip_heights import find_paper
from tests.conftest import Session, connect, fast_config
from tests.test_placement import feeders, ok, placed, set_up
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
        assert op["status"] == "succeeded", op.get("error")
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


def test_heights_come_from_the_paper_not_the_bare_board() -> None:
    """As on a real LumenPnP (2026-11-14), a tip on the bare datum board leaks until it is
    pressed 3 mm into it, so stepping down onto the board would find it 3 mm too low.
    Paper seals a tip as soon as it touches: the heights come from the disc, less its
    thickness, and are right."""
    config = offsets_config(sim_datum_seal_press=3.0, sim_datum_height_error=0.3)
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
        op = s.wait(s.post("/api/v1/machine/nozzle-offsets", {}))
        assert op["status"] == "succeeded", op.get("error")
        detail = str(lines(client)["nozzle_offsets"]["detail"])
        assert detail.count("touching the datum board at Z -27.20") == 2, detail
        assert not feeders_parts_crashed(client)


def test_small_tips_take_their_height_from_the_bare_board() -> None:
    """On a real LumenPnP (2026-11-16) an N045 pulled the paper disc up from about a
    millimeter away, so its height would come out a millimeter high; but it sealed on the
    bare datum board as soon as it touched (as did an N08), though bigger tips must be
    pressed 3 mm in. Its first 0.1 mm of touching seals only partly (an N08 rose 46% as far
    as holding the disc), which is not taken for a leak pressed shut. Small tips touch a
    bare spot of the board, clear of the disc, and the disc still gives the offsets."""
    config = offsets_config(
        sim_datum_seal_press=3.0,
        sim_bare_seal_tip_mm=1.5,
        sim_bare_first_seal=0.46,
        sim_paper_lift={"N045": 1.0},
        sim_datum_height_error=0.3,
        sim_tips={"N1": "N045", "N2": "N08"},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        for nozzle_id, tip_id in (("N1", "N045"), ("N2", "N08")):
            r = s.put(f"/api/v1/machine/nozzles/{nozzle_id}/tip", {"tip_id": tip_id})
            assert r.status_code == 200, r.json()
        move_camera_over_disc(client, s, config)
        op = s.wait(s.post("/api/v1/machine/nozzle-offsets", {}))
        assert op["status"] == "succeeded", op.get("error")
        detail = str(lines(client)["nozzle_offsets"]["detail"])
        assert detail.count("touching the datum board at Z -27.20") == 2, detail
        assert not feeders_parts_crashed(client)


def test_a_disc_that_jumps_to_a_big_tip_takes_the_nozzles_height_from_a_small_one() -> None:
    """On a real LumenPnP (2026-11-17) an N24 pulled a plastic punch up from about 1 mm on
    the first step down, so its height could not come from the disc; the nozzle's height
    measured with an N045 on the bare board (all its tips are as long) is used instead,
    and the disc still gives the offsets. The real disc jumped on the first step because
    the nozzle sat 0.9 mm higher than the profile says; here it jumps from farther."""
    config = offsets_config(
        sim_datum_seal_press=3.0,
        sim_bare_seal_tip_mm=1.5,
        sim_paper_lift={"N24": 2.5},
        sim_datum_height_error=0.3,
        sim_tips={"N1": "N045", "N2": "N08"},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        for nozzle_id, tip_id in (("N1", "N045"), ("N2", "N08")):
            r = s.put(f"/api/v1/machine/nozzles/{nozzle_id}/tip", {"tip_id": tip_id})
            assert r.status_code == 200, r.json()
        move_camera_over_disc(client, s, config)
        op = s.wait(s.post("/api/v1/machine/nozzle-offsets", {"nozzle_ids": ["N1"]}))
        assert op["status"] == "succeeded", op.get("error")

        r = s.put("/api/v1/machine/nozzles/N1/tip", {"tip_id": "N24"})
        assert r.status_code == 200, r.json()
        move_camera_over_disc(client, s, config)
        op = s.wait(s.post("/api/v1/machine/nozzle-offsets", {"nozzle_ids": ["N1"]}))
        assert op["status"] == "succeeded", op.get("error")
        detail = str(lines(client)["nozzle_offsets"]["detail"])
        assert "N24 tip: (" in detail and "touching the datum board at Z -27.20" in detail
        assert "the disc jumped up to the tip" in detail and "the N045 tip's" in detail
        assert not feeders_parts_crashed(client)


def test_a_disc_that_jumps_with_no_height_known_stops_the_measuring() -> None:
    config = offsets_config(sim_paper_lift={"N24": 2.5}, sim_tips={"N1": "N24"})
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        r = s.put("/api/v1/machine/nozzles/N1/tip", {"tip_id": "N24"})
        assert r.status_code == 200, r.json()
        move_camera_over_disc(client, s, config)
        op = s.wait(s.post("/api/v1/machine/nozzle-offsets", {"nozzle_ids": ["N1"]}))
        assert op["status"] == "failed"
        assert "as soon as it came down" in op["error"]


def test_the_disc_search_sees_a_disc_whole_anywhere() -> None:
    """On a real LumenPnP (2026-11-15) the search's frames, 11.6 mm tall and stepped 8.1 mm
    apart, cut a 4.9 mm disc off at the edge of every one, so it was never found. Now some
    frame holds a disc that size, or a hole punch's, whole wherever it lies."""
    fov = (20.217, 11.631)  # the v4.1 top camera's, as measured
    area = (173.1, 167.3, 263.1, 212.3)  # around the datum board's keep-out
    spots = search_spots(area, fov, DISC_FIT_MM)
    for d in (4.9, 6.4):
        half = ((fov[0] - d) / 2, (fov[1] - d) / 2)
        for i in range(361):
            for j in range(181):
                x, y = area[0] + i * 0.25, area[1] + j * 0.25
                assert any(
                    abs(x - p.x) <= half[0] + 1e-9 and abs(y - p.y) <= half[1] + 1e-9 for p in spots
                ), (d, x, y)


RING_LIGHT = (217.0, 136.0)
"""In the bottom camera's keep-out, and within the paper searches' reach past the datum
board's edge."""


def test_the_disc_search_leaves_out_what_lies_past_the_board() -> None:
    """On a real LumenPnP (2026-11-16) the disc search, which reaches 5 mm past the datum
    board's keep-out, took an LED of the bottom camera's ring light for the disc, and sent
    the nozzle down onto it. Something round and bright in another keep-out is left out."""
    config = offsets_config(
        sim_paper=(RING_LIGHT[0], RING_LIGHT[1], 6.0), sim_calibration_disc=(240.0, 120.0)
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        machine = client.app.state.ctx.machine  # type: ignore[attr-defined]
        assert client.portal is not None
        at, _ = client.portal.call(search_disc, machine, None)
        assert math.hypot(at.x - 240.0, at.y - 120.0) < 0.1, at


def test_the_disc_search_leaves_out_the_boards_own_pad() -> None:
    """On a real LumenPnP (2026-11-16) the disc search took the datum board's tinned pad,
    whiter than paper and the same size as a disc, for the disc. The board's own marks are
    left out, wherever it is mounted."""
    config = offsets_config(sim_calibration_disc=(210.0, 126.0))
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        machine = client.app.state.ctx.machine  # type: ignore[attr-defined]
        assert client.portal is not None
        at, _ = client.portal.call(search_disc, machine, None)
        assert math.hypot(at.x - 210.0, at.y - 126.0) < 0.1, at


def test_the_paper_search_leaves_out_what_lies_past_the_board() -> None:
    """Looking for a paper square, white in another keep-out is left out too."""
    config = offsets_config(sim_paper=(RING_LIGHT[0], RING_LIGHT[1], 10.0))
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        machine = client.app.state.ctx.machine  # type: ignore[attr-defined]
        assert client.portal is not None
        with pytest.raises(NozzleOffsetError, match="found no paper on the datum board"):
            # More room than the 6 mm paper disc on the board has; the square has 5 mm.
            client.portal.call(find_paper, machine, 3.5)


def asked(s: Session, timeout: float = 60.0) -> dict[str, Any]:
    """Wait for the machine to ask the operator something, and return what."""
    deadline = time.monotonic() + timeout
    while (request := s.state()["request"]) is None:
        assert time.monotonic() < deadline, "the machine asked nothing"
        time.sleep(0.05)
    return request


def test_a_tip_too_big_for_the_disc_is_changed_before_anything_goes_down() -> None:
    """An N24 tip (4 mm across) on a real 4.9 mm disc sealed half over its edge and dropped
    it. Before any nozzle goes down, a tip without a millimeter of paper all round is
    changed: the head comes to the front, nozzles up, and asks for tips that leave that
    much, and asks again until they do. Then each nozzle is measured with its new tip."""
    config = offsets_config()
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
        assert s.put("/api/v1/machine/nozzles/N1/tip", {"tip_id": "N40"}).status_code == 200
        assert s.put("/api/v1/machine/nozzles/N2/tip", {"tip_id": "N08"}).status_code == 200
        started = s.post("/api/v1/machine/nozzle-offsets", {})
        assert started.status_code == 202, started.json()

        request = asked(s)
        assert request["kind"] == "tip_change"
        assert "N40 (6.2 mm)" in request["message"], request
        wanted = {n["nozzle_id"]: n for n in request["nozzles"]}
        assert wanted["N1"]["fitted"] == "N40"
        # A 6 mm disc leaves room for tips up to 4 mm across, smallest first.
        assert wanted["N1"]["choices"] == ["N045", "N08", "N14", "N24"]
        at = s.state()["position"]
        assert (at["X"], at["Y"], at["Z"]) == pytest.approx((216.5, 44.454, 0.0), abs=0.01)

        # An answer to some other question, or one leaving a nozzle out, is refused.
        r = s.post("/api/v1/machine/answer", {"request_id": "x", "tips": {"N1": "N08"}})
        assert r.status_code == 422, r.json()
        r = s.post("/api/v1/machine/answer", {"request_id": request["id"], "tips": {"N1": "N08"}})
        assert r.status_code == 422 and "right nozzle" in r.json()["detail"], r.json()
        # Still too big: asked again.
        tips = {"N1": "N75", "N2": "N08"}
        r = s.post("/api/v1/machine/answer", {"request_id": request["id"], "tips": tips})
        assert r.status_code == 200, r.json()
        again = asked(s)
        assert again["id"] != request["id"] and "N75 (9 mm)" in again["message"], again
        tips = {"N1": "N045", "N2": "N08"}
        s.post("/api/v1/machine/answer", {"request_id": again["id"], "tips": tips})

        op = s.wait(started)
        assert op["status"] == "succeeded", op.get("error")
        nozzles = client.get("/api/v1/machine/description").json()["nozzles"]
        assert [n["tip"] for n in nozzles] == ["N045", "N08"]
        assert lines(client)["nozzle_offsets"]["status"] == "done"


def test_a_tip_change_waits_at_the_front_until_answered_or_stopped() -> None:
    """Changing tips by hand: the head comes to the front and center with the nozzles up,
    and waits for the operator to say which tip is on each nozzle. Stop ends it with the
    tips as they were."""
    with TestClient(create_app(offsets_config())) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        started = s.post("/api/v1/machine/tip-change", {})
        assert started.status_code == 202, started.json()
        request = asked(s)
        assert [n["nozzle_id"] for n in request["nozzles"]] == ["N1", "N2"]
        assert all(n["choices"] == [] for n in request["nozzles"])
        tips = {"N1": "N14", "N2": None}
        r = s.post("/api/v1/machine/answer", {"request_id": request["id"], "tips": tips})
        assert r.status_code == 200, r.json()
        op = s.wait(started)
        assert op["status"] == "succeeded", op
        assert op["message"] == "Tips: Left nozzle: N14; Right nozzle: none."
        nozzles = client.get("/api/v1/machine/description").json()["nozzles"]
        assert [n["tip"] for n in nozzles] == ["N14", None]

        started = s.post("/api/v1/machine/tip-change", {"nozzle_ids": ["N2"]})
        asked(s)
        s.post("/api/v1/machine/stop")
        op = s.wait(started)
        assert op["status"] == "failed" and "stopped" in op["error"], op
        assert s.state()["request"] is None
        nozzles = client.get("/api/v1/machine/description").json()["nozzles"]
        assert [n["tip"] for n in nozzles] == ["N14", None]


def test_a_nozzle_slows_for_the_last_two_millimeters_down() -> None:
    """Going down, a nozzle moves at speed until 2 mm above where it is going, then at
    10 mm/s, so it meets a part or the board gently whatever the machine's speed; going up,
    it is not slowed."""
    config = offsets_config()
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        machine = client.app.state.ctx.machine  # type: ignore[attr-defined]
        driver = machine.driver
        moves: list[tuple[dict[str, float], float | None]] = []
        real = driver.move_to

        async def logged(target: dict[str, float], speed: float | None) -> None:
            moves.append((dict(target), speed))
            await real(target, speed)

        driver.move_to = logged
        nozzle = machine.profile.nozzles[0]
        portal = client.portal
        assert portal is not None
        portal.call(machine.lower_nozzle, nozzle, -20.0)
        z = nozzle.z_axis
        down = [(t[z], v) for t, v in moves if z in t]
        assert [round(h * nozzle.z_sign, 3) for h, _ in down] == [-18.0, -20.0], down
        assert down[0][1] is None or down[0][1] > APPROACH_SPEED
        assert down[1][1] == APPROACH_SPEED

        moves.clear()
        portal.call(machine.lower_nozzle, nozzle, -19.5)  # half a millimeter up
        assert [v for t, v in moves if z in t] == [None]

        moves.clear()
        portal.call(machine.lower_nozzle, nozzle, -20.5)  # half a millimeter down
        assert [v for t, v in moves if z in t] == [APPROACH_SPEED]


def feeders_parts_crashed(client: TestClient) -> list[str]:
    return client.app.state.ctx.feeders.sim_parts.crashes  # type: ignore[attr-defined,no-any-return]


def test_a_bottom_camera_with_a_deep_field_keeps_the_profiles_focus() -> None:
    """A real LumenPnP's bottom camera was as sharp all through the focus search, and each
    calibration, searching round the last, settled up to 2 mm nearer the camera: Z -4.63,
    then -6.49, then -8.10. The search is always round the profile's focus height, and with
    no focus to find, it keeps that height, however many times it is run."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_bottom_camera_deep_focus": True},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        design = next(
            c
            for c in client.get("/api/v1/machine/description").json()["cameras"]
            if c["looking"] == "up"
        )["focus_height"]
        assert home(s)["status"] == "succeeded"
        for _ in range(3):
            op = ok(s.wait(s.post("/api/v1/machine/bottom-camera", {})))
            assert "the profile's focus height: the picture was as sharp" in op["message"]
            bottom = next(
                c
                for c in client.get("/api/v1/machine/description").json()["cameras"]
                if c["looking"] == "up"
            )
            assert bottom["focus_height"] == pytest.approx(design, abs=1e-6)


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


def test_the_bottom_camera_focus_keeps_up_with_its_nozzles_height() -> None:
    """The bottom camera is calibrated before the nozzle offsets, as stop 1 comes before
    stop 2. N1's tip is 0.9 mm longer than the profile's, so the offsets give it a Z offset
    of +0.9 mm. Sent to the focus height with that offset, its tip's end still comes to the
    camera's focus, and calibrating the bottom camera again finds it there."""
    config = offsets_config(sim_nozzle_z_error={"N1": 0.9})
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        ok(s.wait(s.post("/api/v1/machine/bottom-camera", {})))
        before = next(
            c
            for c in client.get("/api/v1/machine/description").json()["cameras"]
            if c["looking"] == "up"
        )["focus_height"]

        move_camera_over_disc(client, s, config)
        ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {})))
        description = client.get("/api/v1/machine/description").json()
        n1 = next(n for n in description["nozzles"] if n["id"] == "N1")
        assert n1["z_offset"] == pytest.approx(0.9, abs=0.1)
        bottom = next(c for c in description["cameras"] if c["looking"] == "up")
        # The same place for the nozzle (raw), so a height lower by the offset.
        assert bottom["focus_height"] == pytest.approx(before - n1["z_offset"], abs=0.01)

        op = ok(s.wait(s.post("/api/v1/machine/bottom-camera", {})))
        found = re.search(r"nozzle at Z (-?\d+\.\d+)", op["message"])
        assert found is not None, op["message"]
        assert float(found[1]) == pytest.approx(before - n1["z_offset"], abs=0.06)
        assert s.state()["z_safe"]


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


def test_a_longer_tip_is_found_on_the_datum_board() -> None:
    """N2's tip is 0.8 mm longer than the profile's: sent to the profile's heights, it
    presses the part it places into the board (its pick finds the part by touch, so that
    does not). Sniffing the datum board while calibrating the nozzle offsets finds the
    difference, and from then on N2 sets the part down on the board."""
    config = offsets_config(sim_nozzle_z_error={"N2": 0.8})
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        parts = feeders(client).sim_parts
        assert parts is not None

        op = ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N2"})))
        assert "by touch, +1.00 mm" in op["message"], op["message"]
        ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N2"})))
        target = {"nozzle_id": "N2", "x": 300.0, "y": 250.0, "rotation": 0.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        assert len(parts.crashes) == 1, parts.crashes
        assert "N2 pressed its part 0.80 mm into the board" in parts.crashes[0]

        disc = config.machine.sim_calibration_disc
        assert disc is not None
        cam = client.get("/api/v1/machine/description").json()["cameras"][0]["position"]
        s.wait(
            s.post(
                "/api/v1/machine/move",
                {"position": {"X": disc[0] - cam["x"], "Y": disc[1] - cam["y"]}},
            )
        )
        ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {})))
        nozzles = {n["id"]: n for n in client.get("/api/v1/machine/description").json()["nozzles"]}
        assert nozzles["N1"]["z_offset"] == pytest.approx(0.0, abs=0.1)
        assert nozzles["N2"]["z_offset"] == pytest.approx(0.8, abs=0.1)
        # The part top N2 found before is no good with its new offset.
        assert feeders(client).part_top("1") is None

        parts.crashes.clear()
        ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "2", "nozzle_id": "N2"})))
        ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N2"})))
        target = {"nozzle_id": "N2", "x": 260.0, "y": 230.0, "rotation": 0.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        assert parts.crashes == []
        landed = placed(client)[-1]
        assert landed.fell < 0.1
        assert (landed.x, landed.y) == pytest.approx((260.0, 230.0), abs=0.05)


def move_camera_over_disc(client: TestClient, s: Session, config: Config) -> None:
    disc = config.machine.sim_calibration_disc
    assert disc is not None
    cam = client.get("/api/v1/machine/description").json()["cameras"][0]["position"]
    s.wait(
        s.post(
            "/api/v1/machine/move", {"position": {"X": disc[0] - cam["x"], "Y": disc[1] - cam["y"]}}
        )
    )


def test_each_nozzles_vacuum_levels_are_measured() -> None:
    """N1's pump is weak: holding a part, its vacuum never reaches the profile's
    threshold. N2's tip leaks: open, it already reads past it. Neither can pick until
    calibrating the nozzle offsets measures each one's levels; then both pick and place."""
    # Fractions of the sensor's range, open and holding a part; the threshold is at 0.64.
    config = offsets_config(sim_vacuum_levels={"N1": (0.3, 0.5), "N2": (0.7, 0.95)})
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        assert lines(client)["vacuum"]["status"] == "assumed"
        op = s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N1"}))
        assert op["status"] == "failed" and "did not seal" in op["error"], op
        op = s.wait(s.post("/api/v1/placement/pick", {"slot_id": "2", "nozzle_id": "N2"}))
        assert op["status"] == "failed", op
        assert "reads as holding a part before touching" in op["error"]
        assert "measures this nozzle's own vacuum levels" in op["error"]
        parts = feeders(client).sim_parts
        assert parts is not None
        # N1 took its part without knowing, and pressed it down to where sniffing stops.
        parts.crashes.clear()

        move_camera_over_disc(client, s, config)
        ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {})))
        line = lines(client)["vacuum"]
        assert line["status"] == "done", line
        assert "Left nozzle: open -750,000, holding a part -1,250,000" in str(line["detail"]), line
        nozzles = {n["id"]: n for n in client.get("/api/v1/machine/description").json()["nozzles"]}
        # Halfway from open to holding a part, in counts (the range runs 0 to -2,500,000).
        assert nozzles["N1"]["vacuum"]["threshold"] == pytest.approx(-1_000_000, rel=0.01)
        assert nozzles["N2"]["vacuum"]["threshold"] == pytest.approx(-2_062_500, rel=0.01)

        for nozzle, slot, x in (("N1", "1", 260.0), ("N2", "2", 300.0)):
            ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": slot, "nozzle_id": nozzle})))
            ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": nozzle})))
            target = {"nozzle_id": nozzle, "x": x, "y": 230.0, "rotation": 0.0}
            ok(s.wait(s.post("/api/v1/placement/place", target)))
            landed = placed(client)[-1]
            assert (landed.x, landed.y) == pytest.approx((x, 230.0), abs=0.05)
        assert parts.crashes == []

    # Used again after a restart.
    with TestClient(create_app(config)) as client:
        nozzles = {n["id"]: n for n in client.get("/api/v1/machine/description").json()["nozzles"]}
        assert nozzles["N2"]["vacuum"]["threshold"] == pytest.approx(-2_062_500, rel=0.01)
