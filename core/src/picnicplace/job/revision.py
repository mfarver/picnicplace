"""Job revisions: a job as published to machines, which never changes afterward (spec §5.4).

A revision holds the job's design (the board, the panel, which side faces up) and a
copy of every part, footprint, and tape it uses, so it runs the same whatever happens
to the library later. It holds nothing that belongs to one machine: where the panel
sits on the bed and the board's measured height are kept by each machine.

Its file is one JSON document, written in a fixed order (library entries sorted by ID),
so two revisions compare cleanly. The revision's hash is the SHA-256 of its design in
a canonical form (keys sorted, no spaces), so a file changed by hand after it was saved
is caught when it is read.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from pydantic import Field, ValidationError

from picnicplace.board.models import Board, BoardModel, Placement, Side
from picnicplace.board.panel import Panel
from picnicplace.job.models import Job, JobSetup
from picnicplace.library import Footprint, Library, Part, Tape

HASH = re.compile(r"^[0-9a-f]{64}$")


class PartsSource(Protocol):
    """Where a run finds its parts, footprints, and tapes: a revision's copy of them, or
    (for a plan of a job not yet published) the library."""

    def find_part(self, part_id: str) -> Part | None: ...

    def find_footprint(self, footprint_id: str) -> Footprint | None: ...

    def tape_of(self, part: Part | None) -> Tape | None: ...


class LibrarySnapshot(BoardModel):
    """The library entries a revision uses, each sorted by ID."""

    footprints: list[Footprint] = Field(default_factory=list[Footprint])
    parts: list[Part] = Field(default_factory=list[Part])
    tapes: list[Tape] = Field(default_factory=list[Tape])

    def find_part(self, part_id: str) -> Part | None:
        return next((p for p in self.parts if p.id == part_id), None)

    def find_footprint(self, footprint_id: str) -> Footprint | None:
        return next((f for f in self.footprints if f.id == footprint_id), None)

    def tape_of(self, part: Part | None) -> Tape | None:
        if part is None or part.tape_id is None:
            return None
        return next((t for t in self.tapes if t.id == part.tape_id), None)


class JobDesign(BoardModel):
    """What to build: everything in a revision that its hash covers."""

    name: str = Field(min_length=1, max_length=200)
    board: Board
    panel: Panel
    side_up: Side = Field(description="The board side facing up on the machine.")
    library: LibrarySnapshot = Field(
        description="A copy of every part, footprint, and tape the job uses."
    )
    missing: list[str] = Field(
        default_factory=list[str],
        description=(
            "Part and footprint IDs the job uses that the library did not have when the "
            "revision was made, sorted."
        ),
    )

    def job(self, setup: JobSetup | None = None) -> Job:
        """The design as a job, set up as one machine keeps it."""
        job = Job(name=self.name, board=self.board, panel=self.panel, side_up=self.side_up)
        return job.with_setup(setup) if setup else job


class RevisionInfo(BoardModel):
    number: int = Field(ge=1, description="1 for a job's first revision, then 2, 3, …")
    hash: str = Field(pattern=HASH.pattern, description="SHA-256 of the design, hex.")
    author: str = Field(max_length=200, description="Who made the revision.")
    created: datetime
    reason: str = Field(default="", max_length=2000, description="What changed, and why.")


class JobRevision(BoardModel):
    format: Literal["picnicplace-job"] = "picnicplace-job"
    version: Literal[2] = 2
    job_id: str
    revision: RevisionInfo
    design: JobDesign


class JobFileError(ValueError):
    """A job file that cannot be read; the message says why, for the user."""


def design_hash(design: JobDesign) -> str:
    canonical = json.dumps(
        design.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _uses(placements: list[Placement]) -> tuple[set[str], set[str]]:
    parts = {p.part_id for p in placements if p.part_id}
    footprints = {p.footprint_id for p in placements}
    return parts, footprints


def snapshot(job: Job, library: Library | None) -> tuple[LibrarySnapshot, list[str]]:
    """Copies of the library entries a job uses, and the IDs the library lacks."""
    part_ids, footprint_ids = _uses(job.board.placements + job.panel.fiducials)
    missing: set[str] = set()
    parts: list[Part] = []
    for part_id in sorted(part_ids):
        part = library.find_part(part_id) if library else None
        if part is None:
            missing.add(part_id)
            continue
        parts.append(part)
        footprint_ids.add(part.footprint_id)
    footprints: list[Footprint] = []
    for footprint_id in sorted(footprint_ids):
        footprint = library.find_footprint(footprint_id) if library else None
        if footprint is None:
            missing.add(footprint_id)
        else:
            footprints.append(footprint)
    tapes = {t.id: t for p in parts if library and (t := library.tape_of(p)) is not None}
    return (
        LibrarySnapshot(
            footprints=footprints, parts=parts, tapes=[tapes[k] for k in sorted(tapes)]
        ),
        sorted(missing),
    )


def make_revision(
    job_id: str,
    job: Job,
    library: Library | None,
    *,
    number: int,
    author: str,
    reason: str,
    now: datetime | None = None,
) -> JobRevision:
    """A new revision of a job, with copies of what it uses from the library."""
    library_copy, missing = snapshot(job, library)
    design = JobDesign(
        name=job.name,
        board=job.board,
        panel=job.panel,
        side_up=job.side_up,
        library=library_copy,
        missing=missing,
    )
    return JobRevision(
        job_id=job_id,
        revision=RevisionInfo(
            number=number,
            hash=design_hash(design),
            author=author.strip(),
            created=(now or datetime.now(UTC)).replace(microsecond=0),
            reason=reason.strip(),
        ),
        design=design,
    )


def dump(revision: JobRevision) -> str:
    """A revision's file: its fields in a fixed order, two-space indents, a final newline."""
    return revision.model_dump_json(indent=2) + "\n"


def load(text: str) -> JobRevision:
    """Read a revision's file and check that it has not been changed since it was made."""
    try:
        data = cast(object, json.loads(text))
    except json.JSONDecodeError as e:
        raise JobFileError(f"not a job file: it is not JSON ({e.msg}, line {e.lineno})") from e
    fields = cast(dict[str, object], data) if isinstance(data, dict) else {}
    if fields.get("format") != "picnicplace-job":
        raise JobFileError("not a PicnicPlace job file")
    version = fields.get("version")
    if version == 1:
        raise JobFileError(
            "this is a job saved before revisions: open it in Prepare and publish it"
        )
    if version != 2:
        raise JobFileError(
            f"this job file is version {version}, which this PicnicPlace cannot read: "
            "update PicnicPlace"
        )
    try:
        revision = JobRevision.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise JobFileError(f"the job file is not valid: {where}: {first['msg']}") from e
    if design_hash(revision.design) != revision.revision.hash:
        raise JobFileError(
            "the job file was changed after it was saved (its contents do not match its "
            "hash): ask for a fresh copy, or publish a new revision"
        )
    return revision
