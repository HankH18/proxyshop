"""The buyer-facing shortlist — up to four differentiated slots (R2/A6/D29/D30).

The shortlist is not "the top four rows of the ranking". Three rules make it a different
object, and each of them is a promise to the buyer rather than an implementation detail:

* **One store per slot.** A shortlist whose four slots are four bids from one store offers
  the buyer one choice wearing four hats. Each store contributes its own best bid and
  nothing more.
* **Four different reasons to pick.** The slot NAMES are `fit`, `value`, `reliability` and
  `specialist` (D29) — labels for *why this one is here*, never terms of the rank formula
  (D50). Each slot goes to the candidate that leads on that slot's dimension, so a buyer
  reading four slots is reading four arguments rather than one argument four times.
* **It collapses rather than pads.** One eligible store yields one slot. There is no
  filling-out with a store that failed a filter, because the filters are eligibility, not
  preference: a candidate excluded for a blacklist or a contradicted hard constraint is not
  a worse choice, it is not a choice.

Slot order is rank order, so the shortlist opens with the ranking's leader; the slot NAME it
carries is whichever dimension it leads on. The two are separate on purpose — the leader is
usually, but not always, the best fit.

The assembled object is validated through the pinned `Shortlist` / `ShortlistSlot` contract
types before it is returned, so a slot this package builds is a slot the buyer app can read.
It is returned as plain data rather than as the model, because every caller of `rank()`
reads the result by subscripting it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from contracts.labels import LABEL_UNVERIFIED, buyer_label
from contracts.protocol import Shortlist, ShortlistSlot, ShortlistSlotName

from .filters import read

#: D29's four slot names, in the order they are handed out. `fit` first because the leading
#: reason to show a candidate is that it answers the question that was asked.
SLOT_NAMES: tuple[str, ...] = tuple(name.value for name in ShortlistSlotName)

#: The published feature each slot is the leader of. `specialist` reads
#: `verified_claim_ratio`: the store that stands behind the most of what it claims is the
#: one whose speciality is evidenced rather than asserted.
SLOT_DIMENSIONS: dict[str, str] = {
    ShortlistSlotName.fit.value: "intent_match",
    ShortlistSlotName.value.value: "price_value",
    ShortlistSlotName.reliability.value: "trust",
    ShortlistSlotName.specialist.value: "verified_claim_ratio",
}

#: R2/A6 cap. Four is a shortlist; more is a search results page.
MAX_SLOTS = 4


def provenance_labels(claims: Any) -> list[str]:
    """The buyer-facing provenance labels for one candidate's claims (D30).

    The label map is `packages/contracts`' — imported, never restated — precisely so the
    exchange that produces these strings and the buyer app that renders them cannot drift.
    A source with no published label is skipped rather than guessed at; a candidate whose
    every source is unknown still carries the honest `unverified` label rather than an empty
    list, because a slot with no provenance at all reads as a slot with nothing to hide.
    """
    labels: list[str] = []
    for claim in claims or ():
        provenance = read(claim, "provenance", None)
        if provenance is None:
            continue
        try:
            label = buyer_label(provenance)
        except KeyError:
            continue
        if label not in labels:
            labels.append(label)
    if not labels:
        return [LABEL_UNVERIFIED]
    return sorted(labels)


def trust_summary(store_id: str, trust_row: Any) -> dict[str, Any]:
    """The slot's trust summary: the snapshot's own numbers, not a re-derivation.

    A slot with an empty summary would be a slot claiming a trust story it cannot show, so
    the store id is always present even when the snapshot carried nothing else.
    """
    summary: dict[str, Any] = {"store_id": store_id}
    if trust_row is None:
        summary["available"] = False
        return summary
    summary["available"] = True
    for field in ("score", "confidence"):
        value = read(trust_row, field, None)
        if value is not None:
            summary[field] = float(value)
    low_data = read(trust_row, "low_data", None)
    if low_data is not None:
        summary["low_data"] = bool(low_data)
    dims = read(trust_row, "dims", None)
    if dims is not None:
        summary["dimensions"] = sorted(str(key) for key in dims)
    return summary


def _best_bid_per_store(ranked: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The leading bid from each distinct store, keeping the ranking's order.

    Distinct by `bid_id` as well as by `store_id`. Two rows sharing a `bid_id` are two bids
    the exchange cannot tell apart, and a shortlist naming the same `bid_ref` twice is a
    shortlist whose slots cannot be resolved back to offers — a buyer clicking either one
    reaches an ambiguous bid. Ranking still reports both rows; only the slots are deduped,
    because inventing an eligibility rule for a shape the spec does not describe would be a
    bigger change than keeping the published invariant.
    """
    seen_stores: set[str] = set()
    seen_bids: set[str] = set()
    pool: list[dict[str, Any]] = []
    for row in ranked:
        store_id = str(row["store_id"])
        bid_id = str(row["bid_id"])
        if store_id in seen_stores or bid_id in seen_bids:
            continue
        seen_stores.add(store_id)
        seen_bids.add(bid_id)
        pool.append(row)
    return pool


