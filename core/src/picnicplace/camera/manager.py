"""Builds a hub for every camera in the machine profile, from the config."""

from collections.abc import Callable

from picnicplace.camera.device import DeviceCamera
from picnicplace.camera.hub import CameraHub
from picnicplace.camera.synthetic import SceneState, SyntheticCamera
from picnicplace.config import Config
from picnicplace.machine.profile import MachineProfile
from picnicplace.schemas import CameraInfo


class CameraManager:
    def __init__(self, config: Config, profile: MachineProfile, scene: Callable[[], SceneState]):
        self._profile = profile
        self._hubs: dict[str, CameraHub] = {}
        self._sources: dict[str, str] = {}
        for camera in profile.cameras:
            settings = config.camera(camera.id)
            self._sources[camera.id] = settings.source
            if settings.source == "synthetic":
                self._hubs[camera.id] = CameraHub(
                    camera.id, SyntheticCamera(profile, camera, scene)
                )
            elif settings.source == "device":
                assert settings.device is not None
                source = DeviceCamera(
                    settings.device, settings.width, settings.height, settings.fps
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
