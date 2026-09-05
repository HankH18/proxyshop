"""ESC-020 — a bidder cannot make its own claim `verified`.

The defect this file owns, stated as it was measured: ``ranking/filters.py`` decided R19's
hard constraints on ``claim["status"]``, a field the published ``Claim`` does not declare
(``packages/contracts/schemas/protocol.schema.json`` — ``additionalProperties: false``) and
which nothing on the auction path validated. The only producer of it was the bidder. Two
otherwise identical stores, one adding six characters, and the liar took the entire shortlist
while the honest store came back excluded ``hard_constraint_unsatisfied``. The same field
sets ``verified_hard_fit_count``, the FIRST published tie-break (D13), so the lie won ties as
well as filters. Wiring the ranker onto ``POST /auctions`` (T-310) is what made it reachable
by an unauthenticated request.

**Both directions are asserted here, because either one alone is a different bug.** A ranker
that ignores the field and shortlists nobody is not a fix, it is a denial of service dressed
as one — so every forgery test in §1 has a partner in §2 showing that a claim the exchange
verified ITSELF still ranks, and §3 drives both through the HTTP door with the real
``claim_verification`` verifier and a real catalog snapshot.

What the MAC is and is not
---------------------------
:mod:`exchange.ranking.attestation` MACs the exchange's verdict with a per-process key. The
tests below are inside that boundary — they can mint an attestation, and so can any fixture, exactly
as they can construct a trust snapshot — so nothing here proves "an attacker cannot compute
the MAC"; that is the property of HMAC, not of this repo. What they prove is the thing that
was actually broken: **the fields that arrive on a bid decide nothing**. Every forgery below
is written the way a bidder would have to write it — as data on the claim — and each buys
zero.
"""

from __future__ import annotations

from typing import Any

import pytest

T_NOW = 1_700_000_000.0
T_PAST = 1_600_000_000.0
T_FUTURE = 2_000_000_000.0

TRUST_DIMENSIONS = (
    "price_honored",
    "discount_honored",
    "shipped_on_time",
    "not_returned",
    "feedback_match",
)

HARD_KEY = "capacity_l"
HARD_MIN = 30
TRUE_VALUE = 35  # satisfies `capacity_l >= 30`, and the catalog agrees
FALSE_VALUE = 12  # satisfies nothing, and the catalog contradicts it


# ------------------------------------------------------------------------------------
# Builders. A claim as a BIDDER writes it — no attestation, because a bidder cannot mint one.
# ------------------------------------------------------------------------------------
def _bidder_claim(key: str, value: Any, **forged: Any) -> dict[str, Any]:
    """One claim exactly as it arrives from a store, plus whatever it tried to forge."""
    claim: dict[str, Any] = {
        "key": key,
        "value": value,
        "provenance": {
            "source": "owner_statement",
            "ref": f"ref:{key}",
            "observed_at": T_PAST,
            "authority_rank": 1,
        },
    }
    claim.update(forged)
    return claim


def _candidate(store_id: str, claims: list[dict[str, Any]]) -> dict[str, Any]:
    domain = f"{store_id}.example.com"
    return {
        "bid_id": f"auction-1:{store_id}",
        "store_id": store_id,
        "store_domain": domain,
        "offer": {
            "product_ref": f"product-{store_id}",
            "unit_price": 100.0,
            "total_price": 100.0,
            "checkout_url": f"https://{domain}/cart/1:1",
            "expires_at": T_FUTURE,
        },
        "claims": claims,
    }


def _intent() -> dict[str, Any]:
    return {
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "query": "a 30 litre commuter backpack",
        "category": "backpacks",
        "hard_constraints": [{"field": HARD_KEY, "op": "gte", "value": HARD_MIN}],
        "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
        "ship_to": "US",
        "currency": "USD",
        "budget_band": "100-200",
        "created_at": T_PAST,
        "schema_version": "1.0.0",
    }


def _trust_snapshot(store_ids) -> dict[str, Any]:
    return {
        sid: {
            "store_id": sid,
            "score": 0.5,
            "confidence": 0.4,
            "dims": {
                dim: {"alpha": 2.0, "beta": 2.0, "decayed_at": T_PAST} for dim in TRUST_DIMENSIONS
            },
            "blacklisted": False,
            "low_data": False,
        }
        for sid in store_ids
    }


