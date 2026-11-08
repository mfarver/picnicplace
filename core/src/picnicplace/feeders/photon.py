"""Photon feeders: the RS-485 protocol of the LumenPnP's tape feeders.

Written from Opulo's protocol description (docs.opulo.io, "Photon Protocol") and checked
against a TRACE log of a working LumenPnP, whose every packet this code decodes with a
valid checksum.

Every exchange is one packet from the host and at most one reply. A packet is a
five-byte header, then the payload:

========  ===========================================================================
Byte      Meaning
========  ===========================================================================
0         To: the feeder's address (its slot), 0x00 for the host, 0xFF to broadcast
1         From
2         Packet ID: the host counts up, wrapping at 0xFF; a reply repeats it
3         Payload length
4         CRC-8 (polynomial 0x07, initial value 0) of the other header bytes and the
          payload
5...      Payload: a command from the host, or a status from a feeder, then data
========  ===========================================================================

A feeder's address is programmed into the slot it sits in (the "feeder floor"), so it
names the slot, not the feeder; every feeder also has a fixed 12-byte ID. A feeder only
feeds after the host has initialized it with its ID, which makes sure the host knows
which feeder (and so which part) is in the slot.

Feeding blocks the feeder: it answers the move command with the time the move should
take, then ignores the bus until it is done. The host polls with ``MOVE_FEED_STATUS``
until it answers.

On a LumenPnP the packets travel through the motherboard (Marlin's ``M485``); any
driver that can exchange packets can run this code.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum

from picnicplace.machine.errors import FeederError

log = logging.getLogger(__name__)

HOST = 0x00
BROADCAST = 0xFF
UUID_LENGTH = 12
HEADER_LENGTH = 5

MAX_STEP_TENTHS = 0xFF
"""One move command carries the distance in tenths of a millimeter in one byte."""

FEED_TIMEOUT_MARGIN_S = 2.0
"""Wait this much longer than the feeder's own estimate before giving up on a feed."""

POLL_INTERVAL_S = 0.05


class Command(IntEnum):
    GET_FEEDER_ID = 0x01
    INITIALIZE_FEEDER = 0x02
    GET_VERSION = 0x03
    MOVE_FEED_FORWARD = 0x04
    MOVE_FEED_BACKWARD = 0x05
    MOVE_FEED_STATUS = 0x06
    VENDOR_OPTIONS = 0xBF
    GET_FEEDER_ADDRESS = 0xC0
    IDENTIFY_FEEDER = 0xC1
    PROGRAM_FEEDER_FLOOR = 0xC2
    UNINITIALIZED_FEEDERS_RESPOND = 0xC3


class Status(IntEnum):
    OK = 0x00
    WRONG_FEEDER_ID = 0x01
    COULDNT_REACH = 0x02
    UNINITIALIZED_FEEDER = 0x03
    FEEDING_IN_PROGRESS = 0x04
    FAIL = 0x05
    TIMEOUT = 0xFE
    UNKNOWN = 0xFF


STATUS_TEXT = {
    Status.WRONG_FEEDER_ID: "a different feeder is in the slot",
    Status.COULDNT_REACH: "the feeder could not reach the next part (jammed or empty?)",
    Status.UNINITIALIZED_FEEDER: "the feeder has not been initialized",
    Status.FEEDING_IN_PROGRESS: "the feeder is still feeding",
    Status.FAIL: "the feeder reported a failure",
    Status.TIMEOUT: "the feeder timed out",
    Status.UNKNOWN: "the feeder reported an unknown error",
}


