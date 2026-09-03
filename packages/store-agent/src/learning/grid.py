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


def _number(value: object) -> float | None:
    """A finite, non-negative float, or ``None``.

    NaN and infinity are refused rather than passed on: `bucket_index` scores rungs by
    ``abs(bucket - depth)``, and against NaN every comparison is False, so it picks the first
    rung and calls it evidence. A silently-wrong tally is worse than a dropped row.

    `bool` is refused explicitly because `True` is a perfectly good `float(1.0)` in Python, and a
    `won` flag read into a depth field would tally a 100% discount.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number if number >= 0.0 else None


def percent_as_fraction(value: object) -> float | None:
    """Read an explicitly-percent depth (`discount_pct: 20.0`) as a fraction.

    Always divided by :data:`PERCENT_PER_UNIT`. The field says what unit it is in, so there is
    nothing to infer — and inferring anyway is how `discount_pct: 0.5` ("half a percent off")
    becomes a 50% discount in the tally.
    """
    number = _number(value)
    if number is None:
        return None
    fraction = number / PERCENT_PER_UNIT
    return fraction if fraction <= FRACTION_CEILING else None


def as_fraction(value: object) -> float | None:
    """Read a fractional depth (`discount_depth: 0.2`), or ``None`` when it is not usable.

    A value above :data:`FRACTION_CEILING` is read as a percent written into a fraction's field
    and divided down. That rescue is confined to THIS reader, where the unit is implied by the
    field name and can therefore be wrong; :func:`percent_as_fraction` never guesses, because
    there the unit is stated. The rescue is not tolerance dressed up as a feature: no legitimate
    fraction exceeds 1.0 (that is 100% off), the two spellings sit side by side in the very rows
    this loop reads, and the alternative — snapping 20.0 to the nearest rung, 0.2 — happens to
    give the right answer on a grid that stops at 20% and the wrong one on any grid that does not.
    """
    number = _number(value)
    if number is None:
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
