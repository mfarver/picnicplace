"""Camera endpoints, hubs, sources, and the synthetic renderers."""

import asyncio

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.camera import hub as hub_module
from picnicplace.camera.device import DeviceCamera
from picnicplace.camera.hub import CameraHub
from picnicplace.camera.source import Image
from picnicplace.camera.synthetic import BottomRenderer, SceneState, TopRenderer, render_size
from picnicplace.machine.profile import load_profile

from .conftest import TEST_PROFILE, fast_config

TOKEN = "correct-horse-battery-staple"


def decode(data: bytes) -> Image:
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image  # type: ignore[return-value]


# --- Endpoints ---------------------------------------------------------------------------


def test_list_and_snapshot(client: TestClient) -> None:
    cameras = client.get("/api/v1/cameras").json()
    assert [(c["id"], c["source"]) for c in cameras] == [
        ("top", "synthetic"),
        ("bottom", "synthetic"),
    ]
    r = client.get("/api/v1/cameras/top/snapshot.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert float(r.headers["x-timestamp"]) > 0
    assert decode(r.content).shape[1] == 1280

    small = client.get("/api/v1/cameras/top/snapshot.jpg?width=320&quality=50")
    assert decode(small.content).shape[1] == 320


def test_stream_parts(client: TestClient) -> None:
    r = client.get("/api/v1/cameras/bottom/stream.mjpg?frames=2&fps=30&width=160")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("multipart/x-mixed-replace; boundary=")
    parts = [p for p in r.content.split(b"--picnicplace-frame\r\n") if p]
    assert len(parts) == 2
    head, body = parts[0].split(b"\r\n\r\n", 1)
    assert b"Content-Type: image/jpeg" in head
    assert b"X-Frame: " in head
    assert decode(body.removesuffix(b"\r\n")).shape[1] == 160


def test_unknown_and_disabled_cameras() -> None:
    config = fast_config()
    config = config.model_validate(
        {**config.model_dump(), "cameras": {"bottom": {"source": "none"}}}
    )
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/cameras/side/snapshot.jpg").status_code == 404
        assert client.get("/api/v1/cameras/bottom/snapshot.jpg").status_code == 503
        info = {c["id"]: c for c in client.get("/api/v1/cameras").json()}
        assert info["bottom"]["source"] == "none"


def test_cameras_config_must_name_profile_cameras() -> None:
    with pytest.raises(ValueError, match="no such camera"):
        fast_config(cameras={"side": {"source": "synthetic"}})


def test_token_in_query() -> None:
    config = fast_config(server={"token": TOKEN})
    with TestClient(create_app(config)) as client:
        url = "/api/v1/cameras/top/snapshot.jpg"
        assert client.get(url).status_code == 401
        assert client.get(f"{url}?token=wrong").status_code == 401
        assert client.get(f"{url}?token={TOKEN}").status_code == 200
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get(url, headers=headers).status_code == 200


# --- Hub and sources ---------------------------------------------------------------------


class _FakeCapture:
    def __init__(self, frames: int) -> None:
        self.left = frames
        self.released = False

    def isOpened(self) -> bool:
        return True

    def read(self) -> tuple[bool, Image | None]:
        if self.left <= 0:
            return False, None
        self.left -= 1
        return True, np.full((48, 64, 3), 100, np.uint8)

    def set(self, prop: int, value: float) -> bool:
        return True

    def get(self, prop: int) -> float:
        return {cv2.CAP_PROP_FRAME_WIDTH: 64, cv2.CAP_PROP_FRAME_HEIGHT: 48}.get(prop, 0.0)

    def release(self) -> None:
        self.released = True


def test_device_camera_and_idle_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_module, "IDLE_STOP_S", 0.05)
    opened: list[_FakeCapture] = []

    def opener(_device: str) -> _FakeCapture:
        cap = _FakeCapture(frames=5)
        opened.append(cap)
        return cap

    async def scenario() -> None:
        source = DeviceCamera("0", 1920, 1080, 30, opener=opener)  # type: ignore[arg-type]
        hub = CameraHub("top", source)
        async with hub.viewer():
            frame = await source.next_frame(0, 2.0)
            assert frame is not None
            assert source.size == (64, 48)
            jpeg = await hub.jpeg(frame, None, 80)
            assert jpeg[:2] == b"\xff\xd8"
            assert await hub.jpeg(frame, None, 80) is jpeg  # encoded once
        # The source keeps running briefly, then stops with no viewers.
        await asyncio.sleep(0.3)
        assert source._thread is None  # pyright: ignore[reportPrivateUsage]
        assert opened and opened[0].released

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_device_camera_reports_open_failure() -> None:
    def opener(_device: str) -> _FakeCapture:
        raise OSError("no such device")

    async def scenario() -> None:
        source = DeviceCamera("/dev/video9", 640, 480, 30, opener=opener)  # type: ignore[arg-type]
        await source.start()
        await asyncio.sleep(0.1)
        assert source.error is not None and "cannot open camera /dev/video9" in source.error
        await source.stop()

    asyncio.run(asyncio.wait_for(scenario(), 10))


