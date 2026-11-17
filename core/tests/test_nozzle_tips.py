"""Nozzle tips: which is fitted on each nozzle, kept across restarts, and each nozzle's Z
offset and vacuum levels kept for each tip it has been calibrated with."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.machine.bottom_camera import focus_height_for, measure_tip, tip_size_note
from picnicplace.machine.errors import VisionError
from picnicplace.machine.profile import NozzleTip, load_profile
from tests.conftest import Session, connect, fast_config
from tests.test_calibration import lines, move_camera_over_disc, offsets_config
from tests.test_placement import feeders, ok, placed, set_up
from tests.test_vision import home


def fit(s: Session, nozzle_id: str, tip_id: str | None) -> dict[str, dict[str, object]]:
    r = s.put(f"/api/v1/machine/nozzles/{nozzle_id}/tip", {"tip_id": tip_id})
    assert r.status_code == 200, r.json()
    return {n["id"]: n for n in r.json()["nozzles"]}


def test_fitting_a_tip_is_kept() -> None:
    config = offsets_config()
    with TestClient(create_app(config)) as client, connect(client) as s:
        description = client.get("/api/v1/machine/description").json()
        assert [t["id"] for t in description["nozzle_tips"]] == [
            "N045",
            "N08",
            "N14",
            "N24",
            "N40",
            "N75",
            "N110",
        ]
        n08 = description["nozzle_tips"][1]
        assert (n08["inner_diameter"], n08["outer_diameter"]) == (0.8, 1.5)
        assert description["nozzle_tips"][5]["outer_diameter"] == 9.0
        assert {t["length"] for t in description["nozzle_tips"][:6]} == {14.5}
        assert all(n["tip"] is None for n in description["nozzles"])
        assert fit(s, "N1", "N08")["N1"]["tip"] == "N08"
        r = s.put("/api/v1/machine/nozzles/N1/tip", {"tip_id": "N99"})
        assert r.status_code == 422 and "no nozzle tip 'N99'" in r.json()["detail"]
        # Changing a tip is recorded only by the client holding control.
        with connect(client, "other") as other:
            r = other.put("/api/v1/machine/nozzles/N2/tip", {"tip_id": "N14"})
            assert r.status_code == 409, r.json()

    with TestClient(create_app(config)) as client:
        nozzles = {n["id"]: n for n in client.get("/api/v1/machine/description").json()["nozzles"]}
        assert nozzles["N1"]["tip"] == "N08" and nozzles["N2"]["tip"] is None


def test_each_tip_keeps_its_own_heights() -> None:
    """The N14 tip is 0.6 mm longer than the N08. N1 is calibrated with each; changing
    back and forth then brings each tip's Z offset back without measuring again. A part's
    top found with one tip is sniffed again with the other, and a nozzle holding a part
    cannot have its tip changed."""
    config = offsets_config(sim_tip_z_error={"N14": 0.6})
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        fit(s, "N1", "N08")
        move_camera_over_disc(client, s, config)
        ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {"nozzle_ids": ["N1"]})))
        line = lines(client)["nozzle_offsets"]
        assert line["status"] == "done" and "Left nozzle with the N08 tip" in str(line["detail"])

        nozzles = fit(s, "N1", "N14")
        assert nozzles["N1"]["z_offset"] == 0.0 and nozzles["N1"]["vacuum"] is None
        line = lines(client)["nozzle_offsets"]
        assert line["status"] == "stale", line
        assert "not measured with the tip fitted on Left nozzle (N14)" in str(line["stale"])

        move_camera_over_disc(client, s, config)
        ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {"nozzle_ids": ["N1"]})))
        assert lines(client)["nozzle_offsets"]["status"] == "done"
        n14 = client.get("/api/v1/machine/description").json()["nozzles"][0]
        assert n14["z_offset"] == pytest.approx(0.6, abs=0.1)

        n08 = fit(s, "N1", "N08")["N1"]
        assert n08["z_offset"] == pytest.approx(0.0, abs=0.1) and n08["vacuum"] is not None
        assert lines(client)["nozzle_offsets"]["status"] == "done"

        # A part's top, found with the N08 tip, is not used with the N14.
        parts = feeders(client).sim_parts
        assert parts is not None
        ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N1"})))
        assert feeders(client).part_top("1") is not None
        r = s.put("/api/v1/machine/nozzles/N1/tip", {"tip_id": "N14"})
        assert r.status_code == 422 and "holding a part" in r.json()["detail"]
        ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        target = {"nozzle_id": "N1", "x": 260.0, "y": 230.0, "rotation": 0.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        assert fit(s, "N1", "N14")["N1"]["z_offset"] == pytest.approx(0.6, abs=0.1)
        assert feeders(client).part_top("1") is None

        # With the longer tip and its own offset, it still sets parts down on the board.
        ok(s.wait(s.post("/api/v1/placement/pick", {"slot_id": "1", "nozzle_id": "N1"})))
        ok(s.wait(s.post("/api/v1/placement/align", {"nozzle_id": "N1"})))
        target = {"nozzle_id": "N1", "x": 300.0, "y": 230.0, "rotation": 0.0}
        ok(s.wait(s.post("/api/v1/placement/place", target)))
        assert parts.crashes == []
        landed = placed(client)[-1]
        assert landed.fell < 0.1
        assert (landed.x, landed.y) == pytest.approx((300.0, 230.0), abs=0.05)

    # After a restart, the N14 is still on N1, with its own offset.
    with TestClient(create_app(config)) as client:
        n1 = client.get("/api/v1/machine/description").json()["nozzles"][0]
        assert n1["tip"] == "N14" and n1["z_offset"] == pytest.approx(0.6, abs=0.1)


def test_a_tip_near_the_discs_edge_seals_but_drops_it() -> None:
    """The left nozzle is 1.3 mm from where the profile says. Before it is calibrated it
    comes down that far from the disc's center: an N14 tip (2.7 mm across) has only 0.35 mm
    of paper beyond its face there, seals, and drops the disc as it lifts. The calibration
    stops and says so, keeping nothing, and the disc lies on the board, not on the nozzle.
    The N08 tip (1.5 mm) has room, and finds the nozzle."""
    config = offsets_config(sim_nozzle_offset_error={"N1": (1.3, 0.0)})
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        fit(s, "N1", "N14")
        move_camera_over_disc(client, s, config)
        op = s.wait(s.post("/api/v1/machine/nozzle-offsets", {"nozzle_ids": ["N1"]}))
        assert op["status"] == "failed", op
        assert "sealed on the disc" in op["error"] and "dropped it as it lifted" in op["error"]
        parts = feeders(client).sim_parts
        assert parts is not None and not parts.held
        assert [p.shape.disc for p in parts.placed] == [6.0]
        outputs = s.state()["outputs"]
        assert not any(on for name, on in outputs.items() if name.startswith(("pump", "valve")))
        assert lines(client)["nozzle_offsets"]["status"] != "done"
        nozzles = {n["id"]: n for n in client.get("/api/v1/machine/description").json()["nozzles"]}
        assert nozzles["N1"]["z_offset"] == 0.0
        assert s.state()["z_safe"]

        fit(s, "N1", "N08")
        move_camera_over_disc(client, s, config)
        ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {"nozzle_ids": ["N1"]})))
        assert lines(client)["nozzle_offsets"]["status"] == "done"


def test_a_big_tips_height_is_measured_on_paper() -> None:
    """A LumenPnP comes with one tip of each size, and the N24 is too big for its disc. The
    offsets are measured with small tips; then, with the N24 fitted, the paper stop's
    Start finds a sheet of paper on the datum board and measures only its height (it is
    0.8 mm longer) and vacuum, leaving where the nozzle is as it was."""
    paper = (245.0, 120.0, 25.0)  # on the datum board, clear of its dot and the disc
    config = offsets_config(sim_tip_z_error={"N24": 0.8}, sim_paper=paper)
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        r = s.post("/api/v1/machine/tip-heights", {})
        assert r.status_code == 422 and "every nozzle" in r.json()["detail"], r.json()
        fit(s, "N1", "N045")
        fit(s, "N2", "N08")
        r = s.post("/api/v1/machine/tip-heights", {"nozzle_ids": ["N1"]})
        assert r.status_code == 422 and "has not been measured yet" in r.json()["detail"]
        move_camera_over_disc(client, s, config)
        ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {})))
        before = client.get("/api/v1/machine/description").json()["nozzles"][0]["offset"]

        fit(s, "N1", "N24")
        line = lines(client)["nozzle_offsets"]
        assert line["status"] == "stale" and "square of plain paper" in str(line["stale"])
        op = s.wait(s.post("/api/v1/wizard/run", {"stop": 2}))
        assert op["status"] == "succeeded", (op, client.get("/api/v1/wizard").json()["run"])
        step = client.get("/api/v1/wizard").json()["run"]["steps"][0]
        assert step["title"] == "Find the paper, and measure each new tip's height", step
        assert "Left nozzle with the N24 tip touched at Z" in str(step["message"]), step

        line = lines(client)["nozzle_offsets"]
        assert line["status"] == "done" and "Tip heights on paper" in str(line["detail"])
        n1 = client.get("/api/v1/machine/description").json()["nozzles"][0]
        assert n1["z_offset"] == pytest.approx(0.8, abs=0.1) and n1["vacuum"] is not None
        assert n1["offset"] == before
        assert lines(client)["vacuum"]["status"] == "done"
        # The paper was picked up and set down where it lay.
        parts = feeders(client).sim_parts
        assert parts is not None
        sheet = next(p for p in parts.placed if p.shape.disc == paper[2])
        assert (sheet.x, sheet.y) == pytest.approx(paper[:2], abs=0.5)

        # Without the paper, the camera says what to lay down, and nothing goes down.
        parts.placed.remove(sheet)
        fit(s, "N2", "N40")
        op = s.wait(s.post("/api/v1/machine/tip-heights", {}))
        assert op["status"] == "failed" and "found no paper" in op["error"], op
        assert "at least 12 mm across" in op["error"], op


def test_small_new_tips_take_their_height_from_the_bare_board() -> None:
    """With the offsets measured, new small tips have only their heights measured by the
    paper stop. On a real LumenPnP (2026-11-16) an N045 pulled the paper up from about a
    millimeter away, and then a plastic punch: it touches a bare spot of the board, which
    the top camera sees is clear, and the paper is picked up only to read the vacuum."""
    paper = (245.0, 120.0, 25.0)  # on the datum board, clear of its dot and the disc
    config = offsets_config(
        sim_paper=paper,
        sim_datum_seal_press=3.0,
        sim_bare_seal_tip_mm=1.5,
        sim_bare_first_seal=0.46,
        sim_paper_lift={"N045": 1.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        fit(s, "N1", "N14")
        fit(s, "N2", "N14")
        move_camera_over_disc(client, s, config)
        ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {})))
        fit(s, "N1", "N045")
        fit(s, "N2", "N08")
        op = ok(s.wait(s.post("/api/v1/machine/tip-heights", {})))
        assert op["message"].count("touched at Z -27.50") == 2, op["message"]
        nozzles = client.get("/api/v1/machine/description").json()["nozzles"]
        assert [n["z_offset"] for n in nozzles] == pytest.approx([0.0, 0.0], abs=0.05)
        assert lines(client)["vacuum"]["status"] == "done"


@pytest.mark.parametrize(("tip", "diameter"), [("N045", 1.0), ("N08", 1.5), ("N75", 9.0)])
def test_the_bottom_camera_sees_the_fitted_tip(tip: str, diameter: float) -> None:
    """The simulated bottom camera draws the end of the tip fitted, in front of its body.
    Calibrating looks for a circle that tip's size, even one larger than a tip of unknown
    size may be (the N75), and says how big its end looked."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_bottom_camera_error": (-0.8, 0.5, -0.4)},
        presence={"heartbeat_timeout_s": 300.0},
    )

    def bottom() -> dict[str, dict[str, float]]:
        cameras = client.get("/api/v1/machine/description").json()["cameras"]
        return next(c for c in cameras if c["looking"] == "up")

    with TestClient(create_app(config)) as client, connect(client) as s:
        design = bottom()
        fit(s, "N1", tip)
        assert home(s)["status"] == "succeeded"
        op = s.wait(s.post("/api/v1/machine/bottom-camera", {}))
        assert op["status"] == "succeeded", op
        detail = str(lines(client)["bottom_camera"]["detail"])
        assert f"as the {tip} tip should" in detail, detail
        seen = float(detail.split("looked ")[1].split(" mm")[0])
        assert seen == pytest.approx(diameter, rel=0.1)
        found = bottom()["position"]
        assert found["x"] == pytest.approx(design["position"]["x"] - 0.8, abs=0.03)
        assert found["y"] == pytest.approx(design["position"]["y"] + 0.5, abs=0.03)


