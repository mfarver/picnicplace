"""The Marlin driver against the fake LumenPnP firmware."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

import pytest

from picnicplace.machine.errors import MachineError, MotionInterruptedError
from picnicplace.machine.marlin.driver import MarlinDriver
from picnicplace.machine.marlin.fake import (
    FakeMarlin,
    FakeMarlinMemory,
    FakeMarlinPty,
    FakeSettings,
)
from picnicplace.machine.marlin.transport import SerialTransport
from picnicplace.machine.profile import load_profile

FAST = 50.0
"""Simulated time runs this much faster than real time."""


def run(scenario: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(asyncio.wait_for(scenario(), 20))


@asynccontextmanager
async def lumenpnp(
    variant: str | None = None, **fake: float | bool
) -> AsyncIterator[tuple[MarlinDriver, FakeMarlin]]:
    """A Marlin driver connected to a fake LumenPnP."""
    profile = load_profile("lumenpnp-v4.1")
    assert profile.marlin is not None
    host = FakeMarlinMemory(FakeSettings(time_scale=FAST, **fake))  # type: ignore[arg-type]
    driver = MarlinDriver(profile, profile.marlin.resolved(variant), host.open, connect_wait_s=0)
    await driver.connect()
    try:
        yield driver, host.firmware
    finally:
        await driver.disconnect()
        await host.stop()


async def wait_until(condition: Callable[[], bool], limit_s: float = 5.0) -> None:
    """Poll a condition: the fake and driver have no events to wait on for these."""
    async with asyncio.timeout(limit_s):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


# --- The fake on its own -----------------------------------------------------------------


def test_fake_replies_like_the_lumenpnp() -> None:
    async def scenario() -> None:
        out: list[str] = []
        fake = FakeMarlin(out.append, FakeSettings(time_scale=FAST))
        fake.start()

        async def ask(line: str) -> list[str]:
            out.clear()
            fake.receive(line)
            await wait_until(lambda: bool(out) and out[-1] == "ok")
            return out[:-1]

        assert "MACHINE_TYPE:LumenPnP" in (await ask("M115"))[0]
        assert await ask("G90") == []
        # The stock firmware calls the nozzles level Z = 31.5 after homing; G92 renames it,
        # and G28 forgets the renaming.
        assert (await ask("G28"))[0].startswith("X:0.00 Y:0.00 Z:31.50 A:0.00 B:0.00 Count")
        await ask("G92 Z0")
        assert (await ask("M114"))[0].startswith("X:0.00 Y:0.00 Z:0.00 A:0.00 B:0.00 Count")
        await ask("G0 Z-2 F600")
        await ask("M400")
        assert fake.target["Z"] == pytest.approx(29.5)
        await ask("G28")
        assert (await ask("M114"))[0].startswith("X:0.00 Y:0.00 Z:31.50")
        await ask("G92 Z0")
        await ask("G0 X10 Y20 F6000")
        await ask("M400")
        assert (await ask("M114"))[0].startswith("X:10.00 Y:20.00")
        assert await ask("G4 P10") == ["//action:notification Sleep..."]
        assert await ask("M485 1234") == ["rs485-reply: TIMEOUT"]
        assert (await ask("Q99"))[0].startswith("echo:Unknown command")

        # Vacuum sensor 1, read the patched (24-bit) way and the stock (8-bit) way.
        for line in ("M106 P1 S180", "M106 P0 S255", "M260 A112 B1 S1", "M260 A109 B6 S1"):
            await ask(line)
        await asyncio.sleep(0.5 / FAST * 10)
        reply = (await ask("M261 A109 B3 S4"))[0]
        assert reply.startswith("echo:i2c-reply: from:109 bytes:3 data:-")
        value = int(reply.rsplit(":", 1)[1])
        assert -1_600_000 < value < -1_300_000  # an open nozzle
        top = int((await ask("M261 A109 B1 S2"))[0].rsplit(":", 1)[1])
        assert top == (value & 0xFFFFFF) >> 16

        out.clear()
        fake.receive("M112")
        await wait_until(lambda: "kill() called" in "".join(out))
        assert fake.halted
        await fake.stop()

    run(scenario)


# --- The driver --------------------------------------------------------------------------


def test_connect_home_and_move() -> None:
    async def scenario() -> None:
        async with lumenpnp() as (driver, fake):
            assert driver.connected
            assert driver.firmware is not None and "Marlin" in driver.firmware
            assert driver.machine_type == "LumenPnP"
            # The profile's connect commands were sent, and every output switched off.
            assert "M113 S0" in fake.received
            assert "M107 P1" in fake.received

            fake.target["A"] = -6300.0  # rotation keeps its position through homing
            await driver.home()
            assert driver.position == {"X": 0, "Y": 0, "Z": 0, "A": -6300, "B": 0}

            seen: list[float] = []
            driver.on_change = lambda: seen.append(driver.position["X"])
            await driver.move_to({"X": 120.0, "Y": 80.0}, speed=None)
            assert driver.position["X"] == pytest.approx(120)
            assert driver.position["Y"] == pytest.approx(80)
            assert fake.received[-3].startswith("G0 X120.0000 Y80.0000 F")
            # Only the axes that change are sent, at the slowest of their speeds (Y: 500).
            assert fake.received[-3].endswith("F30000")
            assert any(0 < x < 120 for x in seen), "position updates during the move"

    run(scenario)


def test_outputs_send_profile_gcode() -> None:
    async def scenario() -> None:
        async with lumenpnp() as (driver, fake):
            await driver.set_output("light_top", True)
            assert fake.received[-1] == "M150 P255 R255 U255 B255 S1"
            assert driver.outputs["light_top"] is True
            await driver.set_output("pump_n2", True)
            assert fake.fans[3] == 180

    run(scenario)


@pytest.mark.parametrize("variant", ["stock", "vacuum-24bit"])
def test_vacuum_readings(variant: str) -> None:
    """Both firmware variants report the same counts; stock only to 1/65536 precision."""

    async def scenario() -> None:
        async with lumenpnp(variant) as (driver, _fake):
            await driver.set_output("pump_n1", True)
            await driver.set_output("valve_n1", True)
            await wait_until(lambda: float(driver.inputs["vac_n1"]) < -1_400_000)
            reading = float(driver.inputs["vac_n1"])
            assert -1_600_000 < reading < -1_400_000
            if variant == "stock":
                assert reading % 65536 == 0
            # Lower N1 (Z down) with suction on: the fake picks a part.
            await driver.move_to({"Z": -25.0}, speed=None)
            await wait_until(lambda: float(driver.inputs["vac_n1"]) < -1_900_000)

    run(scenario)


def test_stop_interrupts_a_move() -> None:
    async def scenario() -> None:
        async with lumenpnp() as (driver, _fake):
            move = asyncio.create_task(driver.move_to({"X": 400.0}, speed=2.0))
            await asyncio.sleep(0.2)
            await driver.stop()
            with pytest.raises(MotionInterruptedError):
                await move
            # Position is resynchronized, and the driver keeps working afterward.
            assert 0 < driver.position["X"] < 400
            await driver.move_to({"Y": 50.0}, speed=None)
            assert driver.position["Y"] == pytest.approx(50)

    run(scenario)


def test_emergency_stop_and_reset() -> None:
    async def scenario() -> None:
        async with lumenpnp(reboot_delay_s=5.0) as (driver, fake):
            await driver.set_output("pump_n1", True)
            move = asyncio.create_task(driver.move_to({"X": 400.0}, speed=2.0))
            await asyncio.sleep(0.2)
            await driver.emergency_stop()
            with pytest.raises(MotionInterruptedError):
                await move
            assert fake.halted
            assert driver.outputs["pump_n1"] is False
            # The firmware comes back (as if reset) and the driver reconnects.
            await driver.reset()
            assert driver.connected
            assert not fake.halted
            await driver.move_to({"X": 10.0}, speed=None)
            assert driver.position["X"] == pytest.approx(10)

    run(scenario)


def test_connection_lost() -> None:
    async def scenario() -> None:
        profile = load_profile("lumenpnp-v4.1")
        assert profile.marlin is not None
        host = FakeMarlinMemory(FakeSettings(time_scale=FAST))
        transports = []

        async def open_and_keep():  # type: ignore[no-untyped-def]
            t = await host.open()
            transports.append(t)
            return t

        driver = MarlinDriver(
            profile, profile.marlin.resolved(None), open_and_keep, connect_wait_s=0
        )
        await driver.connect()
        await host.stop()  # unplug
        await wait_until(lambda: not driver.connected)
        await driver.disconnect()

    run(scenario)


def test_over_a_pseudo_terminal() -> None:
    """The real serial transport, end to end, against the fake on a pty."""

    async def scenario() -> None:
        fake = FakeMarlinPty(FakeSettings(time_scale=FAST))
        path = await fake.start()
        profile = load_profile("lumenpnp-v4.1")
        assert profile.marlin is not None
        driver = MarlinDriver(
            profile,
            profile.marlin.resolved(None),
            lambda: SerialTransport.open(path, 115200),
            connect_wait_s=0,
        )
        try:
            await driver.connect()
            await driver.home()
            await driver.move_to({"X": 33.0}, speed=None)
            assert driver.position["X"] == pytest.approx(33)
        finally:
            await driver.disconnect()
            await fake.stop()

    run(scenario)


# --- The whole core on the Marlin driver -------------------------------------------------


def test_core_with_fake_marlin() -> None:
    from fastapi.testclient import TestClient

    from picnicplace.api import create_app

    from .conftest import connect, fast_config

    config = fast_config(machine={"driver": "fake-marlin", "profile": "lumenpnp-v4.1"})
    with TestClient(create_app(config)) as client, connect(client) as s:
        state = s.state()
        assert state["driver"] == "marlin"
        assert state["connected"] is True
        assert "Marlin" in state["firmware"]
        s.home()
        assert s.move(X=100, Y=200)["status"] == "succeeded"
        pos = s.state()["position"]
        assert (pos["X"], pos["Y"]) == pytest.approx((100, 200))
        assert s.put("/api/v1/machine/outputs/light_top", {"on": True}).status_code == 200


def test_homing_renames_level_to_z_zero() -> None:
    """The stock firmware calls level Z = 31.5; the profile's G92 makes it 0."""
    from fastapi.testclient import TestClient

    from picnicplace.api import create_app

    from .conftest import connect, fast_config

    config = fast_config(machine={"driver": "fake-marlin", "profile": "lumenpnp-v4.1"})
    with TestClient(create_app(config)) as client, connect(client) as s:
        s.home()
        assert s.state()["position"]["Z"] == pytest.approx(0)
        # A lift to safe Z from level leaves the nozzles where they are.
        assert s.move(X=50)["status"] == "succeeded"
        assert s.state()["position"]["Z"] == pytest.approx(0)


