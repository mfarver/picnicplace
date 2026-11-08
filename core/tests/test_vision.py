"""Fiducial finding and visual homing, against the synthetic cameras."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.vision.fiducial import find_circle

from .conftest import TEST_PROFILE, connect, fast_config


def test_find_circle_subpixel() -> None:
    image = np.full((400, 600, 3), 60, np.uint8)
    # A 40 px dot centered at (310.25, 190.75), drawn with subpixel precision.
    cv2.circle(
        image,
        (round(310.25 * 16), round(190.75 * 16)),
        20 * 16,
        (230, 230, 230),
        -1,
        cv2.LINE_AA,
        4,
    )
    cv2.rectangle(image, (50, 50), (90, 70), (230, 230, 230), -1)  # a pad: not round
    found = find_circle(image, 30, 50)
    assert found is not None
    assert found.x == pytest.approx(310.25, abs=0.1)
    assert found.y == pytest.approx(190.75, abs=0.1)
    assert find_circle(image, 60, 80) is None  # wrong size


def visual_config(tmp_path: Path, **machine: Any) -> Any:
    profile = tmp_path / "visual.toml"
    profile.write_text(
        TEST_PROFILE.read_text().replace("safe_z = -1.0", "safe_z = -1.0\nvisual_homing = true")
    )
    return fast_config(machine={"profile": str(profile), **machine})


@pytest.fixture
def visual_client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(visual_config(tmp_path, sim_home_error=(0.3, -0.2)))) as c:
        yield c


def home(s: Any) -> dict[str, Any]:
    return s.wait(s.post("/api/v1/machine/home"))


def found_offset(message: str) -> tuple[float, float]:
    """The (X, Y) offset reported in a visual homing message."""
    m = re.search(r"X ([-+][\d.]+), Y ([-+][\d.]+)", message)
    assert m, message
    return float(m.group(1)), float(m.group(2))


def test_visual_homing_corrects_the_homing_error(visual_client: TestClient) -> None:
    with connect(visual_client) as s:
        op = home(s)
        assert op["status"] == "succeeded", op
        assert found_offset(op["message"]) == pytest.approx((0.3, -0.2), abs=0.02)
        driver = visual_client.app.state.ctx.machine.driver  # type: ignore[attr-defined]
        # Reported and physical positions now agree, to well under the tolerance.
        for axis in ("X", "Y"):
            assert driver.position[axis] == pytest.approx(driver.scene_position[axis], abs=0.01)
        # The camera sits over the homing fiducial (440, 120 on the test machine).
        state = s.state()
        assert (state["position"]["X"], state["position"]["Y"]) == pytest.approx((440, 120))

        # Running it again finds nothing left to correct.
        again = s.wait(s.post("/api/v1/machine/visual-home"))
        assert again["status"] == "succeeded"
        assert found_offset(again["message"]) == pytest.approx((0, 0), abs=0.02)


def test_visual_homing_larger_error(tmp_path: Path) -> None:
    config = visual_config(tmp_path, sim_home_error=(2.5, 1.5))
    with TestClient(create_app(config)) as client, connect(client) as s:
        op = home(s)
        assert op["status"] == "succeeded", op
        assert found_offset(op["message"]) == pytest.approx((2.5, 1.5), abs=0.02)


def test_visual_homing_refuses_a_far_fiducial(tmp_path: Path) -> None:
    """Farther off than the search radius: fail clearly rather than guess."""
    config = visual_config(tmp_path, sim_home_error=(5.0, 0.0))
    with TestClient(create_app(config)) as client, connect(client) as s:
        op = home(s)
        assert op["status"] == "failed"
        assert "not found within 4 mm" in op["error"]


@pytest.mark.parametrize(
    ("dx", "dy", "light"),
    [(0.0, 0.0, True), (3.0, -2.0, True), (-1.2, 0.7, False), (2.2, 2.9, True)],
)
def test_finds_the_datum_board_center(dx: float, dy: float, light: bool) -> None:
    """The LumenPnP datum board: a 1 mm dot in a bare-board window inside a reticle,
    rings, grid lines, and other fiducials. Only the center dot is the homing one."""
    from picnicplace.camera.synthetic import SceneState, TopRenderer, render_size
    from picnicplace.machine.profile import load_profile

    profile = load_profile("lumenpnp-v4.1")
    camera = next(c for c in profile.cameras if c.id == "top")
    size = render_size(camera)
    fiducial = next(loc for loc in profile.locations if loc.kind == "homing_fiducial")
    image = TopRenderer(profile, camera, size).render(
        SceneState(
            position={"X": fiducial.position.x + dx, "Y": fiducial.position.y + dy},
            outputs={"light_top": light},
            held={},
        )
    )
    mm_per_px = camera.field_of_view.x / size[0]
    found = find_circle(image, 0.6 / mm_per_px, 1.5 / mm_per_px, 4.0 / mm_per_px)
    assert found is not None
    seen = profile.image_to_machine(
        (found.x - size[0] / 2) * mm_per_px, (found.y - size[1] / 2) * mm_per_px
    )
    assert seen == pytest.approx((-dx, -dy), abs=0.01)
    assert found.diameter * mm_per_px == pytest.approx(1.0, abs=0.05)


def test_visual_homing_fails_without_a_camera(tmp_path: Path) -> None:
    config = visual_config(tmp_path)
    config = config.model_validate({**config.model_dump(), "cameras": {"top": {"source": "none"}}})
    with TestClient(create_app(config)) as client, connect(client) as s:
        op = home(s)
        assert op["status"] == "failed"
        assert "sent no frame" in op["error"]
        assert s.state()["homed"] is False


def test_camera_scale(visual_client: TestClient) -> None:
    with connect(visual_client) as s:
        home(s)
        op = s.wait(s.post("/api/v1/machine/camera-scale"))
        assert op["status"] == "succeeded", op
        m = re.search(
            r"([\d.]+) mm/pixel in X and ([\d.]+) in Y \(.*implies ([\d.]+)\)", op["message"]
        )
        assert m, op["message"]
        x, y, assumed = (float(g) for g in m.groups())
        # The synthetic camera's true scale is what the profile implies.
        assert x == pytest.approx(assumed, rel=0.01)
        assert y == pytest.approx(assumed, rel=0.01)


def test_visual_homing_on_the_lumenpnp_profile() -> None:
    """The real profile: origin at the right rear (+X left, +Y front), the datum board,
    and the staging plate. The homing error must come back with the right signs."""
    config = fast_config(machine={"profile": "lumenpnp-v4.1", "sim_home_error": (0.4, -0.25)})
    with TestClient(create_app(config)) as client, connect(client) as s:
        op = home(s)
        assert op["status"] == "succeeded", op
        assert found_offset(op["message"]) == pytest.approx((0.4, -0.25), abs=0.02)
        state = s.state()
        assert (state["position"]["X"], state["position"]["Y"]) == pytest.approx((217, 106))


def test_rotated_view_puts_the_front_at_the_bottom() -> None:
    """On the LumenPnP (+X right, +Y toward the back), something behind the camera
    center appears higher in the image, and something at larger X to the right."""
    from picnicplace.camera.synthetic import SceneState, TopRenderer, render_size
    from picnicplace.machine.profile import load_profile

    profile = load_profile("lumenpnp-v4.1")
    camera = next(c for c in profile.cameras if c.id == "top")
    size = render_size(camera)
    fiducial = next(loc for loc in profile.locations if loc.kind == "homing_fiducial")
    # Camera 2 mm in front of (-Y) and 3 mm left of (-X) the fiducial: the fiducial is
    # behind and to the right of the center.
    image = TopRenderer(profile, camera, size).render(
        SceneState(
            position={"X": fiducial.position.x - 3, "Y": fiducial.position.y - 2},
            outputs={"light_top": True},
            held={},
        )
    )
    mm_per_px = camera.field_of_view.x / size[0]
    found = find_circle(image, 0.6 / mm_per_px, 1.5 / mm_per_px, 4.0 / mm_per_px)
    assert found is not None
    assert found.x > size[0] / 2  # right
    assert found.y < size[1] / 2  # higher: toward the back
