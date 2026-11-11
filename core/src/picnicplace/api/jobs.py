"""Job endpoints: saved jobs, their published revisions, this machine's setup for each,
and where a job's placements land on the machine."""

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, ValidationError

from picnicplace.api.context import Context, require
from picnicplace.auth import Permission
from picnicplace.job.layout import JobLayout, find_location, layout
from picnicplace.job.models import Job, JobSetup, MachineLocation
from picnicplace.job.revision import JobFileError, JobRevision, load, make_revision
from picnicplace.job.store import (
    JOB_ID,
    JobStore,
    JobSummary,
    RevisionConflict,
    RevisionSummary,
    job_id_for,
)
from picnicplace.schemas import ErrorResponse, WireModel

router = APIRouter(
    prefix="/api/v1/jobs", tags=["jobs"], dependencies=[Depends(require(Permission.VIEW))]
)
CanEdit = Depends(require(Permission.JOB))

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No job with this ID."}
}
REVISION_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No such job or revision."}
}


def get_store(ctx: Context) -> JobStore:
    return JobStore(ctx.config.data.jobs_dir, ctx.config.data.setups_dir)


Store = Annotated[JobStore, Depends(get_store)]


def _checked(job_id: str) -> str:
    if not JOB_ID.match(job_id):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="job IDs are letters, digits, '.', '_', and '-'",
        )
    return job_id


@router.get("")
def list_jobs(store: Store) -> list[JobSummary]:
    return store.list()


@router.post("/layout")
def job_layout(ctx: Context, job: Job) -> JobLayout:
    """Where each placement of a job (saved or not) lands on this machine, and anything
    that stops the machine from doing it: placements out of reach, fiducials the
    camera cannot see, or the board over a keep-out."""
    return layout(job, ctx.machine.profile)


@router.post(
    "/find-location",
    responses={409: {"model": ErrorResponse, "description": "The board fits nowhere."}},
)
def job_find_location(ctx: Context, job: Job) -> MachineLocation:
    """A place on the bed where the job fits: every placement within reach of every
    nozzle, fiducials where the camera sees them, clear of the keep-outs."""
    location = find_location(job, ctx.machine.profile)
    if location is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                "the board fits nowhere on this machine's placement area "
                "clear of its keep-outs, at any quarter turn"
            ),
        )
    return location


@router.get("/{job_id}", responses=NOT_FOUND)
def get_job(store: Store, job_id: str) -> Job:
    """A job's working copy, with this machine's setup for it (location, board height)."""
    job = store.get(_checked(job_id))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no job {job_id!r}")
    return job


