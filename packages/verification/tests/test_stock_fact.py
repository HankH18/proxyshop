"""The binary in-stock fact, on a CRAWL-SHAPED catalogue (D59).

The defect this file grades. ``in_stock`` — what every producer of a stock claim writes: the
store agent's live feed, the pitch decomposer's two flag rules, the live-page reader — and
``availability`` — what the crawl writes onto the graph ``Offer`` — are two names for one fact,
and nothing joined them. ``_lookup_attribute`` matches on ``str(key)``, the crawl writes **zero**
``AttributeValue`` nodes so the snapshot's ``attributes`` block is empty for every crawled
product, and the result was that every stock claim resolved nowhere: ``unsupported`` from the
verifier, rewritten to ``ambiguous`` by the exchange as its own gap, costing a lying store
nothing and proving nothing about an honest one.

The ruling is D59 and it is narrow on purpose — the owner's words: *"it's not our job to
perfectly track their inventory. We just need to know if the product is in stock or not."* So:
a BINARY fact, derived from the availability token the platform itself defined, and held to a
freshness window because a shelf is live state and a roast level is not.

Both directions are graded here and both must hold. A store whose stock moved since the crawl
must NOT be called a liar; a store claiming stock the platform's current reading says it does
not have must still be caught.
"""

from __future__ import annotations

from typing import Any

import pytest
from claim_verification.verifier import (
    IN_STOCK_BY_AVAILABILITY,
    STOCK_EVIDENCE_WINDOW_DAYS,
    catalog_keys,
    verify,
)

VERIFIER_VERSION = "test-1"
PRODUCT = "prod-1"

#: The crawl's clock format — ``ingest.scheduler.catalog.observed_now``'s.
CRAWLED_AT = "2026-09-07T12:00:00Z"

#: Twenty minutes after the crawl: inside the one-hour cadence the platform publishes for
#: ``offer.availability``, so its reading is current and it may grade.
READ_FRESH = "2026-09-07T12:20:00Z"

#: A day after the crawl: the platform has not looked since, so its reading is not current.
READ_LATE = "2026-09-08T12:00:00Z"

#: The stamp the crawl writes on the Offer node. It is a last-CHANGED time, not a
#: last-CONFIRMED one, and every test here leaves it far in the past on purpose: nothing may
#: read it as the age of the platform's evidence.
OFFER_LAST_CHANGED = "2026-06-01T09:00:00Z"


def _crawled(
    availability: Any,
    *,
    captured_at: str = CRAWLED_AT,
    read_at: str | None = None,
    attributes: dict[str, Any] | None = None,
    freshness_window_days: float | None = None,
) -> dict[str, Any]:
    """A snapshot shaped exactly as ``GraphCatalogSnapshots.as_snapshot`` builds one.

    An EMPTY ``attributes`` block, because that is what the recorded corpus crawl produces —
    ``ingest.adapters.mapping.build_upserts`` emits no ``attribute`` op at all — and the offer
    block as bare scalars carrying the availability token and its own ``observed_at``.

    ``captured_at`` is when the platform last OBSERVED this pair and ``read_at`` is when the
    exchange read the snapshot; the age of the evidence is the gap between them. ``read_at`` of
    ``None`` is the hand-authored-document shape — a stated catalogue asserts a current reading
    and there is nothing to measure it against.
    """
    offer: dict[str, Any] = {
        "price": 28.0,
        "currency": "USD",
        "availability": availability,
        "observed_at": OFFER_LAST_CHANGED,
    }
    snapshot: dict[str, Any] = {
        "snapshot_id": f"neo4j-crawl:store-1:{PRODUCT}",
        "captured_at": captured_at,
        "products": [
            {
                "product_ref": PRODUCT,
                "canonical_name": "Merino beanie",
                "brand": "Northerly",
                "status": "active",
                "attributes": dict(attributes or {}),
                "offer": offer,
            }
        ],
    }
    if read_at is not None:
        snapshot["read_at"] = read_at
    if freshness_window_days is not None:
        snapshot["freshness_window_days"] = freshness_window_days
    return snapshot


