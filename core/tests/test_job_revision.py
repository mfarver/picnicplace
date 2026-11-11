"""Job revisions: what they hold, their file and hash, the store, and the endpoints."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.board.kicad import read_kicad_pcb
from picnicplace.job.models import Job, JobSetup, MachineLocation
from picnicplace.job.revision import (
    JobFileError,
    JobRevision,
    design_hash,
    dump,
    load,
    make_revision,
)
from picnicplace.job.store import JobStore, RevisionConflict
from picnicplace.library import Library, Part
from tests.conftest import fast_config, put_taped

DATA = Path(__file__).parent / "data" / "kicad"
WHEN = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def demo() -> tuple[Job, Library]:
    """The demo board's job, and a library with its footprints and two of its parts."""
    result = read_kicad_pcb((DATA / "picnic-demo.kicad_pcb").read_text(), "picnic-demo")
    library = Library(None)
    for footprint in result.footprints:
        library.put_footprint(footprint)
    put_taped(
        library,
        Part(id="EX-R0603-10K", footprint_id="R_0603_1608Metric"),
        "paper",
        8,
        4,
        90,
    )
    library.put_part(Part(id="EX-MCU-8", footprint_id="SOIC-8_3.9x4.9mm_P1.27mm"))
    library.put_part(Part(id="NOT-ON-THE-BOARD", footprint_id="R_0603_1608Metric"))
    return Job.for_board(result.board), library


def revision(number: int = 1, reason: str = "First") -> JobRevision:
    job, library = demo()
    return make_revision(
        "picnic-demo", job, library, number=number, author=" Pat ", reason=reason, now=WHEN
    )


def test_a_revision_holds_what_the_job_uses() -> None:
    rev = revision()
    design = rev.design
    assert [p.id for p in design.library.parts] == ["EX-MCU-8", "EX-R0603-10K"]
    assert [t.id for t in design.library.tapes] == ["8mm-paper-p4-r90"]
    used = {p.footprint_id for p in design.board.placements}
    assert [f.id for f in design.library.footprints] == sorted(used)
    # Parts the board names that the library lacks are listed, not guessed.
    assert "EX-SWITCH-1" in design.missing and "EX-MCU-8" not in design.missing
    assert design.missing == sorted(design.missing)
    assert rev.revision.author == "Pat" and rev.revision.number == 1


def test_a_revision_holds_nothing_of_one_machine() -> None:
    job, library = demo()
    placed = job.model_copy(update={"location": MachineLocation(x=10, y=20), "board_z": -27.0})
    a = make_revision("j", job, library, number=1, author="a", reason="", now=WHEN)
    b = make_revision("j", placed, library, number=1, author="a", reason="", now=WHEN)
    assert a.revision.hash == b.revision.hash
    assert "location" not in dump(a) and "board_z" not in dump(a)


def test_the_hash_covers_the_design_only() -> None:
    a = revision(reason="One reason")
    b = revision(number=2, reason="Another")
    assert a.revision.hash == b.revision.hash == design_hash(a.design)
    turned = a.design.model_copy(update={"side_up": "bottom"})
    assert design_hash(turned) != a.revision.hash


def test_the_file_reads_back_and_catches_changes() -> None:
    rev = revision()
    text = dump(rev)
    assert text.endswith("}\n") and load(text) == rev
    assert dump(load(text)) == text  # written the same way every time

    changed = text.replace('"EX-MCU-8"', '"EX-MCU-9"', 1)
    with pytest.raises(JobFileError, match="changed after it was saved"):
        load(changed)
    with pytest.raises(JobFileError, match="not JSON"):
        load("{")
    with pytest.raises(JobFileError, match="not a PicnicPlace job file"):
        load('{"format": "something-else"}')
    with pytest.raises(JobFileError, match="saved before revisions"):
        load('{"format": "picnicplace-job", "version": 1}')
    with pytest.raises(JobFileError, match="version 3"):
        load('{"format": "picnicplace-job", "version": 3}')


