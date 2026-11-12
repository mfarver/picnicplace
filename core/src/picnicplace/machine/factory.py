"""Build the motion driver a config asks for."""

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from picnicplace.config import MachineConfig
from picnicplace.feeders.sim import SimulatedPhotonBus
from picnicplace.feeders.slots import machine_slots
from picnicplace.machine.driver import MotionDriver
from picnicplace.machine.marlin.driver import MarlinDriver
from picnicplace.machine.marlin.fake import FakeMarlinPty, FakeSettings
from picnicplace.machine.marlin.transport import LineTransport, SerialTransport
from picnicplace.machine.simulator import SimulatorDriver, sim_profile


async def _nothing() -> None:
    return None


@dataclass
class DriverBundle:
    driver: MotionDriver
    shutdown: Callable[[], Awaitable[None]] = _nothing
    """Stops anything started alongside the driver, such as a fake firmware."""
    photon_sim: SimulatedPhotonBus | None = None
    """Simulated feeders, with the simulator or the fake firmware."""


SIM_FEEDER_STEP_SCALE = 1.01
"""Simulated feeders move the tape 1% too far, so vision has drift to correct: after ten
4 mm feeds the pocket is 0.4 mm past the pick location."""


def simulated_feeders(config: MachineConfig) -> SimulatedPhotonBus | None:
    """The simulated Photon feeders a config asks for, if the machine has a Photon bus."""
    profile = config.machine_profile
    if profile.feeder_bus != "photon":
        return None
    by_number = {s.number: s for s in machine_slots(profile)}
    addresses: list[int] = []
    for number in config.sim_feeders:
        slot = by_number.get(number)
        if slot is not None and slot.address is not None:
            addresses.append(slot.address)
    return SimulatedPhotonBus.with_feeders(
        addresses, config.sim_time_scale, step_scale=SIM_FEEDER_STEP_SCALE
    )


SIM_DATUM_RADIUS_MM = 25.0
"""The simulated datum board's flat top, around its center fiducial."""


def sim_surface(config: MachineConfig) -> Callable[[float, float], float | None] | None:
    """What a simulated nozzle can seal on: the datum board's top (the profile's height of
    its center fiducial) near it, and a board's top on the staging plates (the profile's
    board height), each off by the config's simulator error."""
    profile = config.machine_profile
    fiducial = next((loc for loc in profile.locations if loc.kind == "homing_fiducial"), None)
    datum = fiducial if fiducial is not None and fiducial.z is not None else None
    board = profile.board_height
    if datum is None and board is None:
        return None
    plates = profile.staging_plates

    def surface(x: float, y: float) -> float | None:
        if datum is not None and datum.z is not None:
            center = datum.position
            if math.hypot(x - center.x, y - center.y) <= SIM_DATUM_RADIUS_MM:
                return datum.z + config.sim_datum_height_error
        if board is not None:
            for p in plates:
                if abs(x - p.center.x) <= p.size.x / 2 and abs(y - p.center.y) <= p.size.y / 2:
                    return board + config.sim_board_height_error
        return None

    return surface


def build_driver(config: MachineConfig) -> DriverBundle:
    profile = config.machine_profile
    if config.driver == "simulator":
        photon = simulated_feeders(config)
        driver = SimulatorDriver(
            sim_profile(profile, config.sim_nozzle_offset_error),
            config.sim_time_scale,
            config.sim_home_error,
            photon=photon,
            tip_errors=config.sim_nozzle_z_error,
        )
        driver.surface_at = sim_surface(config)
        return DriverBundle(driver, photon_sim=photon)

    assert profile.marlin is not None  # checked when the config was loaded
    settings = profile.marlin.resolved(config.marlin.variant)
    marlin = config.marlin

    if config.driver == "marlin":
        assert marlin.port is not None
        port: str = marlin.port

        async def open_port() -> LineTransport:
            return await SerialTransport.open(port, marlin.baud)

        driver = MarlinDriver(
            profile,
            settings,
            open_port,
            connect_wait_s=marlin.connect_wait_s,
            command_timeout_s=marlin.command_timeout_s,
        )
        return DriverBundle(driver)

    # fake-marlin: the real driver and serial transport, against a fake on a pty.
    photon = simulated_feeders(config)
    fake = FakeMarlinPty(FakeSettings(time_scale=config.sim_time_scale, photon=photon))

    async def open_fake() -> LineTransport:
        path = fake.path or await fake.start()
        return await SerialTransport.open(path, marlin.baud)

    driver = MarlinDriver(
        profile, settings, open_fake, connect_wait_s=0, command_timeout_s=marlin.command_timeout_s
    )
    return DriverBundle(driver, fake.stop, photon_sim=photon)
