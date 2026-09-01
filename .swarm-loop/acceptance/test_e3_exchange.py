"""Epic E3 — Exchange: auction fan-out, published ranking, accept, bandit, loss reports.

FROZEN acceptance suite. These 15 tests encode the SPEC's exchange-side promises through the
public surfaces the E3 tickets (T-030, T-032, T-033, T-034, T-035) must provide:

* R10 — bids fan out in parallel with a hard timeout; every store is represented, a silent
  Tier-1 store and every Tier-0 store falling back to a list-price offer (T-030).
* R11 — ranking is a published deterministic combination that is blind to network fee, tier
  and envelope contents, with a stable published tie-break (T-032).
* R19 / R12 / C10 — hard constraints are eligibility *filters* requiring verified supporting
  facts; blacklisted, expired-offer and off-domain-checkout candidates are excluded before
  any score is computed, each with a recorded exclusion reason (T-032).
* R2 / A6 — the shortlist is up to 4 differentiated slots carrying trust and provenance
  labels, collapsing gracefully below four distinct stores (T-032).
* R3 / A5 / C11 — accept returns a permalink and emits the golden LedgerEvent sequence, the
  redirect and Shopify modes emit identical event kinds, and a double accept is refused
  without creating a second code (T-033).
* R16 / R12 / S4 — the exposure bandit shifts with in-cluster outcomes, honours the
  exploration floor for low-data stores, and gives blacklisted stores exactly zero
  exposure (T-034).
* R9 — loss reports aggregate reason categories only, leaking no amounts and no rival store
  identities (T-035).

The three S8 release blockers that also carry `epic("E3")` live in `test_spec_criteria.py`.

Authoring rules (README): every product import happens INSIDE a test function, and every
test carries an `epic` and a `ticket` marker. Nothing here needs a database, a container,
a network socket or the wall clock: all timestamps are passed in as plain floats and every
seeded surface is seeded explicitly.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from urllib.parse import urlsplit

import pytest

# --- Fixed, deterministic clock values. Never `time.time()`. ------------------------
T_NOW = 1_700_000_000.0
T_PAST = 1_600_000_000.0
T_FUTURE = 2_000_000_000.0


# --- Tolerant accessors: the product may model these records as mappings or as objects.
def _has(obj, name: str) -> bool:
    if isinstance(obj, Mapping):
        return name in obj
    return hasattr(obj, name)


def _field(obj, name: str):
    if isinstance(obj, Mapping):
        assert name in obj, f"expected key {name!r} in {obj!r}"
        return obj[name]
    assert hasattr(obj, name), f"expected attribute {name!r} on {obj!r}"
    return getattr(obj, name)


def _as_dict(obj):
    if isinstance(obj, Mapping):
        return {str(k): v for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return {str(k): v for k, v in vars(obj).items()}
    return obj


def _scalars(node, key=None, seen=None):
    """Recursively yield ``(key, scalar)`` for every scalar reachable from ``node``."""
    if seen is None:
        seen = set()
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, Mapping):
        for k, v in node.items():
            yield from _scalars(v, str(k), seen)
    elif isinstance(node, (list, tuple, set, frozenset)):
        for v in node:
            yield from _scalars(v, key, seen)
    elif hasattr(node, "__dict__") and not isinstance(node, (str, bytes, bool, int, float)):
        for k, v in vars(node).items():
            yield from _scalars(v, str(k), seen)
    else:
        yield (key, node)


def _strings(node) -> str:
    """Lower-cased concatenation of every string reachable from ``node``."""
    return " ".join(str(v).lower() for _, v in _scalars(node) if isinstance(v, str))


# --- Input builders. Plain stdlib data; no product import at module scope. -----------
def _claim(key, value, source="owner_statement", status="verified"):
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


def _default_claims():
    return [_claim("capacity_l", 35), _claim("ships_in_days", 2)]


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
    store_domain=None,
    tier=1,
    network_fee=0.0,
    fee_rate=0.0,
):
    """An eligible bid candidate as the exchange hands it to the ranker."""
    store_id = store_id if store_id is not None else bid_id.replace("bid", "store")
    store_domain = store_domain if store_domain is not None else f"{store_id}.example.com"
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
            "total_price": total_price if total_price is not None else unit_price,
            "checkout_url": checkout_url,
            "expires_at": expires_at,
        },
        "claims": _default_claims() if claims is None else list(claims),
        "intent_match": intent_match,
        "price_value": price_value,
        "delivery_fit": delivery_fit,
        "verified_claim_ratio": verified_claim_ratio,
        "policy_penalties": policy_penalties,
    }


def _intent(hard_constraints=None):
    if hard_constraints is None:
        hard_constraints = [{"field": "capacity_l", "op": "gte", "value": 30}]
    return {
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "query": "a 30 litre commuter backpack",
        "category": "backpacks",
        "hard_constraints": list(hard_constraints),
        "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
        "ship_to": "US",
        "currency": "USD",
        "budget_band": "100-200",
        "created_at": T_PAST,
        "schema_version": "1.0.0",
    }


def _trust_snapshot(store_ids, *, scores=None, blacklisted=(), low_data=()):
    scores = scores or {}
    snapshot = {}
    for sid in store_ids:
        snapshot[sid] = {
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
    return snapshot


def _config():
    """Ranker config. `now` is supplied so no test depends on the wall clock."""
    return {"now": T_NOW}


# --- Result accessors ----------------------------------------------------------------
def _ranked(result):
    return list(_field(result, "ranked"))


def _all_candidates(result):
    return list(_field(result, "candidates"))


def _by_bid(result):
    return {_field(c, "bid_id"): c for c in _all_candidates(result)}


def _order(result):
    return [_field(c, "bid_id") for c in _ranked(result)]


def _slots(result):
    return list(_field(_field(result, "shortlist"), "slots"))


def _slot_refs(result):
    return [_field(s, "bid_ref") for s in _slots(result)]


def _unit_price(entry):
    node = _field(entry, "bid") if _has(entry, "bid") else entry
    return _field(_field(node, "offer"), "unit_price")


def _exposure_map(raw):
    """Normalize an exposure result to ``{store_id: float}``."""
    if isinstance(raw, Mapping):
        return {str(k): float(v) for k, v in raw.items()}
    out = {}
    for rec in raw:
        sid = str(_field(rec, "store_id"))
        for key in ("exposure", "share", "weight"):
            if _has(rec, key):
                out[sid] = float(_field(rec, key))
                break
        else:  # pragma: no cover - defensive
            raise AssertionError(f"exposure record {rec!r} carries no exposure/share/weight")
    return out


class _RecordingCodeCreator:
    """In-process stand-in for the merchant `POST /codes` client."""

    def __init__(self, code="PROXY-TEST-CODE", domain="store-a.example.com"):
        self.code = code
        self.permalink = f"https://{domain}/cart/1:1?discount={code}"
        self.calls = []

    def create_code(self, store_id, offer):
        self.calls.append((store_id, offer))
        return {"code": self.code, "permalink_url": self.permalink}

    __call__ = create_code


def _auction(bid_ids=("bid-a", "bid-b")):
    bids = []
    for bid_id in bid_ids:
        store_id = bid_id.replace("bid", "store")
        domain = f"{store_id}.example.com"
        bids.append(
            {
                "bid_id": bid_id,
                "store_id": store_id,
                "store_domain": domain,
                "offer": {
                    "product_ref": "product-1",
                    "unit_price": 100.0,
                    "total_price": 100.0,
                    "checkout_url": f"https://{domain}/cart/1:1?discount=NET",
                    "expires_at": T_FUTURE,
                },
            }
        )
    return {
        "auction_id": "auction-1",
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "bids": bids,
        "accepted_bid_ref": None,
        "now": T_NOW,
    }


def _event_kinds(result):
    return [_field(e, "kind") for e in _field(result, "events")]


# =====================================================================================
# T-030 — auction fan-out, timeout, universal representation (R10)
# =====================================================================================
@pytest.mark.epic("E3")
@pytest.mark.ticket("T-030")
def test_every_store_is_represented_including_silent_and_tier0():
    """R10: every rostered store is represented; a silent Tier-1 and all Tier-0 stores fall back to list price."""
    from apps.exchange.src.auction import collect_bids

    roster = [
        {"store_id": "store-r1", "tier": 1, "product_ref": "product-1", "list_price": 120.0},
        {"store_id": "store-r2", "tier": 1, "product_ref": "product-1", "list_price": 130.0},
        {"store_id": "store-r3", "tier": 1, "product_ref": "product-1", "list_price": 140.0},
        {"store_id": "store-silent", "tier": 1, "product_ref": "product-1", "list_price": 150.0},
        {"store_id": "store-t0a", "tier": 0, "product_ref": "product-1", "list_price": 160.0},
        {"store_id": "store-t0b", "tier": 0, "product_ref": "product-1", "list_price": 170.0},
    ]
    responses = [
        {
            "store_id": sid,
            "received_at": T_NOW - 1.0,
            "bid": {
                "auction_id": "auction-1",
                "store_id": sid,
                "offer": {"product_ref": "product-1", "unit_price": 80.0, "total_price": 80.0},
                "claims": _default_claims(),
            },
        }
        for sid in ("store-r1", "store-r2", "store-r3")
    ]

    entries = list(collect_bids(roster, responses, T_NOW))

    seen = [_field(e, "store_id") for e in entries]
    assert len(entries) == 6, f"expected one entry per rostered store, got {seen}"
    assert sorted(seen) == sorted(r["store_id"] for r in roster)

    by_store = {_field(e, "store_id"): e for e in entries}
    list_prices = {r["store_id"]: r["list_price"] for r in roster}

    for sid in ("store-r1", "store-r2", "store-r3"):
        assert _field(by_store[sid], "fallback") is False, f"{sid} responded; it is not a fallback"
        assert _unit_price(by_store[sid]) == 80.0

    for sid in ("store-silent", "store-t0a", "store-t0b"):
        assert _field(by_store[sid], "fallback") is True, f"{sid} must be a list-price fallback"
        assert _unit_price(by_store[sid]) == list_prices[sid]


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-030")
def test_bids_arriving_after_the_deadline_are_rejected():
    """R10: a response arriving after the passed-in deadline is rejected and replaced by the list-price fallback."""
    from apps.exchange.src.auction import collect_bids

    roster = [
        {"store_id": "store-ontime", "tier": 1, "product_ref": "product-1", "list_price": 120.0},
        {"store_id": "store-late", "tier": 1, "product_ref": "product-1", "list_price": 155.0},
    ]
    responses = [
        {
            "store_id": "store-ontime",
            "received_at": T_NOW - 0.5,
            "bid": {
                "auction_id": "auction-1",
                "store_id": "store-ontime",
                "offer": {"product_ref": "product-1", "unit_price": 90.0, "total_price": 90.0},
                "claims": _default_claims(),
            },
        },
        {
            # Arrives after the deadline with an irresistible price: it must not be used.
            "store_id": "store-late",
            "received_at": T_NOW + 5.0,
            "bid": {
                "auction_id": "auction-1",
                "store_id": "store-late",
                "offer": {"product_ref": "product-1", "unit_price": 1.0, "total_price": 1.0},
                "claims": _default_claims(),
            },
        },
    ]

    entries = list(collect_bids(roster, responses, T_NOW))
    by_store = {_field(e, "store_id"): e for e in entries}

    assert len(entries) == 2
    assert _field(by_store["store-ontime"], "fallback") is False
    assert _unit_price(by_store["store-ontime"]) == 90.0

    assert _field(by_store["store-late"], "fallback") is True, "a late bid must be rejected"
    assert _unit_price(by_store["store-late"]) == 155.0, "the late 1.0 price must not survive"


# =====================================================================================
# T-032 — published ranking, eligibility filters, shortlist (R11, R19, R12, R2, A6, C10)
# =====================================================================================
@pytest.mark.epic("E3")
@pytest.mark.ticket("T-032")
def test_rank_score_is_blind_to_fee_and_tier():
    """R11: rank_score and ordering are bit-identical when fee, tier and envelope-derived fields change."""
    from apps.exchange.src.ranking import rank

    def build():
        return [
            _candidate("bid-a", intent_match=0.95, price_value=0.90, delivery_fit=0.90),
            _candidate("bid-b", intent_match=0.60, price_value=0.55, delivery_fit=0.50),
            _candidate("bid-c", intent_match=0.20, price_value=0.15, delivery_fit=0.10),
        ]

    intent = _intent()
    snapshot = _trust_snapshot(["store-a", "store-b", "store-c"])

    plain = rank(build(), intent, snapshot, _config())
    baseline_scores = {_field(c, "bid_id"): _field(c, "rank_score") for c in _ranked(plain)}
    baseline_order = _order(plain)

    assert len(set(baseline_scores.values())) > 1, (
        "candidates with very different features must not all score identically — "
        "fee-blindness achieved by returning a constant is not ranking"
    )

    loaded = build()
    for i, cand in enumerate(loaded):
        cand["network_fee"] = 25.0 * (i + 1)
        cand["fee_rate"] = 0.05 * (i + 1)
        cand["tier"] = 2 - i
        cand["envelope_max_discount_pct"] = 40.0 - (10.0 * i)
        cand["envelope_budget_cap"] = 9_000.0 * (i + 1)
    # Reverse the fee gradient too: a fee-sensitive ranker cannot match both runs.
    loaded[0]["network_fee"], loaded[-1]["network_fee"] = (
        loaded[-1]["network_fee"],
        loaded[0]["network_fee"],
    )

    mutated = rank(loaded, intent, snapshot, _config())
    mutated_scores = {_field(c, "bid_id"): _field(c, "rank_score") for c in _ranked(mutated)}

    assert _order(mutated) == baseline_order, "fee/tier/envelope fields changed the ordering"
    assert mutated_scores == baseline_scores, "fee/tier/envelope fields changed rank_score"


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-032")
def test_rank_is_deterministic_and_tie_breaks_in_the_published_order():
    """R11: identical inputs give an identical ordering and component map; exact ties break by price then bid id."""
    from apps.exchange.src.ranking import rank

    def build():
        # Every scoring feature is identical, so these three are an exact score tie.
        # They differ only in raw total price and in bid id.
        return [
            _candidate("bid-c", store_id="store-c", unit_price=100.0, total_price=100.0),
            _candidate("bid-a", store_id="store-a", unit_price=100.0, total_price=100.0),
            _candidate("bid-b", store_id="store-b", unit_price=90.0, total_price=90.0),
        ]

    intent = _intent()
    snapshot = _trust_snapshot(["store-a", "store-b", "store-c"], scores={s: 0.5 for s in ("store-a", "store-b", "store-c")})

    first = rank(build(), intent, snapshot, _config())
    second = rank(build(), intent, snapshot, _config())

    assert _order(first) == _order(second), "ranking is not deterministic across identical runs"
    assert [_as_dict(_field(c, "components")) for c in _ranked(first)] == [
        _as_dict(_field(c, "components")) for c in _ranked(second)
    ], "component maps differ across identical runs"

    scores = [_field(c, "rank_score") for c in _ranked(first)]
    assert len(set(scores)) == 1, (
        "these candidates differ only in raw price and bid id; the published formula must "
        f"score them identically so the tie-break is what orders them (got {scores})"
    )
    assert _order(first) == ["bid-b", "bid-a", "bid-c"], (
        "published tie-break is price ascending, then bid id ascending"
    )

    reversed_input = list(reversed(build()))
    assert _order(rank(reversed_input, intent, snapshot, _config())) == _order(first), (
        "the tie-break must be stable: input order must not decide output order"
    )


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-032")
def test_eligibility_filters_run_before_scoring_and_record_exclusion_reasons():
    """R19/R12/C10: blacklisted, expired, off-domain and hard-constraint-failing candidates are excluded before scoring, with reasons."""
    from apps.exchange.src.ranking import rank

    good = _candidate("bid-good", store_id="store-good")
    blacklisted = _candidate("bid-black", store_id="store-black")
    expired = _candidate("bid-exp", store_id="store-exp", expires_at=T_PAST)
    offdomain = _candidate(
        "bid-dom",
        store_id="store-dom",
        checkout_url="https://attacker.tld/cart/1:1?discount=NET",
    )
    hard_fail = _candidate(
        "bid-hard",
        store_id="store-hard",
        claims=[_claim("capacity_l", 12, status="contradicted"), _claim("ships_in_days", 2)],
    )

    candidates = [good, blacklisted, expired, offdomain, hard_fail]
    snapshot = _trust_snapshot(
        [c["store_id"] for c in candidates], blacklisted=("store-black",)
    )

    result = rank(candidates, _intent(), snapshot, _config())
    records = _by_bid(result)

    expected_reason_token = {
        "bid-black": "blacklist",
        "bid-exp": "expir",
        "bid-dom": "domain",
        "bid-hard": "constraint",
    }
    for bid_id, token in expected_reason_token.items():
        rec = records[bid_id]
        assert _field(rec, "eligible") is False, f"{bid_id} must be ineligible"
        assert _field(rec, "rank_score") is None, (
            f"{bid_id} was scored; eligibility filters must run before any score is computed"
        )
        assert not _field(rec, "components"), f"{bid_id} must carry no component map"
        reasons = _field(rec, "exclusion_reasons")
        assert reasons, f"{bid_id} must record why it was excluded"
        assert token in _strings(reasons), (
            f"{bid_id}'s exclusion_reasons {reasons!r} do not name the {token!r} filter"
        )
        assert bid_id not in _order(result)
        assert bid_id not in _slot_refs(result)

    assert _field(records["bid-good"], "eligible") is True
    assert isinstance(_field(records["bid-good"], "rank_score"), float)
    assert _field(records["bid-good"], "components")
    assert _order(result) == ["bid-good"]


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-032")
def test_ambiguous_evidence_cannot_satisfy_a_hard_constraint():
    """R19: only a `verified` supporting claim satisfies a hard constraint; ambiguous, unsupported and contradicted do not."""
    from apps.exchange.src.ranking import rank

    statuses = {
        "bid-verified": "verified",
        "bid-ambiguous": "ambiguous",
        "bid-unsupported": "unsupported",
        "bid-contradicted": "contradicted",
    }
    candidates = [
        _candidate(
            bid_id,
            store_id=bid_id.replace("bid", "store"),
            # Everything else is maximal and identical, so only the evidence status can decide.
            intent_match=1.0,
            price_value=1.0,
            delivery_fit=1.0,
            claims=[_claim("capacity_l", 35, status=status), _claim("ships_in_days", 2)],
        )
        for bid_id, status in statuses.items()
    ]
    snapshot = _trust_snapshot([c["store_id"] for c in candidates], scores={c["store_id"]: 0.9 for c in candidates})

    result = rank(candidates, _intent(), snapshot, _config())
    records = _by_bid(result)

    assert _field(records["bid-verified"], "eligible") is True
    for bid_id in ("bid-ambiguous", "bid-unsupported", "bid-contradicted"):
        assert _field(records[bid_id], "eligible") is False, (
            f"{statuses[bid_id]} evidence must not satisfy a hard constraint"
        )
        assert bid_id not in _slot_refs(result)

    assert _order(result) == ["bid-verified"]
    assert _slot_refs(result) == ["bid-verified"]


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-032")
def test_shortlist_collapses_gracefully_below_four_distinct_stores():
    """R2/A6: 1-3 distinct eligible stores yield 1-3 slots without error; 5 yield exactly 4, never a duplicate store."""
    from apps.exchange.src.ranking import rank

    letters = "abcde"
    for n in (1, 2, 3, 5):
        candidates = [
            _candidate(f"bid-{letters[i]}", store_id=f"store-{letters[i]}") for i in range(n)
        ]
        snapshot = _trust_snapshot([c["store_id"] for c in candidates])
        result = rank(candidates, _intent(), snapshot, _config())

        refs = _slot_refs(result)
        expected = min(n, 4)
        assert len(refs) == expected, f"{n} eligible stores must yield {expected} slots, got {refs}"
        assert len(set(refs)) == len(refs), f"duplicate bid_ref in slots: {refs}"

        store_of = {c["bid_id"]: c["store_id"] for c in candidates}
        stores = [store_of[r] for r in refs]
        assert len(set(stores)) == len(stores), f"the same store occupies two slots: {stores}"


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-032")
def test_shortlist_slots_carry_trust_and_provenance_labels():
    """R2: every slot names its differentiator, its fit score, a trust summary, and store-confirmed vs from-their-website provenance."""
    from apps.exchange.src.ranking import rank

    owner_claims = [_claim("capacity_l", 35, source="owner_statement"), _claim("returns_days", 30, source="envelope_rule")]
    scraped_claims = [_claim("capacity_l", 35, source="scraped"), _claim("returns_days", 30, source="scraped")]

    candidates = [
        _candidate("bid-a", store_id="store-a", claims=owner_claims, intent_match=0.95),
        _candidate("bid-b", store_id="store-b", claims=scraped_claims, intent_match=0.85),
        _candidate("bid-c", store_id="store-c", claims=owner_claims, intent_match=0.75),
        _candidate("bid-d", store_id="store-d", claims=scraped_claims, intent_match=0.65),
    ]
    snapshot = _trust_snapshot(
        [c["store_id"] for c in candidates],
        scores={"store-a": 0.9, "store-b": 0.7, "store-c": 0.5, "store-d": 0.3},
    )

    slots = _slots(rank(candidates, _intent(), snapshot, _config()))
    assert len(slots) == 4

    kinds = [_field(s, "slot") for s in slots]
    assert set(kinds) <= {"fit", "value", "reliability", "specialist"}, kinds
    assert len(set(kinds)) == 4, f"the four slots must be differentiated, got {kinds}"

    source_of = {c["bid_id"]: c["claims"][0]["provenance"]["source"] for c in candidates}
    for slot in slots:
        ref = _field(slot, "bid_ref")
        assert isinstance(_field(slot, "fit_score"), float)
        assert _field(slot, "trust_summary"), f"slot {ref} carries no trust summary"

        labels = _strings(_field(slot, "provenance_labels"))
        assert labels, f"slot {ref} carries no provenance labels"
        if source_of[ref] == "scraped":
            assert "from their website" in labels, ref
            assert "store-confirmed" not in labels, ref
        else:
            assert "store-confirmed" in labels, ref
            assert "from their website" not in labels, ref


# =====================================================================================
# T-033 — accept, permalink, event parity, double accept (R3, A5, C11)
# =====================================================================================
@pytest.mark.epic("E3")
@pytest.mark.ticket("T-033")
def test_accept_returns_a_permalink_and_emits_the_golden_event_sequence():
    """R3: accepting an offer delegates code creation, returns that permalink, and emits accepted -> code_created -> checkout_redirect."""
    from apps.exchange.src.accept import accept

    creator = _RecordingCodeCreator()
    result = accept(_auction(), "bid-a", creator, "shopify")

    assert len(creator.calls) == 1, f"code creation must be delegated exactly once, got {creator.calls}"
    assert creator.calls[0][0] == "store-a"

    permalink = _field(result, "permalink_url")
    assert isinstance(permalink, str) and permalink
    assert creator.code in permalink, "the returned permalink must carry the created code"
    assert urlsplit(permalink).hostname == "store-a.example.com"

    kinds = _event_kinds(result)
    golden = ["accepted", "code_created", "checkout_redirect"]
    assert [k for k in kinds if k in golden] == golden, (
        f"expected the golden accept sequence {golden} in order, got {kinds}"
    )


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-033")
def test_redirect_and_shopify_modes_emit_identical_event_kinds():
    """C11: the simulated/redirect checkout path and the Shopify path emit the identical ordered LedgerEvent kinds."""
    from apps.exchange.src.accept import accept

    redirect_kinds = _event_kinds(
        accept(copy.deepcopy(_auction()), "bid-a", _RecordingCodeCreator(), "redirect")
    )
    shopify_kinds = _event_kinds(
        accept(copy.deepcopy(_auction()), "bid-a", _RecordingCodeCreator(), "shopify")
    )

    assert redirect_kinds == shopify_kinds, (
        f"event kinds diverge by checkout mode: redirect={redirect_kinds} shopify={shopify_kinds}"
    )
    for kind in ("accepted", "code_created", "checkout_redirect"):
        assert kind in redirect_kinds, f"{kind} missing — an empty event stream is not parity"


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-033")
def test_double_accept_on_one_auction_is_rejected():
    """R3/A5: a second accept on an already-accepted auction is refused and creates no second discount code."""
    from apps.exchange.src.accept import accept

    creator = _RecordingCodeCreator()
    auction = _auction()

    first = accept(auction, "bid-a", creator, "shopify")
    assert _field(first, "permalink_url")
    assert len(creator.calls) == 1

    refused = True
    second = None
    try:
        second = accept(auction, "bid-b", creator, "shopify")
    except Exception:  # an exception is a perfectly good refusal
        pass
    else:
        refused = not _field(second, "permalink_url")
        assert "code_created" not in _event_kinds(second), (
            "the second accept emitted a second code_created event"
        )

    assert refused, "a double accept on one auction must not issue a second permalink"
    assert len(creator.calls) == 1, (
        f"a second discount code was created for an already-accepted auction: {creator.calls}"
    )


# =====================================================================================
# T-034 — exposure bandit (R16, R12, S4)
# =====================================================================================
@pytest.mark.epic("E3")
@pytest.mark.ticket("T-034")
def test_positive_outcomes_raise_a_store_exposure_in_its_cluster():
    """R16/S4: positive conversion outcomes raise a store's exposure inside its own cluster and leave other clusters untouched."""
    from apps.exchange.src.policy import exposure, initial_state, update

    stores = ["store-a", "store-b", "store-c"]
    clusters = ["cluster-1", "cluster-2"]
    state = initial_state(stores, clusters, _trust_snapshot(stores), {"exploration_floor": 0.10})

    before_c1 = _exposure_map(exposure(state, "cluster-1", 7))
    before_c2 = _exposure_map(exposure(state, "cluster-2", 7))

    outcomes = []
    for _ in range(40):
        outcomes.append({"store_id": "store-a", "cluster_id": "cluster-1", "converted": True})
        outcomes.append({"store_id": "store-b", "cluster_id": "cluster-1", "converted": False})
        outcomes.append({"store_id": "store-c", "cluster_id": "cluster-1", "converted": False})

    updated = update(state, outcomes)

    after_c1 = _exposure_map(exposure(updated, "cluster-1", 7))
    after_c2 = _exposure_map(exposure(updated, "cluster-2", 7))

    assert after_c1["store-a"] > before_c1["store-a"], (
        f"positive outcomes did not raise exposure: {before_c1['store-a']} -> {after_c1['store-a']}"
    )
    assert after_c1["store-a"] > after_c1["store-b"], "the converting store must outrank its losing rivals"
    assert after_c2 == before_c2, (
        f"cluster-2 exposure moved on cluster-1 outcomes: {before_c2} -> {after_c2}"
    )
    assert _exposure_map(exposure(updated, "cluster-1", 7)) == after_c1, "exposure is not seed-deterministic"


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-034")
def test_exploration_slice_floor_holds_for_a_low_data_store():
    """R12: a low-data store keeps at least the configured exploration-slice floor of exposure across a seeded window."""
    from apps.exchange.src.policy import exposure, initial_state, update

    # Four established stores with a long winning record against one brand-new store, and a
    # floor high enough that only a real exploration slice can satisfy it: on the outcome
    # evidence alone `store-new` would draw well under a tenth of the exposure.
    established = ["store-a", "store-b", "store-c", "store-d"]
    stores = established + ["store-new"]
    floor = 0.25
    state = initial_state(
        stores,
        ["cluster-1"],
        _trust_snapshot(stores, low_data=("store-new",)),
        {"exploration_floor": floor},
    )

    outcomes = []
    for _ in range(60):
        for sid in established:
            outcomes.append({"store_id": sid, "cluster_id": "cluster-1", "converted": True})
    updated = update(state, outcomes)

    for seed in range(5):
        shares = _exposure_map(exposure(updated, "cluster-1", seed))
        assert shares.get("store-new", 0.0) >= floor - 1e-9, (
            f"seed {seed}: low-data store fell below the exploration floor ({shares})"
        )