def _graded(snapshot: dict[str, Any], claims: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    pitch = {
        "pitch_id": "pitch-1",
        "store_id": "store-1",
        "product_ref": PRODUCT,
        "text": "",
        "claims": [dict(claim, claim_ref=str(claim["key"])) for claim in claims],
    }
    return {row["key"]: row for row in verify(pitch, snapshot, VERIFIER_VERSION)["claims"]}


def _stock(value: Any) -> dict[str, Any]:
    return {"key": "in_stock", "value": value}


# =====================================================================================
# The fact is decidable at all — which is the defect
# =====================================================================================
def test_a_crawled_availability_makes_the_stock_fact_decidable() -> None:
    """The regression. Before D59 this key resolved nowhere on a crawl-shaped snapshot."""
    assert "in_stock" in catalog_keys(_crawled("in_stock"), PRODUCT), (
        "the exchange asks this function what it can decide; while `in_stock` was absent from "
        "the answer every stock claim was rewritten to `ambiguous` as the exchange's own gap"
    )


def test_the_honest_store_is_verified_and_the_liar_is_contradicted() -> None:
    """Both directions, same key, same claim, opposite crawls."""
    in_stock = _graded(_crawled("in_stock"), [_stock(True)])["in_stock"]
    sold_out = _graded(_crawled("out_of_stock"), [_stock(True)])["in_stock"]

    assert in_stock["status"] == "verified", in_stock["reason"]
    assert in_stock["observed_value"] is True
    assert sold_out["status"] == "contradicted", sold_out["reason"]
    assert sold_out["observed_value"] is False


def test_the_claim_is_typed_so_the_verdict_reaches_a_trust_dimension() -> None:
    """An untyped verdict is announced to nobody: ``claim_verdict_payload`` returns ``None``
    and ``trust.scoring.claim_dimension`` raises. ``specifications`` is the type
    ``pitch._FlagRule`` has always minted for a stock reading read out of prose, and an
    asserted claim must earn and cost exactly what a prose one does."""
    graded = _graded(_crawled("out_of_stock"), [_stock(True)])["in_stock"]
    assert graded["claim_type"] == "specifications"


@pytest.mark.parametrize("token", ["in_stock", "limited", "available", "InStock", "IN STOCK"])
def test_the_tokens_that_read_as_in_stock(token: str) -> None:
    assert _graded(_crawled(token), [_stock(True)])["in_stock"]["status"] == "verified"


@pytest.mark.parametrize("token", ["out_of_stock", "soldout", "sold out", "OutOfStock"])
def test_the_tokens_that_read_as_out_of_stock(token: str) -> None:
    assert _graded(_crawled(token), [_stock(True)])["in_stock"]["status"] == "contradicted"


# =====================================================================================
# The honest store, protected in every direction the ruling names
# =====================================================================================
@pytest.mark.parametrize("token", ["preorder", "backorder", "discontinued", "unknown", "", None])
def test_a_token_with_no_binary_reading_accuses_nobody(token: Any) -> None:
    """``unsupported`` — absence of evidence — and never ``contradicted``.

    Each of these is a real value the crawl writes. ``preorder``/``backorder`` describe a
    purchase filled later, so "is it in stock" is not a question they answer; ``discontinued``
    is kept separate from ``out_of_stock`` by the crawl's own vocabulary because a discontinued
    line can still have units; ``unknown`` is what ``coerce_availability`` assigns to a token
    it did not recognise, and reading the platform's own parser gap as "out of stock" would
    manufacture a contradiction against a store that did nothing wrong.
    """
    graded = _graded(_crawled(token), [_stock(True)])["in_stock"]
    assert graded["status"] == "unsupported", graded["reason"]
    assert "in_stock" not in catalog_keys(_crawled(token), PRODUCT), (
        "the vocabulary must agree with the resolution rule: reporting a key as decidable that "
        "`verify` then answers `unsupported` for is worse than reporting no vocabulary at all"
    )


def test_a_reading_the_platform_has_not_refreshed_can_neither_support_nor_contradict() -> None:
    """The honest seller whose stock MOVED since the crawl, which is the case a naive alias
    gets wrong. The platform last looked at this shelf a day before it graded the claim, so its
    own reading is not current and it declines to decide in EITHER direction rather than
    accusing a store of lying about its own inventory.
    """
    restocked = _crawled("out_of_stock", read_at=READ_LATE)
    sold_out = _crawled("in_stock", read_at=READ_LATE)

    honest = _graded(restocked, [_stock(True)])["in_stock"]
    assert honest["status"] == "unsupported", honest["reason"]
    assert "freshness window" in honest["reason"]

    # And symmetrically: a stale reading cannot VERIFY either. A window that only protected
    # sellers would be a window that let a liar bank an ancient "in stock".
    assert _graded(sold_out, [_stock(True)])["in_stock"]["status"] == "unsupported"


def test_a_fresh_reading_inside_the_window_still_decides() -> None:
    """The other half of the freshness rule, and the one that keeps it from being vacuous.

    Twenty minutes since the platform last looked is inside the crawl's own published cadence
    for ``offer.availability``, so the platform grades it.
    """
    assert STOCK_EVIDENCE_WINDOW_DAYS == pytest.approx(3600.0 / 86400.0)
    recent = _crawled("out_of_stock", read_at=READ_FRESH)
    assert _graded(recent, [_stock(True)])["in_stock"]["status"] == "contradicted"


def test_the_offers_own_last_changed_stamp_is_never_read_as_the_evidences_age() -> None:
    """``Offer.observed_at`` is when the shelf last MOVED, not when the platform last CONFIRMED
    it: ``build_upserts`` does not rewrite an unchanged product's Offer. Reading it as the
    confirmation time would retire the grading of every steady product within one cadence
    interval while leaving churning ones graded — the exact inverse of what the floor is for.

    Here the offer has not changed since June and the platform looked twenty minutes ago.
    """
    fresh_crawl_of_an_old_offer = _crawled("out_of_stock", read_at=READ_FRESH)
    assert fresh_crawl_of_an_old_offer["products"][0]["offer"]["observed_at"] == OFFER_LAST_CHANGED
    graded = _graded(fresh_crawl_of_an_old_offer, [_stock(True)])["in_stock"]
    assert graded["status"] == "contradicted", graded["reason"]


def test_an_operator_window_may_tighten_the_stock_rule_but_never_loosen_it() -> None:
    """A policy written for roast levels must not declare a fortnight-old shelf current."""
    stale = _crawled("out_of_stock", read_at=READ_LATE, freshness_window_days=14.0)
    assert _graded(stale, [_stock(True)])["in_stock"]["status"] == "unsupported", (
        "a 14-day operator policy reopened the hole the stock floor closes"
    )

    tight = _crawled("out_of_stock", read_at=READ_FRESH, freshness_window_days=1.0 / 1440.0)
    assert _graded(tight, [_stock(True)])["in_stock"]["status"] == "unsupported", (
        "an operator who said 'nothing older than a minute' was overruled by the floor"
    )


def test_a_stated_document_with_no_read_time_is_taken_as_current() -> None:
    """Every hand-authored catalogue in the tree — the demo deployment, the devstack, the
    fixtures, the frozen acceptance suite — states an availability and no ``read_at``. There is
    nothing to measure an age against, and inventing one would silently stop grading all of
    them."""
    assert "read_at" not in _crawled("out_of_stock")
    assert _graded(_crawled("out_of_stock"), [_stock(True)])["in_stock"]["status"] == "contradicted"


# =====================================================================================
# What the derivation is NOT
# =====================================================================================
def test_a_stated_in_stock_attribute_wins_over_the_derivation() -> None:
    """The demo document and the S1 fixture both state ``in_stock`` outright. What a snapshot
    STATES is answered by what it states — the derivation is a fallback, not an override."""
    conflicting = _crawled("out_of_stock", attributes={"in_stock": {"value": True}})
    assert _graded(conflicting, [_stock(True)])["in_stock"]["status"] == "verified"


def test_the_other_spelling_cannot_route_around_the_freshness_floor() -> None:
    """One fact, two spellings, and the floor has to reach both or it reaches neither.

    The offer block is written as bare scalars and ``_is_stale`` skips anything that is not a
    Mapping, so before this a store claiming ``availability: "in_stock"`` banked a ``verified``
    off a day-old reading while the same store claiming ``in_stock: true`` got ``unsupported``.
    """
    stale = _crawled("in_stock", read_at=READ_LATE)
    graded = _graded(stale, [{"key": "availability", "value": "in_stock"}, _stock(True)])
    assert graded["availability"]["status"] == "unsupported", graded["availability"]["reason"]
    assert graded["in_stock"]["status"] == "unsupported"


def test_availability_keeps_its_own_spelling_and_its_own_verdict() -> None:
    """This is a derived reading, not a synonym table. ``retrieval/catalogue.py`` forbids the
    latter — "the platform states what it observed under the name it observed it under" — and
    a claim naming ``availability`` is still graded against the verbatim token."""
    graded = _graded(
        _crawled("in_stock"),
        [{"key": "availability", "value": "in_stock"}, _stock(True)],
    )
    assert graded["availability"]["status"] == "verified"
    assert graded["availability"]["observed_value"] == "in_stock"
    assert graded["availability"]["claim_type"] is None, (
        "typing an `availability` claim would route it to a dimension nobody approved; only "
        "the platform's own `in_stock` reading is typed"
    )
    assert graded["in_stock"]["observed_value"] is True


def test_the_ruling_is_binary_and_does_not_reconcile_quantity() -> None:
    """The owner's scope, kept: a units count is not derivable from a stock token, and D59
    does not invent one. ``units_left`` resolves where it always did — the attributes block —
    and is untouched by this."""
    graded = _graded(_crawled("in_stock"), [{"key": "units_left", "value": 12}])["units_left"]
    assert graded["status"] == "unsupported", graded["reason"]
    assert "units_left" not in catalog_keys(_crawled("in_stock"), PRODUCT), (
        "a crawled availability token was read as a units count"
    )
    assert set(IN_STOCK_BY_AVAILABILITY.values()) == {True, False}, (
        "the derived reading is binary; a third outcome would be a quantity model in disguise"
    )


def test_a_snapshot_that_flags_its_own_evidence_stale_is_not_laundered_by_the_derivation() -> None:
    """The repo's own stale-evidence gate fixture, ``gp-002-stale-evidence``, carries a
    ``stale: true`` availability row in ``attributes`` beside an unflagged copy in the offer
    block. A derivation that read the offer block there answered ``verified`` off a June
    reading for ``in_stock`` while answering ``unsupported`` for ``availability`` — one
    document, one fact, opposite verdicts, and the seller credited with the fresh confirmation
    the fixture exists to deny. Its own rationale: *"Stale evidence must not be silently
    treated as fresh confirmation."*
    """
    flagged = _crawled(
        "in_stock",
        attributes={
            "availability": {
                "value": "in_stock",
                "observed_at": "2026-06-02T09:00:00Z",
                "stale": True,
            }
        },
        freshness_window_days=14.0,
    )
    graded = _graded(flagged, [{"key": "availability", "value": "in_stock"}, _stock(True)])
    assert graded["availability"]["status"] == "unsupported", graded["availability"]["reason"]
    assert graded["in_stock"]["status"] == "unsupported", graded["in_stock"]["reason"]
    assert "in_stock" not in catalog_keys(flagged, PRODUCT)


def test_a_product_flagged_stale_cannot_be_regraded_through_the_other_spelling() -> None:
    """The same rule one level up: a ``stale`` flag on the product, or on the snapshot, reaches
    the derived reading too."""
    for where in ("product", "snapshot"):
        snapshot = _crawled("out_of_stock", read_at=READ_FRESH)
        if where == "product":
            snapshot["products"][0]["stale"] = True
        else:
            snapshot["stale"] = True
        graded = _graded(snapshot, [_stock(True)])["in_stock"]
        assert graded["status"] == "unsupported", f"{where}: {graded['reason']}"


def test_a_stale_reading_is_dropped_from_the_vocabulary_so_it_costs_the_store_nothing() -> None:
    """A gap on the PLATFORM's side must not be a cost on the store's.

    ``catalog_keys`` is what the exchange asks "could you decide this key"; an ``unsupported``
    on a key inside that vocabulary is a real cost (it lands in ``verified_claim_ratio``'s
    denominator with no gain), while one outside it is rewritten to ``ambiguous`` — this
    exchange's own gap — and is worth exactly what silence is worth. A reading the platform
    could not keep current is the platform's gap, so the key leaves the vocabulary with it.
    """
    assert "in_stock" in catalog_keys(_crawled("out_of_stock", read_at=READ_FRESH), PRODUCT)
    assert "in_stock" not in catalog_keys(_crawled("out_of_stock", read_at=READ_LATE), PRODUCT)


def test_both_spellings_leave_the_vocabulary_together_when_the_reading_is_stale() -> None:
    """They are one fact, so the cost of a platform-side gap cannot depend on which word the
    store used. Dropping ``in_stock`` while leaving ``availability`` would just move the
    ``verified_claim_ratio`` penalty onto the other spelling."""
    fresh = _crawled("out_of_stock", read_at=READ_FRESH)
    stale = _crawled("out_of_stock", read_at=READ_LATE)

    assert {"in_stock", "availability"} <= catalog_keys(fresh, PRODUCT)
    assert not ({"in_stock", "availability"} & catalog_keys(stale, PRODUCT)), (
        "a stale reading was still offered to the exchange as decidable under one of its names"
    )
    # And the other offer keys are untouched: only LIVE state expires.
    assert {"price", "currency"} <= catalog_keys(stale, PRODUCT)
