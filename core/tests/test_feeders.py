"""Photon feeders: the protocol, simulated feeders, and the feeder service end to end."""

import asyncio
import math
from collections.abc import Iterator
from itertools import pairwise
from typing import Any

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.feeders.photon import (
    Command,
    Packet,
    PhotonBus,
    crc8,
    format_uuid,
)
from picnicplace.feeders.pocket import pocket_from_holes
from picnicplace.feeders.service import (
    FeederService,
    rotate,
    sim_slot_error,
)
from picnicplace.feeders.sim import SimulatedPhotonBus, simulated_uuid
from picnicplace.library import Footprint, Pad, Part, Size, Tape
from picnicplace.machine.errors import FeederError
from picnicplace.machine.profile import Point
from tests.conftest import Session, connect, fast_config

# --- Protocol ------------------------------------------------------------------------


def test_checksum_matches_the_protocol_description() -> None:
    # The GET_FEEDER_ID example in Opulo's protocol description: checksum 0xC0.
    assert crc8(bytes([0x02, 0x00, 0x07, 0x01, 0x01])) == 0xC0
    packet = Packet(2, 0, 7, bytes([Command.GET_FEEDER_ID])).encode()
    assert packet.hex().upper() == "02000701C001"


def test_packets_round_trip_and_reject_damage() -> None:
    packet = Packet(0, 5, 200, bytes([0]) + simulated_uuid(5))
    raw = packet.encode()
    assert Packet.decode(raw) == packet
    damaged = raw[:-1] + bytes([raw[-1] ^ 1])
    with pytest.raises(FeederError, match="checksum"):
        Packet.decode(damaged)
    with pytest.raises(FeederError, match="length"):
        Packet.decode(raw[:-1])


def bus_with(*addresses: int, **feeder: Any) -> tuple[PhotonBus, SimulatedPhotonBus]:
    sim = SimulatedPhotonBus.with_feeders(list(addresses), time_scale=100)
    for f in sim.feeders.values():
        for key, value in feeder.items():
            setattr(f, key, value)

    async def exchange(packet: bytes) -> bytes | None:
        return sim.handle(packet)

    return PhotonBus(exchange, poll_interval_s=0.001), sim


def test_find_initialize_and_feed() -> None:
    async def scenario() -> None:
        bus, sim = bus_with(3, step_scale=1.0)
        assert await bus.feeder_id(2) is None
        uuid = format_uuid(simulated_uuid(3))
        assert await bus.feeder_id(3) == uuid
        with pytest.raises(FeederError, match="not been initialized"):
            await bus.feed(3, 4)
        await bus.initialize(3, uuid)
        await bus.feed(3, 4)
        assert sim.feeders[3].fed_mm == pytest.approx(4.0)
        # Longer than one command can carry (25.5 mm): sent in steps.
        await bus.feed(3, 30)
        assert sim.feeders[3].fed_mm == pytest.approx(34.0)
        await bus.feed(3, -2)
        assert sim.feeders[3].fed_mm == pytest.approx(32.0)
        assert await bus.version(3) == 1

    asyncio.run(scenario())


def test_wrong_feeder_and_jams_are_reported() -> None:
    async def scenario() -> None:
        bus, sim = bus_with(4, jammed=True)
        with pytest.raises(FeederError, match="different feeder"):
            await bus.initialize(4, format_uuid(simulated_uuid(9)))
        await bus.initialize(4, format_uuid(simulated_uuid(4)))
        with pytest.raises(FeederError, match="could not reach"):
            await bus.feed(4, 4)
        assert sim.feeders[4].fed_mm == 0

    asyncio.run(scenario())


