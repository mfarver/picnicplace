"""Nozzle tips: which is fitted on each nozzle, kept across restarts, and each nozzle's Z
offset and vacuum levels kept for each tip it has been calibrated with."""

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.machine.bottom_camera import tip_size_note
from picnicplace.machine.profile import NozzleTip
from tests.conftest import Session, connect, fast_config
from tests.test_calibration import lines, move_camera_over_disc, offsets_config
from tests.test_placement import feeders, ok, placed, set_up
from tests.test_vision import home


def fit(s: Session, nozzle_id: str, tip_id: str | None) -> dict[str, dict[str, object]]:
    r = s.put(f"/api/v1/machine/nozzles/{nozzle_id}/tip", {"tip_id": tip_id})
    assert r.status_code == 200, r.json()
    return {n["id"]: n for n in r.json()["nozzles"]}


def test_fitting_a_tip_is_kept() -> None:
    config = offsets_config()
    with TestClient(create_app(config)) as client, connect(client) as s:
        description = client.get("/api/v1/machine/description").json()
        assert [t["id"] for t in description["nozzle_tips"]] == [
            "N045",
            "N08",
            "N14",
            "N24",
            "N40",
            "N75",
            "N110",
        ]
        n08 = description["nozzle_tips"][1]
        assert (n08["inner_diameter"], n08["outer_diameter"]) == (0.8, 1.5)
        assert description["nozzle_tips"][5]["outer_diameter"] == 9.0
        assert {t["length"] for t in description["nozzle_tips"][:6]} == {14.5}
        assert all(n["tip"] is None for n in description["nozzles"])
        assert fit(s, "N1", "N08")["N1"]["tip"] == "N08"
        r = s.put("/api/v1/machine/nozzles/N1/tip", {"tip_id": "N99"})
        assert r.status_code == 422 and "no nozzle tip 'N99'" in r.json()["detail"]
        # Changing a tip is recorded only by the client holding control.
        with connect(client, "other") as other:
            r = other.put("/api/v1/machine/nozzles/N2/tip", {"tip_id": "N14"})
            assert r.status_code == 409, r.json()

    with TestClient(create_app(config)) as client:
        nozzles = {n["id"]: n for n in client.get("/api/v1/machine/description").json()["nozzles"]}
        assert nozzles["N1"]["tip"] == "N08" and nozzles["N2"]["tip"] is None


def test_each_tip_keeps_its_own_heights() -> None:
    """The N14 tip is 0.6 mm longer than the N08. N1 is calibrated with each; changing
    back and forth then brings each tip's Z offset back without measuring again. A part's
    top found with one tip is sniffed again with the other, and a nozzle holding a part
    cannot have its tip changed."""
    config = offsets_config(sim_tip_z_error={"N14": 0.6})
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        fit(s, "N1", "N08")
        move_camera_over_disc(client, s, config)
        ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {"nozzle_ids": ["N1"]})))
        line = lines(client)["nozzle_offsets"]
        assert line["status"] == "done" and "Left nozzle with the N08 tip" in str(line["detail"])

        nozzles = fit(s, "N1", "N14")
        assert nozzles["N1"]["z_offset"] == 0.0 and nozzles["N1"]["vacuum"] is None
        line = lines(client)["nozzle_offsets"]
        assert line["status"] == "stale", line
        assert "not measured with the tip fitted on Left nozzle (N14)" in str(line["stale"])

        move_camera_over_disc(client, s, config)
        ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {"nozzle_ids": ["N1"]})))
        assert lines(client)["nozzle_offsets"]["status"] == "done"
        n14 = client.get("/api/v1/machine/description").json()["nozzles"][0]
        assert n14["z_offset"] == pytest.approx(0.6, abs=0.1)

        n08 = fit(s, "N1", "N08")["N1"]
        assert n08["z_offset"] == pytest.approx(0.0, abs=0.1) and n08["vacuum"] is not None
        assert lines(client)["nozzle_offsets"]["status"] == "done"

        # A part's top, found with the N08 tip, is not used with the N14.
        parts = feeders(client).sim_parts
        assert parts is not None
        ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N1"})))
        assert feeders(client).part_top("1") is not None
        r = s.put("/api/v1/machine/nozzles/N1/tip", {"tip_id": "N14"})
        assert r.status_code == 422 and "holding a part" in r.json()["detail"]
        ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        target = {"nozzle_id": "N1", "x": 260.0, "y": 230.0, "rotation": 0.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        assert fit(s, "N1", "N14")["N1"]["z_offset"] == pytest.approx(0.6, abs=0.1)
        assert feeders(client).part_top("1") is None

        # With the longer tip and its own offset, it still sets parts down on the board.
        ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N1"})))
        ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        target = {"nozzle_id": "N1", "x": 300.0, "y": 230.0, "rotation": 0.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        assert parts.crashes == []
        landed = placed(client)[-1]
        assert landed.fell < 0.1
        assert (landed.x, landed.y) == pytest.approx((300.0, 230.0), abs=0.05)

    # After a restart, the N14 is still on N1, with its own offset.
    with TestClient(create_app(config)) as client:
        n1 = client.get("/api/v1/machine/description").json()["nozzles"][0]
        assert n1["tip"] == "N14" and n1["z_offset"] == pytest.approx(0.6, abs=0.1)


@pytest.mark.parametrize(("tip", "diameter"), [("N045", 1.0), ("N08", 1.5), ("N75", 9.0)])
def test_the_bottom_camera_sees_the_fitted_tip(tip: str, diameter: float) -> None:
    """The simulated bottom camera draws the end of the tip fitted, in front of its body.
    Calibrating looks for a circle that tip's size, even one larger than a tip of unknown
    size may be (the N75), and says how big its end looked."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_bottom_camera_error": (-0.8, 0.5, -0.4)},
        presence={"heartbeat_timeout_s": 300.0},
    )

    def bottom() -> dict[str, dict[str, float]]:
        cameras = client.get("/api/v1/machine/description").json()["cameras"]
        return next(c for c in cameras if c["looking"] == "up")

    with TestClient(create_app(config)) as client, connect(client) as s:
        design = bottom()
        fit(s, "N1", tip)
        assert home(s)["status"] == "succeeded"
        op = s.wait(s.post("/api/v1/machine/bottom-camera", {}))
        assert op["status"] == "succeeded", op
        detail = str(lines(client)["bottom_camera"]["detail"])
        assert f"as the {tip} tip should" in detail, detail
        seen = float(detail.split("looked ")[1].split(" mm")[0])
        assert seen == pytest.approx(diameter, rel=0.1)
        found = bottom()["position"]
        assert found["x"] == pytest.approx(design["position"]["x"] - 0.8, abs=0.03)
        assert found["y"] == pytest.approx(design["position"]["y"] + 0.5, abs=0.03)


def test_a_tip_unlike_the_fitted_one_is_noted() -> None:
    n08 = NozzleTip(id="N08", label="N08", inner_diameter=0.8, outer_diameter=1.5)
    assert tip_size_note(n08, 1.6).endswith("as the N08 tip should.")
    note = tip_size_note(n08, 2.7)
    assert "but the N08 tip is 1.5 mm: check which tip is fitted" in note
    assert tip_size_note(None, 5.2) == "The tip's end looked 5.20 mm across."
