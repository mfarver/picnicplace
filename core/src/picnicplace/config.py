"""Core configuration, loaded from a TOML file and validated with Pydantic."""

import ipaddress
import os
import tomllib
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from picnicplace.feeders.setup import PickCorrection
from picnicplace.machine.profile import (
    LightColor,
    Location,
    MachineProfile,
    is_profile_path,
    merge_profile_data,
    profile_data,
)


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Section):
    host: str = Field(
        default="127.0.0.1",
        description="Address to bind. Anything other than loopback requires a token.",
    )
    port: int = Field(default=7420, ge=1, le=65535)
    token: str | None = Field(
        default=None,
        min_length=16,
        description="Shared secret for API access. Required when not bound to loopback.",
    )
    studio_dir: Path | None = Field(
        default=None, description="Built Studio GUI to serve at '/', if any."
    )

    @property
    def is_loopback(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False


class PresenceConfig(_Section):
    grace_period_s: float = Field(
        default=5.0,
        ge=0,
        description="How long the last client may be gone before running jobs pause.",
    )
    heartbeat_timeout_s: float = Field(
        default=6.0,
        gt=0,
        description="A client that sends nothing for this long is disconnected.",
    )


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "picnicplace"


class DataConfig(_Section):
    dir: Path = Field(
        default_factory=_default_data_dir,
        description=(
            "Where the core keeps its data, such as the parts library. "
            "Default: $XDG_DATA_HOME/picnicplace, or ~/.local/share/picnicplace."
        ),
    )

    @property
    def library_path(self) -> Path:
        return self.dir.expanduser() / "library.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.dir.expanduser() / "jobs"

    @property
    def setups_dir(self) -> Path:
        return self.dir.expanduser() / "setups"

    @property
    def feeders_path(self) -> Path:
        return self.dir.expanduser() / "feeders.json"

    @property
    def captures_dir(self) -> Path:
        return self.dir.expanduser() / "captures"

    @property
    def calibration_path(self) -> Path:
        return self.dir.expanduser() / "calibration.json"

    @property
    def bringup_path(self) -> Path:
        return self.dir.expanduser() / "bring-up.json"

    @property
    def recordings_dir(self) -> Path:
        return self.dir.expanduser() / "recordings"

    @property
    def setup_path(self) -> Path:
        return self.dir.expanduser() / "machine-setup.json"

    @property
    def wizard_path(self) -> Path:
        return self.dir.expanduser() / "wizard.json"

    @property
    def trained_parts_path(self) -> Path:
        return self.dir.expanduser() / "trained-parts.json"

    @property
    def trained_frames_dir(self) -> Path:
        return self.dir.expanduser() / "trained-parts"

    @property
    def tips_path(self) -> Path:
        return self.dir.expanduser() / "nozzle-tips.json"

    @property
    def local_profile_path(self) -> Path:
        return self.dir.expanduser() / "local-profile.json"


class MarlinConfig(_Section):
    port: str | None = Field(
        default=None, description="Serial device, such as /dev/ttyACM0. Required for 'marlin'."
    )
    baud: int = 115200
    variant: str | None = Field(
        default=None,
        description="Firmware variant from the machine profile, such as 'vacuum-24bit'.",
    )
    connect_wait_s: float = Field(
        default=3.0, ge=0, description="Wait after opening the port, while the board boots."
    )
    command_timeout_s: float = Field(default=10.0, gt=0)


class MachineConfig(_Section):
    driver: Literal["simulator", "marlin", "fake-marlin"] = Field(
        default="simulator",
        description=(
            "'simulator': no hardware. 'marlin': a real machine on marlin.port. "
            "'fake-marlin': the Marlin driver talking to a built-in fake firmware."
        ),
    )
    marlin: MarlinConfig = Field(default_factory=MarlinConfig)
    locations: list[Location] = Field(
        default_factory=list[Location],
        description=(
            "Places this machine's owner chooses, such as the discard and park positions, "
            "added to the machine profile's. The Studio can set the discard and park "
            "locations itself (in the local profile, which wins over these)."
        ),
    )
    profile: str = Field(
        default="lumenpnp-v4.1",
        description="Built-in machine profile ID, or a path to a profile TOML file.",
    )
    pick_correction: PickCorrection = Field(
        default="top",
        description=(
            "How picks are kept centered on the parts, unless a run or a pick says "
            "otherwise. 'top': before each pick from tape, the top camera finds the pocket "
            "from the sprocket holes. 'bottom': no camera before the pick; each feeder's "
            "pick place moves by what the bottom camera saw of its earlier picks. 'off': "
            "pick where the feeder setup says. 'auto': the bottom camera's correction, "
            "with the top camera on each feeder's first pick in a run, before anything is "
            "learned, after an empty pick, and for a few picks after a part sat well off "
            "center. The bottom camera learns from every pick "
            "whatever the choice, and trays, which have no sprocket holes, use what it "
            "learned unless this is 'off'."
        ),
    )
    place_press: float | None = Field(
        default=None,
        ge=0,
        le=1.0,
        description=(
            "How far (mm) to press a part into the board's paste or tape when placing it: "
            "the nozzle goes this far below the part's top, resting on the board. Empty: "
            "the profile's (a LumenPnP's spring-loaded nozzles: 0.3)."
        ),
    )
    check_placements: bool = Field(
        default=False,
        description=(
            "Look at each two-terminal chip with the top camera once a run has placed it, "
            "and pause the run if it landed off its target, turned, or is not there. A run "
            "can ask either way."
        ),
    )
    sniff_heights: bool = Field(
        default=True,
        description=(
            "Find heights by touch during runs and picks: a nozzle, with suction on, steps "
            "down until the vacuum seals. The board's top on one of its fiducials at the "
            "start of each run, and the top of the parts in each feeder, strip, and tray on "
            "the first pick from it (kept with that feeder and part); a strip with no pick "
            "height is searched for from safe Z. Off: the job's or the profile's board "
            "height, and the profile's or the setup's pick heights (a strip with none is not "
            "used)."
        ),
    )
    sim_time_scale: float = Field(
        default=1.0, gt=0, description="Simulator only: values above 1 run faster than real time."
    )
    sim_home_error: tuple[float, float] = Field(
        default=(0.3, -0.2),
        description=(
            "Simulator only: how far (X, Y in mm) the head really is from where homing "
            "says, as with real endstops. Visual homing finds and corrects it."
        ),
    )
    sim_feeders: list[int] = Field(
        default_factory=lambda: [1, 2, 3, 4, 5, 26],
        description=(
            "Simulator and fake firmware: the numbers of the feeder slots that hold a "
            "simulated Photon feeder."
        ),
    )
    sim_board_error: tuple[float, float, float] = Field(
        default=(0.6, -0.4, 0.5),
        description=(
            "Simulator only: how far a job's board really is from where the job puts it, "
            "as X and Y in mm and a turn in degrees about its front-left corner. Fiducial "
            "alignment finds and corrects it."
        ),
    )
    sim_board_bump: tuple[int, float, float] | None = Field(
        default=None,
        description=(
            "Simulator only: once this many parts are placed in a run, the job's board slides "
            "by X and Y mm, with the parts on it, as a real board knocked by the parts pressed "
            "onto its tape did. Checking placements finds its pads moved."
        ),
    )
    sim_top_camera_scale: float = Field(
        default=1.0,
        gt=0,
        description=(
            "Simulator only: the top camera's true field of view, as a multiple of the "
            "profile's. Measuring the camera's scale finds and corrects it."
        ),
    )
    sim_bottom_camera_scale: float = Field(
        default=1.0,
        gt=0,
        description=(
            "Simulator only: the bottom camera's true field of view, as a multiple of the "
            "profile's (a wider lens sees more). Calibrating the bottom camera finds it."
        ),
    )
    sim_bottom_camera_deep_focus: bool = Field(
        default=False,
        description=(
            "Simulator only: the bottom camera is as sharp at every height the focus search "
            "tries, as a real LumenPnP's was, so it finds no focus and keeps the profile's."
        ),
    )
    sim_tips: dict[str, str] = Field(
        default_factory=dict[str, str],
        description=(
            "Simulator only: the nozzle tip really fitted on each nozzle, by nozzle ID, "
            "whatever the operator says: the bottom camera sees these."
        ),
    )
    sim_tip_collars: dict[str, float] = Field(
        default_factory=dict[str, float],
        description=(
            "Simulator only: a lit ring this many mm across behind each kind of nozzle tip's "
            "end, by tip ID, as seen from below on a real LumenPnP (an N24 showed one about "
            "8.4 mm across, much like an N75's end). Naming tips looks for the hole too."
        ),
    )
    sim_bottom_camera_turn: float = Field(
        default=0.0,
        description=(
            "Simulator only: degrees the bottom camera's picture is really turned "
            "(clockwise as shown), as if mounted turned. Calibrating the bottom camera "
            "measures it, and bottom vision undoes it."
        ),
    )
    sim_bottom_washed_out: bool = Field(
        default=False,
        description=(
            "Simulator only: parts held over the bottom camera are white chips, overexposed "
            "under a full light, so no metal shows to fit their pads to until the light is "
            "turned down (as an 0603's outline was all that showed on a real LumenPnP)."
        ),
    )
    sim_bottom_camera_flipped: bool = Field(
        default=False,
        description=(
            "Simulator only: the bottom camera's picture is flipped to show the machine as "
            "from above, not mirrored as from below (as flip_y does on a LumenPnP). "
            "Calibrating the bottom camera measures it."
        ),
    )
    sim_bottom_camera_error: tuple[float, float, float] = Field(
        default=(0.0, 0.0, 0.0),
        description=(
            "Simulator only: how far the bottom camera really is from the profile's place "
            "and focus, as X and Y in mm and its focus height in mm. Calibrating the "
            "bottom camera finds and corrects it."
        ),
    )
    sim_nozzle_offset_error: dict[str, tuple[float, float]] = Field(
        default_factory=dict[str, tuple[float, float]],
        description=(
            "Simulator only: how far each nozzle really is from the profile's offset, by "
            "nozzle ID, as X and Y in mm. Calibrating the nozzle offsets finds and corrects it."
        ),
    )
    sim_nozzle_z_error: dict[str, float] = Field(
        default_factory=dict[str, float],
        description=(
            "Simulator only: how much longer (mm) each nozzle's tip really is than the "
            "profile's, by nozzle ID: it reaches that much lower. Sniffing the datum board "
            "while calibrating the nozzle offsets finds and corrects it."
        ),
    )
    sim_nozzle_runout: dict[str, tuple[float, float]] = Field(
        default_factory=dict[str, tuple[float, float]],
        description=(
            "Simulator only: where each nozzle's tip end really is from the axis it turns "
            "about, by nozzle ID, as X and Y in mm with the nozzle at 0 degrees (it turns "
            "with the nozzle), as for a bent tip. Measuring the nozzle runout finds it."
        ),
    )
    sim_tip_z_error: dict[str, float] = Field(
        default_factory=dict[str, float],
        description=(
            "Simulator only: how much longer (mm) each kind of nozzle tip really is, by tip "
            "ID, on whichever nozzle it is said to be fitted: it reaches that much lower. "
            "Calibrating the nozzle offsets with the tip fitted finds it."
        ),
    )
    sim_calibration_limits: bool = Field(
        default=False,
        description=(
            "Simulator only: hold the simulated machine to the setup wizard's limits, as a "
            "real machine always is (no picks, placements or runs until it is calibrated, "
            "and moves at a quarter of full speed). Off, the simulator can try anything."
        ),
    )
    sim_vacuum_levels: dict[str, tuple[float, float]] = Field(
        default_factory=dict[str, tuple[float, float]],
        description=(
            "Simulator only: what each nozzle's vacuum sensor reads with its tip open and "
            "holding a part, by nozzle ID, as fractions (0 to 1) of the sensor's range, as "
            "for a tip that leaks or a weak pump. Calibrating the nozzle offsets measures "
            "them. Not given: open reads just short of the sensor's threshold, holding a "
            "part well past it."
        ),
    )
    sim_drops_on_lift: dict[str, int] = Field(
        default_factory=dict[str, int],
        description=(
            "Simulator only: how many of its next picks each nozzle drops as it lifts, by "
            "nozzle ID: it seals on the part, then loses it on the way up, as a worn tip "
            "might. A pick checks the part is still held once the vacuum has settled."
        ),
    )
    sim_place_shift: dict[str, tuple[float, float]] = Field(
        default_factory=dict[str, tuple[float, float]],
        description=(
            "Simulator only: how far (X, Y mm) each part a nozzle places lands from where it "
            "was let go, by nozzle ID, as a part dragged as the nozzle lifts would."
        ),
    )
    sim_sticks_on_release: dict[str, int] = Field(
        default_factory=dict[str, int],
        description=(
            "Simulator only: how many of its next parts each nozzle keeps when its suction "
            "goes off, by nozzle ID, as a part stuck to a tip would: the part-off check after "
            "placing finds it still there."
        ),
    )
    sim_datum_height_error: float = Field(
        default=0.0,
        description=(
            "Simulator only: how far (mm, up positive) the datum board's top really is from "
            "the profile's height. Sniffing for it with a nozzle finds it."
        ),
    )
    sim_datum_seal_press: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Simulator only: how far (mm) a tip must be pressed into the bare datum board "
            "before it seals, as a real tip leaks on the board's printing until pressed "
            "into its spring. A paper disc seals at once."
        ),
    )
    sim_bare_seal_tip_mm: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Simulator only: tips with a face up to this size (mm) seal on the bare datum "
            "board as soon as they touch it, however far bigger ones must be pressed (on a "
            "real LumenPnP the N045 and N08 did, the N24 and N75 did not)."
        ),
    )
    sim_bare_first_seal: float = Field(
        default=1.0,
        gt=0,
        le=1,
        description=(
            "Simulator only: how far a small tip's seal on the bare datum board rises in its "
            "first 0.1 mm of touching, as a fraction of the way from open to holding a part "
            "(a real N08 rose 46%, then fully 0.1 mm further in)."
        ),
    )
    sim_paper_lift: dict[str, float] = Field(
        default_factory=dict[str, float],
        description=(
            "Simulator only: how far (mm) a paper disc jumps up to each kind of tip, by tip "
            "ID, with suction on, before the tip touches it (a real N045 lifted one about a "
            "millimeter)."
        ),
    )
    sim_board_height_error: float = Field(
        default=0.0,
        description=(
            "Simulator only: how far (mm, up positive) the top of a board on the staging "
            "plates really is from the profile's board height, as for a thicker board. "
            "Sniffing a fiducial at the start of a run finds it."
        ),
    )
    sim_part_height_error: float = Field(
        default=0.0,
        description=(
            "Simulator only: how far (mm, up positive) the tops of parts in feeders, strips, "
            "and trays really are from the profile's or the setup's pick height. Sniffing "
            "on the first pick from each finds it."
        ),
    )
    sim_parts_turned: dict[str, float] = Field(
        default_factory=dict[str, float],
        description=(
            "Simulator only: degrees each slot's parts really sit turned in their tape from "
            "what the parts library says, by slot ID (a reel of SOT-23s turned 180, say)."
        ),
    )
    sim_empty_pockets: dict[str, int] = Field(
        default_factory=dict[str, int],
        description=(
            "Simulator only: how many empty pockets (a reel's leader) come to each slot's "
            "pick location before its first part, by slot ID, the one there at the start "
            "first. The top camera sees them empty."
        ),
    )
    sim_calibration_disc: tuple[float, float] | None = Field(
        default=None,
        description=(
            "Simulator only: where (X, Y in mm) a 6 mm paper disc lies on the bed, for "
            "calibrating the nozzle offsets. None: there is none."
        ),
    )
    sim_paper: tuple[float, float, float] | None = Field(
        default=None,
        description=(
            "Simulator only: a sheet of plain paper lying on the bed (X, Y of its center, "
            "and its size, in mm; drawn round), for measuring tip heights. None: there is "
            "none."
        ),
    )
    sim_rail_error: dict[str, tuple[float, float, float]] = Field(
        default_factory=dict[str, tuple[float, float, float]],
        description=(
            "Simulator only: how far each feeder rail really is from the profile's place, "
            "by feeder bank ID, as X and Y in mm and a turn in degrees about its first "
            "slot. Rail calibration finds and corrects it."
        ),
    )
    speed_scale: float = Field(
        default=1.0,
        gt=0,
        le=1,
        description=(
            "Cap every move at this fraction of each axis's maximum speed. Use 0.25 or less "
            "when bringing up a new machine. Homing speed is set by the firmware."
        ),
    )

    _profile: MachineProfile | None = PrivateAttr(default=None)
    _data: dict[str, Any] = PrivateAttr(default_factory=dict[str, Any])

    @model_validator(mode="after")
    def _load_profile(self) -> Self:
        data = profile_data(self.profile)
        if self.locations:
            # Validated together, so the added locations get the profile's checks.
            data["locations"] = [
                *data.get("locations", []),
                *(loc.model_dump(mode="json", exclude_defaults=True) for loc in self.locations),
            ]
        self._data = data
        self._profile = MachineProfile.model_validate(data)
        if self.driver in ("marlin", "fake-marlin"):
            if self._profile.marlin is None:
                raise ValueError(f"machine profile {self.profile!r} has no [marlin] settings")
            self._profile.marlin.resolved(self.marlin.variant)  # check the variant exists
            if self.driver == "marlin" and not self.marlin.port:
                raise ValueError("machine.marlin.port is required for the marlin driver")
        return self

    @property
    def machine_profile(self) -> MachineProfile:
        assert self._profile is not None
        return self._profile

    def with_local(self, local: dict[str, Any]) -> MachineProfile:
        """The profile with a local profile (see ``machine/local.py``) laid over it."""
        try:
            return MachineProfile.model_validate(merge_profile_data(self._data, local))
        except ValueError as e:
            raise ValueError(
                f"the local profile does not fit machine profile {self.profile!r}: {e}"
            ) from e

    def apply_local(self, local: dict[str, Any]) -> None:
        """Use the profile with the local profile laid over it from now on."""
        self._profile = self.with_local(local)