def _catalog_snapshot(store_id: str, capacity: int) -> dict[str, Any]:
    """The catalogue the EXCHANGE holds for one store. Never supplied by the store."""
    return {
        "snapshot_id": f"snap-{store_id}",
        "captured_at": "2023-11-14T00:00:00Z",
        "store_id": store_id,
        "products": [
            {
                "product_ref": f"product-{store_id}",
                "canonical_name": f"product-{store_id}",
                "evidence_ref": f"snap-{store_id}#product-{store_id}",
                "attributes": {
                    HARD_KEY: {"value": capacity},
                    "ships_in_days": {"value": 2},
                },
                "offer": {"unit_price": 100.0, "currency": "USD", "availability": "in_stock"},
            }
        ],
    }


def _by_store(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["store_id"]: row for row in result["candidates"]}


def _slot_refs(result: dict[str, Any]) -> list[str]:
    return [slot["bid_ref"] for slot in result["shortlist"]["slots"]]


# =====================================================================================
# 1. The forgeries. Each one is exactly what a bidder can write, and each buys nothing.
# =====================================================================================
FORGERIES: dict[str, dict[str, Any]] = {
    # The original defect, verbatim: the string the old filter read.
    "plain_status": {"status": "verified"},
    # The enum spelling, because the old filter unwrapped `.value` off an enum member.
    "status_object": {"status": {"value": "verified"}},
    # A store that has read the fix and writes the exchange's own block, with no MAC…
    "attestation_without_a_mac": {"exchange_verification": {"status": "verified", "subject": None}},
    # …with a MAC that is a plausible-looking hex digest…
    "invented_mac": {
        "exchange_verification": {
            "status": "verified",
            "subject": None,
            "key": HARD_KEY,
            "value": TRUE_VALUE,
            "unit": None,
            "verifier_version": "verification/1.0.0",
            "catalog_snapshot": "snap-liar-store",
            "mac": "f" * 64,
        }
    },
    # …and with a MAC it copied out of somewhere, i.e. the empty string / None cases.
    "empty_mac": {"exchange_verification": {"status": "verified", "mac": ""}},
    "null_mac": {"exchange_verification": {"status": "verified", "mac": None}},
}


@pytest.mark.parametrize("forgery", sorted(FORGERIES), ids=sorted(FORGERIES))
def test_no_field_a_bidder_writes_can_make_its_own_claim_verified(forgery):
    """§1. The liar and the honest store are identical but for the forged field.

    Both must be excluded. The honest store is in the assertion on purpose: without it a
    ranker that excluded EVERYBODY would pass, and that ranker is the regression this fix
    had to avoid — §2 and §3 are where the other direction is proved.
    """
    from exchange.ranking import rank

    liar = _candidate("liar-store", [_bidder_claim(HARD_KEY, TRUE_VALUE, **FORGERIES[forgery])])
    honest = _candidate("honest-store", [_bidder_claim(HARD_KEY, TRUE_VALUE)])

    result = rank(
        [honest, liar],
        _intent(),
        _trust_snapshot(["honest-store", "liar-store"]),
        {"now": T_NOW, "auction_id": "auction-1"},
    )
    rows = _by_store(result)

    assert rows["liar-store"]["eligible"] is False, (
        f"the {forgery!r} forgery made a self-asserted claim satisfy a hard constraint"
    )
    assert rows["liar-store"]["verified_hard_fit_count"] == 0, (
        "verified_hard_fit_count is the first published tie-break (D13); a bidder that can "
        "move it wins ties as well as filters"
    )
    assert _slot_refs(result) == [], "a forged claim reached the buyer-facing shortlist"
    assert rows["honest-store"]["eligible"] is False, (
        "the premise of this test has changed: the unforged claim was admitted, so the two "
        "candidates are no longer identical but for the forgery"
    )


