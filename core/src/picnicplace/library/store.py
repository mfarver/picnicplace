"""The parts library on disk: one SQLite file (spec 7.6).

Each footprint and part is stored as its JSON document, keyed by ID, so the models can
grow without table migrations; ``schema_version`` is there for the day one is needed.

Imports merge rather than overwrite by default. A new ID is added; for an ID the library
already has, values the library lacks are filled in, and values that differ are reported
as conflicts and left alone. That makes re-importing a board's library after a design
change safe: nothing tuned on the machine is lost.
"""

import json
import math
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field

from picnicplace.library.models import Footprint, LibraryModel, Part
from picnicplace.schemas import WireModel

SCHEMA_VERSION = 1

Kind = Literal["footprint", "part"]


class LibraryError(Exception):
    """A library change that would leave it inconsistent."""


class NotFound(LibraryError):
    pass


class LibraryDocument(LibraryModel):
    """The whole library, or part of it, as one document: the text export format."""

    format: Literal["picnicplace-library"] = "picnicplace-library"
    version: Literal[1] = 1
    footprints: list[Footprint] = Field(default_factory=list[Footprint])
    parts: list[Part] = Field(default_factory=list[Part])


class Filled(WireModel):
    kind: Kind
    id: str
    fields: list[str] = Field(description="Fields that were empty and now have a value.")


class Conflict(WireModel):
    kind: Kind
    id: str
    field: str
    current: Any = Field(description="The library's value, which was kept.")
    incoming: Any = Field(description="The imported value, which was not used.")


class ImportReport(WireModel):
    added: list[str] = Field(
        default_factory=list[str], description="'footprint:ID' or 'part:ID' for new entries."
    )
    replaced: list[str] = Field(
        default_factory=list[str], description="Entries overwritten (overwrite mode only)."
    )
    filled: list[Filled] = Field(default_factory=list[Filled])
    conflicts: list[Conflict] = Field(default_factory=list[Conflict])
    unchanged: int = 0
    errors: list[str] = Field(default_factory=list[str])


# Fields whose meaning depends on another: when the main value conflicts, its basis and
# source are not filled in from the import either.
_COMPANIONS = {
    "height": ("height_basis", "height_source"),
    "body": ("body_basis",),
}


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == "unknown"


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float | int) and isinstance(b, float | int):
        return math.isclose(a, b, abs_tol=1e-6)
    if isinstance(a, dict) and isinstance(b, dict):
        da: dict[str, Any] = a  # pyright: ignore[reportUnknownVariableType]
        db: dict[str, Any] = b  # pyright: ignore[reportUnknownVariableType]
        return da.keys() == db.keys() and all(_same(da[k], db[k]) for k in da)
    if isinstance(a, list) and isinstance(b, list):
        la: list[Any] = a  # pyright: ignore[reportUnknownVariableType]
        lb: list[Any] = b  # pyright: ignore[reportUnknownVariableType]
        return len(la) == len(lb) and all(_same(x, y) for x, y in zip(la, lb, strict=True))
    return a == b


def merge(
    current: dict[str, Any], incoming: dict[str, Any], prefix: str = ""
) -> tuple[dict[str, Any], list[str], list[tuple[str, Any, Any]]]:
    """Fill ``current``'s empty values from ``incoming``. Returns the merged document,
    the fields filled, and (field, current, incoming) for each value that differs."""
    merged = dict(current)
    filled: list[str] = []
    conflicts: list[tuple[str, Any, Any]] = []
    skip: set[str] = set()
    for key, new in incoming.items():
        if key in skip or _empty(new):
            continue
        old: Any = current.get(key)
        if _empty(old):
            merged[key] = new
            filled.append(prefix + key)
        elif type(old) is dict and type(new) is dict:
            sub, sub_filled, sub_conflicts = merge(
                cast(dict[str, Any], old), cast(dict[str, Any], new), f"{prefix}{key}."
            )
            merged[key] = sub
            filled += sub_filled
            conflicts += sub_conflicts
        elif not _same(old, new):
            conflicts.append((prefix + key, cast(Any, old), new))
            skip.update(_COMPANIONS.get(key, ()))
    return merged, filled, conflicts


