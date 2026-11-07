"""Machine description, simulated I/O, and the safe-Z state."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app

from .conftest import fast_config, operation_done, receive_until, wait_for

PROFILE = Path(__file__).parents[1] / "src/picnicplace/profiles/lumenpnp-v4.toml"


def move(client: TestClient, **position: float) -> None:
    r = client.post("/api/v1/machine/move", json={"position": position})
    assert r.status_code == 202, r.json()
    wait_for(
        lambda: client.get(f"/api/v1/operations/{r.json()['id']}").json()["status"] != "running"
    )


def set_output(client: TestClient, output_id: str, on: bool) -> int:
    return client.put(f"/api/v1/machine/outputs/{output_id}", json={"on": on}).status_code


def state(client: TestClient) -> dict[str, Any]:
    return client.get("/api/v1/machine").json()


def test_description(client: TestClient) -> None:
    body = client.get("/api/v1/machine/description").json()
    assert body["id"] == "lumenpnp-v4"
    assert [n["id"] for n in body["nozzles"]] == ["N1", "N2"]
    assert {c["id"] for c in body["cameras"]} == {"top", "bottom"}
    assert body["safe_z_input"] is None


def test_io_in_machine_state(client: TestClient) -> None:
    s = client.get("/api/v1/machine").json()
    assert s["inputs"]["vac_n1"] == 0
    assert s["outputs"] == {
        "pump_n1": False,
        "valve_n1": False,
        "pump_n2": False,
        "valve_n2": False,
        "light_top": False,
        "light_bottom": False,
    }
    assert s["z_safe"] is True


def test_outputs_require_a_client_to_turn_on(client: TestClient) -> None:
    assert set_output(client, "light_top", True) == 409
    assert set_output(client, "light_top", False) == 200
    with client.websocket_connect("/api/v1/events") as ws:
        ws.receive_json()
        assert set_output(client, "light_top", True) == 200
        assert state(client)["outputs"]["light_top"] is True
        assert set_output(client, "nope", True) == 404


def test_simulated_vacuum_and_pick(client: TestClient) -> None:
    with client.websocket_connect("/api/v1/events") as ws:
        ws.receive_json()
        op_id = client.post("/api/v1/machine/home").json()["id"]
        receive_until(ws, operation_done(op_id))

        def vac() -> float:
            return state(client)["inputs"]["vac_n1"]

        # Suction with an empty nozzle settles at the open-nozzle level.
        set_output(client, "pump_n1", True)
        set_output(client, "valve_n1", True)
        wait_for(lambda: 10 < vac() < 15)

        # Lowering the nozzle with suction on picks a part; vacuum rises past threshold.
        move(client, Z=-10)
        assert state(client)["z_safe"] is False
        wait_for(lambda: vac() > 50)
        move(client, Z=0)
        assert state(client)["z_safe"] is True
        assert vac() > 50

        # Closing the valve drops the part.
        set_output(client, "valve_n1", False)
        wait_for(lambda: vac() == 0)


def test_estop_turns_outputs_off(client: TestClient) -> None:
    with client.websocket_connect("/api/v1/events") as ws:
        ws.receive_json()
        set_output(client, "pump_n2", True)
        client.post("/api/v1/machine/estop")
        assert state(client)["outputs"]["pump_n2"] is False
        assert set_output(client, "pump_n2", True) == 409


@pytest.fixture
def sensed_client(tmp_path: Path) -> TestClient:
    text = PROFILE.read_text().replace(
        "safe_z = -1.0",
        'safe_z = -1.0\nsafe_z_input = "safe_z"',
    )
    text += '\n[[inputs]]\nid = "safe_z"\nlabel = "Safe Z"\nkind = "digital"\nrole = "safe_z"\n'
    profile = tmp_path / "sensed.toml"
    profile.write_text(text)
    return TestClient(create_app(fast_config(machine={"profile": str(profile)})))


def test_sensed_safe_z(sensed_client: TestClient) -> None:
    with sensed_client as client, client.websocket_connect("/api/v1/events") as ws:
        ws.receive_json()
        op_id = client.post("/api/v1/machine/home").json()["id"]
        receive_until(ws, operation_done(op_id))
        assert client.get("/api/v1/machine/description").json()["safe_z_input"] == "safe_z"
        assert state(client)["inputs"]["safe_z"] is True
        move(client, Z=8)
        s = state(client)
        assert s["inputs"]["safe_z"] is False
        assert s["z_safe"] is False