def test_homing_works_with_firmware_that_reports_zero() -> None:
    """Other LumenPnP firmware calls level Z = 0 after G28; the G92 changes nothing."""

    async def scenario() -> None:
        async with lumenpnp(level_z=0.0) as (driver, fake):
            await driver.home()
            assert driver.position["Z"] == pytest.approx(0)
            assert fake.target["Z"] == pytest.approx(0)

    run(scenario)


def test_homing_in_another_z_frame_is_refused(tmp_path: Path) -> None:
    """A profile without the G92 (the crash on a real v4.1): homing fails, nothing else
    moves, and the machine is not homed, so nothing can move it until it is fixed."""
    from fastapi.testclient import TestClient

    from picnicplace.api import create_app

    from .conftest import connect, fast_config

    text = (resources.files("picnicplace") / "profiles" / "lumenpnp-v4.1.toml").read_text()
    profile = tmp_path / "no-g92.toml"
    profile.write_text(text.replace('home = ["G28", "G92 Z0"]', 'home = ["G28"]'))
    config = fast_config(machine={"driver": "fake-marlin", "profile": str(profile)})
    with TestClient(create_app(config)) as client, connect(client) as s:
        op = s.wait(s.post("/api/v1/machine/home"))
        assert op["status"] == "failed"
        assert "Z = 31.5" in op["error"] and "G92 Z0" in op["error"]
        state = s.state()
        assert state["homed"] is False
        assert state["position"]["Z"] == pytest.approx(31.5)
        assert (state["position"]["X"], state["position"]["Y"]) == (0, 0)
        assert s.post("/api/v1/machine/move", {"position": {"X": 50}}).json()["code"] == (
            "not_homed"
        )