@pytest.mark.epic("E3")
@pytest.mark.ticket("T-034")
def test_blacklisted_stores_draw_zero_exposure():
    """R12: a blacklisted store draws exactly zero exposure across the seeded window, exploration floor and good outcomes notwithstanding."""
    from apps.exchange.src.policy import exposure, initial_state, update

    stores = ["store-a", "store-b", "store-banned"]
    state = initial_state(
        stores,
        ["cluster-1"],
        # Blacklisted *and* low-data: neither the bandit nor the exploration floor may admit it.
        _trust_snapshot(stores, blacklisted=("store-banned",), low_data=("store-banned",)),
        {"exploration_floor": 0.10},
    )

    outcomes = []
    for _ in range(50):
        outcomes.append({"store_id": "store-banned", "cluster_id": "cluster-1", "converted": True})
        outcomes.append({"store_id": "store-a", "cluster_id": "cluster-1", "converted": False})
    updated = update(state, outcomes)

    for seed in range(10):
        shares = _exposure_map(exposure(updated, "cluster-1", seed))
        assert shares.get("store-banned", 0.0) == 0.0, (
            f"seed {seed}: blacklisted store drew exposure {shares.get('store-banned')}"
        )
        assert sum(v for k, v in shares.items() if k != "store-banned") > 0.0, (
            f"seed {seed}: no store drew any exposure at all ({shares})"
        )


