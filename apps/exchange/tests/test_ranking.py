"""T-032 gate — the published ranking formula, the eligibility filters, the shortlist.

This is the ticket's own declared verify (``pytest apps/exchange/tests/test_ranking.py``).
It is written to be independent of the frozen acceptance suite: it recomputes the score
from the published weights in ``packages.contracts.src.ranking`` rather than hard-coding
numbers, and it drives ``rank()`` through the same four-positional surface every caller
uses.

Every product import happens inside a test function, matching the house rule.
"""

from __future__ import annotations

import pytest

T_NOW = 1_700_000_000.0
T_PAST = 1_600_000_000.0
T_FUTURE = 2_000_000_000.0


def _claim(key, value, *, source="owner_statement", status="verified"):
    return {
        "key": key,
        "value": value,
        "provenance": {
            "source": source,
            "ref": f"ref:{key}",
            "observed_at": T_PAST,
            "authority_rank": 1,
        },
        "status": status,
    }


def _candidate(
    bid_id,
    store_id=None,
    *,
    unit_price=100.0,
    total_price=None,
    intent_match=0.80,
    price_value=0.60,
    delivery_fit=0.50,
    verified_claim_ratio=1.0,
    policy_penalties=0.0,
    claims=None,
    expires_at=T_FUTURE,
    checkout_url=None,
    tier=1,
    network_fee=0.0,
    fee_rate=0.0,
):
    store_id = store_id if store_id is not None else bid_id.replace("bid", "store")
    store_domain = f"{store_id}.example.com"
    if checkout_url is None:
        checkout_url = f"https://{store_domain}/cart/1:1?discount=NET"
    return {
        "bid_id": bid_id,
        "store_id": store_id,
        "store_domain": store_domain,
        "tier": tier,
        "network_fee": network_fee,
        "fee_rate": fee_rate,
        "envelope_max_discount_pct": 0.0,
        "envelope_budget_cap": 0.0,
        "offer": {
            "product_ref": "product-1",
            "unit_price": unit_price,
            "total_price": unit_price if total_price is None else total_price,
            "checkout_url": checkout_url,
            "expires_at": expires_at,
        },
        "claims": [_claim("capacity_l", 35), _claim("ships_in_days", 2)]
        if claims is None
        else list(claims),
        "intent_match": intent_match,
        "price_value": price_value,
        "delivery_fit": delivery_fit,
        "verified_claim_ratio": verified_claim_ratio,
        "policy_penalties": policy_penalties,
    }


def _intent():
    return {
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "query": "a 30 litre commuter backpack",
        "category": "backpacks",
        "hard_constraints": [{"field": "capacity_l", "op": "gte", "value": 30}],
        "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
        "ship_to": "US",
        "currency": "USD",
        "budget_band": "100-200",
        "created_at": T_PAST,
        "schema_version": "1.0.0",
    }


def _snapshot(store_ids, *, scores=None, blacklisted=(), low_data=()):
    scores = scores or {}
    return {
        sid: {
            "store_id": sid,
            "score": float(scores.get(sid, 0.5)),
            "confidence": 0.4,
            "dims": {
                dim: {"alpha": 2.0, "beta": 2.0, "decayed_at": T_PAST}
                for dim in (
                    "price_honored",
                    "discount_honored",
                    "shipped_on_time",
                    "not_returned",
                    "feedback_match",
                )
            },
            "blacklisted": sid in blacklisted,
            "low_data": sid in low_data,
        }
        for sid in store_ids
    }


def _config():
    return {"now": T_NOW}


def _order(result):
    return [row["bid_id"] for row in result["ranked"]]


def _by_bid(result):
    return {row["bid_id"]: row for row in result["candidates"]}


def _slot_refs(result):
    return [slot["bid_ref"] for slot in result["shortlist"]["slots"]]


def _reason_blob(record):
    return " ".join(str(r) for r in record["exclusion_reasons"]).lower()


