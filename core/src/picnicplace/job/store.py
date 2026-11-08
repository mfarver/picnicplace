"""Job files: one JSON file per job in the data directory's ``jobs`` folder."""

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from picnicplace.job.models import Job

JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class JobSummary(BaseModel):
    id: str
    name: str
    board: str
    copies: int
    side_up: str
    placed: bool
    modified: datetime


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
        result: list[JobSummary] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                job = Job.model_validate_json(path.read_text())
            except ValueError:
                continue
            result.append(
                JobSummary(
                    id=path.stem,
                    name=job.name,
                    board=job.board.name,
                    copies=len(job.panel.copies),
                    side_up=job.side_up,
                    placed=job.location is not None,
                    modified=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                )
            )
        return result

    def get(self, job_id: str) -> Job | None:
        path = self._path(job_id)
        if not path.is_file():
            return None
        return Job.model_validate_json(path.read_text())

    def put(self, job_id: str, job: Job) -> None:
        path = self._path(job_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file and rename, so a crash never leaves half a job.
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f".{job_id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(job.model_dump_json(indent=2))
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def delete(self, job_id: str) -> bool:
        path = self._path(job_id)
        if not path.is_file():
            return False
        path.unlink()
        return True