def test_broadcast_commands() -> None:
    async def scenario() -> None:
        bus, sim = bus_with(1, 2)
        uuid = format_uuid(simulated_uuid(2))
        assert await bus.find_address(uuid) == 2
        assert await bus.identify(uuid)
        assert sim.feeders[2].identified == 1
        await bus.program_floor(uuid, 7)
        assert await bus.find_address(uuid) == 7
        assert await bus.feeder_id(7) == uuid
        with pytest.raises(FeederError):
            await bus.program_floor(uuid, 1)  # slot 1's address is taken

    asyncio.run(scenario())


# --- Pockets -------------------------------------------------------------------------


def holes_for(along: float, across: float, pocket_line: float = 3.5) -> list[tuple[float, float]]:
    """Sprocket holes seen with the pick pocket at (along, across): EIA-481 puts them
    2 mm along from it, every 4 mm, F across."""
    return [(along + 2 + 4 * k, across + pocket_line) for k in range(-2, 2)]


@pytest.mark.parametrize(
    ("along", "across", "pitch"),
    [(0.0, 0.0, 4), (0.4, -0.2, 4), (-1.3, 0.3, 4), (1.9, 0.0, 4), (0.6, 0.1, 2), (1.5, 0, 8)],
)
def test_pocket_from_holes(along: float, across: float, pitch: float) -> None:
    fix = pocket_from_holes(holes_for(along, across), 3.5, pitch)
    assert fix is not None
    expected = along - pitch * round(along / pitch) if pitch < 4 else along
    assert fix.along == pytest.approx(expected, abs=1e-6)
    assert fix.across == pytest.approx(across, abs=1e-6)
    assert fix.holes == 4


def test_pocket_ignores_other_round_things() -> None:
    holes = [*holes_for(0.2, 0.0), (1.0, -6.0), (-3.0, 9.0)]  # a fiducial, a plate hole
    fix = pocket_from_holes(holes, 3.5, 4)
    assert fix is not None
    assert fix.along == pytest.approx(0.2)
    assert pocket_from_holes([(1.0, -6.0)], 3.5, 4) is None


# --- The service, with simulated feeders on the LumenPnP profile ------------------------


def test_lumenpnp_slots_are_numbered_as_owners_number_them() -> None:
    """1-25 along the front rail from left to right, 26-50 along the back rail from right
    to left, each number also its Photon address. On the v4.1, +X is right and +Y back."""
    from picnicplace.feeders.slots import machine_slots
    from picnicplace.machine.profile import load_profile

    slots = machine_slots(load_profile("lumenpnp-v4.1"))
    assert [s.number for s in slots] == list(range(1, 51))
    assert all(s.address == s.number and s.id == str(s.number) for s in slots)
    front, back = slots[:25], slots[25:]
    assert all(a.pick.x < b.pick.x for a, b in pairwise(front))
    assert all(a.pick.x > b.pick.x for a, b in pairwise(back))
    assert max(s.pick.y for s in front) < min(s.pick.y for s in back)
    assert slots[0].label == "Slot 1"


@pytest.fixture
def lumen() -> Iterator[TestClient]:
    config = fast_config(machine={"profile": "lumenpnp-v4.1", "sim_home_error": (0.3, -0.2)})
    with TestClient(create_app(config)) as c:
        yield c


def service(client: TestClient) -> FeederService:
    return client.app.state.ctx.feeders  # type: ignore[attr-defined,no-any-return]


def slot_state(s: Session, slot_id: str) -> dict[str, Any]:
    state = s.client.get("/api/v1/feeders").json()
    return next(x for x in state["slots"] if x["slot"]["id"] == slot_id)


def true_pick(slot: dict[str, Any]) -> Point:
    """Where the simulator really put a slot's tape."""
    error = sim_slot_error(slot["slot"]["number"])
    return Point(x=slot["slot"]["pick"]["x"] + error.x, y=slot["slot"]["pick"]["y"] + error.y)


def succeeded(op: dict[str, Any]) -> dict[str, Any]:
    assert op["status"] == "succeeded", op
    return op


