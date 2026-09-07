"""The auction log R9's loss reports are built from, and the thing that writes it.

:mod:`.loss` turns an auction log into per-store reports. Until this module existed **nothing
produced that log**: ``build_loss_report``'s only input anywhere in the repository was a test
fixture, so 454 lines of privacy-preserving projection graded data no request had ever made.
A route on top of that would have answered ``200`` with an empty report forever and looked
healthy, which is the failure this module exists to rule out rather than to decorate.

What one row is
===============
:func:`loss_rows` turns ONE closed auction into one row per candidate, and it writes the row
the way :mod:`.loss` documents its input: **everything, amounts included.** The rival's
identity, the winning price and the store's own offer are all on it, because that is what
makes the report's suppression a real property rather than an accident of an input that never
carried a secret. A producer that pre-emptied the row would leave layer 3 of
:func:`~.loss.build_loss_report`'s defence — the egress scan against the log's residue —
scanning against nothing.

Why a losing candidate's reason is decidable here and nowhere else
==================================================================
R9 wants ``fit`` / ``price`` / ``commitments`` / ``trust``, and the auction speaks two other
vocabularies: :mod:`exchange.ranking.reasons`' exclusion prefixes for a candidate that never
reached the formula, and the published rank COMPONENTS for one that did. Both are mapped here,
and the two mappings answer different questions:

``excluded``
    the candidate was filtered out, so the reason is *which filter*.
    :data:`EXCLUSION_CATEGORIES` maps every prefix
    :data:`~exchange.ranking.reasons.EXCLUSION_REASON_PREFIXES` publishes — the mapping is
    asserted total, so a new filter cannot quietly become an unreported loss.

``ranked but not shown``
    the candidate was scored and lost the slot to somebody, so the reason is *the axis it
    trailed on*. :data:`COMPONENT_CATEGORIES` maps the four published rank components onto the
    same four categories, and the loss is attributed to the component where this candidate fell
    furthest behind the best candidate in the auction.

**The second one is a comparison against a rival and it still leaks nothing**, which is the
distinction worth being explicit about: what leaves this function is the NAME of an axis, one
of four fixed words. The gap that chose it is computed here and discarded here. A merchant is
told "you lost on price", never by how much and never to whom.

A candidate that filled a shortlist slot is ``won=True`` and is skipped by the report.
"Winning" is winning a SLOT, not winning the auction, because R2's shortlist awards four slots
on four distinct axes (R11) — there is no single winner to be the complement of.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from contracts import LossReasons

from ..ranking.reasons import (
    REASON_BLACKLIST_UNREADABLE,
    REASON_BLACKLISTED,
    REASON_ELIGIBILITY_DENIED,
    REASON_EXPIRED,
    REASON_HARD_CONSTRAINT,
    REASON_MALFORMED,
    REASON_OFF_DOMAIN,
    REASON_OVER_BUDGET,
    REASON_PRICE_UNREADABLE,
    REASON_UNDECIDABLE_INTENT,
)

__all__ = [
    "COMPONENT_CATEGORIES",
    "DEFAULT_LOSS_LOG_CAPACITY",
    "EXCLUSION_CATEGORIES",
    "LossLog",
    "loss_rows",
    "record_losses",
]

#: The reason categories, taken from the contract rather than restated — the same import
#: :mod:`.loss` makes, for the same reason: a category this module invented would be aggregated
#: into a ``LossReasons`` that forbids it and 500 the merchant's own route.
_CATEGORIES: frozenset[str] = frozenset(LossReasons.model_fields)

#: Every exclusion prefix the ranker can emit, mapped onto one of R9's four categories.
#:
#: Two of these are worth defending, because the obvious reading is not the merchant's:
#:
#: ``expired_offer`` -> ``commitments``
#:     an expiry is a promise about how long the offer stands. A store whose offer died before
#:     the shortlist was built did not fail on fit and did not fail on price; it failed to stand
#:     behind the window it stated. Filing it under ``fit`` would tell a merchant to change its
#:     catalogue, which is the wrong repair.
#: ``off_domain_checkout`` -> ``commitments``
#:     same argument. The checkout host is the platform's registered record of where this store
#:     transacts, and a bid pointing somewhere else is a commitment the store did not keep.
#:
#: The mapping is asserted TOTAL against
#: :data:`~exchange.ranking.reasons.EXCLUSION_REASON_PREFIXES` by the gate, because an unmapped
#: prefix would silently become a loss nobody is told about — the quietest possible way for a
#: report to be wrong.
EXCLUSION_CATEGORIES: Mapping[str, str] = {
    REASON_BLACKLISTED: "trust",
    REASON_BLACKLIST_UNREADABLE: "trust",
    REASON_ELIGIBILITY_DENIED: "trust",
    REASON_EXPIRED: "commitments",
    REASON_OFF_DOMAIN: "commitments",
    REASON_HARD_CONSTRAINT: "fit",
    REASON_UNDECIDABLE_INTENT: "fit",
    REASON_MALFORMED: "fit",
    REASON_OVER_BUDGET: "price",
    REASON_PRICE_UNREADABLE: "price",
}

#: The four published rank components, mapped onto the same four categories. ``verified_claim_
#: ratio`` is ``commitments`` because it measures how much of what a store claimed it can stand
#: behind, which is the specialist slot's own dimension (``ranking.shortlist.SLOT_DIMENSIONS``).
COMPONENT_CATEGORIES: Mapping[str, str] = {
    "intent_match": "fit",
    "price_value": "price",
    "trust": "trust",
    "verified_claim_ratio": "commitments",
}

#: The category a scored candidate is filed under when nothing distinguishes it — every
#: component tied with the leader, or the row carried no readable components at all. ``fit`` is
#: the honest default: it is the axis a shortlist is for, and it is the one a merchant can act
#: on without being told anything about a rival.
_DEFAULT_CATEGORY = "fit"

#: How many rows one process keeps. A ring rather than a growing list, for the reason
#: ``ShortlistStore`` has a capacity: this is written on an unauthenticated path (``POST
#: /auctions``) once per rostered store per auction, so an unbounded log is a memory bound a
#: caller chooses. Old rows are dropped oldest-first; the window a report asks for is applied
#: over whatever is still held, and a report is aggregated and delayed by design (R9).
DEFAULT_LOSS_LOG_CAPACITY = 20_000


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _prefix(reason: Any) -> str:
    """The filter's name out of one exclusion reason. ``'name: detail'`` -> ``'name'``."""
    return _text(reason).split(":", 1)[0].strip()


