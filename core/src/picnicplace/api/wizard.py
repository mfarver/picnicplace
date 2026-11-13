"""The setup and calibration wizard, under /api/v1/wizard (spec §7.7, §8.2)."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from picnicplace.api.context import Context, require, require_control
from picnicplace.auth import Permission
from picnicplace.wizard import ConfirmedPage, WizardState

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


@router.get("")
def wizard(ctx: Context) -> WizardState:
    """Every page of the setup wizard, in order, with where each stands; the machine's
    stage (limited, out of date, or calibrated), and the page Continue opens."""
    return ctx.wizard.state()


@router.put("/pages/{page_id}", dependencies=[Depends(require_control)])
def set_page(ctx: Context, page_id: ConfirmedPage, body: PageRequest) -> WizardState:
    """Finish a page the owner confirms (the tips fitted, the discard and park locations),
    skip an optional one (strips and trays), or open one again. Pages the machine
    measures are finished by measuring them."""
    state = ctx.wizard.state()
    page = next(p for p in state.pages if p.id == page_id)
    if body.status == "skipped" and not page.optional:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{page.title} cannot be skipped: the machine needs it",
        )
    if body.status == "done" and page_id == "nozzle_tips" and page.status == "todo":
        unsaid = [n.label for n in ctx.machine.profile.nozzles if n.tip is None]
        if unsaid:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"say which tip is on {', '.join(unsaid)} first",
            )
    ctx.wizard.store.set(page_id, body.status)
    return ctx.wizard.state()