def add_part(client: TestClient) -> None:
    library = client.app.state.ctx.library  # type: ignore[attr-defined]
    library.put_footprint(
        Footprint(
            id="R0603",
            body=Size(x=1.6, y=0.8),
            pads=[
                Pad(name="1", x=-0.8, y=0, width=0.8, height=0.9),
                Pad(name="2", x=0.8, y=0, width=0.8, height=0.9),
            ],
        )
    )
    library.put_part(
        Part(
            id="RC0603-10K",
            footprint_id="R0603",
            height=0.45,
            tape=Tape(kind="paper", width=8, pitch=4, rotation=90),
        )
    )


def test_scan_assign_and_feed(lumen: TestClient) -> None:
    add_part(lumen)
    with connect(lumen) as s:
        op = succeeded(s.wait(s.post("/api/v1/feeders/scan")))
        assert op["message"].startswith("Found 6 feeders, 6 new")
        slot = slot_state(s, "2")
        assert slot["slot"]["address"] == 2
        assert slot["feeder"]["initialized"]
        uuid = slot["feeder"]["uuid"]
        assert slot_state(s, "26")["slot"]["address"] == 26

        r = s.put(f"/api/v1/feeders/assignments/{uuid}", {"part_id": "RC0603-10K"})
        assert r.status_code == 200, r.json()
        assert slot_state(s, "2")["feeder"]["assignment"]["part_id"] == "RC0603-10K"

        assert succeeded(s.wait(s.post("/api/v1/feeders/slots/2/feed", {})))["message"] == (
            "Slot 2: fed 4 mm."
        )
        sim = lumen.app.state.ctx.feeders._sim  # type: ignore[attr-defined]
        assert sim.feeders[2].fed_mm == pytest.approx(4.04)

        r = s.post("/api/v1/feeders/slots/9/feed", {})
        assert r.status_code == 409
        assert "scan the feeders" in r.json()["detail"]

        sim.feeders[3].jammed = True
        op = s.wait(s.post("/api/v1/feeders/slots/3/feed", {}))
        assert op["status"] == "failed"
        assert "could not reach" in op["error"]
        assert "could not reach" in slot_state(s, "3")["error"]

        # The assignment is saved, by feeder ID, and survives a restart.
        assert service(lumen).setup.setup.feeders[uuid].part_id == "RC0603-10K"


def test_teach_measure_fiducial_and_calibrate(lumen: TestClient) -> None:
    with connect(lumen) as s:
        s.home()
        succeeded(s.wait(s.post("/api/v1/feeders/scan")))

        # Teach 1 by eye: center the camera over its pocket.
        slot = slot_state(s, "1")
        pick = true_pick(slot)
        succeeded(s.move(X=pick.x, Y=pick.y))
        r = s.put("/api/v1/feeders/slots/1/pick", {})
        assert r.status_code == 200, r.json()
        taught = slot_state(s, "1")
        assert taught["calibration"]["basis"] == "taught"
        assert (taught["pick"]["x"], taught["pick"]["y"]) == pytest.approx(
            (pick.x, pick.y), abs=0.02
        )

        # Then the fiducial: jog roughly onto it and let vision find its center. On a
        # Photon feeder it is 19.15 mm along the feed from the pick location and 4.1 mm
        # from the sprocket holes' line toward the pockets: 0.6 mm past the pocket line
        # of 8 mm tape, whose pockets are 3.5 mm from the holes.
        fid = rotate(Point(x=19.15, y=-0.6), slot["slot"]["rotation"])
        succeeded(s.move(X=pick.x + fid.x + 0.6, Y=pick.y + fid.y - 0.4))
        succeeded(s.wait(s.post("/api/v1/feeders/slots/1/fiducial")))
        offset = service(lumen).setup.setup.fiducial_offset
        assert offset is not None
        assert (offset.x, offset.y) == pytest.approx((19.15, -4.1), abs=0.05)

        # Now every slot with a feeder is found from its fiducial.
        op = succeeded(s.wait(s.post("/api/v1/feeders/calibrate", {})))
        assert op["message"] == "Calibrated 6 slots."
        for slot_id in ("2", "5", "26"):
            state = slot_state(s, slot_id)
            want = true_pick(state)
            assert state["calibration"]["basis"] == "fiducial"
            assert (state["pick"]["x"], state["pick"]["y"]) == pytest.approx(
                (want.x, want.y), abs=0.05
            ), slot_id