# ---------------------------------------------------------------------------------
# The published formula
# ---------------------------------------------------------------------------------
def test_rank_score_equals_the_published_weighted_combination():
    """The score is exactly the published weights applied to the published features."""
    from contracts.ranking import DEFAULT_RANKING_WEIGHTS

    from apps.exchange.src.ranking import rank

    weights = DEFAULT_RANKING_WEIGHTS.weights
    assert set(weights) == {"w_m", "w_e", "w_t", "w_v", "w_d"}, weights
    assert sum(weights.values()) == pytest.approx(1.0)

    # symbol -> feature comes from contracts, not from this file. Writing the pairing out
    # here is how it drifts: `w_e` weights `verified_claim_ratio` and `w_v` weights
    # `price_value`, and swapping the two is invisible in every assertion but this one.
    features = DEFAULT_RANKING_WEIGHTS.feature_weights
    assert set(features) == {
        "intent_match",
        "verified_claim_ratio",
        "trust",
        "price_value",
        "delivery_fit",
    }, features

    cand = _candidate(
        "bid-a",
        intent_match=0.95,
        price_value=0.90,
        delivery_fit=0.90,
        verified_claim_ratio=0.80,
    )
    snapshot = _snapshot(["store-a"], scores={"store-a": 0.75})
    result = rank([cand], _intent(), snapshot, _config())

    expected = (
        features["intent_match"] * 0.95
        + features["verified_claim_ratio"] * 0.80
        + features["trust"] * 0.75
        + features["price_value"] * 0.90
        + features["delivery_fit"] * 0.90
    )
    row = _by_bid(result)["bid-a"]
    assert row["rank_score"] == pytest.approx(expected), (
        f"rank_score {row['rank_score']} is not the published weighted combination {expected}"
    )
    # Every published term must be visible as its own component, so the score is auditable.
    assert set(row["components"]) >= {
        "intent_match",
        "price_value",
        "trust",
        "verified_claim_ratio",
        "delivery_fit",
    }, row["components"]


def test_policy_penalties_lower_the_score_and_nothing_else_does():
    from apps.exchange.src.ranking import rank

    snapshot = _snapshot(["store-a"])
    clean = rank([_candidate("bid-a")], _intent(), snapshot, _config())
    penalised = rank(
        [_candidate("bid-a", policy_penalties=0.25)], _intent(), snapshot, _config()
    )
    assert (
        _by_bid(penalised)["bid-a"]["rank_score"] < _by_bid(clean)["bid-a"]["rank_score"]
    ), "a policy penalty must reduce the published score"


def test_score_ignores_network_fee_tier_and_envelope_caps():
    """R11: the formula has no fee, tier or discount-cap term, so mutating them is a no-op."""
    from apps.exchange.src.ranking import rank

    snapshot = _snapshot(["store-a", "store-b"])
    base = [
        _candidate("bid-a", intent_match=0.9, price_value=0.8, delivery_fit=0.7),
        _candidate("bid-b", intent_match=0.4, price_value=0.3, delivery_fit=0.2),
    ]
    before = rank([dict(c) for c in base], _intent(), snapshot, _config())

    loaded = [dict(c) for c in base]
    loaded[0].update(
        network_fee=999.0, fee_rate=0.9, tier=0, envelope_max_discount_pct=80.0,
        envelope_budget_cap=1.0,
    )
    loaded[1].update(
        network_fee=0.0, fee_rate=0.0, tier=2, envelope_max_discount_pct=0.0,
        envelope_budget_cap=9_000.0,
    )
    after = rank(loaded, _intent(), snapshot, _config())

    assert _order(after) == _order(before)
    assert {b: _by_bid(after)[b]["rank_score"] for b in ("bid-a", "bid-b")} == {
        b: _by_bid(before)[b]["rank_score"] for b in ("bid-a", "bid-b")
    }
    scores = {_by_bid(before)[b]["rank_score"] for b in ("bid-a", "bid-b")}
    assert len(scores) == 2, "a constant scorer is fee-blind but is not a ranker"


def test_ties_break_by_price_then_bid_id_regardless_of_input_order():
    from apps.exchange.src.ranking import rank

    def build():
        return [
            _candidate("bid-c", unit_price=100.0),
            _candidate("bid-a", unit_price=100.0),
            _candidate("bid-b", unit_price=90.0),
        ]

    snapshot = _snapshot(["store-a", "store-b", "store-c"])
    intent = _intent()
    first = rank(build(), intent, snapshot, _config())
    assert len({row["rank_score"] for row in first["ranked"]}) == 1
    assert _order(first) == ["bid-b", "bid-a", "bid-c"]
    assert _order(rank(list(reversed(build())), intent, snapshot, _config())) == _order(
        first
    ), "input order must not decide output order"