class Library:
    """Thread-safe access to the library file. ``path=None`` keeps it in memory."""

    def __init__(self, path: Path | None) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            ":memory:" if path is None else path, check_same_thread=False, isolation_level=None
        )
        self._db.execute("PRAGMA foreign_keys = ON")
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS footprints (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS parts (id TEXT PRIMARY KEY, data TEXT NOT NULL,"
                " footprint_id TEXT NOT NULL REFERENCES footprints (id))"
            )
            row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row[0]) > SCHEMA_VERSION:
                raise LibraryError(f"{path} was written by a newer PicnicPlace (schema {row[0]})")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # --- Reading ---------------------------------------------------------------------

    def footprints(self) -> list[Footprint]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM footprints ORDER BY id").fetchall()
        return [Footprint.model_validate_json(r[0]) for r in rows]

    def parts(self) -> list[Part]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM parts ORDER BY id").fetchall()
        return [Part.model_validate_json(r[0]) for r in rows]

    def footprint(self, footprint_id: str) -> Footprint:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM footprints WHERE id = ?", (footprint_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"no footprint {footprint_id!r}")
        return Footprint.model_validate_json(row[0])

    def part(self, part_id: str) -> Part:
        with self._lock:
            row = self._db.execute("SELECT data FROM parts WHERE id = ?", (part_id,)).fetchone()
        if row is None:
            raise NotFound(f"no part {part_id!r}")
        return Part.model_validate_json(row[0])

    def find_part(self, part_id: str) -> Part | None:
        """A part, or None if the library has none with this ID."""
        try:
            return self.part(part_id)
        except NotFound:
            return None

    def find_footprint(self, footprint_id: str) -> Footprint | None:
        try:
            return self.footprint(footprint_id)
        except NotFound:
            return None

    def export(self) -> LibraryDocument:
        return LibraryDocument(footprints=self.footprints(), parts=self.parts())

    # --- Writing ---------------------------------------------------------------------

    def put_footprint(self, footprint: Footprint) -> None:
        with self._transaction() as db:
            _write_footprint(db, footprint)

    def put_part(self, part: Part) -> None:
        with self._transaction() as db:
            _check_footprint(db, part)
            _write_part(db, part)

    def delete_footprint(self, footprint_id: str) -> None:
        with self._transaction() as db:
            users = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM parts WHERE footprint_id = ? ORDER BY id", (footprint_id,)
                )
            ]
            if users:
                raise LibraryError(
                    f"footprint {footprint_id!r} is used by {len(users)} part(s): "
                    + ", ".join(users[:5])
                    + ("..." if len(users) > 5 else "")
                )
            if db.execute("DELETE FROM footprints WHERE id = ?", (footprint_id,)).rowcount == 0:
                raise NotFound(f"no footprint {footprint_id!r}")

    def delete_part(self, part_id: str) -> None:
        with self._transaction() as db:
            if db.execute("DELETE FROM parts WHERE id = ?", (part_id,)).rowcount == 0:
                raise NotFound(f"no part {part_id!r}")

    def import_document(self, document: LibraryDocument, overwrite: bool = False) -> ImportReport:
        """Add a document's footprints and parts, merging or overwriting existing ones.
        Parts whose footprint is in neither the library nor the document are skipped
        and reported as errors. Everything else is applied in one transaction."""
        report = ImportReport()
        with self._transaction() as db:
            for footprint in document.footprints:
                _import_one(db, "footprint", footprint, overwrite, report)
            for part in document.parts:
                try:
                    _check_footprint(db, part)
                except LibraryError as e:
                    report.errors.append(str(e))
                    continue
                _import_one(db, "part", part, overwrite, report)
        return report


def _check_footprint(db: sqlite3.Connection, part: Part) -> None:
    if db.execute("SELECT 1 FROM footprints WHERE id = ?", (part.footprint_id,)).fetchone():
        return
    raise LibraryError(f"part {part.id!r}: no footprint {part.footprint_id!r} in the library")


def _write_footprint(db: sqlite3.Connection, footprint: Footprint) -> None:
    db.execute(
        "INSERT INTO footprints (id, data) VALUES (?, ?)"
        " ON CONFLICT (id) DO UPDATE SET data = excluded.data",
        (footprint.id, footprint.model_dump_json()),
    )


def _write_part(db: sqlite3.Connection, part: Part) -> None:
    db.execute(
        "INSERT INTO parts (id, footprint_id, data) VALUES (?, ?, ?)"
        " ON CONFLICT (id) DO UPDATE SET"
        " footprint_id = excluded.footprint_id, data = excluded.data",
        (part.id, part.footprint_id, part.model_dump_json()),
    )


def _import_one(
    db: sqlite3.Connection,
    kind: Kind,
    item: Footprint | Part,
    overwrite: bool,
    report: ImportReport,
) -> None:
    table = "footprints" if kind == "footprint" else "parts"
    row = db.execute(f"SELECT data FROM {table} WHERE id = ?", (item.id,)).fetchone()
    label = f"{kind}:{item.id}"
    if row is None or overwrite:
        if row is not None:
            if _same(json.loads(row[0]), item.model_dump(mode="json")):
                report.unchanged += 1
                return
            report.replaced.append(label)
        else:
            report.added.append(label)
        _write(db, item)
        return
    current: dict[str, Any] = json.loads(row[0])
    merged, filled, conflicts = merge(current, item.model_dump(mode="json"))
    for field, old, new in conflicts:
        report.conflicts.append(
            Conflict(kind=kind, id=item.id, field=field, current=old, incoming=new)
        )
    if filled:
        report.filled.append(Filled(kind=kind, id=item.id, fields=filled))
        model = Footprint if kind == "footprint" else Part
        _write(db, model.model_validate(merged))
    elif not conflicts:
        report.unchanged += 1


def _write(db: sqlite3.Connection, item: Footprint | Part) -> None:
    if isinstance(item, Footprint):
        _write_footprint(db, item)
    else:
        _write_part(db, item)