def assign_slot_names(pool: Sequence[dict[str, Any]]) -> list[str | None]:
    """One slot name per pool member, in pool order — each gets the name it leads on.

    Greedy over :data:`SLOT_NAMES` in order: each name goes to the highest-scoring unclaimed
    member on that name's dimension, with the ranking's own order breaking a draw. That
    ordering is what makes the assignment deterministic; a tie broken by dictionary order
    would make the slot labels depend on which candidate arrived first.

    The result is a LIST indexed by position, not a map keyed by `bid_id`. Two candidates can
    arrive carrying the same `bid_id` — the ranker is handed whatever the auction collected,
    and duplicate ids are exactly the kind of thing that arrives from a misbehaving fan-out —
    and a map keyed by id gave both of them one entry, so the second silently inherited the
    first's name and the shortlist published two slots called "value". Position is unique by
    construction; `bid_id` is not.
    """
    remaining = list(range(len(pool)))
    names: list[str | None] = [None] * len(pool)
    for name in SLOT_NAMES:
        if not remaining:
            break
        dimension = SLOT_DIMENSIONS[name]
        winner = max(
            remaining,
            key=lambda index: (float(pool[index]["features"].get(dimension, 0.0)), -index),
        )
        names[winner] = name
        remaining.remove(winner)
    # `None` only for a pool larger than the slot vocabulary, which `build` caps away. It is
    # returned rather than dropped so the result stays index-aligned with `pool`.
    return names


def slot_pool(
    ranked: Sequence[dict[str, Any]], *, max_slots: int = MAX_SLOTS
) -> list[dict[str, Any]]:
    """The rows that will actually FILL slots, in rank order — one store each, capped.

    Named and exported because a second reader needs the same answer: R12's exploration slice
    (:mod:`exchange.policy.exploration`) decides whether to spend the last of these on a
    low-data store, and it can only do that if "which rows would have been shown" is a question
    with one published answer rather than two implementations of the same slice arithmetic.
    """
    return _best_bid_per_store(ranked)[: min(max_slots, len(SLOT_NAMES))]


def bench(ranked: Sequence[dict[str, Any]], *, max_slots: int = MAX_SLOTS) -> list[dict[str, Any]]:
    """The eligible rows :func:`slot_pool` leaves out — one per store, in rank order.

    The complement of the pool over the SAME deduplication, so a store's second bid is never
    offered as a candidate for a slot its first bid already lost: a shortlist naming one store
    twice is the thing ``_best_bid_per_store`` exists to prevent, and exploration must not be
    the door that reintroduces it.
    """
    return _best_bid_per_store(ranked)[min(max_slots, len(SLOT_NAMES)) :]


def build(
    ranked: Sequence[dict[str, Any]], auction_id: str, *, max_slots: int = MAX_SLOTS
) -> dict[str, Any]:
    """The shortlist for one ranking, as plain data validated against the contract type."""
    pool = slot_pool(ranked, max_slots=max_slots)
    names = assign_slot_names(pool)

    slots = [
        ShortlistSlot(
            slot=ShortlistSlotName(name),
            bid_ref=str(row["bid_id"]),
            fit_score=float(row["rank_score"]),
            trust_summary=dict(row["trust_summary"]),
            provenance_labels=list(row["provenance_labels"]),
        )
        for row, name in zip(pool, names, strict=True)
        if name is not None
    ]
    return Shortlist(auction_id=auction_id, slots=slots).model_dump(mode="json")


__all__ = [
    "MAX_SLOTS",
    "SLOT_DIMENSIONS",
    "SLOT_NAMES",
    "assign_slot_names",
    "bench",
    "build",
    "provenance_labels",
    "slot_pool",
    "trust_summary",
]
