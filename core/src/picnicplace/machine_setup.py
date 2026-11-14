"""The setup wizard's choices: which machine this is, the serial port it is on, and its
cameras (spec §7.7, §8.2).

They are kept in ``machine-setup.json`` in the data directory, written by the wizard,
and laid under the config when the core starts (``layer``): anything the owner set in
``picnicd.toml`` (or on the command line) still wins, and is reported as pinned, so the
wizard can say so. The core never edits the TOML.

Also here: the serial ports and cameras the wizard offers, found without opening them,
and probing a port's firmware without moving anything.
"""

import asyncio
import copy
import glob
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from picnicplace.config import Config, load_raw
from picnicplace.machine.marlin.transport import SerialTransport
from picnicplace.machine.profile import MachineProfile
from picnicplace.probe import run_probe
from picnicplace.schemas import WireModel
from picnicplace.storage import write_atomic
from picnicplace.wizard import SetupFacts

PROBE_TIMEOUT_S = 30.0


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class ProbeResult(WireModel):
    """What a serial port's firmware said, asked without moving anything."""

    serial_port: str
    firmware: str | None = Field(description="Its name and version, from M115.")
    machine_type: str | None
    expected_machine_type: str | None = Field(
        description="What the chosen machine's profile expects, if it says."
    )
    emergency_parser: bool | None = Field(
        description="STOP acts mid-move (the firmware's emergency parser), if it says."
    )
    vacuum: dict[str, dict[str, float | str]] = Field(
        description="The vacuum sensors read in each firmware variant the profile knows: "
        "the one that gives numbers is the one this firmware speaks."
    )
    at: datetime


class CameraChoice(_Model):
    device: str | None = Field(
        default=None, max_length=500, description="A path or an index; None: the camera is off."
    )
    flip_x: bool = Field(default=False, description="Mirror the picture left to right.")
    flip_y: bool = Field(default=False, description="Mirror the picture top to bottom.")
    rotation: float = Field(
        default=0.0, ge=-45, le=45, description="Degrees counterclockwise, after the flips."
    )


class MachineSetup(_Model):
    format: Literal["picnicplace-machine-setup"] = "picnicplace-machine-setup"
    version: Literal[1] = 1
    profile: str | None = Field(
        default=None, description="A built-in machine profile's ID, such as 'lumenpnp-v4.1'."
    )
    driver: Literal["simulator", "marlin"] | None = Field(
        default=None,
        description="'marlin': the real machine on serial_port. 'simulator': try it with none.",
    )
    serial_port: str | None = Field(default=None, max_length=500)
    cameras: dict[str, CameraChoice] = Field(
        default_factory=dict[str, CameraChoice],
        description="Camera ID from the profile to its device and how its picture is turned.",
    )
    probe: ProbeResult | None = Field(default=None, description="The last probe of serial_port.")


SETUP_KEYS = ("machine.profile", "machine.driver", "machine.marlin.port")
"""The config keys the wizard's choices set, besides each camera's (``cameras.<id>``)."""


class SetupStore:
    """The wizard's choices, kept across restarts. With no path (tests), in memory."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._memory = MachineSetup()

    def get(self) -> MachineSetup:
        if self._path is None:
            return self._memory.model_copy(deep=True)
        if not self._path.is_file():
            return MachineSetup()
        try:
            return MachineSetup.model_validate_json(self._path.read_text())
        except ValueError:
            return MachineSetup()

    def put(self, setup: MachineSetup) -> None:
        with self._lock:
            if self._path is None:
                self._memory = setup.model_copy(deep=True)
            else:
                write_atomic(self._path, setup.model_dump_json(indent=2))


def _has(data: dict[str, Any], key: str) -> bool:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]  # pyright: ignore[reportUnknownVariableType]
    return True


def _set(data: dict[str, Any], key: str, value: Any) -> None:
    *path, last = key.split(".")
    node = data
    for part in path:
        node = node.setdefault(part, {})
    node[last] = value


def setup_values(setup: MachineSetup) -> dict[str, Any]:
    """The config keys the wizard's choices set, and their values."""
    values: dict[str, Any] = {}
    if setup.profile is not None:
        values["machine.profile"] = setup.profile
    if setup.driver is not None:
        values["machine.driver"] = setup.driver
    if setup.serial_port is not None:
        values["machine.marlin.port"] = setup.serial_port
    for camera_id, choice in setup.cameras.items():
        values[f"cameras.{camera_id}"] = (
            {
                "source": "device",
                "device": choice.device,
                "flip_x": choice.flip_x,
                "flip_y": choice.flip_y,
                "rotation": choice.rotation,
            }
            if choice.device is not None
            else {"source": "none"}
        )
    return values


def pinned_keys(raw: dict[str, Any], camera_ids: list[str]) -> set[str]:
    """Which of the wizard's keys the TOML (or the command line) sets itself."""
    keys = [*SETUP_KEYS, *(f"cameras.{c}" for c in camera_ids)]
    return {k for k in keys if _has(raw, k)}


def layer(raw: dict[str, Any], setup: MachineSetup) -> tuple[dict[str, Any], set[str]]:
    """The config's raw data with the wizard's choices laid under it: each choice is used
    unless the TOML sets that key. Returns the data and the keys the TOML pinned."""
    data: dict[str, Any] = copy.deepcopy(raw)
    values = setup_values(setup)
    pinned = {k for k in values if _has(raw, k)}
    for key, value in values.items():
        if key not in pinned:
            _set(data, key, value)
    return data, pinned


