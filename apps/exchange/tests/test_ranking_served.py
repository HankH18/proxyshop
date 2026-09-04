"""T-310 — the published ranking on a served request, driven through the real app.

Ticket verify: ``pytest apps/exchange/tests/test_ranking_served.py -q``.

``apps/exchange/tests/test_ranking.py`` grades ``rank()`` as a library and does it thoroughly.
Nothing there could see T-310, because a unit test constructs the ranker itself: the defect was
that no *process* did. So every assertion in this file goes through
``exchange.main.create_app()`` and a ``TestClient`` — the same object ``uvicorn
exchange.main:app`` builds — rather than through an import of the package under test. A green
suite over a function nobody calls is exactly the shape T-310 was.

Three properties, and they are separate on purpose:

1. **Reachability and the published path.** The app mounts ``exchange.ranking.routes`` and
   answers ``GET /auctions/{auction_id}/shortlist``, which
   ``packages/contracts/openapi/exchange.openapi.json`` has declared all along.
2. **The filters actually decide a served auction.** A candidate that fails an R19 hard
   constraint, R12's blacklist read or C10's checkout-domain check is reported excluded and is
   in no shortlist slot — over the HTTP door, not over ``rank()``.
3. **A bidder cannot score itself.** The candidate handed to the scorer is assembled by NAMING
   its fields, so no key a store writes into its reply reaches the formula. This is asserted
   twice: a parametrised probe over the specific keys worth gaming (each closes exactly one),
   and a randomised property over arbitrary keys (which closes the class, including the key
   nobody thought to list).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from contracts.ranking import DEFAULT_RANKING_WEIGHTS, RANK_FEATURES
from exchange.auction.collect import BidEntry
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.candidates import (
    CANDIDATE_FIELDS,
    candidate_from_entry,
    mint_bid_id,
)
from exchange.ranking.serving import ShortlistStore, configure_ranking, rank_auction
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Far enough ahead that no offer here expires while the suite runs, and computed from the
#: clock rather than hard-coded because the route ranks against ``time.time()`` at close.
LIVE_FOR_AN_HOUR = 3600.0

STORE_A = "store-a"
STORE_B = "store-b"
STORE_OFF_DOMAIN = "store-offdomain"
STORE_BLACKLISTED = "store-black"


# --- builders -------------------------------------------------------------------------
def _domain(store_id: str) -> str:
    return f"{store_id}.example.com"


def _claim(key: str, value: Any, *, status: str = "verified") -> dict[str, Any]:
    return {
        "key": key,
        "value": value,
        "status": status,
        "provenance": {"source": "owner_statement", "ref": f"ref:{key}", "authority_rank": 1},
    }


def _rostered(store_id: str, list_price: float) -> dict[str, Any]:
    """A roster row that states no authorised discount depth.

    Deliberate: with no ``max_discount_pct`` and no declared discount on the reply, the T-177
    price wall holds nothing to judge, so every bid below reaches the ranker as a real bid
    rather than degrading to a list-price fallback for a reason this file is not about.
    """
    return {
        "store_id": store_id,
        "tier": 1,
        "product_ref": "product-1",
        "list_price": list_price,
    }


def _offer(price: float, store_id: str, *, expires_in: float = LIVE_FOR_AN_HOUR) -> dict[str, Any]:
    return {
        "product_ref": "product-1",
        "unit_price": price,
        "total_price": price,
        "currency": "USD",
        "checkout_url": f"https://{_domain(store_id)}/cart/1:1",
        "expires_at": time.time() + expires_in,
    }


class Bidders:
    """The outbound bid client, answering from a table. Every reply is a protocol ``Bid``."""

    def __init__(self, bids: dict[str, dict[str, Any]]) -> None:
        self.bids = dict(bids)
        self.asked: list[str] = []

    def solicit(self, store: dict[str, Any]) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        self.asked.append(store_id)
        bid = self.bids.get(store_id)
        if bid is None:
            return None
        return {"store_id": store_id, "received_at": time.time(), "bid": dict(bid)}

    __call__ = solicit


def _bid(store_id: str, price: float, *, claims: list[dict[str, Any]] | None = None) -> dict:
    return {
        "auction_id": None,
        "store_id": store_id,
        "offer": _offer(price, store_id),
        "claims": [_claim("capacity_l", 35)] if claims is None else claims,
        "agent_version": "1.0.0",
        "schema_version": "1.0.0",
    }


def _intent(hard_constraints: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "hard_constraints": [{"field": "capacity_l", "op": "gte", "value": 30}]
        if hard_constraints is None
        else hard_constraints,
    }


def _wired_app(
    *,
    bidders: Bidders,
    trust_snapshot: dict[str, Any] | None = None,
    domains: dict[str, str] | None = None,
    stores: tuple[str, ...] = (STORE_A, STORE_B),
) -> Any:
    """A fully wired exchange: eligibility, a bid client, a trust snapshot, a seller registry."""
    app = create_app()
    configure_auctions(
        app,
        solicitor=bidders,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot=(
            {store: {"blacklisted": False, "score": 0.6} for store in stores}
            if trust_snapshot is None
            else trust_snapshot
        ),
        registered_domains=StaticRegisteredDomains(
            {store: _domain(store) for store in stores} if domains is None else domains
        ),
    )
    return app


def _post(app: Any, roster: list[dict[str, Any]], intent: dict[str, Any] | None = None) -> dict:
    response = TestClient(app).post(
        "/auctions",
        json={
            "intent": _intent() if intent is None else intent,
            "roster": roster,
            "bid_timeout_seconds": 2.0,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# =====================================================================================
# 1. The ranker is on a served path, and it is THIS tree's ranker
# =====================================================================================
def test_the_served_app_mounts_the_ranking_router_and_answers_the_published_shortlist_path():
    """The evidence for T-310, taken off the real application object.

    The resolved ``__file__`` of every ``exchange.ranking`` module the app pulled in is
    asserted to be inside this checkout. The venv's ``_proxyshop.pth`` puts a checkout root
    and its ``.pkgroot`` on ``sys.path`` for every process that uses it, so "the package
    imported" is not by itself evidence that THIS tree's package imported.
    """
    import sys  # noqa: PLC0415

    app = create_app()

    assert "exchange.ranking.routes" in app.state.mounted_routers, (
        f"the frozen entrypoint did not discover the ranking router: {app.state.mounted_routers}"
    )
    assert "/auctions/{auction_id}/shortlist" in app.openapi()["paths"], sorted(
        app.openapi()["paths"]
    )

    ranking_modules = {
        name: getattr(module, "__file__", "")
        for name, module in sys.modules.items()
        if name == "exchange.ranking" or name.startswith("exchange.ranking.")
    }
    assert ranking_modules, "building the app imported no exchange.ranking module"
    for name, filename in sorted(ranking_modules.items()):
        assert filename, f"{name} has no __file__ to check"
        assert REPO_ROOT in Path(filename).resolve().parents, (
            f"{name} resolved to {filename}, outside the tree under test ({REPO_ROOT})"
        )


# =====================================================================================
# 2. The filters decide a SERVED auction
# =====================================================================================
def test_a_served_auction_scores_its_bids_and_publishes_a_shortlist():
    bidders = Bidders({STORE_A: _bid(STORE_A, 100.0), STORE_B: _bid(STORE_B, 120.0)})
    app = _wired_app(bidders=bidders)

    body = _post(app, [_rostered(STORE_A, 100.0), _rostered(STORE_B, 120.0)])

    assert body["excluded"] == [], body["excluded"]
    ranked = body["ranked"]
    assert [row["store_id"] for row in ranked] == [STORE_A, STORE_B], ranked
    # Every candidate's components sum to its score — the audit trail is real, not decorative.
    for row in ranked:
        assert row["rank_score"] == pytest.approx(sum(row["components"].values()))
    # Both stores are identical on every feature the formula reads, so the scores TIE and the
    # published price tie-break is what ordered them. That is the ranking running, not the
    # fan-out's arrival order.
    assert ranked[0]["rank_score"] == pytest.approx(ranked[1]["rank_score"])

    slots = body["shortlist"]["slots"]
    assert body["shortlist"]["auction_id"] == body["auction_id"]
    assert [slot["bid_ref"] for slot in slots] == [
        mint_bid_id(body["auction_id"], STORE_A),
        mint_bid_id(body["auction_id"], STORE_B),
    ]
    assert {slot["slot"] for slot in slots} == {"fit", "value"}


def test_a_hard_constraint_the_bid_cannot_evidence_keeps_it_out_of_the_shortlist():
    """R19 on a served request — the property T-310 says never ran on one.

    ``store-b`` answers with a claim that does not satisfy the intent's ``capacity_l >= 30``.
    Before the wiring it was offered to the buyer anyway, because nothing on the served path
    consulted a hard constraint at all.
    """
    bidders = Bidders(
        {
            STORE_A: _bid(STORE_A, 100.0),
            STORE_B: _bid(STORE_B, 60.0, claims=[_claim("capacity_l", 12)]),
        }
    )
    app = _wired_app(bidders=bidders)

    body = _post(app, [_rostered(STORE_A, 100.0), _rostered(STORE_B, 60.0)])

    assert [row["store_id"] for row in body["ranked"]] == [STORE_A]
    excluded = {row["store_id"]: row for row in body["excluded"]}
    assert list(excluded) == [STORE_B]
    assert any("hard_constraint" in reason for reason in excluded[STORE_B]["exclusion_reasons"]), (
        excluded[STORE_B]["exclusion_reasons"]
    )
    # And the cheaper, non-conforming bid is in NO slot. It is the price-leading offer, so a
    # shortlist that had skipped the filters would have led with it.
    assert [slot["bid_ref"] for slot in body["shortlist"]["slots"]] == [
        mint_bid_id(body["auction_id"], STORE_A)
    ]


@pytest.mark.parametrize(
    ("label", "snapshot_override", "domain_override", "expected_reason"),
    [
        ("blacklisted", {"blacklisted": True, "score": 0.6}, None, "blacklisted"),
        ("unreadable blacklist", {"score": 0.6}, None, "blacklist_unreadable"),
        ("no snapshot row", "drop", None, "blacklist_unreadable"),
        ("unregistered domain", None, "drop", "off_domain"),
        ("wrong registered domain", None, "elsewhere.example.com", "off_domain"),
    ],
)
def test_each_eligibility_filter_refuses_a_store_on_the_served_path(
    label, snapshot_override, domain_override, expected_reason
):
    """One parametrisation per filter, each closing exactly one way through the door."""
    snapshot: dict[str, Any] = {store: {"blacklisted": False, "score": 0.6} for store in (STORE_A,)}
    domains = {STORE_A: _domain(STORE_A)}
    if snapshot_override == "drop":
        snapshot.pop(STORE_A)
    elif snapshot_override is not None:
        snapshot[STORE_A] = snapshot_override
    if domain_override == "drop":
        domains.pop(STORE_A)
    elif domain_override is not None:
        domains[STORE_A] = domain_override

    app = _wired_app(
        bidders=Bidders({STORE_A: _bid(STORE_A, 100.0)}),
        trust_snapshot=snapshot,
        domains=domains,
        stores=(STORE_A,),
    )
    body = _post(app, [_rostered(STORE_A, 100.0)])

    assert body["ranked"] == [], f"[{label}] a refused store was scored anyway"
    assert body["shortlist"]["slots"] == [], f"[{label}] a refused store reached a slot"
    reasons = " ".join(body["excluded"][0]["exclusion_reasons"])
    assert expected_reason in reasons, f"[{label}] expected {expected_reason!r} in {reasons!r}"


@pytest.fixture
def no_platform_registry():
    """Neutralise the PROCESS-WIDE registered-domain source for one test, and restore it.

    ``exchange.accept.offer`` keeps that source in module state, and two tests in this
    directory (``test_accept.py:651`` and ``:663``) wire a real table into it and never put it
    back. ``registered_domains_of`` falls back to that source by design — a deployment should
    not have to wire the seller registry twice — so without this fixture the one test in this
    file that asserts the UNWIRED default would be asserting whatever an earlier file happened
    to leave behind, and would flip from real to vacuous purely on collection order.
    """
    from exchange.accept import use_registered_domains  # noqa: PLC0415

    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


def test_an_unwired_exchange_ranks_nothing_rather_than_ranking_everything(no_platform_registry):
    """The deployment reading of fail-closed, applied to the ranking as well as the gate.

    An exchange with no trust snapshot and no seller registry can establish neither that a
    store is off the blacklist (R12) nor that its checkout host is one the platform registered
    (C10/D22). It therefore publishes an empty shortlist — not a shortlist of everything it
    could not check.
    """
    app = create_app()
    configure_auctions(
        app,
        solicitor=Bidders({STORE_A: _bid(STORE_A, 100.0)}),
        eligibility=StaticSellerEligibility({STORE_A: ELIGIBLE}),
    )

    body = _post(app, [_rostered(STORE_A, 100.0)])

    assert body["entries"], "the auction itself must still report what the store offered"
    assert body["ranked"] == []
    assert body["shortlist"]["slots"] == []
    reasons = " ".join(body["excluded"][0]["exclusion_reasons"])
    assert "blacklist_unreadable" in reasons and "off_domain" in reasons, reasons


def test_the_ranking_reads_the_platform_registry_the_accept_path_was_already_wired_with(
    no_platform_registry,
):
    """One seller registry, wired once — not two that can disagree about who owns a host.

    ``use_registered_domains`` is the deployment seam the accept path already uses (T-169).
    An exchange that has wired it, and has said nothing about ranking, must rank against that
    same table: two sources would mean the checkout the ranker vouched for and the checkout
    the accept minted could be checked against different hosts.
    """
    from exchange.accept import use_registered_domains  # noqa: PLC0415

    use_registered_domains(StaticRegisteredDomains({STORE_A: _domain(STORE_A)}))

    app = create_app()
    configure_auctions(
        app,
        solicitor=Bidders({STORE_A: _bid(STORE_A, 100.0)}),
        eligibility=StaticSellerEligibility({STORE_A: ELIGIBLE}),
    )
    # Deliberately does NOT pass registered_domains — the fallback is what is under test.
    configure_ranking(app, trust_snapshot={STORE_A: {"blacklisted": False, "score": 0.6}})

    body = _post(app, [_rostered(STORE_A, 100.0)])

    assert [row["store_id"] for row in body["ranked"]] == [STORE_A], body["excluded"]
    assert len(body["shortlist"]["slots"]) == 1


def test_a_list_price_fallback_is_ranked_as_a_candidate_and_excluded_on_its_own_merits():
    """A silent store's fallback is a real candidate that the filters then refuse.

    ``collect_bids`` calls a fallback "a real, rankable, list-price offer, not a hole", and it
    reaches the ranker as one — carrying a minted ``bid_id`` so it is referable at all. It then
    fails the C10 check, because a catalog-derived offer names no checkout URL and a buyer
    cannot be sent to a destination that does not exist.
    """
    app = _wired_app(bidders=Bidders({}), stores=(STORE_A,))
    body = _post(app, [_rostered(STORE_A, 100.0)])

    assert body["entries"][0]["fallback"] is True
    excluded = body["excluded"]
    assert [row["bid_ref"] for row in excluded] == [mint_bid_id(body["auction_id"], STORE_A)]
    assert any("off_domain" in reason for reason in excluded[0]["exclusion_reasons"])


# =====================================================================================
# 3. The published shortlist path
# =====================================================================================
def test_the_shortlist_is_readable_at_the_published_path_after_the_auction_closes():
    bidders = Bidders({STORE_A: _bid(STORE_A, 100.0), STORE_B: _bid(STORE_B, 120.0)})
    app = _wired_app(bidders=bidders)
    client = TestClient(app)

    body = _post(app, [_rostered(STORE_A, 100.0), _rostered(STORE_B, 120.0)])
    auction_id = body["auction_id"]

    read_back = client.get(f"/auctions/{auction_id}/shortlist")
    assert read_back.status_code == 200, read_back.text
    assert read_back.json() == body["shortlist"]
    assert client.get("/auctions/auction-never-existed/shortlist").status_code == 404


def test_an_empty_shortlist_is_served_as_a_shortlist_and_an_unknown_auction_as_a_404():
    """The two must not answer alike: "everything was refused" is not "no such auction"."""
    app = _wired_app(bidders=Bidders({}), stores=(STORE_A,))
    client = TestClient(app)
    body = _post(app, [_rostered(STORE_A, 100.0)])

    served = client.get(f"/auctions/{body['auction_id']}/shortlist")
    assert served.status_code == 200
    assert served.json()["slots"] == []
    assert served.json()["auction_id"] == body["auction_id"]


def test_the_shortlist_store_forgets_on_the_ttl_and_is_bounded():
    """``POST /auctions`` is unauthenticated, so the store it writes to needs both bounds."""
    store = ShortlistStore(capacity=3, ttl_seconds=900.0)
    for index in range(5):
        store.put(f"auction-{index}", {"auction_id": f"auction-{index}", "slots": []}, now=0.0)

    assert len(store) == 3
    assert store.get("auction-0", now=0.0) is None, "the oldest was not evicted"
    assert store.get("auction-4", now=0.0) is not None
    assert store.get("auction-4", now=901.0) is None, "the TTL did not expire the entry"
    assert len(store) == 2, "an expired entry was not dropped on read"


# =====================================================================================
# 4. A bidder cannot score itself
# =====================================================================================
#: Keys a store would write into its reply if writing them worked. Each closes exactly one
#: way of gaming the formula; the randomised property below closes the rest of the class.
GAMING_KEYS: tuple[tuple[str, Any], ...] = (
    ("intent_match", 1.0),
    ("verified_claim_ratio", 1.0),
    ("price_value", 1.0),
    ("delivery_fit", 1.0),
    ("trust", 1.0),
    ("policy_penalties", -5.0),
    ("policy_events", []),
    ("store_domain", "attacker.tld"),
    ("bid_id", "aaaaaaaa"),
    ("rank_score", 99.0),
    ("eligible", True),
)


def _entry(store_id: str, extra: dict[str, Any] | None = None) -> BidEntry:
    bid = _bid(store_id, 100.0)
    bid.update(extra or {})
    return BidEntry(
        store_id=store_id,
        tier=1,
        fallback=False,
        bid=bid,
        received_at=time.time(),
        claims=list(bid["claims"]),
    )


def _score_of(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The one ranked row for ``store-a``, ranked exactly as the served route ranks it."""
    result = rank_auction(
        [_entry(STORE_A, extra)],
        auction_id="auction-1",
        intent=_intent(),
        now=time.time(),
        trust_snapshot={STORE_A: {"blacklisted": False, "score": 0.6}},
        registered_domains=StaticRegisteredDomains({STORE_A: _domain(STORE_A)}),
        weights=DEFAULT_RANKING_WEIGHTS,
    )
    assert result["ranked"], f"the baseline candidate was excluded: {result['candidates']}"
    return result["ranked"][0]