def test_a_forged_status_cannot_overwrite_a_real_verdict():
    """A store that writes `status: verified` onto a claim the exchange found FALSE.

    The MAC covers the claim's value, so a `contradicted` verdict cannot be repainted; and
    the store's own `status` is not read at all. Two independent reasons the forgery fails,
    asserted together because a repair could plausibly remove one of them.
    """
    from exchange.ranking import rank
    from exchange.ranking.verification import StaticCatalogSnapshots, attest_candidates

    catalog = StaticCatalogSnapshots({"liar-store": _catalog_snapshot("liar-store", FALSE_VALUE)})
    # The store CLAIMS the satisfying value; its catalogue says otherwise.
    liar = _candidate(
        "liar-store",
        [_bidder_claim(HARD_KEY, TRUE_VALUE, status="verified")],
    )
    attested = attest_candidates([liar], catalog=catalog)

    verdict = attested[0]["claims"][0]["exchange_verification"]
    assert verdict["status"] == "contradicted", verdict
    assert "status" not in attested[0]["claims"][0], (
        "the store's own `status` was carried through onto the attested claim"
    )

    result = rank(
        attested,
        _intent(),
        _trust_snapshot(["liar-store"]),
        {"now": T_NOW, "auction_id": "auction-1"},
    )
    assert _by_store(result)["liar-store"]["eligible"] is False
    assert _slot_refs(result) == []


def test_a_verdict_attested_for_one_store_is_not_evidence_for_another():
    """An attestation names the store it is about, and the ranker checks it against the auction's."""
    from exchange.ranking import rank
    from exchange.ranking.attestation import attest_claim

    stolen = attest_claim(
        _bidder_claim(HARD_KEY, TRUE_VALUE), status="verified", subject="honest-store"
    )
    thief = _candidate("thief-store", [stolen])
    owner = _candidate("honest-store", [stolen])

    result = rank(
        [thief, owner],
        _intent(),
        _trust_snapshot(["thief-store", "honest-store"]),
        {"now": T_NOW, "auction_id": "auction-1"},
    )
    rows = _by_store(result)
    assert rows["thief-store"]["eligible"] is False
    assert rows["honest-store"]["eligible"] is True, (
        "the store the verdict was attested for must still be able to use it — otherwise this "
        "test would pass on a ranker that refuses every attestation"
    )
    assert _slot_refs(result) == ["auction-1:honest-store"]


def test_an_attested_verdict_does_not_survive_a_rewritten_value():
    """The MAC covers the claim's identity, not merely its verdict."""
    from exchange.ranking.attestation import attest_claim, attested_status

    attested = attest_claim(_bidder_claim(HARD_KEY, TRUE_VALUE), status="verified")
    attestation = attested["exchange_verification"]

    assert attested_status(attestation, key=HARD_KEY, value=TRUE_VALUE) == "verified"
    assert attested_status(attestation, key=HARD_KEY, value=300) is None
    assert attested_status(attestation, key="ships_in_days", value=TRUE_VALUE) is None
    assert attested_status(attestation, key=HARD_KEY, value=TRUE_VALUE, unit="litres") is None
    assert (
        attested_status(
            {**attestation, "status": "verified", "mac": None}, key=HARD_KEY, value=TRUE_VALUE
        )
        is None
    )
    assert attested_status(None, key=HARD_KEY, value=TRUE_VALUE) is None


# =====================================================================================
# 2. The other direction — a verdict the exchange produced still ranks.
# =====================================================================================
def test_the_exchange_verifies_a_true_claim_and_it_satisfies_the_hard_constraint():
    """§2. The producer half. Without this the fix would be a denial of service."""
    from exchange.ranking import rank
    from exchange.ranking.verification import StaticCatalogSnapshots, attest_candidates

    catalog = StaticCatalogSnapshots(
        {
            "true-store": _catalog_snapshot("true-store", TRUE_VALUE),
            "false-store": _catalog_snapshot("false-store", FALSE_VALUE),
        }
    )
    # Both stores CLAIM a satisfying value. Only the catalogue disagrees with one of them,
    # and the catalogue is the exchange's, not the store's.
    candidates = [
        _candidate("true-store", [_bidder_claim(HARD_KEY, TRUE_VALUE)]),
        _candidate("false-store", [_bidder_claim(HARD_KEY, TRUE_VALUE)]),
    ]
    result = rank(
        attest_candidates(candidates, catalog=catalog),
        _intent(),
        _trust_snapshot(["true-store", "false-store"]),
        {"now": T_NOW, "auction_id": "auction-1"},
    )
    rows = _by_store(result)

    assert rows["true-store"]["eligible"] is True
    assert rows["true-store"]["verified_hard_fit_count"] == 1
    assert rows["false-store"]["eligible"] is False
    assert _slot_refs(result) == ["auction-1:true-store"]


