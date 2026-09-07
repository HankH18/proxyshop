"""T-032 gate — the published ranking formula, the eligibility filters, the shortlist.

This is the ticket's own declared verify (``pytest apps/exchange/tests/test_ranking.py``).
It is written to be independent of the frozen acceptance suite: it recomputes the score
from the published weights in ``contracts.ranking`` — including which weight symbol goes
with which feature, which is the pairing this file got wrong the first time — rather than
hard-coding numbers, and it drives ``rank()`` through the same four-positional surface
every caller uses.

Every product import happens inside a test function, matching the house rule.
"""

from __future__ import annotations

import pytest

T_NOW = 1_700_000_000.0
T_PAST = 1_600_000_000.0
T_FUTURE = 2_000_000_000.0


def _claim(key, value, *, source="owner_statement", status="verified"):
    """One supporting fact, carrying the exchange's attested verdict on it (ESC-020).

    `status` names the verdict this fixture is asking the exchange to have reached; it is
    handed to the attester rather than written onto the claim, because a `status` written onto
    a claim is a field the bidder can write and R19 may not read.
    """
    from exchange.ranking.attestation import attest_claim

    return attest_claim(
        {
            "key": key,
            "value": value,
            "provenance": {
                "source": source,
                "ref": f"ref:{key}",
                "observed_at": T_PAST,
                "authority_rank": 1,
            },
        },
        status=status,
    )


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
    penalised = rank([_candidate("bid-a", policy_penalties=0.25)], _intent(), snapshot, _config())
    assert _by_bid(penalised)["bid-a"]["rank_score"] < _by_bid(clean)["bid-a"]["rank_score"], (
        "a policy penalty must reduce the published score"
    )


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
        network_fee=999.0,
        fee_rate=0.9,
        tier=0,
        envelope_max_discount_pct=80.0,
        envelope_budget_cap=1.0,
    )
    loaded[1].update(
        network_fee=0.0,
        fee_rate=0.0,
        tier=2,
        envelope_max_discount_pct=0.0,
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
    assert _order(rank(list(reversed(build())), intent, snapshot, _config())) == _order(first), (
        "input order must not decide output order"
    )


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
    snapshot = _snapshot(["store-weak", "store-ok"], scores={"store-weak": 1.0, "store-ok": 0.0})
    result = rank([weak, strong], _intent(), snapshot, _config())

    assert _by_bid(result)["bid-weak"]["eligible"] is False, (
        f"{status} evidence must not satisfy a hard constraint however good the pitch is"
    )
    assert _order(result) == ["bid-ok"]
    assert _slot_refs(result) == ["bid-ok"]


def test_a_candidate_missing_the_constrained_claim_entirely_is_excluded():
    from apps.exchange.src.ranking import rank

    silent = _candidate("bid-silent", "store-silent", claims=[_claim("ships_in_days", 2)])
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
    candidates = [_candidate(f"bid-{letters[i]}", f"store-{letters[i]}") for i in range(n)]
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
    import copy

    from apps.exchange.src.ranking import rank

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
    assert [row["components"] for row in a["ranked"]] == [row["components"] for row in b["ranked"]]


# ---------------------------------------------------------------------------------
# Rules the published contract states but the surface above cannot see: the neutral
# default for an absent feature, and the optional SellerEligibility port.
#
# The builders come from `_fixtures_ranking.py`, which the exchange conftest also
# auto-loads as fixtures. It is test support, not product code, so it is imported at
# module scope; every product import below is still inside its test.
# ---------------------------------------------------------------------------------
from apps.exchange.tests._fixtures_ranking import (  # noqa: E402
    make_candidate,
    make_config,
    make_intent,
    make_trust_snapshot,
)


def test_an_absent_feature_reads_neutral_not_zero():
    """D13/D14: a feature the verifier has not reached yet is unknown, not bad.

    Scoring an absent `delivery_fit`/`verified_claim_ratio` as 0 would systematically
    punish every store nobody has crawled yet, which is a bias rather than a measurement.
    """
    from contracts.ranking import DEFAULT_RANKING_WEIGHTS

    from apps.exchange.src.ranking import rank

    features = DEFAULT_RANKING_WEIGHTS.feature_weights
    bounds = DEFAULT_RANKING_WEIGHTS.normalization

    silent = make_candidate("bid-a", intent_match=0.9, price_value=0.4)
    assert "delivery_fit" not in silent and "verified_claim_ratio" not in silent

    result = rank([silent], make_intent(), make_trust_snapshot(["store-a"]), make_config())
    row = _by_bid(result)["bid-a"]

    expected = (
        features["intent_match"] * 0.9
        + features["verified_claim_ratio"] * float(bounds.verified_claim_ratio_when_absent)
        + features["trust"] * 0.5
        + features["price_value"] * 0.4
        + features["delivery_fit"] * float(bounds.delivery_fit_when_absent)
    )
    assert row["rank_score"] == pytest.approx(expected)

    zeroed = make_candidate(
        "bid-a", intent_match=0.9, price_value=0.4, delivery_fit=0.0, verified_claim_ratio=0.0
    )
    zeroed_row = _by_bid(
        rank([zeroed], make_intent(), make_trust_snapshot(["store-a"]), make_config())
    )["bid-a"]
    assert zeroed_row["rank_score"] < row["rank_score"], (
        "an absent feature scored the same as an explicit 0.0 — absent is not zero"
    )


def test_components_sum_to_the_score_even_with_a_penalty():
    """The score is auditable: the published terms are exactly what produced it."""
    from apps.exchange.src.ranking import rank

    candidates = [
        make_candidate(
            "bid-a",
            intent_match=0.9,
            price_value=0.4,
            delivery_fit=0.3,
            verified_claim_ratio=0.7,
            policy_penalties=0.30,
        )
    ]
    row = _by_bid(rank(candidates, make_intent(), make_trust_snapshot(["store-a"]), make_config()))[
        "bid-a"
    ]
    assert sum(row["components"].values()) == pytest.approx(row["rank_score"])
    assert row["components"]["policy_penalties"] == pytest.approx(-0.30)


def test_an_injected_eligibility_source_only_ever_adds_denials():
    """T-032 acceptance 4: `rank()` may consult a SellerEligibility source, and every
    unhappy read denies (R12). It is OPTIONAL — the published surface is four positionals
    and the blacklist is derived from the trust snapshot."""
    from apps.exchange.src.eligibility import BLACKLISTED, ELIGIBLE, StaticSellerEligibility
    from apps.exchange.src.ranking import rank

    good = make_candidate("bid-good", "store-good", intent_match=0.9)
    bad = make_candidate("bid-bad", "store-bad", intent_match=0.9)
    # The snapshot says BOTH stores are clean, so only the injected source can exclude.
    snapshot = make_trust_snapshot(["store-good", "store-bad"])
    intent = make_intent()

    without = rank([good, bad], intent, snapshot, make_config())
    assert sorted(_slot_refs(without)) == ["bid-bad", "bid-good"], (
        "with no source injected the four-positional call must be unaffected"
    )

    source = StaticSellerEligibility({"store-good": ELIGIBLE, "store-bad": BLACKLISTED})
    with_source = rank([good, bad], intent, snapshot, make_config(), eligibility=source)
    assert _by_bid(with_source)["bid-bad"]["eligible"] is False
    assert "blacklist" in _reason_blob(_by_bid(with_source)["bid-bad"])
    assert _slot_refs(with_source) == ["bid-good"]

    # A store the source has never heard of is UNAVAILABLE, which denies.
    unknown_only = StaticSellerEligibility({"store-good": ELIGIBLE})
    unknown = rank([good, bad], intent, snapshot, make_config(), eligibility=unknown_only)
    assert _by_bid(unknown)["bid-bad"]["eligible"] is False
    assert _slot_refs(unknown) == ["bid-good"]


def test_an_eligibility_source_speaking_another_interface_version_denies_everything():
    """Item 4's other half at this boundary: an unsupported `interface_version` is refused,
    not interpreted."""
    from apps.exchange.src.eligibility import ELIGIBLE, StaticSellerEligibility
    from apps.exchange.src.ranking import rank

    source = StaticSellerEligibility({"store-a": ELIGIBLE}, version="seller-eligibility/9.9.9")
    result = rank(
        [make_candidate("bid-a")],
        make_intent(),
        make_trust_snapshot(["store-a"]),
        make_config(),
        eligibility=source,
    )
    assert _by_bid(result)["bid-a"]["eligible"] is False
    assert _slot_refs(result) == []


def test_an_undecidable_hard_constraint_excludes_rather_than_admits():
    """R19: 'I cannot evaluate this constraint' and 'this constraint is satisfied' must
    never be the same outcome."""
    from apps.exchange.src.ranking import rank

    intent = make_intent([{"field": "capacity_l", "op": "no_such_op", "value": 30}])
    result = rank(
        [make_candidate("bid-a", intent_match=1.0)],
        intent,
        make_trust_snapshot(["store-a"]),
        make_config(),
    )
    row = _by_bid(result)["bid-a"]
    assert row["eligible"] is False
    assert row["rank_score"] is None
    assert "constraint" in _reason_blob(row)
    assert _slot_refs(result) == []


@pytest.mark.parametrize(
    ("url", "on_domain"),
    [
        ("https://store-a.example.com/cart/1:1", True),
        ("https://STORE-A.EXAMPLE.COM/cart/1:1", True),
        ("https://store-a.example.com:443/cart/1:1", True),
        # A rooted FQDN is the same host. The published rule normalises the trailing dot;
        # this filter's first draft did not, and refused a legitimate seller.
        ("https://store-a.example.com./cart/1:1", True),
        ("https://store-a.example.com@attacker.tld/cart", False),
        ("https://evil.store-a.example.com/cart", False),
        ("https://store-a.example.com.attacker.tld/cart", False),
        # No scheme: on-domain but not a destination a browser can be redirected to.
        ("//store-a.example.com/cart", False),
        ("store-a.example.com/cart", False),
        ("javascript:alert(1)", False),
        ("", False),
    ],
)
def test_the_checkout_domain_filter_is_the_published_rule(url, on_domain):
    """C10/D22: the ranker refuses exactly what `checkout.domain` refuses.

    This is the same rule the S8-3 release blocker is graded on. It is imported rather than
    restated, and these cases are the ones that catch a restatement drifting.
    """
    from apps.exchange.src.ranking import rank

    cand = make_candidate("bid-a", "store-a", checkout_url=url, intent_match=0.9)
    result = rank([cand], make_intent(), make_trust_snapshot(["store-a"]), make_config())
    row = _by_bid(result)["bid-a"]
    assert row["eligible"] is on_domain, row["exclusion_reasons"]
    if not on_domain:
        assert "domain" in _reason_blob(row)
        assert _slot_refs(result) == []


def test_an_offer_with_no_checkout_url_is_not_shortlisted():
    """`checkout.domain` leaves "absent" to its caller because an R10 list-price fallback
    bid carries no URL. For RANKING the answer is deny: a candidate whose checkout
    destination cannot be established is one the buyer cannot be sent to, so it must not
    occupy a shortlist slot."""
    from apps.exchange.src.ranking import rank

    cand = make_candidate("bid-a", "store-a", intent_match=0.9)
    cand["offer"].pop("checkout_url")
    result = rank([cand], make_intent(), make_trust_snapshot(["store-a"]), make_config())
    row = _by_bid(result)["bid-a"]
    assert row["eligible"] is False
    assert row["rank_score"] is None
    assert "domain" in _reason_blob(row)
    assert _slot_refs(result) == []


# ---------------------------------------------------------------------------------
# Fail-closed holes found by adversarial review. Each of these admitted a candidate
# it should have refused, and none of them was visible to the frozen acceptance suite.
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize("expires_at", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_expiry_is_not_a_live_offer(expires_at):
    """NaN is the dangerous one: every comparison against it is False, so a plain
    `expires_at <= now` test reads "not expired" and the offer walks into a slot."""
    from apps.exchange.src.ranking import rank

    cand = make_candidate("bid-a", "store-a", expires_at=expires_at, intent_match=1.0)
    result = rank([cand], make_intent(), make_trust_snapshot(["store-a"]), make_config())
    row = _by_bid(result)["bid-a"]
    assert row["eligible"] is False, f"expires_at={expires_at!r} was accepted as live"
    assert row["rank_score"] is None
    assert "expir" in _reason_blob(row)
    assert _slot_refs(result) == []


@pytest.mark.parametrize(
    "intent",
    [
        None,
        "a 30 litre commuter backpack",
        {},
        {"intent_id": "intent-1"},
        {"intent_id": "intent-1", "hard_constraints": None},
        {"intent_id": "intent-1", "hard_constraints": 5},
        {"intent_id": "intent-1", "hard_constraints": "capacity_l >= 30"},
        {"intent_id": "intent-1", "hard_constraints": [{"field": "capacity_l"}]},
        {"intent_id": "intent-1", "hard_constraints": [7]},
    ],
)
def test_an_unreadable_intent_excludes_everyone_rather_than_admitting_everyone(intent):
    """R19: an intent whose constraints cannot be read must not be read as "no constraints".

    They are opposite outcomes that look alike, and getting them confused admits every
    candidate for every intent shape this exchange does not recognise. `rank()` must also
    not raise — an unreadable intent is a decision, not a crash.
    """
    from apps.exchange.src.ranking import rank

    cand = make_candidate("bid-a", "store-a", intent_match=1.0)
    result = rank([cand], intent, make_trust_snapshot(["store-a"]), make_config())
    row = _by_bid(result)["bid-a"]
    assert row["eligible"] is False, f"intent {intent!r} admitted a candidate unchecked"
    assert row["rank_score"] is None
    assert "constraint" in _reason_blob(row)
    assert _slot_refs(result) == []


def test_an_intent_with_an_explicitly_empty_constraint_list_admits():
    """The other half of the rule above: an EMPTY list is a readable answer — the buyer
    asked for nothing mandatory — and must not be confused with an absent one."""
    from apps.exchange.src.ranking import rank

    cand = make_candidate("bid-a", "store-a", intent_match=1.0)
    result = rank([cand], make_intent([]), make_trust_snapshot(["store-a"]), make_config())
    assert _by_bid(result)["bid-a"]["eligible"] is True
    assert _slot_refs(result) == ["bid-a"]


def test_two_candidates_sharing_a_bid_id_never_produce_two_identical_slots():
    """A slot is addressed by `bid_ref`. Two rows carrying one id are two bids the exchange
    cannot tell apart, so at most one of them may occupy a slot — and the slot NAMES must
    still be distinct, which a name map keyed by bid_id could not guarantee."""
    from apps.exchange.src.ranking import rank

    candidates = [
        make_candidate("bid-dup", "store-a", intent_match=0.9),
        make_candidate("bid-dup", "store-b", intent_match=0.8),
        make_candidate("bid-c", "store-c", intent_match=0.7),
    ]
    result = rank(
        candidates,
        make_intent(),
        make_trust_snapshot(["store-a", "store-b", "store-c"]),
        make_config(),
    )
    slots = result["shortlist"]["slots"]
    refs = [s["bid_ref"] for s in slots]
    kinds = [s["slot"] for s in slots]
    assert len(set(refs)) == len(refs), f"a bid_ref occupies two slots: {refs}"
    assert len(set(kinds)) == len(kinds), f"a slot name is used twice: {kinds}"
    assert refs == ["bid-dup", "bid-c"]


# ---------------------------------------------------------------------------------
# NaN. Its own section because it is not "a wrong number" — it is a value that makes
# the sort comparator INCONSISTENT, and `sorted()` given an inconsistent comparator
# returns an order that depends on where the poisoned element sat in the input. That
# is R11's "input order must not decide output order" lost, silently.
# ---------------------------------------------------------------------------------
import itertools  # noqa: E402
import math  # noqa: E402

_NAN = float("nan")


@pytest.mark.parametrize(
    "field", ["intent_match", "price_value", "delivery_fit", "verified_claim_ratio"]
)
@pytest.mark.parametrize("bad", [_NAN, float("inf"), float("-inf")])
def test_a_non_finite_feature_never_reaches_the_score(field, bad):
    """A feature that is not a finite number is unreadable, so it reads the published
    neutral — exactly as an absent one does — rather than propagating into rank_score."""
    from apps.exchange.src.ranking import rank

    cand = make_candidate("bid-a", "store-a", **{field: bad})
    result = rank([cand], make_intent(), make_trust_snapshot(["store-a"]), make_config())
    row = _by_bid(result)["bid-a"]
    assert math.isfinite(row["rank_score"]), f"{field}={bad!r} produced {row['rank_score']}"
    assert all(math.isfinite(v) for v in row["components"].values()), row["components"]
    assert row["features"][field] == 0.5, "an unreadable feature must read the neutral value"


def test_a_non_finite_trust_score_never_reaches_the_score():
    from apps.exchange.src.ranking import rank

    snapshot = make_trust_snapshot(["store-a"])
    snapshot["store-a"]["score"] = _NAN
    result = rank([make_candidate("bid-a", "store-a")], make_intent(), snapshot, make_config())
    row = _by_bid(result)["bid-a"]
    assert math.isfinite(row["rank_score"])
    assert row["trust"] is None, "an unreadable trust score must not masquerade as a number"


def test_an_unreadable_policy_penalty_takes_the_maximum_not_zero():
    """`min(nan, cap)` is nan. Of the two safe answers, assuming the worst is the one that
    does not reward a producer whose penalty arithmetic broke."""
    from contracts.ranking import DEFAULT_RANKING_WEIGHTS

    from apps.exchange.src.ranking import rank

    cap = float(DEFAULT_RANKING_WEIGHTS.penalties.max_total_penalty)
    for bad in (_NAN, float("inf"), "not-a-number"):
        cand = make_candidate("bid-a", "store-a", policy_penalties=bad)
        row = _by_bid(rank([cand], make_intent(), make_trust_snapshot(["store-a"]), make_config()))[
            "bid-a"
        ]
        assert math.isfinite(row["rank_score"]), f"policy_penalties={bad!r}"
        assert row["components"]["policy_penalties"] == pytest.approx(-cap), bad


def test_a_nan_candidate_cannot_make_the_order_depend_on_input_position():
    """The regression that matters: with a NaN in play the ranked order used to come out
    four different ways across the six permutations of three candidates, and the poisoned
    candidate took the top shortlist slot whenever it happened to be passed first."""
    from apps.exchange.src.ranking import rank

    best = make_candidate(
        "bid-best",
        "store-b",
        intent_match=1.0,
        price_value=1.0,
        delivery_fit=1.0,
        verified_claim_ratio=1.0,
    )
    worst = make_candidate(
        "bid-worst",
        "store-w",
        intent_match=0.0,
        price_value=0.0,
        delivery_fit=0.0,
        verified_claim_ratio=0.0,
    )
    poisoned = make_candidate(
        "bid-nan",
        "store-n",
        intent_match=_NAN,
        price_value=1.0,
        delivery_fit=1.0,
        verified_claim_ratio=1.0,
    )
    snapshot = make_trust_snapshot(["store-b", "store-w", "store-n"])

    orders = set()
    slot_leads = set()
    for perm in itertools.permutations([best, worst, poisoned]):
        result = rank(list(perm), make_intent(), snapshot, make_config())
        orders.add(tuple(_order(result)))
        slot_leads.add(_slot_refs(result)[0])

    assert len(orders) == 1, f"input order decided the ranked order: {sorted(orders)}"
    assert len(slot_leads) == 1, f"input order decided the leading slot: {slot_leads}"
    assert next(iter(orders))[0] == "bid-best", (
        "the genuinely best candidate must lead; a poisoned one must not outrank it"
    )


# =====================================================================================
# R11 — the formula's INPUTS arrive on the served path
#
# Everything above grades `rank()` as a library, handing it candidates that already carry
# the five published features. That is precisely how R11 went inert without anything going
# red: `exchange.ranking.candidates` produced NO feature at all, so on a served request four
# of the five terms took their neutral value and `rank_score` was `0.4 + 0.2*trust`. Measured
# over a real socket before this section existed, three stores bidding 90/100/90 against a
# roster listing 100/200/300 all came back `rank_score=0.52`, and INVERTING every list price
# on the roster returned bit-identical scores and the identical shortlist.
#
# So these tests drive the SERVED route — `POST /auctions` through the app object
# `uvicorn exchange.main:app` builds — and assert on the JSON that comes back. A unit test on
# the feature functions cannot see this defect, for the same reason the suite could not see
# it the first time.
# =====================================================================================
SERVED_STORES = ("store-a", "store-b", "store-c")


def _served_domain(store_id):
    return f"{store_id}.example.com"


def _served_claim(key, value):
    """A claim exactly as a BIDDER writes it — no verdict on it (ESC-020)."""
    return {
        "key": key,
        "value": value,
        "provenance": {"source": "owner_statement", "ref": f"ref:{key}", "authority_rank": 1},
    }


def _served_bid(store_id, price, *, claims=None, delivery_days=None, offer_extra=None, extra=None):
    import time

    offer = {
        "product_ref": "product-1",
        "unit_price": price,
        "total_price": price,
        "currency": "USD",
        "checkout_url": f"https://{_served_domain(store_id)}/cart/1:1",
        "expires_at": time.time() + 3600.0,
    }
    if delivery_days is not None:
        offer["delivery_estimate_days"] = delivery_days
    offer.update(offer_extra or {})
    bid = {
        "auction_id": None,
        "store_id": store_id,
        "offer": offer,
        "claims": [_served_claim("capacity_l", 35)] if claims is None else list(claims),
        "agent_version": "1.0.0",
        "schema_version": "1.0.0",
    }
    bid.update(extra or {})
    return bid


def _served_app(bids, *, trust=None, stores=SERVED_STORES, capacity_l=35, catalog=None):
    """A fully wired exchange, answering from `bids` (`{store_id: bid}`).

    `catalog` overrides the wired snapshot source. `None` means the one built below — a
    snapshot per store, which is what almost every test here wants. Pass
    `NoCatalogSnapshots()` for the deployment that wired none, which is a supported
    configuration rather than a broken one and has its own ranking behaviour to pin.
    """
    import time

    from exchange.auction.routes import configure_auctions
    from exchange.checkout.sellers import StaticRegisteredDomains
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
    from exchange.main import create_app
    from exchange.ranking.serving import configure_ranking
    from exchange.ranking.verification import StaticCatalogSnapshots

    def solicit(store):
        store_id = str(store["store_id"])
        bid = bids.get(store_id)
        if bid is None:
            return None
        return {"store_id": store_id, "received_at": time.time(), "bid": dict(bid)}

    app = create_app()
    configure_auctions(
        app,
        solicitor=solicit,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={
            store: {"blacklisted": False, "score": float((trust or {}).get(store, 0.6))}
            for store in stores
        },
        registered_domains=StaticRegisteredDomains(
            {store: _served_domain(store) for store in stores}
        ),
        catalog=StaticCatalogSnapshots(
            {
                store: {
                    "snapshot_id": f"snap-{store}",
                    "products": [
                        {
                            "product_ref": "product-1",
                            "canonical_name": "product-1",
                            "evidence_ref": f"snap-{store}#product-1",
                            "attributes": {"capacity_l": {"value": capacity_l}},
                        }
                    ],
                }
                for store in stores
            }
        )
        if catalog is None
        else catalog,
    )
    return app


def _served_roster(list_prices, *, tier=1, extra=None):
    return [
        {
            "store_id": store_id,
            "tier": tier,
            "product_ref": "product-1",
            "list_price": float(list_price),
            **(extra or {}),
        }
        for store_id, list_price in list_prices.items()
    ]


def _served_post(app, roster, *, intent=None):
    from fastapi.testclient import TestClient

    response = TestClient(app).post(
        "/auctions",
        json={
            "intent": {
                "intent_id": "intent-1",
                "cluster_id": "cluster-1",
                "hard_constraints": [{"field": "capacity_l", "op": "gte", "value": 30}],
            }
            if intent is None
            else intent,
            "roster": roster,
            "bid_timeout_seconds": 2.0,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _served_scores(body):
    return {row["store_id"]: row["rank_score"] for row in body["ranked"]}


def _served_order(body):
    return [row["store_id"] for row in body["ranked"]]


def _served_slot_stores(body):
    """The shortlist's slots as store ids — the minted `bid_ref` is `{auction_id}:{store_id}`
    and the auction id is new on every request."""
    return [slot["bid_ref"].split(":", 1)[1] for slot in body["shortlist"]["slots"]]


def _served_features(body):
    """`{store_id: features}` — read off the served response's own component map.

    `components[name] / weight` recovers the feature, and the response publishes the
    components rather than the features, which is the only reason the division is here.
    """
    from contracts.ranking import DEFAULT_RANKING_WEIGHTS

    weights = DEFAULT_RANKING_WEIGHTS.feature_weights
    return {
        row["store_id"]: {
            name: row["components"][name] / weights[name] for name in weights if weights[name]
        }
        for row in body["ranked"]
    }


def test_inverting_every_list_price_changes_the_served_scores_and_the_shortlist():
    """The auditor's own experiment, rerun. THE gate for R11 on the served path.

    Three stores bid 90/100/90 and those bids are held constant. The only thing that moves
    between the two requests is the roster's list prices, inverted end to end. Before the
    fix both requests answered `rank_score=0.52` for all three and published the identical
    shortlist; after it the shortlist reverses, because `price_value` is
    `clamp((list_price - total_price)/list_price, 0, 1)` and the roster is where
    `list_price` lives.
    """
    bids = {
        store: _served_bid(store, price)
        for store, price in (("store-a", 90.0), ("store-b", 100.0), ("store-c", 90.0))
    }

    upright = _served_post(
        _served_app(bids), _served_roster({"store-a": 100.0, "store-b": 200.0, "store-c": 300.0})
    )
    inverted = _served_post(
        _served_app(bids), _served_roster({"store-a": 300.0, "store-b": 200.0, "store-c": 100.0})
    )

    assert upright["excluded"] == [], upright["excluded"]
    assert inverted["excluded"] == [], inverted["excluded"]

    # depth: a=(100-90)/100=0.10  b=(200-100)/200=0.50  c=(300-90)/300=0.70, and each is then
    # divided by this auction's own band depth, (300-100)/300 = 2/3, and clamped:
    # price_value a=0.15  b=0.75  c=1.0 (c has CLEARED the band and saturates).
    assert _served_order(upright) == ["store-c", "store-b", "store-a"], upright["ranked"]
    # inverted: depth a=0.70 b=0.50 c=0.10 -> price_value a=1.0 b=0.75 c=0.15
    assert _served_order(inverted) == ["store-a", "store-b", "store-c"], inverted["ranked"]

    assert _served_scores(upright) != _served_scores(inverted), (
        "inverting every list price left the served rank scores bit-identical — the "
        "published formula is inert on the served path (R11)"
    )
    assert _served_slot_stores(upright) == ["store-c", "store-b", "store-a"], upright["shortlist"]
    assert _served_slot_stores(inverted) == ["store-a", "store-b", "store-c"], inverted["shortlist"]

    # And the number is the published formula's, not merely "different": every non-price
    # feature is equal across these three stores, so the score gap is w_v times the
    # price_value gap and nothing else.
    #
    # The gap is computed from the published SATURATING definition rather than written as a
    # literal, so it stays a statement about the formula rather than about this arrangement.
    # Under features 2.0.0 `price_value` is `clamp(depth / band_depth, 0, 1)`: the roster's
    # list prices still reach the score (which is this test's whole subject) and they now also
    # set the point past which extra depth is worth nothing.
    from contracts.ranking import DEFAULT_RANKING_WEIGHTS
    from exchange.ranking.features import band_depth, price_band

    w_v = DEFAULT_RANKING_WEIGHTS.feature_weights["price_value"]
    to_clear = band_depth(price_band([100.0, 200.0, 300.0]))
    assert to_clear == pytest.approx((300.0 - 100.0) / 300.0)
    expected = min(0.70 / to_clear, 1.0) - min(0.10 / to_clear, 1.0)
    scores = _served_scores(upright)
    assert scores["store-c"] - scores["store-a"] == pytest.approx(w_v * expected)

    # store-c bid 90 against a 300 list price — a depth of 0.70 against a band that is cleared
    # at 2/3 — so it is PAST the band. The saturation is asserted directly rather than left
    # implicit in the gap: this is R11's "no marginal return past the auction's own price band".
    assert _served_features(upright)["store-c"]["price_value"] == pytest.approx(1.0)


def test_the_served_price_value_is_the_published_formula_over_the_rosters_list_price():
    """`price_value = clamp((list_price - total_price)/list_price, 0, 1)` (DESIGN.md:127).

    Asserted per candidate off the served response, including both clamps: a store bidding
    ABOVE its list price shows no saving (0.0, never negative), and a store bidding at its
    list price shows exactly 0.0 — which is the positive control for the whole batch. A fix
    that returned results by admitting everything would show a flattering number here.
    """
    bids = {
        "store-a": _served_bid("store-a", 50.0),  # half of a 100 list price
        "store-b": _served_bid("store-b", 100.0),  # exactly its list price
        "store-c": _served_bid("store-c", 150.0),  # ABOVE its list price
    }
    body = _served_post(
        _served_app(bids),
        _served_roster({"store-a": 100.0, "store-b": 100.0, "store-c": 100.0}),
    )
    features = _served_features(body)

    assert features["store-a"]["price_value"] == pytest.approx(0.50)
    assert features["store-b"]["price_value"] == pytest.approx(0.0)
    assert features["store-c"]["price_value"] == pytest.approx(0.0), (
        "a bid ABOVE the list price produced a negative price_value; the published clamp is [0, 1]"
    )


def test_the_served_verified_claim_ratio_is_the_exchanges_own_verdicts():
    """`verified_claim_ratio` is a count over the verdicts THIS exchange attested.

    Driven on an intent with no hard constraint, so that a store presenting no claim at all
    is still eligible and its feature can be read: a claimless store has no evidence to
    aggregate, so the feature is absent and reads the published neutral rather than 0.0 —
    scoring silence as zero is the D13/D14 bias this codebase refuses everywhere else. Every
    store bids the same price against the same list price, so `price_value` is 0.0 for all
    three and this term is the only one that can separate them.

    **The intent STATES the thing the claims are about**, which features 2.0.0 requires and
    the raw ratio did not: `verified_claim_ratio` counts verified claims whose key lands on
    something THIS buyer asked about, so an intent that asks for nothing makes every claim
    irrelevant and the term reads its neutral for everybody. A `capacity_l` preference is the
    smallest ask that keeps this test testing what it has always tested — WHOSE verdict the
    feature reads — without also turning it into an eligibility filter, which a hard
    constraint would and which would take the claimless control store out of the comparison.
    """
    bids = {
        "store-a": _served_bid(
            "store-a",
            100.0,
            claims=[_served_claim("capacity_l", 35), _served_claim("capacity_l", 12)],
        ),
        "store-b": _served_bid(
            "store-b",
            100.0,
            claims=[_served_claim("capacity_l", 35), _served_claim("capacity_l", 35.0)],
        ),
        "store-c": _served_bid("store-c", 100.0, claims=[]),
    }
    body = _served_post(
        _served_app(bids),
        _served_roster({"store-a": 100.0, "store-b": 100.0, "store-c": 100.0}),
        intent={
            "intent_id": "intent-1",
            "cluster_id": "cluster-1",
            "hard_constraints": [],
            "preferences": [{"field": "capacity_l", "direction": "maximize", "weight": 1.0}],
        },
    )
    features = _served_features(body)

    from contracts.ranking import EVIDENCE_GAIN_BY_RELEVANCE, diminishing_evidence

    gain = EVIDENCE_GAIN_BY_RELEVANCE["preference"]

    # The second claim says 12 where the catalogue says 35 -> `contradicted`, which is a
    # verdict this exchange reached and is not `verified`. One verified claim on something
    # this buyer asked about, and the contradicted one adds no evidence.
    #
    # It used to be a claim on `colour`, a key the snapshot does not carry at all, and that
    # case has MOVED rather than been dropped: an unanswerable key is now `ambiguous` and
    # counted as nothing at all, which
    # `test_a_claim_this_catalogue_could_never_decide_is_undecided_rather_than_a_cost` pins.
    # The PROPERTY here is unchanged — a claim the exchange DECIDED against buys the store
    # nothing, and under features 2.0.0 it also mints a `contradicted_claim` penalty, which
    # the ordering below is what proves.
    assert features["store-a"]["verified_claim_ratio"] == pytest.approx(gain)
    # Both claims agree with the catalogue: two verified, aggregated with diminishing returns.
    assert features["store-b"]["verified_claim_ratio"] == pytest.approx(
        diminishing_evidence([gain, gain])
    )
    assert (
        features["store-b"]["verified_claim_ratio"] > features["store-a"]["verified_claim_ratio"]
    ), "two proved facts did not beat one, so the aggregation is not counting the second"
    # No claims at all: absent, therefore the published neutral.
    assert features["store-c"]["verified_claim_ratio"] == pytest.approx(0.5)
    assert _served_order(body)[0] == "store-b", body["ranked"]
    # And the store caught contradicting its own catalogue now ranks BELOW the store that said
    # nothing, which is the `contradicted_claim` penalty doing the only thing it exists to do.
    scores = _served_scores(body)
    assert scores["store-a"] < scores["store-c"], (
        f"a contradicted claim cost the store nothing against staying silent: {scores}"
    )


def test_a_claim_this_catalogue_could_never_decide_is_undecided_rather_than_a_cost():
    """A key the exchange's catalogue does not carry is the EXCHANGE's gap, not the store's.

    `claim_verification.verify` answers `unsupported` for a key its snapshot has no reading
    for — "the catalog snapshot records no 'policy_action' for this product" — and
    `verified_claim_ratio` counts every `unsupported` verdict in its denominator as a cost the
    store bears. That is right for a key the catalogue DOES carry and does not support. It is
    wrong for a key no catalogue was ever going to carry, and the S1 demo is the measurement:
    the hosted store agent publishes a `policy_action` claim — its own record of the discount
    decision, which `trust.scoring.claim_dimension` already refuses to route to any trust
    dimension because it is not an assertion about the product or the offer — so a store making
    three true, checked, VERIFIED claims plus that one audit record read 0.75, while the store
    that answered with nothing at all read the published neutral 0.5. Three such records beside
    three true claims would have read exactly 0.5, and a fourth would have put an entirely
    honest store BELOW silence. Being richer than silence was a cost.

    `claim_verification.verifier.catalog_keys` is the permitted key vocabulary and
    `exchange.ranking.verification.UNDECIDABLE_KEY_REASON` is the verdict: outside the
    vocabulary is `ambiguous`, R18's "no comparison was made", which the ratio leaves out of
    the denominator and which R19 still refuses to let satisfy a hard constraint.

    Four stores, one term, all four bidding the same price against the same list price so
    `price_value` cannot separate them:

    * `store-a` — one true claim and one on a key the catalogue cannot decide;
    * `store-b` — the same true claim alone (the control the rule must make `store-a` equal to);
    * `store-c` — no claims at all, the published neutral;
    * `store-d` — one claim the catalogue CONTRADICTS, which must still cost.

    The intent asks about BOTH keys under features 2.0.0. `capacity_l` is the true claim's key
    and `policy_action` is the unanswerable one's, and naming both is what keeps the test
    honest: with `policy_action` left out of the intent it would be irrelevant and therefore
    free for a second, weaker reason, and the test would pass without the ``ambiguous`` rule it
    exists to pin ever being exercised.
    """
    bids = {
        "store-a": _served_bid(
            "store-a",
            100.0,
            claims=[_served_claim("capacity_l", 35), _served_claim("policy_action", "intro_5pct")],
        ),
        "store-b": _served_bid("store-b", 100.0, claims=[_served_claim("capacity_l", 35)]),
        "store-c": _served_bid("store-c", 100.0, claims=[]),
        "store-d": _served_bid("store-d", 100.0, claims=[_served_claim("capacity_l", 12)]),
    }
    stores = ("store-a", "store-b", "store-c", "store-d")
    body = _served_post(
        _served_app(bids, stores=stores),
        _served_roster(dict.fromkeys(stores, 100.0)),
        intent={
            "intent_id": "intent-1",
            "cluster_id": "cluster-1",
            "hard_constraints": [],
            "preferences": [
                {"field": "capacity_l", "direction": "maximize", "weight": 1.0},
                {"field": "policy_action", "direction": "prefer", "weight": 1.0},
            ],
        },
    )
    features = _served_features(body)
    scores = _served_scores(body)

    from contracts.ranking import EVIDENCE_GAIN_BY_RELEVANCE

    gain = EVIDENCE_GAIN_BY_RELEVANCE["preference"]

    assert features["store-a"]["verified_claim_ratio"] == pytest.approx(gain), (
        "a claim on a key this exchange's catalogue cannot decide was counted against the "
        "store, so publishing an audit record beside true claims cost it rank"
    )
    assert features["store-b"]["verified_claim_ratio"] == pytest.approx(gain)
    # THE ordering this rule exists for: saying more, truthfully, is never worse than silence.
    assert scores["store-a"] > scores["store-c"], (
        f"a store making a true, verified claim plus one this exchange could not check scored "
        f"{scores['store-a']} against {scores['store-c']} for a store that said nothing at all"
    )
    assert scores["store-a"] == pytest.approx(scores["store-b"]), (
        "an unanswerable claim moved the score, so it is not undecided — it is being graded"
    )
    # …and the positive control, without which the rule above is indistinguishable from
    # "nothing a store says is ever counted against it": a claim the catalogue DECIDED
    # against still costs, and still costs exactly what it did before — it is relevant and
    # decided, and no verified evidence came of it, so the aggregation is over nothing.
    assert features["store-d"]["verified_claim_ratio"] == pytest.approx(0.0)
    assert scores["store-d"] < scores["store-c"], (
        "a store the catalogue contradicted is no longer scored below one that said nothing"
    )


def test_an_exchange_with_no_catalog_does_not_rank_the_store_that_bid_below_the_one_that_did_not():
    """A claim this exchange could NOT check is undecided, not failed.

    An exchange with no catalog wired holds no snapshot for anybody, so
    `attest_candidate_claims` attests every claim `ambiguous` with the reason "the exchange
    holds no catalog snapshot for this store, so its claims could not be checked". Counting
    those in the ratio's denominator scores 0.0 for "we do not know" — the D13/D14 bias this
    codebase refuses everywhere else — and it does it to the stores that ANSWERED, because a
    candidate with no claims at all (an R10 fallback, by construction) has no ratio to take
    and therefore reads the published neutral.

    That is not a hypothetical ordering. It is what the S1 demo produced the day
    `ranking/features.py` landed: two hosted stores bid over real sockets, read 0.0 on this
    term, and the silent store's list-price fallback read 0.5 and took the top slot off both
    of them — and accepting a fallback is a handoff that mints no discount code, so the whole
    journey ended with `code=''`.

    Both halves are asserted. The feature, because that is the defect; and the ORDER, because
    a feature that reads correctly and still leaves the non-bidder on top would have fixed
    nothing. `store-b` never answers, so its entry is the exchange's own R10 fallback.
    """
    from exchange.ranking.verification import NoCatalogSnapshots

    bids = {"store-a": _served_bid("store-a", 100.0, claims=[_served_claim("capacity_l", 35)])}
    body = _served_post(
        _served_app(
            bids,
            stores=("store-a", "store-b"),
            trust={"store-a": 0.8, "store-b": 0.6},
            catalog=NoCatalogSnapshots(),
        ),
        _served_roster({"store-a": 100.0, "store-b": 100.0}),
        # No hard constraint: with no catalog nothing is `verified`, so a constrained intent
        # would exclude both candidates (R19) before there was a score to compare.
        intent={"intent_id": "intent-1", "cluster_id": "cluster-1", "hard_constraints": []},
    )

    fallbacks = {row["store_id"]: row.get("fallback") for row in body["entries"]}
    assert fallbacks == {"store-a": False, "store-b": True}, body["entries"]

    features = _served_features(body)
    assert features["store-a"]["verified_claim_ratio"] == pytest.approx(0.5), (
        "a store whose claims this exchange could not check at all was scored as if it had "
        "presented evidence and failed"
    )
    assert features["store-b"]["verified_claim_ratio"] == pytest.approx(0.5)
    assert _served_order(body) == ["store-a", "store-b"], (
        "the store that never replied outranked the store that bid, on an exchange that "
        "graded neither one's claims"
    )


def test_a_store_cannot_verify_its_own_claims_by_writing_a_verdict_onto_them():
    """ESC-020's rule, applied to the new feature: the ratio reads attested verdicts only.

    A store writing `status: "verified"` and a whole `exchange_verification` block onto a
    claim the catalogue contradicts gets the same ratio as one that writes nothing — the
    MAC it cannot compute is what the count reads.
    """
    forged = _served_claim("capacity_l", 999)
    forged["status"] = "verified"
    forged["exchange_verification"] = {
        "status": "verified",
        "subject": "store-b",
        "mac": "0" * 64,
        "verifier_version": "claim-verification/1",
    }
    bids = {
        "store-a": _served_bid("store-a", 100.0, claims=[_served_claim("capacity_l", 999)]),
        "store-b": _served_bid("store-b", 100.0, claims=[forged]),
    }
    body = _served_post(
        _served_app(bids, stores=("store-a", "store-b")),
        _served_roster({"store-a": 100.0, "store-b": 100.0}),
        # No hard constraint: a contradicted claim would otherwise take both stores out on
        # eligibility (R19) before there was a score to compare. The buyer does state a
        # `capacity_l` PREFERENCE, because under features 2.0.0 a claim nobody asked about is
        # irrelevant and scores nothing — which would make both stores read the neutral and
        # this test pass without the forged attestation ever being looked at.
        intent={
            "intent_id": "intent-1",
            "cluster_id": "cluster-1",
            "hard_constraints": [],
            "preferences": [{"field": "capacity_l", "direction": "maximize", "weight": 1.0}],
        },
    )
    features = _served_features(body)
    assert features["store-a"]["verified_claim_ratio"] == pytest.approx(0.0)
    assert features["store-b"]["verified_claim_ratio"] == pytest.approx(0.0), (
        "a store moved its own verified_claim_ratio by writing a verdict onto its own claim"
    )


def test_the_served_delivery_fit_orders_the_estimates_the_offers_declare():
    """`delivery_fit` reads `Offer.delivery_estimate_days`, compared within the auction.

    The exchange holds no shipping model, so there is no absolute days -> [0,1] curve to
    apply and none is invented: the fit is the offer's estimate normalised across the
    estimates this auction actually received, which is the same "normalised across the
    eligible set, neutral when there is nothing to discriminate on" rule
    `retrieval.criteria.NEUTRAL_ALIGNMENT` already applies. An offer that declares nothing
    is absent, therefore neutral.
    """
    bids = {
        "store-a": _served_bid("store-a", 100.0, delivery_days=1.0),
        "store-b": _served_bid("store-b", 100.0, delivery_days=5.0),
        "store-c": _served_bid("store-c", 100.0),
    }
    body = _served_post(
        _served_app(bids),
        _served_roster({"store-a": 100.0, "store-b": 100.0, "store-c": 100.0}),
    )
    features = _served_features(body)
    assert features["store-a"]["delivery_fit"] == pytest.approx(1.0)
    assert features["store-b"]["delivery_fit"] == pytest.approx(0.0)
    assert features["store-c"]["delivery_fit"] == pytest.approx(0.5)
    assert _served_order(body)[0] == "store-a", body["ranked"]


def test_one_delivery_estimate_in_an_auction_discriminates_nothing():
    """A degenerate range is not a ranking signal, and must not be read as one.

    With a single declared estimate there is no comparison to make, so every candidate reads
    the published neutral and the delivery term cannot move the order. This is the positive
    control against a normalisation that hands the only declarant a free 1.0.
    """
    bids = {
        "store-a": _served_bid("store-a", 100.0, delivery_days=3.0),
        "store-b": _served_bid("store-b", 100.0),
    }
    body = _served_post(
        _served_app(bids, stores=("store-a", "store-b")),
        _served_roster({"store-a": 100.0, "store-b": 100.0}),
    )
    features = _served_features(body)
    assert features["store-a"]["delivery_fit"] == pytest.approx(0.5)
    assert features["store-b"]["delivery_fit"] == pytest.approx(0.5)


def test_an_unreadable_delivery_estimate_is_absent_rather_than_the_fastest():
    """A negative or non-finite estimate is not an estimate.

    Read as a number it would be the SMALLEST in the auction and would therefore win the
    delivery term outright — a store promising `-1000` days is not promising anything.
    """
    bids = {
        "store-a": _served_bid("store-a", 100.0, delivery_days=-1000.0),
        "store-b": _served_bid("store-b", 100.0, delivery_days=2.0),
        "store-c": _served_bid("store-c", 100.0, delivery_days=4.0),
    }
    body = _served_post(
        _served_app(bids),
        _served_roster({"store-a": 100.0, "store-b": 100.0, "store-c": 100.0}),
    )
    features = _served_features(body)
    assert features["store-a"]["delivery_fit"] == pytest.approx(0.5)
    assert features["store-b"]["delivery_fit"] == pytest.approx(1.0)
    assert features["store-c"]["delivery_fit"] == pytest.approx(0.0)


def test_trust_still_moves_a_served_ranking_with_price_held_constant():
    """The second direction, and the fix is not demonstrated without it.

    Every store bids its own list price, so `price_value` is 0.0 for all three and the only
    thing left to separate them is the trust snapshot. The gap is exactly `w_t` times the
    trust gap: the other four terms are equal, so nothing else contributed.
    """
    from contracts.ranking import DEFAULT_RANKING_WEIGHTS

    bids = {store: _served_bid(store, 100.0) for store in SERVED_STORES}
    body = _served_post(
        _served_app(bids, trust={"store-a": 0.2, "store-b": 0.9, "store-c": 0.5}),
        _served_roster({"store-a": 100.0, "store-b": 100.0, "store-c": 100.0}),
    )

    assert _served_order(body) == ["store-b", "store-c", "store-a"], body["ranked"]
    scores = _served_scores(body)
    w_t = DEFAULT_RANKING_WEIGHTS.feature_weights["trust"]
    assert scores["store-b"] - scores["store-a"] == pytest.approx(w_t * (0.9 - 0.2))


def test_the_served_ranking_stays_blind_to_fee_tier_and_envelope():
    """R11's other half, re-measured now that the features are live.

    Adding features is exactly when blindness gets broken by accident, so the same auction
    is driven twice: once plain, once with a network fee and a fee rate on the bid, an
    envelope's commitments and discount ceiling on the offer, and a different (still
    agent-bearing) tier on the roster. Every rank score must be bit-identical — not
    approximately equal, because a formula that reads a fee produces a different float.
    """
    plain = {store: _served_bid(store, 90.0) for store in SERVED_STORES}
    laden = {
        store: _served_bid(
            store,
            90.0,
            offer_extra={
                "commitments": [_served_claim("returns_days", 30)],
                "envelope_max_discount_pct": 40.0,
            },
            extra={"network_fee": 12.5, "fee_rate": 0.30, "tier": 3, "envelope_budget_cap": 900.0},
        )
        for store in SERVED_STORES
    }
    roster = {"store-a": 100.0, "store-b": 200.0, "store-c": 300.0}

    first = _served_post(_served_app(plain), _served_roster(roster, tier=1))
    second = _served_post(_served_app(laden), _served_roster(roster, tier=2))

    assert _served_scores(first) == _served_scores(second), (
        "a network fee, a tier or an envelope moved a served rank score (R11)"
    )
    assert _served_order(first) == _served_order(second)


def test_the_served_ranking_is_deterministic_over_identical_inputs():
    """R11/S3: the same inputs produce the same scores, to the bit."""
    bids = {store: _served_bid(store, 90.0) for store in SERVED_STORES}
    roster = _served_roster({"store-a": 100.0, "store-b": 200.0, "store-c": 300.0})
    first = _served_post(_served_app(bids), roster)
    second = _served_post(_served_app(bids), roster)
    assert _served_scores(first) == _served_scores(second)
    assert _served_order(first) == _served_order(second)
    assert [row["components"] for row in first["ranked"]] == [
        row["components"] for row in second["ranked"]
    ]


def test_a_silent_store_is_ranked_at_its_list_price_and_shows_no_saving():
    """The R10 fallback keeps its slot, and the new feature does not hand it a bonus.

    A store that never answered is represented at the roster's list price, so its
    `price_value` is exactly 0.0 — no discount was offered, and none is invented for it.
    """
    bids = {"store-a": _served_bid("store-a", 60.0)}  # store-b answers nothing at all
    body = _served_post(
        _served_app(bids, stores=("store-a", "store-b")),
        _served_roster({"store-a": 100.0, "store-b": 100.0}),
        # A fallback carries no claims by construction (R10/R18/R19), so it can evidence no
        # hard constraint; this test is about its price, not about its eligibility.
        intent={"intent_id": "intent-1", "cluster_id": "cluster-1", "hard_constraints": []},
    )
    entries = {entry["store_id"]: entry for entry in body["entries"]}
    assert entries["store-b"]["fallback"] is True, entries
    features = _served_features(body)
    assert features["store-b"]["price_value"] == pytest.approx(0.0)
    assert features["store-a"]["price_value"] == pytest.approx(0.4)
    assert _served_order(body) == ["store-a", "store-b"], body["ranked"]


def test_the_served_features_are_not_read_off_the_bid():
    """A store writing the published feature names into its own reply moves nothing.

    The projection computes these four; it never copies them. This is the same property
    `test_ranking_served.py` §4 asserts at the library level, re-driven over HTTP now that
    three of the four have a producer.
    """
    gamed = {
        "intent_match": 1.0,
        "verified_claim_ratio": 1.0,
        "price_value": 1.0,
        "delivery_fit": 1.0,
        "trust": 1.0,
        "policy_penalties": -5.0,
        "rank_score": 99.0,
    }
    roster = _served_roster({"store-a": 100.0, "store-b": 100.0})
    honest = _served_post(
        _served_app(
            {store: _served_bid(store, 100.0) for store in ("store-a", "store-b")},
            stores=("store-a", "store-b"),
        ),
        roster,
    )
    cheating = _served_post(
        _served_app(
            {
                "store-a": _served_bid("store-a", 100.0),
                "store-b": _served_bid("store-b", 100.0, extra=gamed, offer_extra=dict(gamed)),
            },
            stores=("store-a", "store-b"),
        ),
        roster,
    )
    assert _served_scores(honest) == _served_scores(cheating), (
        f"a bidder moved its own score by writing feature names into its reply: "
        f"{_served_scores(honest)} vs {_served_scores(cheating)}"
    )


def test_intent_match_has_no_served_producer_and_says_so_by_staying_neutral():
    """The one feature this fix does NOT produce, asserted rather than left to be discovered.

    `intent_match` is retrieval+rerank's output (DESIGN.md:132). Its producer exists —
    `exchange.retrieval.fit.intent_match_by_bid` — but it consumes `retrieve()` assessments,
    and a served auction has no retrieval source: `POST /auctions` is handed a ROSTER by its
    caller and never queries a candidate index. Fabricating a number here would move
    rankings for a reason nobody could audit, so the term stays at its neutral value and
    this test is the record of that. The day a retrieval source is wired into the auction
    route, this assertion should start failing.
    """
    bids = {
        "store-a": _served_bid("store-a", 50.0),
        "store-b": _served_bid("store-b", 100.0),
        "store-c": _served_bid("store-c", 90.0),
    }
    body = _served_post(
        _served_app(bids),
        _served_roster({"store-a": 100.0, "store-b": 100.0, "store-c": 100.0}),
    )
    features = _served_features(body)
    assert {row["intent_match"] for row in features.values()} == {0.5}, features


# =====================================================================================
# 6. Features 2.0.0 — buyer-conditional evidence, a saturating price band, and a
#    penalty for lying. Every test in this section drives POST /auctions.
#
# The defect this section exists for, stated once so no test has to restate it: the
# product is a matching-and-persuasion market (D55) and the published formula paid
# NOTHING for customization. Sort the five terms by "can a store move it at bid time"
# and "does its value depend on THIS buyer" and the movable-and-buyer-dependent cell is
# empty; `scoring.score` is strictly additive with no interaction term; so a store's
# argmax over pitches was identical for every buyer, and the store-agent learning loop
# about to be wired would have measured that correctly and converged every store onto
# one generic pitch.
# =====================================================================================
def _asked_intent(*, hard=(), prefer=(), query=None):
    """An intent stating exactly the asks a test needs, and nothing else.

    `hard_constraints` is always present (an ABSENT one is `read_criteria`'s undecidable
    intent, which denies every candidate) and empty unless a test needs a filter.
    """
    intent = {
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "hard_constraints": [dict(entry) for entry in hard],
        "preferences": [
            {"field": field, "direction": "maximize", "weight": 1.0} for field in prefer
        ],
    }
    if query is not None:
        intent["query"] = query
    return intent


def _multi_key_app(bids, *, stores, attributes):
    """A served exchange whose catalogue carries `attributes` for every store.

    `_served_app` publishes a single `capacity_l` reading, which is enough for one claim and
    not enough to tell three claims apart from ten.
    """
    import time

    from exchange.auction.routes import configure_auctions
    from exchange.checkout.sellers import StaticRegisteredDomains
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
    from exchange.main import create_app
    from exchange.ranking.serving import configure_ranking
    from exchange.ranking.verification import StaticCatalogSnapshots

    def solicit(store):
        store_id = str(store["store_id"])
        bid = bids.get(store_id)
        if bid is None:
            return None
        return {"store_id": store_id, "received_at": time.time(), "bid": dict(bid)}

    app = create_app()
    configure_auctions(
        app,
        solicitor=solicit,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={store: {"blacklisted": False, "score": 0.6} for store in stores},
        registered_domains=StaticRegisteredDomains(
            {store: _served_domain(store) for store in stores}
        ),
        catalog=StaticCatalogSnapshots(
            {
                store: {
                    "snapshot_id": f"snap-{store}",
                    "products": [
                        {
                            "product_ref": "product-1",
                            "canonical_name": "product-1",
                            "evidence_ref": f"snap-{store}#product-1",
                            "attributes": {
                                key: {"value": value}
                                for key, value in attributes.get(store, {}).items()
                            },
                        }
                    ],
                }
                for store in stores
            }
        ),
    )
    return app


#: Ten true facts about a product that no shopper in these tests asks about.
UNASKED_FACTS: dict = {
    "box_colour": "brown",
    "carton_count": 6,
    "pallet_code": "PX-9",
    "sku_prefix": "AAA",
    "label_font": "helvetica",
    "warehouse_bay": "B12",
    "barcode_kind": "ean13",
    "shrinkwrap": True,
    "insert_card": "yes",
    "tape_width": 48,
}

#: Three true facts about the same product that the shopper in `test_three_asked...` DOES ask
#: about — one per relevance tier, which is also what makes the ordering in the customization
#: test visible.
ASKED_FACTS: dict = {
    "capacity_l": 35,
    "frame_material": "aluminium",
    "waterproof_rating": "ip67",
}


def test_three_verified_claims_this_buyer_asked_about_beat_ten_nobody_asked():
    """THE headline. Customization pays; volume does not.

    Both stores are honest and both are believed: every claim either one makes is checked
    against this exchange's own catalogue snapshot and comes back `verified`. They bid the
    same price against the same list price, they have the same trust score, and neither
    declares a delivery estimate — so `price_value`, `trust`, `delivery_fit` and
    `intent_match` are identical and this term is the only thing that can separate them.

    `store-focused` makes THREE claims, all about things this shopper asked for.
    `store-loud` makes TEN, all true, none of them about anything the shopper asked.

    Under the raw ratio both read 1.0 and the shortlist was a coin toss decided by
    `bid_id`. Under features 2.0.0 the ten score exactly nothing — a claim nobody asked
    about is worth what silence is worth, so `store-loud` reads the published neutral —
    and the three win.
    """
    stores = ("store-focused", "store-loud")
    attributes = {
        "store-focused": {**ASKED_FACTS, **UNASKED_FACTS},
        "store-loud": {**ASKED_FACTS, **UNASKED_FACTS},
    }
    bids = {
        "store-focused": _served_bid(
            "store-focused",
            100.0,
            claims=[_served_claim(key, value) for key, value in ASKED_FACTS.items()],
        ),
        "store-loud": _served_bid(
            "store-loud",
            100.0,
            claims=[_served_claim(key, value) for key, value in UNASKED_FACTS.items()],
        ),
    }
    body = _served_post(
        _multi_key_app(bids, stores=stores, attributes=attributes),
        _served_roster(dict.fromkeys(stores, 100.0)),
        # PREFERENCES and a query, no hard constraint: a hard constraint is an eligibility
        # filter (R19), so `store-loud` — which claims nothing about `capacity_l` — would be
        # EXCLUDED rather than out-scored and this test would prove nothing about the formula.
        intent=_asked_intent(
            prefer=["capacity_l", "frame_material"],
            query="a waterproof rating I can trust",
        ),
    )
    features = _served_features(body)
    scores = _served_scores(body)

    assert len(bids["store-loud"]["claims"]) == 10
    assert len(bids["store-focused"]["claims"]) == 3
    assert _served_order(body) == ["store-focused", "store-loud"], body["ranked"]

    from contracts.ranking import EVIDENCE_GAIN_BY_RELEVANCE, diminishing_evidence

    # `capacity_l` and `frame_material` are stated preferences; `waterproof_rating` is a word
    # in the query. Three relevant facts, aggregated with diminishing returns.
    expected = diminishing_evidence(
        [
            EVIDENCE_GAIN_BY_RELEVANCE["preference"],
            EVIDENCE_GAIN_BY_RELEVANCE["preference"],
            EVIDENCE_GAIN_BY_RELEVANCE["query_term"],
        ]
    )
    assert features["store-focused"]["verified_claim_ratio"] == pytest.approx(expected)
    # Ten verified claims, none of them asked for: absent, therefore the published neutral.
    assert features["store-loud"]["verified_claim_ratio"] == pytest.approx(0.5)
    assert features["store-focused"]["price_value"] == pytest.approx(
        features["store-loud"]["price_value"]
    ), "the two stores were separated by price rather than by evidence"

    from contracts.ranking import DEFAULT_RANKING_WEIGHTS

    w_e = DEFAULT_RANKING_WEIGHTS.feature_weights["verified_claim_ratio"]
    assert scores["store-focused"] - scores["store-loud"] == pytest.approx(
        w_e * (expected - 0.5)
    ), f"the whole gap is the evidence term and nothing else: {scores}"


def test_the_same_store_has_a_different_best_pitch_for_two_different_buyers():
    """Customization pays: the ORDERING of a store's own facts by value moves with the intent.

    One store, one catalogue, one price. Two shoppers who want different things. For each
    shopper the store is offered the same three facts to lead with, and the auction is driven
    once per fact per shopper — six requests — so what is measured is the store's own argmax
    over pitches, which is the quantity the store-agent learning loop optimises.

    Under the raw ratio all six requests answered the identical `verified_claim_ratio` (1.0:
    one verified claim of one decided), the argmax was a tie, and there was nothing for a
    learning loop to learn except that customization does not pay.
    """
    store = "store-one"
    facts = {"capacity_l": 35, "frame_material": "aluminium", "waterproof_rating": "ip67"}
    shoppers = {
        # Shopper A came for capacity and merely mentioned the frame. Stated as a PREFERENCE
        # rather than a hard constraint for the same reason as the test above: a constraint
        # would exclude the store outright on the two runs where it leads with another fact,
        # and an excluded candidate has no score to compare.
        "capacity-shopper": _asked_intent(
            prefer=["capacity_l"],
            query="a frame material that lasts",
        ),
        # Shopper B came for a waterproof rating and merely mentioned capacity.
        "waterproof-shopper": _asked_intent(
            prefer=["waterproof_rating"],
            query="does it have the capacity",
        ),
    }

    def value_of(fact_key, intent):
        bids = {store: _served_bid(store, 100.0, claims=[_served_claim(fact_key, facts[fact_key])])}
        body = _served_post(
            _multi_key_app(bids, stores=(store,), attributes={store: facts}),
            _served_roster({store: 100.0}),
            intent=intent,
        )
        return _served_features(body)[store]["verified_claim_ratio"]

    by_shopper = {
        name: {fact: value_of(fact, intent) for fact in facts} for name, intent in shoppers.items()
    }
    ordered = {
        name: sorted(values, key=lambda fact: (-values[fact], fact))
        for name, values in by_shopper.items()
    }

    assert ordered["capacity-shopper"][0] == "capacity_l", by_shopper
    assert ordered["waterproof-shopper"][0] == "waterproof_rating", by_shopper
    assert ordered["capacity-shopper"] != ordered["waterproof-shopper"], (
        f"the same store's facts rank identically for two different buyers, so its best "
        f"pitch is buyer-independent and customization pays nothing: {by_shopper}"
    )

    from contracts.ranking import EVIDENCE_GAIN_BY_RELEVANCE as GAIN

    # And the arithmetic, so the ordering is the published tiers rather than a coincidence.
    assert by_shopper["capacity-shopper"]["capacity_l"] == pytest.approx(GAIN["preference"])
    assert by_shopper["capacity-shopper"]["frame_material"] == pytest.approx(GAIN["query_term"])
    assert by_shopper["capacity-shopper"]["waterproof_rating"] == pytest.approx(0.5), (
        "a fact this shopper never mentioned was worth more than saying nothing"
    )
    assert by_shopper["waterproof-shopper"]["waterproof_rating"] == pytest.approx(
        GAIN["preference"]
    )
    assert by_shopper["waterproof-shopper"]["capacity_l"] == pytest.approx(GAIN["query_term"])
    assert by_shopper["waterproof-shopper"]["frame_material"] == pytest.approx(0.5)


def test_the_evidence_term_does_not_saturate_so_effort_keeps_paying():
    """One more relevant proved fact is always worth something, and always worth less.

    Both halves matter. Without diminishing returns the term is a claim-count race and the
    pitch that wins is the longest one; with a hard ceiling the term goes flat at full effort
    and every store converges on the same pitch again — which is the failure this whole
    section exists to prevent, arriving one step later.
    """
    from contracts.ranking import EVIDENCE_GAIN_BY_RELEVANCE, diminishing_evidence

    gain = EVIDENCE_GAIN_BY_RELEVANCE["hard_constraint"]
    values = [diminishing_evidence([gain] * n) for n in range(1, 9)]
    steps = [later - earlier for earlier, later in zip(values, values[1:], strict=False)]

    assert all(step > 0.0 for step in steps), f"the term saturates: {values}"
    assert all(later < earlier for earlier, later in zip(steps, steps[1:], strict=False)), (
        f"the returns are not diminishing: {steps}"
    )
    assert values[-1] < 1.0, "the aggregation reached its ceiling, so effort stopped paying"
    # And every published tier is worth more than silence on ONE claim, or the term would pay
    # a store to say nothing.
    assert all(value > 0.5 for value in EVIDENCE_GAIN_BY_RELEVANCE.values())


def test_a_store_discounting_past_the_band_gains_exactly_nothing_for_the_extra_depth():
    """R11's saturation, measured over the served route to five decimal places of zero.

    Three stores, one roster spanning 100..300 so the band is a real one, and the SAME store
    driven twice: once priced exactly at the band floor and once priced far below it. The
    difference in its `price_value` — and in its `rank_score` — is asserted to be 0.0 exactly
    rather than "small".
    """
    roster = _served_roster({"store-a": 100.0, "store-b": 200.0, "store-c": 300.0})
    intent = _asked_intent()

    def priced(store_a_price):
        bids = {
            "store-a": _served_bid("store-a", store_a_price),
            "store-b": _served_bid("store-b", 200.0),
            "store-c": _served_bid("store-c", 300.0),
        }
        body = _served_post(_served_app(bids), roster, intent=intent)
        return _served_features(body)["store-a"], _served_scores(body)["store-a"]

    # The band is cleared at a depth of (300-100)/300 = 2/3, so store-a (list 100) clears it
    # by charging 100 * (1 - 2/3) = 33.33... or less. 33.00 is the first cent past that.
    at_the_band, score_at = priced(33.00)
    past_the_band, score_past = priced(1.0)

    assert at_the_band["price_value"] == pytest.approx(1.0), at_the_band
    assert past_the_band["price_value"] - at_the_band["price_value"] == 0.0, (
        f"discounting from 33.00 to 1.00 — past the band — still moved price_value: "
        f"{at_the_band['price_value']} -> {past_the_band['price_value']}"
    )
    assert score_past - score_at == 0.0, (
        f"the extra depth still moved the rank score: {score_at} -> {score_past}"
    )

    # The positive control, without which the above is indistinguishable from a dead term:
    # depth INSIDE the band still pays.
    inside, _ = priced(80.0)
    assert 0.0 < inside["price_value"] < 1.0, inside
    assert inside["price_value"] < at_the_band["price_value"]


def test_a_contradicted_claim_costs_more_than_silence_and_silence_is_still_viable():
    """The only asymmetric downside in the design, and the bound on it.

    Four stores, same price, same list price, same trust, on an intent that asks about the
    key all four speak to:

    * `store-true` proves it and is believed;
    * `store-quiet` says nothing at all;
    * `store-liar` claims a value this exchange's own catalogue contradicts;
    * `store-liar-twice` does it twice, so the penalty is shown to accumulate.

    The ordering is the whole point: evidence beats silence beats lying, and the liar is
    still ranked rather than annihilated — one bad verdict must not be a death sentence.
    """
    stores = ("store-true", "store-quiet", "store-liar", "store-liar-twice")
    attributes = dict.fromkeys(stores, {"capacity_l": 35, "frame_material": "aluminium"})
    bids = {
        "store-true": _served_bid("store-true", 100.0, claims=[_served_claim("capacity_l", 35)]),
        "store-quiet": _served_bid("store-quiet", 100.0, claims=[]),
        "store-liar": _served_bid("store-liar", 100.0, claims=[_served_claim("capacity_l", 999)]),
        "store-liar-twice": _served_bid(
            "store-liar-twice",
            100.0,
            claims=[_served_claim("capacity_l", 999), _served_claim("frame_material", "gold")],
        ),
    }
    body = _served_post(
        _multi_key_app(bids, stores=stores, attributes=attributes),
        _served_roster(dict.fromkeys(stores, 100.0)),
        # A PREFERENCE and not a hard constraint: a contradicted claim on a hard constraint is
        # excluded by R19 before it is scored, and this test is about what a lie costs a
        # candidate that is still in the auction.
        intent=_asked_intent(prefer=["capacity_l", "frame_material"]),
    )
    scores = _served_scores(body)
    components = {row["store_id"]: row["components"] for row in body["ranked"]}

    from contracts.ranking import CONTRADICTED_CLAIM, DEFAULT_RANKING_WEIGHTS

    penalty = DEFAULT_RANKING_WEIGHTS.penalty_for(CONTRADICTED_CLAIM)
    assert penalty > 0.0, "the published catalogue prices a contradicted claim at nothing"

    assert scores["store-true"] > scores["store-quiet"] > scores["store-liar"], (
        f"evidence does not beat silence, or silence does not beat lying: {scores}"
    )
    assert scores["store-liar-twice"] < scores["store-liar"], (
        f"a second contradicted claim cost nothing: {scores}"
    )
    assert components["store-liar"]["policy_penalties"] == pytest.approx(-penalty)
    assert components["store-liar-twice"]["policy_penalties"] == pytest.approx(-2 * penalty)
    assert "policy_penalties" not in components["store-quiet"], components["store-quiet"]

    # STAYING SILENT IS STILL VIABLE: the quiet store is eligible, scored, and shortlisted.
    assert "store-quiet" in _served_slot_stores(body), body["shortlist"]
    # And a single bad verdict is survivable: the liar is still ranked, still shortlisted,
    # and is beatable rather than beaten into a negative score.
    assert scores["store-liar"] > 0.0, scores
    assert "store-liar" in _served_order(body), body["ranked"]
    # The bound: no amount of true evidence buys out one lie. The published penalty exceeds
    # the most the whole evidence term can pay a store.
    w_e = DEFAULT_RANKING_WEIGHTS.feature_weights["verified_claim_ratio"]
    neutral = float(DEFAULT_RANKING_WEIGHTS.normalization.verified_claim_ratio_when_absent)
    assert penalty > w_e * (1.0 - neutral), (
        "a store could out-evidence a contradiction, so the penalty is not a deterrent"
    )


def test_a_price_preference_cannot_become_a_second_price_term():
    """R6's structural rule: `intent_match` refuses a field a published term already scores.

    The measured hazard is S1's: its intent carries exactly ONE preference,
    `{price, minimize, 1.0}`, so with the min-max-across-the-eligible-set normalisation
    `retrieval.service` applies, an `intent_match` wired naively over that intent IS
    normalised inverse price — and price's share of the published weight goes from
    `w_v` = 0.15 to `w_v + w_m` = 0.50.

    Two halves, because the rule has to hold in both directions.
    """
    from contracts.ranking import (
        DEFAULT_RANKING_WEIGHTS,
        PREFERENCE_FIELD_TERMS,
        RANK_FEATURES,
        preference_term_conflict,
    )
    from exchange.ranking.features import intent_surface

    # 1. Every refusal names a term the published formula actually has, so the map cannot
    #    quietly refuse a field on behalf of a term that does not exist.
    assert set(PREFERENCE_FIELD_TERMS.values()) <= set(RANK_FEATURES)
    assert preference_term_conflict("price") == "price_value"
    assert preference_term_conflict("Price USD") == "price_value"
    assert preference_term_conflict("delivery_estimate_days") == "delivery_fit"
    assert preference_term_conflict("capacity_l") is None

    # 2. On S1's own intent shape, the price preference is refused by name and never reaches
    #    the soft-scoring surface — and the arithmetic of what admitting it would cost is
    #    asserted rather than described, so the number in the docstring cannot rot.
    surface = intent_surface(
        {
            "intent_id": "intent-s1-espresso",
            "hard_constraints": [],
            "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
            "query": "a heat-exchange espresso machine for an office under $500",
        }
    )
    assert surface.refused == {"price": "price_value"}
    assert "price" not in surface.tiers, surface.tiers
    weights = DEFAULT_RANKING_WEIGHTS.feature_weights
    assert weights["price_value"] + weights["intent_match"] == pytest.approx(0.50)
    assert weights["price_value"] == pytest.approx(0.15)


def test_the_published_versions_travel_with_the_answer():
    """R15/S3: a replay needs the FEATURE definitions as well as the weights.

    Redefining a feature under an unchanged weights version re-scores history silently —
    every recorded number still validates and every published weight still matches — so the
    ranker answers with both.
    """
    from contracts.ranking import RANKING_FEATURES_VERSION, RANKING_WEIGHTS_VERSION
    from exchange.ranking import rank

    result = rank([_candidate("bid-a")], _intent(), _snapshot(["store-a"]), _config())
    assert result["ranking_versions"] == {
        "weights": RANKING_WEIGHTS_VERSION,
        "features": RANKING_FEATURES_VERSION,
    }
    assert RANKING_FEATURES_VERSION != RANKING_WEIGHTS_VERSION, (
        "the two versions are the same string, so a feature redefinition is invisible"
    )
