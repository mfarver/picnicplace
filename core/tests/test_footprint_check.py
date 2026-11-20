"""A part's footprint against the board's where it goes: the same pads by any name pass;
pads turned, moved, reversed, or different are warned of in the run's plan."""

from fastapi.testclient import TestClient

from picnicplace.job.footprint_check import footprint_mismatch
from picnicplace.library.models import Footprint, Pad
from tests.conftest import connect
from tests.test_run import lumen, publish, set_up  # noqa: F401 (fixture)


def pad(name: str, x: float, y: float, pin1: bool = False) -> Pad:
    return Pad(name=name, x=x, y=y, width=0.6, height=0.5, pin1=pin1)


def diode(id: str, pin1_x: float = -0.8, shift: float = 0.0) -> Footprint:
    return Footprint(
        id=id,
        pads=[
            pad("1", pin1_x + shift, 0, pin1=True),
            pad("2", -pin1_x + shift, 0),
        ],
    )


def sot23(id: str) -> Footprint:
    return Footprint(
        id=id,
        pads=[pad("1", -0.95, -1.1, pin1=True), pad("2", 0.95, -1.1), pad("3", 0, 1.1)],
    )


def turned(f: Footprint, id: str) -> Footprint:
    """``f`` turned 90 degrees counterclockwise."""
    return Footprint(id=id, pads=[p.model_copy(update={"x": -p.y, "y": p.x}) for p in f.pads])


def test_the_same_pads_by_another_name_fit() -> None:
    assert footprint_mismatch(diode("D_0603"), diode("D_0603")) is None
    assert footprint_mismatch(diode("D_0603"), diode("SOD-523-ish")) is None
    assert footprint_mismatch(sot23("SOT-23"), sot23("SOT23_3")) is None


def test_turned_reversed_moved_or_different_pads_do_not() -> None:
    board = sot23("SOT-23")
    assert footprint_mismatch(board, turned(board, "SOT23_ROT")) == (
        "its pads are the board's turned 90° counterclockwise: the part would be placed "
        "turned 90° from the board's footprint"
    )
    # A diode whose cathode (pin 1) is on the other side: the pads alone fit.
    reason = footprint_mismatch(diode("D_0603"), diode("D_REV", pin1_x=0.8))
    assert reason is not None and "turned 180°" in reason
    reason = footprint_mismatch(diode("D_0603"), diode("D_OFF", shift=0.5))
    assert reason is not None and reason.startswith("its origin is 0.50 mm from the board's")
    assert footprint_mismatch(diode("D_0603"), sot23("SOT-23")) == "it has 3 pads, the board's 2"
    wide = Footprint(id="D_1206", pads=[pad("1", -1.5, 0, pin1=True), pad("2", 1.5, 0)])
    assert footprint_mismatch(diode("D_0603"), wide) == "its pads are not where the board's are"
    assert footprint_mismatch(diode("D_0603"), None) is not None


def test_the_plan_warns_of_a_part_on_a_footprint_unlike_the_boards(
    lumen: TestClient,  # noqa: F811
) -> None:
    with connect(lumen) as s:
        set_up(lumen, s)
        library = lumen.app.state.ctx.library  # type: ignore[attr-defined]
        mcu = library.find_part("EX-MCU-8")
        board = library.find_footprint(mcu.footprint_id)

        # The same pads under another name: no warning.
        library.put_footprint(board.model_copy(update={"id": "MY-SOIC-8"}))
        library.put_part(mcu.model_copy(update={"footprint_id": "MY-SOIC-8"}))
        publish(s)
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        assert not [i for i in plan["issues"] if "is not the board's" in i["message"]]

        # Turned a quarter turn: warned of, naming the placement.
        library.put_footprint(turned(board, "MY-SOIC-8"))
        publish(s)
        plan = s.post("/api/v1/run/plan", {"job_id": "demo"}).json()
        found = [i for i in plan["issues"] if "is not the board's" in i["message"]]
        assert len(found) == 1 and found[0]["severity"] == "warning", plan["issues"]
        assert found[0]["message"].startswith(
            f"EX-MCU-8's footprint MY-SOIC-8 is not the board's {mcu.footprint_id} at U2: its "
            "pads are the board's turned 90° counterclockwise"
        )
        assert found[0]["ref"] == "U2"
