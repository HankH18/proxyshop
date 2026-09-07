"""R12's exploration slice, on a SERVED auction — the read half of the bandit's loop.

**The defect.** ``exchange/policy/bandit.py`` is a complete contextual Thompson sampler with a
fail-closed R12 eligibility rule and a water-filled exploration floor, and until this file
:func:`~exchange.policy.bandit.exposure` had **zero production callers**. The UPDATE half was
wired — ``POST /internal/outcomes`` folds conversions into the posterior — and ``policy/routes.py``
said so in its own source: "nothing on the served path reads the state this door writes". So the
loop was open at the far end: outcomes went in, and no shopper was ever shown anything different
because of them.

**Why that is not a cosmetic gap.** Phase 2 made the quality terms SATURATING and trust is
exogenous and slow to move. Combined, whoever transacts first holds a permanent lock: a new store
with a genuinely better pitch can never accumulate the evidence that would prove it, because it is
never shown, and it is never shown because it has no evidence. R12 answers that with a guaranteed
exploration slice, and a guarantee nothing delivers is not one.

**The bound, stated because exploration has a real cost to the shopper in front of it.** A slot
filled by exploration is a slot NOT filled by the candidate the ranking put there, so the shopper
pays for the market's information. This is what the cost is capped at, and every clause is
asserted below:

* **one slot, ever** — :data:`~exchange.policy.exploration.EXPLORATION_SLOTS` is 1 of the four;
* **never the leader** — the displaced candidate is the LAST of the slots that would have been
  filled, so the ranking's top three are untouchable;
* **never when nobody loses** — an auction whose eligible stores all fit in the shortlist explores
  nothing, because there is no slot to take;
* **never when the slice is already filled** — a low-data store that earned a slot on rank alone
  means exploration has nothing left to buy;
* **never past eligibility** — R19's hard constraints, R12's blacklist, expiry and the domain check
  all run first. Exploration reorders the eligible; it admits nobody.
* **never silent** — the promoted slot, its exposure share and the candidate it displaced are
  published on ``POST /auctions`` under ``exploration``.

**Determinism.** ``exposure(state, cluster, seed)`` is seeded from a BLAKE2b digest, never from
``random`` and never from the clock, and the seed here is the auction's own id — so one auction
ranks the same way every time it is ranked, and two auctions do not have to agree.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.policy.exploration import EXPLORATION_SLOTS, MAX_SAMPLED_CHALLENGERS
from exchange.ranking.serving import configure_ranking, rank_auction
from exchange.ranking.shortlist import MAX_SLOTS
from exchange.ranking.verification import StaticCatalogSnapshots
from fastapi.testclient import TestClient

#: Four incumbents with a record, ranked by their trust rows, plus the newcomers each test adds.
INCUMBENTS = ("store-a", "store-b", "store-c", "store-d")
TRUST = {"store-a": 0.90, "store-b": 0.80, "store-c": 0.70, "store-d": 0.60}


def _domain(store_id: str) -> str:
    return f"{store_id}.example.com"


def _offer(store_id: str, price: float = 100.0) -> dict[str, Any]:
    return {
        "product_ref": "product-1",
        "unit_price": price,
        "total_price": price,
        "currency": "USD",
        "checkout_url": f"https://{_domain(store_id)}/cart/1:1",
        "expires_at": time.time() + 3600.0,
    }


def _bid(store_id: str, price: float = 100.0, claims: Any = ()) -> dict[str, Any]:
    return {
        "auction_id": None,
        "store_id": store_id,
        "offer": _offer(store_id, price),
        "claims": list(claims),
        "agent_version": "1.0.0",
        "schema_version": "1.0.0",
    }


def _snapshot(store_id: str) -> dict[str, Any]:
    return {
        "snapshot_id": f"snap-{store_id}",
        "store_id": store_id,
        "products": [
            {
                "product_ref": "product-1",
                "canonical_name": "product-1",
                "evidence_ref": f"snap-{store_id}#product-1",
                "attributes": {"capacity_l": {"value": 35}},
            }
        ],
    }


def _app(stores: dict[str, float], *, low_data: tuple[str, ...] = ()) -> Any:
    """A fully wired exchange. ``stores`` is ``{store_id: trust score}``."""

    def solicit(store: Any) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        if store_id not in stores:
            return None
        return {"store_id": store_id, "received_at": time.time(), "bid": _bid(store_id)}

    app = create_app()
    configure_auctions(
        app,
        solicitor=solicit,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={
            store: {
                "blacklisted": False,
                "score": score,
                "confidence": 0.5,
                "low_data": store in low_data,
            }
            for store, score in stores.items()
        },
        registered_domains=StaticRegisteredDomains({s: _domain(s) for s in stores}),
        catalog=StaticCatalogSnapshots({s: _snapshot(s) for s in stores}),
    )
    return app


INTENT = {
    "intent_id": "intent-1",
    "cluster_id": "cluster-1",
    "query": "a 35 litre cabin backpack",
    "hard_constraints": [],
}


def _post(app: Any, stores: dict[str, float], *, intent: Any = None) -> dict[str, Any]:
    roster = [
        {"store_id": store, "tier": 1, "product_ref": "product-1", "list_price": 200.0}
        for store in stores
    ]
    response = TestClient(app).post(
        "/auctions",
        json={"intent": dict(intent or INTENT), "roster": roster, "bid_timeout_seconds": 2.0},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _slot_stores(body: dict[str, Any]) -> list[str]:
    """Which STORE holds each slot, in slot order. ``bid_ref`` is ``{auction}:{store}``."""
    return [slot["bid_ref"].split(":", 1)[1] for slot in body["shortlist"]["slots"]]


def _outcome(store_id: str, cluster_id: str, *, delta: float) -> dict[str, Any]:
    """A ``TrustEventPayload`` the published outcomes door accepts."""
    return {
        "store_id": store_id,
        "event": {
            "event_id": f"ev-{store_id}-{delta}",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "feedback",
            "payload": {},
        },
        "dim": "price_honored",
        "delta": delta,
        "pseudonymous_context": {"cluster_id": cluster_id, "pseudonym": "psn-0001"},
    }


# =====================================================================================
# 1. The read half exists at all
# =====================================================================================
def test_exposure_is_called_on_a_served_auction() -> None:
    """The gap this file closes, stated as the smallest possible fact.

    RED before: ``bandit.exposure`` had no production call site anywhere in the repository, so
    this counter stayed at zero through a complete ``POST /auctions``.

    Counted at the USE site — ``exploration`` binds the name at import — with the identity
    asserted first, so this is the sampler itself and not a same-named wrapper.
    """
    from exchange.policy import bandit, exploration

    assert exploration.exposure is bandit.exposure

    stores = {**TRUST, "store-new": 0.10}
    app = _app(stores, low_data=("store-new",))

    calls: list[tuple[str, Any]] = []
    real = bandit.exposure

    def counting(state: Any, cluster_id: str, seed: Any) -> Any:
        calls.append((cluster_id, seed))
        return real(state, cluster_id, seed)

    exploration.exposure = counting  # type: ignore[assignment]
    try:
        body = _post(app, stores)
    finally:
        exploration.exposure = real  # type: ignore[assignment]

    assert calls, "a served auction still reads no posterior"
    assert calls[0][0] == "cluster-1", calls
    assert calls[0][1] == body["auction_id"], calls


def test_an_auction_with_nothing_to_explore_never_reaches_the_sampler() -> None:
    """The cost bound on the READ itself, which is not the same as the bound on the slot.

    ``exposure`` draws :data:`~exchange.policy.bandit.DEFAULT_DRAWS` (512) joint samples over
    every store in the auction, and it runs inside R10's synchronous window. Measured on this
    tree: 2.5 ms at 5 stores, 24 ms at 50, **250 ms at 500** — and ``MAX_ROSTER_ENTRIES`` is
    500. So the sampler is reached only after every cheap structural clause has already said
    there is something to explore, and this pins it: the ordinary auction — no low-data store
    anywhere — pays nothing at all to be told it explores nothing.
    """
    from exchange.policy import exploration

    stores = {**TRUST, "store-new": 0.10}
    app = _app(stores)  # nobody marked low_data

    calls: list[Any] = []
    real = exploration.exposure
    exploration.exposure = lambda *a, **k: (calls.append(a), real(*a, **k))[1]  # type: ignore[assignment]
    try:
        body = _post(app, stores)
    finally:
        exploration.exposure = real  # type: ignore[assignment]

    assert calls == [], "the sampler ran on an auction with no low-data candidate"
    assert body["exploration"] is None


# =====================================================================================
# 2. The slice actually reaches the buyer
# =====================================================================================
def test_a_low_data_store_reaches_a_slot_it_would_never_have_reached_on_rank_alone() -> None:
    """The headline. Five eligible stores, four slots, and the newcomer ranks fifth.

    The control is the same auction with the newcomer's ``low_data`` flag cleared: nothing about
    the bids, the prices, the catalogue or the trust SCORES changes, and the newcomer does not
    appear. So the slot it holds above is the exploration slice and not an accident of ranking.
    """
    stores = {**TRUST, "store-new": 0.10}

    without = _post(_app(stores), stores)
    assert _slot_stores(without) == list(INCUMBENTS), without["shortlist"]
    assert without["exploration"] is None, without["exploration"]
    assert [row["store_id"] for row in without["ranked"]][-1] == "store-new", without["ranked"]

    with_slice = _post(_app(stores, low_data=("store-new",)), stores)
    slots = _slot_stores(with_slice)
    assert "store-new" in slots, with_slice["shortlist"]
    assert len(slots) == MAX_SLOTS, slots

    # The newcomer is still ranked last. Exploration moved what the buyer is SHOWN; it did not
    # touch the published score or its order, which is what `policy/bandit.py` means by "it
    # never touches rank".
    assert [row["store_id"] for row in with_slice["ranked"]][-1] == "store-new"
    assert {row["store_id"]: row["rank_score"] for row in with_slice["ranked"]} == {
        row["store_id"]: row["rank_score"] for row in without["ranked"]
    }


def test_the_promoted_slot_is_published_rather_than_silent() -> None:
    """A slot the shopper did not get for the published reason must say so."""
    stores = {**TRUST, "store-new": 0.10}
    body = _post(_app(stores, low_data=("store-new",)), stores)

    reported = body["exploration"]
    assert reported is not None, body
    assert reported["store_id"] == "store-new"
    assert reported["bid_ref"].endswith(":store-new")
    assert reported["displaced_store_id"] == "store-d", reported
    assert 0.0 < reported["exposure_share"] <= 1.0, reported
    # The named slot is one the shortlist really contains, and it belongs to the promoted bid.
    by_ref = {slot["bid_ref"]: slot["slot"] for slot in body["shortlist"]["slots"]}
    assert by_ref.get(reported["bid_ref"]) == reported["slot"], (reported, by_ref)


def test_the_cost_is_one_slot_and_it_is_never_the_leader() -> None:
    """Three newcomers below the cut, and exactly one of them is shown.

    The bound is structural rather than statistical: whatever the posterior says, exploration
    can take :data:`EXPLORATION_SLOTS` of the four and the top three are not reachable from it.
    """
    newcomers = ("store-x", "store-y", "store-z")
    stores = {**TRUST, **{store: 0.10 for store in newcomers}}
    body = _post(_app(stores, low_data=newcomers), stores)

    slots = _slot_stores(body)
    explored = [store for store in slots if store in newcomers]
    assert len(explored) == EXPLORATION_SLOTS == 1, slots
    assert slots[: MAX_SLOTS - 1] == list(INCUMBENTS[: MAX_SLOTS - 1]), slots
    assert body["exploration"]["store_id"] in newcomers


def test_nothing_is_explored_when_every_eligible_store_is_shown_anyway() -> None:
    """Four stores, four slots. Exploration costs nobody a slot, so it takes none."""
    stores = {store: TRUST[store] for store in INCUMBENTS}
    body = _post(_app(stores, low_data=("store-d",)), stores)
    assert _slot_stores(body) == list(INCUMBENTS), body["shortlist"]
    assert body["exploration"] is None, body["exploration"]


def test_a_low_data_store_that_earned_its_slot_buys_no_second_one() -> None:
    """The slice is a floor, not a bonus: an already-shown newcomer displaces nobody."""
    stores = {**TRUST, "store-new": 0.10}
    # `store-a` is the ranking's leader AND low-data, so the slice is already filled.
    body = _post(_app(stores, low_data=("store-a",)), stores)
    assert _slot_stores(body) == list(INCUMBENTS), body["shortlist"]
    assert body["exploration"] is None, body["exploration"]


def test_exploration_admits_nobody_the_eligibility_filters_refused() -> None:
    """R19 first. A low-data store that fails a hard constraint stays out of the shortlist.

    Exploration reorders the ELIGIBLE. If it could promote a refused candidate it would be a
    bypass of the one rule the spec calls a filter and never a score term.
    """
    stores = {**TRUST, "store-new": 0.10}

    def solicit(store: Any) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        # Every incumbent claims the must-have; the newcomer claims nothing, so R19 refuses it.
        claims = (
            ()
            if store_id == "store-new"
            else (
                {
                    "key": "capacity_l",
                    "value": 35,
                    "provenance": {
                        "source": "owner_statement",
                        "ref": "ref:capacity_l",
                        "authority_rank": 1,
                    },
                },
            )
        )
        return {
            "store_id": store_id,
            "received_at": time.time(),
            "bid": _bid(store_id, claims=claims),
        }

    app = create_app()
    configure_auctions(
        app,
        solicitor=solicit,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={
            store: {
                "blacklisted": False,
                "score": score,
                "confidence": 0.5,
                "low_data": store == "store-new",
            }
            for store, score in stores.items()
        },
        registered_domains=StaticRegisteredDomains({s: _domain(s) for s in stores}),
        catalog=StaticCatalogSnapshots({s: _snapshot(s) for s in stores}),
    )

    body = _post(
        app,
        stores,
        intent={**INTENT, "hard_constraints": [{"field": "capacity_l", "op": "gte", "value": 30}]},
    )
    assert "store-new" in {row["store_id"] for row in body["excluded"]}, body["excluded"]
    assert "store-new" not in _slot_stores(body), body["shortlist"]
    assert body["exploration"] is None, body["exploration"]


def test_the_exposure_draw_is_bounded_by_the_published_challenger_cap() -> None:
    """The CPU bound on the read, pinned where a large roster would otherwise pay for it.

    The sampler is O(stores x 512 draws) inside R10's synchronous window and
    ``MAX_ROSTER_ENTRIES`` is 500, so an auction whose whole roster is low-data would spend a
    quarter of a second deciding one slot. Only the stores that could decide it are sampled —
    the four-slot pool plus at most :data:`MAX_SAMPLED_CHALLENGERS` challengers — and this
    asserts the count the sampler was actually handed rather than the constant.

    Measured end to end over 500 candidates after this bound: 18 ms with nothing to explore,
    20 ms with one challenger, 52 ms with all 500 low-data. Before it: 330 ms.
    """
    from exchange.policy import exploration

    newcomers = tuple(f"store-n{i:03d}" for i in range(MAX_SAMPLED_CHALLENGERS + 40))
    stores = {**TRUST, **{s: 0.10 for s in newcomers}}
    app = _app(stores, low_data=newcomers)

    widths: list[int] = []
    real = exploration.exposure

    def counting(state: Any, cluster_id: str, seed: Any) -> Any:
        widths.append(len(state.stores))
        return real(state, cluster_id, seed)

    exploration.exposure = counting  # type: ignore[assignment]
    try:
        body = _post(app, stores)
    finally:
        exploration.exposure = real  # type: ignore[assignment]

    assert widths, "the sampler was never reached"
    assert max(widths) <= MAX_SLOTS + MAX_SAMPLED_CHALLENGERS, widths
    assert max(widths) < len(stores), (
        f"the draw covered {max(widths)} of {len(stores)} stores; the cap did not bite"
    )
    # Still exactly one slot, and still from among the challengers that were sampled.
    assert body["exploration"]["store_id"] in newcomers[:MAX_SAMPLED_CHALLENGERS]
    assert sum(1 for s in _slot_stores(body) if s in newcomers) == EXPLORATION_SLOTS


# =====================================================================================
# 3. The posteriors the outcomes door writes are what choose the store
# =====================================================================================
def test_the_recorded_outcomes_decide_which_newcomer_is_explored() -> None:
    """R16's loop, closed at both ends over two published HTTP doors.

    Two newcomers, identical in every published feature and identical in their trust rows, both
    ranked below the cut. The only thing that separates them is what ``POST /internal/outcomes``
    folded into the bandit — and the store the exchange chooses to show follows it. Reverse the
    outcomes and the choice reverses, which is what makes this a read of the posterior rather
    than a stable tie-break wearing one's name.
    """
    newcomers = ("store-x", "store-y")
    stores = {**TRUST, **{store: 0.10 for store in newcomers}}

    def explored_after(winner: str) -> str:
        app = _app(stores, low_data=newcomers)
        client = TestClient(app)
        loser = next(store for store in newcomers if store != winner)
        for _ in range(20):
            assert (
                client.post(
                    "/internal/outcomes", json=_outcome(winner, "cluster-1", delta=1.0)
                ).status_code
                == 204
            )
            assert (
                client.post(
                    "/internal/outcomes", json=_outcome(loser, "cluster-1", delta=-1.0)
                ).status_code
                == 204
            )
        return _post(app, stores)["exploration"]["store_id"]

    assert explored_after("store-x") == "store-x"
    assert explored_after("store-y") == "store-y"


def test_an_outcome_in_another_cluster_does_not_decide_this_ones_slice() -> None:
    """``update`` folds an outcome into its own cluster and nowhere else; so does the read."""
    newcomers = ("store-x", "store-y")
    stores = {**TRUST, **{store: 0.10 for store in newcomers}}
    app = _app(stores, low_data=newcomers)
    client = TestClient(app)
    for _ in range(20):
        client.post("/internal/outcomes", json=_outcome("store-x", "cluster-9", delta=1.0))
        client.post("/internal/outcomes", json=_outcome("store-y", "cluster-9", delta=-1.0))

    in_cluster_9 = _post(app, stores, intent={**INTENT, "cluster_id": "cluster-9"})
    assert in_cluster_9["exploration"]["store_id"] == "store-x", in_cluster_9["exploration"]

    # `cluster-1` saw none of those outcomes, so its posterior is still the trust-seeded prior
    # and the two newcomers are indistinguishable there. The assertion is deliberately weak —
    # only that the answer is one of the two — because a strong one would be asserting the
    # sampler's arithmetic rather than the routing.
    in_cluster_1 = _post(app, stores)
    assert in_cluster_1["exploration"]["store_id"] in newcomers


# =====================================================================================
# 4. Determinism
# =====================================================================================
def test_the_same_auction_ranks_byte_identically_twice() -> None:
    """No clock, no ``random``, no process salt: one auction has one answer.

    Driven through ``rank_auction`` rather than through two POSTs because two POSTs are two
    auctions with two ids, and the id IS the seed — that is the point, not a limitation. The
    served pair below asserts the part a caller can observe.
    """
    from exchange.policy.routes import InMemoryBanditPosteriors

    stores = {**TRUST, "store-new": 0.10}
    snapshot = {
        store: {
            "blacklisted": False,
            "score": score,
            "confidence": 0.5,
            "low_data": store == "store-new",
        }
        for store, score in stores.items()
    }

    class _Entry:
        fallback = False

        def __init__(self, store_id: str) -> None:
            self.store_id = store_id
            self.bid = _bid(store_id)
            self.claims = []
            self.list_price = 200.0

    entries = [_Entry(store) for store in stores]
    frozen = time.time()

    def once() -> dict[str, Any]:
        return rank_auction(
            entries,
            auction_id="auction-fixed",
            intent=dict(INTENT),
            now=frozen,
            trust_snapshot=snapshot,
            registered_domains=StaticRegisteredDomains({s: _domain(s) for s in stores}),
            bandit_posteriors=InMemoryBanditPosteriors(),
        )

    first, second = once(), once()
    assert first["shortlist"] == second["shortlist"]
    assert first["exploration"] == second["exploration"]
    assert first["exploration"] is not None


def test_two_served_auctions_over_the_same_inputs_show_the_same_shops() -> None:
    """The observable half, over the real socket: same bodies in, same shops per slot out."""
    stores = {**TRUST, "store-new": 0.10}
    app = _app(stores, low_data=("store-new",))
    first, second = _post(app, stores), _post(app, stores)
    assert _slot_stores(first) == _slot_stores(second)
    assert first["exploration"]["store_id"] == second["exploration"]["store_id"]


def test_an_exchange_with_no_bandit_book_still_delivers_the_guarantee() -> None:
    """R12's slice is a guarantee, not an opt-in.

    Before any outcome has ever been recorded there is no learned posterior — the trust-seeded
    prior IS the posterior — and a newcomer that can never be shown can never produce one. So
    the slice runs on a deployment that has taken no outcomes at all, which is exactly the
    deployment where the lock-in would otherwise be permanent.
    """
    stores = {**TRUST, "store-new": 0.10}
    app = _app(stores, low_data=("store-new",))
    assert getattr(app.state, "bandit_posteriors", None) is None
    body = _post(app, stores)
    assert body["exploration"] is not None, body
    assert "store-new" in _slot_stores(body)


@pytest.mark.parametrize("row", [None, {}, {"blacklisted": False, "score": 0.1}])
def test_a_store_the_snapshot_will_not_vouch_for_claims_no_slice(row: Any) -> None:
    """Fail-closed, R12's own direction: an unanswered ``low_data`` is not a yes.

    A missing row, an empty one and one that simply does not mention ``low_data`` all mean the
    exchange cannot say this store is new — and a slice handed out on "we could not tell" is
    exploration budget taken from a store the platform actually knows is new.
    """
    stores = {**TRUST, "store-new": 0.10}
    app = create_app()

    def solicit(store: Any) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        return {"store_id": store_id, "received_at": time.time(), "bid": _bid(store_id)}

    configure_auctions(
        app,
        solicitor=solicit,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    snapshot: dict[str, Any] = {
        store: {"blacklisted": False, "score": score, "confidence": 0.5, "low_data": False}
        for store, score in TRUST.items()
    }
    if row is not None:
        snapshot["store-new"] = {**row}
    configure_ranking(
        app,
        trust_snapshot=snapshot,
        registered_domains=StaticRegisteredDomains({s: _domain(s) for s in stores}),
        catalog=StaticCatalogSnapshots({s: _snapshot(s) for s in stores}),
    )
    body = _post(app, stores)
    assert body["exploration"] is None, body["exploration"]
    assert "store-new" not in _slot_stores(body), body["shortlist"]
