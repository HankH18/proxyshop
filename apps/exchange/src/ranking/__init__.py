"""T-032 — the published ranking: eligibility filters, one formula, one shortlist.

`rank(candidates, intent, trust_snapshot, config)` is the whole public surface. It is four
positional arguments because every caller in the system passes exactly those four, and the
result is a plain mapping::

    {
      "ranked":     [row, ...],           # eligible rows, best first
      "candidates": [row, ...],           # EVERY row, in the order they were given
      "shortlist":  {"auction_id": ..., "slots": [slot, ...]},
      "relaxed_constraints": [ ... ],     # normally empty; see below
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

A filter that excludes everyone
-------------------------------
R19's rule is decided per candidate, and per candidate it is right: an attribute the
candidate carries no verified reading for does not satisfy a constraint. But "not satisfied"
and "unanswerable" are the same verdict about ONE candidate and opposite facts about a SET
of them, and only this module sees the set. A constraint no candidate in the auction carries
any reading for excludes all of them, and what the shopper is shown is "no stores matched"
when the truth is "nobody here could answer the question you asked".

That is not hypothetical. Measured through this exchange's own `POST /auctions` with the S1
roster: `brew_method eq espresso` — a buyer saying "espresso" — produced **0 slots**, and so
did `list_price lte 500`, `boiler_type eq 'heat exchange'` and `roast_level eq dark`, three
attributes `fixtures/catalog/coffee.json` declares and the stores' own catalogue rows carry.
The deciding fact is never what a catalogue CONFIG names; it is what the candidates in
*this* auction carry as verified readings, which is a fact no upstream service holds.

So one narrow relaxation lives here, and its guards are in :func:`rank`. A constraint is set
aside only when no catalogue snapshot THIS EXCHANGE holds declares the attribute and no
candidate's claim about it came back `verified` or `contradicted` — never one a candidate
merely failed, and never one it was caught contradicting — and only when the caller could tell
us what its catalogues declare (`network_attributes`), only when nothing is eligible without
it, only when the intent itself was readable, and only when setting it aside actually fills a
slot. The buyer is never quietly given a shortlist that ignores a must-have: every set-aside
constraint is published verbatim, with its reason, under `relaxed_constraints`, and it is NOT
counted in `verified_hard_fit_count`, so no store wins the D13 tie-break on a constraint
nobody proved.

`config` supplies `now` (so nothing here reads the wall clock) and optionally `auction_id`.
`weights` and `eligibility` are keyword-only extras with inert defaults: the published
surface is the four positionals, and an optional collaborator must never be something a
caller has to know about to get a correct answer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from contracts.ranking import (
    DEFAULT_RANKING_WEIGHTS,
    RANKING_FEATURES_VERSION,
    RankingWeights,
)

from . import shortlist as _shortlist
from .filters import (
    exclusion_reasons,
    offer_price,
    read,
    read_criteria,
    trust_row,
    unanswerable_criteria,
    unanswerable_reason,
)
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


#: The offer's price for the published price tie-break. It lives in :mod:`.filters` now
#: because the budget filter decides the buyer's ceiling against the same number, and a price
#: this module read one way and the gate read another would be two prices — the one a
#: candidate is refused on and the one it is ordered by. Kept under the old private name so
#: nothing that already reads it has to learn a second one.
_price_of = offer_price


def _sort_key(row: Mapping[str, Any], tie_breakers: Sequence[str]) -> tuple:
    """`(-rank_score, *published tie-breakers)` for one eligible row.

    The tie-breakers come from the weight set rather than from a literal list here, so a
    deployment that publishes a different order gets that order applied instead of this
    module's opinion of it.
    """
    # Belt and braces against a non-finite score. `scoring._number` refuses NaN at the edge
    # so this cannot fire today, but the cost of being wrong here is not a wrong order — it
    # is an INCONSISTENT comparator, and `sorted()` given one returns an order that depends
    # on input position. A guard that keeps the comparator total is worth one line.
    raw_score = float(row["rank_score"])
    key: list[Any] = [-raw_score if math.isfinite(raw_score) else math.inf]
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
        number = None if value is None else float(value)
        if number is None or not math.isfinite(number):
            # An unreadable tie-break value sorts LAST whichever way the field sorts. The
            # alternative — reading it as 0.0 — would make an unknown price the cheapest one
            # in the auction, and a NaN one would make the comparator inconsistent.
            key.append(math.inf)
            continue
        key.append(direction * number)
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
    network_attributes: Sequence[Mapping[str, Any]] | None = None,
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
        network_attributes: which attributes this exchange's own catalogue snapshots declare
            for the stores in this auction (`{"key": ...}` mappings, as
            :func:`~exchange.ranking.verification.declared_attributes` builds them). The
            DEFAULT IS `None`, meaning "the caller cannot say" — and then nothing is ever
            relaxed, so every existing four-argument caller keeps the answer it had. Only a
            caller holding the catalog can say, which is why the served path
            (:func:`~exchange.ranking.serving.rank_auction`) is the one that passes it.

    Returns:
        `{"ranked": [...], "candidates": [...], "shortlist": {"auction_id", "slots"},
        "relaxed_constraints": [...]}`. The inputs are never written to.

        `relaxed_constraints` is normally empty. It is non-empty only in the one case
        described under "A filter that excludes everyone" above, and each entry names a
        constraint that was NOT applied and says why.
    """
    weights = DEFAULT_RANKING_WEIGHTS if weights is None else weights
    candidates = list(candidates or ())
    now = _now_from(config)
    criteria, intent_reason = read_criteria(intent)

    def rows_for(applied: Sequence[Any]) -> list[dict[str, Any]]:
        built: list[dict[str, Any]] = []
        for candidate in candidates:
            reasons, verified_fits = exclusion_reasons(
                candidate,
                criteria=applied,
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
                        candidate_trust = float(raw_trust)
                    except (TypeError, ValueError):
                        candidate_trust = None
                    # A snapshot score that is not a finite number is unreadable, not a
                    # number. Leaving it as NaN here would put NaN in the `trust` tie-break as
                    # well as in the score, and both comparators need it to be a real number
                    # or absent.
                    if candidate_trust is not None and math.isfinite(candidate_trust):
                        trust_score = candidate_trust
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
            built.append(row)
        return built

    rows = rows_for(criteria)
    relaxed: list[dict[str, Any]] = []

    # A filter that excludes EVERYONE, and the one condition under which it is set aside.
    #
    # FIVE guards, and every one of them has to hold. Together they say: relax only a
    # constraint that was never a filter in the first place, only when applying it left the
    # buyer with nothing, and only when setting it aside actually gives them something.
    #
    #  0. The CALLER HOLDS THE CATALOGUE and it declares something. `network_attributes` of
    #     `None` — every four-argument caller, and any served exchange whose catalog is not
    #     wired — relaxes nothing at all. An exchange that can verify nothing has discovered a
    #     misconfiguration, not an unanswerable question, and ESC-020 already settled which
    #     way that fails: it satisfies no hard constraint and shortlists nobody.
    #  1. NOTHING is eligible. One surviving candidate means the filters are narrowing rather
    #     than emptying, and a narrowed shortlist is the correct answer — nothing is relaxed.
    #  2. The intent was READABLE. `intent_reason` is "this exchange could not parse your
    #     constraints", which denies everyone on purpose (R19); relaxing it would turn an
    #     unreadable intent into an unconstrained one, which is the exact confusion
    #     `read_criteria` exists to prevent.
    #  3. The constraint is UNDECIDABLE FOR EVERYONE — no catalogue snapshot this exchange
    #     holds declares the attribute, and no candidate's claim about it came back `verified`
    #     or `contradicted` — not merely failed by them. A constraint some store was GRADED on
    #     stays a filter for all of them, so a store that fails a must-have (or is caught
    #     contradicting one) is still excluded. Note it is the VERDICT that decides this and
    #     not the fact of a claim: a claim nothing could check is this exchange reporting its
    #     own gap, and reading it as "the question was answerable" let one store's honest
    #     sentence empty the shortlist for every store in the auction.
    #  4. Setting them aside CHANGES the answer. If the shortlist is empty because everyone
    #     is blacklisted, off-domain or expired, the relaxed pass is empty too and nothing is
    #     published — a relaxation nobody benefited from is a claim about a filter that was
    #     never the reason.
    if criteria and intent_reason is None and not any(row["eligible"] for row in rows):
        unanswerable = unanswerable_criteria(candidates, criteria, network_attributes)
        if unanswerable:
            kept = [criterion for criterion in criteria if criterion not in unanswerable]
            retried = rows_for(kept)
            if any(row["eligible"] for row in retried):
                rows = retried
                relaxed = [
                    {
                        "field": criterion.field,
                        "op": criterion.op,
                        "value": criterion.value,
                        "reason": unanswerable_reason(criterion),
                    }
                    for criterion in unanswerable
                ]

    tie_breakers = tuple(str(name) for name in weights.tie_breakers)
    ranked = sorted(
        (row for row in rows if row["eligible"]),
        key=lambda row: _sort_key(row, tie_breakers),
    )

    return {
        "ranked": ranked,
        "candidates": rows,
        "shortlist": _shortlist.build(ranked, _auction_id(candidates, intent, config)),
        "relaxed_constraints": relaxed,
        # BOTH versions, because a score is reproducible from neither alone (R15/S3). The
        # weights version says which numbers were applied; the features version says what they
        # were applied TO, and a feature redefined under an unchanged weights version re-scores
        # history with every recorded number still validating and every published weight still
        # matching. A replay that compares only the weights version cannot tell that happened.
        "ranking_versions": {
            "weights": str(weights.version),
            "features": RANKING_FEATURES_VERSION,
        },
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