def _exclusion_category(reasons: Sequence[Any]) -> str | None:
    """The category for the FIRST mapped exclusion reason, or ``None`` when none maps.

    First, not "the most severe": ``exclusion_reasons`` runs every filter rather than
    short-circuiting, and it appends them in a fixed order, so the first is the one nearest the
    top of that ladder. Ranking severity would be inventing an order the ranker does not have.
    """
    for reason in reasons:
        category = EXCLUSION_CATEGORIES.get(_prefix(reason))
        if category is not None:
            return category
    return None


def _trailing_component(row: Mapping[str, Any], best: Mapping[str, float]) -> str:
    """The axis this scored-but-unshown candidate fell furthest behind the field on.

    ``best`` is the auction's own maximum per component. The gap is computed and thrown away:
    only the axis NAME leaves this function, which is what keeps a comparison against a rival
    from being a fact about a rival.
    """
    components = row.get("components")
    if not isinstance(components, Mapping):
        return _DEFAULT_CATEGORY
    worst_gap = 0.0
    worst: str | None = None
    for name, category in COMPONENT_CATEGORIES.items():
        mine = _number(components.get(name))
        if mine is None:
            continue
        gap = best.get(name, mine) - mine
        if gap > worst_gap:
            worst_gap, worst = gap, category
    return worst if worst is not None else _DEFAULT_CATEGORY


