"""The setup wizard's choices: kept in the data directory, laid under the config file
(which wins), probing a serial port without moving anything, and restarting to use them."""

import threading
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.bringup import BringUpRecord
from picnicplace.calibration import CalibrationChecklist
from picnicplace.machine.marlin.fake import FakeMarlinPty, FakeSettings
from picnicplace.machine.profile import load_profile
from picnicplace.machine_setup import CameraChoice, MachineSetup, SetupStore, layer, load_layered
from picnicplace.wizard import SetupFacts, WizardRecord, build_pages
from tests.conftest import connect, fast_config


def pages(client: TestClient) -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in client.get("/api/v1/wizard").json()["pages"]}


def test_the_config_file_wins_over_the_wizard() -> None:
    raw = {"machine": {"driver": "simulator"}, "cameras": {"top": {"source": "synthetic"}}}
    setup = MachineSetup(
        profile="lumenpnp-v4.1",
        driver="marlin",
        serial_port="/dev/ttyACM0",
        cameras={"top": CameraChoice(device="/dev/video0"), "bottom": CameraChoice()},
    )
    data, pinned = layer(raw, setup)
    assert pinned == {"machine.driver", "cameras.top"}
    assert data["machine"] == {
        "driver": "simulator",
        "profile": "lumenpnp-v4.1",
        "marlin": {"port": "/dev/ttyACM0"},
    }
    assert data["cameras"] == {"top": {"source": "synthetic"}, "bottom": {"source": "none"}}
    assert raw == {"machine": {"driver": "simulator"}, "cameras": {"top": {"source": "synthetic"}}}


def test_the_core_starts_with_the_wizards_choices(tmp_path: Path) -> None:
    toml = tmp_path / "picnicd.toml"
    toml.write_text(f'[data]\ndir = "{tmp_path}"\n\n[machine]\nspeed_scale = 0.5\n')
    store = SetupStore(tmp_path / "machine-setup.json")
    store.put(
        MachineSetup(
            profile="lumenpnp-v4.1", cameras={"top": CameraChoice(device="2", flip_x=True)}
        )
    )
    layered = load_layered(toml)
    assert layered.warning is None and layered.pinned == frozenset()
    assert layered.config.machine.profile == "lumenpnp-v4.1"
    assert layered.config.machine.speed_scale == 0.5
    top = layered.config.cameras["top"]
    assert (top.source, top.device, top.flip_x) == ("device", "2", True)

    # The command line pins what it sets, as the file does.
    layered = load_layered(toml, {"machine.profile": "lumenpnp-v4.2"})
    assert layered.config.machine.profile == "lumenpnp-v4.2"
    assert layered.pinned == frozenset({"machine.profile"})

    # A choice the config cannot take is set aside, saying why.
    store.put(MachineSetup(profile="lumenpnp-v4.1", cameras={"side": CameraChoice(device="3")}))
    layered = load_layered(toml)
    assert layered.warning and "machine-setup.json" in layered.warning
    assert "side" not in layered.config.cameras and "top" not in layered.config.cameras


