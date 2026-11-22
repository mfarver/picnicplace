"""The calibration token in stop 2: told from a paper disc by its opening, found by it, and
sealed on only by tips that fit inside it."""

import math

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.config import Config
from picnicplace.vision.token import find_opening
from tests.conftest import Session, connect
from tests.test_calibration import asked, lines, offsets_config
from tests.test_vision import home

TOKEN_AT = (217.0, 94.5)

PX_PER_MM = 20.0
MASK = (34, 33, 32)
WHITE = (235, 238, 240)
GOLD = (70, 170, 215)


def _image(
    opening: tuple[float, float] | None, stub: bool = False
) -> tuple[np.ndarray, tuple[float, float]]:
    """A token (or, with no ``opening``, a paper disc) 5 mm across on black mask, at the
    image's center; the opening's center in pixels, offset by ``opening`` mm. ``stub``: a
    tab's stub left on the token's edge, which pulls the outline's center aside."""
    image = np.zeros((480, 640, 3), np.uint8)
    image[:] = MASK
    cx, cy = 320.0, 240.0
    s = 16  # draw at 1/16 pixel
    r = 2.5 * PX_PER_MM

    def circle(x: float, y: float, radius: float, color: tuple[int, int, int]) -> None:
        cv2.circle(
            image, (round(x * s), round(y * s)), round(radius * s), color, -1, cv2.LINE_AA, 4
        )

    circle(cx, cy, r, WHITE)
    if stub:
        cv2.rectangle(
            image,
            (round(cx + r - 2), round(cy - 15)),
            (round(cx + r + 8), round(cy + 15)),
            WHITE,
            -1,
        )
    at = (cx, cy)
    if opening is not None:
        at = (cx + opening[0] * PX_PER_MM, cy + opening[1] * PX_PER_MM)
        circle(*at, 1.25 * PX_PER_MM, GOLD)
    return image, at


def test_the_opening_is_found_where_the_mask_puts_it() -> None:
    """The opening is found to a few hundredths of a millimeter, even 0.15 mm off the
    outline's center and with a tab's stub on the edge, which would carry the outline's
    center round as the token turns."""
    image, (ox, oy) = _image((0.15, -0.1), stub=True)
    found = find_opening(image, 322.0, 240.0, 5.0 * PX_PER_MM, 2.5 * PX_PER_MM)
    assert found is not None
    assert math.hypot(found.x - ox, found.y - oy) / PX_PER_MM < 0.02
    assert found.diameter / PX_PER_MM == pytest.approx(2.5, abs=0.1)


def test_a_paper_disc_has_no_opening() -> None:
    image, _ = _image(None)
    assert find_opening(image, 320.0, 240.0, 5.0 * PX_PER_MM, 2.5 * PX_PER_MM) is None


def token_config(**machine: object) -> Config:
    return offsets_config(
        sim_calibration_disc=None,
        sim_calibration_token=TOKEN_AT,
        sim_datum_seal_press=3.0,
        sim_bare_seal_tip_mm=1.5,
        **machine,
    )


def over_token(client: TestClient, s: Session) -> None:
    cam = client.get("/api/v1/machine/description").json()["cameras"][0]["position"]
    position = {"X": TOKEN_AT[0] - cam["x"], "Y": TOKEN_AT[1] - cam["y"]}
    s.wait(s.post("/api/v1/machine/move", {"position": position}))


def test_the_nozzle_offsets_are_measured_on_the_token() -> None:
    """With the token on the datum board, stop 2 tells it from paper by its opening, and
    measures each nozzle's offset from the opening's center."""
    errors = {"N1": (0.4, -0.25), "N2": (-0.15, 0.3)}
    config = token_config(sim_nozzle_offset_error=errors, sim_tips={"N1": "N045", "N2": "N08"})
    with TestClient(create_app(config)) as client, connect(client) as s:
        design = client.get("/api/v1/machine/description").json()["nozzles"]
        assert home(s)["status"] == "succeeded"
        for nozzle_id, tip_id in (("N1", "N045"), ("N2", "N08")):
            r = s.put(f"/api/v1/machine/nozzles/{nozzle_id}/tip", {"tip_id": tip_id})
            assert r.status_code == 200, r.json()
        over_token(client, s)
        op = s.wait(s.post("/api/v1/machine/nozzle-offsets", {}))
        assert op["status"] == "succeeded", op.get("error")
        nozzles = client.get("/api/v1/machine/description").json()["nozzles"]
        for before, after in zip(design, nozzles, strict=True):
            dx, dy = errors[after["id"]]
            assert after["offset"]["x"] == pytest.approx(before["offset"]["x"] + dx, abs=0.03)
            assert after["offset"]["y"] == pytest.approx(before["offset"]["y"] + dy, abs=0.03)
        detail = str(lines(client)["nozzle_offsets"]["detail"])
        assert "The token's turns, found by its opening, agreed" in detail, detail
        assert detail.count("touching the datum board at Z -27.50") == 2, detail


def test_only_tips_that_fit_inside_the_opening_are_asked_for() -> None:
    """The token's 5 mm would leave room for an N14 (2.7 mm) on paper, but a tip must sit
    inside its 2.5 mm opening, on bare copper: the N14 is changed, and only the N045 and
    N08 are offered."""
    config = token_config()
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        assert s.put("/api/v1/machine/nozzles/N1/tip", {"tip_id": "N14"}).status_code == 200
        assert s.put("/api/v1/machine/nozzles/N2/tip", {"tip_id": "N08"}).status_code == 200
        over_token(client, s)
        started = s.post("/api/v1/machine/nozzle-offsets", {})
        assert started.status_code == 202, started.json()
        request = asked(s)
        assert request["title"] == "Fit small tips for the token", request
        assert "2.5 mm opening" in request["message"] and "N14 (2.7 mm)" in request["message"]
        assert {n["nozzle_id"]: n["choices"] for n in request["nozzles"]} == {
            "N1": ["N045", "N08"],
            "N2": ["N045", "N08"],
        }
        tips = {"N1": "N045", "N2": "N08"}
        s.post("/api/v1/machine/answer", {"request_id": request["id"], "tips": tips})
        op = s.wait(started)
        assert op["status"] == "succeeded", op.get("error")
