"""Running a whole job on the simulated LumenPnP.

The demo board's import leaves R3, R4, and J1 out (do-not-populate, no part number,
through-hole); here U1's part also has no height, so it is blocked.

The simulator puts the job's board on the bed a little off from where the job says
(``sim_board_error``), so the run must find it by its fiducials. Each test checks where
the parts really landed against where the simulated board really wants them.
"""

import json
import math
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.board.kicad import read_kicad_pcb
from picnicplace.job.models import Job
from picnicplace.job.run import JobRunner, RunItem, fit_board, machine_angle
from picnicplace.library import Part
from picnicplace.library.models import TapeKind
from picnicplace.machine.profile import Point, load_profile
from picnicplace.simparts import PartShape, PlacedPart
from tests.conftest import Session, connect, fast_config, put_taped

DATA = Path(__file__).parent / "data" / "kicad"

PARTS = {
    # part ID: (height, tape width, tape pitch, rotation in the tape, slot)
    "EX-C0805-1U": (0.85, 8, 4, 90, "1"),
    "EX-R0603-10K": (0.45, 8, 4, 90, "2"),
    "EX-R0603-1K": (0.45, 8, 4, 90, "3"),
    "EX-MCU-8": (1.5, 12, 8, 0, "5"),
}


@pytest.fixture
def lumen() -> Iterator[TestClient]:
    # A whole run takes longer than the default heartbeat timeout; the test does not ping.
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1"}, presence={"heartbeat_timeout_s": 300.0}
    )
    with TestClient(create_app(config)) as c:
        yield c


def runner(client: TestClient) -> JobRunner:
    return client.app.state.ctx.runner  # type: ignore[attr-defined,no-any-return]


def placed(client: TestClient) -> list[PlacedPart]:
    parts = client.app.state.ctx.feeders.sim_parts  # type: ignore[attr-defined]
    assert parts is not None
    return list(parts.placed)


def set_up(
    client: TestClient, s: Session, calibrate: bool = True, tape: TapeKind = "embossed"
) -> None:
    """The demo board as a saved job, with its parts in the library and on feeders (with
    their slots calibrated, unless not), in ``tape`` (embossed or paper)."""
    result = read_kicad_pcb((DATA / "picnic-demo.kicad_pcb").read_text(), "picnic-demo")
    library = client.app.state.ctx.library  # type: ignore[attr-defined]
    for footprint in result.footprints:
        library.put_footprint(footprint)
    for p in result.board.placements:
        if p.part_id and p.part_id not in PARTS:
            library.put_part(Part(id=p.part_id, footprint_id=p.footprint_id))
    footprints = {p.part_id: p.footprint_id for p in result.board.placements if p.part_id}
    for part_id, (height, width, pitch, rotation, _) in PARTS.items():
        put_taped(
            library,
            Part(id=part_id, footprint_id=footprints[part_id], height=height),
            tape,
            width,
            pitch,
            rotation,
        )
    s.home()
    ok(s.wait(s.post("/api/v1/feeders/scan")))
    slots = {v[4]: k for k, v in PARTS.items()}
    for slot in s.client.get("/api/v1/feeders").json()["slots"]:
        part_id = slots.get(slot["slot"]["id"])
        if part_id:
            uuid = slot["feeder"]["uuid"]
            r = s.put(f"/api/v1/feeders/assignments/{uuid}", {"part_id": part_id})
            assert r.status_code == 200, r.json()
    if calibrate:
        ok(s.wait(s.post("/api/v1/feeders/calibrate", {"slot_ids": list(slots)})))

    job = Job.for_board(result.board).model_dump(mode="json")
    location = client.post("/api/v1/jobs/find-location", json=job)
    assert location.status_code == 200, location.json()
    job["location"] = location.json()
    save(s, job)


def publish(s: Session, job_id: str = "demo") -> int:
    """Publish a job's working copy as its next revision, with the library as it is now:
    a run uses the revision's copy of the library."""
    r = s.post(f"/api/v1/jobs/{job_id}/revisions", {"author": "test"})
    assert r.status_code == 200, r.json()
    return r.json()["revision"]["number"]


def save(s: Session, job: dict[str, Any], job_id: str = "demo") -> int:
    """Save a job and publish it, so the next run runs it."""
    assert s.put(f"/api/v1/jobs/{job_id}", job).status_code == 200
    return publish(s, job_id)


def ok(op: dict[str, Any]) -> dict[str, Any]:
    assert op["status"] == "succeeded", op
    return op


def angle_error(a: float, b: float, period: float) -> float:
    d = math.fmod(a - b, period)
    return min(abs(d), period - abs(d))


def check_landed(client: TestClient, run: dict[str, Any], turn: float = 0.5) -> int:
    """Every placed item's part is where the simulated board wants it, turned within
    ``turn`` degrees."""
    parts = placed(client)
    n = 0
    for data in run["plan"]["items"]:
        if data["status"] != "placed":
            continue
        item = RunItem.model_validate(data)
        truth = runner(client).sim_target(item)
        assert truth is not None
        target, angle = truth
        part = min(parts, key=lambda p: math.hypot(p.x - target.x, p.y - target.y))
        assert (part.x, part.y) == pytest.approx((target.x, target.y), abs=0.06), item.ref
        # Two-pad parts look the same turned half way round.
        period = 180.0 if item.ref[0] in "RC" else 360.0
        assert angle_error(part.angle, angle, period) < turn, (item.ref, part.angle, angle)
        n += 1
    assert n == len(parts)
    return n


