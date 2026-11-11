"""Tapes as records of their own, which parts link to, and pin 1 on footprints."""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from picnicplace.board.kicad import read_kicad_pcb
from picnicplace.library import Footprint, Library, LibraryDocument, LibraryError, Pad, Part
from picnicplace.library.models import (
    QUADRANT_ROTATION,
    pin1_quadrant,
    standard_tape,
    with_pin1,
)
from tests.conftest import connect
from tests.test_run import DATA, set_up
from tests.test_run import lumen as lumen_run  # noqa: F401 (fixture)

R0603 = Footprint(
    id="R0603",
    pads=[
        Pad(name="1", x=-0.8, y=0, width=0.8, height=0.95),
        Pad(name="2", x=0.8, y=0, width=0.8, height=0.95),
    ],
)


def old_library(path: Path) -> None:
    """A library as schema 1 stored it: each part's tape inside the part."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    db.execute("CREATE TABLE footprints (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    db.execute(
        "CREATE TABLE parts (id TEXT PRIMARY KEY, data TEXT NOT NULL,"
        " footprint_id TEXT NOT NULL REFERENCES footprints (id))"
    )
    db.execute("INSERT INTO footprints VALUES ('R0603', ?)", (R0603.model_dump_json(),))
    paper = {"kind": "paper", "width": 8, "pitch": 4, "rotation": 90, "pin1_quadrant": None}
    parts = {
        "R1K": {"tape": {**paper, "basis": "datasheet", "source": "reel label"}},
        "R10K": {"tape": paper},
        "C1U": {"tape": {**paper, "kind": "embossed", "rotation": 0}},
        "BARE": {},
    }
    for part_id, extra in parts.items():
        data = {"id": part_id, "footprint_id": "R0603", **extra}
        db.execute("INSERT INTO parts VALUES (?, ?, 'R0603')", (part_id, json.dumps(data)))
    db.commit()
    db.close()


def test_an_older_library_moves_tapes_into_shared_records(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    old_library(path)
    library = Library(path)
    tapes = {t.id: t for t in library.tapes()}
    assert set(tapes) == {"8mm-paper-p4-r90", "8mm-embossed-p4-r0"}
    paper = tapes["8mm-paper-p4-r90"]
    assert (paper.rotation, paper.basis, paper.source) == (90, "datasheet", "reel label")
    assert paper.name == "8 mm paper, 4 mm pitch, 90°"
    parts = {p.id: p.tape_id for p in library.parts()}
    assert parts == {
        "R1K": "8mm-paper-p4-r90",
        "R10K": "8mm-paper-p4-r90",
        "C1U": "8mm-embossed-p4-r0",
        "BARE": None,
    }
    assert library.parts_on_tape("8mm-paper-p4-r90") == ["R10K", "R1K"]
    library.close()
    version = sqlite3.connect(path).execute("SELECT value FROM meta").fetchone()[0]
    assert version == "2"


def test_an_older_export_file_is_read_the_same_way() -> None:
    old = {
        "format": "picnicplace-library",
        "version": 1,
        "footprints": [R0603.model_dump(mode="json")],
        "parts": [
            {"id": "A", "footprint_id": "R0603", "tape": {"width": 8, "pitch": 4}},
            {"id": "B", "footprint_id": "R0603", "tape": {"width": 8, "pitch": 4}},
        ],
    }
    document = LibraryDocument.model_validate(old)
    assert [t.id for t in document.tapes] == ["8mm-unknown-p4-r0"]
    assert [p.tape_id for p in document.parts] == ["8mm-unknown-p4-r0"] * 2
    library = Library(None)
    report = library.import_document(document)
    assert report.errors == [] and "tape:8mm-unknown-p4-r0" in report.added


def test_parts_link_to_tapes_that_exist() -> None:
    library = Library(None)
    library.put_footprint(R0603)
    with pytest.raises(LibraryError, match="no tape 'nope'"):
        library.put_part(Part(id="X", footprint_id="R0603", tape_id="nope"))
    tape = standard_tape("paper", 8, 4, None)
    assert tape.id == "8mm-paper-p4-rna" and tape.rotation is None
    library.put_tape(tape)
    library.put_part(Part(id="X", footprint_id="R0603", tape_id=tape.id))
    with pytest.raises(LibraryError, match="used by 1 part"):
        library.delete_tape(tape.id)
    library.delete_part("X")
    library.delete_tape(tape.id)
    assert library.tapes() == []
    # A part with no tape stores nothing more than it did before tapes were records.
    bare = Part(id="Y", footprint_id="R0603")
    library.put_part(bare)
    assert library.part("Y") == bare and library.tape_of(bare) is None


def pin1_top_left() -> Footprint:
    """A SOT-23-ish footprint with pin 1 at the top left at 0 degrees."""
    return Footprint(
        id="IC",
        pads=[
            Pad(name="1", x=-1.0, y=1.0, width=0.6, height=0.6, pin1=True),
            Pad(name="2", x=1.0, y=1.0, width=0.6, height=0.6),
            Pad(name="3", x=0.0, y=-1.0, width=0.6, height=0.6),
        ],
    )


def test_the_quadrant_table_matches_the_rotation() -> None:
    """EIA-481 quadrants for a part with pin 1 at the top left at 0 degrees: Q1 = 0,
    Q2 = -90, Q3 = +90, Q4 = 180."""
    footprint = pin1_top_left()
    for quadrant, rotation in QUADRANT_ROTATION.items():
        assert pin1_quadrant(footprint, rotation) == quadrant
    # No mark, or pin 1 on a center line (a diode's cathode): the quadrant does not say.
    assert pin1_quadrant(R0603, 0) is None
    diode = R0603.model_copy(update={"pads": with_pin1(R0603.pads)})
    assert diode.pads[0].pin1 and pin1_quadrant(diode, 0) is None


def test_imports_suggest_pin_1() -> None:
    bga = [Pad(name="B1", x=0, y=0, width=1, height=1), Pad(name="A1", x=1, y=1, width=1, height=1)]
    assert [p.pin1 for p in with_pin1(bga)] == [False, True]
    chosen = [
        Pad(name="1", x=0, y=0, width=1, height=1),
        Pad(name="2", x=1, y=0, width=1, height=1, pin1=True),
    ]
    assert with_pin1(chosen) == chosen  # the owner's choice stands
    result = read_kicad_pcb((DATA / "picnic-demo.kicad_pcb").read_text(), "picnic-demo")
    for f in result.footprints:
        named_1 = any(p.name == "1" for p in f.pads)
        assert [p.name for p in f.pads if p.pin1] == (["1"] if named_1 else []), f.id


def test_a_run_warns_when_the_rotation_in_tape_is_not_known(
    lumen_run: TestClient,  # noqa: F811
) -> None:
    client = lumen_run
    with connect(client) as s:
        set_up(client, s)
        library = client.app.state.ctx.library  # type: ignore[attr-defined]
        part = library.part("EX-R0603-10K")
        tape = library.tape_of(part)
        library.put_tape(tape.model_copy(update={"rotation": None}))
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        warnings = [i["message"] for i in plan["issues"] if i["severity"] == "warning"]
        assert any("rotation in tape is not known for EX-R0603-10K" in w for w in warnings)
        # Every part on that tape changed with it.
        assert all(
            library.tape_of(library.part(p)).rotation is None
            for p in library.parts_on_tape(tape.id)
        )


def test_a_feeder_and_its_parts_tape_of_another_width_are_flagged(
    lumen_run: TestClient,  # noqa: F811
) -> None:
    """The demo's resistors come in 8 mm tape. A feeder that takes 12 mm tape holding
    them means the part is in the wrong feeder, or has the wrong tape."""
    client = lumen_run
    with connect(client) as s:
        set_up(client, s)
        slot = next(
            x for x in client.get("/api/v1/feeders").json()["slots"] if x["slot"]["id"] == "2"
        )
        uuid = slot["feeder"]["uuid"]
        body = {**slot["feeder"]["assignment"], "tape_width": 12}
        assert s.put(f"/api/v1/feeders/assignments/{uuid}", body).status_code == 200
        slot = next(
            x for x in client.get("/api/v1/feeders").json()["slots"] if x["slot"]["id"] == "2"
        )
        assert "takes 12 mm tape, but EX-R0603-10K comes in 8 mm tape" in slot["warning"]
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        assert any("takes 12 mm tape" in i["message"] for i in plan["issues"])

        # The right width: nothing to say.
        assert (
            s.put(f"/api/v1/feeders/assignments/{uuid}", {**body, "tape_width": 8}).status_code
            == 200
        )
        slot = next(
            x for x in client.get("/api/v1/feeders").json()["slots"] if x["slot"]["id"] == "2"
        )
        assert slot["warning"] is None
