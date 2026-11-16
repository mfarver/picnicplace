"""Running a stop of the setup wizard (spec §7.7): one Start runs every step the machine
can do on its own, one after another, as one operation, stopping at the first that
fails and saying what to check.

Start skips the steps already done and still good (homing, if homed; visual homing, if
done since homing); Run again redoes them. Stop 1, with a hand on the power switch: home
(unless homed), visual homing, the top
camera's scale, the bottom camera, naming the tip on each nozzle by camera, and the
feeders (scanned, and their rails calibrated). Stop 2, with a paper disc on the datum
board: find the disc, and measure each nozzle's offset, height and vacuum levels.

Each step's progress is kept on the wizard (``Wizard.run``), so any Studio can show it as
it runs. Stop (``MachineService.request_stop``) ends the run before its next move,
leaving the machine homed.
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

from picnicplace.machine.bottom_camera import bottom_camera_work, identify_tips
from picnicplace.machine.errors import FeederError, InvalidTargetError, StoppedError
from picnicplace.machine.nozzle_offsets import nozzle_offsets_work
from picnicplace.machine.tip_heights import measured_offset, tip_heights_work, tips_to_measure
from picnicplace.schemas import Operation
from picnicplace.wizard import RunStep, WizardRun

if TYPE_CHECKING:
    from picnicplace.feeders.service import FeederService
    from picnicplace.machine.service import MachineService
    from picnicplace.wizard import Wizard

StepWork = Callable[[], Awaitable[str | None]]
Step = tuple[str, str, StepWork | str]
"""An ID, a title, and its work, or why it is skipped."""


class Skip(Exception):
    """A step that does not apply after all (says why)."""


def _stop1(
    wizard: "Wizard", machine: "MachineService", feeders: "FeederService | None", redo: bool
) -> list[Step]:
    profile = machine.profile
    status = {p.id: p.status for p in wizard.state().pages}

    def unless_done(page: str, work: Callable[[], Awaitable[str | None]]) -> StepWork:
        """Skip a step whose page is done and still good, unless redoing them all."""

        async def step() -> str | None:
            if not redo and status.get(page) == "done":
                raise Skip("already done (Run again redoes it)")
            return await work()

        return step

    has_fiducial = any(loc.kind == "homing_fiducial" for loc in profile.locations)
    has_bottom = any(c.looking == "up" for c in profile.cameras)
    no_fiducial = "this machine's profile has no homing fiducial"

    homed_now = False

    async def home() -> str | None:
        nonlocal homed_now
        if machine.homed:
            raise Skip("already homed")
        homed_now = True
        return await machine.home_now() or "Homed."

    async def tips() -> str:
        return await identify_tips(machine)

    async def visual_home() -> str:
        if machine.visually_homed and (not redo or homed_now):
            raise Skip("done when homing")
        return await machine.visual_home_now()

    async def feeders_step() -> str:
        if feeders is None or (not feeders.has_bus and not profile.feeder_banks):
            raise Skip("this machine has no feeders")
        notes: list[str] = []
        if feeders.has_bus:
            notes.append(await feeders.scan())
        try:
            work = feeders.calibrate_rails_work()
        except FeederError as e:
            raise Skip(f"{' '.join(notes)} Rails not calibrated: {e}".strip()) from e
        notes.append(await work())
        return " ".join(notes)

    return [
        ("home", "Home", home),
        (
            "visual_home",
            "Visual homing",
            visual_home if has_fiducial else no_fiducial,
        ),
        (
            "camera_scale",
            "Top camera scale",
            unless_done("top_camera_scale", machine.measure_camera_scale_now)
            if has_fiducial
            else no_fiducial,
        ),
        (
            "bottom_camera",
            "Bottom camera",
            unless_done("bottom_camera", lambda: bottom_camera_work(machine, None)())
            if has_bottom
            else "this machine has no bottom camera",
        ),
        (
            "tips",
            "Nozzle tips",
            unless_done("nozzle_tips", tips)
            if has_bottom and profile.nozzle_tips
            else "this machine's profile lists no nozzle tips",
        ),
        ("feeders", "Feeders", unless_done("feeders", feeders_step)),
    ]


def _stop2(machine: "MachineService", redo: bool) -> list[Step]:
    # Offsets measured, and up to date but for a tip fitted since: only its height is new,
    # measured on a paper square (a tip too big for the disc needs it).
    line = machine.calibration.get().lines.get("nozzle_offsets")
    offsets_kept = (
        line is not None
        and line.stale is None
        and all(measured_offset(machine, n) for n in machine.profile.nozzles)
    )
    if not redo and offsets_kept and tips_to_measure(machine):
        work = tip_heights_work(machine, None)
        return [("nozzle_offsets", "Find the paper, and measure each new tip's height", work)]
    work = nozzle_offsets_work(machine, None, None, None, search=True)
    return [("nozzle_offsets", "Find the disc, and measure each nozzle", work)]


def start_stop(
    wizard: "Wizard",
    machine: "MachineService",
    feeders: "FeederService | None",
    stop: Literal[1, 2],
    redo: bool = False,
) -> Operation:
    """Run a stop's steps, one after another, as one operation. Steps already done and
    still good are skipped, unless ``redo`` (Run again)."""
    if stop == 2 and not machine.homed:
        raise InvalidTargetError("home the machine first (the first stop does)")
    steps = _stop1(wizard, machine, feeders, redo) if stop == 1 else _stop2(machine, redo)
    run = WizardRun(
        stop=stop,
        steps=[
            RunStep(
                id=step_id,
                title=title,
                status="skipped" if isinstance(work, str) else "waiting",
                message=work if isinstance(work, str) else None,
            )
            for step_id, title, work in steps
        ],
        running=True,
    )
    wizard.run = run

    async def work_all() -> str:
        try:
            for (_, _, work), record in zip(steps, run.steps, strict=True):
                if isinstance(work, str):
                    continue
                record.status = "running"
                try:
                    message = await work()
                except Skip as e:
                    record.status, record.message = "skipped", str(e)
                    continue
                except StoppedError:
                    record.status = "stopped"
                    record.message = "Stopped by the operator; Start carries on from here."
                    raise
                except Exception as e:
                    record.status, record.message = "failed", str(e) or type(e).__name__
                    raise
                record.status, record.message = "done", message or "Done."
            done = [s.title for s in run.steps if s.status == "done"]
            return f"Done: {', '.join(done) or 'nothing to do'}."
        finally:
            run.running = False
            await machine.raise_nozzles()

    return machine.start_sequence("wizard_run", work_all)