def test_plan_says_what_it_can_and_cannot_do(lumen: TestClient) -> None:
    with connect(lumen) as s:
        set_up(lumen, s)
        r = s.post("/api/v1/run/plan", {"job_id": "demo"})
        assert r.status_code == 200, r.json()
        plan = r.json()
        assert not plan["ok"] and plan["runnable"]
        status = {i["ref"]: i["status"] for i in plan["items"]}
        # Top side only; the bottom-side parts are not on this run.
        assert status == {
            "C1": "pending",
            "J1": "skipped",
            "R1": "pending",
            "R2": "pending",
            "R3": "skipped",
            "R4": "skipped",
            "U1": "blocked",
            "U2": "pending",
        }
        blocked = [i for i in plan["items"] if i["status"] == "blocked"]
        assert blocked[0]["note"] == "EX-SWITCH-1 has no height in the parts library"
        assert any(i["message"].startswith("EX-SWITCH-1 has no") for i in plan["issues"])
        # Lowest parts first, then by feeder slot.
        order = [i["ref"] for i in plan["items"] if i["status"] == "pending"]
        assert order == ["R1", "R2", "C1", "U2"]
        assert plan["fiducials"] == 3
        need = {p["part_id"]: (p["count"], p["slot_ids"]) for p in plan["parts"]}
        assert need["EX-R0603-10K"] == (2, ["2"])
        assert need["EX-SWITCH-1"] == (1, [])

        # Tape no feeder on the machine takes.
        library = lumen.app.state.ctx.library  # type: ignore[attr-defined]
        mcu = library.find_part("EX-MCU-8")
        tape = library.tape_of(mcu)
        assert mcu is not None and tape is not None
        put_taped(library, mcu, tape.kind, 24.0, tape.pitch, tape.rotation)
        # The published revision keeps its own copy of the part's tape.
        items = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()["items"]
        assert next(i for i in items if i["ref"] == "U2")["status"] == "pending"
        assert publish(s) == 2
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        assert plan["revision"] == 2
        u2 = next(i for i in plan["items"] if i["ref"] == "U2")
        assert u2["status"] == "blocked"
        assert u2["note"] == (
            "EX-MCU-8 comes in 24 mm tape; this machine's feeders take 8 and 12 mm "
            "(hold it in a strip on the bed instead)"
        )
        library.put_part(mcu)
        put_taped(library, mcu, tape.kind, tape.width, tape.pitch, tape.rotation)
        # Revision 1 is still there to run, as it was.
        r1 = s.post("/api/v1/run/plan", {"job_id": "demo", "revision": 1}).json()
        assert r1["revision"] == 1
        assert next(i for i in r1["items"] if i["ref"] == "U2")["status"] == "pending"
        assert s.post("/api/v1/run/plan", {"job_id": "demo", "revision": 9}).status_code == 422

        # Blocked placements must be skipped on purpose.
        r = s.post("/api/v1/run/start", {"job_id": "demo", "revision": 1})
        assert r.status_code == 409 and "1 placement is blocked" in r.json()["detail"]


def test_fiducials_are_not_fooled_by_a_smaller_round_mark() -> None:
    """A round silkscreen mark where the job expects each fiducial, smaller than it and
    nearer the camera's center (as a label's "D" was on a real board, 2 mm from the dot):
    the run still finds the fiducials and the board's true position."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_board_error": (1.6, -1.2, 0.5)},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        runner(client).sim_decoys = True
        set_up(client, s)
        op = ok(s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        run = client.get("/api/v1/run").json()
        fix = run["fix"]
        assert len(fix["fiducials"]) == 3 and fix["residual"] < 0.05
        assert (fix["dx"], fix["dy"], fix["angle"]) == pytest.approx((1.6, -1.2, 0.5), abs=0.05)
        assert check_landed(client, run) == 4


def test_check_the_board_without_placing(lumen: TestClient) -> None:
    """Check fiducials: the board is found and fitted with every part turned off, and
    nothing is picked or placed."""
    with connect(lumen) as s:
        set_up(lumen, s)
        job = lumen.get("/api/v1/jobs/demo").json()
        for p in job["board"]["placements"]:
            if p["kind"] == "part":
                p["enabled"] = False
        save(s, job)
        r = s.post("/api/v1/run/start", {"job_id": "demo"})
        assert r.status_code == 409 and "nothing to place" in r.json()["detail"]

        assert lumen.get("/api/v1/run/board-check").json() is None
        op = ok(s.wait(s.post("/api/v1/run/check-board", {"job_id": "demo"})))
        assert op["kind"] == "board_check" and "3 fiducials" in op["message"]
        check = lumen.get("/api/v1/run/board-check").json()
        assert check["job_id"] == "demo" and check["ok"]
        fix = check["fix"]
        assert len(fix["fiducials"]) == 3 and fix["residual"] < 0.05
        assert (fix["dx"], fix["dy"], fix["angle"]) == pytest.approx((0.6, -0.4, 0.5), abs=0.05)
        assert placed(lumen) == []
        assert lumen.get("/api/v1/placement").json()["loads"] == []
        assert lumen.get("/api/v1/run").json() is None  # no run was started


def test_check_needs_fiducials_and_a_location(lumen: TestClient) -> None:
    with connect(lumen) as s:
        set_up(lumen, s)
        job = lumen.get("/api/v1/jobs/demo").json()
        for p in job["board"]["placements"]:
            if p["kind"] == "fiducial":
                p["enabled"] = False
        save(s, job)
        r = s.post("/api/v1/run/check-board", {"job_id": "demo"})
        assert r.status_code == 409 and "no fiducials" in r.json()["detail"]
        # Where the board sits is this machine's setup, not part of the job.
        assert s.put("/api/v1/jobs/demo/setup", {"location": None}).status_code == 200
        r = s.post("/api/v1/run/check-board", {"job_id": "demo"})
        assert r.status_code == 409 and "no location" in r.json()["detail"]


def test_a_part_loaded_while_paused_is_placed_on_resume(lumen: TestClient) -> None:
    """A part no feeder holds at the start is blocked, and the run places the rest. Loaded
    while the run is paused, its placements are planned again on resume, and placed. The
    plan's parts list is the load list: what each part is, its tape, and its references."""
    with connect(lumen) as s:
        set_up(lumen, s)
        slot = next(
            x for x in s.client.get("/api/v1/feeders").json()["slots"] if x["slot"]["id"] == "1"
        )
        uuid = slot["feeder"]["uuid"]
        r = s.put(f"/api/v1/feeders/assignments/{uuid}", {"part_id": None})
        assert r.status_code == 200, r.json()

        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        need = {n["part_id"]: n for n in plan["parts"]}
        missing = need["EX-C0805-1U"]
        assert missing["problem"] == "no feeder holds EX-C0805-1U" and missing["slot_ids"] == []
        assert plan["parts"][0]["problem"] is not None  # what is missing comes first
        chip = need["EX-R0603-10K"]
        assert chip["refs"] and chip["refs"] == sorted(chip["refs"])
        assert chip["footprint_id"] and chip["tape"]["width"] == 8 and chip["tape"]["pitch"] == 4
        assert chip["slot_ids"] == ["2"]

        body = {"job_id": "demo", "skip_blocked": True, "step": True}
        ok(s.wait(s.post("/api/v1/run/start", body)))
        run = lumen.get("/api/v1/run").json()
        blocked = [i for i in run["plan"]["items"] if i["status"] == "blocked"]
        assert {i["note"] for i in blocked} == {
            "no feeder holds EX-C0805-1U",
            "EX-SWITCH-1 has no height in the parts library",
        }

        r = s.put(f"/api/v1/feeders/assignments/{uuid}", {"part_id": "EX-C0805-1U"})
        assert r.status_code == 200, r.json()
        op = ok(s.wait(s.post("/api/v1/run/resume")))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        run = lumen.get("/api/v1/run").json()
        capacitor = [i for i in run["plan"]["items"] if i["part_id"] == "EX-C0805-1U"]
        assert capacitor and all(i["status"] == "placed" for i in capacitor)
        need = {n["part_id"]: n for n in run["plan"]["parts"]}
        assert need["EX-C0805-1U"]["problem"] is None
        assert need["EX-C0805-1U"]["slot_ids"] == ["1"]
        assert not any("EX-C0805-1U" in i["message"] for i in run["plan"]["issues"])
        assert check_landed(lumen, run) == 4


