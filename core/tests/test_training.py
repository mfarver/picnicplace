"""Training how the bottom camera sees a part (picnicplace.training), in the simulator."""

from typing import Any

from fastapi.testclient import TestClient

from picnicplace.api import create_app
from tests.conftest import Session, connect, fast_config, operation_done, receive_until
from tests.test_placement import angle_error, ok, placed, set_up


def _long(s: Session, response: Any) -> dict[str, Any]:
    """Wait for an operation that sends many events (training's moves and lights)."""
    assert response.status_code == 202, response.json()
    s.ws.send_json({"type": "ping"})
    return receive_until(s.ws, operation_done(response.json()["id"]), limit=50000)["operation"]


def _washed_out_machine() -> TestClient:
    """White chips, overexposed under a full white light: their metal shows only with the
    ring light turned down. The LumenPnP's ring light takes a color."""
    return TestClient(
        create_app(
            fast_config(
                machine={"profile": "lumenpnp-v4.1", "sim_bottom_washed_out": True},
                presence={"heartbeat_timeout_s": 300.0},
            )
        )
    )


def test_training_finds_a_light_that_shows_the_metal_and_waits_for_approval() -> None:
    with _washed_out_machine() as client, connect(client) as s:
        set_up(client, s, slots=("3",))
        bin_ = {"position": {"x": 300.0, "y": 100.0}, "label": "Discard"}
        assert s.put("/api/v1/machine/locations/discard", bin_).status_code == 200
        part_id = client.app.state.ctx.feeders.part(  # type: ignore[attr-defined]
            client.app.state.ctx.feeders.slot("3")  # type: ignore[attr-defined]
        ).id
        body = {"slot_id": "3", "nozzle_id": "N1", "pick_correction": "off"}
        op = ok(_long(s, s.post("/api/v1/placement/train", body)))
        assert f"Trained {part_id}" in op["message"], op["message"]
        assert "dropped its part" in op["message"]
        assert s.client.get("/api/v1/placement").json()["loads"] == []

        trained = s.client.get("/api/v1/placement/trained").json()
        assert [t["part_id"] for t in trained] == [part_id]
        t = trained[0]
        assert not t["approved"]
        assert t["tried"] > 100 and t["candidates"][0]["score"] is not None
        best = t["candidates"][0]
        assert best["vision"] == t["vision"]
        assert best["angle_spread"] < 1.0 and best["offset_spread"] < 0.05
        # Under a full white light the chip is one white rectangle: the way chosen sees
        # its metal under a dimmer light, or its outline.
        light = t["vision"]["light"]
        if t["vision"]["method"] == "pads":
            assert light is not None and light["brightness"] < 255
        for name in ("overlay", "beside", "light-00"):
            r = s.client.get(f"/api/v1/placement/trained/{part_id}/images/{name}")
            assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", name
        # The light is back to white after training.
        assert s.state()["lights"] == {}

        # Not used until approved.
        pick = {"slot_id": "3", "nozzle_id": "N1", "feed": True, "pick_correction": "off"}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        op = ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        assert "Seen as trained" not in op["message"]
        ok(s.wait(s.post("/api/v1/placement/discard", {"nozzle_id": "N1"})))

        r = s.post(f"/api/v1/placement/trained/{part_id}/approve", {}).json()
        assert r["approved"], r
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        op = ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        assert "Seen as trained" in op["message"], op["message"]
        target = {"nozzle_id": "N1", "x": 150.0, "y": 280.0, "rotation": 30.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        part = placed(client)[-1]
        assert abs(part.x - 150.0) < 0.08 and abs(part.y - 280.0) < 0.08
        assert angle_error(part.angle, 30.0) < 1.0

        assert s.delete(f"/api/v1/placement/trained/{part_id}").status_code == 204
        assert s.client.get("/api/v1/placement/trained").json() == []