@router.put("/{job_id}", dependencies=[CanEdit])
def put_job(store: Store, job_id: str, job: Job) -> Job:
    """Save a job's working copy, creating or replacing it. Its location and board height
    are kept as this machine's setup for the job, not in the job."""
    store.put(_checked(job_id), job)
    return job


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND,
    dependencies=[CanEdit],
)
def delete_job(store: Store, job_id: str) -> None:
    if not store.delete(_checked(job_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no job {job_id!r}")


# --- This machine's setup ----------------------------------------------------------------


def _existing(store: JobStore, job_id: str) -> str:
    if not store.exists(_checked(job_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no job {job_id!r}")
    return job_id


@router.get("/{job_id}/setup", responses=NOT_FOUND)
def get_setup(store: Store, job_id: str) -> JobSetup:
    """Where this machine has the job's panel on the bed, and the board's measured height.
    Kept by the machine, apart from the job and its revisions."""
    return store.setup(_existing(store, job_id))


@router.put(
    "/{job_id}/setup", responses=NOT_FOUND, dependencies=[Depends(require(Permission.MOTION))]
)
def put_setup(store: Store, job_id: str, setup: JobSetup) -> JobSetup:
    """Set where the job's panel sits on this machine, and the board's height. Works for a
    job loaded from a file too, which has no working copy."""
    store.put_setup(_existing(store, job_id), setup)
    return setup


@router.get("/{job_id}/layout", responses=REVISION_NOT_FOUND)
def get_job_layout(
    ctx: Context, store: Store, job_id: str, revision: int | None = None
) -> JobLayout:
    """Where each placement of what runs for the job (``revision``, else its newest, else
    its working copy) lands on this machine, as set up here."""
    try:
        job, _ = store.resolve(_checked(job_id), revision)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except JobFileError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
    return layout(job, ctx.machine.profile)


# --- Revisions ----------------------------------------------------------------------------


@router.get("/{job_id}/revisions", responses=NOT_FOUND)
def list_revisions(store: Store, job_id: str) -> list[RevisionSummary]:
    """A job's published revisions, oldest first."""
    return store.revisions(_checked(job_id))


class PublishRequest(BaseModel):
    author: str = Field(min_length=1, max_length=200, description="Who is publishing it.")
    reason: str = Field(default="", max_length=2000, description="What changed, and why.")


@router.post(
    "/{job_id}/revisions",
    responses={
        **NOT_FOUND,
        409: {"model": ErrorResponse, "description": "Another revision was just published."},
    },
    dependencies=[CanEdit],
)
def publish_revision(ctx: Context, store: Store, job_id: str, body: PublishRequest) -> JobRevision:
    """Publish the job's working copy as its next revision, with a copy of every part,
    footprint, and tape it uses from the library. A revision never changes afterward."""
    job = store.get(_checked(job_id))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no job {job_id!r}")
    revision = make_revision(
        job_id,
        job,
        ctx.library,
        number=store.next_number(job_id),
        author=body.author,
        reason=body.reason,
    )
    try:
        store.add_revision(revision)
    except RevisionConflict as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"{e}: try again") from e
    return revision


def _revision_or_404(store: JobStore, job_id: str, number: int) -> JobRevision:
    try:
        revision = store.revision(_checked(job_id), number)
    except JobFileError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
    if revision is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"job {job_id!r} has no revision {number}"
        )
    return revision


@router.get("/{job_id}/revisions/{number}", responses=REVISION_NOT_FOUND)
def get_revision(store: Store, job_id: str, number: int) -> JobRevision:
    return _revision_or_404(store, job_id, number)


@router.get(
    "/{job_id}/revisions/{number}/file",
    response_class=Response,
    responses={**REVISION_NOT_FOUND, 200: {"content": {"application/json": {}}}},
)
def download_revision(store: Store, job_id: str, number: int) -> Response:
    """The revision's job file, exactly as kept, to save or take to another machine."""
    _revision_or_404(store, job_id, number)
    text = store.revision_text(job_id, number) or ""
    name = f"{job_id}-r{number:04d}.json"
    return Response(
        content=text,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


class ImportJobRequest(BaseModel):
    text: str = Field(description="The job file's contents.")


class ImportedJob(WireModel):
    job_id: str
    kind: Literal["revision", "working-copy"] = Field(
        description=(
            "A revision is kept as published; a job file saved before revisions becomes "
            "a working copy, to publish from Prepare."
        )
    )
    number: int | None = Field(default=None, description="The revision's number.")
    already_kept: bool = Field(
        default=False, description="This very revision was already here; nothing changed."
    )


@router.post(
    "/import",
    responses={
        400: {"model": ErrorResponse, "description": "The file is not a job file."},
        409: {"model": ErrorResponse, "description": "It clashes with a job already here."},
    },
    dependencies=[CanEdit],
)
def import_job(store: Store, body: ImportJobRequest) -> ImportedJob:
    """Load a job file from disk: a published revision, or a job saved before revisions."""
    try:
        version: object = json.loads(body.text).get("version")
    except (json.JSONDecodeError, AttributeError):
        version = None
    if version == 1:
        try:
            job = Job.model_validate_json(body.text)
        except ValidationError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"the job file is not valid: {e.errors()[0]['msg']}",
            ) from e
        job_id = job_id_for(job.name)
        if store.get(job_id) is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"there is already a job {job_id!r}: rename or delete it first",
            )
        store.put(job_id, job)
        return ImportedJob(job_id=job_id, kind="working-copy")
    try:
        revision = load(body.text)
    except JobFileError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    _checked(revision.job_id)
    try:
        added = store.add_revision(revision)
    except RevisionConflict as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
    return ImportedJob(
        job_id=revision.job_id,
        kind="revision",
        number=revision.revision.number,
        already_kept=not added,
    )