def test_choosing_a_machine_and_restarting(tmp_path: Path) -> None:
    """Nothing chosen, the machine page is to do and the core runs the simulator. A choice
    is kept, and the core says it must restart to use it; it restarts itself when picnicd
    started it, and otherwise says to restart it."""
    config = fast_config(data={"dir": str(tmp_path)})
    restarted = threading.Event()
    with TestClient(create_app(config, restart=restarted.set)) as client, connect(client) as s:
        machine = pages(client)["machine"]
        assert machine["status"] == "todo" and "Choose your machine" in machine["detail"]
        state = client.get("/api/v1/setup").json()
        assert {m["id"] for m in state["machines"]} >= {"lumenpnp-v4.1", "lumenpnp-v4.2"}
        assert not state["restart_needed"] and state["can_restart"]

        r = s.put("/api/v1/setup", {"profile": "lumenpnp-v9", "driver": "simulator"})
        assert r.status_code == 422 and "no built-in machine 'lumenpnp-v9'" in r.json()["detail"]
        side = {"side": {"device": "1"}}
        r = s.put("/api/v1/setup", {"profile": "lumenpnp-v4.1", "cameras": side})
        assert r.status_code == 422 and "has no camera side" in r.json()["detail"]

        choice = {"profile": "lumenpnp-v4.1", "driver": "simulator"}
        r = s.put("/api/v1/setup", choice)
        assert r.status_code == 200, r.json()
        assert r.json()["restart_needed"]
        machine = pages(client)["machine"]
        assert machine["status"] == "todo" and "Restart the core" in machine["detail"]

        # Only the client holding control changes the setup.
        with connect(client, "other") as other:
            assert other.put("/api/v1/setup", choice).status_code == 409
            assert other.post("/api/v1/setup/restart").status_code == 409

        r = s.post("/api/v1/setup/restart")
        assert r.status_code == 202, r.json()
        assert restarted.wait(2.0)

    assert SetupStore(config.data.setup_path).get().profile == "lumenpnp-v4.1"
    # As picnicd starts it again: the choice is used, and the machine page is done.
    layered = load_layered(None, {"data.dir": str(tmp_path)})
    assert layered.config.machine.profile == "lumenpnp-v4.1"
    with TestClient(create_app(layered.config, pinned=layered.pinned)) as client:
        machine = pages(client)["machine"]
        assert machine["status"] == "done" and "chosen here" in machine["detail"]
        assert not client.get("/api/v1/setup").json()["restart_needed"]
        with connect(client) as s:
            r = s.post("/api/v1/setup/restart")
            assert r.status_code == 409 and "cannot restart itself" in r.json()["detail"]


def test_probing_a_serial_port_moves_nothing(tmp_path: Path) -> None:
    """The fake firmware on a pseudo-terminal stands in for the machine: the probe reads
    what it is and its vacuum sensors, and keeps the answer with the setup."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "marlin": {"connect_wait_s": 0}},
        data={"dir": str(tmp_path)},
    )
    fake = FakeMarlinPty(FakeSettings())
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert client.portal is not None
        port = client.portal.call(fake.start)
        try:
            r = s.post("/api/v1/setup/probe", {"serial_port": port})
            assert r.status_code == 200, r.json()
            result = r.json()
            assert result["firmware"] and "Marlin" in result["firmware"]
            assert result["vacuum"], result
            words = {line.split()[0] for line in fake.firmware.received if line.strip()}
            assert not words & {"G0", "G1", "G28", "M3", "M106"}, words
            assert client.get("/api/v1/setup").json()["saved"]["probe"]["serial_port"] == port
        finally:
            client.portal.call(fake.stop)
        r = s.post("/api/v1/setup/probe", {"serial_port": str(tmp_path / "no-such-port")})
        assert r.status_code == 422 and "could not probe" in r.json()["detail"]


def test_a_real_machines_setup_pages() -> None:
    profile = load_profile("lumenpnp-v4.1")
    empty = (profile, CalibrationChecklist(lines=[]), BringUpRecord(), WizardRecord())

    def status(facts: SetupFacts) -> dict[str, str]:
        return {p.id: p.status for p in build_pages(*empty, facts) if p.part == 1}

    simulator = status(SetupFacts(chosen="none"))
    assert simulator["machine"] == "todo"
    assert simulator["connect"] == simulator["cameras"] == "not_needed"

    offline = SetupFacts(chosen="wizard", driver="marlin", serial_port="/dev/ttyACM0")
    assert status(offline)["machine"] == "done"
    assert status(offline)["connect"] == "todo"
    online = SetupFacts(
        chosen="wizard",
        driver="marlin",
        serial_port="/dev/ttyACM0",
        connected=True,
        firmware="Marlin 2.1",
        cameras_chosen=False,
    )
    assert status(online)["connect"] == "done"
    assert status(online)["cameras"] == "todo"