def test_a_reel_is_counted_down_by_each_feed(lumen: TestClient) -> None:
    """A Photon feeder's reel count, from its label, goes down by one part each feed (and
    up for a feed back), is kept while the feeder holds the same part, and is warned of
    in the plan when it has fewer parts left than the job places from it."""
    with connect(lumen) as s:
        set_up(lumen, s)
        slot = next(
            x for x in s.client.get("/api/v1/feeders").json()["slots"] if x["slot"]["id"] == "2"
        )
        uuid = slot["feeder"]["uuid"]

        r = s.put(f"/api/v1/feeders/assignments/{uuid}/reel", {"left": 1, "low": 1})
        assert r.status_code == 200, r.json()
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        short = [i for i in plan["issues"] if "reel of EX-R0603-10K" in i["message"]]
        assert len(short) == 1 and short[0]["severity"] == "warning", plan["issues"]
        assert "has 1 parts left by its count, and the job places 2" in short[0]["message"]

        assert s.put(f"/api/v1/feeders/assignments/{uuid}/reel", {"left": 5, "low": 3}).is_success
        feed = "/api/v1/feeders/slots/2/feed"
        for _ in range(2):
            ok(s.wait(s.post(feed, {})))

        def reel() -> tuple[int | None, int | None]:
            slots = s.client.get("/api/v1/feeders").json()["slots"]
            a = next(x for x in slots if x["slot"]["id"] == "2")["feeder"]["assignment"]
            return a["reel_left"], a["reel_low"]

        assert reel() == (3, 3)
        ok(s.wait(s.post(feed, {"distance": -4.0})))
        assert reel() == (4, 3)
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        assert not [i for i in plan["issues"] if "reel of" in i["message"]]

        # Saving the same part keeps the count; another part is another reel.
        body = {"part_id": "EX-R0603-10K"}
        assert s.put(f"/api/v1/feeders/assignments/{uuid}", body).is_success
        assert reel() == (4, 3)
        assert s.put(f"/api/v1/feeders/assignments/{uuid}", {"part_id": "EX-R0603-1K"}).is_success
        assert reel() == (None, None)
        assert s.put(f"/api/v1/feeders/assignments/{uuid}", {"part_id": None}).is_success
        r = s.put(f"/api/v1/feeders/assignments/{uuid}/reel", {"left": 5})
        assert r.status_code == 422 and "holds no part" in r.json()["detail"], r.json()


def test_the_plan_names_parts_whose_tape_is_guessed(lumen: TestClient) -> None:
    """A tape guessed from the footprint at import is to be checked against the reel."""
    from picnicplace.library.tape_rules import guessed_tape

    with connect(lumen) as s:
        set_up(lumen, s)
        library = lumen.app.state.ctx.library  # type: ignore[attr-defined]
        part = library.find_part("EX-R0603-10K")
        tape = guessed_tape(library.find_footprint(part.footprint_id))
        assert tape is not None
        library.put_tape(tape)
        library.put_part(part.model_copy(update={"tape_id": tape.id}))
        publish(s)
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        found = [i for i in plan["issues"] if "is guessed from its footprint" in i["message"]]
        assert len(found) == 1 and found[0]["message"].startswith(
            "the tape of EX-R0603-10K is guessed from its footprint"
        ), plan["issues"]
        need = next(n for n in plan["parts"] if n["part_id"] == "EX-R0603-10K")
        assert need["tape"]["id"] == "8mm-paper-p4-r90-guessed"


def test_start_in_step_mode(lumen: TestClient) -> None:
    """Started in step mode, the run finds the board and pauses before the first part;
    each step places one batch, and resume places the rest."""
    with connect(lumen) as s:
        set_up(lumen, s)
        body = {"job_id": "demo", "skip_blocked": True, "step": True}
        op = ok(s.wait(s.post("/api/v1/run/start", body)))
        run = lumen.get("/api/v1/run").json()
        assert run["status"] == "paused" and run["fix"] is not None
        assert op["message"].startswith("paused before the first part: board found")
        assert run["placed"] == 0 and placed(lumen) == []

        ok(s.wait(s.post("/api/v1/run/step")))
        run = lumen.get("/api/v1/run").json()
        assert run["status"] == "paused" and 1 <= run["placed"] <= 2
        assert run["message"].startswith("paused: ")

        op = ok(s.wait(s.post("/api/v1/run/resume")))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        assert check_landed(lumen, lumen.get("/api/v1/run").json()) == 4