def test_a_store_cannot_choose_which_of_its_products_its_claim_is_graded_against():
    """The second lever, found while auditing the producer rather than reported with the bug.

    ``claim_verification.verify`` resolves a claim against ``claim["product_ref"]`` before it
    falls back to the pitch's. So a store bidding a 12-litre bag could put the ``product_ref``
    of its 35-litre bag on the CLAIM and collect a genuinely verified verdict about a product
    it is not selling — a true fact, satisfying the buyer's hard constraint, about the wrong
    thing. Which product an auction is about is the AUCTION's answer, so the claim's own
    ``product_ref`` is dropped and the roster's is used.
    """
    from exchange.ranking import rank
    from exchange.ranking.verification import StaticCatalogSnapshots, attest_candidates

    catalog = StaticCatalogSnapshots(
        {
            "swapper": {
                "snapshot_id": "snap-swapper",
                "products": [
                    {
                        "product_ref": "small-bag",
                        "canonical_name": "small-bag",
                        "evidence_ref": "snap-swapper#small-bag",
                        "attributes": {HARD_KEY: {"value": FALSE_VALUE}},
                    },
                    {
                        "product_ref": "big-bag",
                        "canonical_name": "big-bag",
                        "evidence_ref": "snap-swapper#big-bag",
                        "attributes": {HARD_KEY: {"value": TRUE_VALUE}},
                    },
                ],
            }
        }
    )
    candidate = _candidate("swapper", [_bidder_claim(HARD_KEY, TRUE_VALUE, product_ref="big-bag")])
    candidate["offer"]["product_ref"] = "small-bag"

    # The auction is for the small bag: that is what the roster said.
    attested = attest_candidates(
        [candidate], catalog=catalog, product_refs={"swapper": "small-bag"}
    )
    verdict = attested[0]["claims"][0]["exchange_verification"]
    assert verdict["status"] == "contradicted", verdict

    result = rank(
        attested,
        _intent(),
        _trust_snapshot(["swapper"]),
        {"now": T_NOW, "auction_id": "auction-1"},
    )
    assert _by_store(result)["swapper"]["eligible"] is False
    assert _slot_refs(result) == []

    # And the arming half: the SAME claim against the product the auction really is for
    # verifies, so this test cannot pass on a producer that grades nothing.
    honest = attest_candidates(
        [_candidate("swapper", [_bidder_claim(HARD_KEY, TRUE_VALUE)])],
        catalog=catalog,
        product_refs={"swapper": "big-bag"},
    )
    assert honest[0]["claims"][0]["exchange_verification"]["status"] == "verified"


def _united_catalog(store_id, *, value, unit, captured_at="2026-01-01T00:00:00Z", observed_at=None):
    """A catalogue whose attribute carries a UNIT, and optionally its own observation time."""
    attribute = {"value": value, "unit": unit}
    if observed_at is not None:
        attribute["observed_at"] = observed_at
    return {
        "snapshot_id": f"snap-{store_id}",
        "captured_at": captured_at,
        "freshness_window_days": 7,
        "products": [
            {
                "product_ref": f"product-{store_id}",
                "canonical_name": f"product-{store_id}",
                "evidence_ref": f"snap-{store_id}#product-{store_id}",
                "attributes": {"weight": attribute},
            }
        ],
    }


def _weight_intent(unit):
    intent = _intent()
    intent["hard_constraints"] = [{"field": "weight", "op": "gte", "value": 30, "unit": unit}]
    return intent


