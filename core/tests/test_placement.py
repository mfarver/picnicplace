"""Picking, aligning, and placing single parts, on the simulated LumenPnP.

The simulator knows where every part really is: picked from its pocket where the nozzle
really was, held off-center and turned as it was picked, and left where it was let go.
So these tests check where the part landed, not what the core believes.
"""

import math
import re
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


def test_alignment_with_a_turned_bottom_camera() -> None:
    """A real LumenPnP's bottom camera was mounted turned a quarter turn, and saw 3.7 times
    the profile's field of view. Calibrating it measures both; bottom vision then undoes
    the turn, so the off-center pick is still found and removed the right way."""
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_home_error": (0.3, -0.2),
            "sim_bottom_camera_turn": 90.0,
            "sim_bottom_camera_scale": 3.7,
        },
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s, slots=("3",), calibrate=False)
        op = ok(s.wait(s.post("/api/v1/machine/bottom-camera", {})))
        assert "image turned +90" in op["message"] or "image turned 90" in op["message"], op
        bottom = next(
            c
            for c in client.get("/api/v1/machine/description").json()["cameras"]
            if c["looking"] == "up"
        )
        assert bottom["image_rotation"] == pytest.approx(90.0, abs=0.5)

        pick = {"slot_id": "3", "nozzle_id": "N1", "feed": False, "pick_correction": "off"}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        alignment = client.get("/api/v1/placement").json()["loads"][0]["alignment"]
        assert math.hypot(alignment["dx"], alignment["dy"]) == pytest.approx(0.447, abs=0.08)
        target = {"nozzle_id": "N1", "x": 150.0, "y": 280.0, "rotation": 30.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        part = placed(client)[-1]
        assert (part.x, part.y) == pytest.approx((150.0, 280.0), abs=0.08)
        assert angle_error(part.angle, 30.0) < 1.0


def test_alignment_with_a_flipped_bottom_camera() -> None:
    """A real LumenPnP's bottom camera picture is flipped (flip_y) to show the machine as
    from above, not mirrored as from below. Taken for a mirrored one, its X seemed turned
    180 degrees and its Y not at all, and calibrating it found a turn of +89.7 degrees:
    the pads were looked for a quarter turn off. Calibrating now finds it flipped and not
    turned, and the part is found and placed."""
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_home_error": (0.3, -0.2),
            "sim_bottom_camera_flipped": True,
        },
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s, slots=("3",), calibrate=False)
        op = ok(s.wait(s.post("/api/v1/machine/bottom-camera", {})))
        assert "flipped to show the machine as from above" in op["message"], op
        assert "Measured with Left nozzle," in op["message"], op
        bottom = next(
            c
            for c in client.get("/api/v1/machine/description").json()["cameras"]
            if c["looking"] == "up"
        )
        assert bottom["image_rotation"] == pytest.approx(0.0, abs=0.5)
        assert bottom["image_mirrored"] is False

        pick = {"slot_id": "3", "nozzle_id": "N1", "feed": False, "pick_correction": "off"}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        alignment = client.get("/api/v1/placement").json()["loads"][0]["alignment"]
        assert math.hypot(alignment["dx"], alignment["dy"]) == pytest.approx(0.447, abs=0.08)
        target = {"nozzle_id": "N1", "x": 150.0, "y": 280.0, "rotation": 30.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        part = placed(client)[-1]
        assert (part.x, part.y) == pytest.approx((150.0, 280.0), abs=0.08)
        assert angle_error(part.angle, 30.0) < 1.0


def test_a_washed_out_part_is_found_by_its_outline() -> None:
    """As on a real LumenPnP at full white: the bottom camera's picture is flipped, and
    the part's tin and white underside are one overexposed rectangle, with no metal to
    fit its pads to. Its outline is its body's: it is found, turned and placed by that."""
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_bottom_camera_flipped": True,
            "sim_bottom_washed_out": True,
        },
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s, slots=("3",), calibrate=False)
        ok(s.wait(s.post("/api/v1/machine/bottom-camera", {})))
        pick = {"slot_id": "3", "nozzle_id": "N1", "feed": False, "pick_correction": "off"}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        op = ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        assert "Found by its body's outline." in op["message"], op["message"]
        target = {"nozzle_id": "N1", "x": 150.0, "y": 280.0, "rotation": 30.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        part = placed(client)[-1]
        assert (part.x, part.y) == pytest.approx((150.0, 280.0), abs=0.08)
        assert angle_error(part.angle, 30.0) < 1.0


@pytest.mark.parametrize("turned", [0.0, 180.0])
def test_a_sot23_is_placed_the_right_way_round(turned: float) -> None:
    """A SOT-23's body looks the same turned 180 degrees; its pads, one on one side and
    two on the other, do not. With its reel's parts turned half way from what the library
    says, the pads show it, the message says so, and it is still placed the right way."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_parts_turned": {"3": turned}},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        library = client.app.state.ctx.library  # type: ignore[attr-defined]
        library.put_footprint(
            Footprint(
                id="SOT-23-T",
                body=Size(x=2.9, y=1.3),
                pads=[
                    Pad(name="1", x=-0.95, y=-1.1, width=0.6, height=0.8),
                    Pad(name="2", x=0.95, y=-1.1, width=0.6, height=0.8),
                    Pad(name="3", x=0.0, y=1.1, width=0.6, height=0.8),
                ],
            )
        )
        part = Part(id="SOT23-PART", footprint_id="SOT-23-T", height=1.0)
        put_taped(library, part, "embossed", 8, 4, 90)
        set_up(client, s, slots=("3",))
        uuid = next(
            slot["feeder"]["uuid"]
            for slot in s.client.get("/api/v1/feeders").json()["slots"]
            if slot["slot"]["id"] == "3"
        )
        r = s.put(f"/api/v1/feeders/assignments/{uuid}", {"part_id": "SOT23-PART"})
        assert r.status_code == 200, r.json()
        pick = {"slot_id": "3", "nozzle_id": "N1", "pick_correction": "off"}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        op = ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        assert ("turned about 180 degrees" in op["message"]) == (turned == 180), op["message"]
        target = {"nozzle_id": "N1", "x": 200.0, "y": 260.0, "rotation": 30.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        landed = placed(client)[-1]
        assert (landed.x, landed.y) == pytest.approx((200.0, 260.0), abs=0.05)
        assert angle_error(landed.angle, 30.0, 360.0) < 0.5


def test_bottom_vision_sets_the_configured_light() -> None:
    """The config gives the bottom camera's ring light a dim blue, as for parts washed out
    at full white: aligning sets it so, and the part is still found."""
    blue = {"red": 0, "green": 0, "blue": 255, "brightness": 60}
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1"},
        cameras={"top": {"source": "synthetic"}, "bottom": {"source": "synthetic", "light": blue}},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s, slots=("3",))
        pick = {"slot_id": "3", "nozzle_id": "N1", "feed": False, "pick_correction": "off"}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        assert s.state()["lights"]["light_bottom"] == blue


def test_a_part_whose_leads_are_off_its_center_lands_on_them(lumen: TestClient) -> None:
    """Three leads 0.8 mm to one side of the body's center, as on a connector, in a tape
    on a rail turned 2 degrees: bottom vision fits the footprint's pads, finds the turn,
    and the part's origin, not its leads' middle, goes where it is sent."""
    feeders(lumen).sim_rail_errors = {"front": (0.0, 0.0, 2.0), "back": (0.0, 0.0, 2.0)}
    with connect(lumen) as s:
        library = lumen.app.state.ctx.library  # type: ignore[attr-defined]
        library.put_footprint(
            Footprint(
                id="LEADS3",
                body=Size(x=3.0, y=1.4),
                pads=[
                    Pad(name=str(i + 1), x=x, y=0.8, width=0.5, height=1.0)
                    for i, x in enumerate((-1.0, 0.0, 1.0))
                ],
            )
        )
        part = Part(id="LEADS3-PART", footprint_id="LEADS3", height=1.0)
        put_taped(library, part, "paper", 8, 4, 90)
        set_up(lumen, s, slots=("3",))
        uuid = next(
            slot["feeder"]["uuid"]
            for slot in s.client.get("/api/v1/feeders").json()["slots"]
            if slot["slot"]["id"] == "3"
        )
        r = s.put(f"/api/v1/feeders/assignments/{uuid}", {"part_id": "LEADS3-PART"})
        assert r.status_code == 200, r.json()
        pick = {"slot_id": "3", "nozzle_id": "N1", "pick_correction": "off"}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        op = ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        assert "pads" not in op["message"], op["message"]
        turned = re.search(r"turned ([-+]\d+\.\d) degrees", op["message"])
        assert turned is not None, op["message"]
        assert float(turned[1]) == pytest.approx(2.0, abs=0.3)
        target = {"nozzle_id": "N1", "x": 200.0, "y": 260.0, "rotation": 30.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        landed = placed(lumen)[-1]
        assert (landed.x, landed.y) == pytest.approx((200.0, 260.0), abs=0.05)
        # Three leads in a row look alike turned 180 degrees; the body does not.
        assert angle_error(landed.angle, 30.0, 360.0) < 0.5


def test_an_empty_pocket_is_fed_past_once(lumen: TestClient) -> None:
    """Paper tape, and a part whose body the library knows: the top camera sees the pick
    pocket empty before a nozzle goes down to it, and the pick feeds once, looks again,
    and takes the part in the next pocket."""
    with connect(lumen) as s:
        set_up(lumen, s)
        pick = {"slot_id": "1", "nozzle_id": "N1", "feed": False, "pick_correction": "top"}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        # The same pocket again without feeding: it is empty, so the pick feeds on.
        again = {"slot_id": "1", "nozzle_id": "N2", "feed": False, "pick_correction": "top"}
        op = ok(s.wait(s.post("/api/v1/placement/pick", again)))
        assert op["message"].startswith(
            "N2 picked RC0805-1K from Slot 1 (fed once past an empty pocket) (pocket "
        ), op
        parts = feeders(lumen).sim_parts
        assert parts is not None
        assert sorted(parts.held) == ["N1", "N2"]


def test_a_second_empty_pocket_stops_the_pick(lumen: TestClient) -> None:
    """The pocket fed to after an empty one is empty too: the feeder is out of parts or
    not advancing, and the pick stops with a feeder error, no nozzle having gone down."""
    with connect(lumen) as s:
        set_up(lumen, s)
        pick = {"slot_id": "1", "nozzle_id": "N1", "feed": False, "pick_correction": "top"}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        top = feeders(lumen).part_top("1")
        # The pocket just emptied, then one more empty one after it.
        feeders(lumen).sim_empty_pockets["1"] = 2
        again = {"slot_id": "1", "nozzle_id": "N2", "feed": False, "pick_correction": "top"}
        op = s.wait(s.post("/api/v1/placement/pick", again))
        assert op["status"] == "failed"
        assert "fed once, and saw the next one empty too" in op["error"], op
        # It never touched the tape, so the part's top is still known.
        assert feeders(lumen).part_top("1") == top
        state = lumen.get("/api/v1/feeders").json()
        pocket = next(x for x in state["slots"] if x["slot"]["id"] == "1")["pocket"]
        assert pocket["part"] is False and pocket["loose"] == []


def test_picking_from_an_empty_pocket_fails(lumen: TestClient) -> None:
    with connect(lumen) as s:
        set_up(lumen, s)
        pick = {"slot_id": "1", "nozzle_id": "N1", "feed": False, "pick_correction": "off"}
        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        # The same pocket again, without feeding or looking: nothing there.
        again = {"slot_id": "1", "nozzle_id": "N2", "feed": False, "pick_correction": "off"}
        op = s.wait(s.post("/api/v1/placement/pick", again))
        assert op["status"] == "failed"
        assert "N2 picked nothing from Slot 1" in op["error"]
        # The part top found on the first pick is forgotten, to be found again.
        assert feeders(lumen).part_top("1") is None
        # N1 still holds its part, so it cannot pick another.
        r = s.post("/api/v1/placement/pick", pick)
        assert r.status_code == 409
        assert "already holds a part" in r.json()["detail"]


def test_a_part_lost_on_the_way_up_is_a_mispick() -> None:
    """N1 seals on its next two parts but drops each as it lifts. Just after the lift the
    vacuum still reads the seal: each pick waits for it to settle, and fails, both going
    straight to the part's known top and finding it by touch. The third pick holds."""
    config = fast_config(machine={"profile": "lumenpnp-v4.1", "sim_drops_on_lift": {"N1": 2}})
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        parts = feeders(client).sim_parts
        assert parts is not None
        # N2 finds the part's top by touch, so N1's first pick goes straight to it.
        ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N2"})))
        pick = {"slot_id": "1", "nozzle_id": "N1"}
        op = s.wait(s.post("/api/v1/placement/pick", pick))
        assert op["status"] == "failed", op
        assert "N1 picked nothing from Slot 1" in op["error"]
        assert "N1" not in parts.held

        # The top is forgotten after a miss: found by touch again, and lost again.
        op = s.wait(s.post("/api/v1/placement/pick", pick))
        assert op["status"] == "failed", op
        assert "N1 sealed at Z" in op["error"] and "lifted nothing" in op["error"]
        assert "N1" not in parts.held
        loads = client.get("/api/v1/placement").json()["loads"]
        assert [load["nozzle"] for load in loads] == ["N2"]

        ok(s.wait(s.post("/api/v1/placement/pick", pick)))
        assert sorted(parts.held) == ["N1", "N2"]


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