def test_run_a_job(lumen: TestClient) -> None:
    with connect(lumen) as s:
        set_up(lumen, s)
        op = ok(s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        run = lumen.get("/api/v1/run").json()
        assert run["status"] == "finished" and run["placed"] == 4
        fix = run["fix"]
        assert len(fix["fiducials"]) == 3 and fix["residual"] < 0.05
        assert (fix["dx"], fix["dy"], fix["angle"]) == pytest.approx((0.6, -0.4, 0.5), abs=0.05)
        assert check_landed(lumen, run) == 4
        # Both nozzles did the work.
        assert {i["nozzle"] for i in run["plan"]["items"] if i["status"] == "placed"} == {
            "N1",
            "N2",
        }
        # Finished, the head is parked out of the way for the board to come off: with no
        # park location set, at the back.
        y = next(a for a in lumen.app.state.ctx.machine.profile.axes if a.name == "Y")  # type: ignore[attr-defined]
        assert lumen.get("/api/v1/machine").json()["position"]["Y"] == pytest.approx(y.max)


def test_the_run_finds_the_boards_top_by_touch() -> None:
    """The board is 0.6 mm thicker than the profile's board height: placed there, every
    part would be pressed into it. Before the first part, the run finds the board's top
    by touch on a fiducial, and sets each part down on the board as it is."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_board_height_error": 0.6},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        op = ok(s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        run = client.get("/api/v1/run").json()
        board = load_profile("lumenpnp-v4.1").board_height
        assert board is not None
        assert run["board_z"] == pytest.approx(board + 0.6, abs=0.15)
        parts = client.app.state.ctx.feeders.sim_parts  # type: ignore[attr-defined]
        assert parts.crashes == []
        assert all(p.fell < 0.15 for p in parts.placed)
        assert check_landed(client, run) == 4


def test_a_run_feeds_past_an_empty_pocket() -> None:
    """Slot 2's reel starts with an empty pocket, as a real reel's leader may. In paper
    tape the top camera sees it empty before a nozzle goes down, and the pick feeds once
    and takes the part in the next pocket."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_empty_pockets": {"2": 2}},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s, tape="paper")
        op = ok(s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        run = client.get("/api/v1/run").json()
        assert check_landed(client, run) == 4


def test_a_long_empty_leader_pauses_the_run() -> None:
    """Slot 2's reel has a long empty leader. Pockets seen empty are fed past, and one
    seen empty twice is tried anyway; after three tries come up empty, the run stops for
    the operator to see to the feeder, once the other nozzle's part is placed."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_empty_pockets": {"2": 8}},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s, tape="paper")
        # Three tries, each feeding past empty pockets and searching twice: many events.
        start = s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})
        op = s.wait(start, limit=3000)
        run = client.get("/api/v1/run").json()
        assert run["status"] == "paused", (op, run["error"])
        assert "(3 tries). Check the feeder" in run["error"], run["error"]
        # The other nozzle's part was placed first: none waits on a nozzle meanwhile.
        assert s.client.get("/api/v1/placement").json()["loads"] == []
        assert run["held"] == {} and run["placed"] == 1, run["placed"]
        assert check_landed(client, run) == 1
        # The step shown is the pick that failed.
        assert run["current"]["action"] == "picking"
        assert run["plan"]["items"][run["current"]["index"]]["slot_id"] == "2"


def test_a_part_loose_on_the_tape_pauses_the_run() -> None:
    """A part that jumped out of its pocket lies on the paper tape by the pick pocket:
    the run stops before picking there, and says where it is."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1"}, presence={"heartbeat_timeout_s": 300.0}
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s, tape="paper")
        feeders = client.app.state.ctx.feeders  # type: ignore[attr-defined]
        slot = next(
            x for x in client.get("/api/v1/feeders").json()["slots"] if x["slot"]["id"] == "2"
        )
        pick = slot["pick"]
        # Between the pick pocket and the next one, turned, as one was on a real LumenPnP.
        tape = feeders.slot("2").rotation
        a = math.radians(tape)
        loose = PlacedPart(
            PartShape(body=(1.6, 0.8), height=0.45),
            pick["x"] + 2.0 * math.cos(a),
            pick["y"] + 2.0 * math.sin(a),
            tape + 30,
        )
        feeders.sim_parts.placed.append(loose)
        op = s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True}))
        run = client.get("/api/v1/run").json()
        assert run["status"] == "paused", (op, run["error"])
        assert "a part lying loose on the tape near the pick pocket" in run["error"], run["error"]


def test_pause_skip_and_resume(lumen: TestClient) -> None:
    with connect(lumen) as s:
        set_up(lumen, s)
        r = s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})
        assert r.status_code == 202, r.json()
        assert s.post("/api/v1/run/pause").json()["pause_requested"]
        s.wait(r)
        run = lumen.get("/api/v1/run").json()
        # Paused before the first part, with the board found.
        assert run["status"] == "paused" and run["placed"] == 0 and run["fix"] is not None
        assert run["current"] is None  # a clean pause: no step under way

        ok(s.wait(s.post("/api/v1/run/step")))
        run = lumen.get("/api/v1/run").json()
        assert run["status"] == "paused" and run["placed"] == 2  # one batch: both nozzles
        assert "head moved" not in run["message"]  # a step leaves the head where it is
        assert lumen.get("/api/v1/placement").json()["loads"] == []

        u2 = next(i for i in run["plan"]["items"] if i["ref"] == "U2")
        r = s.put(f"/api/v1/run/items/{u2['index']}", {"skip": True})
        assert r.status_code == 200, r.json()
        op = ok(s.wait(s.post("/api/v1/run/resume")))
        assert op["message"] == "finished: 3 placed, 1 blocked, 4 skipped"
        run = lumen.get("/api/v1/run").json()
        assert check_landed(lumen, run) == 3
        skipped = {i["ref"]: i["note"] for i in run["plan"]["items"] if i["status"] == "skipped"}
        assert skipped["U2"] == "skipped by the operator"


def test_board_fit() -> None:
    about = Point(x=100, y=100)
    expected = [Point(x=100, y=100), Point(x=160, y=100), Point(x=100, y=140)]
    a = math.radians(0.7)

    def moved(p: Point) -> Point:
        x, y = p.x - about.x, p.y - about.y
        return Point(
            x=about.x + x * math.cos(a) - y * math.sin(a) + 0.3,
            y=about.y + x * math.sin(a) + y * math.cos(a) - 0.2,
        )

    dx, dy, angle = fit_board(about, [(e, moved(e)) for e in expected])
    assert (dx, dy, angle) == pytest.approx((0.3, -0.2, 0.7), abs=1e-9)
    # One fiducial: a shift only.
    assert fit_board(about, [(expected[1], Point(x=161, y=99))]) == pytest.approx((1, -1, 0))


def test_machine_angles_follow_the_axes() -> None:
    lumen = load_profile("lumenpnp-v4.1")  # +X right, +Y back: the same as the view
    assert machine_angle(lumen, 30) == pytest.approx(30)
    flipped = lumen.model_copy(update={"x_points": "left", "y_points": "front"})
    assert machine_angle(flipped, 30) == pytest.approx(-150)


def test_a_jammed_feeder_pauses_the_run(lumen: TestClient) -> None:
    """The second batch is C1 (slot 1) and U2 (slot 5). With slot 5 jammed, C1 is placed
    before the run pauses, so no part waits on a nozzle while the jam is cleared; then
    resuming places U2."""
    with connect(lumen) as s:
        set_up(lumen, s)
        sim = lumen.app.state.ctx.feeders._sim  # type: ignore[attr-defined]
        sim.feeders[5].jammed = True
        op = s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True}))
        assert op["status"] == "failed" and "could not reach the next part" in op["error"]
        run = lumen.get("/api/v1/run").json()
        assert run["status"] == "paused" and op["error"] in run["error"]
        assert run["placed"] == 3
        items = {i["ref"]: i for i in run["plan"]["items"]}
        assert items["C1"]["status"] == "placed"
        # It shows where it stopped: picking U2, with nothing left on a nozzle.
        current = run["current"]
        u2 = items["U2"]["index"]
        assert (current["stage"], current["action"], current["index"]) == ("item", "picking", u2)
        assert run["held"] == {}
        assert lumen.get("/api/v1/placement").json()["loads"] == []

        sim.feeders[5].jammed = False
        op = ok(s.wait(s.post("/api/v1/run/resume")))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        run = lumen.get("/api/v1/run").json()
        assert check_landed(lumen, run) == 4 and run["current"] is None


