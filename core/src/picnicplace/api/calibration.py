"""The calibration checklist, under /api/v1/calibration (spec §7.7, §8.2)."""

from fastapi import APIRouter, Depends

from picnicplace.api.context import Context, require
from picnicplace.auth import Permission
from picnicplace.calibration import CalibrationChecklist, current

router = APIRouter(
    prefix="/api/v1/calibration",
    tags=["calibration"],
    dependencies=[Depends(require(Permission.VIEW))],
)


@router.get("")
def checklist(ctx: Context) -> CalibrationChecklist:
    """Every calibration this machine needs, in order: whether each is done, its value
    and when it was measured, or that it is out of date and why, and what starts it."""
    return current(ctx.machine, ctx.feeders)