def test_calibrating_a_12mm_feeder(lumen: TestClient) -> None:
    """The fiducial is fixed relative to the sprocket holes, so on 12 mm tape (pockets
    5.5 mm from the holes, not 3.5) it is 2 mm closer to the pocket line; calibration
    must allow for that."""
    library = lumen.app.state.ctx.library  # type: ignore[attr-defined]
    add_part(lumen)
    library.put_part(
        Part(
            id="BIG-12",
            footprint_id="R0603",
            tape=Tape(kind="embossed", width=12, pitch=8, rotation=0),
        )
    )
    with connect(lumen) as s:
        s.home()
        succeeded(s.wait(s.post("/api/v1/feeders/scan")))
        uuid = slot_state(s, "4")["feeder"]["uuid"]
        s.put(f"/api/v1/feeders/assignments/{uuid}", {"part_id": "BIG-12"})
        succeeded(s.wait(s.post("/api/v1/feeders/calibrate", {"slot_ids": ["4", "5"]})))
        for slot_id in ("4", "5"):
            state = slot_state(s, slot_id)
            want = true_pick(state)
            assert (state["pick"]["x"], state["pick"]["y"]) == pytest.approx(
                (want.x, want.y), abs=0.05
            ), slot_id


def test_feeder_noses_keep_boards_off_the_plate_edge() -> None:
    from picnicplace.machine.profile import load_profile

    profile = load_profile("lumenpnp-v4.1")
    feeders = {k.id: k.area for k in profile.board_keep_outs() if k.id.startswith("feeders")}
    # Front rail picks at Y 73.24 and its feeders reach 27.3 mm past: over the camera
    # plate's front edge (Y 93.07). The back rail's stop short of the rear plate.
    assert feeders["feeders-front"].y_max == pytest.approx(73.24 + 27.3)
    assert feeders["feeders-back"].y_min == pytest.approx(388.58 - 27.3)


def test_vision_finds_the_drifted_pocket(lumen: TestClient) -> None:
    with connect(lumen) as s:
        s.home()
        succeeded(s.wait(s.post("/api/v1/feeders/scan")))
        slot = slot_state(s, "26")
        pick = true_pick(slot)
        s.put("/api/v1/feeders/slots/26/pick", {"at": {"x": pick.x, "y": pick.y}})
        # Simulated feeders step 1% long: ten 4 mm feeds leave the tape 0.4 mm ahead.
        for _ in range(10):
            succeeded(s.wait(s.post("/api/v1/feeders/slots/26/feed", {})))
        op = succeeded(s.wait(s.post("/api/v1/feeders/slots/26/pocket")))
        pocket = slot_state(s, "26")["pocket"]
        assert pocket["along"] == pytest.approx(0.4, abs=0.05), op
        assert pocket["across"] == pytest.approx(0.0, abs=0.05)
        # The back rail feeds toward -Y: along the feed is -Y in machine coordinates.
        assert pocket["at"]["y"] == pytest.approx(pick.y - 0.4, abs=0.05)
        assert pocket["at"]["x"] == pytest.approx(pick.x, abs=0.05)


