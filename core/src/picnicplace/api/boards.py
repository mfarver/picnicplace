"""Board import endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from picnicplace.api.context import Context, get_principal, require
from picnicplace.api.library import get_library
from picnicplace.auth import Permission, Principal
from picnicplace.board.kicad import read_bom, read_kicad_pcb
from picnicplace.board.models import Board
from picnicplace.board.panel import Panel, find_copies
from picnicplace.board.sexpr import SexprError
from picnicplace.job.models import Job
from picnicplace.library import ImportReport, LibraryDocument
from picnicplace.schemas import ErrorResponse

router = APIRouter(
    prefix="/api/v1/boards", tags=["boards"], dependencies=[Depends(require(Permission.JOB))]
)

BAD_FILE: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "A file could not be read."}
}


class KicadImportRequest(BaseModel):
    kicad_pcb: str = Field(description="Contents of the .kicad_pcb file.")
    name: str = Field(default="board", description="Name for the board.")
    bom_csv: str | None = Field(
        default=None,
        description=(
            "Optional BOM CSV from the schematic, to take part numbers from instead of "
            "the board's fields. Grouped or one row per reference."
        ),
    )
    panel_pcb: str | None = Field(
        default=None,
        description=(
            "Optional panel .kicad_pcb made from this board (such as by KiKit). Its board "
            "copies are found by matching references; its own fiducials are kept."
        ),
    )
    part_field: str | None = Field(
        default=None,
        description="Field holding the part number. Default: the first of MP, MPN, ... found.",
    )
    add_to_library: bool = Field(
        default=False,
        description=(
            "Merge the board's footprints and parts into the library (existing entries "
            "keep their values). Needs the config permission."
        ),
    )


class KicadImportResult(BaseModel):
    board: Board
    panel: Panel = Field(description="The panel layout, or a single copy of the board.")
    job: Job = Field(description="A new job for the board or panel, not yet saved or placed.")
    library: LibraryDocument = Field(
        description="Footprints read from the board, and a part for each part number."
    )
    library_report: ImportReport | None = Field(
        description="What merging into the library did, when add_to_library was set."
    )
    warnings: list[str]


@router.post("/import/kicad", responses=BAD_FILE)
def import_kicad(
    ctx: Context,
    body: KicadImportRequest,
    principal: Annotated[Principal, Depends(get_principal)],
) -> KicadImportResult:
    """Read a KiCad board: its placements, and the footprints and parts it uses."""
    try:
        bom = read_bom(body.bom_csv, body.part_field) if body.bom_csv else None
        result = read_kicad_pcb(body.kicad_pcb, body.name, bom=bom, part_field=body.part_field)
        warnings = list(result.warnings)
        panel = Panel.single(result.board)
        if body.panel_pcb:
            panel_board = read_kicad_pcb(body.panel_pcb, f"{body.name} panel").board
            panel, panel_warnings = find_copies(result.board, panel_board)
            warnings += panel_warnings
    except (SexprError, ValueError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    document = LibraryDocument(footprints=result.footprints, parts=result.parts)
    report: ImportReport | None = None
    if body.add_to_library:
        if Permission.CONFIG not in principal.permissions:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="adding to the library needs the config permission",
            )
        library = get_library(ctx)
        report = library.import_document(document)
    return KicadImportResult(
        board=result.board,
        panel=panel,
        job=Job.for_board(result.board, panel, body.name),
        library=document,
        library_report=report,
        warnings=warnings,
    )
