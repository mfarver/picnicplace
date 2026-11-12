"""Running a whole job on the simulated LumenPnP.

The demo board's import leaves R3, R4, and J1 out (do-not-populate, no part number,
through-hole); here U1's part also has no height, so it is blocked.

The simulator puts the job's board on the bed a little off from where the job says
(``sim_board_error``), so the run must find it by its fiducials. Each test checks where
the parts really landed against where the simulated board really wants them.
"""

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.board.kicad import read_kicad_pcb
from picnicplace.job.models import Job
from picnicplace.job.run import JobRunner, RunItem, fit_board, machine_angle
from picnicplace.library import Part
from picnicplace.machine.profile import Point, load_profile
from picnicplace.simparts import PlacedPart
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


def set_up(client: TestClient, s: Session, calibrate: bool = True) -> None:
    """The demo board as a saved job, with its parts in the library and on feeders (with
    their slots calibrated, unless not)."""
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
            "embossed",
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


def check_landed(client: TestClient, run: dict[str, Any]) -> int:
    """Every placed item's part is where the simulated board wants it."""
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
        assert angle_error(part.angle, angle, period) < 0.5, (item.ref, part.angle, angle)
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
    """The second batch is C1 (slot 1) and U2 (slot 5). With slot 5 jammed, C1 is
    already on its nozzle when the run pauses; after the jam is cleared, resuming
    places both."""
    with connect(lumen) as s:
        set_up(lumen, s)
        sim = lumen.app.state.ctx.feeders._sim  # type: ignore[attr-defined]
        sim.feeders[5].jammed = True
        op = s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True}))
        assert op["status"] == "failed" and "could not reach the next part" in op["error"]
        run = lumen.get("/api/v1/run").json()
        assert run["status"] == "paused" and run["error"] == op["error"]
        assert run["placed"] == 2
        c1 = next(i["index"] for i in run["plan"]["items"] if i["ref"] == "C1")
        u2 = next(i["index"] for i in run["plan"]["items"] if i["ref"] == "U2")
        # It shows where it stopped: picking U2, with C1 waiting on its nozzle.
        current = run["current"]
        assert (current["stage"], current["action"], current["index"]) == ("item", "picking", u2)
        assert current["nozzle"] not in run["held"]
        assert list(run["held"].values()) == [c1]
        loads = lumen.get("/api/v1/placement").json()["loads"]
        assert [load["part_id"] for load in loads] == ["EX-C0805-1U"]

        # A part on a nozzle cannot be skipped.
        r = s.put(f"/api/v1/run/items/{c1}", {"skip": True})
        assert r.status_code == 409 and "on a nozzle" in r.json()["detail"]

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
        blind = {"left": 1}

        async def fails_once(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "find_part" and blind["left"] > 0:
                blind["left"] -= 1
                return None
            return await vision(name, *args, **kwargs)

        recorder.vision = fails_once
        op = s.wait(s.post("/api/v1/run/start", {"job_id": "demo", "skip_blocked": True}))
        assert op["status"] == "failed"
        assert "saw no part on N1, and there is no discard location" in op["error"]
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
        assert op["message"] == "finished: 4 placed, 1 blocked, 3 skipped"
        assert check_landed(lumen, lumen.get("/api/v1/run").json()) == 4


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
        assert check_landed(client, client.get("/api/v1/run").json()) == 4
