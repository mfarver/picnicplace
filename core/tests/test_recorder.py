"""The flight recorder: keeping what the machine saw and did, and replaying its vision."""

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from picnicplace.cli import replay
from picnicplace.recorder import FlightRecorder, RecorderError, _difference, replay_vision
from tests.conftest import connect
from tests.test_run import lumen, ok, set_up  # noqa: F401 (fixture)


def events(folder: Path) -> list[dict[str, Any]]:
    lines = (folder / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_a_recorded_run(lumen: TestClient, tmp_path: Path) -> None:  # noqa: F811
    with connect(lumen) as s:
        set_up(lumen, s)
        body = {"job_id": "demo", "skip_blocked": True, "record": True}
        ok(s.wait(s.post("/api/v1/run/start", body)))
        run = lumen.get("/api/v1/run").json()
        assert run["recording"] is not None

        listed = lumen.get("/api/v1/recordings").json()
        assert len(listed) == 1
        info = listed[0]
        assert info["id"] == run["recording"] and info["reason"] == "run"
        assert not info["recording"] and info["outcome"].startswith("finished:")
        assert info["frames"] > 0 and info["events"] > info["frames"]

        r = lumen.get(f"/api/v1/recordings/{info['id']}/download")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        saved = tmp_path / "run.zip"
        saved.write_bytes(r.content)

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = set(z.namelist())
        root = info["id"]
        for name in ("manifest.json", "events.jsonl", "snapshot/config.json"):
            assert f"{root}/{name}" in names
        for name in ("profile.json", "feeders.json", "job.json", "parts.json"):
            assert f"{root}/snapshot/{name}" in names
        assert sum(1 for n in names if n.endswith(".png")) == info["frames"]
        config = json.loads(z.read(f"{root}/snapshot/config.json"))
        assert config["data"]["dir"] == "(not recorded)"
        manifest = json.loads(z.read(f"{root}/manifest.json"))
        assert manifest["format"] == "picnicplace-recording" and manifest["version"] == 1
        z.extractall(tmp_path)

    kinds = {e["kind"] for e in events(tmp_path / root)}
    for kind in ("run.start", "vision", "pick", "align", "learn", "place", "run.placed"):
        assert kind in kinds, kind
    assert {"move", "vacuum", "core.operation", "core.machine.state", "run.end"} <= kinds
    seen = {e["fn"] for e in events(tmp_path / root) if e["kind"] == "vision"}
    assert seen == {"find_circle", "find_circles", "fit_pads"}

    # Today's vision gives the same answers on the recorded frames.
    checks = replay_vision(saved)
    assert len(checks) == info["frames"]
    assert all(c.same for c in checks), [c for c in checks if not c.same][:2]
    assert replay(tmp_path / root) == 0


def test_recording_by_hand(lumen: TestClient) -> None:  # noqa: F811
    with connect(lumen) as s:
        s.home()
        r = s.post("/api/v1/recordings", {"label": "Homing check!"})
        assert r.status_code == 200, r.json()
        info = r.json()
        assert info["recording"] and info["id"].endswith("-homing-check")
        assert s.post("/api/v1/recordings", {"label": "again"}).status_code == 409
        assert s.delete(f"/api/v1/recordings/{info['id']}").status_code == 409
        r = lumen.get(f"/api/v1/recordings/{info['id']}/download")
        assert r.status_code == 409
        ok(s.wait(s.post("/api/v1/machine/visual-home")))
        stopped = s.post("/api/v1/recordings/stop").json()
        assert stopped["outcome"] == "stopped by the operator" and stopped["frames"] == 1
        assert s.post("/api/v1/recordings/stop").json() is None
        assert s.delete(f"/api/v1/recordings/{info['id']}").status_code == 204
        assert lumen.get("/api/v1/recordings").json() == []
        assert lumen.get("/api/v1/recordings/../etc").status_code == 404


def test_old_recordings_are_deleted(tmp_path: Path) -> None:
    recorder = FlightRecorder(tmp_path, keep=2)
    ids = []
    for label in ("one", "two", "three"):
        ids.append(recorder.start(label).id)
        recorder.event("note", text=label)
        recorder.stop("done")
    kept = [r.id for r in recorder.list()]
    assert len(kept) == 2 and ids[0] not in kept
    with pytest.raises(RecorderError):
        recorder.get(ids[0])


def test_replay_says_what_changed(tmp_path: Path) -> None:
    recorder = FlightRecorder(tmp_path)
    import numpy as np

    image = np.zeros((200, 300, 3), np.uint8)
    image[90:110, 140:160] = 255
    info = recorder.start("part")

    import asyncio

    box = asyncio.run(recorder.vision("find_part", image, 60.0, camera="bottom"))
    assert box is not None
    recorder.stop()
    folder = tmp_path / info.id
    assert all(c.same for c in replay_vision(folder))

    # As if the recorded answer had been different: the replay says so.
    lines = events(folder)
    for e in lines:
        if e["kind"] == "vision":
            e["result"]["u"] += 0.5
    (folder / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines))
    [check] = replay_vision(folder)
    assert not check.same and check.difference == pytest.approx(0.5)