def test_the_projection_carries_exactly_the_named_fields():
    """The arming assertion for everything below: the candidate is a NAMED record.

    If this ever grows a passthrough, the two tests below still pass whenever the generated
    keys happen to be inert — so the set is pinned here rather than inferred from behaviour.
    """
    candidate = candidate_from_entry(
        _entry(STORE_A, {key: value for key, value in GAMING_KEYS}),
        auction_id="auction-1",
        registered_domains=StaticRegisteredDomains({STORE_A: _domain(STORE_A)}),
    )
    assert tuple(sorted(candidate)) == tuple(sorted(CANDIDATE_FIELDS))
    assert candidate["store_domain"] == _domain(STORE_A), "the bid supplied its own domain"
    assert candidate["bid_id"] == mint_bid_id("auction-1", STORE_A), "the bid named its own id"


@pytest.mark.parametrize(("key", "value"), GAMING_KEYS, ids=[key for key, _ in GAMING_KEYS])
def test_no_field_a_store_writes_into_its_bid_moves_its_own_score(key, value):
    baseline = _score_of()
    gamed = _score_of({key: value})

    assert gamed["rank_score"] == pytest.approx(baseline["rank_score"]), (
        f"a store moved its own rank_score by writing {key}={value!r} into its bid"
    )
    assert gamed["components"] == pytest.approx(baseline["components"])
    assert gamed["bid_id"] == baseline["bid_id"]