def test_a_simulated_pick_takes_the_part_from_its_pocket(lumen: TestClient) -> None:
    feeders = service(lumen)
    parts = feeders.sim_parts
    assert parts is not None
    tapes = feeders._scene_tapes()  # pyright: ignore[reportPrivateUsage]
    assert len(tapes) == 6
    first = tapes[0]
    assert first.pick_full
    # Nowhere near a pocket: nothing to pick.
    assert not parts.pick("N1", first.pick[0] + 5, first.pick[1] + 5, 0.0)
    # 0.3 mm off the pocket: the part is held 0.3 mm off the nozzle, at its tape angle.
    assert parts.pick("N1", first.pick[0] + 0.3, first.pick[1], 0.0)
    held = parts.held["N1"]
    assert (held.dx, held.dy) == pytest.approx((-0.3, 0.0))
    assert held.angle == pytest.approx(first.rotation + first.part_rotation)
    after = feeders._scene_tapes()  # pyright: ignore[reportPrivateUsage]
    assert not after[0].pick_full
    assert after[1].pick_full
    # The pocket is empty now: a second pick there gets nothing.
    assert not parts.pick("N2", first.pick[0], first.pick[1], 0.0)


def test_bring_up_record_round_trips(lumen: TestClient) -> None:
    record = lumen.get("/api/v1/bring-up").json()
    assert record["steps"] == {}
    record["steps"]["jog"] = {"status": "done", "note": "", "values": {"x_points": "right"}}
    record["points"] = [{"label": "Datum corner", "x": 177.2, "y": 96.1}]
    r = lumen.put("/api/v1/bring-up", json=record)
    assert r.status_code == 200, r.json()
    again = lumen.get("/api/v1/bring-up").json()
    assert again["steps"]["jog"]["values"] == {"x_points": "right"}
    assert again["points"][0]["label"] == "Datum corner"


# --- Rails ---------------------------------------------------------------------------

RAIL_FEEDERS = [1, 13, 25, 26, 38, 50]
RAIL_ERRORS = {"front": (4.0, -2.5, 0.4), "back": (-3.0, 5.0, -0.3)}


def rails_client(**machine: Any) -> TestClient:
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_home_error": (0.3, -0.2),
            "sim_feeders": RAIL_FEEDERS,
            **machine,
        },
        presence={"heartbeat_timeout_s": 300.0},
    )
    return TestClient(create_app(config))


def sim_truth(client: TestClient, number: int) -> tuple[Point, Point]:
    """Where the simulator really put a slot's pick location and its feeder fiducial
    (8 mm tape): the profile's place, moved with its rail, plus the slot's own error."""
    from picnicplace.feeders.slots import machine_slots

    feeders = service(client)
    nominal = machine_slots(feeders._profile)[number - 1]  # pyright: ignore[reportPrivateUsage]
    bank = next(b for b in feeders._profile.feeder_banks if b.id == nominal.bank)  # pyright: ignore[reportPrivateUsage]
    dx, dy, angle = feeders.sim_rail_errors.get(bank.id, (0.0, 0.0, 0.0))
    turned = rotate(
        Point(x=nominal.pick.x - bank.first_pick.x, y=nominal.pick.y - bank.first_pick.y), angle
    )
    error = sim_slot_error(number)
    pick = Point(
        x=bank.first_pick.x + turned.x + dx + error.x, y=bank.first_pick.y + turned.y + dy + error.y
    )
    fid = rotate(Point(x=19.15, y=-4.1 + 3.5), nominal.rotation + angle)
    return pick, Point(x=pick.x + fid.x, y=pick.y + fid.y)


