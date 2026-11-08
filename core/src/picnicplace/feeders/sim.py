"""Simulated Photon feeders, for the simulator and the fake firmware.

They answer packets the way the protocol describes: a feeder ignores the bus while it
feeds, a feeder that has not been initialized refuses to feed, and broadcast commands
are answered only by the feeder they name. A feeder can be set to jam, for tests.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from picnicplace.feeders.photon import (
    BROADCAST,
    HEADER_LENGTH,
    HOST,
    UUID_LENGTH,
    Command,
    Packet,
    Status,
    crc8,
)

FEED_SPEED_MM_S = 20.0
FEED_OVERHEAD_S = 0.15

SIM_UUID_PREFIX = bytes.fromhex("5049434E4943")
"""Simulated feeder IDs start with "PICNIC" in ASCII."""


def simulated_uuid(address: int) -> bytes:
    return SIM_UUID_PREFIX + bytes(4) + address.to_bytes(2, "big")


@dataclass
class SimulatedFeeder:
    address: int
    uuid: bytes
    initialized: bool = False
    fed_mm: float = 0.0
    """Tape moved forward since power-up (backward moves subtract)."""
    busy_until: float = 0.0
    jammed: bool = False
    step_scale: float = 1.0
    """How far the tape really moves per millimeter asked for: real feeders are not exact,
    and their errors add up."""
    last_feed_ok: bool = True
    identified: int = 0
    """How many times it was asked to flash its light."""


@dataclass
class SimulatedPhotonBus:
    feeders: dict[int, SimulatedFeeder] = field(default_factory=dict[int, SimulatedFeeder])
    time_scale: float = 1.0
    clock: Callable[[], float] = time.monotonic
    on_change: Callable[[], None] | None = None

    @classmethod
    def with_feeders(
        cls, addresses: list[int], time_scale: float = 1.0, step_scale: float = 1.0
    ) -> "SimulatedPhotonBus":
        return cls(
            {a: SimulatedFeeder(a, simulated_uuid(a), step_scale=step_scale) for a in addresses},
            time_scale,
        )

    def handle(self, raw: bytes) -> bytes | None:
        """Answer one packet from the host, or None for silence."""
        if len(raw) < HEADER_LENGTH + 1 or raw[3] != len(raw) - HEADER_LENGTH:
            return None
        if crc8(raw[:4] + raw[HEADER_LENGTH:]) != raw[4]:
            return None
        request = Packet.decode(raw)
        if request.sender != HOST:
            return None
        command, data = request.payload[0], request.payload[1:]
        if request.to == BROADCAST:
            return self._broadcast(request, command, data)
        feeder = self.feeders.get(request.to)
        if feeder is None or self.clock() < feeder.busy_until:
            return None
        payload = self._unicast(feeder, command, data)
        if payload is None:
            return None
        return Packet(HOST, feeder.address, request.packet_id, payload).encode()

    def _unicast(self, f: SimulatedFeeder, command: int, data: bytes) -> bytes | None:
        ok = bytes([Status.OK])
        if command == Command.GET_FEEDER_ID:
            return ok + f.uuid
        if command == Command.INITIALIZE_FEEDER:
            if data[:UUID_LENGTH] != f.uuid:
                return bytes([Status.WRONG_FEEDER_ID]) + f.uuid
            f.initialized = True
            return ok + f.uuid
        if command == Command.GET_VERSION:
            return ok + bytes([1])
        if command in (Command.MOVE_FEED_FORWARD, Command.MOVE_FEED_BACKWARD):
            if not f.initialized:
                return bytes([Status.UNINITIALIZED_FEEDER]) + f.uuid
            distance = (data[0] if data else 0) / 10
            seconds = FEED_OVERHEAD_S + distance / FEED_SPEED_MM_S
            f.busy_until = self.clock() + seconds / self.time_scale
            f.last_feed_ok = not f.jammed
            if not f.jammed:
                moved = distance * f.step_scale
                f.fed_mm += moved if command == Command.MOVE_FEED_FORWARD else -moved
                self._changed()
            return ok + round(seconds * 1000 / self.time_scale).to_bytes(2, "big")
        if command == Command.MOVE_FEED_STATUS:
            return bytes([Status.OK if f.last_feed_ok else Status.COULDNT_REACH])
        if command == Command.VENDOR_OPTIONS:
            return ok
        return bytes([Status.UNKNOWN])

    def _broadcast(self, request: Packet, command: int, data: bytes) -> bytes | None:
        if command == Command.UNINITIALIZED_FEEDERS_RESPOND:
            waiting = [f for f in self.feeders.values() if not f.initialized]
            if len(waiting) != 1:  # several at once would collide on a real bus
                return None
            f = waiting[0]
            return Packet(HOST, f.address, request.packet_id, bytes([Status.OK]) + f.uuid).encode()
        target = next((f for f in self.feeders.values() if f.uuid == data[:UUID_LENGTH]), None)
        if target is None or self.clock() < target.busy_until:
            return None
        ok = bytes([Status.OK])
        if command == Command.GET_FEEDER_ADDRESS:
            payload = ok
        elif command == Command.IDENTIFY_FEEDER:
            target.identified += 1
            payload = ok
        elif command == Command.PROGRAM_FEEDER_FLOOR and len(data) > UUID_LENGTH:
            new = data[UUID_LENGTH]
            if new in self.feeders and self.feeders[new] is not target:
                payload = bytes([Status.FAIL])
            else:
                del self.feeders[target.address]
                target.address = new
                self.feeders[new] = target
                payload = ok
        else:
            return None
        return Packet(HOST, target.address, request.packet_id, payload).encode()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()