@pytest.mark.parametrize(
    ("tip", "scale", "named", "alike"),
    [
        ("N045", 2.17, "N045", []),
        ("N08", 2.17, "N08", []),
        ("N14", 2.17, "N14", []),
        ("N045", 3.7, None, ["N045", "N08"]),
    ],
)
def test_small_tips_are_named_or_said_to_be_too_alike(
    tip: str, scale: float, named: str | None, alike: list[str]
) -> None:
    """The small tips are named by the size of their ends at a real LumenPnP's bottom
    camera scale (0.067 mm per pixel). On a camera half as fine, an N045's 1 mm end looks
    about 1.3 mm across, more like an N08: rather than name it wrong, the camera says it
    cannot tell the two apart."""
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_tips": {"N1": tip},
            "sim_bottom_camera_scale": scale,
        },
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        assert s.wait(s.post("/api/v1/machine/bottom-camera", {}))["status"] == "succeeded"
        machine = client.app.state.ctx.machine  # type: ignore[attr-defined]
        assert client.portal is not None
        found, _, said = client.portal.call(measure_tip, machine, machine.profile.nozzles[0])
        assert (found.id if found else None, said) == (named, alike)


@pytest.mark.parametrize(("tip", "collars"), [("N24", {"N24": 8.4}), ("N14", {"N14": 8.4})])
def test_a_lit_ring_behind_the_end_is_not_taken_for_a_larger_tip(
    tip: str, collars: dict[str, float]
) -> None:
    """On a real LumenPnP, an N24 showed a lit ring about 8.4 mm across behind its end, much
    like an N75's end, and was named an N75. A tip is named only if its hole is seen inside
    its end, so it is named as it is."""
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_tips": {"N1": tip},
            "sim_tip_collars": collars,
            "sim_bottom_camera_scale": 2.17,
        },
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        fit(s, "N1", "N24")
        assert s.wait(s.post("/api/v1/machine/bottom-camera", {}))["status"] == "succeeded"
        machine = client.app.state.ctx.machine  # type: ignore[attr-defined]
        assert client.portal is not None
        found, _, said = client.portal.call(measure_tip, machine, machine.profile.nozzles[0])
        assert (found.id if found else None, said) == (tip, [])


