"""Builds a hub for every camera in the machine profile, from the config."""

from collections.abc import Callable

from picnicplace.camera.device import DeviceCamera
from picnicplace.camera.hub import CameraHub
from picnicplace.camera.synthetic import SceneState, SyntheticCamera
from picnicplace.config import Config
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
        truth.focus_height = (camera.focus_height or SIM_FOCUS_HEIGHT) + dz
    return truth


class CameraManager:
    def __init__(self, config: Config, profile: MachineProfile, scene: Callable[[], SceneState]):
        self._profile = profile
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
                self._hubs[camera.id] = CameraHub(camera.id, SyntheticCamera(seen, truth, scene))
            elif settings.source == "device":
                assert settings.device is not None
                source = DeviceCamera(
                    settings.device,
                    settings.width,
                    settings.height,
                    settings.fps,
                    flip_x=settings.flip_x,
                    flip_y=settings.flip_y,
                    rotation=settings.rotation,
                )
                self._hubs[camera.id] = CameraHub(camera.id, source)

    def hub(self, camera_id: str) -> CameraHub | None:
        return self._hubs.get(camera_id)

    def exists(self, camera_id: str) -> bool:
        return camera_id in self._sources

    def info(self) -> list[CameraInfo]:
        result: list[CameraInfo] = []
        for camera in self._profile.cameras:
            hub = self._hubs.get(camera.id)
            running = hub is not None and hub.source.latest is not None and hub.viewers > 0
            size = hub.source.size if hub is not None else None
            result.append(
                CameraInfo(
                    id=camera.id,
                    label=camera.label,
                    looking=camera.looking,
                    source=self._sources[camera.id],  # type: ignore[arg-type]
                    width=size[0] if size else None,
                    height=size[1] if size else None,
                    streaming=running,
                    error=hub.source.error if hub is not None else "camera is off",
                )
            )
        return result

    async def shutdown(self) -> None:
        for hub in self._hubs.values():
            await hub.shutdown()
