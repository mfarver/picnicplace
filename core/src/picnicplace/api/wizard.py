"""The setup and calibration wizard, under /api/v1/wizard (spec §7.7, §8.2)."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from picnicplace.api.context import Context, require, require_control
from picnicplace.auth import Permission
from picnicplace.schemas import Operation
from picnicplace.wizard import ConfirmedPage, WizardState
from picnicplace.wizard_run import start_stop

router = APIRouter(
    prefix="/api/v1/wizard",
    tags=["wizard"],
    dependencies=[Depends(require(Permission.VIEW))],
)


class PageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["done", "skipped", "todo"] = Field(
        description="'done': the owner confirms it. 'skipped': an optional page, skipped. "
        "'todo': open it again."
    )


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stop: Literal[1, 2] = Field(
        description="1: everything the machine can do with a hand on the power switch "
        "(homing, visual homing, the cameras, naming the tips, the feeders). 2: with a paper "
        "disc on the datum board, each nozzle's offset, height and vacuum levels."
    )
    redo: bool = Field(
        default=False,
        description="Redo every step (Run again), not only those still to do or out of date.",
    )


@router.post(
    "/run",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require(Permission.MOTION)), Depends(require_control)],
)
async def run_stop(ctx: Context, body: RunRequest) -> Operation:
    """Run a stop of the wizard: its steps one after another, as one operation, stopping
    at the first that fails. Progress is in the wizard's ``run``."""
    return start_stop(ctx.wizard, ctx.machine, ctx.feeders, body.stop, body.redo)


@router.get("")
def wizard(ctx: Context) -> WizardState:
    """Every page of the setup wizard, in order, with where each stands; the machine's
    stage (limited, out of date, or calibrated), and the page Continue opens."""
    return ctx.wizard.state()


@router.put("/pages/{page_id}", dependencies=[Depends(require_control)])
def set_page(ctx: Context, page_id: ConfirmedPage, body: PageRequest) -> WizardState:
    """Finish a page the owner confirms (the discard and park locations),
    skip an optional one (strips and trays), or open one again. Pages the machine
    measures are finished by measuring them."""
    state = ctx.wizard.state()
    page = next(p for p in state.pages if p.id == page_id)
    if body.status == "skipped" and not page.optional:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{page.title} cannot be skipped: the machine needs it",
        )
    ctx.wizard.store.set(page_id, body.status)
    return ctx.wizard.state()
