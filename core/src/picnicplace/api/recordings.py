"""Flight recordings, under /api/v1/recordings: what the machine saw and did, kept to send
to PicnicPlace's developers."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import Field

from picnicplace.api.context import AppContext, Context, require, require_control
from picnicplace.auth import Permission
from picnicplace.job.models import Job
from picnicplace.recorder import RecorderError, RecordingInfo
from picnicplace.schemas import ErrorResponse, WireModel

router = APIRouter(
    prefix="/api/v1/recordings",
    tags=["recordings"],
    dependencies=[Depends(require(Permission.VIEW))],
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse}}
CONFLICT: dict[int | str, dict[str, Any]] = {409: {"model": ErrorResponse}}


class StartRecordingRequest(WireModel):
    label: str = Field(default="", max_length=100, description="What it is for, in a few words.")


def _not_found(e: RecorderError) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e))


@router.get("")
async def list_recordings(ctx: Context) -> list[RecordingInfo]:
    """Kept recordings, newest first, with the one being made, if any."""
    return ctx.machine.recorder.list()


@router.post("", responses=CONFLICT, dependencies=[Depends(require_control)])
async def start_recording(ctx: Context, body: StartRecordingRequest) -> RecordingInfo:
    """Start recording: every frame vision looks at, and everything the machine does,
    until stopped."""
    try:
        return ctx.machine.recorder.start(body.label.strip())
    except RecorderError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e


@router.post("/stop", dependencies=[Depends(require_control)])
async def stop_recording(ctx: Context) -> RecordingInfo | None:
    """Stop the recording being made, if any."""
    return ctx.machine.recorder.stop("stopped by the operator")


@router.get("/{recording_id}", responses=NOT_FOUND)
async def get_recording(ctx: Context, recording_id: str) -> RecordingInfo:
    try:
        return ctx.machine.recorder.get(recording_id)
    except RecorderError as e:
        raise _not_found(e) from e


@router.get(
    "/{recording_id}/download",
    response_class=FileResponse,
    responses={200: {"content": {"application/zip": {}}}, **NOT_FOUND, **CONFLICT},
)
async def download_recording(ctx: Context, recording_id: str) -> FileResponse:
    """The recording as one zip file, to send."""
    recorder = ctx.machine.recorder
    try:
        info = recorder.get(recording_id)
    except RecorderError as e:
        raise _not_found(e) from e
    if info.recording:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="stop the recording first")
    path = recorder.zip(recording_id)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.delete(
    "/{recording_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**NOT_FOUND, **CONFLICT},
    dependencies=[Depends(require(Permission.CONFIG))],
)
async def delete_recording(ctx: Context, recording_id: str) -> None:
    recorder = ctx.machine.recorder
    try:
        info = recorder.get(recording_id)
    except RecorderError as e:
        raise _not_found(e) from e
    if info.recording:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="stop the recording first")
    recorder.delete(recording_id)


def snapshot_builder(
    ctx: AppContext, get_job: Callable[[str], Job | None]
) -> Callable[[str | None], dict[str, Any]]:
    """What a recording keeps of the setup at its start, by file name."""

    def build(job_id: str | None) -> dict[str, Any]:
        config = ctx.config.model_dump(mode="json")
        if config["server"].get("token"):
            config["server"]["token"] = "(set, not recorded)"
        config["data"]["dir"] = "(not recorded)"
        out: dict[str, Any] = {
            "config.json": config,
            "profile.json": ctx.machine.profile.model_dump(mode="json"),
            "machine.json": ctx.machine.state.model_dump(mode="json"),
        }
        part_ids: set[str] = set()
        if ctx.feeders is not None:
            state = ctx.feeders.state()
            out["feeders.json"] = state.model_dump(mode="json")
            part_ids |= {a.part_id for a in state.assignments.values() if a.part_id}
            part_ids |= {f.feeder.part_id for f in state.fixed if f.feeder.part_id}
        job = get_job(job_id) if job_id else None
        if job is not None:
            out["job.json"] = job.model_dump(mode="json")
            part_ids |= {p.part_id for p in job.board.placements if p.part_id}
        library = ctx.library
        if library is not None:
            parts = [p for p in (library.find_part(i) for i in sorted(part_ids)) if p]
            footprints = {p.footprint_id for p in parts if p.footprint_id}
            out["parts.json"] = {
                "parts": [p.model_dump(mode="json") for p in parts],
                "footprints": [
                    f.model_dump(mode="json")
                    for f in (library.find_footprint(i) for i in sorted(footprints))
                    if f
                ],
            }
        return out

    return build
