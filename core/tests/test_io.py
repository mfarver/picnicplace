"""Machine description, simulated I/O, and the safe-Z state."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app

from .conftest import TEST_PROFILE, connect, fast_config, wait_for


def test_description(client: TestClient) -> None:
    body = client.get("/api/v1/machine/description").json()
    assert body["id"] == "test-machine"
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


def test_outputs(client: TestClient) -> None:
    url = "/api/v1/machine/outputs/light_top"
    assert client.put(url, json={"on": True}).json()["code"] == "client_required"
    with connect(client) as s:
        assert s.put(url, {"on": True}).status_code == 200
        assert s.state()["outputs"]["light_top"] is True
        assert s.put(url, {"on": False}).status_code == 200
        assert s.put("/api/v1/machine/outputs/nope", {"on": True}).status_code == 404


def test_simulated_vacuum_and_pick(client: TestClient) -> None:
    with connect(client) as s:
        s.home()

        def vac() -> float:
            return s.state()["inputs"]["vac_n1"]

        # Suction with an empty nozzle settles at the open-nozzle level.
        s.put("/api/v1/machine/outputs/pump_n1", {"on": True})
        s.put("/api/v1/machine/outputs/valve_n1", {"on": True})
        wait_for(lambda: 20 < vac() < 30)  # below the 30 kPa pick threshold

        # Lowering the nozzle with suction on picks a part; vacuum rises past threshold.
        s.move(Z=-10)
        assert s.state()["z_safe"] is False
        wait_for(lambda: vac() > 50)
        s.move(Z=0)
        assert s.state()["z_safe"] is True
        assert vac() > 50

        # Closing the valve drops the part.
        s.put("/api/v1/machine/outputs/valve_n1", {"on": False})
        wait_for(lambda: vac() == 0)


def test_estop_turns_outputs_off(client: TestClient) -> None:
    with connect(client) as s:
        s.put("/api/v1/machine/outputs/pump_n2", {"on": True})
        client.post("/api/v1/machine/estop")
        assert s.state()["outputs"]["pump_n2"] is False
        assert s.put("/api/v1/machine/outputs/pump_n2", {"on": True}).status_code == 409


# --- Safe Z ------------------------------------------------------------------------------


def test_move_travels_at_safe_z(client: TestClient) -> None:
    with connect(client) as s:
        s.home()
        s.move(Z=-12)
        # A move to X/Y with a nozzle down raises Z first, then travels, then lowers.
        z_during_travel: list[float] = []
        r = s.post("/api/v1/machine/move", {"position": {"X": 50, "Y": 40, "Z": -5}})
        op_id = r.json()["id"]
        while True:
            msg = s.ws.receive_json()
            if msg["type"] == "machine.state":
                pos = msg["machine"]["position"]
                if 0 < pos["X"] < 50:
                    z_during_travel.append(pos["Z"])
            elif msg["type"] == "operation" and msg["operation"]["id"] == op_id:
                status = msg["operation"]["status"]
                if status != "running":
                    assert status == "succeeded"
                    break
        assert z_during_travel
        assert all(z == pytest.approx(0) for z in z_during_travel)
        pos = s.state()["position"]
        assert (pos["X"], pos["Y"], pos["Z"]) == pytest.approx((50, 40, -5))


def test_jog(client: TestClient) -> None:
    with connect(client) as s:
        s.home()
        jog = "/api/v1/machine/jog"
        assert s.wait(s.post(jog, {"axis": "X", "distance": 10}))["status"] == "succeeded"
        assert s.state()["position"]["X"] == pytest.approx(10)
        # Clamped to the soft limit rather than refused.
        s.wait(s.post(jog, {"axis": "X", "distance": -100}))
        assert s.state()["position"]["X"] == pytest.approx(0)
        # X/Y jogs are refused while a nozzle is down; Z and rotation are not.
        s.wait(s.post(jog, {"axis": "Z", "distance": -5}))
        assert s.post(jog, {"axis": "Y", "distance": 5}).json()["code"] == "z_not_safe"
        assert s.wait(s.post(jog, {"axis": "A", "distance": 90}))["status"] == "succeeded"
        # A is clamped to its limits; B is continuous and turns past them.
        s.wait(s.post(jog, {"axis": "A", "distance": 1000}))
        assert s.state()["position"]["A"] == pytest.approx(360)
        s.wait(s.post(jog, {"axis": "B", "distance": 1000}))
        assert s.state()["position"]["B"] == pytest.approx(1000)
        assert s.post(jog, {"axis": "Q", "distance": 1}).status_code == 422


@pytest.fixture
def sensed_client(tmp_path: Path) -> Iterator[TestClient]:
    """The test machine with a physical safe-Z sensor added."""
    text = TEST_PROFILE.read_text().replace(
        "safe_z = -1.0",
        'safe_z = -1.0\nsafe_z_input = "safe_z"',
    )
    text += '\n[[inputs]]\nid = "safe_z"\nlabel = "Safe Z"\nkind = "digital"\nrole = "safe_z"\n'
    profile = tmp_path / "sensed.toml"
    profile.write_text(text)
    with TestClient(create_app(fast_config(machine={"profile": str(profile)}))) as c:
        yield c


def test_sensed_safe_z(sensed_client: TestClient) -> None:
    with connect(sensed_client) as s:
        s.home()
        assert s.client.get("/api/v1/machine/description").json()["safe_z_input"] == "safe_z"
        assert s.state()["inputs"]["safe_z"] is True
        s.move(Z=8)
        state = s.state()
        assert state["inputs"]["safe_z"] is False
        assert state["z_safe"] is False


def test_lost_z_steps_fault(sensed_client: TestClient) -> None:
    with connect(sensed_client) as s:
        s.home()
        # Z loses steps: commanded Z still says safe, but the nozzle is really low.
        s.client.app.state.ctx.machine.driver.inject_z_slip("Z", -5)  # type: ignore[attr-defined]
        state = s.state()
        assert state["z_safe"] is False
        op = s.move(X=20)
        assert op["status"] == "failed"
        assert "lost steps" in op["error"]
        assert s.state()["homed"] is False
        # Homing clears the fault.
        s.home()
        assert s.move(X=20)["status"] == "succeeded"


# --- Control lease -----------------------------------------------------------------------


def test_lease(client: TestClient) -> None:
    with connect(client, "Studio") as a, connect(client, "Panel") as b:
        assert a.hello["lease"] == {"holder": None}
        # The first client to command motion takes the free lease.
        a.home()
        assert client.get("/api/v1/lease").json()["holder"]["name"] == "Studio"

        jog = {"axis": "X", "distance": 5}
        r = b.post("/api/v1/machine/jog", jog)
        assert r.status_code == 409
        assert r.json()["code"] == "lease_held"
        assert "Studio" in r.json()["detail"]
        assert b.post("/api/v1/lease", {}).json()["code"] == "lease_held"

        # Taking control needs force; then the other client is refused.
        assert b.post("/api/v1/lease", {"force": True}).json()["holder"]["name"] == "Panel"
        assert b.wait(b.post("/api/v1/machine/jog", jog))["status"] == "succeeded"
        assert a.post("/api/v1/machine/jog", jog).json()["code"] == "lease_held"

        # E-stop never needs the lease.
        assert a.post("/api/v1/machine/estop").json()["estop"] is True

        assert b.delete("/api/v1/lease").json() == {"holder": None}
        assert a.post("/api/v1/machine/reset").status_code == 200


def test_lease_released_when_holder_leaves(client: TestClient) -> None:
    with connect(client, "Watcher") as watcher:
        with connect(client, "Studio") as s:
            s.home()
            r = s.post("/api/v1/machine/move", {"position": {"X": 400}, "speed": 1})
            op_id = r.json()["id"]
        wait_for(lambda: client.get("/api/v1/lease").json() == {"holder": None})
        wait_for(lambda: client.get(f"/api/v1/operations/{op_id}").json()["status"] == "failed")
        assert watcher.state()["busy"] is False


def test_speed_scale_caps_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    from picnicplace.machine.simulator import SimulatorDriver

    speeds: list[float | None] = []
    original = SimulatorDriver.move_to

    async def recording(self: SimulatorDriver, target: dict[str, float], speed: float | None):
        speeds.append(speed)
        await original(self, target, speed)

    monkeypatch.setattr(SimulatorDriver, "move_to", recording)
    config = fast_config(machine={"speed_scale": 0.25})
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert s.state()["speed_scale"] == 0.25
        s.home()
        s.move(X=100)  # test machine: X max 500 mm/s
        s.wait(s.post("/api/v1/machine/jog", {"axis": "Z", "distance": -2}))  # Z max 100
        r = s.post("/api/v1/machine/move", {"position": {"X": 10}, "speed": 50})
        s.wait(r)
    # X at a quarter of 500; Z at a quarter of 100; the last move first raises Z (N1 was
    # below safe Z), then moves X at the requested 50, under its 125 cap.
    assert speeds == [125, 25, 25, 50]


def test_jog_from_outside_the_soft_limits(tmp_path: Path) -> None:
    """Like a LumenPnP's Y, which homes at 0 below its 44.45 mm minimum."""
    text = TEST_PROFILE.read_text().replace(
        'name = "Y"\nmin = 0\nmax = 400', 'name = "Y"\nmin = 40\nmax = 400'
    )
    profile = tmp_path / "low-home.toml"
    profile.write_text(text)
    with (
        TestClient(create_app(fast_config(machine={"profile": str(profile)}))) as client,
        connect(client) as s,
    ):
        s.home()
        jog = "/api/v1/machine/jog"
        # Away from the range: no move at all (not a jump to the limit).
        s.wait(s.post(jog, {"axis": "Y", "distance": -1}))
        assert s.state()["position"]["Y"] == pytest.approx(0)
        # Toward it: the jog happens as asked.
        s.wait(s.post(jog, {"axis": "Y", "distance": 5}))
        assert s.state()["position"]["Y"] == pytest.approx(5)


def test_raising_z_with_unchanged_xy_is_allowed(client: TestClient) -> None:
    with connect(client) as s:
        s.home()
        s.move(X=50, Y=50)
        s.move(Z=-10)  # N1 below safe Z
        # A full-position move that only raises Z must not be refused as X/Y travel.
        assert s.move(X=50, Y=50, Z=0)["status"] == "succeeded"


def test_firmware_halt_latches_estop() -> None:
    config = fast_config(machine={"driver": "fake-marlin", "profile": "lumenpnp-v4.1"})
    with TestClient(create_app(config)) as client, connect(client) as s:
        s.home()
        driver = client.app.state.ctx.machine.driver  # type: ignore[attr-defined]
        # A kill() the firmware reports by itself, not one the core asked for.
        driver._on_line("Error:Printer halted. kill() called!")  # pyright: ignore[reportPrivateUsage]
        wait_for(lambda: s.state()["estop"] is True)
        state = s.state()
        assert state["homed"] is False
        assert s.post("/api/v1/machine/reset").status_code == 200
        assert s.state()["estop"] is False
