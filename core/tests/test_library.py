"""The parts library: storage, merging imports, OpenPnP files, and the endpoints."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from picnicplace.library import Footprint, Library, LibraryDocument, LibraryError, Pad, Part, Tape
from picnicplace.library.models import Size
from picnicplace.library.openpnp import OpenPnpFormatError, read_library
from picnicplace.library.tape import geometry

R0603 = Footprint(
    id="R_0603_1608Metric",
    body=Size(x=1.6, y=0.8),
    body_basis="estimated",
    pads=[
        Pad(name="1", x=-0.825, y=0, width=0.8, height=0.95, roundness=25),
        Pad(name="2", x=0.825, y=0, width=0.8, height=0.95, roundness=25),
    ],
)
R10K = Part(
    id="EX-R0603-10K",
    name="10k",
    footprint_id=R0603.id,
    height=0.45,
    height_basis="datasheet",
    tape=Tape(kind="paper", width=8, pitch=4, rotation=90, basis="standard"),
)

# A made-up library in OpenPnP's format, shaped like a KiCad export.
PACKAGES_XML = """<?xml version='1.0' encoding='UTF-8'?>
<openpnp-packages>
   <package version="1.1" id="SOT-23-6" description="SOT-23-6">
      <footprint units="Millimeters" body-width="1.6" body-height="2.9">
         <pad name="1" x="-1.1375" y="0.95" width="1.325" height="0.6"
              rotation="0.0" roundness="50.0" />
         <pad name="6" x="1.1375" y="0.95" width="1.325" height="0.6"
              rotation="0.0" roundness="50.0" />
      </footprint>
      <compatible-nozzle-tip-ids />
   </package>
   <package version="1.1" id="QFN-16" description="16-pin QFN">
      <footprint units="Inches" body-width="0.118" body-height="0.118" />
   </package>
   <package version="1.1" id="Bare" />
</openpnp-packages>
"""
PARTS_XML = """<?xml version='1.0' encoding='UTF-8'?>
<openpnp-parts>
   <part id="EX-SWITCH-1" name="Load switch" height-units="Millimeters" height="1.45"
         package-id="SOT-23-6" speed="0.5" tape-rotation="90.0" tape-pitch="4" />
   <part id="EX-QFN-1" name="EX-QFN-1" height-units="Millimeters" height="0.0"
         package-id="QFN-16" speed="1.0" />
   <part id="EX-ORPHAN" height="1" package-id="Missing" />
