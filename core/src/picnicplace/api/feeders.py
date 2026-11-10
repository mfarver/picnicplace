"""Feeders and the bring-up record, under /api/v1."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from picnicplace.api.context import Context, require, require_control
from picnicplace.api.routes import MACHINE_ERRORS
from picnicplace.auth import Permission
from picnicplace.bringup import BringUpRecord
from picnicplace.feeders.photon import parse_uuid
from picnicplace.feeders.service import FeederService, FeedersState, FixedFeederState
from picnicplace.feeders.setup import FeederAssignment, FixedFeeder
from picnicplace.machine.profile import Point
from picnicplace.schemas import Operation, WireModel

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require(Permission.VIEW))])


def get_feeders(ctx: Context) -> FeederService:
    if ctx.feeders is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="feeders not ready")
    return ctx.feeders


Feeders = Annotated[FeederService, Depends(get_feeders)]


class FeedRequest(WireModel):
    distance: float | None = Field(
        default=None,
        ge=-50,
        le=50,
        description="Millimeters; negative feeds backward. Empty: one part's pitch.",
    )


class IdentifyResult(WireModel):
    answered: bool


class PickRequest(WireModel):
    at: Point | None = Field(
        default=None, description="The pick location. Empty: where the top camera is now."
    )


class RailsRequest(WireModel):
    bank_ids: list[str] = Field(
        default_factory=list[str],
        description="Rails (feeder bank IDs) to calibrate. Empty: every rail.",
    )


class CalibrateRequest(WireModel):
    slot_ids: list[str] = Field(
        default_factory=list[str],
        description="Slots to calibrate. Empty: every slot with a feeder found.",
    )


@router.get("/feeders", tags=["feeders"])
async def feeders_state(feeders: Feeders) -> FeedersState:
    """Every feeder slot, the feeder found in it, and where to pick from it."""
    return feeders.state()


@router.post(
    "/feeders/scan",
    tags=["feeders"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def scan(feeders: Feeders) -> Operation:
    """Ask every slot on the feeder bus for its feeder, and initialize the ones found."""
    return feeders.start_scan()


@router.post(
    "/feeders/slots/{slot_id}/feed",
    tags=["feeders"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def feed(feeders: Feeders, slot_id: str, body: FeedRequest) -> Operation:
    """Advance a feeder's tape."""
    return feeders.start_feed(slot_id, body.distance)


