"""The stock claim, graded on a crawl-shaped catalogue over ``POST /auctions`` (D59).

``packages/verification/tests/test_stock_fact.py`` grades the rule; this file drives it through
the served door, because a verifier that can decide a stock claim is worth nothing if the
exchange never asks it. The catalogue here is shaped exactly as ``GraphCatalogSnapshots``
builds one from the recorded corpus crawl: an EMPTY ``attributes`` block — the crawl writes no
``AttributeValue`` nodes at all — an ``offer`` block carrying the availability token, and the
``read_at`` stamp that says when this exchange read the graph. The age of the platform's
evidence is ``read_at`` minus ``captured_at`` and nothing else; every store's Offer node here
carries an ancient ``observed_at``, because that is a last-CHANGED stamp and nothing may read
it as a confirmation time.

Four stores, one request, and every direction the ruling has to survive:

* the honest store whose shelf matches the crawl — ``verified``, no penalty, shortlisted;
* the liar claiming stock the platform's current reading says it does not have — still
  ``contradicted``, still ``policy_penalties`` at the published 0.15;
* the honest store whose stock MOVED since the platform last looked — the reading is older
  than the crawl's own published refresh cadence, so the key leaves the decidable vocabulary
  with it and the exchange records its OWN gap (``ambiguous``): no penalty, no cost in
  ``verified_claim_ratio``, slot kept. This is the one a naive ``in_stock``/``availability``
  alias gets wrong, and getting it wrong means accusing a restocked seller of lying about its
  own inventory;
* the store that says it in PROSE rather than as a structured claim — graded identically,
  because a claim read out of a pitch earns and costs exactly what an asserted one does.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from exchange.auction.ledger import InMemoryLedgerSink
from exchange.auction.routes import configure_auctions
from exchange.auction.state import AuctionStateMachine
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.serving import configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from fastapi.testclient import TestClient

PRODUCT = "beanie-merino-01"
LIVE_FOR_AN_HOUR = 3600.0

#: When the exchange READ the graph — the moment it grades, stamped by
#: ``GraphCatalogSnapshots.as_snapshot``. The age of the platform's evidence is this minus the
#: crawl that produced it, and nothing else: ``captured_at`` is itself the latest observation
#: behind the entry, so a snapshot measured against its own contents is zero old at any age.
READ_AT = "2026-09-07T12:00:00Z"

#: Twenty minutes before the exchange looked — inside the one-hour cadence the platform
#: publishes for ``offer.availability``, so this reading is current and may grade.
CRAWLED_RECENTLY = "2026-09-07T11:40:00Z"

#: A day before the exchange looked. The platform has not refreshed this shelf since, so its
#: reading can neither support nor contradict.
CRAWLED_A_DAY_AGO = "2026-09-06T12:00:00Z"

#: When the Offer node last CHANGED. Deliberately ancient on every store: it is not a
#: confirmation time and nothing may read it as one.
OFFER_LAST_CHANGED = "2026-06-01T09:00:00Z"

HONEST = "store-honest"
LIAR = "store-liar"
RESTOCKED = "store-restocked"
PROSE = "store-prose"

#: The claim-type -> trust-dimension routing, without which no verdict is announced at all.
CLAIM_DIMENSIONS: dict[Any, str] = {
    "price": "price_honored",
    "specifications": "catalog_claim_accuracy",
    None: "catalog_claim_accuracy",
}

#: ``{store: (availability token, when the platform last crawled that shelf)}``.
SHELVES: dict[str, tuple[str, str]] = {
    HONEST: ("in_stock", CRAWLED_RECENTLY),
    LIAR: ("out_of_stock", CRAWLED_RECENTLY),
    RESTOCKED: ("out_of_stock", CRAWLED_A_DAY_AGO),
    PROSE: ("out_of_stock", CRAWLED_RECENTLY),
}


def _domain(store_id: str) -> str:
    return f"{store_id}.example.com"


def _crawled_snapshot(store_id: str) -> dict[str, Any]:
    availability, crawled_at = SHELVES[store_id]
    return {
        "snapshot_id": f"neo4j-crawl:{store_id}:{PRODUCT}",
        "captured_at": crawled_at,
        "read_at": READ_AT,
        "evidence_refs": [f"src:{store_id}"],
        "products": [
            {
                "product_ref": PRODUCT,
                "canonical_name": "Merino beanie",
                "brand": "Northerly",
                "status": "active",
                # EMPTY, as every crawled product's is. This is the whole scenario.
                "attributes": {},
                "offer": {
                    "price": 39.0,
                    "currency": "USD",
                    "availability": availability,
                    "observed_at": OFFER_LAST_CHANGED,
                },
            }
        ],
    }


def _bid(store_id: str) -> dict[str, Any]:
    """One bid. Every store but ``PROSE`` asserts the stock claim structurally; ``PROSE`` says
    the same thing in its pitch and asserts nothing."""
    claims: list[dict[str, Any]] = []
    message: str | None = None
    if store_id == PROSE:
        message = "In stock and ready to ship today."
    else:
        claims = [
            {
                "key": "in_stock",
                "value": True,
                "provenance": {
                    "source": "pixel_feed",
                    "ref": f"feed:{store_id}",
                    "authority_rank": 1,
                },
            }
        ]
    return {
        "auction_id": None,
        "store_id": store_id,
        "offer": {
            "product_ref": PRODUCT,
            "unit_price": 39.0,
            "total_price": 39.0,
            "currency": "USD",
            "checkout_url": f"https://{_domain(store_id)}/cart/1:1",
            "expires_at": time.time() + LIVE_FOR_AN_HOUR,
        },
        "claims": claims,
        "message": message,
        "agent_version": "1.0.0",
        "schema_version": "2.0.0",
    }


class _Bidders:
    def solicit(self, store: dict[str, Any]) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        return {
            "store_id": store_id,
            "received_at": time.time(),
            "bid": _bid(store_id),
        }

    __call__ = solicit


def _served() -> tuple[dict[str, Any], InMemoryLedgerSink]:
    """One real ``POST /auctions`` against the crawl-shaped catalogue."""
    stores = tuple(SHELVES)
    sink = InMemoryLedgerSink()
    app = create_app()
    configure_auctions(
        app,
        machine=AuctionStateMachine(ledger=sink),
        solicitor=_Bidders(),
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={store: {"blacklisted": False, "score": 0.6} for store in stores},
        registered_domains=StaticRegisteredDomains({store: _domain(store) for store in stores}),
        catalog=StaticCatalogSnapshots({store: _crawled_snapshot(store) for store in stores}),
        claim_dimensions=CLAIM_DIMENSIONS,
    )
    response = TestClient(app).post(
        "/auctions",
        json={
            "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1", "hard_constraints": []},
            "roster": [
                {"store_id": store, "tier": 1, "product_ref": PRODUCT, "list_price": 45.0}
                for store in stores
            ],
            "bid_timeout_seconds": 5.0,
        },
    )
    assert response.status_code == 201, response.text
    return response.json(), sink


@pytest.fixture(scope="module")
def served() -> tuple[dict[str, Any], InMemoryLedgerSink]:
    return _served()


def _stock_verdicts(sink: InMemoryLedgerSink, store_id: str) -> list[str]:
    return [
        str(event["payload"]["status"])
        for event in sink.events
        if event["kind"] == "claim_verified" and event["store_id"] == store_id
    ]


def _components(body: dict[str, Any], store_id: str) -> dict[str, float]:
    for row in body["ranked"]:
        if row["store_id"] == store_id:
            return dict(row["components"])
    raise AssertionError(f"{store_id} is not ranked at all: {body['ranked']}")


def _slot_stores(body: dict[str, Any]) -> set[str]:
    ranked = {row["bid_ref"]: row["store_id"] for row in body["ranked"]}
    return {ranked[slot["bid_ref"]] for slot in body["shortlist"]["slots"]}


# =====================================================================================
# The platform can confirm a stock claim at all — which is the defect
# =====================================================================================
def test_a_structured_stock_claim_is_decided_rather_than_shrugged_at(
    served: tuple[dict[str, Any], InMemoryLedgerSink],
) -> None:
    """THE REGRESSION. Before D59 every one of these came back ``ambiguous`` — the exchange
    reporting its own gap — so the claim cost the liar nothing and proved nothing about the
    honest store."""
    _body, sink = served
    assert _stock_verdicts(sink, HONEST) == ["verified"]
    assert _stock_verdicts(sink, LIAR) == ["contradicted"]
    assert "ambiguous" not in _stock_verdicts(sink, HONEST) + _stock_verdicts(sink, LIAR)


def test_a_pitch_derived_stock_claim_is_graded_the_same_as_an_asserted_one(
    served: tuple[dict[str, Any], InMemoryLedgerSink],
) -> None:
    """The store that only SAID it. The decomposer's vocabulary is this exchange's own
    ``catalog_keys``, so the sentence had nowhere to land until the derived reading joined it."""
    body, sink = served
    assert _stock_verdicts(sink, PROSE) == ["contradicted"], (
        "prose asserting stock the crawl denies was not graded; a claim read out of a pitch "
        "must earn and cost exactly what an asserted one does"
    )
    assert _components(body, PROSE)["policy_penalties"] == pytest.approx(-0.15)


# =====================================================================================
# Both directions, on the one served request
# =====================================================================================
def test_the_honest_store_is_not_penalised_and_keeps_its_slot(
    served: tuple[dict[str, Any], InMemoryLedgerSink],
) -> None:
    body, _sink = served
    assert "policy_penalties" not in _components(body, HONEST), _components(body, HONEST)
    assert HONEST in _slot_stores(body)


def test_the_liar_is_still_caught_and_still_pays(
    served: tuple[dict[str, Any], InMemoryLedgerSink],
) -> None:
    """If the fix made this penalty disappear it would be the wrong fix."""
    body, sink = served
    assert _components(body, LIAR)["policy_penalties"] == pytest.approx(-0.15)
    events = [
        dict(event["payload"])
        for event in sink.events
        if event["kind"] == "policy_event" and event["store_id"] == LIAR
    ]
    assert [event["kind"] for event in events] == ["contradicted_claim"]
    assert events[0]["penalty_per_event"] == pytest.approx(0.15)


def test_the_store_whose_stock_moved_since_the_crawl_is_not_accused(
    served: tuple[dict[str, Any], InMemoryLedgerSink],
) -> None:
    """The freshness half of the ruling, and the reason a bare alias would have been wrong.

    This store's shelf reads ``out_of_stock`` in the platform's record, exactly as the liar's
    does — the ONLY difference between them is that the platform last crawled this one a day
    before the exchange graded the claim, outside the one-hour cadence it publishes for
    ``offer.availability`` itself. An out-of-date reading can neither support nor contradict, so
    the exchange declines to decide rather than calling a restocked seller a liar.

    And because the reading is out of date, the key leaves ``catalog_keys``' vocabulary with
    it, so the exchange records its OWN gap (`ambiguous`) rather than charging the store an
    ``unsupported`` in ``verified_claim_ratio``'s denominator. A platform-side gap must not
    rank an honest store below one that said nothing.
    """
    body, sink = served
    assert _stock_verdicts(sink, RESTOCKED) == ["ambiguous"]
    assert "policy_penalties" not in _components(body, RESTOCKED), _components(body, RESTOCKED)
    assert RESTOCKED in _slot_stores(body)


def test_only_the_two_dishonest_stores_are_penalised_on_this_request(
    served: tuple[dict[str, Any], InMemoryLedgerSink],
) -> None:
    """Stated as a set so a penalty leaking onto an honest store fails here, not silently."""
    _body, sink = served
    penalised = {
        str(event["store_id"])
        for event in sink.events
        if event["kind"] == "policy_event" and event["payload"]["kind"] == "contradicted_claim"
    }
    assert penalised == {LIAR, PROSE}


# =====================================================================================
# The same rule, on the snapshot the REAL graph adapter builds
#
# The tests above hand-write the snapshot. That is how the first version of this rule shipped
# broken: it measured a reading's age against `captured_at`, which `graph.query.catalogue_entry`
# computes as `latest_instant(store, product, attributes, offer)` — the offer's own stamp is one
# of the maxands, so the quantity is structurally ZERO for a single crawl pass however old the
# pass is. Driven through the real adapter, an honest restocked store was `contradicted` at 0h,
# 1h, 1d, 7d, 30d, 90d and 365d alike. So the age is now `read_at - captured_at`, stamped by the
# adapter when it reads the graph, and this section drives that adapter rather than a fixture
# shaped like its output.
# =====================================================================================
def _entry(*, availability: str, crawled_at: str) -> Any:
    from ingest.graph.query import CatalogueEntry, ShopOffer  # noqa: PLC0415

    return CatalogueEntry(
        store_id="s1",
        domain="s1.example.com",
        product_id=PRODUCT,
        canonical_name="Merino beanie",
        brand="Northerly",
        status="active",
        via=["SELLS"],
        attributes=[],
        offer=ShopOffer(
            offer_id="o1",
            product_id=PRODUCT,
            variant_id="v1",
            price=39.0,
            currency="USD",
            availability=availability,
            # Ancient, and it must not matter: a last-CHANGED stamp is not a confirmation time.
            observed_at=OFFER_LAST_CHANGED,
            source_ids=["src1"],
        ),
        source_ids=["src1"],
        observed_at=crawled_at,
    )


def _graph_snapshot(*, availability: str, crawled_at: str) -> dict[str, Any]:
    from datetime import UTC, datetime  # noqa: PLC0415

    from exchange.retrieval.catalogue import GraphCatalogSnapshots  # noqa: PLC0415

    source = GraphCatalogSnapshots(
        lambda: None, clock=lambda: datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    )
    return source.as_snapshot(_entry(availability=availability, crawled_at=crawled_at))


def test_the_graph_adapter_stamps_the_moment_it_read_the_graph() -> None:
    """Without this the age of a crawled reading is not expressible at all."""
    snapshot = _graph_snapshot(availability="out_of_stock", crawled_at=CRAWLED_RECENTLY)
    assert snapshot["read_at"] == READ_AT
    assert snapshot["captured_at"] == CRAWLED_RECENTLY
    assert snapshot["products"][0]["offer"]["observed_at"] == OFFER_LAST_CHANGED


def test_on_the_real_adapter_a_fresh_crawl_decides_and_a_stale_one_does_not() -> None:
    """Both directions, on the object the served exchange actually grades against.

    The two snapshots differ in ONE field — when the platform last crawled the shelf. The
    availability token is the same, the offer's own stamp is the same and is ancient in both.
    """
    from claim_verification.verifier import catalog_keys, stock_reading  # noqa: PLC0415

    fresh = _graph_snapshot(availability="out_of_stock", crawled_at=CRAWLED_RECENTLY)
    stale = _graph_snapshot(availability="out_of_stock", crawled_at=CRAWLED_A_DAY_AGO)

    assert stock_reading(fresh["products"][0], fresh)["value"] is False
    assert "in_stock" in catalog_keys(fresh, PRODUCT), (
        "the platform crawled this shelf twenty minutes ago and still cannot decide a stock "
        "claim about it"
    )
    assert "in_stock" not in catalog_keys(stale, PRODUCT), (
        "a day-old reading was offered to the exchange as decidable, which is how an honest "
        "restocked store gets called a liar"
    )