# ---------------------------------------------------------------------------------
# Eligibility filters run BEFORE scoring
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kwargs", "token"),
    [
        ({"expires_at": T_PAST}, "expir"),
        ({"checkout_url": "https://attacker.tld/cart/1:1"}, "domain"),
        (
            {"claims": [_claim("capacity_l", 35, status="contradicted")]},
            "constraint",
        ),
        ({"claims": [_claim("capacity_l", 10)]}, "constraint"),
    ],
)
def test_ineligible_candidates_are_never_scored_and_say_why(kwargs, token):
    from apps.exchange.src.ranking import rank

    bad = _candidate("bid-bad", "store-bad", **kwargs)
    good = _candidate("bid-good", "store-good")
    snapshot = _snapshot(["store-bad", "store-good"])
    result = rank([bad, good], _intent(), snapshot, _config())

    row = _by_bid(result)["bid-bad"]
    assert row["eligible"] is False
    assert row["rank_score"] is None, "an ineligible candidate must never be scored"
    assert not row["components"], "an ineligible candidate must carry no component map"
    assert token in _reason_blob(row), row["exclusion_reasons"]
    assert "bid-bad" not in _order(result)
    assert "bid-bad" not in _slot_refs(result)
    # The filter must not empty the shortlist.
    assert _slot_refs(result) == ["bid-good"]


def test_blacklist_is_excluded_and_an_unreadable_blacklist_fails_closed():
    """R12: blacklisted excludes; a missing row or a null flag excludes too."""
    from apps.exchange.src.ranking import rank

    good = _candidate("bid-good", "store-good")
    bad = _candidate("bid-bad", "store-bad")

    blacklisted = rank(
        [good, bad],
        _intent(),
        _snapshot(["store-good", "store-bad"], blacklisted=("store-bad",)),
        _config(),
    )
    assert "blacklist" in _reason_blob(_by_bid(blacklisted)["bid-bad"])
    assert _slot_refs(blacklisted) == ["bid-good"]

    missing_row = rank([good, bad], _intent(), _snapshot(["store-good"]), _config())
    assert _by_bid(missing_row)["bid-bad"]["eligible"] is False, (
        "a store with no trust row has an unreadable blacklist status and must fail closed"
    )
    assert _slot_refs(missing_row) == ["bid-good"]

    null_flag = _snapshot(["store-good", "store-bad"])
    null_flag["store-bad"]["blacklisted"] = None
    nulled = rank([good, bad], _intent(), null_flag, _config())
    assert _by_bid(nulled)["bid-bad"]["eligible"] is False, (
        "a null blacklist flag is an unreadable read and must fail closed"
    )
    assert _slot_refs(nulled) == ["bid-good"]


@pytest.mark.parametrize("status", ["ambiguous", "unsupported", "contradicted"])
def test_only_verified_evidence_satisfies_a_hard_constraint(status):
    """R19: the claimed value satisfies the constraint; only `status` can decide."""
    from apps.exchange.src.ranking import rank

    weak = _candidate(
        "bid-weak",
        "store-weak",
        intent_match=1.0,
        price_value=1.0,
        delivery_fit=1.0,
        unit_price=1.0,
        claims=[_claim("capacity_l", 35, status=status), _claim("ships_in_days", 2)],
    )
    strong = _candidate(
        "bid-ok",
        "store-ok",
        intent_match=0.0,
        price_value=0.0,
        delivery_fit=0.0,
        unit_price=999.0,
    )
    snapshot = _snapshot(
        ["store-weak", "store-ok"], scores={"store-weak": 1.0, "store-ok": 0.0}
    )
    result = rank([weak, strong], _intent(), snapshot, _config())

    assert _by_bid(result)["bid-weak"]["eligible"] is False, (
        f"{status} evidence must not satisfy a hard constraint however good the pitch is"
    )
    assert _order(result) == ["bid-ok"]
    assert _slot_refs(result) == ["bid-ok"]


def test_a_candidate_missing_the_constrained_claim_entirely_is_excluded():
    from apps.exchange.src.ranking import rank

    silent = _candidate(
        "bid-silent", "store-silent", claims=[_claim("ships_in_days", 2)]
    )
    result = rank([silent], _intent(), _snapshot(["store-silent"]), _config())
    assert _by_bid(result)["bid-silent"]["eligible"] is False
    assert _slot_refs(result) == []


