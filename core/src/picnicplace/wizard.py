"""The setup and calibration wizard (spec §7.7, §8.2): every page from "which machine do
you have" to a calibrated machine, each with where it stands, and what the machine is
allowed to do until they are all done.

Most pages' states come from the machine the core is running (which machine, its
serial port and cameras: ``SetupFacts``, from the wizard's choices in
``machine_setup``), from what the machine has measured (the calibration checklist), and
from checks made by hand (steps of the bring-up checklist). The rest are pages the owner
finishes by confirming them (the tips fitted, the discard and park locations) or may
skip (strips and trays): those are kept in ``wizard.json`` in the data directory, so
leaving the wizard and coming back, from any Studio, finds it where it was.

The limits follow the stage (``limit``). Until every page that applies is done, the
machine homes, jogs and runs the wizard's own steps at a quarter of full speed at most;
picks, placements and runs are refused, and teaching feeders waits for homing and the
top camera. Once calibrated, a page going out of date (after a crash, say) pauses runs
until it is redone; single picks and placements go ahead, with a warning naming it.
"""

import threading
from collections.abc import Callable
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

ConfirmedPage = Literal["discard_park", "strips_trays"]
"""Pages the owner finishes by confirming them, rather than by measuring."""

OPTIONAL: frozenset[PageId] = frozenset({"feeders", "strips_trays"})
"""Pages that need not be done to be calibrated: feeder rails refine where feeders are, but
each feeder's fiducial is still found on its first pick in a run, and rails can only be
calibrated with feeders at their ends."""

STOPS: dict[PageId, Literal[0, 1, 2, 3]] = {
    "welcome": 0,
    "machine": 0,
    "connect": 0,
    "cameras": 0,
    "first_motion": 1,
    "bed": 1,
    "visual_homing": 1,
    "top_camera_scale": 1,
    "bottom_camera": 1,
    "nozzle_tips": 1,
    "feeders": 1,
    "nozzle_offsets": 2,
    "nozzle_runout": 2,
    "discard_park": 3,
    "strips_trays": 3,
    "done": 3,
}
"""Which stop does each page: the owner acts only at the start of a stop, and one Start
runs its steps."""

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
    "cameras": ("camera",),
    "first_motion": ("slow", "home", "jog", "nozzles", "outputs", "stop"),
    "bed": ("keep_outs",),
}
"""Setup pages checked by hand, with steps of the bring-up checklist shown on the page."""


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
    stop: Literal[0, 1, 2, 3] = Field(
        description="Which of the wizard's stops does it: 0 choosing the machine; 1 with a "
        "hand on the power switch; 2 with a paper disc on the datum board; 3 checking the "
        "discard and park spots."
    )


RunStepStatus = Literal["waiting", "running", "done", "failed", "skipped", "stopped"]


class RunStep(WireModel):
    id: str
    title: str
    status: RunStepStatus
    message: str | None = Field(description="What it found or did, or why it failed.")


class WizardRun(WireModel):
    """A stop being run (or the last one run): its steps, one after another."""

    stop: Literal[1, 2]
    steps: list[RunStep]
    running: bool


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
    next_stop: Literal[0, 1, 2, 3] | None = Field(
        description="The first stop with a page still to do (or out of date)."
    )
    run: WizardRun | None = Field(description="The stop being run, or the last one run.")


@dataclass(frozen=True)
class Limit:
    """What the stage says about a task: refused (and why), or allowed, maybe with a
    warning."""

    refused: str | None = None
    warning: str | None = None


def _line(checklist: CalibrationChecklist, line_id: LineId) -> CalibrationLine | None:
    return next((line for line in checklist.lines if line.id == line_id), None)


@dataclass(frozen=True)
class SetupFacts:
    """What the setup pages need to know of the machine the core is running."""

    chosen: Literal["wizard", "config", "none"] = "config"
    """Where the machine came from: chosen in the wizard, set in the config, or neither
    (the simulator, by default)."""
    driver: str = "simulator"
    serial_port: str | None = None
    connected: bool = False
    firmware: str | None = None
    cameras_chosen: bool = True
    """Every camera in the profile has a device (or is off), in the wizard or the config."""
    restart_needed: bool = False
    """The wizard's choices differ from what the core is running: restart it to use them."""


