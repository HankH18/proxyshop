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
    """One supporting fact, carrying the exchange's SEALED verdict on it (ESC-020).

    `status` names the verdict this fixture is asking the exchange to have reached; it is
    handed to the sealer rather than written onto the claim, because a `status` written onto
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
