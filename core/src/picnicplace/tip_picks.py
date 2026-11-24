"""How each nozzle tip has done picking each part, on this machine: the parts it has
picked, and the times it came up empty. A tip that has never picked a part and has come up
empty on it twice is taken to be wrong for it, as the N08 was for 0603 capacitors on a real
LumenPnP (2026-11-20: it sealed on the paper tape beside them and lifted nothing), while it
picked 0603 resistors, thinner and lighter, every time. Kept across restarts in the data
directory; with no path (tests), in memory."""

import threading
from pathlib import Path

from pydantic import BaseModel, Field

from picnicplace.storage import write_atomic

NEVER_PICKED_EMPTIES = 2
"""Empty picks, with not one part picked, that make a tip look wrong for a part."""


class TipRecord(BaseModel):
    picked: int = Field(default=0, description="Parts the tip picked.")
    empty: int = Field(default=0, description="Picks with the tip that came up empty.")


class _Record(BaseModel):
    format: str = "picnicplace-tip-picks"
    version: int = 1
    parts: dict[str, dict[str, TipRecord]] = Field(
        default_factory=dict[str, dict[str, TipRecord]],
        description="Part ID to tip ID to how that tip has done picking it.",
    )


class TipPicks:
    """Picks and empty picks by part and nozzle tip."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._memory = _Record()

    def _get(self) -> _Record:
        if self._path is None:
            return self._memory
        if not self._path.is_file():
            return _Record()
        try:
            return _Record.model_validate_json(self._path.read_text())
        except ValueError:
            return _Record()

    def _put(self, record: _Record) -> None:
        if self._path is None:
            self._memory = record
        else:
            write_atomic(self._path, record.model_dump_json(indent=2))

    def record(self, part_id: str, tip_id: str, picked: bool) -> None:
        """Count a pick of ``part_id`` with ``tip_id``: a part taken, or an empty pick."""
        with self._lock:
            record = self._get()
            tips = record.parts.setdefault(part_id, {})
            tip = tips.setdefault(tip_id, TipRecord())
            if picked:
                tip.picked += 1
            else:
                tip.empty += 1
            self._put(record)

    def get(self, part_id: str, tip_id: str) -> TipRecord:
        return self._get().parts.get(part_id, {}).get(tip_id, TipRecord())

    def never_picked(self, part_id: str, tip_id: str) -> bool:
        """The tip has come up empty on the part ``NEVER_PICKED_EMPTIES`` times and never
        picked one: in a run, its placements go to another nozzle."""
        tip = self.get(part_id, tip_id)
        return tip.picked == 0 and tip.empty >= NEVER_PICKED_EMPTIES

    def barred(self, part_id: str, tip_id: str) -> bool:
        """The tip never picked the part, and another tip has: plans leave it out. Empty
        picks with a tip that has picked the part before are the feeder's, not the tip's."""
        if not self.never_picked(part_id, tip_id):
            return False
        others = self._get().parts.get(part_id, {})
        return any(t != tip_id and r.picked > 0 for t, r in others.items())