# =====================================================================================
# T-035 — loss reports (R9)
# =====================================================================================
@pytest.mark.epic("E3")
@pytest.mark.ticket("T-035")
def test_loss_reports_carry_reason_categories_and_no_amounts():
    """R9: loss reports aggregate reason categories per cluster inside the window and leak no amounts and no rival identities."""
    from apps.exchange.src.reports import build_loss_report

    def loss(auction_id, cluster_id, reason, ts, unmet=(), rival="store-rival-alpha", price=199.99):
        return {
            "auction_id": auction_id,
            "cluster_id": cluster_id,
            "ts": ts,
            "store_id": "store-loser",
            "won": False,
            "reason": reason,
            "unmet_criteria": list(unmet),
            # Amounts and rival identities are present in the INPUT on purpose: the report
            # must aggregate them away, not copy them through.
            "offer": {"unit_price": price, "total_price": price, "discount": {"type": "percentage", "value": 15.0}},
            "winning_price": 149.5,
            "rival_store_id": rival,
        }

    window = {"start": T_NOW - 3600.0, "end": T_NOW}
    auction_log = [
        loss("auction-1", "cluster-1", "fit", T_NOW - 3000.0, unmet=["capacity_l >= 30"]),
        loss("auction-2", "cluster-1", "fit", T_NOW - 2000.0, unmet=["capacity_l >= 30"]),
        loss("auction-3", "cluster-1", "price", T_NOW - 1000.0, rival="store-rival-beta"),
        loss("auction-4", "cluster-2", "trust", T_NOW - 900.0),
        loss("auction-5", "cluster-2", "commitments", T_NOW - 800.0, unmet=["ships_in_days <= 2"]),
        # Outside the window: must not be counted.
        loss("auction-6", "cluster-1", "fit", T_NOW - 7200.0, unmet=["capacity_l >= 30"]),
    ]

    report = build_loss_report(auction_log, window)
    if isinstance(report, (list, tuple)):
        assert len(report) == 1, f"one store lost in this log; got {len(report)} reports"
        report = report[0]

    assert _field(report, "store_id") == "store-loser"

    by_cluster = {_field(c, "cluster_id"): c for c in _field(report, "by_cluster")}
    assert set(by_cluster) == {"cluster-1", "cluster-2"}, sorted(by_cluster)

    golden = {
        "cluster-1": {"fit": 2, "price": 1, "commitments": 0, "trust": 0},
        "cluster-2": {"fit": 0, "price": 0, "commitments": 1, "trust": 1},
    }
    for cluster_id, expected in golden.items():
        entry = by_cluster[cluster_id]
        assert _field(entry, "lost") == sum(expected.values()), cluster_id
        reasons = _as_dict(_field(entry, "reasons"))
        assert set(reasons) == {"fit", "price", "commitments", "trust"}, reasons
        assert {k: int(v) for k, v in reasons.items()} == expected, (
            f"{cluster_id}: expected {expected}, got {reasons}"
        )

    assert "capacity_l >= 30" in list(_field(by_cluster["cluster-1"], "unmet_criteria"))

    forbidden_keys = {
        "unit_price",
        "total_price",
        "discount",
        "winning_price",
        "rival_store_id",
        "offer",
        "amount",
    }
    forbidden_numbers = {199.99, 149.5, 15.0}
    forbidden_text = ("store-rival-alpha", "store-rival-beta", "199.99", "149.5")

    for key, value in _scalars(report):
        assert key not in forbidden_keys, f"loss report leaks field {key!r}"
        if isinstance(value, float):
            assert value not in forbidden_numbers, f"loss report leaks amount {value!r} at {key!r}"
        if isinstance(value, str):
            for needle in forbidden_text:
                assert needle not in value, f"loss report leaks {needle!r} at {key!r}"