@router.post(
    "/feeders/slots/{slot_id}/identify",
    tags=["feeders"],
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def identify(feeders: Feeders, slot_id: str) -> IdentifyResult:
    """Ask the feeder in a slot to flash its light."""
    return IdentifyResult(answered=await feeders.identify(slot_id))


@router.put(
    "/feeders/slots/{slot_id}/pick",
    tags=["feeders"],
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def teach_pick(
    ctx: Context, feeders: Feeders, slot_id: str, body: PickRequest
) -> FeedersState:
    """Teach a slot's pick location: by default, where the top camera is centered now."""
    feeders.teach_pick(slot_id, body.at or ctx.machine.camera_position())
    return feeders.state()


@router.delete(
    "/feeders/slots/{slot_id}/pick",
    tags=["feeders"],
    dependencies=[Depends(require(Permission.CONFIG))],
)
async def forget_pick(feeders: Feeders, slot_id: str) -> FeedersState:
    """Go back to the profile's nominal pick location for a slot."""
    feeders.forget_pick(slot_id)
    return feeders.state()


@router.delete(
    "/feeders/learned/{source_id}",
    tags=["feeders"],
    dependencies=[Depends(require(Permission.CONFIG))],
)
async def forget_learned(feeders: Feeders, source_id: str) -> FeedersState:
    """Forget what the bottom camera has learned of a slot's, strip's, or tray's picks."""
    feeders.forget_learned(source_id)
    return feeders.state()


@router.post(
    "/feeders/slots/{slot_id}/pocket",
    tags=["feeders"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def locate_pocket(feeders: Feeders, slot_id: str) -> Operation:
    """Find the pick pocket from the tape's sprocket holes, and center the camera on it."""
    return feeders.start_locate_pocket(slot_id)


@router.post(
    "/feeders/slots/{slot_id}/fiducial",
    tags=["feeders"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def measure_fiducial(feeders: Feeders, slot_id: str) -> Operation:
    """With the top camera over this slot's feeder fiducial, record where the fiducial is
    relative to the pick location. Teach the slot's pick location first."""
    return feeders.start_measure_fiducial(slot_id)


@router.post(
    "/feeders/calibrate",
    tags=["feeders"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def calibrate(feeders: Feeders, body: CalibrateRequest) -> Operation:
    """Find each slot's feeder fiducial and correct the slot's pick location."""
    return feeders.start_calibrate(body.slot_ids or None)


@router.post(
    "/feeders/rails/calibrate",
    tags=["feeders"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def calibrate_rails(feeders: Feeders, body: RailsRequest) -> Operation:
    """Find where each rail really is, from the fiducials of the two feeders farthest
    apart on it, and move its slots there. Its slot corrections are cleared."""
    return feeders.start_calibrate_rails(body.bank_ids or None)


@router.post(
    "/feeders/slots/{slot_id}/rail-fiducial",
    tags=["feeders"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def rail_fiducial_here(feeders: Feeders, slot_id: str) -> Operation:
    """The top camera is centered on this slot's feeder fiducial (the rail search did not
    find it): find it there, and fit the slot's rail with it."""
    return feeders.start_rail_fiducial_here(slot_id)


@router.delete(
    "/feeders/rails/{bank_id}",
    tags=["feeders"],
    dependencies=[Depends(require(Permission.CONFIG))],
)
async def forget_rail(feeders: Feeders, bank_id: str) -> FeedersState:
    """Go back to the profile's place for a rail."""
    feeders.forget_rail(bank_id)
    return feeders.state()


@router.put(
    "/feeders/assignments/{feeder_id}",
    tags=["feeders"],
    dependencies=[Depends(require(Permission.JOB))],
    responses={422: {"description": "Not a feeder ID."}},
)
async def assign(feeders: Feeders, feeder_id: str, body: FeederAssignment) -> FeedersState:
    """Say which part a feeder holds. The part follows the feeder from slot to slot."""
    _check_feeder_id(feeder_id)
    feeders.assign(feeder_id.upper(), body)
    return feeders.state()


@router.delete(
    "/feeders/assignments/{feeder_id}",
    tags=["feeders"],
    dependencies=[Depends(require(Permission.JOB))],
)
async def forget(feeders: Feeders, feeder_id: str) -> FeedersState:
    feeders.forget(feeder_id.upper())
    return feeders.state()


class RefillRequest(WireModel):
    picked: int = Field(
        default=0, ge=0, description="Parts already gone from the start of it. 0: it is full."
    )


class TeachPartRequest(WireModel):
    which: Literal["first", "row_end", "last"] = Field(
        description=(
            "The first part; the last part of the first row (sets the step along a row); "
            "or the last part of the last row (sets the step from row to row)."
        )
    )
    at: Point | None = Field(
        default=None, description="Where the part is. Empty: where the top camera is now."
    )


@router.post(
    "/feeders/fixed",
    tags=["feeders"],
    status_code=status.HTTP_201_CREATED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require(Permission.JOB))],
)
async def add_fixed(feeders: Feeders, body: FixedFeeder) -> FixedFeederState:
    """Add a strip of tape or a tray of parts held on the bed. It gets the next free ID:
    T1, T2, and so on."""
    return feeders.fixed_state(feeders.add_fixed(body))


@router.put(
    "/feeders/fixed/{fixed_id}",
    tags=["feeders"],
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require(Permission.JOB))],
)
async def put_fixed(feeders: Feeders, fixed_id: str, body: FixedFeeder) -> FixedFeederState:
    """Change a strip or tray (or add one with this ID)."""
    feeders.put_fixed(fixed_id, body)
    return feeders.fixed_state(fixed_id)


@router.delete(
    "/feeders/fixed/{fixed_id}",
    tags=["feeders"],
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require(Permission.JOB))],
)
async def remove_fixed(feeders: Feeders, fixed_id: str) -> FeedersState:
    feeders.remove_fixed(fixed_id)
    return feeders.state()


@router.post(
    "/feeders/fixed/{fixed_id}/refill",
    tags=["feeders"],
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require(Permission.JOB))],
)
async def refill(feeders: Feeders, fixed_id: str, body: RefillRequest) -> FixedFeederState:
    """The strip or tray was refilled: start again from its first part."""
    feeders.refill(fixed_id, body.picked)
    return feeders.fixed_state(fixed_id)


@router.put(
    "/feeders/fixed/{fixed_id}/teach",
    tags=["feeders"],
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def teach_fixed(
    ctx: Context, feeders: Feeders, fixed_id: str, body: TeachPartRequest
) -> FixedFeederState:
    """Teach where a part of a strip or tray is: by default, where the top camera is
    centered now. Teach the first part before the others."""
    feeders.teach_fixed(fixed_id, body.which, body.at or ctx.machine.camera_position())
    return feeders.fixed_state(fixed_id)


@router.post(
    "/feeders/fixed/{fixed_id}/pocket",
    tags=["feeders"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def locate_strip_pocket(feeders: Feeders, fixed_id: str) -> Operation:
    """Find a strip's next pocket from its sprocket holes, and center the camera on it."""
    return feeders.start_locate_strip_pocket(fixed_id)


def _check_feeder_id(feeder_id: str) -> None:
    try:
        parse_uuid(feeder_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e


@router.get("/bring-up", tags=["bring-up"])
async def get_bring_up(ctx: Context) -> BringUpRecord:
    """Progress through the bring-up checklist, and what it found."""
    return ctx.bring_up.get()


@router.put("/bring-up", tags=["bring-up"], dependencies=[Depends(require(Permission.CONFIG))])
async def put_bring_up(ctx: Context, body: BringUpRecord) -> BringUpRecord:
    ctx.bring_up.put(body)
    return body
