"""Picking, aligning, and placing single parts, on the simulated LumenPnP.

The simulator knows where every part really is: picked from its pocket where the nozzle
really was, held off-center and turned as it was picked, and left where it was let go.
So these tests check where the part landed, not what the core believes.
"""

import math
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.feeders.service import FeederService
from picnicplace.library import Footprint, Pad, Part, Size
from picnicplace.simparts import PlacedPart
from tests.conftest import Session, connect, fast_config, put_taped


@pytest.fixture
def lumen() -> Iterator[TestClient]:
    config = fast_config(machine={"profile": "lumenpnp-v4.1", "sim_home_error": (0.3, -0.2)})
    with TestClient(create_app(config)) as c:
        yield c


def feeders(client: TestClient) -> FeederService:
    return client.app.state.ctx.feeders  # type: ignore[attr-defined,no-any-return]


def placed(client: TestClient) -> list[PlacedPart]:
    parts = feeders(client).sim_parts
    assert parts is not None
    return parts.placed


def ok(op: dict[str, Any]) -> dict[str, Any]:
    assert op["status"] == "succeeded", op
    return op


def set_up(
    client: TestClient, s: Session, slots: tuple[str, ...] = ("1", "2"), calibrate: bool = True
) -> None:
    library = client.app.state.ctx.library  # type: ignore[attr-defined]
    library.put_footprint(
        Footprint(
            id="R0805",
            body=Size(x=2.0, y=1.25),
            pads=[
                Pad(name="1", x=-0.95, y=0, width=0.9, height=1.35),
                Pad(name="2", x=0.95, y=0, width=0.9, height=1.35),
            ],
        )
    )
    put_taped(library, Part(id="RC0805-1K", footprint_id="R0805", height=0.5), "paper", 8, 4, 90)
    s.home()
    ok(s.wait(s.post("/api/v1/feeders/scan")))
    state = s.client.get("/api/v1/feeders").json()
    for slot in state["slots"]:
        if slot["slot"]["id"] in slots:
            uuid = slot["feeder"]["uuid"]
            r = s.put(f"/api/v1/feeders/assignments/{uuid}", {"part_id": "RC0805-1K"})
            assert r.status_code == 200, r.json()
    if calibrate:
        ok(s.wait(s.post("/api/v1/feeders/calibrate", {"slot_ids": list(slots)})))


def angle_error(a: float, b: float, period: float = 180.0) -> float:
    """Difference of two part angles; a two-pad part looks the same turned 180 degrees."""
    d = math.fmod(a - b, period)
    return min(abs(d), period - abs(d))


