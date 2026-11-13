"""The setup and calibration wizard (spec §7.7, §8.2): every page from "which machine do
you have" to a calibrated machine, each with where it stands, and what the machine is
allowed to do until they are all done.

Most pages' states come from what the machine has measured (the calibration checklist)
and from the bring-up checklist. The rest are pages the owner finishes by confirming
them (the tips fitted, the discard and park locations) or may skip (strips and trays):
those are kept in ``wizard.json`` in the data directory, so leaving the wizard and
coming back, from any Studio, finds it where it was.

The limits follow the stage (``limit``). Until every page that applies is done, the
machine homes, jogs and runs the wizard's own steps at a quarter of full speed at most;
picks, placements and runs are refused, and teaching feeders waits for homing and the
top camera. Once calibrated, a page going out of date (after a crash, say) pauses runs
until it is redone; single picks and placements go ahead, with a warning naming it.
"""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from picnicplace.bringup import BringUpRecord
from picnicplace.calibration import Action, CalibrationChecklist, CalibrationLine, LineId
from picnicplace.calibration import current as current_checklist
from picnicplace.machine.profile import MachineProfile
from picnicplace.schemas import WireModel
from picnicplace.storage import write_atomic

if TYPE_CHECKING:
    from picnicplace.bringup import BringUpStore
    from picnicplace.feeders.service import FeederService
    from picnicplace.machine.service import MachineService

PageId = Literal[
    "welcome",
    "machine",
    "connect",
    "cameras",
    "first_motion",
    "bed",
    "visual_homing",
    "top_camera_scale",
    "nozzle_tips",
    "bottom_camera",
    "nozzle_offsets",
    "nozzle_runout",
    "discard_park",
    "feeders",
    "strips_trays",
    "done",
]

PageStatus = Literal["done", "todo", "stale", "skipped", "not_needed", "not_built"]
"""``done``: finished, and still good. ``todo``: still to do. ``stale``: finished, but out
of date (the reason says why): do it again. ``skipped``: an optional page the owner
skipped. ``not_needed``: nothing on this machine for it. ``not_built``: PicnicPlace cannot
do it yet."""

Stage = Literal["limited", "out_of_date", "calibrated"]
"""``limited``: a page that applies is still to do. ``out_of_date``: every page is done, but
some are out of date. ``calibrated``: every page is done and current."""

ConfirmedPage = Literal["nozzle_tips", "discard_park", "strips_trays"]
"""Pages the owner finishes by confirming them, rather than by measuring."""

OPTIONAL: frozenset[PageId] = frozenset({"strips_trays"})

LIMITED_SPEED = 0.25
"""Moves are capped at this fraction of full speed until the machine is calibrated."""

RUN_KINDS = frozenset({"job_run", "board_check"})
PICK_KINDS = frozenset({"pick", "align", "pick_and_place"})
TEACH_KINDS = frozenset(
    {"feeder_fiducial", "feeder_calibrate", "feeder_pocket", "rail_calibrate", "rail_fiducial"}
)
"""Tasks the limits apply to. Everything else (homing, the wizard's own measurements,
discarding or releasing a part already held) goes ahead at any stage."""

TITLES: dict[PageId, str] = {
    "welcome": "Welcome",
    "machine": "Your machine",
    "connect": "Connect",
    "cameras": "Cameras",
    "first_motion": "First motion",
    "bed": "The bed",
    "visual_homing": "Visual homing",
    "top_camera_scale": "Top camera scale",
    "nozzle_tips": "Nozzle tips",
    "bottom_camera": "Bottom camera",
    "nozzle_offsets": "Nozzle offsets, heights and vacuum",
    "nozzle_runout": "Nozzle runout",
    "discard_park": "Discard and park",
    "feeders": "Feeders",
    "strips_trays": "Strips and trays",
    "done": "Done",
}

