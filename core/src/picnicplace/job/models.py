"""Jobs: a board (or a panel of boards) on the machine, ready to assemble.

A job is the board, the panel layout, and which side faces up (its design), plus where
it sits on this machine and the board's measured height (its setup). The design is what
is published as a revision (``revision.py``); the setup belongs to the machine and is
kept by it per job (``JobSetup``). Placing a job means taking each placement from the
board frame to the machine:

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


class JobSetup(BoardModel):
    """What one machine keeps about a job: where its panel sits on the bed and the
    board's measured height. Never part of a revision, which any machine can run."""

    location: MachineLocation | None = Field(
        default=None, description="Where the panel sits on the machine, once set."
    )
    board_z: float | None = Field(
        default=None, description="Machine Z of the board's top surface, once measured."
    )


class Job(BoardModel):
    format: Literal["picnicplace-job"] = "picnicplace-job"
    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=200)
    board: Board
    panel: Panel
    side_up: Side = Field(description="The board side facing up on the machine.")
    location: MachineLocation | None = Field(
        default=None,
        description="Where the panel sits on this machine, once set: from its setup.",
    )
    board_z: float | None = Field(
        default=None,
        description="Machine Z of the board's top surface, once measured: from its setup.",
    )

    def setup(self) -> "JobSetup":
        return JobSetup(location=self.location, board_z=self.board_z)

    def with_setup(self, setup: "JobSetup") -> "Job":
        return self.model_copy(update={"location": setup.location, "board_z": setup.board_z})

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