def test_a_store_cannot_restate_the_unit_its_own_evidence_is_filed_under():
    """The unit lever: 500 grams meeting a thirty-KILOGRAM floor.

    ``claim_verification.verify`` is never handed the claim's unit — a bare claimed number is
    read in the CATALOGUE's unit — while ``HardCriterion.decide`` selects which readings may
    satisfy a constraint by matching the reading's unit against the constraint's. So a store
    writing ``unit: "kg"`` onto a claim the catalogue records in grams collected a genuine
    ``verified`` and had it filed against the wrong constraint. Measured before the fix:
    ``unit="g"`` gave eligible=False, ``unit="kg"`` gave eligible=True with
    ``verified_hard_fit_count=1``.
    """
    from exchange.ranking import rank
    from exchange.ranking.verification import StaticCatalogSnapshots, attest_candidates

    catalog = StaticCatalogSnapshots(
        {"gram-store": _united_catalog("gram-store", value=500, unit="g")}
    )

    def ranked_with(claim_unit, constraint_unit):
        claim = _bidder_claim("weight", 500)
        if claim_unit is not None:
            claim["unit"] = claim_unit
        attested = attest_candidates([_candidate("gram-store", [claim])], catalog=catalog)
        result = rank(
            attested,
            _weight_intent(constraint_unit),
            _trust_snapshot(["gram-store"]),
            {"now": T_NOW, "auction_id": "auction-1"},
        )
        return attested[0]["claims"][0], _by_store(result)["gram-store"]

    claim, row = ranked_with("kg", "kg")
    assert claim["exchange_verification"]["status"] == "verified", (
        "the premise has changed: the catalogue no longer confirms the claimed value, so this "
        "test would pass for the wrong reason"
    )
    assert claim["exchange_verification"]["unit"] == "g", claim
    assert claim["unit"] == "g", "the store's own unit reached the ranker"
    assert row["eligible"] is False, "500 grams satisfied a 30-kilogram floor"
    assert row["verified_hard_fit_count"] == 0

    # The arming half: the same verified reading DOES satisfy a constraint stated in the unit
    # the exchange's own catalogue uses, so this is not a ranker that refuses every unit.
    _, honest = ranked_with(None, "g")
    assert honest["eligible"] is True
    assert honest["verified_hard_fit_count"] == 1


def test_a_store_cannot_backdate_its_way_past_the_stale_evidence_gate():
    """The staleness lever: the store was choosing the clock it was judged against.

    ``verify``'s freshness window is measured back from ``claim["provenance"]["observed_at"]``
    when the claim carries one, and from the snapshot's own ``captured_at`` otherwise. So a
    backdated provenance moved the reference a year earlier and turned year-old evidence
    fresh. The exchange now hands the verifier no provenance at all, which pins the reference
    to its own snapshot; the claim the RANKER sees keeps its provenance, because D30's
    buyer-facing labels are built from it.
    """
    from exchange.ranking.verification import StaticCatalogSnapshots, attest_candidates

    catalog = StaticCatalogSnapshots(
        {
            "backdater": _united_catalog(
                "backdater",
                value=500,
                unit="g",
                captured_at="2026-01-01T00:00:00Z",
                observed_at="2025-01-01T00:00:00Z",  # a year older than the snapshot
            )
        }
    )
    claim = _bidder_claim("weight", 500)
    claim["provenance"]["observed_at"] = "2025-01-02T00:00:00Z"  # the backdate

    attested = attest_candidates([_candidate("backdater", [claim])], catalog=catalog)
    verdict = attested[0]["claims"][0]["exchange_verification"]

    assert verdict["status"] == "unsupported", verdict
    assert "freshness window" in str(verdict["reason"]), verdict
    assert attested[0]["claims"][0]["provenance"]["observed_at"] == "2025-01-02T00:00:00Z", (
        "the claim the ranker sees must keep its provenance — D30's labels are built from it"
    )

    # Arming half: a reading INSIDE the window still verifies, so the gate is not simply on.
    fresh = StaticCatalogSnapshots(
        {
            "backdater": _united_catalog(
                "backdater",
                value=500,
                unit="g",
                captured_at="2026-01-01T00:00:00Z",
                observed_at="2025-12-30T00:00:00Z",
            )
        }
    )
    ok = attest_candidates([_candidate("backdater", [claim])], catalog=fresh)
    assert ok[0]["claims"][0]["exchange_verification"]["status"] == "verified"


def test_an_exchange_with_no_catalog_verifies_nothing_and_says_why():
    """The fail-closed direction, stated as a property rather than left to be discovered."""
    from exchange.ranking.verification import NoCatalogSnapshots, attest_candidates

    attested = attest_candidates(
        [_candidate("store-a", [_bidder_claim(HARD_KEY, TRUE_VALUE)])],
        catalog=NoCatalogSnapshots(),
    )
    verdict = attested[0]["claims"][0]["exchange_verification"]
    assert verdict["status"] != "verified"
    assert verdict["catalog_snapshot"] is None
    assert "no catalog snapshot" in str(verdict["reason"])