def test_rails_are_found_from_their_end_feeders() -> None:
    """Rails a few millimeters off the profile and a little turned: the end feeders'
    fiducials place every slot between, close enough for slot calibration to find each
    feeder's fiducial, which a 3 mm search from the profile's places could not."""
    with rails_client() as client, connect(client) as s:
        service(client).sim_rail_errors = dict(RAIL_ERRORS)
        s.home()
        succeeded(s.wait(s.post("/api/v1/feeders/scan")))
        op = s.wait(s.post("/api/v1/feeders/calibrate", {}))
        assert op["status"] == "succeeded" and "No fiducial found" in op["message"]

        op = succeeded(s.wait(s.post("/api/v1/feeders/rails/calibrate", {})))
        assert op["message"].startswith("Front rail placed:")
        assert "Back rail placed:" in op["message"]
        rails = {r["bank"]: r for r in client.get("/api/v1/feeders").json()["rails"]}
        front = rails["front"]["calibration"]
        assert front["fitted"] and set(front["points"]) == {"1", "25"}
        # Each end feeder carries its slot's own error (up to 0.5 mm), so over the 378 mm
        # rail the angle can be off by up to about 0.15 degrees.
        assert front["angle"] == pytest.approx(0.4, abs=0.15)
        assert set(rails["back"]["calibration"]["points"]) == {"26", "50"}

        # The slot between, before its own calibration, is within its own small error.
        middle = slot_state(s, "13")
        pick, _ = sim_truth(client, 13)
        assert math.hypot(middle["pick"]["x"] - pick.x, middle["pick"]["y"] - pick.y) < 0.8

        op = succeeded(s.wait(s.post("/api/v1/feeders/calibrate", {})))
        assert op["message"] == "Calibrated 6 slots."
        for number in RAIL_FEEDERS:
            state = slot_state(s, str(number))
            pick, _ = sim_truth(client, number)
            assert (state["pick"]["x"], state["pick"]["y"]) == pytest.approx(
                (pick.x, pick.y), abs=0.05
            ), number

        # Back to the profile's places.
        r = s.delete("/api/v1/feeders/rails/front")
        assert r.status_code == 200
        assert next(x for x in r.json()["rails"] if x["bank"] == "front")["calibration"] is None
        assert slot_state(s, "13")["calibration"] is None


def test_a_rail_needs_two_feeders_far_apart(lumen: TestClient) -> None:
    with connect(lumen) as s:
        s.home()
        succeeded(s.wait(s.post("/api/v1/feeders/scan")))  # slots 1-5 and 26
        r = s.post("/api/v1/feeders/rails/calibrate", {"bank_ids": ["front"]})
        assert r.status_code == 409
        assert "put feeders in slot 1 and slot 25" in r.json()["detail"]
        r = s.post("/api/v1/feeders/rails/calibrate", {"bank_ids": ["back"]})
        assert "slot 26 and slot 50" in r.json()["detail"]


def test_the_operator_shows_a_fiducial_the_search_missed() -> None:
    """A rail more than half a slot along from the profile's place: the search will not
    take a fiducial that far along (it could be the next feeder's), so it asks; the
    operator centers the camera on it."""
    with rails_client() as client, connect(client) as s:
        service(client).sim_rail_errors = {"front": (10.0, 0.0, 0.0)}
        s.home()
        succeeded(s.wait(s.post("/api/v1/feeders/scan")))
        op = succeeded(s.wait(s.post("/api/v1/feeders/rails/calibrate", {"bank_ids": ["front"]})))
        assert "No feeder fiducial was found at slot 1, slot 25" in op["message"]
        assert "Fiducial is here" in op["message"]
        assert not client.get("/api/v1/feeders").json()["rails"][0]["calibration"]["fitted"]

        for number in (1, 25):
            _, fid = sim_truth(client, number)
            succeeded(s.move(X=fid.x + 0.7, Y=fid.y - 0.5))  # roughly, by jogging
            op = succeeded(s.wait(s.post(f"/api/v1/feeders/slots/{number}/rail-fiducial")))
        assert op["message"].startswith("Front rail placed: ")
        shift = client.get("/api/v1/feeders").json()["rails"][0]["calibration"]["shift"]
        assert (shift["x"], shift["y"]) == pytest.approx((10, 0), abs=0.6)
        pick, _ = sim_truth(client, 13)
        middle = slot_state(s, "13")
        assert math.hypot(middle["pick"]["x"] - pick.x, middle["pick"]["y"] - pick.y) < 0.8
