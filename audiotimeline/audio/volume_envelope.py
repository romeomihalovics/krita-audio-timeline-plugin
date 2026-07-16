"""Shared gain-envelope evaluation for a clip's volume_points.

Used by both timeline_widget.py (to draw the volume curve/bend points) and
mixdown.py (to compute per-sample gain during render) so the two can never
disagree about the shape of the curve between control points.
"""


def evaluate(points, fraction):
    """Evaluates the gain envelope defined by `points` at `fraction`.

    `points` is a list of (fraction, gain) pairs with fraction in [0, 1];
    order doesn't matter (sorted here). With 2 points the result is a plain
    linear interpolation (phase 1's flat-line case, or any 2-point
    envelope). With more points, a Catmull-Rom spline is used, evaluated
    per-segment with the two neighboring control points as extra tangent
    input (duplicating the nearest point at the ends where a true neighbor
    doesn't exist) -- it passes exactly through every point, needs only the
    4 surrounding points per segment, and doesn't overshoot as aggressively
    as a naive global cubic through sparse/uneven points.
    """
    if not points:
        return 1.0
    pts = sorted(points, key=lambda p: p[0])
    if len(pts) == 1:
        return pts[0][1]

    fraction = max(0.0, min(1.0, fraction))
    if fraction <= pts[0][0]:
        return pts[0][1]
    if fraction >= pts[-1][0]:
        return pts[-1][1]

    i = 0
    for j in range(len(pts) - 1):
        if pts[j][0] <= fraction <= pts[j + 1][0]:
            i = j
            break

    x1, y1 = pts[i]
    x2, y2 = pts[i + 1]
    if x2 == x1:
        return y2
    t = (fraction - x1) / (x2 - x1)

    if len(pts) == 2:
        return y1 + (y2 - y1) * t

    y0 = pts[i - 1][1] if i - 1 >= 0 else y1
    y3 = pts[i + 2][1] if i + 2 < len(pts) else y2

    t2 = t * t
    t3 = t2 * t
    a0 = -0.5 * y0 + 1.5 * y1 - 1.5 * y2 + 0.5 * y3
    a1 = y0 - 2.5 * y1 + 2 * y2 - 0.5 * y3
    a2 = -0.5 * y0 + 0.5 * y2
    a3 = y1
    result = a0 * t3 + a1 * t2 + a2 * t + a3

    # Catmull-Rom through 3+ unevenly-spaced points can overshoot past the
    # nearest two control points' own gains (e.g. a steep dip right next to
    # a shallow one can briefly swing negative between them) -- clamped to
    # this segment's own [min, max] so the result never implies a gain the
    # user didn't actually place a point at, and never goes negative
    # (silently phase-inverting audio) or astronomically high.
    lo, hi = min(y1, y2), max(y1, y2)
    return max(lo, min(hi, result))


def retrim(points, start_fraction, end_fraction):
    """Returns a new points list remapping `points` (fractions relative to
    some original duration) onto the sub-range [start_fraction,
    end_fraction] of that same space, rescaled to [0, 1] of the new
    (shorter-or-equal) duration -- used whenever a clip's played window
    changes (trimming an edge, splitting into two siblings) so a
    multi-point envelope's bend points stay anchored to their original
    absolute-time position instead of stretching/squishing to refill
    whatever duration remains. Points outside the kept range are dropped;
    fresh boundary points at 0.0 and 1.0 are evaluated from the original
    curve so the remapped envelope's edges match what the original curve
    sounded like there, with no jump."""
    if not points:
        return [(0.0, 1.0), (1.0, 1.0)]
    pts = sorted(points, key=lambda p: p[0])
    start_fraction = max(0.0, min(1.0, start_fraction))
    end_fraction = max(start_fraction, min(1.0, end_fraction))
    span = end_fraction - start_fraction
    start_gain = evaluate(pts, start_fraction)
    end_gain = evaluate(pts, end_fraction)
    if span <= 0:
        return [(0.0, start_gain), (1.0, end_gain)]
    interior = [
        ((f - start_fraction) / span, g)
        for f, g in pts if start_fraction < f < end_fraction
    ]
    return [(0.0, start_gain)] + interior + [(1.0, end_gain)]


def split(points, split_fraction, keep_left):
    """Returns a new points list for one side of a clip that's been split
    at `split_fraction` (in the original, pre-split clip's own fraction
    space): `keep_left=True` keeps the portion before the cut, remapped
    onto [0, 1] of its own (now shorter) duration; `keep_left=False` keeps
    the portion after. Both sides always gain a fresh boundary point right
    at the cut, evaluated from the original curve, so neither sibling's
    envelope jumps at the seam. A thin wrapper over retrim()'s more general
    "keep an arbitrary sub-range" logic, since a split is just retrimming
    each sibling down to one half of the original."""
    split_fraction = max(0.0, min(1.0, split_fraction))
    if keep_left:
        return retrim(points, 0.0, split_fraction)
    return retrim(points, split_fraction, 1.0)