def test_pick_align_place_step_by_step(lumen: TestClient) -> None:
    with connect(lumen) as s:
        set_up(lumen, s)
        op = ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N1"})))
        assert "N1 picked RC0805-1K from Slot 1" in op["message"]
        load = lumen.get("/api/v1/placement").json()["loads"][0]
        assert load["nozzle"] == "N1" and load["alignment"] is None

        op = ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        assert "check the part" not in op["message"], op["message"]
        load = lumen.get("/api/v1/placement").json()["loads"][0]
        assert load["alignment"] is not None

        target = {"nozzle_id": "N1", "x": 300.0, "y": 250.0, "rotation": 45.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        assert lumen.get("/api/v1/placement").json()["loads"] == []
        part = placed(lumen)[-1]
        assert (part.x, part.y) == pytest.approx((300.0, 250.0), abs=0.05)
        assert angle_error(part.angle, 45.0) < 0.5


def test_pick_and_place_with_the_other_nozzle(lumen: TestClient) -> None:
    with connect(lumen) as s:
        set_up(lumen, s)
        body = {"slot_id": "2", "nozzle_id": "N2", "x": 260.0, "y": 230.0, "rotation": -30.0}
        op = ok(s.wait(s.post("/api/v1/placement/pick-and-place", body)))
        assert "N2 placed its part at (260.000, 230.000)" in op["message"]
        part = placed(lumen)[-1]
        assert (part.x, part.y) == pytest.approx((260.0, 230.0), abs=0.05)
        assert angle_error(part.angle, -30.0) < 0.5


def test_alignment_corrects_an_off_center_pick(lumen: TestClient) -> None:
    """Picked blind from an uncalibrated slot, the part sits about 0.45 mm off the nozzle
    (the simulator's error for slot 3); bottom vision must find and remove that."""
    with connect(lumen) as s:
        set_up(lumen, s, slots=("3",), calibrate=False)
        pick = {"slot_id": "3", "nozzle_id": "N1", "feed": False, "pick_correction": "off"}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        op = ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        alignment = lumen.get("/api/v1/placement").json()["loads"][0]["alignment"]
        assert math.hypot(alignment["dx"], alignment["dy"]) == pytest.approx(0.447, abs=0.05)
        assert "0.4" in op["message"]
        target = {"nozzle_id": "N1", "x": 150.0, "y": 280.0, "rotation": 180.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        part = placed(lumen)[-1]
        assert (part.x, part.y) == pytest.approx((150.0, 280.0), abs=0.05)
        assert angle_error(part.angle, 180.0) < 0.5


def test_picking_from_an_empty_pocket_fails(lumen: TestClient) -> None:
    with connect(lumen) as s:
        set_up(lumen, s)
        pick = {"slot_id": "1", "nozzle_id": "N1", "feed": False}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        # The same pocket again, without feeding: nothing there.
        again = {"slot_id": "1", "nozzle_id": "N2", "feed": False}
        op = s.wait(s.post("/api/v1/placement/pick", again))
        assert op["status"] == "failed"
        assert "N2 picked nothing from Slot 1" in op["error"]
        # The part top found on the first pick is forgotten, to be found again.
        assert feeders(lumen).part_top("1") is None
        # N1 still holds its part, so it cannot pick another.
        r = s.post("/api/v1/placement/pick", pick)
        assert r.status_code == 409
        assert "already holds a part" in r.json()["detail"]


def test_the_first_pick_finds_the_parts_top_by_touch() -> None:
    """The parts in the feeders sit 0.4 mm lower than the profile's pick height, too low
    for a pick there to reach them. The first pick from a feeder steps down onto its part
    until the vacuum seals and keeps that height with the feeder; later picks go straight
    to it, with either nozzle."""
    config = fast_config(machine={"profile": "lumenpnp-v4.1", "sim_part_height_error": -0.4})
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        f = feeders(client)
        expected = f.bank(f.slot("1")).pick_height
        assert expected is not None
        op = ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N1"})))
        # The simulated vacuum takes a part from 0.25 mm above it.
        assert "found the part's top at" in op["message"], op["message"]
        assert "by touch, -0.20 mm" in op["message"], op["message"]
        assert f.part_top("1") == pytest.approx(expected - 0.2)

        op = ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N2"})))
        assert "by touch" not in op["message"]
        parts = f.sim_parts
        assert parts is not None and parts.crashes == []
        assert sorted(parts.held) == ["N1", "N2"]


def test_without_sniffing_a_low_part_is_missed() -> None:
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_part_height_error": -0.4,
            "sniff_heights": False,
        }
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        op = s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N1"}))
        assert op["status"] == "failed" and "picked nothing" in op["error"]
        assert feeders(client).part_top("1") is None


def test_place_needs_alignment_and_discard_needs_a_bin(lumen: TestClient) -> None:
    with connect(lumen) as s:
        set_up(lumen, s)
        ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N1"})))
        r = s.post("/api/v1/placement/place", {"nozzle_id": "N1", "x": 300, "y": 250})
        assert r.status_code == 409 and "align the part" in r.json()["detail"]
        r = s.post("/api/v1/placement/discard", {"nozzle_id": "N1"})
        assert r.status_code == 409 and "no discard location" in r.json()["detail"]


def test_captures_are_saved_with_the_machine_state(lumen: TestClient) -> None:
    with connect(lumen) as s:
        s.home()
        r = s.post("/api/v1/cameras/captures", {"label": "datum, light off"})
        assert r.status_code == 200, r.json()
        saved = r.json()
        assert {c["camera"] for c in saved} == {"top", "bottom"}
        top = next(c for c in saved if c["camera"] == "top")
        assert top["label"] == "datum, light off" and top["homed"]
        assert top["position"]["X"] == pytest.approx(217.0, abs=0.05)
        assert top["id"].endswith("-top-datum-light-off")
        listed = lumen.get("/api/v1/cameras/captures").json()
        assert {c["id"] for c in listed} >= {c["id"] for c in saved}
        image = lumen.get(f"/api/v1/cameras/captures/{top['id']}.png")
        assert image.status_code == 200
        assert image.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert lumen.get("/api/v1/cameras/captures/../../etc/passwd.png").status_code == 404