def test_the_first_pick_from_each_feeder_finds_its_fiducial() -> None:
    """Rails a little off the profile, and no slot calibrated: the first pick from each
    feeder in the run finds its fiducial (searching around where the rail puts it), and
    the fix is kept with the feeder."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1", "sim_rail_error": {"front": (2.0, -1.5, 0.3)}},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s, calibrate=False)
        op = ok(s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        run = client.get("/api/v1/run").json()
        used = {i["slot_id"] for i in run["plan"]["items"] if i["status"] == "placed"}
        assert sorted(run["located"]) == sorted(used) and len(used) == 3
        assert check_landed(client, run) == 4
        feeders = client.get("/api/v1/feeders").json()["slots"]
        for slot in feeders:
            if slot["slot"]["id"] in run["located"]:
                cal = slot["calibration"]
                assert cal["basis"] == "fiducial" and cal["feeder"] == slot["feeder"]["uuid"]


def test_a_run_follows_feeders_moved_since_planning(lumen: TestClient) -> None:
    """The feeders in slots 2 and 3 trade places (reels and all) after the job is
    planned: the run scans before it starts, picks each part from where it is now, and
    does not use the other feeder's fix in either slot."""
    with connect(lumen) as s:
        set_up(lumen, s)
        before = {
            i["ref"]: i["slot_id"]
            for i in s.post("/api/v1/run/plan", {"job_id": "demo"}).json()["items"]
            if i["slot_id"] in ("2", "3")
        }
        sim = lumen.app.state.ctx.feeders._sim  # type: ignore[attr-defined]
        sim.feeders[2].uuid, sim.feeders[3].uuid = sim.feeders[3].uuid, sim.feeders[2].uuid
        op = ok(s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        run = lumen.get("/api/v1/run").json()
        after = {i["ref"]: i["slot_id"] for i in run["plan"]["items"] if i["ref"] in before}
        assert after == {ref: "3" if slot == "2" else "2" for ref, slot in before.items()}
        assert check_landed(lumen, run) == 4


def test_a_run_pauses_when_a_part_is_no_longer_loaded(lumen: TestClient) -> None:
    with connect(lumen) as s:
        set_up(lumen, s)
        sim = lumen.app.state.ctx.feeders._sim  # type: ignore[attr-defined]
        del sim.feeders[5]
        op = s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True}))
        assert op["status"] == "failed"
        assert "feeders changed since the run was planned: U2 (no feeder holds" in op["error"]
        run = lumen.get("/api/v1/run").json()
        assert run["status"] == "paused" and run["placed"] == 0
        assert run["current"] == {
            "stage": "scan",
            "action": "scanning",
            "index": None,
            "nozzle": None,
        }


def test_without_a_discard_location_the_operator_takes_the_part_off(lumen: TestClient) -> None:
    """Bottom vision cannot find R1's part, and there is no bin to drop it in: the run
    parks the head and pauses with the part held. Skip is refused while it is on the
    nozzle; after the operator takes it off and says so, resuming picks R1 again."""
    with connect(lumen) as s:
        set_up(lumen, s)
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        assert any("no discard location" in i["message"] for i in plan["issues"])

        recorder = lumen.app.state.ctx.machine.recorder  # type: ignore[attr-defined]
        vision = recorder.vision
        blind = {"on": True}

        async def blind_for_now(name: str, *args: Any, **kwargs: Any) -> Any:
            # Every way of looking fails, the other lights and methods too, until it pauses.
            if name in ("find_part", "fit_pads") and blind["on"]:
                return None
            return await vision(name, *args, **kwargs)

        recorder.vision = blind_for_now
        # Not trained either (training would see it: it looks without the recorder).
        placement = lumen.app.state.ctx.placement  # type: ignore[attr-defined]
        placement._may_train = lambda part_id: False  # type: ignore[method-assign]
        op = s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True}))
        blind["on"] = False
        assert op["status"] == "failed"
        assert (
            "saw no part on N1, nor under other lights and ways of looking, and there is no "
            "discard location"
        ) in op["error"], op["error"]
        assert "The head moved to the back" in op["error"]
        run = lumen.get("/api/v1/run").json()
        assert run["status"] == "paused" and "N1" in run["held"]
        index = run["held"]["N1"]
        r = s.put(f"/api/v1/run/items/{index}", {"skip": True})
        assert r.status_code == 409 and "Part removed" in r.json()["detail"]

        ok(s.wait(s.post("/api/v1/placement/release", {"nozzle_id": "N1"})))
        loads = lumen.get("/api/v1/placement").json()["loads"]
        assert [x["nozzle"] for x in loads] == ["N2"]

        op = ok(s.wait(s.post("/api/v1/run/resume")))
        # The run says how it went: one part the bottom camera could not find.
        assert op["message"] == (
            "finished: 4 placed, 1 blocked, 3 skipped (the bottom camera could not find 1 "
            "of 5 parts)"
        )
        stats = lumen.get("/api/v1/run").json()["stats"]
        assert (stats["alignments"], stats["failed_alignments"]) == (5, 1)
        assert check_landed(lumen, lumen.get("/api/v1/run").json()) == 4