def test_the_store_keeps_revisions_once(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    first = revision()
    assert store.next_number("picnic-demo") == 1
    assert store.add_revision(first)
    assert not store.add_revision(first)  # the same one again changes nothing
    design = first.design.model_copy(update={"side_up": "bottom"})
    other = first.model_copy(
        update={
            "design": design,
            "revision": first.revision.model_copy(update={"hash": design_hash(design)}),
        }
    )
    with pytest.raises(RevisionConflict):
        store.add_revision(other)
    assert store.revision("picnic-demo", 1) == first
    assert store.revision_text("picnic-demo", 1) == dump(first)
    assert store.next_number("picnic-demo") == 2

    # A job with revisions but no working copy (one loaded from a file) is listed.
    [summary] = store.list()
    assert summary.id == "picnic-demo" and not summary.draft
    assert [r.number for r in summary.revisions] == [1]

    # Deleting the working copy keeps the revisions.
    store.put("picnic-demo", demo()[0])
    assert store.list()[0].draft
    assert store.delete("picnic-demo")
    assert [r.number for r in store.list()[0].revisions] == [1]


def test_revision_endpoints(client: TestClient) -> None:
    imported = client.post(
        "/api/v1/boards/import/kicad",
        json={
            "kicad_pcb": (DATA / "picnic-demo.kicad_pcb").read_text(),
            "name": "picnic-demo",
            "add_to_library": True,
        },
    ).json()
    job = imported["job"]
    job["location"] = {"x": 100, "y": 60, "rotation": 0}
    assert client.put("/api/v1/jobs/picnic-demo", json=job).status_code == 200

    url = "/api/v1/jobs/picnic-demo/revisions"
    assert client.get(url).json() == []
    r = client.post(url, json={"author": "Pat", "reason": "First run"})
    assert r.status_code == 200, r.json()
    published = r.json()
    assert published["revision"]["number"] == 1
    assert {f["id"] for f in published["design"]["library"]["footprints"]} >= {"R_0603_1608Metric"}
    assert client.post(url, json={"author": "Pat"}).json()["revision"]["number"] == 2
    assert client.post(url, json={"author": ""}).status_code == 422
    assert client.post("/api/v1/jobs/none/revisions", json={"author": "Pat"}).status_code == 404
    assert [r["number"] for r in client.get(url).json()] == [1, 2]
    [summary] = client.get("/api/v1/jobs").json()
    assert summary["draft"] and len(summary["revisions"]) == 2

    file = client.get(f"{url}/1/file")
    assert file.status_code == 200
    assert 'filename="picnic-demo-r0001.json"' in file.headers["content-disposition"]
    assert load(file.text).revision.number == 1
    assert client.get(f"{url}/9").status_code == 404

    # Another machine, with no Creator, loads the file from disk.
    with TestClient(create_app(fast_config())) as other:
        load_url = "/api/v1/jobs/import"
        r = other.post(load_url, json={"text": file.text})
        assert r.json() == {
            "job_id": "picnic-demo",
            "kind": "revision",
            "number": 1,
            "already_kept": False,
        }
        assert other.post(load_url, json={"text": file.text}).json()["already_kept"]
        [loaded] = other.get("/api/v1/jobs").json()
        assert not loaded["draft"] and loaded["revisions"][0]["author"] == "Pat"

        tampered = file.text.replace('"top"', '"bottom"', 1)
        r = other.post(load_url, json={"text": tampered})
        assert r.status_code == 400 and "changed after it was saved" in r.json()["detail"]
        assert other.post(load_url, json={"text": "nonsense"}).status_code == 400

        # A job file saved before revisions becomes a working copy to publish.
        old = json.dumps(job | {"name": "Old job"})
        r = other.post(load_url, json={"text": old})
        assert r.json()["kind"] == "working-copy" and r.json()["job_id"] == "Old-job"
        assert other.post(load_url, json={"text": old}).status_code == 409


def test_the_machine_keeps_where_a_job_sits(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs", tmp_path / "setups")
    job, _ = demo()
    at = MachineLocation(x=10, y=20, rotation=90)
    store.put("picnic-demo", job.model_copy(update={"location": at, "board_z": -27.0}))
    # The working copy holds the design only; the setup is the machine's.
    saved = json.loads((tmp_path / "jobs" / "picnic-demo.json").read_text())
    assert "location" not in saved and "board_z" not in saved
    assert store.setup("picnic-demo") == JobSetup(location=at, board_z=-27.0)
    got = store.get("picnic-demo")
    assert got is not None and got.location == at and got.board_z == -27.0
    assert store.list()[0].placed

    # A working copy saved before setups were kept apart still has its place.
    old = job.model_copy(update={"location": at}).model_dump_json()
    (tmp_path / "jobs" / "older.json").write_text(old)
    assert store.setup("older").location == at

    # What runs: the newest revision, set up as the machine keeps it.
    assert store.resolve("picnic-demo")[1] is None
    store.add_revision(revision())
    run, published = store.resolve("picnic-demo")
    assert published is not None and published.revision.number == 1
    assert run.location == at and run.board_z == -27.0
    with pytest.raises(LookupError, match="no revision 2"):
        store.resolve("picnic-demo", 2)
    with pytest.raises(LookupError, match="no job"):
        store.resolve("nothing")

    # The setup outlives the working copy.
    store.delete("picnic-demo")
    assert store.resolve("picnic-demo")[0].location == at


def test_a_job_from_a_file_is_set_up_and_planned(client: TestClient) -> None:
    job, library = demo()
    rev = make_revision("picnic-demo", job, library, number=1, author="Pat", reason="", now=WHEN)
    assert client.post("/api/v1/jobs/import", json={"text": dump(rev)}).status_code == 200
    assert client.get("/api/v1/jobs/picnic-demo").status_code == 404  # no working copy

    url = "/api/v1/jobs/picnic-demo/setup"
    assert client.get(url).json() == {"location": None, "board_z": None}
    at = client.post("/api/v1/jobs/find-location", json=job.model_dump(mode="json")).json()
    assert client.put(url, json={"location": at}).status_code == 200
    assert client.get(url).json()["location"] == at
    assert client.get("/api/v1/jobs/none/setup").status_code == 404
    assert client.get("/api/v1/jobs").json()[0]["placed"]

    layout = client.get("/api/v1/jobs/picnic-demo/layout").json()
    assert layout["placements"] and layout["outline"]
    assert client.get("/api/v1/jobs/picnic-demo/layout?revision=2").status_code == 404

    # The plan uses the revision's copy of the library, not this machine's (empty) one.
    plan = client.post("/api/v1/run/plan", json={"job_id": "picnic-demo"}).json()
    assert plan["revision"] == 1
    notes = {i["ref"]: i["note"] for i in plan["items"]}
    assert "not in the parts library" not in (notes.get("U2") or "")
    assert "not in the parts library" in (notes.get("U1") or "")  # missing from the revision


def test_a_job_not_yet_published_is_planned_but_not_run(client: TestClient) -> None:
    job, _ = demo()
    assert client.put("/api/v1/jobs/draft", json=job.model_dump(mode="json")).status_code == 200
    plan = client.post("/api/v1/run/plan", json={"job_id": "draft"}).json()
    assert plan["revision"] is None and not plan["runnable"]
    assert plan["issues"][0]["message"].startswith("the job is not published yet")