def build_pages(
    profile: MachineProfile,
    checklist: CalibrationChecklist,
    bring_up: BringUpRecord,
    record: WizardRecord,
    facts: SetupFacts | None = None,
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
                stop=STOPS[page_id],
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
            add(page_id, "done", f"{what}: each checked.")
        else:
            left = len(open_)
            add(page_id, "todo", f"{what}: {left} check{'s' if left != 1 else ''} still to do.")

    # Part 1: setting the machine up.
    facts = facts or SetupFacts()
    simulated = facts.driver != "marlin"
    add("welcome", "done", "Where each page stands, and Continue to the next.")
    restart = " Restart the core to use the new choices." if facts.restart_needed else ""
    what = f"{profile.name}{' in the simulator' if simulated else ''}"
    if facts.chosen == "none":
        add(
            "machine",
            "todo",
            "Choose your machine. Until then, the core runs the simulator.",
        )
    elif facts.restart_needed:
        add("machine", "todo", f"Running {what}.{restart}")
    else:
        source = "chosen here" if facts.chosen == "wizard" else "set in the config"
        add("machine", "done", f"{what}, {source}.")

    if simulated:
        add("connect", "not_needed", "The simulator needs no serial port.")
    elif facts.connected and facts.firmware:
        add("connect", "done", f"Connected to {facts.firmware} on {facts.serial_port}.")
    else:
        add(
            "connect",
            "todo",
            f"Not connected to {facts.serial_port or 'a serial port'}: choose the port the "
            "machine is on, probe it, and restart the core.",
        )

    if simulated:
        add("cameras", "not_needed", "The simulator draws its own camera pictures.")
    elif not facts.cameras_chosen:
        add("cameras", "todo", "Choose which camera is the top one and which the bottom.")
    else:
        add("cameras", "done", "Chosen; the top camera's picture is checked by visual homing.")

    # Visual homing finding the datum board's dot where the profile says confirms that X
    # and Y move the way the profile says, the top camera's picture is the right way
    # around, and the keep-outs (placed from the dot) are where the board is.
    homing = _line(checklist, "homing_fiducial")
    seen = homing is not None and homing.status in ("done", "stale")
    if homing is None or homing.status == "not_needed":
        from_bring_up("first_motion", "Homing, each axis's direction, Z, the pumps and valves")
        from_bring_up("bed", "Where boards cannot go on the bed")
    elif seen:
        add(
            "first_motion",
            "done",
            "Homed, and visual homing found the datum board's dot where the profile says: "
            "X, Y and the top camera's picture are the right way around.",
        )
        add(
            "bed",
            "done",
            "The keep-outs are placed from the datum board's dot, found where the profile says.",
        )
    else:
        add(
            "first_motion",
            "todo",
            "Home and run visual homing (Start, with a hand on the power switch).",
        )
        add("bed", "todo", "Found with visual homing.")

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
                f"Named by the bottom camera, or say which tip is on {', '.join(unsaid)}. "
                f"Now: {fitted}.",
            )
        else:
            add("nozzle_tips", "done", f"Fitted: {fitted}.")

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
            "Lay a small paper disc on the datum board, clear of its dot: the top camera "
            "finds it, and each nozzle finds its height, picks the disc up, turns it and "
            "sets it down.",
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


def build_state(
    pages: list[WizardPage], enforced: bool, run: WizardRun | None = None
) -> WizardState:
    next_page: PageId | None = next(
        (
            p.id
            for p in pages
            if p.id != "done" and (p.status == "stale" or (p.status == "todo" and not p.optional))
        ),
        None,
    )
    stops: list[Literal[0, 1, 2, 3]] = [
        p.stop
        for p in pages
        if p.id not in ("welcome", "done")
        and (p.status == "stale" or (p.status == "todo" and not p.optional))
    ]
    return WizardState(
        stage=stage_of(pages),
        next_page=next_page,
        enforced=enforced,
        pages=pages,
        next_stop=min(stops) if stops else None,
        run=run,
    )


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
        facts: Callable[[], SetupFacts] = SetupFacts,
    ) -> None:
        self.machine = machine
        self.store = store
        self.bring_up = bring_up
        self.enforced = enforced
        self.facts = facts
        """What the setup pages know of the machine the core is running."""
        self.run: WizardRun | None = None
        """The stop being run, or the last one run."""
        self.feeders: FeederService | None = None

    def state(self) -> WizardState:
        checklist = current_checklist(self.machine, self.feeders)
        pages = build_pages(
            self.machine.profile,
            checklist,
            self.bring_up.get(),
            self.store.get(),
            self.facts(),
        )
        return build_state(pages, self.enforced, self.run)

    def limit(self, kind: str) -> Limit:
        return limit(self.state(), kind)

    def limited(self) -> bool:
        """Moves are capped at ``LIMITED_SPEED``."""
        return self.state().stage == "limited"
