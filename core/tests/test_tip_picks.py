"""Picks by part and nozzle tip, and which tips they rule out."""

from pathlib import Path

from picnicplace.tip_picks import TipPicks


def test_a_tip_that_never_picked_a_part_is_barred_once_another_has() -> None:
    tips = TipPicks(None)
    tips.record("C", "N08", picked=False)
    assert not tips.never_picked("C", "N08")
    tips.record("C", "N08", picked=False)
    assert tips.never_picked("C", "N08") and not tips.barred("C", "N08")
    tips.record("C", "N045", picked=True)
    assert tips.barred("C", "N08") and not tips.barred("C", "N045")


def test_empty_picks_with_a_tip_that_has_picked_the_part_are_the_feeders() -> None:
    tips = TipPicks(None)
    tips.record("R", "N08", picked=True)
    for _ in range(5):
        tips.record("R", "N08", picked=False)
    tips.record("R", "N045", picked=True)
    assert not tips.never_picked("R", "N08") and not tips.barred("R", "N08")


def test_kept_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "tip-picks.json"
    TipPicks(path).record("C", "N08", picked=False)
    TipPicks(path).record("C", "N08", picked=False)
    again = TipPicks(path)
    assert again.get("C", "N08").empty == 2 and again.never_picked("C", "N08")