def test_a_part_training_cannot_see_is_skipped_not_picked_again() -> None:
    """A new IC is trained on its first pick; when training finds no way of seeing it and
    no other look does either, the part is discarded and its placements skipped (each
    pick would cost a part and see no more), and the run goes on with the rest. They may
    be un-skipped once the part is trained."""
    from picnicplace.placement import PlacementError

    config = fast_config(
        machine={"profile": "lumenpnp-v4.2", "train_new": "multi-pad"},
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        placement = client.app.state.ctx.placement  # type: ignore[attr-defined]
        recorder = client.app.state.ctx.machine.recorder  # type: ignore[attr-defined]
        vision = recorder.vision
        trainings: list[str] = []

        async def cannot_train(nozzle: Any, load: Any, part: Any, *args: Any) -> Any:
            trainings.append(part.id)
            raise PlacementError(f"training found no way of seeing {part.id} in every frame")

        async def mcu_unseen(name: str, *args: Any, **kwargs: Any) -> Any:
            if name in ("find_part", "fit_pads") and kwargs.get("part") == "EX-MCU-8":
                return None
            return await vision(name, *args, **kwargs)

        placement._train_held = cannot_train  # type: ignore[method-assign]
        recorder.vision = mcu_unseen
        op = ok(s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})))
        assert op["message"].startswith("finished: 3 placed"), op["message"]
        assert trainings == ["EX-MCU-8"]
        run = client.get("/api/v1/run").json()
        mcu = [i for i in run["plan"]["items"] if i["part_id"] == "EX-MCU-8"]
        assert mcu and all(i["status"] == "skipped" for i in mcu)
        assert all(i["note"].startswith("skipped: training found no way") for i in mcu)
        assert sum(i["discards"] for i in mcu) == 1
        assert run["stats"]["picks"] == 4  # three chips, and the MCU once

        r = s.put(f"/api/v1/run/items/{mcu[0]['index']}", {"skip": False})
        assert r.status_code == 200, r.json()
        item = client.get("/api/v1/run").json()["plan"]["items"][mcu[0]["index"]]
        assert item["status"] == "pending"