class CameraConfig(_Section):
    source: Literal["synthetic", "device", "none"] = Field(
        description=(
            "'device': a real camera. 'synthetic': rendered by the simulator. 'none': off."
        )
    )
    device: str | None = Field(
        default=None,
        description=(
            "For 'device': a path such as /dev/v4l/by-id/usb-...-video-index0, or an index."
        ),
    )
    width: int = Field(default=1920, gt=0)
    height: int = Field(default=1080, gt=0)
    fps: float = Field(default=30.0, gt=0)
    flip_x: bool = Field(default=False, description="Mirror the image left to right.")
    flip_y: bool = Field(default=False, description="Mirror the image top to bottom.")
    rotation: float = Field(
        default=0.0,
        ge=-45,
        le=45,
        description="Rotate the image this many degrees counterclockwise (after flips).",
    )
    light: LightColor | None = Field(
        default=None,
        description=(
            "The color and brightness (each 0 to 255) the camera's light is set to for "
            "vision, if the light can take a color: say a dimmer or blue light, if parts "
            "look washed out at full white. Unset: full white."
        ),
    )

    @model_validator(mode="after")
    def _device_needs_path(self) -> Self:
        if self.source == "device" and not self.device:
            raise ValueError("a camera with source 'device' needs a device")
        return self