def test_a_bottom_camera_far_from_the_profiles_scale() -> None:
    """A real LumenPnP's bottom camera saw 3.7 times the profile's field of view: the
    fitted tip, looked for at the profile's scale, was too small to be found. The tip is
    found first by any round mark near the middle, the scale measured by stepping the head,
    and then the tip is centered and sized with the measured scale."""
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_bottom_camera_error": (-0.8, 0.5, -0.4),
            "sim_bottom_camera_scale": 3.7,
        },
        presence={"heartbeat_timeout_s": 300.0},
    )

    def bottom() -> dict[str, dict[str, float]]:
        cameras = client.get("/api/v1/machine/description").json()["cameras"]
        return next(c for c in cameras if c["looking"] == "up")

    with TestClient(create_app(config)) as client, connect(client) as s:
        design = bottom()
        fit(s, "N1", "N24")
        assert home(s)["status"] == "succeeded"
        op = s.wait(s.post("/api/v1/machine/bottom-camera", {}))
        assert op["status"] == "succeeded", op
        found = bottom()
        assert found["field_of_view"]["x"] == pytest.approx(
            design["field_of_view"]["x"] * 3.7, rel=0.02
        )
        assert found["position"]["x"] == pytest.approx(design["position"]["x"] - 0.8, abs=0.05)
        assert found["position"]["y"] == pytest.approx(design["position"]["y"] + 0.5, abs=0.05)
        detail = str(lines(client)["bottom_camera"]["detail"])
        assert "as the N24 tip should" in detail, detail