#: Key names the randomised property draws from. Free text ALONE is a weak generator here and
#: it was measured weak: with the projection sabotaged into a passthrough
#: (``{**dict(bid), ...}``) the parametrised probe above failed six ways and this property
#: still passed 150 examples, because a passed-through ``"aBc"`` changes no score — only a
#: name the FORMULA reads does, and hypothesis will not invent ``verified_claim_ratio`` from
#: ``st.text()``. So the published names are mixed in explicitly, and the structural assertion
#: inside the test is what closes the rest of the class whatever the key is called.
_GAMEABLE_NAMES = tuple(RANK_FEATURES) + (
    "policy_penalties",
    "policy_events",
    "store_domain",
    "bid_id",
    "rank_score",
    "eligible",
    "exclusion_reasons",
    "verified_hard_fit_count",
    "trust_summary",
    "provenance_labels",
)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    payload=st.dictionaries(
        # Anything but the two fields a store is legitimately answering WITH: the priced offer
        # and the claims it is standing behind. Everything else is the exchange's to decide.
        keys=(st.sampled_from(_GAMEABLE_NAMES) | st.text(min_size=1, max_size=24)).filter(
            lambda k: k not in {"offer", "claims"}
        ),
        values=st.recursive(
            st.none()
            | st.booleans()
            | st.floats(allow_nan=False, allow_infinity=False)
            | st.text(max_size=16),
            lambda children: (
                st.lists(children, max_size=3)
                | st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=3)
            ),
            max_leaves=6,
        ),
        min_size=1,
        max_size=6,
    )
)
def test_no_key_whatever_a_store_invents_can_move_its_own_score(payload):
    """The class, not the list: an arbitrary reply body scores identically to a bare one.

    Asserted STRUCTURALLY first and behaviourally second, and the order matters. "The score
    did not move" is only evidence about the keys the formula happens to read today; "the
    projection carried exactly its five named fields" is evidence about every key there could
    ever be, including whichever one a future feature starts reading. A passthrough
    regression fails the first assertion for any generated key at all.
    """
    entry = _entry(STORE_A, dict(payload))
    candidate = candidate_from_entry(
        entry,
        auction_id="auction-1",
        registered_domains=StaticRegisteredDomains({STORE_A: _domain(STORE_A)}),
    )
    assert tuple(sorted(candidate)) == tuple(sorted(CANDIDATE_FIELDS)), (
        f"the projection carried a field the store wrote: {sorted(set(candidate) - set(CANDIDATE_FIELDS))}"
    )

    baseline = _score_of()
    gamed = _score_of(dict(payload))

    assert gamed["rank_score"] == pytest.approx(baseline["rank_score"]), (
        f"a store moved its own rank_score by writing {sorted(payload)} into its bid"
    )
    assert gamed["components"] == pytest.approx(baseline["components"])
    assert gamed["eligible"] is baseline["eligible"]


def test_the_gaming_probes_are_armed():
    """A sweep that iterates nothing passes. State the counts so that cannot happen quietly."""
    assert len(GAMING_KEYS) >= 10, GAMING_KEYS
    baseline = _score_of()
    assert baseline["rank_score"] > 0.0, (
        "the baseline candidate scores zero, so 'the score did not move' would be trivially "
        "true for every key and the probes above would be measuring nothing"
    )
    assert baseline["components"], "the baseline produced no components to compare"