@dataclass(frozen=True)
class Layered:
    """The config the core runs with: the TOML, over the wizard's choices."""

    config: Config
    pinned: frozenset[str]
    """The wizard's keys the TOML or the command line set, which the wizard cannot change."""
    warning: str | None = None
    """Why the wizard's choices were not used, if they were not."""


def load_layered(path: Path | None, overrides: dict[str, Any] | None = None) -> Layered:
    """Load the TOML config (``overrides``, from the command line, win over it), and lay
    the wizard's choices from its data directory under it."""
    raw = load_raw(path)
    for key, value in (overrides or {}).items():
        _set(raw, key, value)
    base = Config.model_validate(raw)
    setup = SetupStore(base.data.setup_path).get()
    data, _ = layer(raw, setup)
    try:
        config = Config.model_validate(data)
    except ValidationError as e:
        problem = "; ".join(str(err["msg"]) for err in e.errors())
        return Layered(
            base,
            frozenset(pinned_keys(raw, [c.id for c in base.machine.machine_profile.cameras])),
            f"the setup wizard's choices (machine-setup.json) were not used: {problem}",
        )
    cameras = [c.id for c in config.machine.machine_profile.cameras]
    return Layered(config, frozenset(pinned_keys(raw, cameras)))


class SerialPort(WireModel):
    device: str
    label: str = Field(description="What the device's name says, for people.")


class CameraDevice(WireModel):
    device: str = Field(description="What to put in the camera's config: a path or an index.")
    label: str


SERIAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("/dev/serial/by-id/*", "USB serial"),
    ("/dev/ttyACM*", "USB modem"),
    ("/dev/ttyUSB*", "USB serial adapter"),
    ("/dev/cu.usbmodem*", "USB modem"),
    ("/dev/cu.usbserial*", "USB serial adapter"),
)


def serial_ports() -> list[SerialPort]:
    """The serial devices a machine might be on, from their names: none is opened.
    Stable /dev/serial/by-id paths come first, where the system has them."""
    found: list[SerialPort] = []
    seen: set[str] = set()
    for pattern, label in SERIAL_PATTERNS:
        for device in sorted(glob.glob(pattern)):
            target = str(Path(device).resolve())
            if target in seen:
                continue
            seen.add(target)
            name = Path(device).name
            found.append(SerialPort(device=device, label=f"{label}: {name}"))
    return found


def camera_devices() -> list[CameraDevice]:
    """The cameras the wizard offers. On Linux, the stable /dev/v4l/by-id paths (the first
    node of each camera); elsewhere, the first few camera indexes, unchecked."""
    by_id = Path("/dev/v4l/by-id")
    if by_id.is_dir():
        return [
            CameraDevice(device=str(p), label=p.name.removeprefix("usb-").removesuffix("-index0"))
            for p in sorted(by_id.iterdir())
            if p.name.endswith("-index0")
        ]
    if sys.platform.startswith("linux"):
        return []
    return [CameraDevice(device=str(i), label=f"Camera {i}") for i in range(4)]


async def probe_port(
    serial_port: str, profile: MachineProfile, connect_wait_s: float = 3.0
) -> ProbeResult:
    """Ask a serial port's firmware what it is, and read its vacuum sensors in each
    variant the profile knows. Sends no motion command and switches nothing on."""
    transport = await SerialTransport.open(serial_port)
    report = await asyncio.wait_for(
        run_probe(transport, profile, connect_wait_s=connect_wait_s), PROBE_TIMEOUT_S
    )
    parser = report.capabilities.get("EMERGENCY_PARSER")
    return ProbeResult(
        serial_port=serial_port,
        firmware=report.firmware,
        machine_type=report.machine_type,
        expected_machine_type=profile.marlin.expected_machine_type if profile.marlin else None,
        emergency_parser={"1": True, "0": False}.get(parser or ""),
        vacuum=report.vacuum,
        at=datetime.now(UTC).replace(microsecond=0),
    )


def running_values(config: Config) -> dict[str, Any]:
    """The config keys the wizard sets, as the core is running them."""
    values: dict[str, Any] = {
        "machine.profile": config.machine.profile,
        "machine.driver": config.machine.driver,
        "machine.marlin.port": config.machine.marlin.port,
    }
    for camera_id, camera in config.cameras.items():
        values[f"cameras.{camera_id}"] = (
            {
                "source": "device",
                "device": camera.device,
                "flip_x": camera.flip_x,
                "flip_y": camera.flip_y,
                "rotation": camera.rotation,
            }
            if camera.source == "device"
            else {"source": camera.source}
        )
    return values


def changed_keys(config: Config, setup: MachineSetup, pinned: frozenset[str]) -> list[str]:
    """The wizard's choices the core is not running yet (those the TOML pins aside)."""
    running = running_values(config)
    return [
        key
        for key, value in setup_values(setup).items()
        if key not in pinned and running.get(key) != value
    ]


def setup_facts(
    config: Config,
    setup: MachineSetup,
    pinned: frozenset[str],
    connected: bool,
    firmware: str | None,
) -> SetupFacts:
    """What the wizard's setup pages need to know of the machine the core is running."""
    if setup.profile or setup.driver:
        chosen: Literal["wizard", "config", "none"] = "wizard"
    elif pinned & {"machine.profile", "machine.driver"}:
        chosen = "config"
    else:
        chosen = "none"
    cameras = [c.id for c in config.machine.machine_profile.cameras]
    return SetupFacts(
        chosen=chosen,
        driver=config.machine.driver,
        serial_port=config.machine.marlin.port,
        connected=connected,
        firmware=firmware,
        cameras_chosen=all(f"cameras.{c}" in pinned or c in setup.cameras for c in cameras),
        restart_needed=bool(changed_keys(config, setup, pinned)),
    )
