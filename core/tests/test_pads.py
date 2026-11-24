"""The pad fit's solve, on pads that cannot fix where the part lies."""

from picnicplace.vision.pads import PadRect, _rigid  # pyright: ignore[reportPrivateUsage]


def test_pads_found_at_one_point_give_no_fit() -> None:
    """Two pads found, both at the footprint's middle (an exposed pad found twice): they
    say nothing of the part's turn, so the solve is singular. No fit, not a crash."""
    pad = PadRect(0.0, 0.0, 10.0, 16.0)
    found = [(pad, 100.0, 100.0, 50.0), (pad, 101.0, 100.0, 50.0)]
    assert _rigid(found, 100.0, 100.0, 0.0) is None


def test_pads_apart_give_a_fit() -> None:
    """Two pads either side of the middle, found 2 px right of where they were expected:
    the fit moves the part 2 px, and leaves its turn alone."""
    left, right = PadRect(-20.0, 0.0, 8.0, 8.0), PadRect(20.0, 0.0, 8.0, 8.0)
    found = [(left, 82.0, 100.0, 64.0), (right, 122.0, 100.0, 64.0)]
    fit = _rigid(found, 100.0, 100.0, 0.0)
    assert fit is not None
    u, v, angle, residual = fit
    assert abs(u - 102.0) < 1e-6 and abs(v - 100.0) < 1e-6
    assert abs(angle) < 1e-6 and residual < 1e-6
