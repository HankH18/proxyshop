"""The discount-depth grid, and the units it is measured in.

Depth is carried two ways in this codebase and confusing them is silent:

* **A fraction** — ``0.2`` — in outcome records (``discount_depth``), in the store context's
  ``network_priors[cluster]["depth_buckets"]``, and everywhere in this package.
* **A percent** — ``20.0`` — in the approved envelope (``max_discount_pct``), in
  ``hooks.ToolHooks.authorize_discount``, and in ``learned_policy['actions'][c]['discount_pct']``.

A loop that hands the runtime the fraction it learned in asks for a 0.2% discount, which is
inside every wall and therefore never refused, never logged, and never noticed. So the
conversion happens in exactly one place — :func:`as_percent` — and the grid below is stated in
fractions, once.

The grid is a **constant**, not evidence. That matters for R17: the network prior is forbidden
to pool discount elasticity across stores, and a grid of candidate depths derived from nothing
carries no cross-store information. What each store learns is the *weight* it puts on each rung,
from its own outcomes only.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Candidate discount depths, as fractions, ascending. The same rungs the store context's
#: `network_priors[cluster]["depth_buckets"]` carries.
DEFAULT_DEPTH_BUCKETS: tuple[float, ...] = (0.0, 0.05, 0.10, 0.15, 0.20)

#: Percent points per unit of fractional depth.
PERCENT_PER_UNIT = 100.0

#: Above this, a "depth" is read as a percent that was written into a fraction's field. 20.0 is
#: not a 2000% discount; it is `discount_pct` handed over where `discount_depth` was expected.
FRACTION_CEILING = 1.0


def as_percent(depth: float) -> float:
    """A fractional depth as the percent the envelope and `learned_policy` speak."""
    return float(depth) * PERCENT_PER_UNIT


def as_fraction(value: object) -> float | None:
    """Read a depth as a fraction, or ``None`` when the value is not a usable number.

    A value above :data:`FRACTION_CEILING` is read as a percent and divided down. This is not
    guesswork dressed up as tolerance: no legitimate discount fraction exceeds 1.0 (that is a
    100% discount), the two spellings live side by side in the very records this loop reads
    (`discount_depth: 0.2` next to `discount_pct: 20.0`), and the alternative — snapping 20.0 to
    the nearest rung, 0.2 — happens to give the right answer here and the wrong one for any
    grid that does not stop at 20%.

    NaN and infinity are refused rather than snapped: `min(..., key=abs(bucket - nan))` picks an
    arbitrary rung and calls it evidence.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number < 0.0:
        return None
    if number > FRACTION_CEILING:
        number = number / PERCENT_PER_UNIT
        if number > FRACTION_CEILING:
            return None
    return number


def bucket_index(buckets: Sequence[float], depth: float) -> int:
    """The rung `depth` belongs to: the nearest one, ties going to the shallower rung."""
    best = 0
    best_gap = abs(float(buckets[0]) - depth)
    for index in range(1, len(buckets)):
        gap = abs(float(buckets[index]) - depth)
        if gap < best_gap:
            best, best_gap = index, gap
    return best
