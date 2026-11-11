"""Saved jobs in the data directory's ``jobs`` folder.

Each job's working copy (the one Prepare edits) is ``<id>.json``. Its published
revisions (spec §5.4) are ``<id>/r0001.json``, ``r0002.json``, …, each written once and
never changed.
"""

import builtins
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field

from picnicplace.job.models import Job
from picnicplace.job.revision import JobFileError, JobRevision, dump, load
from picnicplace.schemas import WireModel
from picnicplace.storage import write_atomic

JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class RevisionSummary(WireModel):
    number: int
    hash: str
    author: str
    created: datetime
    reason: str


class JobSummary(WireModel):
    id: str
    name: str
    board: str
    copies: int
    side_up: str
    placed: bool
    modified: datetime
    draft: bool = Field(description="Whether there is a working copy to edit in Prepare.")
    revisions: list[RevisionSummary] = Field(
        default_factory=list[RevisionSummary], description="Published revisions, oldest first."
    )


class RevisionConflict(Exception):
    """A different revision with the same number is already kept."""


def job_id_for(name: str) -> str:
    """A file-safe ID from a job name."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")[:100]
    return slug or "job"


class JobStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, job_id: str) -> Path:
        if not JOB_ID.match(job_id):
            raise ValueError(f"invalid job ID {job_id!r}: use letters, digits, '.', '_', '-'")
        return self.directory / f"{job_id}.json"

    def list(self) -> list[JobSummary]:
        if not self.directory.is_dir():
            return []
        result: dict[str, JobSummary] = {}
        for path in sorted(self.directory.glob("*.json")):
            try:
                job = Job.model_validate_json(path.read_text())
            except ValueError:
                continue
            result[path.stem] = JobSummary(
                id=path.stem,
                name=job.name,
                board=job.board.name,
                copies=len(job.panel.copies),
                side_up=job.side_up,
                placed=job.location is not None,
                modified=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                draft=True,
            )
        for folder in sorted(p for p in self.directory.iterdir() if p.is_dir()):
            if not JOB_ID.match(folder.name):
                continue
            revisions = self.revisions(folder.name)
            if not revisions:
                continue
            summary = result.get(folder.name)
            if summary is None:
                latest = self.revision(folder.name, revisions[-1].number)
                if latest is None:
                    continue
                design = latest.design
                summary = result[folder.name] = JobSummary(
                    id=folder.name,
                    name=design.name,
                    board=design.board.name,
                    copies=len(design.panel.copies),
                    side_up=design.side_up,
                    placed=False,
                    modified=latest.revision.created,
                    draft=False,
                )
            summary.revisions = revisions
        return sorted(result.values(), key=lambda j: j.id)

    def get(self, job_id: str) -> Job | None:
        path = self._path(job_id)
        if not path.is_file():
            return None
        return Job.model_validate_json(path.read_text())

    def put(self, job_id: str, job: Job) -> None:
        path = self._path(job_id)
        write_atomic(path, job.model_dump_json(indent=2))

    def delete(self, job_id: str) -> bool:
        """Delete a job's working copy. Its published revisions are kept."""
        path = self._path(job_id)
        if not path.is_file():
            return False
        path.unlink()
        return True

    # --- Revisions ------------------------------------------------------------------------

    def _revision_path(self, job_id: str, number: int) -> Path:
        return self._path(job_id).with_suffix("") / f"r{number:04d}.json"

    def revisions(self, job_id: str) -> builtins.list[RevisionSummary]:
        """A job's published revisions, oldest first; files that cannot be read are left out."""
        folder = self._path(job_id).with_suffix("")
        if not folder.is_dir():
            return []
        result: builtins.list[RevisionSummary] = []
        for path in sorted(folder.glob("r[0-9][0-9][0-9][0-9].json")):
            try:
                info = load(path.read_text()).revision
            except JobFileError:
                continue
            result.append(RevisionSummary(**info.model_dump()))
        return result

    def revision(self, job_id: str, number: int) -> JobRevision | None:
        path = self._revision_path(job_id, number)
        return load(path.read_text()) if path.is_file() else None

    def revision_text(self, job_id: str, number: int) -> str | None:
        """A revision's file exactly as kept, for download."""
        path = self._revision_path(job_id, number)
        return path.read_text() if path.is_file() else None

    def next_number(self, job_id: str) -> int:
        revisions = self.revisions(job_id)
        return revisions[-1].number + 1 if revisions else 1

    def add_revision(self, revision: JobRevision) -> bool:
        """Keep a revision. Returns False if this very revision is already kept; raises
        RevisionConflict if a different one has its number."""
        path = self._revision_path(revision.job_id, revision.revision.number)
        if path.exists():
            kept = load(path.read_text())
            if kept.revision.hash == revision.revision.hash:
                return False
            raise RevisionConflict(
                f"job {revision.job_id!r} already has a different revision "
                f"{revision.revision.number}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(dump(revision))
            # A hard link fails if the name exists, so two writers cannot both win.
            os.link(tmp, path)
        except FileExistsError as e:
            raise RevisionConflict(
                f"job {revision.job_id!r} already has a revision {revision.revision.number}"
            ) from e
        finally:
            Path(tmp).unlink(missing_ok=True)
        return True