def test_run_a_job_on_a_lumenpnp_v4_2() -> None:
    """The v4.2 profile extends v4.1: its bottom camera, datum, and fiducials are near the
    left end of the camera plate, and it has a standard discard tray."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.2"}, presence={"heartbeat_timeout_s": 300.0}
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        assert not any("discard" in i["message"] for i in plan["issues"]), plan["issues"]
        op = ok(s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        # The v4.2's bottom camera sees 70.5 mm across, 0.055 mm per simulated pixel: an
        # 0805 is 36 x 23 pixels. Parts are measured turned to their place angle, not
        # square to the picture, and its pads fit to within about 1 degree there (0.02 mm
        # at their ends).
        assert check_landed(client, client.get("/api/v1/run").json(), turn=1.5) == 4


def test_placements_are_checked_with_the_top_camera(lumen: TestClient) -> None:
    """With the check on, the top camera looks at each two-terminal chip once placed:
    where it found it is kept with the placement."""
    with connect(lumen) as s:
        set_up(lumen, s)
        body = {"job_id": "demo", "skip_blocked": True, "check_placements": True}
        op = ok(s.wait(s.post("/api/v1/run/start", body)))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        items = [i for i in lumen.get("/api/v1/run").json()["plan"]["items"] if i["check"]]
        assert items, "no chip was checked"
        for item in items:
            check = item["check"]
            assert check["ok"], item
            assert math.hypot(check["dx"], check["dy"]) < 0.1 and abs(check["turn"]) < 4
            # Its pads were seen where the board's fit puts them, and it lies on them.
            assert check["on_pads"] >= 0.9, item
            assert math.hypot(check["pads_dx"], check["pads_dy"]) < 0.05, item
            assert check["picture"]
            r = lumen.get(f"/api/v1/run/checks/{item['index']}")
            assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        stats = lumen.get("/api/v1/run").json()["stats"]
        assert stats["placed"] == 4 and stats["checked"] == len(items)
        assert stats["picks"] >= 4 and stats["alignments"] >= 4
        assert stats["empty_picks"] == stats["failed_alignments"] == stats["bad_placements"] == 0


def test_a_part_the_camera_cannot_make_out_is_not_checked(lumen: TestClient) -> None:
    """Gray ceramic capacitors on a real board matched 0.42 to 0.5, under the 0.5 a part
    must match to be judged, and the run paused after each though they sat on their pads.
    A match that poor, but not far off, is marked not checked and the run goes on."""
    from dataclasses import replace as with_score

    with connect(lumen) as s:
        set_up(lumen, s)
        recorder = lumen.app.state.ctx.machine.recorder  # type: ignore[attr-defined]
        vision = recorder.vision

        async def faint(name: str, *args: Any, **kwargs: Any) -> Any:
            found = await vision(name, *args, **kwargs)
            if name == "find_placed_chip" and found is not None:
                return with_score(found, score=0.44)
            return found

        recorder.vision = faint
        body = {"job_id": "demo", "skip_blocked": True, "check_placements": True}
        op = ok(s.wait(s.post("/api/v1/run/start", body)))
        assert op["message"].startswith("finished: 4 placed, 1 blocked, 3 skipped (")
        assert "placements could not be made out to check" in op["message"]
        run = lumen.get("/api/v1/run").json()
        checked = [i for i in run["plan"]["items"] if i["check"]]
        assert checked and all(not i["check"]["checked"] for i in checked)
        assert all(i["check"]["ok"] and i["check"]["score"] == 0.44 for i in checked)
        stats = run["stats"]
        assert stats["unsure"] == len(checked) and stats["bad_placements"] == 0
        assert check_landed(lumen, run) == 4


def test_a_part_that_lands_off_its_target_pauses_the_run() -> None:
    """As a real 0603 did, dragged as the nozzle lifted: it landed 0.45 mm from its
    target. The check finds it off its pads, which are where they should be, and pauses
    the run."""
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_place_shift": {"N1": (0.0, 0.45), "N2": (0.0, 0.45)},
            "check_placements": True,
        },
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        op = s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True}))
        run = client.get("/api/v1/run").json()
        assert run["status"] == "paused", (op, run["message"])
        assert "% of an end on its pad" in run["error"], run["error"]
        assert "turned" not in run["error"] and "seems to have moved" not in run["error"]
        # Out of the way of the operator's tweezers: parked at the back.
        assert run["error"].endswith("The head moved to the back (no park location is set).")
        y = next(a for a in client.app.state.ctx.machine.profile.axes if a.name == "Y")  # type: ignore[attr-defined]
        assert client.get("/api/v1/machine").json()["position"]["Y"] == pytest.approx(y.max)
        checked = [i for i in run["plan"]["items"] if i["check"]]
        check = checked[-1]["check"]
        assert checked and not check["ok"] and check["on_pads"] < 0.75
        assert check["dy"] == pytest.approx(0.45, abs=0.1)
        assert math.hypot(check["pads_dx"], check["pads_dy"]) < 0.05
        assert check["picture"]
        assert run["stats"]["bad_placements"] == 1


def test_a_board_that_moves_mid_run_is_found_again() -> None:
    """As a real board did, knocked by the parts pressed onto its tape: after two parts it
    slides 0.45 mm. The next part lands where the board was; its check finds the pads moved
    and pauses. Once that part is set right by hand, resuming finds the board again, and
    the rest land on their pads."""
    config = fast_config(
        machine={
            "profile": "lumenpnp-v4.1",
            "sim_board_bump": (2, 0.0, 0.45),
            "check_placements": True,
        },
        presence={"heartbeat_timeout_s": 300.0},
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        op = s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True}))
        run = client.get("/api/v1/run").json()
        assert run["status"] == "paused", (op, run["message"])
        assert "the board seems to have moved" in run["error"], run["error"]
        assert "finds the fiducials again" in run["error"]
        checked = [i for i in run["plan"]["items"] if i["check"]]
        assert len(checked) == 3
        last = checked[-1]["check"]
        assert last["pads_dx"] == pytest.approx(0.0, abs=0.05)
        assert last["pads_dy"] == pytest.approx(0.45, abs=0.05)
        before = run["fix"]
        # Set the part that landed where the board was right, as the operator would.
        item = RunItem.model_validate(checked[-1])
        truth = runner(client).sim_target(item)
        sim = client.app.state.ctx.feeders.sim_parts  # type: ignore[attr-defined]
        assert truth is not None and sim is not None
        target, _ = truth
        i = min(
            range(len(sim.placed)),
            key=lambda k: math.hypot(sim.placed[k].x - target.x, sim.placed[k].y - target.y),
        )
        sim.placed[i] = replace(sim.placed[i], x=target.x, y=target.y)
        op = ok(s.wait(s.post("/api/v1/run/resume", {})))
        run = client.get("/api/v1/run").json()
        assert run["status"] == "finished", run["message"]
        assert run["fix"]["dy"] - before["dy"] == pytest.approx(0.45, abs=0.05)
        # All four sit where the board, as it now lies, wants them; the last (not a
        # two-terminal chip, so not checked) was placed after the board was found again.
        assert check_landed(client, run) == 4
        others = [i["check"] for i in run["plan"]["items"] if i["check"] and i["ref"] != item.ref]
        assert len(others) == 2 and all(c["ok"] and c["on_pads"] >= 0.9 for c in others)


def test_parts_are_turned_to_their_place_angle_before_the_bottom_camera_looks(
    lumen: TestClient, tmp_path: Path
) -> None:
    """Each part is measured as it will be placed: the nozzle turns it to its place angle
    before the bottom camera looks, and turns it only the few degrees alignment finds
    afterward. (On a real LumenPnP, two of eight 0603s turned 88 and 180 degrees after
    alignment landed 0.3 to 0.45 mm off.)"""
    import io
    import json
    import zipfile

    with connect(lumen) as s:
        set_up(lumen, s)
        body = {"job_id": "demo", "skip_blocked": True, "record": True}
        ok(s.wait(s.post("/api/v1/run/start", body)))
        rid = lumen.get("/api/v1/run").json()["recording"]
        r = lumen.get(f"/api/v1/recordings/{rid}/download")
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            lines = z.read(f"{rid}/events.jsonl").decode().splitlines()
    axis = {"N1": "A", "N2": "B"}
    seen: dict[str, float] = {}
    aligned: dict[str, float] = {}
    turns: list[float] = []
    for line in lines:
        e = json.loads(line)
        if e["kind"] == "vision" and e.get("fn") == "find_part":
            seen[e["nozzle"]] = e["position"][axis[e["nozzle"]]]
        elif e["kind"] == "align":
            aligned[e["nozzle"]] = e["alignment"]["angle"]
        elif e["kind"] == "place":
            n = e["nozzle"]
            at_place = e["rotation"] - aligned[n]
            turns.append(abs((at_place - seen[n] + 180.0) % 360.0 - 180.0))
    assert len(turns) == 4 and max(turns) < 5.0, turns


def test_a_run_comes_back_after_the_core_restarts() -> None:
    """The core stops after a run's first batch, with a part on a nozzle and another being
    placed. Started again, it shows the run paused and interrupted, and says so; once
    homed, resuming finds the board again and places what is left, recording it as the
    run's start was recorded."""
    config = fast_config(
        machine={"profile": "lumenpnp-v4.1"}, presence={"heartbeat_timeout_s": 300.0}
    )
    with TestClient(create_app(config)) as client, connect(client) as s:
        set_up(client, s)
        r = s.post(
            "/api/v1/run/start",
            {"job_id": "demo", "skip_blocked": True, "step": True, "record": True},
        )
        ok(s.wait(r))
        ok(s.wait(s.post("/api/v1/run/step")))
        assert client.get("/api/v1/run").json()["placed"] == 2
        path = runner(client).run_path
        assert path is not None and path.is_file()

    # As if the core had stopped in the next batch: running, N1 holding one part and N2
    # placing another.
    kept = json.loads(path.read_text())
    pending = [i for i in kept["plan"]["items"] if i["status"] == "pending"]
    kept["status"] = "running"
    kept["held"] = {"N1": pending[0]["index"]}
    kept["current"] = {
        "stage": "item",
        "action": "placing",
        "index": pending[1]["index"],
        "nozzle": "N2",
    }
    path.write_text(json.dumps(kept))

    with TestClient(create_app(config)) as client, connect(client) as s:
        run = client.get("/api/v1/run").json()
        assert run["status"] == "paused" and run["interrupted"] and run["placed"] == 2
        assert run["fix"] is None and run["held"] == {} and run["current"] is None
        assert run["error"] == (
            f"the core stopped in the middle of this run; the nozzles held parts "
            f"({pending[0]['ref']} on N1); it stopped while placing {pending[1]['ref']}: if "
            "that part is on the board, skip its placement. Take any parts off the nozzles "
            "by hand, home the machine, then resume: the run finds the board's fiducials "
            "again and places what is left."
        )
        r = s.post("/api/v1/run/resume")
        assert r.status_code == 409, r.json()  # not homed yet
        assert client.get("/api/v1/run").json()["interrupted"]

        s.home()
        assert client.get("/api/v1/run").json()["recording"] is None
        op = ok(s.wait(s.post("/api/v1/run/resume")))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        run = client.get("/api/v1/run").json()
        assert not run["interrupted"] and run["fix"] is not None
        # It was recorded before the restart, so its rest is recorded too.
        assert run["recording"] is not None
        recordings = client.get("/api/v1/recordings").json()
        listed = recordings if isinstance(recordings, list) else recordings["recordings"]
        mine = next(x for x in listed if x["id"] == run["recording"])
        assert mine["label"].endswith("(resumed)"), mine
        # This simulator saw only the parts placed since the restart: they are where the
        # board wants them.
        before = {i["index"] for i in kept["plan"]["items"] if i["status"] == "placed"}
        for i in run["plan"]["items"]:
            if i["index"] in before:
                i["status"] = "placed before"
        assert check_landed(client, run) == 2