def crc8(data: bytes) -> int:
    """CRC-8 with polynomial 0x07, initial value 0, no reflection or final XOR."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


@dataclass(frozen=True)
class Packet:
    to: int
    sender: int
    packet_id: int
    payload: bytes

    def encode(self) -> bytes:
        head = bytes([self.to, self.sender, self.packet_id, len(self.payload)])
        return head + bytes([crc8(head + self.payload)]) + self.payload

    @classmethod
    def decode(cls, raw: bytes) -> "Packet":
        if len(raw) < HEADER_LENGTH + 1:
            raise FeederError(f"feeder reply too short: {raw.hex().upper()}")
        to, sender, packet_id, length, crc = raw[:HEADER_LENGTH]
        payload = raw[HEADER_LENGTH:]
        if len(payload) != length:
            raise FeederError(f"feeder reply has the wrong length: {raw.hex().upper()}")
        if crc8(raw[:4] + payload) != crc:
            raise FeederError(f"feeder reply has a bad checksum: {raw.hex().upper()}")
        return cls(to, sender, packet_id, payload)

    @property
    def status(self) -> int:
        return self.payload[0]

    @property
    def data(self) -> bytes:
        return self.payload[1:]


def format_uuid(raw: bytes) -> str:
    """Feeder IDs are shown as 24 hex digits, as OpenPnP shows them."""
    return raw.hex().upper()


def parse_uuid(text: str) -> bytes:
    raw = bytes.fromhex(text)
    if len(raw) != UUID_LENGTH:
        raise ValueError(f"a feeder ID has {UUID_LENGTH * 2} hex digits")
    return raw


Exchange = Callable[[bytes], Awaitable[bytes | None]]
"""Send one packet and return the reply, or None if none came in time."""


class PhotonBus:
    """The host's side of the Photon protocol."""

    def __init__(
        self,
        exchange: Exchange,
        *,
        poll_interval_s: float = POLL_INTERVAL_S,
        timeout_margin_s: float = FEED_TIMEOUT_MARGIN_S,
    ) -> None:
        self._exchange = exchange
        self._poll_interval_s = poll_interval_s
        self._timeout_margin_s = timeout_margin_s
        self._packet_id = 0
        self._lock = asyncio.Lock()

    async def request(self, address: int, command: Command, data: bytes = b"") -> Packet | None:
        """Send a command and return the reply, or None if there was none."""
        async with self._lock:
            packet_id = self._packet_id
            self._packet_id = (self._packet_id + 1) & 0xFF
        sent = Packet(address, HOST, packet_id, bytes([command]) + data)
        raw = await self._exchange(sent.encode())
        if raw is None:
            return None
        reply = Packet.decode(raw)
        if reply.to != HOST or reply.packet_id != packet_id:
            raise FeederError(f"a feeder reply did not match its request: {raw.hex().upper()}")
        if address != BROADCAST and reply.sender != address:
            raise FeederError(f"slot {address} was answered by address {reply.sender}")
        if not reply.payload:
            raise FeederError(f"a feeder reply had no status: {raw.hex().upper()}")
        return reply

    async def feeder_id(self, address: int) -> str | None:
        """The ID of the feeder in a slot, or None if the slot is empty (no answer)."""
        reply = await self.request(address, Command.GET_FEEDER_ID)
        if reply is None:
            return None
        _check(reply, address)
        return format_uuid(reply.data[:UUID_LENGTH])

    async def initialize(self, address: int, uuid: str) -> None:
        """Tell the feeder in a slot which feeder we expect; it feeds only after this."""
        reply = await self.request(address, Command.INITIALIZE_FEEDER, parse_uuid(uuid))
        if reply is None:
            raise FeederError(f"no feeder answered in slot {address}")
        _check(reply, address)

    async def version(self, address: int) -> int | None:
        reply = await self.request(address, Command.GET_VERSION)
        if reply is None:
            return None
        _check(reply, address)
        return reply.data[0] if reply.data else None

    async def feed(self, address: int, distance_mm: float) -> None:
        """Move the tape forward (or back, if negative) and wait until it has moved."""
        command = Command.MOVE_FEED_FORWARD if distance_mm >= 0 else Command.MOVE_FEED_BACKWARD
        tenths = round(abs(distance_mm) * 10)
        while tenths > 0:
            step = min(tenths, MAX_STEP_TENTHS)
            await self._move(address, command, step)
            tenths -= step

    async def _move(self, address: int, command: Command, tenths: int) -> None:
        reply = await self.request(address, command, bytes([tenths]))
        if reply is None:
            raise FeederError(f"no feeder answered in slot {address}")
        _check(reply, address)
        expected_s = int.from_bytes(reply.data[:2], "big") / 1000 if len(reply.data) >= 2 else 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + expected_s + self._timeout_margin_s
        while True:
            await asyncio.sleep(self._poll_interval_s)
            status = await self.request(address, Command.MOVE_FEED_STATUS)
            if status is not None:
                _check(status, address)
                return
            if loop.time() > deadline:
                raise FeederError(
                    f"the feeder in slot {address} did not finish feeding in "
                    f"{expected_s + self._timeout_margin_s:.1f} s"
                )

    async def identify(self, uuid: str) -> bool:
        """Ask a feeder to flash its light. True if it answered."""
        reply = await self.request(BROADCAST, Command.IDENTIFY_FEEDER, parse_uuid(uuid))
        return reply is not None and reply.status == Status.OK

    async def find_address(self, uuid: str) -> int | None:
        """Which slot a feeder is in, or None if it does not answer."""
        reply = await self.request(BROADCAST, Command.GET_FEEDER_ADDRESS, parse_uuid(uuid))
        if reply is None or reply.status != Status.OK:
            return None
        return reply.sender

    async def program_floor(self, uuid: str, address: int) -> None:
        """Store an address in the slot a feeder sits in. Done once per slot."""
        if not 0 < address < BROADCAST:
            raise ValueError("slot addresses are 1 to 254")
        reply = await self.request(
            BROADCAST, Command.PROGRAM_FEEDER_FLOOR, parse_uuid(uuid) + bytes([address])
        )
        if reply is None:
            raise FeederError(f"feeder {uuid} did not answer")
        _check(reply, address)


def _check(reply: Packet, address: int) -> None:
    if reply.status == Status.OK:
        return
    try:
        text = STATUS_TEXT[Status(reply.status)]
    except ValueError:
        text = f"the feeder answered with status 0x{reply.status:02X}"
    raise FeederError(f"slot {address}: {text}")
