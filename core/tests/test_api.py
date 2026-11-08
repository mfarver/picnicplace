from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from picnicplace.api import create_app
from picnicplace.api.websocket import CLOSE_HEARTBEAT_TIMEOUT, CLOSE_UNAUTHORIZED

from .conftest import connect, fast_config, receive_until, wait_for

TOKEN = "correct-horse-battery-staple"


def test_health(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["protocol"] == 1


def test_hello_and_presence(client: TestClient) -> None:
    assert client.get("/api/v1/presence").json() == {"clients": 0, "present": False}
    with client.websocket_connect("/api/v1/events") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["machine"]["driver"] == "simulator"
        assert hello["presence"] == {"clients": 1, "present": True}
        assert client.get("/api/v1/machine").json()["motion_enabled"] is True
    wait_for(lambda: client.get("/api/v1/presence").json()["clients"] == 0)
    assert client.get("/api/v1/machine").json()["motion_enabled"] is False


def test_ping_pong(client: TestClient) -> None:
    with client.websocket_connect("/api/v1/events") as ws:
        ws.receive_json()
        ws.send_json({"type": "ping"})
        receive_until(ws, lambda m: m["type"] == "pong")
        ws.send_text("nonsense")
        receive_until(ws, lambda m: m["type"] == "error")


def test_motion_requires_a_connected_client(client: TestClient) -> None:
    r = client.post("/api/v1/machine/home")
    assert r.status_code == 409
    assert r.json()["code"] == "client_required"
    r = client.post("/api/v1/machine/home", headers={"X-PicnicPlace-Client": "not-connected"})
    assert r.json()["code"] == "client_required"


def test_home_then_move(client: TestClient) -> None:
    with connect(client) as s:
        assert s.post("/api/v1/machine/move", {"position": {"X": 10}}).json()["code"] == (
            "not_homed"
        )
        s.home()

        r = s.post("/api/v1/machine/move", {"position": {"X": 100, "Y": 50}})
        assert r.status_code == 202
        op_id = r.json()["id"]
        assert s.post("/api/v1/machine/home").json()["code"] == "busy"
        assert s.wait(r)["status"] == "succeeded"

        state = s.state()
        assert state["position"]["X"] == pytest.approx(100)
        assert state["position"]["Y"] == pytest.approx(50)
        assert state["busy"] is False
        assert client.get(f"/api/v1/operations/{op_id}").json()["status"] == "succeeded"


def test_move_rejects_bad_targets(client: TestClient) -> None:
    with connect(client) as s:
        s.home()
        for position in ({"X": 10_000}, {"Q": 1}):
            r = s.post("/api/v1/machine/move", {"position": position})
            assert r.status_code == 422
            assert r.json()["code"] == "invalid_target"


def test_estop_interrupts_and_latches(client: TestClient) -> None:
    with connect(client) as s:
        s.home()
        # A slow move so the E-stop lands mid-motion.
        r = s.post("/api/v1/machine/move", {"position": {"X": 400}, "speed": 1})
        # E-stop needs no lease or client ID.
        state = client.post("/api/v1/machine/estop").json()
        assert state["estop"] is True
        assert state["homed"] is False
        assert s.wait(r)["status"] == "failed"

        assert s.post("/api/v1/machine/home").json()["code"] == "estop_active"
        assert s.post("/api/v1/machine/reset").json()["estop"] is False
        assert s.post("/api/v1/machine/home").status_code == 202


def test_disconnect_stops_motion(client: TestClient) -> None:
    with connect(client) as s:
        s.home()
        r = s.post("/api/v1/machine/move", {"position": {"X": 400}, "speed": 1})
        op_id = r.json()["id"]
    wait_for(lambda: client.get(f"/api/v1/operations/{op_id}").json()["status"] == "failed")
    state = client.get("/api/v1/machine").json()
    assert state["busy"] is False
    assert state["homed"] is True
    assert state["position"]["X"] < 400


def test_heartbeat_timeout() -> None:
    config = fast_config(presence={"heartbeat_timeout_s": 0.2})
    with (
        TestClient(create_app(config)) as client,
        client.websocket_connect("/api/v1/events") as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        while True:
            ws.receive_json()
    assert exc.value.code == CLOSE_HEARTBEAT_TIMEOUT


def test_token_required_when_configured() -> None:
    config = fast_config(server={"token": TOKEN})
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/machine").status_code == 401
        bad = {"Authorization": "Bearer wrong-token"}
        assert client.get("/api/v1/machine", headers=bad).status_code == 401
        good = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/api/v1/machine", headers=good).status_code == 200

        with (
            client.websocket_connect("/api/v1/events") as ws,
            pytest.raises(WebSocketDisconnect) as exc,
        ):
            ws.receive_json()
        assert exc.value.code == CLOSE_UNAUTHORIZED

        with client.websocket_connect(f"/api/v1/events?token={TOKEN}") as ws:
            assert ws.receive_json()["type"] == "hello"


def test_openapi_includes_event_schemas(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert "ServerEvent" in schemas
    assert "HelloEvent" in schemas
    assert "ClientMessage" in schemas


def test_studio_pages_are_not_cached(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Studio</title>")
    (tmp_path / "app-abc123.js").write_text("console.log(1)")
    config = fast_config(server={"studio_dir": str(tmp_path)})
    with TestClient(create_app(config)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-cache"
        assert "cache-control" not in client.get("/app-abc123.js").headers
