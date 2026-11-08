"""Feeder slots: the places on a machine where a feeder can sit, from its profile."""

from picnicplace.machine.profile import MachineProfile, Point
from picnicplace.schemas import WireModel


class Slot(WireModel):
    id: str
    """The slot number, as text: ``"12"``."""
    bank: str
    number: int
    """Slots are numbered from 1 across all banks, in the profile's order. On a
    LumenPnP that is 1-25 along the front rail from the left and 26-50 along the back
    rail from the right, the same as the Photon slot addresses."""
    label: str
    address: int | None
    """Photon bus address, if the bank's slots have addresses."""
    pick: Point
    """Nominal pick location, from the profile."""
    rotation: float
    """Tape direction in machine degrees: the angle of the EIA-481 view's +X."""


def machine_slots(profile: MachineProfile) -> list[Slot]:
    out: list[Slot] = []
    for bank in profile.feeder_banks:
        for i in range(bank.count):
            number = len(out) + 1
            out.append(
                Slot(
                    id=str(number),
                    bank=bank.id,
                    number=number,
                    label=f"Slot {number}",
                    address=None if bank.first_address is None else bank.first_address + i,
                    pick=bank.pick(i),
                    rotation=bank.rotation,
                )
            )
    return out
