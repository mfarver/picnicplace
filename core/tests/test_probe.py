"""The bring-up probe, against the fake LumenPnP firmware."""

import asyncio

import pytest

from picnicplace.machine.marlin.fake import FakeMarlinMemory, FakeSettings
from picnicplace.machine.profile import load_profile
from picnicplace.probe import Prober, ProbeReport, run_probe, summarize


def probe(with_pumps: bool) -> tuple[ProbeReport, list[str]]:
    async def scenario() -> tuple[ProbeReport, list[str]]:
        host = FakeMarlinMemory(FakeSettings(time_scale=50))
        transport = await host.open()
        report = await run_probe(
            transport, load_profile("lumenpnp-v4.1"), connect_wait_s=0, with_pumps=with_pumps
        )
        received = list(host.firmware.received)
        await host.stop()
        return report, received

    return asyncio.run(asyncio.wait_for(scenario(), 20))


def test_probe_reads_without_moving() -> None:
    report, received = probe(with_pumps=False)
    assert report.firmware == "Marlin bugfix-2.1.x (fake)"
    assert report.machine_type == "LumenPnP"
    assert report.capabilities["EMERGENCY_PARSER"] == "1"
    assert report.endstops[0] == "Reporting endstop status"
    assert report.position is not None and report.position.startswith("X:")
    # Both firmware variants were tried; at rest the readings are near zero.
    assert set(report.vacuum) == {"stock", "vacuum-24bit"}
    assert abs(float(report.vacuum["vacuum-24bit"]["vac_n1"])) < 10_000
    # Nothing that moves the machine, and no outputs switched.
    words = {line.split()[0] for line in received}
    assert not words & {"G0", "G1", "G28", "M106"}
    text = summarize(report, load_profile("lumenpnp-v4.1"))
    assert "Emergency parser: yes" in text


def test_probe_with_pumps_sees_suction() -> None:
    report, received = probe(with_pumps=True)
    n1 = report.suction["vacuum-24bit"]["vac_n1"]
    assert isinstance(n1, float) and n1 < -1_000_000
    stock = report.suction["stock"]["vac_n1"]
    assert isinstance(stock, float) and abs(stock - n1) < 70_000  # one 8-bit step
    assert "M107 P1" in received  # pumps switched off again


def test_probe_refuses_motion_commands() -> None:
    async def scenario() -> None:
        host = FakeMarlinMemory(FakeSettings(time_scale=50))
        prober = Prober(await host.open(), ProbeReport())
        with pytest.raises(ValueError, match="does not send motion"):
            await prober.send("G28")
        await prober.close()
        await host.stop()

    asyncio.run(scenario())
