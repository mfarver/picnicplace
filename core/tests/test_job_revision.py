"""Job revisions: what they hold, their file and hash, the store, and the endpoints."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from picnicplace.api import create_app
from picnicplace.board.kicad import read_kicad_pcb
from picnicplace.job.models import Job, MachineLocation
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
