"""Builds a hub for every camera in the machine profile, and every monitor camera, from
the config."""

from collections.abc import Callable
from typing import Literal

from picnicplace.camera.device import ControlSettings, DeviceCamera
from picnicplace.camera.hub import CameraHub
from picnicplace.camera.synthetic import SceneState, SyntheticCamera
from picnicplace.config import CameraConfig, Config
from picnicplace.machine.profile import Camera, MachineProfile, Point
from picnicplace.machine.simulator import sim_profile
from picnicplace.schemas import CameraInfo

SIM_FOCUS_HEIGHT = -4.0
"""The simulated bottom camera's focus, for a profile that gives none."""


def sim_truth(camera: Camera, config: Config) -> Camera:
    """The camera as it really is in the simulator: the profile's, off by the config's
    simulator errors. A copy, so calibrating the profile's camera does not move it."""
    truth = camera.model_copy(deep=True)
    machine = config.machine
    if camera.looking == "down" and camera.mount == "head":
        k = machine.sim_top_camera_scale
        truth.field_of_view = Point(x=camera.field_of_view.x * k, y=camera.field_of_view.y * k)
    elif camera.looking == "up":
        dx, dy, dz = machine.sim_bottom_camera_error
        truth.position = Point(x=camera.position.x + dx, y=camera.position.y + dy)
        truth.image_rotation = machine.sim_bottom_camera_turn
        truth.image_mirrored = not machine.sim_bottom_camera_flipped
        k = machine.sim_bottom_camera_scale
        truth.field_of_view = Point(x=camera.field_of_view.x * k, y=camera.field_of_view.y * k)
        truth.focus_height = (
            None
            if machine.sim_bottom_camera_deep_focus
            else (camera.focus_height or SIM_FOCUS_HEIGHT) + dz
        )
    return truth


class CameraManager:
    def __init__(self, config: Config, profile: MachineProfile, scene: Callable[[], SceneState]):
        self._profile = profile
        self._monitors = config.monitors
        self._hubs: dict[str, CameraHub] = {}
        self._sources: dict[str, str] = {}
        for camera in profile.cameras:
            settings = config.camera(camera.id)
            self._sources[camera.id] = settings.source
            if settings.source == "synthetic":
                truth = sim_truth(camera, config)
                # The bottom camera sees the nozzles where they really are.
                seen = (
                    sim_profile(profile, config.machine.sim_nozzle_offset_error)
                    if camera.looking == "up"
                    else profile
                )
                collars = config.machine.sim_tip_collars
                source = SyntheticCamera(
                    seen,
                    truth,
                    scene,
                    collars=collars,
                    washed_out=config.machine.sim_bottom_washed_out,
                )
                self._hubs[camera.id] = CameraHub(camera.id, source)
            elif settings.source == "device":
                self._hubs[camera.id] = CameraHub(camera.id, _device(settings))
        for monitor_id, settings in self._monitors.items():
            self._sources[monitor_id] = settings.source
            if settings.source == "device":
                self._hubs[monitor_id] = CameraHub(monitor_id, _device(settings))

    def hub(self, camera_id: str) -> CameraHub | None:
        return self._hubs.get(camera_id)

    def exists(self, camera_id: str) -> bool:
        return camera_id in self._sources

    def info(self) -> list[CameraInfo]:
        entries: list[tuple[str, str, Literal["down", "up"] | None]] = [
            *((c.id, c.label, c.looking) for c in self._profile.cameras),
            *((i, m.label, None) for i, m in self._monitors.items()),
        ]
        result: list[CameraInfo] = []
        for camera_id, label, looking in entries:
            hub = self._hubs.get(camera_id)
            running = hub is not None and hub.source.latest is not None and hub.viewers > 0
            size = hub.source.size if hub is not None else None
            result.append(
                CameraInfo(
                    id=camera_id,
                    label=label,
                    looking=looking,
                    monitor=looking is None,
                    source=self._sources[camera_id],  # type: ignore[arg-type]
                    width=size[0] if size else None,
                    height=size[1] if size else None,
                    streaming=running,
                    error=hub.source.error if hub is not None else "camera is off",
                )
            )
        return result

    @property
    def monitor_ids(self) -> list[str]:
        return [i for i in self._monitors if i in self._hubs]

    async def shutdown(self) -> None:
        for hub in self._hubs.values():
            await hub.shutdown()


def _device(settings: CameraConfig) -> DeviceCamera:
    assert settings.device is not None
    return DeviceCamera(
        settings.device,
        settings.width,
        settings.height,
        settings.fps,
        flip_x=settings.flip_x,
        flip_y=settings.flip_y,
        rotation=settings.rotation,
        controls=ControlSettings(settings.exposure_ms, settings.gain, settings.white_balance),
    )