class MonitorConfig(CameraConfig):
    """A camera for people to watch the machine through, such as one in front of it
    looking at the head and the bed. It is not part of the machine profile, and nothing
    in the core uses its pictures."""

    source: Literal["device", "none"] = Field(  # pyright: ignore[reportIncompatibleVariableOverride]
        default="device", description="'device': a real camera. 'none': off."
    )
    label: str = Field(default="Monitor", description="Its name in the Studio.")
    width: int = Field(default=1280, gt=0)
    height: int = Field(default=720, gt=0)
    fps: float = Field(
        default=10.0,
        gt=0,
        description=(
            "Kept low by default: it may share a USB bus with the cameras vision relies on."
        ),
    )


class RecorderConfig(_Section):
    record_runs: bool = Field(
        default=False,
        description=(
            "Record every job run: the frames vision looked at and what the machine did, "
            "to send to PicnicPlace's developers. A run can also ask for it."
        ),
    )
    keep: int = Field(default=10, ge=1, description="Keep at most this many recordings.")
    max_mb: int = Field(
        default=5000, ge=10, description="Delete the oldest recordings beyond this many MB."
    )


class Config(_Section):
    server: ServerConfig = Field(default_factory=ServerConfig)
    presence: PresenceConfig = Field(default_factory=PresenceConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    machine: MachineConfig = Field(default_factory=MachineConfig)
    recorder: RecorderConfig = Field(default_factory=RecorderConfig)
    cameras: dict[str, CameraConfig] = Field(
        default_factory=dict[str, CameraConfig],
        description=(
            "Per camera ID from the machine profile. Cameras left out are synthetic with "
            "the simulator and fake-marlin drivers, and off with a real machine."
        ),
    )

    monitors: dict[str, MonitorConfig] = Field(
        default_factory=dict[str, MonitorConfig],
        description=(
            "Cameras for watching the machine, by an ID of your choice: shown in the "
            "Studio, never used by vision. Not part of the machine profile."
        ),
    )

    @model_validator(mode="after")
    def _known_cameras(self) -> Self:
        known = {c.id for c in self.machine.machine_profile.cameras}
        for camera_id in self.cameras:
            if camera_id not in known:
                raise ValueError(
                    f"cameras.{camera_id}: the machine profile has no such camera "
                    f"(it has: {', '.join(sorted(known)) or 'none'}); a camera only for "
                    "watching the machine goes under [monitors] instead"
                )
        for camera_id in self.monitors:
            if camera_id in known:
                raise ValueError(
                    f"monitors.{camera_id}: the machine profile already has a camera with "
                    "this ID; give the monitor another"
                )
        return self

    def camera(self, camera_id: str) -> CameraConfig:
        """Settings for a camera, with the default for the driver if not configured."""
        configured = self.cameras.get(camera_id)
        if configured is not None:
            return configured
        simulated = self.machine.driver in ("simulator", "fake-marlin")
        return CameraConfig(source="synthetic" if simulated else "none")

    @model_validator(mode="after")
    def _require_token_off_loopback(self) -> Self:
        if not self.server.is_loopback and self.server.token is None:
            raise ValueError(
                f"server.host is {self.server.host!r}, which is reachable from the network; "
                "set server.token to a shared secret of at least 16 characters"
            )
        return self


def load_config(path: Path | None) -> Config:
    """Load configuration from a TOML file, or return defaults when ``path`` is None."""
    return Config.model_validate(load_raw(path))


def load_raw(path: Path | None) -> dict[str, Any]:
    """A TOML config file's data, before validation (none for no file)."""
    if path is None:
        return {}
    with path.open("rb") as f:
        data = tomllib.load(f)
    # A profile given as a relative path is relative to the config file.
    machine: dict[str, Any] = data.get("machine", {})
    profile = machine.get("profile")
    if isinstance(profile, str) and is_profile_path(profile) and not Path(profile).is_absolute():
        machine["profile"] = str(path.parent / profile)
    return data