def manifest(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "manifest.json").read_text())["recording"]


def test_the_manifest_is_kept_up_to_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A recording copied while it is still open says what it holds."""
    import asyncio

    import numpy as np

    import picnicplace.recorder

    monkeypatch.setattr(picnicplace.recorder, "MANIFEST_EVERY_S", 0.0)
    recorder = FlightRecorder(tmp_path)
    info = recorder.start("open")
    image = np.zeros((200, 300, 3), np.uint8)
    asyncio.run(recorder.vision("find_part", image, 60.0, camera="bottom"))
    recorder.event("note", text="still going")
    seen = manifest(tmp_path / info.id)
    assert seen["frames"] == 1 and seen["events"] == 3
    assert seen["ended_at"] is None and seen["outcome"] is None
    assert not (tmp_path / info.id / "manifest.json.part").exists()
    recorder.stop("finished")
    assert manifest(tmp_path / info.id)["outcome"] == "finished"


def test_an_interrupted_recording_is_closed_at_startup(tmp_path: Path) -> None:
    """The core stopped while recording (it crashed, or was killed): the next start
    counts what the recording holds and marks it interrupted."""
    import asyncio

    import numpy as np

    recorder = FlightRecorder(tmp_path)
    info = recorder.start("crash")
    image = np.zeros((200, 300, 3), np.uint8)
    for _ in range(2):
        asyncio.run(recorder.vision("find_part", image, 60.0, camera="bottom"))
    # The core dies: nothing is stopped, and the last line is cut short.
    current = recorder._current  # pyright: ignore[reportPrivateUsage]
    assert current is not None
    current.events.write('{"t": 9.5, "kind": "mo')
    current.events.close()
    assert manifest(tmp_path / info.id)["ended_at"] is None

    again = FlightRecorder(tmp_path).get(info.id)
    assert again.outcome == "interrupted" and not again.recording
    assert again.frames == 2 and again.events == 3  # the start and two vision calls
    assert again.ended_at is not None and again.ended_at >= info.started_at
    # It can be downloaded and replayed like any other.
    assert FlightRecorder(tmp_path).zip(info.id).is_file()
    assert len(replay_vision(tmp_path / info.id)) == 2


def test_differences() -> None:
    assert _difference({"x": 1.0, "y": [1, 2]}, {"x": 1.25, "y": [1, 2]}) == 0.25
    assert _difference(None, {"x": 1}) is None
    assert _difference([1], [1, 2]) is None
    assert _difference(None, None) == 0.0


def test_a_zip_with_paths_outside_it_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(RecorderError):
        replay_vision(bad)


def test_the_log_is_recorded(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    recorder = FlightRecorder(tmp_path)
    with caplog.at_level(logging.INFO, logger="picnicplace"):
        info = recorder.start("log")
        logging.getLogger("picnicplace.feeders").info("fed %d mm", 4)
        recorder.stop()
        logging.getLogger("picnicplace.feeders").info("not recorded")
    logs = [e for e in events(tmp_path / info.id) if e["kind"] == "log"]
    assert [e["message"] for e in logs] == ["fed 4 mm"]
    assert logs[0]["logger"] == "picnicplace.feeders" and logs[0]["level"] == "INFO"
