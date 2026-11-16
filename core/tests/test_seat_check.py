"""Checking a tip sits as when its height was measured: right after a height is measured
on paper, each tip touches the paper pad glued on the datum board, and a tip change
touches it again."""

import math

from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.machine.nozzle_offsets import search_disc
from picnicplace.machine.tip_heights import find_paper
from tests.conftest import connect
from tests.test_calibration import asked, offsets_config
from tests.test_nozzle_tips import fit
from tests.test_placement import feeders, ok
from tests.test_vision import home

PAD = (225.0, 112.0)
"""On the datum board, 10 mm from its dot: nearer than the disc, which is searched for
nearest the dot first."""


def test_a_tip_change_checks_each_tip_on_the_paper_pad() -> None:
    """The bare board seals a tip only when pressed 3 mm into it (as on a real LumenPnP),
    so the pad, a paper square glued on the board, is touched instead. After a tip
    change, the left tip, pushed on 0.3 mm further, is found sitting lower; the right one,
    as it was."""
    config = offsets_config(sim_datum_seal_press=3.0, sim_paper=(PAD[0], PAD[1], 10.0))
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        fit(s, "N1", "N045")
        fit(s, "N2", "N08")
        r = s.put("/api/v1/machine/locations/paper_pad", {"position": {"x": PAD[0], "y": PAD[1]}})
        assert r.status_code == 200, r.json()

        disc = config.machine.sim_calibration_disc
        assert disc is not None
        cam = client.get("/api/v1/machine/description").json()["cameras"][0]["position"]
        s.wait(
            s.post(
                "/api/v1/machine/move",
                {"position": {"X": disc[0] - cam["x"], "Y": disc[1] - cam["y"]}},
            )
        )
        op = ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {})))
        assert "Not touched" not in op["message"], op["message"]
        machine = client.app.state.ctx.machine  # type: ignore[attr-defined]
        values = machine.calibration.get().lines["nozzle_offsets"].values
        assert "board.N1.N045" in values and "board.N2.N08" in values, values

        # The left tip pushed on further this time: its end reaches 0.3 mm lower.
        machine.driver._tip_errors["N1"] = 0.3
        started = s.post("/api/v1/machine/tip-change", {})
        request = asked(s)
        tips = {"N1": "N045", "N2": "N08"}
        r = s.post("/api/v1/machine/answer", {"request_id": request["id"], "tips": tips})
        assert r.status_code == 200, r.json()
        op = ok(s.wait(started))
        message = op["message"]
        assert "Left nozzle with the N045 tip: its end is 0.30 mm lower" in message, message
        assert "Right nozzle with the N08 tip: sits as when its height was measured" in message
        assert s.state()["z_safe"]

        # Touching the pad never moved it.
        parts = feeders(client).sim_parts
        assert parts is not None
        pad = min(parts.placed, key=lambda p: abs(p.x - PAD[0]) + abs(p.y - PAD[1]))
        assert abs(pad.x - PAD[0]) < 0.05 and abs(pad.y - PAD[1]) < 0.05


def test_the_disc_search_leaves_the_pad_out() -> None:
    """The pad is paper, round here and as bright as the disc, and nearer the board's dot,
    where the search starts: it is not taken for the disc."""
    config = offsets_config(sim_paper=(PAD[0], PAD[1], 6.0), sim_calibration_disc=(210.0, 126.0))
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        r = s.put("/api/v1/machine/locations/paper_pad", {"position": {"x": PAD[0], "y": PAD[1]}})
        assert r.status_code == 200, r.json()
        machine = client.app.state.ctx.machine  # type: ignore[attr-defined]
        portal = client.portal
        assert portal is not None
        at, _ = portal.call(search_disc, machine, None)
        disc = config.machine.sim_calibration_disc
        assert disc is not None
        assert math.hypot(at.x - disc[0], at.y - disc[1]) < 0.1, at


def test_the_paper_search_leaves_the_pad_out() -> None:
    """Looking for a paper square to measure a big tip on, the pad (which cannot be picked
    up, glued down) is left out, though it is searched first and has room enough."""
    pad = (205.0, 112.0)
    config = offsets_config(sim_paper=(pad[0], pad[1], 10.0), sim_calibration_disc=(240.0, 120.0))
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        r = s.put("/api/v1/machine/locations/paper_pad", {"position": {"x": pad[0], "y": pad[1]}})
        assert r.status_code == 200, r.json()
        machine = client.app.state.ctx.machine  # type: ignore[attr-defined]
        portal = client.portal
        assert portal is not None
        # More room than the board's 5 mm copper disc has: it is bright, but not paper.
        at = portal.call(find_paper, machine, 2.8)
        assert math.hypot(at.x - 240.0, at.y - 120.0) < 1.0, at


def test_without_a_pad_the_offsets_say_how_to_set_one() -> None:
    config = offsets_config()
    with TestClient(create_app(config)) as client, connect(client) as s:
        assert home(s)["status"] == "succeeded"
        fit(s, "N1", "N045")
        fit(s, "N2", "N08")
        disc = config.machine.sim_calibration_disc
        assert disc is not None
        cam = client.get("/api/v1/machine/description").json()["cameras"][0]["position"]
        s.wait(
            s.post(
                "/api/v1/machine/move",
                {"position": {"X": disc[0] - cam["x"], "Y": disc[1] - cam["y"]}},
            )
        )
        op = ok(s.wait(s.post("/api/v1/machine/nozzle-offsets", {})))
        assert "set it as the paper pad" in op["message"], op["message"]
