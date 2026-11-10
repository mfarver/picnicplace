"""Profiles that extend other profiles, and the local profile: what is true of one
machine (its discard tray, measured keep-outs), laid over its built-in profile."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.config import Config
from picnicplace.machine.profile import load_profile, merge_profile_data
from tests.conftest import connect, fast_config


def test_lists_of_tables_merge_by_id() -> None:
    base = {
        "name": "A",
        "axes": {"x": 1, "y": 2},
        "locations": [{"id": "a", "x": 1}, {"id": "b", "x": 2}],
        "tags": [1, 2],
    }
    over = {"axes": {"y": 3}, "locations": [{"id": "b", "x": 5}, {"id": "c"}], "tags": [9]}
    merged = merge_profile_data(base, over)
    assert merged == {
        "name": "A",
        "axes": {"x": 1, "y": 3},
        "locations": [{"id": "a", "x": 1}, {"id": "b", "x": 5}, {"id": "c"}],
        "tags": [9],
    }
    assert base["locations"] == [{"id": "a", "x": 1}, {"id": "b", "x": 2}]  # unchanged


def test_a_profile_extends_another(tmp_path: Path) -> None:
    """A later model gives only what differs: here a standard discard tray, and a
    keep-out of the earlier model renamed."""
    path = tmp_path / "later.toml"
    path.write_text(
        'extends = "lumenpnp-v4.1"\n'
        'id = "lumenpnp-later"\n'
        'name = "LumenPnP (later)"\n\n'
        "[[locations]]\n"
        'id = "discard"\nlabel = "Discard tray"\nkind = "discard"\n'
        "position = { x = 150, y = 110 }\n\n"
        "[[keep_outs]]\n"
        'id = "datum"\nlabel = "Datum board, later"\n'
    )
    base = load_profile("lumenpnp-v4.1")
    later = load_profile(str(path))
    assert later.id == "lumenpnp-later" and later.extends == "lumenpnp-v4.1"
    assert [loc.id for loc in later.locations] == [*(loc.id for loc in base.locations), "discard"]
    datum = next(k for k in later.keep_outs if k.id == "datum")
    assert datum.label == "Datum board, later"
    assert datum.area == next(k for k in base.keep_outs if k.id == "datum").area
    assert later.axes == base.axes and later.marlin == base.marlin

    # A chain that comes back to itself, and an extension without its own ID.
    (tmp_path / "a.toml").write_text('extends = "b.toml"\nid = "a"\n')
    (tmp_path / "b.toml").write_text('extends = "a.toml"\nid = "b"\n')
    with pytest.raises(ValueError, match="extends itself"):
        load_profile(str(tmp_path / "a.toml"))
    (tmp_path / "c.toml").write_text('extends = "lumenpnp-v4.1"\n')
    with pytest.raises(ValueError, match="needs its own id"):
        load_profile(str(tmp_path / "c.toml"))


def lumen(data_dir: Path, **machine: object) -> Config:
    return fast_config(
        machine={"profile": "lumenpnp-v4.1", **machine},
        data={"dir": str(data_dir)},
    )


def test_the_discard_location_is_set_where_the_camera_looks(tmp_path: Path) -> None:
    with TestClient(create_app(lumen(tmp_path))) as client, connect(client) as s:
        assert not client.app.state.ctx.placement.can_discard()  # type: ignore[attr-defined]
        r = s.put("/api/v1/machine/locations/discard", {})
        assert r.status_code == 409 and "home the machine" in r.json()["detail"]

        s.home()
        here = client.app.state.ctx.machine.camera_position()  # type: ignore[attr-defined]
        r = s.put("/api/v1/machine/locations/discard", {})
        assert r.status_code == 200, r.json()
        discard = next(loc for loc in r.json()["locations"] if loc["kind"] == "discard")
        assert discard["position"] == pytest.approx({"x": here.x, "y": here.y}, abs=1e-3)
        # At once, without a restart.
        assert client.app.state.ctx.placement.can_discard()  # type: ignore[attr-defined]
        state = client.get("/api/v1/machine/local-profile").json()
        assert state["restart_needed"] is False
        assert state["profile"]["locations"][0]["id"] == "discard"

        r = s.put("/api/v1/machine/locations/park", {"position": {"x": 300, "y": 400}})
        assert r.status_code == 200, r.json()

    # Kept for the next start.
    saved = json.loads((tmp_path / "local-profile.json").read_text())
    assert {loc["kind"] for loc in saved["profile"]["locations"]} == {"discard", "park"}
    with TestClient(create_app(lumen(tmp_path))) as client, connect(client) as s:
        kinds = {
            loc["kind"] for loc in client.get("/api/v1/machine/description").json()["locations"]
        }
        assert {"discard", "park"} <= kinds
        r = s.delete("/api/v1/machine/locations/discard")
        assert r.status_code == 200
        assert "discard" not in {loc["kind"] for loc in r.json()["locations"]}
        assert not client.app.state.ctx.placement.can_discard()  # type: ignore[attr-defined]


def test_the_local_location_takes_the_place_of_the_configs(tmp_path: Path) -> None:
    bin_ = {"id": "bin", "label": "Bin", "kind": "discard", "position": {"x": 150, "y": 110}}
    with TestClient(create_app(lumen(tmp_path, locations=[bin_]))) as client, connect(client) as s:
        r = s.put("/api/v1/machine/locations/discard", {"position": {"x": 160, "y": 112}})
        discards = [loc for loc in r.json()["locations"] if loc["kind"] == "discard"]
        assert discards == [
            {**bin_, "position": {"x": 160, "y": 112}, "z": None, "diameter": None, "style": "dot"}
        ]
        r = s.delete("/api/v1/machine/locations/discard")
        discards = [loc for loc in r.json()["locations"] if loc["kind"] == "discard"]
        assert discards[0]["position"] == {"x": 150, "y": 110}  # the config's again


def test_findings_wait_for_a_restart(tmp_path: Path) -> None:
    keep_out = {
        "id": "datum",
        "label": "Datum board (measured)",
        "area": {"x_min": 176, "y_min": 95, "x_max": 258, "y_max": 132},
    }
    with TestClient(create_app(lumen(tmp_path))) as client, connect(client) as s:
        r = s.put("/api/v1/machine/local-profile", {"keep_outs": [keep_out]})
        assert r.status_code == 200, r.json()
        assert r.json()["restart_needed"] is True
        description = client.get("/api/v1/machine/description").json()
        assert next(k for k in description["keep_outs"] if k["id"] == "datum")["label"] == (
            "Datum board"
        )
        bad = {**keep_out, "area": {"x_min": 300, "y_min": 95, "x_max": 258, "y_max": 132}}
        r = s.put("/api/v1/machine/local-profile", {"keep_outs": [bad]})
        assert r.status_code == 422

    with TestClient(create_app(lumen(tmp_path))) as client:
        description = client.get("/api/v1/machine/description").json()
        datum = next(k for k in description["keep_outs"] if k["id"] == "datum")
        assert datum == keep_out
        assert client.get("/api/v1/machine/local-profile").json()["restart_needed"] is False