</openpnp-parts>
"""


@pytest.fixture
def library() -> Library:
    lib = Library(None)
    lib.put_footprint(R0603)
    lib.put_part(R10K)
    return lib


def test_store_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    lib = Library(path)
    lib.put_footprint(R0603)
    lib.put_part(R10K)
    lib.close()
    reopened = Library(path)
    assert reopened.footprint(R0603.id) == R0603
    assert reopened.parts() == [R10K]
    assert (
        reopened.export().model_dump()
        == LibraryDocument(footprints=[R0603], parts=[R10K]).model_dump()
    )


def test_parts_need_their_footprint(library: Library) -> None:
    with pytest.raises(LibraryError, match="no footprint 'SOT-23'"):
        library.put_part(Part(id="X", footprint_id="SOT-23"))
    with pytest.raises(LibraryError, match="used by 1 part"):
        library.delete_footprint(R0603.id)
    library.delete_part(R10K.id)
    library.delete_footprint(R0603.id)
    assert library.footprints() == []


def test_import_merges_without_losing_machine_values(library: Library) -> None:
    # The machine's copy was tuned: a measured height and an observed tape rotation.
    library.put_part(
        R10K.model_copy(
            update={
                "height": 0.5,
                "height_basis": "measured",
                "tape": R10K.tape.model_copy(update={"rotation": -90}) if R10K.tape else None,
                "manufacturer": "",
            }
        )
    )
    incoming = R10K.model_copy(update={"manufacturer": "Example Corp"})
    report = library.import_document(
        LibraryDocument(
            footprints=[R0603], parts=[incoming, Part(id="EX-NEW", footprint_id=R0603.id)]
        )
    )
    assert report.added == ["part:EX-NEW"]
    assert report.unchanged == 1  # the footprint
    assert [(f.id, f.fields) for f in report.filled] == [("EX-R0603-10K", ["manufacturer"])]
    conflicts = {c.field: (c.current, c.incoming) for c in report.conflicts}
    assert conflicts == {"height": (0.5, 0.45), "tape.rotation": (-90.0, 90.0)}
    kept = library.part(R10K.id)
    # The conflicting height's basis is not taken from the import either.
    assert (kept.height, kept.height_basis, kept.manufacturer) == (0.5, "measured", "Example Corp")
    assert kept.tape is not None and kept.tape.rotation == -90

    again = library.import_document(LibraryDocument(parts=[incoming]), overwrite=True)
    assert again.replaced == ["part:EX-R0603-10K"]
    assert library.part(R10K.id).height == 0.45


def test_import_reports_missing_footprints(library: Library) -> None:
    report = library.import_document(LibraryDocument(parts=[Part(id="X", footprint_id="Nope")]))
    assert report.errors == ["part 'X': no footprint 'Nope' in the library"]
    assert report.added == []


def test_openpnp_files() -> None:
    document = read_library(PACKAGES_XML, PARTS_XML)
    sot, qfn, bare = document.footprints
    assert sot.id == "SOT-23-6" and sot.description == ""
    assert sot.body == Size(x=1.6, y=2.9) and sot.body_basis == "estimated"
    assert [p.name for p in sot.pads] == ["1", "6"]
    assert sot.pads[0].x == pytest.approx(-1.1375) and sot.pads[0].roundness == 50
    assert qfn.body is not None and qfn.body.x == pytest.approx(2.9972)
    assert qfn.description == "16-pin QFN"
    assert bare.body is None and bare.pads == []

    switch, qfn_part, orphan = document.parts
    assert switch.name == "Load switch" and switch.speed == 0.5 and switch.height == 1.45
    assert switch.tape == Tape(
        rotation=90, pitch=4, source="OpenPnP parts.xml (tape-orientation build)"
    )
    assert qfn_part.name == "" and qfn_part.height is None and qfn_part.tape is None
    assert orphan.footprint_id == "Missing"

    lib = Library(None)
    report = lib.import_document(document)
    assert len(report.added) == 5 and report.errors == [
        "part 'EX-ORPHAN': no footprint 'Missing' in the library"
    ]


def test_openpnp_rejects_other_files() -> None:
    with pytest.raises(OpenPnpFormatError, match="expected <openpnp-parts>"):
        read_library(None, PACKAGES_XML)
    with pytest.raises(OpenPnpFormatError, match="not valid XML"):
        read_library("<openpnp-packages>", None)


def test_tape_geometry() -> None:
    eight = geometry(8, 4)
    assert eight.pocket_y == pytest.approx(-5.25)  # 1.75 + 3.5 from the top edge
    assert eight.pocket_x(2) == pytest.approx(10.0)
    assert geometry(12, 8).pocket_line == 5.5
    with pytest.raises(ValueError, match="not a standard tape width"):
        geometry(10, 4)


# --- Endpoints ----------------------------------------------------------------------------


def test_endpoints(client: TestClient) -> None:
    base = "/api/v1/library"
    assert client.get(f"{base}/parts").json() == []
    footprint = R0603.model_dump(mode="json")
    assert client.put(f"{base}/footprints/{R0603.id}", json=footprint).status_code == 200

    # Manufacturer part numbers may contain slashes and commas.
    part = R10K.model_copy(update={"id": "LM317T/NOPB,118"}).model_dump(mode="json")
    r = client.put(f"{base}/parts/LM317T/NOPB,118", json=part)
    assert r.status_code == 200, r.json()
    assert client.get(f"{base}/parts/LM317T/NOPB,118").json()["height"] == 0.45
    assert client.put(f"{base}/parts/other", json=part).status_code == 422

    orphan = {**part, "id": "X", "footprint_id": "SOT-23"}
    assert client.put(f"{base}/parts/X", json=orphan).status_code == 409
    assert client.delete(f"{base}/footprints/{R0603.id}").status_code == 409
    assert client.get(f"{base}/parts/nope").status_code == 404

    r = client.post(
        f"{base}/import/openpnp", json={"packages_xml": PACKAGES_XML, "parts_xml": PARTS_XML}
    )
    assert r.status_code == 200
    assert "footprint:SOT-23-6" in r.json()["added"]
    exported = client.get(f"{base}/export").json()
    assert exported["format"] == "picnicplace-library"
    assert len(exported["parts"]) == 3

    r = client.post(f"{base}/import", json={"document": exported})
    assert r.json()["unchanged"] == 3 + 4

    bad = client.post(f"{base}/import/openpnp", json={"parts_xml": "<nope/>"})
    assert bad.status_code == 400
    assert client.delete(f"{base}/parts/LM317T/NOPB,118").status_code == 204
