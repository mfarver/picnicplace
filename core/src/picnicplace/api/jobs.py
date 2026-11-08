"""Job endpoints: saved jobs, and where a job's placements land on the machine."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from picnicplace.api.context import Context, require
from picnicplace.auth import Permission
from picnicplace.job.layout import JobLayout, find_location, layout
from picnicplace.job.models import Job, MachineLocation
from picnicplace.job.store import JOB_ID, JobStore, JobSummary
from picnicplace.schemas import ErrorResponse

router = APIRouter(
    prefix="/api/v1/jobs", tags=["jobs"], dependencies=[Depends(require(Permission.VIEW))]
)
CanEdit = Depends(require(Permission.JOB))

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No job with this ID."}
}


def get_store(ctx: Context) -> JobStore:
    return JobStore(ctx.config.data.jobs_dir)


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
    job = store.get(_checked(job_id))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no job {job_id!r}")
    return job


@router.put("/{job_id}", dependencies=[CanEdit])
def put_job(store: Store, job_id: str, job: Job) -> Job:
    """Save a job, creating or replacing it."""
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
