"""Build the motion driver a config asks for."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from picnicplace.config import MachineConfig
from picnicplace.machine.driver import MotionDriver
from picnicplace.machine.marlin.driver import MarlinDriver
from picnicplace.machine.marlin.fake import FakeMarlinPty, FakeSettings
from picnicplace.machine.marlin.transport import LineTransport, SerialTransport
from picnicplace.machine.simulator import SimulatorDriver


async def _nothing() -> None:
    return None


@dataclass
class DriverBundle:
    driver: MotionDriver
    shutdown: Callable[[], Awaitable[None]] = _nothing
    """Stops anything started alongside the driver, such as a fake firmware."""


def build_driver(config: MachineConfig) -> DriverBundle:
    profile = config.machine_profile
    if config.driver == "simulator":
        return DriverBundle(SimulatorDriver(profile, config.sim_time_scale))

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
    fake = FakeMarlinPty(FakeSettings(time_scale=config.sim_time_scale))

    async def open_fake() -> LineTransport:
        path = fake.path or await fake.start()
        return await SerialTransport.open(path, marlin.baud)

    driver = MarlinDriver(
        profile, settings, open_fake, connect_wait_s=0, command_timeout_s=marlin.command_timeout_s
    )
    return DriverBundle(driver, fake.stop)