def _best_components(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    best: dict[str, float] = {}
    for row in rows:
        components = row.get("components")
        if not isinstance(components, Mapping):
            continue
        for name in COMPONENT_CATEGORIES:
            mine = _number(components.get(name))
            if mine is None:
                continue
            if name not in best or mine > best[name]:
                best[name] = mine
    return best


def _shown_store_ids(shortlist: Any, candidates: Sequence[Mapping[str, Any]]) -> set[str]:
    """Which stores filled a slot, resolved through the rank rows rather than off the slot.

    **A published slot carries no ``store_id``.** ``ShortlistSlot`` names ``slot``, ``bid_ref``,
    ``fit_score``, ``trust_summary``, ``provenance_labels``, ``product``, ``price`` and
    ``commitments`` — the store is reachable only as ``trust_summary.store_id`` or by taking
    apart ``bid_ref``. Reading ``slot["store_id"]`` returns ``None`` for every slot, so every
    store looks like a loser and the winner is handed a report saying it lost; that is what the
    first version of this function did, and the gate caught it.

    So the join is on ``bid_ref`` against the rank rows' own ``bid_id`` — an identifier the
    exchange minted, matched against the rows it minted it for — with
    ``trust_summary.store_id`` as the fallback for a slot whose bid the caller did not pass.
    Splitting ``bid_ref`` on ``':'`` is deliberately NOT done: ``mint_bid_id`` happens to build
    ``auction:store`` today, and a report that silently mislabels every winner the day that
    changes is not worth the two saved lines.
    """
    slots = shortlist.get("slots") if isinstance(shortlist, Mapping) else None
    if not isinstance(slots, Sequence):
        return set()
    by_bid = {
        _text(row.get("bid_id")): _text(row.get("store_id"))
        for row in candidates
        if isinstance(row, Mapping) and _text(row.get("bid_id"))
    }
    shown: set[str] = set()
    for slot in slots:
        if not isinstance(slot, Mapping):
            continue
        store_id = by_bid.get(_text(slot.get("bid_ref")), "")
        if not store_id:
            summary = slot.get("trust_summary")
            if isinstance(summary, Mapping):
                store_id = _text(summary.get("store_id"))
        if store_id:
            shown.add(store_id)
    return shown


def _unmet_criteria(row: Mapping[str, Any], criteria: Sequence[Mapping[str, Any]]) -> list[str]:
    """The BUYER's own stated criteria this candidate failed, rendered as text.

    Built from the intent rather than parsed out of the ranker's prose, because the intent is
    the one source here that is guaranteed to contain nothing about a rival: it is what the
    shopper asked for, and it was written before any bid arrived. The ranker's reason string is
    used only to decide WHICH of the buyer's criteria to name — matched on the field's own
    spelling, which is also the buyer's word.

    Whatever this returns is still redacted by :func:`~.loss.build_loss_report` against the
    row's residue before it reaches a report, so a criterion that happened to quote an amount
    is dropped there. This function is not the privacy boundary; it is an input to one.
    """
    failed = [
        _text(reason)
        for reason in row.get("exclusion_reasons") or ()
        if _prefix(reason) == REASON_HARD_CONSTRAINT
    ]
    if not failed:
        return []
    blob = " ".join(failed).casefold()
    unmet: list[str] = []
    for criterion in criteria:
        field = _text(criterion.get("field")).strip()
        if not field or field.casefold() not in blob:
            continue
        rendered = f"{field} {_text(criterion.get('op')).strip()} {criterion.get('value')}".strip()
        if rendered not in unmet:
            unmet.append(rendered)
    return unmet


def loss_rows(
    *,
    auction_id: str,
    cluster_id: str,
    candidates: Sequence[Mapping[str, Any]],
    shortlist: Any,
    intent: Any,
    now: float,
) -> list[dict[str, Any]]:
    """One auction, as rows :func:`~.loss.build_loss_report` can aggregate.

    Args:
        auction_id: this auction's id. Carried on the row and deliberately NOT projected by the
            report, so it is part of the residue the egress scan measures against.
        cluster_id: the intent's assigned catalogue cluster — what R9 aggregates BY. An auction
            with no cluster produces no rows: a report grouped under ``""`` is a bucket no
            merchant can act on, and inventing a name for it would be worse.
        candidates: ``rank()``'s row projection, every candidate, eligible or not.
        shortlist: the built shortlist. Its slots decide ``won``.
        intent: the buyer's intent, read only for ``hard_constraints``.
        now: the instant the auction closed.

    Returns:
        One row per candidate carrying a store id, in candidate order. Rows carry the store's
        own offer price and the winning price — see the module docstring for why a producer
        that withheld them would weaken the report rather than strengthen it.
    """
    cluster = _text(cluster_id).strip()
    if not cluster:
        return []

    rows = [row for row in candidates if isinstance(row, Mapping) and _text(row.get("store_id"))]
    if not rows:
        return []

    shown = _shown_store_ids(shortlist, rows)
    best = _best_components(rows)
    criteria = [
        entry
        for entry in (intent.get("hard_constraints") if isinstance(intent, Mapping) else None) or ()
        if isinstance(entry, Mapping)
    ]
    winning_price = min(
        (
            price
            for row in rows
            if _text(row.get("store_id")) in shown
            for price in (_number(row.get("price")),)
            if price is not None
        ),
        default=None,
    )

    out: list[dict[str, Any]] = []
    for row in rows:
        store_id = _text(row.get("store_id"))
        won = store_id in shown
        if won:
            category = _DEFAULT_CATEGORY
        elif row.get("eligible"):
            category = _trailing_component(row, best)
        else:
            category = _exclusion_category(row.get("exclusion_reasons") or ()) or _DEFAULT_CATEGORY
        out.append(
            {
                "auction_id": auction_id,
                "store_id": store_id,
                "cluster_id": cluster,
                "reason": category,
                "ts": float(now),
                "won": won,
                "unmet_criteria": _unmet_criteria(row, criteria),
                # Everything below this line is what the report has to suppress. It is written
                # deliberately; see the module docstring.
                "offer_price": _number(row.get("price")),
                "rank_score": _number(row.get("rank_score")),
                "winning_price": winning_price,
                "shown_store_ids": sorted(shown),
            }
        )
    return out


class LossLog:
    """The loss rows this process is currently holding, oldest first.

    In-memory, bounded, and the same trade :class:`~exchange.ranking.serving.ShortlistStore`
    makes: the route that writes a row is the route that computes it, the container runs one
    uvicorn worker, and a loss row is derived data an auction can produce again. A deployment
    that wants reports to survive a restart replaces this object through
    :func:`~exchange.reports.routes.configure_reports`; the route asks only for ``extend`` and
    ``rows_for``.

    ``rows_for`` filters by store BEFORE the report is built, and that is a privacy property
    rather than an optimisation — it is what makes a merchant's report unable to contain
    another merchant's row at all. It is not the only one: :func:`~.loss.build_loss_report`
    also aggregates the rival amounts away. Two layers, because the first is an argument about
    control flow and the second is enforced by projection.
    """

    def __init__(self, *, capacity: int = DEFAULT_LOSS_LOG_CAPACITY) -> None:
        self.capacity = max(1, int(capacity))
        self._rows: deque[dict[str, Any]] = deque(maxlen=self.capacity)

    def extend(self, rows: Iterable[Mapping[str, Any]]) -> None:
        for row in rows:
            self._rows.append(dict(row))

    def rows_for(self, store_id: str) -> list[dict[str, Any]]:
        """Every held row this store is the SUBJECT of, oldest first.

        Copies, because a caller that mutated what it was handed would be rewriting this
        process's own audit trail.
        """
        wanted = str(store_id)
        return [dict(row) for row in self._rows if str(row.get("store_id")) == wanted]

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter([dict(row) for row in self._rows])

    def __len__(self) -> int:
        return len(self._rows)


def record_losses(
    log: Any,
    *,
    auction_id: str,
    cluster_id: str,
    candidates: Sequence[Mapping[str, Any]],
    shortlist: Any,
    intent: Any,
    now: float | None = None,
) -> int:
    """Write one auction's rows into ``log``. Returns how many were written.

    Duck-typed on ``extend`` and defensive about everything, because this is called from inside
    ``POST /auctions`` after the auction has already succeeded. A report is a merchant-facing
    convenience and an auction is a buyer-facing transaction; a bookkeeping failure here must
    never be the thing that fails a request the shopper is waiting on. An exchange with no log
    wired records nothing and serves an empty report, which is the honest answer for a
    deployment that has not asked for reports.
    """
    extend = getattr(log, "extend", None)
    if not callable(extend):
        return 0
    rows = loss_rows(
        auction_id=auction_id,
        cluster_id=cluster_id,
        candidates=candidates,
        shortlist=shortlist,
        intent=intent,
        now=time.time() if now is None else now,
    )
    if rows:
        extend(rows)
    return len(rows)
