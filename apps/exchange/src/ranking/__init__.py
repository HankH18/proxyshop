"""T-032 — the published ranking: eligibility filters, one formula, one shortlist.

`rank(candidates, intent, trust_snapshot, config)` is the whole public surface. It is four
positional arguments because every caller in the system passes exactly those four, and the
result is a plain mapping::

    {
      "ranked":     [row, ...],           # eligible rows, best first
      "candidates": [row, ...],           # EVERY row, in the order they were given
      "shortlist":  {"auction_id": ..., "slots": [slot, ...]},
    }

Each row records `eligible`, `rank_score` (``None`` when ineligible), `components` (empty
when ineligible) and `exclusion_reasons`. The `None` and the empty map are the point rather
than a convention: an ineligible candidate is one the formula never ran on, and a row that
carried a number would be a row somebody could sort by.

The order of operations, which is itself a requirement
------------------------------------------------------
1. **Filter, then score.** R19 makes hard constraints eligibility filters and R12 makes the
   blacklist read fail closed. Both are decided in :mod:`.filters` before :mod:`.scoring` is
   reached, so an excluded candidate has no score to leak.
2. **One formula, blind.** :mod:`.scoring` applies the single published five-term
   combination with `packages/contracts`' weights. Network fee, store tier and a store's own
   discount ceiling are not features and are not reachable from the scorer, so changing them
   changes nothing — not the order, and not a single bit of any score.
3. **Published ties.** Equal scores break in the published order (D13):
   `verified_hard_fit_count`, then `trust`, then `price` ascending, then `bid_id`. The
   formula has no raw-price term, so three bids that differ only in price genuinely tie, and
   the tie-break is what orders them. Input order never does.
4. **Shortlist last.** :mod:`.shortlist` picks up to four differentiated slots, one per
   store, over the ranked rows.

`config` supplies `now` (so nothing here reads the wall clock) and optionally `auction_id`.
`weights` and `eligibility` are keyword-only extras with inert defaults: the published
surface is the four positionals, and an optional collaborator must never be something a
caller has to know about to get a correct answer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from contracts.ranking import DEFAULT_RANKING_WEIGHTS, RankingWeights

from . import shortlist as _shortlist
from .filters import exclusion_reasons, read, read_criteria, trust_row
from .reasons import (
    EXCLUSION_REASON_PREFIXES,
    REASON_BLACKLIST_UNREADABLE,
    REASON_BLACKLISTED,
    REASON_ELIGIBILITY_DENIED,
    REASON_EXPIRED,
    REASON_HARD_CONSTRAINT,
    REASON_MALFORMED,
    REASON_OFF_DOMAIN,
    REASON_UNDECIDABLE_INTENT,
)
from .scoring import score

#: Which way each published tie-breaker sorts. `-1` is "more is better".
TIE_BREAK_DIRECTIONS: Mapping[str, int] = {
    "verified_hard_fit_count": -1,
    "trust": -1,
    "price": 1,
    "bid_id": 1,
}

#: Used when a caller supplies no `auction_id` anywhere. A `Shortlist` needs a non-empty one,
#: and inventing a random id would make two runs over identical inputs differ.
FALLBACK_AUCTION_ID = "auction-unidentified"


def _config_value(config: Any, name: str, default: Any = None) -> Any:
    if config is None:
        return default
    return read(config, name, default)


def _now_from(config: Any) -> float:
    raw = _config_value(config, "now", None)
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    # Only reachable when a caller supplies no clock at all. Every caller in this system
    # does, which is what makes a shortlist reproducible from its inputs.
    import time

    return time.time()


def _auction_id(candidates: Sequence[Any], intent: Any, config: Any) -> str:
    for source, name in (
        (config, "auction_id"),
        (intent, "auction_id"),
        (intent, "intent_id"),
    ):
        value = read(source, name, None) if source is not None else None
        if value:
            return str(value)
    for candidate in candidates:
        value = read(candidate, "auction_id", None)
        if value:
            return str(value)
    return FALLBACK_AUCTION_ID


def _price_of(offer: Any) -> float | None:
    for name in ("total_price", "unit_price", "price"):
        raw = read(offer, name, None)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _sort_key(row: Mapping[str, Any], tie_breakers: Sequence[str]) -> tuple:
    """`(-rank_score, *published tie-breakers)` for one eligible row.

    The tie-breakers come from the weight set rather than from a literal list here, so a
    deployment that publishes a different order gets that order applied instead of this
    module's opinion of it.
    """
    key: list[Any] = [-float(row["rank_score"])]
    for name in tie_breakers:
        direction = TIE_BREAK_DIRECTIONS.get(name, 1)
        value = row.get(name)
        if isinstance(value, str):
            # A string can only sort one way. A published descending string tie-break would
            # be a rule this module cannot honour, and quietly sorting it ascending would be
            # a different rule wearing the published one's name.
            if direction != 1:
                raise ValueError(f"tie-breaker {name!r} is a string and cannot sort descending")
            key.append(value)
            continue
        if value is None:
            # An unreadable tie-break value sorts LAST whichever way the field sorts. The
            # alternative — reading it as 0.0 — would make an unknown price the cheapest one
            # in the auction.
            key.append(math.inf)
            continue
        key.append(direction * float(value))
    if "bid_id" not in tie_breakers:
        key.append(str(row["bid_id"]))
    return tuple(key)


def rank(
    candidates: Sequence[Any],
    intent: Any,
    trust_snapshot: Any,
    config: Any,
    *,
    weights: RankingWeights | None = None,
    eligibility: Any = None,
) -> dict[str, Any]:
    """Rank one auction's candidates and build its shortlist.

    Args:
        candidates: the bid candidates, each carrying `bid_id`, `store_id`, `store_domain`,
            an `offer`, its `claims` and the published features.
        intent: the buyer intent. Only its `hard_constraints` decide eligibility here;
            preference weights already reached `intent_match` upstream and never touch the
            published weights (D50).
        trust_snapshot: `{store_id: row}`. The `blacklisted` flag and the `score` are read
            from it; a store with no row fails closed (R12).
        config: at least `{"now": <epoch seconds>}`; `auction_id` is read from it when given.
        weights: the weight set to apply. Defaults to the published one.
        eligibility: an optional `SellerEligibility` source. Inert by default — the blacklist
            is derived from `trust_snapshot`, and this only ever adds denials.

    Returns:
        `{"ranked": [...], "candidates": [...], "shortlist": {"auction_id", "slots"}}`.
        The inputs are never written to.
    """
    weights = DEFAULT_RANKING_WEIGHTS if weights is None else weights
    candidates = list(candidates or ())
    now = _now_from(config)
    criteria, intent_reason = read_criteria(intent)

    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        reasons, verified_fits = exclusion_reasons(
            candidate,
            criteria=criteria,
            intent_reason=intent_reason,
            trust_snapshot=trust_snapshot,
            now=now,
            eligibility=eligibility,
        )
        store_id = str(read(candidate, "store_id", "") or "")
        row_of_store = trust_row(store_id, trust_snapshot) if store_id else None
        trust_score = None
        if row_of_store is not None:
            raw_trust = read(row_of_store, "score", None)
            if raw_trust is not None and not isinstance(raw_trust, bool):
                try:
                    trust_score = float(raw_trust)
                except (TypeError, ValueError):
                    trust_score = None
        offer = read(candidate, "offer", None)

        row: dict[str, Any] = {
            "bid_id": str(read(candidate, "bid_id", "") or ""),
            "store_id": store_id,
            "eligible": not reasons,
            "rank_score": None,
            "components": {},
            "features": {},
            "exclusion_reasons": list(reasons),
            "verified_hard_fit_count": verified_fits,
            "trust": trust_score,
            "price": _price_of(offer),
            "trust_summary": _shortlist.trust_summary(store_id, row_of_store),
            "provenance_labels": _shortlist.provenance_labels(read(candidate, "claims", None)),
        }
        if row["eligible"]:
            rank_score, components, features = score(candidate, trust_score, weights)
            row["rank_score"] = rank_score
            row["components"] = components
            row["features"] = features
        rows.append(row)

    tie_breakers = tuple(str(name) for name in weights.tie_breakers)
    ranked = sorted(
        (row for row in rows if row["eligible"]),
        key=lambda row: _sort_key(row, tie_breakers),
    )

    return {
        "ranked": ranked,
        "candidates": rows,
        "shortlist": _shortlist.build(ranked, _auction_id(candidates, intent, config)),
    }


__all__ = [
    "EXCLUSION_REASON_PREFIXES",
    "FALLBACK_AUCTION_ID",
    "REASON_BLACKLISTED",
    "REASON_BLACKLIST_UNREADABLE",
    "REASON_ELIGIBILITY_DENIED",
    "REASON_EXPIRED",
    "REASON_HARD_CONSTRAINT",
    "REASON_MALFORMED",
    "REASON_OFF_DOMAIN",
    "REASON_UNDECIDABLE_INTENT",
    "TIE_BREAK_DIRECTIONS",
    "rank",
]