def test_a_tip_unlike_the_fitted_one_is_noted() -> None:
    n08 = NozzleTip(id="N08", label="N08", inner_diameter=0.8, outer_diameter=1.5)
    assert tip_size_note(n08, 1.6).endswith("as the N08 tip should.")
    note = tip_size_note(n08, 2.7)
    assert "but the N08 tip is 1.5 mm: check which tip is fitted" in note
    assert tip_size_note(None, 5.2) == "The tip's end looked 5.20 mm across."


def test_a_nozzle_goes_as_low_as_it_can_for_a_focus_just_out_of_reach() -> None:
    """A real LumenPnP's right nozzle (Z turned over, limits -28.5..28.5) with an N75 that
    sat 1.1 mm low on the datum board, for a focus at -27.45 or -28.36: it would go to raw
    Z -28.55 or -29.46. Within 1 mm of the focus the picture is still sharp."""
    profile = load_profile("lumenpnp-v4.1")
    right = profile.nozzles[1]
    nozzle = right.model_copy(update={"z_offset": -1.1, "tip": "N75"})
    machine = cast(Any, SimpleNamespace(profile=profile))
    camera = cast(Any, SimpleNamespace(focus_height=-27.0, label="Bottom camera"))
    assert focus_height_for(machine, nozzle, camera) == -27.0
    camera.focus_height = -27.45
    assert focus_height_for(machine, nozzle, camera) == pytest.approx(-28.49 + 1.1)
    camera.focus_height = -28.36
    assert focus_height_for(machine, nozzle, camera) == pytest.approx(-28.49 + 1.1)
    camera.focus_height = -28.6
    with pytest.raises(VisionError, match="cannot reach the bottom camera's focus"):
        focus_height_for(machine, nozzle, camera)
