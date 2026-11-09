"""Running jobs, under /api/v1/run: plan one (a dry run), start, pause, step, resume, stop."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from picnicplace.api.context import Context, require, require_control
from picnicplace.api.routes import MACHINE_ERRORS
from picnicplace.auth import Permission
from picnicplace.feeders.setup import PickCorrection
from picnicplace.job.run import JobPlan, JobRunner, RunState
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


class SkipRequest(WireModel):
    skip: bool = Field(description="True to skip the placement, false to do it after all.")


@router.get("")
async def run_state(runner: Runner) -> RunState | None:
    """The current (or last) run, with every placement's status. None before any run."""
    return runner.state()


@router.post("/plan", responses=MACHINE_ERRORS)
async def plan(runner: Runner, body: PlanRequest) -> JobPlan:
    """What a run of a saved job would do, without moving anything: each placement's
    feeder and nozzles, and what stops any of them (no part number, part, height, or
    feeder, or out of reach). A dry run against the loaded feeders."""
    return runner.plan(body.job_id)


@router.post(
    "/start",
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=Control,
)
async def start(runner: Runner, body: StartRequest) -> Operation:
    """Run a saved job: find the board's fiducials, then pick, align, and place every part."""
    return runner.start(body.job_id, body.skip_blocked, body.pick_correction)


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
