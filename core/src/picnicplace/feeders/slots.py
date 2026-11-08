"""Feeder slots: the places on a machine where a feeder can sit, from its profile."""

from picnicplace.machine.profile import MachineProfile, Point
from picnicplace.schemas import WireModel


class Slot(WireModel):
    id: str
    """Bank ID and slot number from 1, such as ``front:3``."""
    bank: str
    number: int
    label: str
    address: int | None
    """Photon bus address, if the bank's slots have addresses."""
    pick: Point
    """Nominal pick location, from the profile."""
    rotation: float
    """Tape direction in machine degrees: the angle of the EIA-481 view's +X."""


def slot_id(bank: str, number: int) -> str:
    return f"{bank}:{number}"


def machine_slots(profile: MachineProfile) -> list[Slot]:
    out: list[Slot] = []
    for bank in profile.feeder_banks:
        for i in range(bank.count):
            out.append(
                Slot(
                    id=slot_id(bank.id, i + 1),
                    bank=bank.id,
                    number=i + 1,
                    label=f"{bank.label} {i + 1}",
                    address=None if bank.first_address is None else bank.first_address + i,
                    pick=bank.pick(i),
                    rotation=bank.rotation,
                )
            )
    return out
