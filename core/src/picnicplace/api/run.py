"""Running jobs, under /api/v1/run: plan one (a dry run), start, pause, step, resume, stop."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from picnicplace.api.context import Context, require, require_control
from picnicplace.api.routes import MACHINE_ERRORS
from picnicplace.auth import Permission
from picnicplace.feeders.setup import PickCorrection
from picnicplace.job.run import BoardCheck, JobPlan, JobRunner, RunState
from picnicplace.schemas import Operation, WireModel

router = APIRouter(
    prefix="/api/v1/run", tags=["run"], dependencies=[Depends(require(Permission.VIEW))]
)


def get_runner(ctx: Context) -> JobRunner:
    if ctx.runner is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="the runner is not ready")
    return ctx.runner


Runner = Annotated[JobRunner, Depends(get_runner)]
Control = [Depends(require_control)]


class PlanRequest(WireModel):
    job_id: str
    revision: int | None = Field(
        default=None,
        description="The published revision to run. Empty: the job's newest.",
    )


class StartRequest(PlanRequest):
    skip_blocked: bool = Field(
        default=False, description="Start even if some placements are blocked, skipping them."
    )
    pick_correction: PickCorrection | None = Field(
        default=None,
        description=(
            "How to keep picks centered: 'top', find each tape pocket with the top camera "
            "first; 'bottom', move each pick by what the bottom camera saw of the feeder's "
            "earlier picks; 'off', neither. Empty: the config's choice."
        ),
    )
    step: bool = Field(
        default=False,
        description=(
            "Pause once the board is found, before the first part; each step then places "
            "one batch (a part on each nozzle that can take one)."
        ),
    )
    record: bool = Field(
        default=False,
        description=(
            "Keep a flight recording of the run (the frames vision looked at and what the "
            "machine did), if nothing else is being recorded. The config can record every "
            "run."
        ),
    )


class SkipRequest(WireModel):
    skip: bool = Field(description="True to skip the placement, false to do it after all.")


@router.get("")
async def run_state(runner: Runner) -> RunState | None:
    """The current (or last) run, with every placement's status. None before any run."""
    return runner.state()


@router.post("/plan", responses=MACHINE_ERRORS)
async def plan(runner: Runner, body: PlanRequest) -> JobPlan:
    """What a run of a job's published revision would do, without moving anything: each
    placement's feeder and nozzles, and what stops any of them (no part number, part,
    height, or feeder, or out of reach). A dry run against the loaded feeders. A job not
    yet published is planned from its working copy, with an issue saying it cannot run."""
    return runner.plan(body.job_id, body.revision)


@router.post(
    "/start",
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=Control,
)
async def start(runner: Runner, body: StartRequest) -> Operation:
    """Run a job's published revision (its newest unless one is given), set up as this
    machine keeps it: find the board's fiducials, then pick, align, and place every part."""
    return runner.start(
        body.job_id, body.skip_blocked, body.pick_correction, body.record, body.step, body.revision
    )


@router.post(
    "/check-board",
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=Control,
)
async def check_board(runner: Runner, body: PlanRequest) -> Operation:
    """Find a saved job's board by its fiducials and fit it, placing nothing: to check the
    board and the job's location before a run. Works even with no placements turned on.
    The result is kept (``GET /api/v1/run/board-check``)."""
    return runner.check_board(body.job_id, body.revision)


@router.get("/board-check")
async def board_check(runner: Runner) -> BoardCheck | None:
    """The last board check: the fiducials found, the fit, and whether it is good."""
    return runner.board_check()


@router.post(
    "/resume",
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=Control,
)
async def resume(runner: Runner) -> Operation:
    return runner.resume()


@router.post(
    "/step",
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=Control,
)
async def step(runner: Runner) -> Operation:
    """Resume a paused run for one batch (a part on each nozzle that can take one)."""
    return runner.resume(step=True)


@router.post("/pause", responses=MACHINE_ERRORS, dependencies=Control)
async def pause(runner: Runner) -> RunState:
    """Pause once the parts on the nozzles are placed."""
    return runner.pause()


@router.post("/stop", responses=MACHINE_ERRORS, dependencies=Control)
async def stop(runner: Runner) -> RunState:
    """End the run, once the parts on the nozzles are placed if it is running."""
    return runner.stop()


@router.put("/items/{index}", responses=MACHINE_ERRORS, dependencies=Control)
async def skip(runner: Runner, index: int, body: SkipRequest) -> RunState:
    """Skip a placement, or take back a skip (or retry a failed placement)."""
    return runner.skip(index, body.skip)