def test_core_starts_without_the_machine() -> None:
    from fastapi.testclient import TestClient

    from picnicplace.api import create_app

    from .conftest import connect, fast_config

    config = fast_config(
        machine={
            "driver": "marlin",
            "profile": "lumenpnp-v4.1",
            "marlin": {"port": "/dev/does-not-exist", "connect_wait_s": 0},
        }
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        state = s.state()
        assert state["connected"] is False
        assert "no such serial port" in state["connection_error"]
        r = s.post("/api/v1/machine/connect")
        assert r.status_code == 409
        assert r.json()["code"] == "connect_failed"
        assert s.post("/api/v1/machine/home").json()["code"] == "not_connected"


def test_wrong_variant_warns() -> None:
    """Stock firmware that does not know the 24-bit read: the driver says so."""

    async def scenario() -> None:
        async with lumenpnp("vacuum-24bit") as (driver, fake):
            original = fake._cmd_m261  # pyright: ignore[reportPrivateUsage]

            async def stock_m261(line: str, args: dict[str, str]) -> list[str]:
                if args.get("S") == "4":
                    return [f'echo:Unknown command: "{line}"']
                return await original(line, args)

            fake._cmd_m261 = stock_m261  # type: ignore[method-assign]
            await wait_until(lambda: len(driver.warnings) == 2)
            assert "does not understand 'M261 A109 B3 S4'" in driver.warnings[0]
            assert "machine.marlin.variant" in driver.warnings[0]

    run(scenario)


def test_unanswered_command_drops_the_link() -> None:
    async def scenario() -> None:
        profile = load_profile("lumenpnp-v4.1")
        assert profile.marlin is not None
        host = FakeMarlinMemory(FakeSettings(time_scale=FAST))
        driver = MarlinDriver(
            profile,
            profile.marlin.resolved(None),
            host.open,
            connect_wait_s=0,
            command_timeout_s=0.3,
        )
        await driver.connect()

        stuck = asyncio.Event()

        async def hang(_line: str, _args: dict[str, str]) -> list[str]:
            await stuck.wait()
            return []

        host.firmware._cmd_m114 = hang  # type: ignore[method-assign]
        with pytest.raises(MachineError, match="no answer to 'M114'"):
            await driver.move_to({"X": 1.0}, speed=None)
        assert not driver.connected
        assert any("did not answer" in w for w in driver.warnings)
        # The firmware recovers; reconnecting starts clean.
        del host.firmware._cmd_m114
        stuck.set()
        await driver.connect()
        assert driver.connected and not driver.warnings
        await driver.disconnect()
        await host.stop()

    run(scenario)


def test_a_slow_serial_close_does_not_freeze_the_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """After M112 the controller stops reading, and closing its port can wait up to 30 s
    for unsent output: the core must keep running meanwhile (Reset blocked it for about
    a minute on a real LumenPnP)."""
    import os
    import time
    import tty

    real_close = os.close

    def slow_close(fd: int) -> None:
        time.sleep(0.5)  # as if waiting for output to drain
        real_close(fd)

    async def scenario() -> None:
        master, slave = os.openpty()
        tty.setraw(slave)
        transport = await SerialTransport.open(os.ttyname(slave), 115200)
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticker = asyncio.create_task(tick())
        monkeypatch.setattr(os, "close", slow_close)
        await transport.close()
        monkeypatch.setattr(os, "close", real_close)
        ticker.cancel()
        os.close(master)
        os.close(slave)
        assert ticks >= 20  # the event loop ran throughout

    asyncio.run(scenario())