def test_parts_go_to_the_nozzles_whose_tips_suit_them(lumen: TestClient) -> None:
    """With an N045 tip (0.45 mm hole) on the left nozzle and an N08 (0.8 mm) on the
    right, the 0603 resistors and the 0805 can go on either (both holes fit inside them),
    and the SOIC only on the right one (the N045's hole is too small to hold it well). The
    plan says so, and the run places them all."""
    from tests.test_nozzle_tips import fit

    with connect(lumen) as s:
        set_up(lumen, s)
        fit(s, "N1", "N045")
        fit(s, "N2", "N08")
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        nozzles = {i["ref"]: i["nozzles"] for i in plan["items"] if i["status"] == "pending"}
        both = ["N1", "N2"]
        assert nozzles == {"R1": both, "R2": both, "C1": both, "U2": ["N2"]}, nozzles
        tips = [i["message"] for i in plan["issues"] if "by the tips fitted" in i["message"]]
        assert tips and "1 only on the right nozzle (N08)" in tips[0], plan["issues"]
        assert not [i for i in plan["issues"] if "only some of the nozzles" in i["message"]]

        op = ok(s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})))
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        run = lumen.get("/api/v1/run").json()
        by = {i["ref"]: i["nozzle"] for i in run["plan"]["items"] if i["status"] == "placed"}
        assert by["U2"] == "N2" and set(by.values()) == {"N1", "N2"}, by
        assert check_landed(lumen, run) == 4


def test_a_tip_that_never_picks_a_part_hands_it_to_another_nozzle(lumen: TestClient) -> None:
    """The N08 on the right nozzle lifts nothing from the resistors' slot, as a real N08
    did on 0603 capacitors. After its second empty pick the run gives that placement to the
    left nozzle, whose N045 picks the part, without pausing; and the next plan leaves the
    resistors to the left nozzle alone."""
    from picnicplace.placement import MispickError
    from tests.test_nozzle_tips import fit

    with connect(lumen) as s:
        set_up(lumen, s)
        fit(s, "N1", "N045")
        fit(s, "N2", "N08")
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        resistors = {i["slot_id"] for i in plan["items"] if i["ref"] in ("R1", "R2")}
        assert len(resistors) == 1
        placement = lumen.app.state.ctx.placement  # type: ignore[attr-defined]
        pick = placement.pick

        feeders = lumen.app.state.ctx.feeders  # type: ignore[attr-defined]

        async def n08_lifts_nothing(
            slot_id: str, nozzle: Any, feed: bool, *args: Any, **kw: Any
        ) -> Any:
            if nozzle.id == "N2" and slot_id in resistors:
                if feed:  # the tape moves on as for any pick; the part stays in its pocket
                    await feeders.feed(feeders.slot(slot_id))
                raise MispickError(f"{nozzle.id} picked nothing from slot {slot_id}")
            return await pick(slot_id, nozzle, feed, *args, **kw)

        placement.pick = n08_lifts_nothing
        op = ok(s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True})))
        assert op["message"].startswith("finished: 4 placed, 1 blocked, 3 skipped")
        run = lumen.get("/api/v1/run").json()
        by = {i["ref"]: i["nozzle"] for i in run["plan"]["items"] if i["status"] == "placed"}
        assert by["R1"] == "N1" and by["R2"] == "N1", by
        assert run["stats"]["empty_picks"] == 2
        assert check_landed(lumen, run) == 4

        part = next(i["part_id"] for i in plan["items"] if i["ref"] == "R1")
        assert placement.tips.barred(part, "N08") and not placement.tips.barred(part, "N045")
        again = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        nozzles = {i["ref"]: i["nozzles"] for i in again["items"] if i["status"] == "pending"}
        assert nozzles["R1"] == ["N1"] and nozzles["R2"] == ["N1"], nozzles


def test_tip_nozzles_by_part_size() -> None:
    """The rule itself: the hole no wider than the part's narrower side and no narrower
    than 35% of it, else the largest that is not too wide; less a tip barred for the part,
    unless that leaves none; with no tip known, or none that suits, any nozzle."""
    from picnicplace.job.run import part_width, tip_nozzles
    from picnicplace.library.models import Footprint, Pad, Size

    profile = load_profile("lumenpnp-v4.1")
    fitted = profile.model_copy(
        update={
            "nozzles": [
                profile.nozzles[0].model_copy(update={"tip": "N045"}),
                profile.nozzles[1].model_copy(update={"tip": "N08"}),
            ]
        }
    )

    class Library:
        def __init__(self, footprint: Footprint) -> None:
            self.footprint = footprint

        def find_part(self, part_id: str) -> Part | None:
            return None

        def find_footprint(self, footprint_id: str) -> Footprint | None:
            return self.footprint

        def tape_of(self, part: Part | None) -> None:
            return None

    def chip(width: float) -> Footprint:
        return Footprint(id="X", body=Size(x=2 * width, y=width))

    qfn = Footprint(
        id="QFN",
        pads=[
            Pad(name=str(n), x=x, y=y, width=0.3, height=0.8)
            for n, (x, y) in enumerate([(-1.45, 0), (1.45, 0), (0, -1.45), (0, 1.45)])
        ],
    )
    assert part_width(qfn) == pytest.approx(3.2)  # the pads' extent
    part = Part(id="P", footprint_id="X")
    both = ["N1", "N2"]
    assert tip_nozzles(both, part, Library(chip(0.8)), fitted) == both  # 0603
    assert tip_nozzles(both, part, Library(chip(1.25)), fitted) == both  # 0805
    assert tip_nozzles(both, part, Library(chip(1.6)), fitted) == ["N2"]  # 1206
    assert tip_nozzles(both, part, Library(qfn), fitted) == ["N2"]  # the largest that fits
    assert tip_nozzles(both, part, Library(chip(0.5)), fitted) == ["N1"]  # 0402
    assert tip_nozzles(both, part, Library(chip(0.3)), fitted) == both  # 01005: none suits
    assert tip_nozzles(both, part, Library(chip(0.8)), profile) == both  # no tips known
    n08 = lambda part_id, tip_id: tip_id == "N08"  # noqa: E731
    assert tip_nozzles(both, part, Library(chip(0.8)), fitted, n08) == ["N1"]
    assert tip_nozzles(both, part, Library(chip(1.6)), fitted, n08) == ["N2"]  # none left