# --- Synthetic rendering: features land where the geometry says ---------------------------


def _bright_centroid(image: Image, threshold: int = 200) -> tuple[float, float]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    ys, xs = np.nonzero(gray > threshold)
    assert len(xs) > 0
    return float(xs.mean()), float(ys.mean())


def test_top_camera_sees_the_homing_fiducial_where_expected() -> None:
    profile = load_profile(str(TEST_PROFILE))
    camera = next(c for c in profile.cameras if c.id == "top")
    size = render_size(camera)
    renderer = TopRenderer(profile, camera, size)
    fiducial = next(loc for loc in profile.locations if loc.kind == "homing_fiducial").position
    # Head 1.0 mm left and 0.5 mm toward the front of the fiducial: the fiducial appears
    # right of center and above center (image Y points down).
    state = SceneState(
        position={"X": fiducial.x - 1.0, "Y": fiducial.y - 0.5},
        outputs={"light_top": True},
        held={},
    )
    u, v = _bright_centroid(renderer.render(state))
    ppm = size[0] / camera.field_of_view.x
    assert u == pytest.approx(size[0] / 2 + 1.0 * ppm, abs=1.5)
    assert v == pytest.approx(size[1] / 2 - 0.5 * ppm, abs=1.5)


def test_top_camera_dims_without_light() -> None:
    profile = load_profile(str(TEST_PROFILE))
    camera = next(c for c in profile.cameras if c.id == "top")
    renderer = TopRenderer(profile, camera, (320, 240))
    position = {"X": 100.0, "Y": 100.0}
    lit = renderer.render(SceneState(position, {"light_top": True}, {}))
    dark = renderer.render(SceneState(position, {"light_top": False}, {}))
    assert dark.mean() < lit.mean() * 0.5


def test_bottom_camera_is_mirrored_and_shows_held_parts() -> None:
    profile = load_profile(str(TEST_PROFILE))
    camera = next(c for c in profile.cameras if c.id == "bottom")
    size = render_size(camera)
    renderer = BottomRenderer(profile, camera, size)
    n1 = next(n for n in profile.nozzles if n.id == "N1")
    # N1 sits 1 mm to the right (+X) of the camera center. Looking up, that is image left.
    head = {
        "X": camera.position.x - n1.offset.x + 1.0,
        "Y": camera.position.y - n1.offset.y,
    }
    empty = renderer.render(SceneState({**head, "A": 0.0}, {}, {"N1": False}))
    u, _v = _bright_centroid(empty, threshold=120)
    ppm = size[0] / camera.field_of_view.x
    assert u == pytest.approx(size[0] / 2 - 1.0 * ppm, abs=3)

    held = renderer.render(SceneState({**head, "A": 0.0}, {}, {"N1": True}))
    assert not np.array_equal(empty, held)
