"""Parts library endpoints: footprints, tapes, parts, import and export."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from picnicplace.api.context import Context, require
from picnicplace.auth import Permission
from picnicplace.library import (
    Footprint,
    ImportReport,
    Library,
    LibraryDocument,
    LibraryError,
    NotFound,
    Part,
    Tape,
)
from picnicplace.library.openpnp import OpenPnpFormatError, read_library
from picnicplace.schemas import ErrorResponse

router = APIRouter(
    prefix="/api/v1/library", tags=["library"], dependencies=[Depends(require(Permission.VIEW))]
)
CanEdit = Depends(require(Permission.CONFIG))

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No entry with this ID."}
}
CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorResponse, "description": "The change would break the library."}
}


def get_library(ctx: Context) -> Library:
    if ctx.library is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="library is not open")
    return ctx.library


Lib = Annotated[Library, Depends(get_library)]


def _http(e: LibraryError) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if isinstance(e, NotFound) else status.HTTP_409_CONFLICT
    return HTTPException(code, detail=str(e))


def _same_id(path_id: str, body_id: str) -> None:
    if path_id != body_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"the body's id {body_id!r} does not match the URL's {path_id!r}",
        )


# --- Footprints ---------------------------------------------------------------------------


@router.get("/footprints")
def list_footprints(library: Lib) -> list[Footprint]:
    return library.footprints()


@router.get("/footprints/{footprint_id:path}", responses=NOT_FOUND)
def get_footprint(library: Lib, footprint_id: str) -> Footprint:
    try:
        return library.footprint(footprint_id)
    except LibraryError as e:
        raise _http(e) from e


@router.put("/footprints/{footprint_id:path}", dependencies=[CanEdit])
def put_footprint(library: Lib, footprint_id: str, footprint: Footprint) -> Footprint:
    """Create or replace a footprint."""
    _same_id(footprint_id, footprint.id)
    library.put_footprint(footprint)
    return footprint


@router.delete(
    "/footprints/{footprint_id:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**NOT_FOUND, **CONFLICT},
    dependencies=[CanEdit],
)
def delete_footprint(library: Lib, footprint_id: str) -> None:
    """Delete a footprint. Refused while any part uses it."""
    try:
        library.delete_footprint(footprint_id)
    except LibraryError as e:
        raise _http(e) from e


# --- Tapes --------------------------------------------------------------------------------


@router.get("/tapes")
def list_tapes(library: Lib) -> list[Tape]:
    return library.tapes()


@router.get("/tapes/{tape_id:path}", responses=NOT_FOUND)
def get_tape(library: Lib, tape_id: str) -> Tape:
    try:
        return library.tape(tape_id)
    except LibraryError as e:
        raise _http(e) from e


@router.put("/tapes/{tape_id:path}", dependencies=[CanEdit])
def put_tape(library: Lib, tape_id: str, tape: Tape) -> Tape:
    """Create or replace a tape. Every part that comes in it changes with it."""
    _same_id(tape_id, tape.id)
    library.put_tape(tape)
    return tape


@router.delete(
    "/tapes/{tape_id:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**NOT_FOUND, **CONFLICT},
    dependencies=[CanEdit],
)
def delete_tape(library: Lib, tape_id: str) -> None:
    """Delete a tape. Refused while any part comes in it."""
    try:
        library.delete_tape(tape_id)
    except LibraryError as e:
        raise _http(e) from e


# --- Parts --------------------------------------------------------------------------------


@router.get("/parts")
def list_parts(library: Lib) -> list[Part]:
    return library.parts()


@router.get("/parts/{part_id:path}", responses=NOT_FOUND)
def get_part(library: Lib, part_id: str) -> Part:
    try:
        return library.part(part_id)
    except LibraryError as e:
        raise _http(e) from e


@router.put("/parts/{part_id:path}", responses=CONFLICT, dependencies=[CanEdit])
def put_part(library: Lib, part_id: str, part: Part) -> Part:
    """Create or replace a part. Its footprint, and its tape if it names one, must
    already be in the library."""
    _same_id(part_id, part.id)
    try:
        library.put_part(part)
    except LibraryError as e:
        raise _http(e) from e
    return part


@router.delete(
    "/parts/{part_id:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND,
    dependencies=[CanEdit],
)
def delete_part(library: Lib, part_id: str) -> None:
    try:
        library.delete_part(part_id)
    except LibraryError as e:
        raise _http(e) from e


# --- Import and export --------------------------------------------------------------------


@router.get("/export")
def export_library(library: Lib) -> LibraryDocument:
    """The whole library as one JSON document, for backup or sharing."""
    return library.export()


class ImportRequest(BaseModel):
    document: LibraryDocument
    overwrite: bool = Field(
        default=False,
        description=(
            "Replace entries that already exist. By default existing entries keep their "
            "values: only empty values are filled in, and differences are reported."
        ),
    )


@router.post("/import", dependencies=[CanEdit])
def import_library(library: Lib, body: ImportRequest) -> ImportReport:
    """Merge a library document (as exported) into the library."""
    return library.import_document(body.document, overwrite=body.overwrite)


class OpenPnpImportRequest(BaseModel):
    packages_xml: str | None = Field(default=None, description="Contents of packages.xml.")
    parts_xml: str | None = Field(default=None, description="Contents of parts.xml.")
    overwrite: bool = False


@router.post(
    "/import/openpnp",
    responses={400: {"model": ErrorResponse, "description": "A file could not be read."}},
    dependencies=[CanEdit],
)
def import_openpnp(library: Lib, body: OpenPnpImportRequest) -> ImportReport:
    """Merge OpenPnP packages (as footprints) and parts into the library."""
    try:
        document = read_library(body.packages_xml, body.parts_xml)
    except (OpenPnpFormatError, ValueError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return library.import_document(document, overwrite=body.overwrite)