BRING_UP_STEPS: dict[PageId, tuple[str, ...]] = {
    "connect": ("probe",),
    "cameras": ("camera",),
    "first_motion": ("slow", "home", "jog", "nozzles", "outputs", "stop"),
    "bed": ("keep_outs",),
}
"""Setup pages whose state comes, until they are built into the wizard, from the steps
of the bring-up checklist."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class PageRecord(_Model):
    status: Literal["done", "skipped"]
    at: datetime


class WizardRecord(_Model):
    format: Literal["picnicplace-wizard"] = "picnicplace-wizard"
    version: Literal[1] = 1
    pages: dict[ConfirmedPage, PageRecord] = Field(default_factory=dict[ConfirmedPage, PageRecord])


class WizardStore:
    """The pages the owner has confirmed or skipped, kept across restarts. With no path
    (tests), kept in memory."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._memory = WizardRecord()

    def get(self) -> WizardRecord:
        if self._path is None:
            return self._memory.model_copy(deep=True)
        if not self._path.is_file():
            return WizardRecord()
        try:
            return WizardRecord.model_validate_json(self._path.read_text())
        except ValueError:
            return WizardRecord()

    def set(self, page: ConfirmedPage, status: Literal["done", "skipped", "todo"]) -> None:
        with self._lock:
            record = self.get()
            if status == "todo":
                record.pages.pop(page, None)
            else:
                record.pages[page] = PageRecord(
                    status=status, at=datetime.now(UTC).replace(microsecond=0)
                )
            if self._path is None:
                self._memory = record
            else:
                write_atomic(self._path, record.model_dump_json(indent=2))


class WizardPage(WireModel):
    id: PageId
    number: int
    part: Literal[1, 2] = Field(description="1: setting the machine up. 2: calibrating it.")
    title: str
    status: PageStatus
    detail: str = Field(description="Where it stands, in words: what was found, or what to do.")
    stale: str | None = Field(description="Why it is out of date, if it is.")
    warning: str | None = Field(description="Something in its result worth a look.")
    measured_at: datetime | None
    optional: bool = Field(description="The owner may skip it.")
    action: Action | None = Field(description="What starts its measurement, if anything does.")
    confirm: bool = Field(description="The owner finishes it by confirming it (or skipping).")


class WizardState(WireModel):
    stage: Stage
    next_page: PageId | None = Field(
        description="The first page still to do (or out of date), where Continue opens."
    )
    enforced: bool = Field(
        description=(
            "The machine is held to the stage's limits. The simulator is not, unless its "
            "config asks to be (sim_calibration_limits)."
        )
    )
    pages: list[WizardPage]


@dataclass(frozen=True)
class Limit:
    """What the stage says about a task: refused (and why), or allowed, maybe with a
    warning."""

    refused: str | None = None
    warning: str | None = None


def _line(checklist: CalibrationChecklist, line_id: LineId) -> CalibrationLine | None:
    return next((line for line in checklist.lines if line.id == line_id), None)


