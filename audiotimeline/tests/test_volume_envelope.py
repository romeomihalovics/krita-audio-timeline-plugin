"""volume_envelope.evaluate: linear interpolation for a flat (2-point) clip
and the Catmull-Rom spline used once a clip has interior bend points, plus
retrim()/split() which reproject an envelope's control points onto a new
(shorter) played window -- the machinery behind trim/split preserving a
multi-point curve's shape.
"""

import pytest

from ..audio import volume_envelope as ve


# ------------------------------------------------------------------- evaluate
def test_evaluate_empty_points_is_unity():
    assert ve.evaluate([], 0.5) == 1.0


def test_evaluate_single_point_is_constant():
    assert ve.evaluate([(0.3, 0.7)], 0.0) == 0.7
    assert ve.evaluate([(0.3, 0.7)], 1.0) == 0.7


def test_evaluate_two_points_is_linear():
    points = [(0.0, 0.0), (1.0, 1.0)]
    assert ve.evaluate(points, 0.5) == pytest.approx(0.5)
    assert ve.evaluate(points, 0.25) == pytest.approx(0.25)


def test_evaluate_clamps_outside_range():
    points = [(0.2, 0.4), (0.8, 0.9)]
    assert ve.evaluate(points, 0.0) == 0.4
    assert ve.evaluate(points, 1.0) == 0.9


def test_evaluate_unsorted_points_are_sorted_first():
    points = [(1.0, 1.0), (0.0, 0.0)]
    assert ve.evaluate(points, 0.5) == pytest.approx(0.5)


def test_evaluate_multipoint_passes_through_control_points():
    points = [(0.0, 0.2), (0.3, 1.0), (0.6, 0.1), (1.0, 0.8)]
    for frac, gain in points:
        assert ve.evaluate(points, frac) == pytest.approx(gain, abs=1e-9)


def test_evaluate_multipoint_never_overshoots_segment_bounds():
    # A steep dip next to a shallow rise can make a naive Catmull-Rom swing
    # past the segment's own min/max -- evaluate() clamps within [min, max]
    # of the two immediate control points of whichever segment it's in.
    points = [(0.0, 1.0), (0.1, 0.0), (0.2, 1.0), (1.0, 1.0)]
    for i in range(0, 21):
        frac = i / 20.0
        gain = ve.evaluate(points, frac)
        assert -1e-9 <= gain <= 1.0 + 1e-9


def test_evaluate_sorted_matches_evaluate():
    points = [(0.6, 0.1), (0.0, 0.2), (1.0, 0.8), (0.3, 1.0)]
    sorted_pts = sorted(points, key=lambda p: p[0])
    for frac in (0.0, 0.15, 0.3, 0.45, 0.6, 0.8, 1.0):
        assert ve.evaluate_sorted(sorted_pts, frac) == ve.evaluate(points, frac)


# --------------------------------------------------------------------- retrim
def test_retrim_flat_envelope_stays_flat():
    points = [(0.0, 0.5), (1.0, 0.5)]
    result = ve.retrim(points, 0.25, 0.75)
    assert result[0] == (0.0, 0.5)
    assert result[-1] == (1.0, 0.5)


def test_retrim_remaps_interior_points_into_0_1():
    points = [(0.0, 1.0), (0.5, 0.0), (1.0, 1.0)]
    result = ve.retrim(points, 0.25, 0.75)
    # 0.5 sits exactly in the middle of [0.25, 0.75] -> remaps to 0.5.
    fracs = [f for f, _ in result]
    assert any(abs(f - 0.5) < 1e-9 for f in fracs)
    # Interior point's gain (the dip to 0.0) is preserved.
    gains = dict(result)
    mid_gain = [g for f, g in result if abs(f - 0.5) < 1e-9][0]
    assert mid_gain == pytest.approx(0.0)


def test_retrim_drops_points_outside_kept_range():
    points = [(0.0, 1.0), (0.1, 0.2), (0.5, 0.9), (0.9, 0.3), (1.0, 1.0)]
    result = ve.retrim(points, 0.3, 0.7)
    fracs = [f for f, _ in result]
    # Only the boundary points (0.0, 1.0 in new space) plus 0.5 (which maps
    # inside [0.3, 0.7]) should remain.
    assert len(result) == 3
    assert fracs[0] == 0.0 and fracs[-1] == 1.0


def test_retrim_boundary_gains_match_original_curve_value():
    points = [(0.0, 0.2), (0.5, 1.0), (1.0, 0.4)]
    start_frac, end_frac = 0.25, 0.75
    result = ve.retrim(points, start_frac, end_frac)
    assert result[0][1] == pytest.approx(ve.evaluate(points, start_frac))
    assert result[-1][1] == pytest.approx(ve.evaluate(points, end_frac))


def test_retrim_zero_span_collapses_to_flat_pair():
    points = [(0.0, 0.2), (0.5, 1.0), (1.0, 0.4)]
    result = ve.retrim(points, 0.5, 0.5)
    assert len(result) == 2
    assert result[0][0] == 0.0 and result[1][0] == 1.0


def test_retrim_empty_points_returns_default_flat():
    assert ve.retrim([], 0.2, 0.8) == [(0.0, 1.0), (1.0, 1.0)]


# ---------------------------------------------------------------------- split
def test_split_keep_left_matches_retrim_to_zero_split():
    points = [(0.0, 0.2), (0.4, 1.0), (0.8, 0.3), (1.0, 0.9)]
    left = ve.split(points, 0.4, keep_left=True)
    expected = ve.retrim(points, 0.0, 0.4)
    assert left == expected


def test_split_keep_right_matches_retrim_from_split_to_one():
    points = [(0.0, 0.2), (0.4, 1.0), (0.8, 0.3), (1.0, 0.9)]
    right = ve.split(points, 0.4, keep_left=False)
    expected = ve.retrim(points, 0.4, 1.0)
    assert right == expected


def test_split_siblings_agree_at_the_seam():
    """No audible jump at the cut point: the left sibling's rightmost gain
    and the right sibling's leftmost gain must both equal the original
    curve's value exactly at the split fraction."""
    points = [(0.0, 0.3), (0.35, 1.0), (0.6, 0.1), (1.0, 0.8)]
    split_frac = 0.6
    left = ve.split(points, split_frac, keep_left=True)
    right = ve.split(points, split_frac, keep_left=False)
    seam_gain = ve.evaluate(points, split_frac)
    assert left[-1][1] == pytest.approx(seam_gain)
    assert right[0][1] == pytest.approx(seam_gain)