# ---------------------------------------------------------------------------------
# The shortlist
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 5, 6])
def test_shortlist_is_at_most_four_distinct_stores_and_collapses_gracefully(n):
    from apps.exchange.src.ranking import rank

    letters = "abcdef"
    candidates = [
        _candidate(f"bid-{letters[i]}", f"store-{letters[i]}") for i in range(n)
    ]
    snapshot = _snapshot([c["store_id"] for c in candidates])
    result = rank(candidates, _intent(), snapshot, _config())

    refs = _slot_refs(result)
    assert len(refs) == min(n, 4)
    assert len(set(refs)) == len(refs)
    store_of = {c["bid_id"]: c["store_id"] for c in candidates}
    assert len({store_of[r] for r in refs}) == len(refs), "one store took two slots"
    kinds = [slot["slot"] for slot in result["shortlist"]["slots"]]
    assert len(set(kinds)) == len(kinds), f"slots must be differentiated, got {kinds}"
    assert set(kinds) <= {"fit", "value", "reliability", "specialist"}, kinds
    assert refs[:1] == _order(result)[:1], "the top-ranked candidate leads the shortlist"


def test_two_bids_from_one_store_never_both_take_a_slot():
    from apps.exchange.src.ranking import rank

    candidates = [
        _candidate("bid-a1", "store-a", intent_match=0.9),
        _candidate("bid-a2", "store-a", intent_match=0.8),
        _candidate("bid-b1", "store-b", intent_match=0.7),
    ]
    result = rank(candidates, _intent(), _snapshot(["store-a", "store-b"]), _config())
    assert sorted(_slot_refs(result)) == ["bid-a1", "bid-b1"]


def test_slots_carry_fit_trust_and_provenance_labels():
    from apps.exchange.src.ranking import rank

    owner = [
        _claim("capacity_l", 35, source="owner_statement"),
        _claim("returns_days", 30, source="envelope_rule"),
    ]
    scraped = [
        _claim("capacity_l", 35, source="scraped"),
        _claim("returns_days", 30, source="scraped"),
    ]
    candidates = [
        _candidate("bid-a", "store-a", claims=owner, intent_match=0.95),
        _candidate("bid-b", "store-b", claims=scraped, intent_match=0.85),
        _candidate("bid-c", "store-c", claims=owner, intent_match=0.75),
        _candidate("bid-d", "store-d", claims=scraped, intent_match=0.65),
    ]
    snapshot = _snapshot(
        [c["store_id"] for c in candidates],
        scores={"store-a": 0.9, "store-b": 0.7, "store-c": 0.5, "store-d": 0.3},
    )
    slots = rank(candidates, _intent(), snapshot, _config())["shortlist"]["slots"]
    assert len(slots) == 4

    source_of = {c["bid_id"]: c["claims"][0]["provenance"]["source"] for c in candidates}
    for slot in slots:
        ref = slot["bid_ref"]
        assert isinstance(slot["fit_score"], float)
        assert slot["trust_summary"], f"slot {ref} carries no trust summary"
        labels = [str(x).lower() for x in slot["provenance_labels"]]
        assert labels, f"slot {ref} carries no provenance labels"
        if source_of[ref] == "scraped":
            assert "from their website" in labels and "store-confirmed" not in labels
        else:
            assert "store-confirmed" in labels and "from their website" not in labels


def test_rank_does_not_mutate_the_candidates_it_was_given():
    from apps.exchange.src.ranking import rank

    import copy

    candidates = [_candidate("bid-a"), _candidate("bid-b", expires_at=T_PAST)]
    pristine = copy.deepcopy(candidates)
    rank(candidates, _intent(), _snapshot(["store-a", "store-b"]), _config())
    assert candidates == pristine, "rank() must not write into its inputs"


def test_result_is_deterministic_across_repeated_calls():
    from apps.exchange.src.ranking import rank

    def build():
        return [
            _candidate("bid-a", intent_match=0.9),
            _candidate("bid-b", intent_match=0.9),
            _candidate("bid-c", intent_match=0.4),
        ]

    snapshot = _snapshot(["store-a", "store-b", "store-c"])
    intent = _intent()
    a = rank(build(), intent, snapshot, _config())
    b = rank(build(), intent, snapshot, _config())
    assert _order(a) == _order(b)
    assert _slot_refs(a) == _slot_refs(b)
    assert [row["components"] for row in a["ranked"]] == [
        row["components"] for row in b["ranked"]
    ]