def build_pages(
    profile: MachineProfile,
    checklist: CalibrationChecklist,
    bring_up: BringUpRecord,
    record: WizardRecord,
) -> list[WizardPage]:
    """Every page of the wizard, in order, with where each stands."""
    pages: list[WizardPage] = []

    def add(
        page_id: PageId,
        status: PageStatus,
        detail: str,
        *,
        line: CalibrationLine | None = None,
        action: Action | None = None,
        confirm: bool = False,
        stale: str | None = None,
    ) -> None:
        pages.append(
            WizardPage(
                id=page_id,
                number=len(pages) + 1,
                part=1 if len(pages) < 6 else 2,
                title=TITLES[page_id],
                status=status,
                detail=detail,
                stale=stale if stale is not None else (line.stale if line else None),
                warning=line.warning if line else None,
                measured_at=line.measured_at if line else None,
                optional=page_id in OPTIONAL,
                action=action,
                confirm=confirm,
            )
        )

    def from_line(page_id: PageId, line_id: LineId, todo: str | None = None) -> None:
        """A page measured by a calibration line: done or out of date once measured."""
        line = _line(checklist, line_id)
        if line is None or line.status == "not_needed":
            add(page_id, "not_needed", line.detail if line else "Nothing to do on this machine.")
        elif line.status in ("done", "stale"):
            add(page_id, line.status, line.detail, line=line, action=line.action)
        else:
            add(page_id, "todo", todo or line.detail, line=line, action=line.action)

    def confirmed(page_id: ConfirmedPage) -> PageStatus:
        r = record.pages.get(page_id)
        return r.status if r else "todo"

    def from_bring_up(page_id: PageId, what: str) -> None:
        steps = [bring_up.steps.get(s) for s in BRING_UP_STEPS[page_id]]
        open_ = [
            s
            for s, r in zip(BRING_UP_STEPS[page_id], steps, strict=True)
            if r is None or r.status not in ("done", "skipped")
        ]
        if not open_:
            add(page_id, "done", f"{what}: checked in the bring-up checklist.")
        else:
            add(
                page_id,
                "todo",
                f"{what}: in the bring-up checklist, until this page is built into the wizard.",
            )

    # Part 1: setting the machine up.
    add("welcome", "done", "Where each page stands, and Continue to the next.")
    add("machine", "done", f"{profile.name}, from the config.")
    from_bring_up("connect", "The serial port, and the firmware probed")
    from_bring_up("cameras", "Which camera is which, and the top camera's picture moving right")
    from_bring_up("first_motion", "Homing, each axis's direction, Z, the pumps and valves, Stop")
    from_bring_up("bed", "Where boards cannot go on the bed")

    # Part 2: calibrating it.
    from_line("visual_homing", "homing_fiducial")
    top = _line(checklist, "top_camera_scale")
    if top is not None and top.status == "assumed" and top.action is None:
        add("top_camera_scale", "not_needed", top.detail)
    else:
        from_line(
            "top_camera_scale",
            "top_camera_scale",
            "Measure the top camera's millimeters per pixel over the homing fiducial.",
        )

    if not profile.nozzle_tips:
        add("nozzle_tips", "not_needed", "This machine's profile lists no nozzle tips.")
    else:
        unsaid = [n.label for n in profile.nozzles if n.tip is None]
        fitted = ", ".join(f"{n.label}: {n.tip or 'not said'}" for n in profile.nozzles)
        if unsaid:
            add(
                "nozzle_tips",
                "todo",
                f"Say which tip is on {', '.join(unsaid)}. Now: {fitted}.",
                confirm=True,
            )
        else:
            add("nozzle_tips", confirmed("nozzle_tips"), f"Fitted: {fitted}.", confirm=True)

    from_line("bottom_camera", "bottom_camera")

    offsets, vacuum = _line(checklist, "nozzle_offsets"), _line(checklist, "vacuum")
    if offsets is not None and offsets.status in ("done", "stale"):
        stale = [line.stale for line in (offsets, vacuum) if line and line.stale]
        detail = offsets.detail
        if vacuum is not None and vacuum.status in ("done", "stale"):
            detail += " " + vacuum.detail
        add(
            "nozzle_offsets",
            "stale" if stale else "done",
            detail,
            line=offsets,
            action="nozzle_offsets",
            stale="; ".join(dict.fromkeys(s for s in stale if s)) or None,
        )
    else:
        add(
            "nozzle_offsets",
            "todo",
            "Lay a small paper disc on the datum board, center the top camera on it, and "
            "calibrate: each nozzle finds its height, picks the disc up, turns it and sets "
            "it down.",
            action="nozzle_offsets",
        )

    add("nozzle_runout", "not_built", "Not measured yet: taken as none.")

    places = [loc.label for loc in profile.locations if loc.kind in ("discard", "park")]
    add(
        "discard_park",
        confirmed("discard_park"),
        f"Set: {', '.join(places)}. Check them, or move them to where they are on this machine."
        if places
        else "Set where rejected parts go, and where the head waits.",
        confirm=True,
    )

    from_line("feeders", "feeder_rails")

    strips = _line(checklist, "strips_and_trays")
    if strips is not None and strips.status == "done":
        add("strips_trays", "done", strips.detail, line=strips)
    else:
        status = confirmed("strips_trays")
        add(
            "strips_trays",
            status,
            "Teach any strips or trays on the bed now, or later from the Feeders workspace.",
            confirm=True,
        )

    rest = [p for p in pages if p.id != "welcome"]
    left = [p for p in rest if p.status == "todo" and not p.optional]
    add(
        "done",
        "done" if not left else "todo",
        "Every page is done: the machine is no longer limited, and can run at full speed."
        if not left
        else f"{len(left)} page{'s' if len(left) != 1 else ''} still to do.",
    )
    return pages


