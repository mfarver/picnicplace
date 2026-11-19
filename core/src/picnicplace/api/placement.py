"""Picking, aligning, and placing single parts, under /api/v1/placement."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import Field

from picnicplace.api.cameras import media_principal
from picnicplace.api.context import Context, require, require_control
from picnicplace.api.routes import MACHINE_ERRORS
from picnicplace.auth import Permission
from picnicplace.feeders.setup import PickCorrection
from picnicplace.placement import PlacementService, PlacementState
from picnicplace.schemas import Operation, WireModel
from picnicplace.training import TrainedPart

router = APIRouter(
    prefix="/api/v1/placement", tags=["placement"], dependencies=[Depends(require(Permission.VIEW))]
)


def get_placement(ctx: Context) -> PlacementService:
    if ctx.placement is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="placement not ready")
    return ctx.placement


Placement = Annotated[PlacementService, Depends(get_placement)]


class PickRequest(WireModel):
    slot_id: str = Field(description="A feeder slot, or a strip or tray on the bed (T1, ...).")
    nozzle_id: str
    feed: bool = Field(default=True, description="Feed the tape first.")
    pick_correction: PickCorrection | None = Field(
        default=None,
        description=(
            "How to keep picks centered: 'top', find each tape pocket with the top camera "
            "first; 'bottom', move each pick by what the bottom camera saw of the feeder's "
            "earlier picks; 'off', neither. Empty: the config's choice."
        ),
    )


class NozzleRequest(WireModel):
    nozzle_id: str


class PlaceRequest(WireModel):
    nozzle_id: str
    x: float
    y: float
    rotation: float = Field(default=0.0, description="The part's angle, in machine degrees.")


class PickAndPlaceRequest(PickRequest):
    x: float
    y: float
    rotation: float = 0.0


def _operation(path: str, summary: str):
    return router.post(
        path,
        status_code=status.HTTP_202_ACCEPTED,
        responses=MACHINE_ERRORS,
        dependencies=[Depends(require_control)],
        summary=summary,
    )


@router.get("")
async def placement_state(placement: Placement) -> PlacementState:
    """What each nozzle holds, how it sits on the nozzle once aligned, and how picks are
    corrected unless asked otherwise."""
    return placement.state()


@_operation("/pick", "Pick a part from a feeder slot, strip, or tray")
async def pick(placement: Placement, body: PickRequest) -> Operation:
    return placement.start_pick(body.slot_id, body.nozzle_id, body.feed, body.pick_correction)


@_operation("/align", "Find a held part with the bottom camera")
async def align(placement: Placement, body: NozzleRequest) -> Operation:
    return placement.start_align(body.nozzle_id)


@_operation("/place", "Place an aligned part")
async def place(placement: Placement, body: PlaceRequest) -> Operation:
    return placement.start_place(body.nozzle_id, body.x, body.y, body.rotation)


@_operation("/pick-and-place", "Pick, align, and place one part")
async def pick_and_place(placement: Placement, body: PickAndPlaceRequest) -> Operation:
    return placement.start_pick_and_place(
        body.slot_id, body.nozzle_id, body.x, body.y, body.rotation, body.feed, body.pick_correction
    )


@_operation("/discard", "Drop a held part in the discard bin")
async def discard(placement: Placement, body: NozzleRequest) -> Operation:
    return placement.start_discard(body.nozzle_id)


@_operation("/release", "A held part was taken off the nozzle by hand")
async def release(placement: Placement, body: NozzleRequest) -> Operation:
    return placement.start_release(body.nozzle_id)


class TrainRequest(WireModel):
    slot_id: str = Field(description="A feeder slot, or a strip or tray on the bed (T1, ...).")
    nozzle_id: str
    pick_correction: PickCorrection | None = None


@_operation("/train", "Train how the bottom camera sees a part")
async def train(placement: Placement, body: TrainRequest) -> Operation:
    """Pick a part, try every light and way of seeing it over the bottom camera, keep the
    best for the part (to be approved), and drop the part in the discard."""
    return placement.start_train(body.slot_id, body.nozzle_id, body.pick_correction)


@router.get("/trained")
async def trained_parts(placement: Placement) -> list[TrainedPart]:
    """Parts trained on this machine, and how the bottom camera sees each."""
    return placement.trained.all()


def _trained(placement: PlacementService, part_id: str) -> TrainedPart:
    trained = placement.trained.get(part_id)
    if trained is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{part_id} is not trained")
    return trained


media = APIRouter(
    prefix="/api/v1/placement", tags=["placement"], dependencies=[Depends(media_principal)]
)
"""Pictures, for <img> tags, which cannot send the token in a header."""


@media.get("/trained/{part_id}/images/{name}", response_class=FileResponse)
async def trained_image(placement: Placement, part_id: str, name: str) -> FileResponse:
    """A picture kept with a trained part: 'overlay' (its footprint drawn over it),
    'beside' (its footprint beside it), or 'light-NN' (under the NNth light tried)."""
    _trained(placement, part_id)
    path = placement.trained.image_path(part_id, name)
    if path is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no picture {name!r}")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/trained/{part_id}/approve", dependencies=[Depends(require_control)])
async def approve_trained(placement: Placement, part_id: str) -> TrainedPart:
    """The footprint lines up with the part, the right way round: use what training
    found from now on."""
    _trained(placement, part_id)
    trained = placement.trained.approve(part_id)
    assert trained is not None
    return trained


@router.delete(
    "/trained/{part_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_control)],
)
async def forget_trained(placement: Placement, part_id: str) -> None:
    """Forget what training found for a part: it is seen the usual way again."""
    _trained(placement, part_id)
    placement.trained.forget(part_id)
