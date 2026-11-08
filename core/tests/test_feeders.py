"""Photon feeders: the protocol, simulated feeders, and the feeder service end to end."""

import asyncio
import math
from collections.abc import Iterator
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
    SIM_FIDUCIAL_OFFSET,
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
    number = slot["slot"]["number"] + 100 * ["back", "front"].index(slot["slot"]["bank"])
    error = sim_slot_error(number)
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
        slot = slot_state(s, "front:2")
        assert slot["slot"]["address"] == 2
        assert slot["feeder"]["initialized"]
        uuid = slot["feeder"]["uuid"]
        assert slot_state(s, "back:1")["slot"]["address"] == 26

        r = s.put(f"/api/v1/feeders/assignments/{uuid}", {"part_id": "RC0603-10K"})
        assert r.status_code == 200, r.json()
        assert slot_state(s, "front:2")["feeder"]["assignment"]["part_id"] == "RC0603-10K"

        assert succeeded(s.wait(s.post("/api/v1/feeders/slots/front:2/feed", {})))["message"] == (
            "Front rail 2: fed 4 mm."
        )
        sim = lumen.app.state.ctx.feeders._sim  # type: ignore[attr-defined]
        assert sim.feeders[2].fed_mm == pytest.approx(4.04)

        r = s.post("/api/v1/feeders/slots/front:9/feed", {})
        assert r.status_code == 409
        assert "scan the feeders" in r.json()["detail"]

        sim.feeders[3].jammed = True
        op = s.wait(s.post("/api/v1/feeders/slots/front:3/feed", {}))
        assert op["status"] == "failed"
        assert "could not reach" in op["error"]
        assert "could not reach" in slot_state(s, "front:3")["error"]

        # The assignment is saved, by feeder ID, and survives a restart.
        assert service(lumen).setup.setup.feeders[uuid].part_id == "RC0603-10K"


def test_teach_measure_fiducial_and_calibrate(lumen: TestClient) -> None:
    with connect(lumen) as s:
        s.home()
        succeeded(s.wait(s.post("/api/v1/feeders/scan")))

        # Teach front:1 by eye: center the camera over its pocket.
        slot = slot_state(s, "front:1")
        pick = true_pick(slot)
        succeeded(s.move(X=pick.x, Y=pick.y))
        r = s.put("/api/v1/feeders/slots/front:1/pick", {})
        assert r.status_code == 200, r.json()
        taught = slot_state(s, "front:1")
        assert taught["calibration"]["basis"] == "taught"
        assert (taught["pick"]["x"], taught["pick"]["y"]) == pytest.approx(
            (pick.x, pick.y), abs=0.02
        )

        # Then the fiducial: jog roughly onto it and let vision find its center.
        fid = rotate(SIM_FIDUCIAL_OFFSET, slot["slot"]["rotation"])
        succeeded(s.move(X=pick.x + fid.x + 0.6, Y=pick.y + fid.y - 0.4))
        succeeded(s.wait(s.post("/api/v1/feeders/slots/front:1/fiducial")))
        offset = service(lumen).setup.setup.fiducial_offset
        assert offset is not None
        assert (offset.x, offset.y) == pytest.approx(
            (SIM_FIDUCIAL_OFFSET.x, SIM_FIDUCIAL_OFFSET.y), abs=0.05
        )

        # Now every slot with a feeder is found from its fiducial.
        op = succeeded(s.wait(s.post("/api/v1/feeders/calibrate", {})))
        assert op["message"] == "Calibrated 6 slots."
        for slot_id in ("front:2", "front:5", "back:1"):
            state = slot_state(s, slot_id)
            want = true_pick(state)
            assert state["calibration"]["basis"] == "fiducial"
            assert (state["pick"]["x"], state["pick"]["y"]) == pytest.approx(
                (want.x, want.y), abs=0.05
            ), slot_id


def test_vision_finds_the_drifted_pocket(lumen: TestClient) -> None:
    with connect(lumen) as s:
        s.home()
        succeeded(s.wait(s.post("/api/v1/feeders/scan")))
        slot = slot_state(s, "back:1")
        pick = true_pick(slot)
        s.put("/api/v1/feeders/slots/back:1/pick", {"at": {"x": pick.x, "y": pick.y}})
        # Simulated feeders step 1% long: ten 4 mm feeds leave the tape 0.4 mm ahead.
        for _ in range(10):
            succeeded(s.wait(s.post("/api/v1/feeders/slots/back:1/feed", {})))
        op = succeeded(s.wait(s.post("/api/v1/feeders/slots/back:1/pocket")))
        pocket = slot_state(s, "back:1")["pocket"]
        assert pocket["along"] == pytest.approx(0.4, abs=0.05), op
        assert pocket["across"] == pytest.approx(0.0, abs=0.05)
        # The back rail feeds toward -Y: along the feed is -Y in machine coordinates.
        assert pocket["at"]["y"] == pytest.approx(pick.y - 0.4, abs=0.05)
        assert pocket["at"]["x"] == pytest.approx(pick.x, abs=0.05)


def test_a_simulated_pick_empties_the_pocket(lumen: TestClient) -> None:
    feeders = service(lumen)
    tapes = feeders._scene_tapes()  # pyright: ignore[reportPrivateUsage]
    assert len(tapes) == 6
    first = tapes[0]
    assert first.pick_full
    feeders._sim_picked("N1", first.pick[0] + 0.3, first.pick[1])  # pyright: ignore[reportPrivateUsage]
    assert not feeders._scene_tapes()[0].pick_full  # pyright: ignore[reportPrivateUsage]
    assert math.isclose(feeders._scene_tapes()[1].pick[0], tapes[1].pick[0])  # pyright: ignore[reportPrivateUsage]


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