def stage_of(pages: list[WizardPage]) -> Stage:
    required = [p for p in pages if not p.optional and p.id not in ("welcome", "done")]
    if any(p.status == "todo" for p in required):
        return "limited"
    if any(p.status == "stale" for p in pages):
        return "out_of_date"
    return "calibrated"


def build_state(pages: list[WizardPage], enforced: bool) -> WizardState:
    next_page: PageId | None = next(
        (
            p.id
            for p in pages
            if p.id != "done" and (p.status == "stale" or (p.status == "todo" and not p.optional))
        ),
        None,
    )
    return WizardState(stage=stage_of(pages), next_page=next_page, enforced=enforced, pages=pages)


def limit(state: WizardState, kind: str) -> Limit:
    """What the wizard's stage says about starting a task of this kind."""
    if state.stage == "calibrated":
        return Limit()
    pages = {p.id: p for p in state.pages}

    def names(statuses: tuple[PageStatus, ...]) -> str:
        return ", ".join(
            p.title.lower()
            for p in state.pages
            if p.status in statuses and not p.optional and p.id not in ("welcome", "done")
        )

    if state.stage == "out_of_date":
        stale = names(("stale",))
        if kind in RUN_KINDS:
            return Limit(
                refused=f"calibration is out of date ({stale}): redo those pages in the "
                "setup wizard, then run"
            )
        if kind in PICK_KINDS:
            return Limit(warning=f"calibration is out of date ({stale})")
        return Limit()

    if kind in RUN_KINDS or kind in PICK_KINDS:
        what = "runs" if kind in RUN_KINDS else "picks and placements"
        todo = [
            p
            for p in state.pages
            if p.status == "todo" and not p.optional and p.id not in ("welcome", "done")
        ]
        more = f", then {len(todo) - 1} more" if len(todo) > 1 else ""
        return Limit(
            refused=f"{what} wait until the machine is calibrated: continue the setup "
            f"wizard (next: {todo[0].title}{more})"
        )
    if kind in TEACH_KINDS:
        needed = [pages[p] for p in ("first_motion", "visual_homing", "top_camera_scale")]
        missing = [p.title.lower() for p in needed if p.status in ("todo", "stale")]
        if missing:
            return Limit(
                refused="teaching feeders needs homing and the top camera calibrated first: "
                f"finish {', '.join(missing)} in the setup wizard"
            )
    return Limit()


class Wizard:
    """The wizard as the machine stands now."""

    def __init__(
        self,
        machine: "MachineService",
        store: WizardStore,
        bring_up: "BringUpStore",
        enforced: bool,
    ) -> None:
        self.machine = machine
        self.store = store
        self.bring_up = bring_up
        self.enforced = enforced
        self.feeders: FeederService | None = None

    def state(self) -> WizardState:
        checklist = current_checklist(self.machine, self.feeders)
        pages = build_pages(self.machine.profile, checklist, self.bring_up.get(), self.store.get())
        return build_state(pages, self.enforced)

    def limit(self, kind: str) -> Limit:
        return limit(self.state(), kind)

    def limited(self) -> bool:
        """Moves are capped at ``LIMITED_SPEED``."""
        return self.state().stage == "limited"