# =====================================================================================
# 3. Through the door, on the served path, with the real verifier.
# =====================================================================================
def _served(monkeypatch, *, catalog, bids):
    """One `POST /auctions` against an exchange wired the way a deployment wires it."""
    from exchange.auction.routes import configure_auctions
    from exchange.checkout.sellers import StaticRegisteredDomains
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
    from exchange.main import create_app
    from exchange.ranking.serving import configure_ranking
    from fastapi.testclient import TestClient

    store_ids = list(bids)

    class _Solicitor:
        def solicit(self, store):
            store_id = str(store["store_id"])
            return {
                "store_id": store_id,
                "received_at": T_NOW,
                "bid": bids[store_id],
            }

    app = create_app()
    configure_auctions(
        app,
        solicitor=_Solicitor(),
        eligibility=StaticSellerEligibility({sid: ELIGIBLE for sid in store_ids}),
    )
    configure_ranking(
        app,
        trust_snapshot=_trust_snapshot(store_ids),
        registered_domains=StaticRegisteredDomains(
            {sid: f"{sid}.example.com" for sid in store_ids}
        ),
        catalog=catalog,
    )
    with TestClient(app) as client:
        response = client.post(
            "/auctions",
            json={
                "intent": _intent(),
                "roster": [
                    {
                        "store_id": sid,
                        "tier": 1,
                        "product_ref": f"product-{sid}",
                        "list_price": 100.0,
                        "max_discount_pct": 0.0,
                    }
                    for sid in store_ids
                ],
                "bid_timeout_seconds": 1.0,
            },
        )
    assert response.status_code == 201, response.text
    return response.json()


def _bid(auction_store_id: str, value: Any, **forged: Any) -> dict[str, Any]:
    return {
        "store_id": auction_store_id,
        "offer": {
            "product_ref": f"product-{auction_store_id}",
            "unit_price": 100.0,
            "total_price": 100.0,
            "checkout_url": f"https://{auction_store_id}.example.com/cart/1:1",
            "expires_at": T_FUTURE,
        },
        "claims": [_bidder_claim(HARD_KEY, value, **forged)],
    }


def test_over_http_the_forger_is_excluded_and_the_true_claim_is_shortlisted(monkeypatch):
    """§3. The whole property, end to end, over the unauthenticated door.

    ``liar-store`` bids a value its catalogue contradicts AND writes ``status: verified``
    onto the claim. ``true-store`` bids a value its catalogue confirms and writes nothing.
    The liar is excluded on the hard constraint; the honest store is shortlisted.
    """
    from exchange.ranking.verification import StaticCatalogSnapshots

    catalog = StaticCatalogSnapshots(
        {
            "true-store": _catalog_snapshot("true-store", TRUE_VALUE),
            "liar-store": _catalog_snapshot("liar-store", FALSE_VALUE),
        }
    )
    body = _served(
        monkeypatch,
        catalog=catalog,
        bids={
            "true-store": _bid("true-store", TRUE_VALUE),
            "liar-store": _bid("liar-store", TRUE_VALUE, status="verified"),
        },
    )

    ranked = [row["store_id"] for row in body["ranked"]]
    excluded = {row["store_id"]: row["exclusion_reasons"] for row in body["excluded"]}
    slots = [slot["bid_ref"] for slot in body["shortlist"]["slots"]]

    assert ranked == ["true-store"], body
    assert "liar-store" in excluded
    assert any("hard_constraint" in reason for reason in excluded["liar-store"]), excluded
    assert slots == ["auction-1:true-store".replace("auction-1", body["auction_id"])]


def test_the_served_response_never_carries_a_mac(monkeypatch):
    """A MAC that appeared in a response would be one a bidder could replay."""
    import json

    from exchange.ranking.verification import StaticCatalogSnapshots

    body = _served(
        monkeypatch,
        catalog=StaticCatalogSnapshots({"true-store": _catalog_snapshot("true-store", TRUE_VALUE)}),
        bids={"true-store": _bid("true-store", TRUE_VALUE)},
    )
    text = json.dumps(body)
    assert "exchange_verification" not in text, body
    assert "mac" not in text, body
