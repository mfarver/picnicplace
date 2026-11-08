"""Jobs: a board (or a panel of boards) on the machine, ready to assemble.

A job holds everything it needs in one file: the board, the panel layout, which side
faces up, and where it sits on the machine. Placing it means taking each placement
from the board frame to the machine:

    board frame -> panel frame (the copy's offset and rotation)
                -> loaded frame (flipped left to right if bottom side up)
                -> operator's view (turned by the job's rotation on the bed)
                -> machine coordinates (the profile's axis directions, plus the location)

The loaded frame and the operator's view have +X to the operator's right and +Y toward
the back of the machine. ``location`` is where the loaded panel's front-left corner is,
in machine coordinates.
"""

from typing import Literal

from pydantic import Field

from picnicplace.board.models import Board, BoardModel, Side
from picnicplace.board.panel import Panel


class MachineLocation(BoardModel):
    x: float = Field(description="Machine X of the panel's front-left corner, mm.")
    y: float = Field(description="Machine Y of the panel's front-left corner, mm.")
    rotation: float = Field(
        default=0.0,
        description="Degrees counterclockwise, as the operator sees the bed from the front.",
    )


class Job(BoardModel):
    format: Literal["picnicplace-job"] = "picnicplace-job"
    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=200)
    board: Board
    panel: Panel
    side_up: Side = Field(description="The board side facing up on the machine.")
    location: MachineLocation | None = Field(
        default=None, description="Where the panel sits on the machine, once set."
    )
    board_z: float | None = Field(
        default=None, description="Machine Z of the board's top surface, once measured."
    )

    @classmethod
    def for_board(cls, board: Board, panel: Panel | None = None, name: str | None = None) -> "Job":
        """A new job, with the side up chosen from where the parts are (the side with
        enabled parts; top if both have them)."""
        sides = board.sides()
        return cls(
            name=name or board.name,
            board=board,
            panel=panel or Panel.single(board),
            side_up="bottom" if sides == {"bottom"} else "top",
        )
