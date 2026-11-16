"""Nozzle runout: each tip's end, off the axis it turns about, is found by turning it over
the bottom camera; the bottom camera's place is the axis's, and picks put the tip's end
over the part."""

import math
import re

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from tests.conftest import connect, fast_config
from tests.test_calibration import lines
from tests.test_nozzle_tips import fit
from tests.test_placement import feeders, ok, placed, set_up
from tests.test_vision import home

RUNOUT = {"N1": (0.15, -0.1), "N2": (-0.05, 0.08)}


def description(client: TestClient) -> dict[str, dict[str, object]]:
    return {n["id"]: n for n in client.get("/api/v1/machine/description").json()["nozzles"]}


def test_runout_is_measured_for_each_tip() -> None:
    """Both tips' ends are off their axes, the left one by 0.18 mm. The bottom camera is
    calibrated on the left nozzle's axis, not its tip's end. Measuring the runout finds
    each end, keeps it for the tip fitted, and warns of the left one. A new tip on the
    left nozzle has none until it is measured, and only it is measured then."""
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_bottom_camera_error": (-0.8, 0.5, -0.4),
            "sim_nozzle_runout": RUNOUT,
        },
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        fit(s, "N1", "N08")
        fit(s, "N2", "N14")
        r = s.post("/api/v1/machine/nozzle-runout", {})
        assert r.status_code == 422 and "calibrate the bottom camera first" in r.json()["detail"]

        cameras = client.get("/api/v1/machine/description").json()["cameras"]
        design = next(c for c in cameras if c["looking"] == "up")
        ok(s.wait(s.post("/api/v1/machine/bottom-camera", {})))
        cameras = client.get("/api/v1/machine/description").json()["cameras"]
        bottom = next(c for c in cameras if c["looking"] == "up")
        assert bottom["position"]["x"] == pytest.approx(design["position"]["x"] - 0.8, abs=0.03)
        assert bottom["position"]["y"] == pytest.approx(design["position"]["y"] + 0.5, abs=0.03)
        assert "its tip's end was 0.18 mm off it" in str(lines(client)["bottom_camera"]["detail"])

        line = lines(client)["nozzle_runout"]
        assert line["status"] == "assumed" and line["action"] == "nozzle_runout"
        op = ok(s.wait(s.post("/api/v1/machine/nozzle-runout", {})))
        assert "Left nozzle with the N08 tip: its end is 0.18" in op["message"], op
        nozzles = description(client)
        for nozzle_id, (x, y) in RUNOUT.items():
            runout = nozzles[nozzle_id]["runout"]
            assert isinstance(runout, dict)
            assert (runout["x"], runout["y"]) == pytest.approx((x, y), abs=0.02)
        line = lines(client)["nozzle_runout"]
        assert line["status"] == "done", line
        assert "0.18 mm off" in str(line["warning"])
        assert "Right nozzle with the N14 tip" in str(line["detail"])
        assert s.state()["z_safe"]

        # A new tip has its own runout, not measured yet; the other nozzle keeps its own.
        fit(s, "N1", "N045")
        nozzles = description(client)
        assert nozzles["N1"]["runout"] == {"x": 0.0, "y": 0.0}
        assert nozzles["N2"]["runout"] != {"x": 0.0, "y": 0.0}
        line = lines(client)["nozzle_runout"]
        assert line["status"] == "assumed" and "Left nozzle (N045)" in str(line["detail"])
        op = ok(s.wait(s.post("/api/v1/machine/nozzle-runout", {})))
        assert "Left nozzle with the N045 tip" in op["message"]
        assert "Right nozzle" not in op["message"]
        assert lines(client)["nozzle_runout"]["status"] == "done"

    # Kept across a restart.
    with TestClient(create_app(config)) as client:
        runout = description(client)["N2"]["runout"]
        assert isinstance(runout, dict)
        assert (runout["x"], runout["y"]) == pytest.approx(RUNOUT["N2"], abs=0.02)


def test_picks_put_the_tips_end_over_the_part() -> None:
    """With its runout measured, the left nozzle picks with its tip's end over the part,
    so the part sits centered on the end; the feeder learns nothing from the runout (only
    its pocket's own small error), and the part lands where it is sent."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_nozzle_runout": {"N1": RUNOUT["N1"]}},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        ok(s.wait(s.post("/api/v1/machine/bottom-camera", {})))
        ok(s.wait(s.post("/api/v1/machine/nozzle-runout", {"nozzle_ids": ["N1"]})))

        ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N1"})))
        parts = feeders(client).sim_parts
        assert parts is not None
        held = parts.held["N1"]
        assert math.hypot(held.dx, held.dy) < 0.03, held

        op = ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        learned = re.search(r"parts lie ([-+]\d+\.\d+), ([-+]\d+\.\d+) mm", op["message"])
        assert learned is not None, op["message"]
        # The pocket's own small error (0.04 mm in the simulator), not the tip's 0.18.
        assert math.hypot(float(learned[1]), float(learned[2])) < 0.08, op["message"]

        target = {"nozzle_id": "N1", "x": 220.0, "y": 250.0, "rotation": 60.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        part = placed(client)[-1]
        assert (part.x, part.y) == pytest.approx((220.0, 250.0), abs=0.05)
