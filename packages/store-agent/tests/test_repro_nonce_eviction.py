"""T-378 — the external door's replay memory must evict, and must still refuse replays.

`NonceStore.purge_expired` existed and had **zero production callers**, so the number of
`(signer_id, nonce)` pairs a process held grew with process lifetime: measured, 200 admitted
bids retained 200 entries and 74,504 bytes, `retain_until` populated and never consulted. The
nonce LENGTH was bounded (`MAX_IDENTIFIER_LENGTH`); the COUNT was not.

**The hard part is not the sweep, it is proving the sweep is safe.** A replay memory that
forgets a nonce while a replay of that nonce could still be admitted has not been bounded, it
has been switched off. So every test here asserts BOTH halves against one shared store:

* memory stays bounded while admissions keep arriving, and
* a replay presented while it could still win is *still refused*, before the queue is touched.

The safety argument the fix rests on, which these tests are written to falsify if it is wrong:

    The door forgets `(signer, nonce)` only at instants when an EARLIER gate already refuses
    every replay of that submission.

Two cases, and they are exhaustive because those are the only two retentions the door hands
the store:

* **An auction deadline was supplied.** `purge_expired` drops the pair once
  `retain_until < now`; gate 4b refuses `after_auction_deadline` once `now > deadline`. Same
  instant. The replay cannot be aimed at a *different*, still-open auction either, because
  `apps/exchange/src/external_bids/routes.py::_reconciled` refuses any submission whose signed
  `auction_id` is not the one in the URL — so the deadline a replay is judged against is the
  deadline its nonce was retained under.
* **No auction deadline** (the default deployment: `_auction_terms` answers `(None, None)` for
  an auction it cannot find). The retention is then the freshness horizon `issued_at + window`.
  `issued_at` is inside `canonical_signing_bytes`, so a replay cannot move it without
  invalidating the signature, and gate 4a refuses `stale_submission` once
  `now - issued_at > window`. Same instant again.

`test_a_replay_is_refused_at_every_instant_across_the_eviction_boundary` is the test that
grades that claim directly: it walks one nonce from "remembered, refused as a replay" to
"forgotten, refused as stale" and asserts the door never once admits it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

SIGNER = "store-external-1"
KEY_ID = "key-2026-01"
KEY = "gate-secret-0001"

#: Every instant below is expressed as an offset from this one, so a test can say "now is
#: `_at(240)`" and mean something a reader can check against the 300-second window.
EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

HONEST_PRICE = 89.0
FAR_FUTURE = "2999-01-01T00:00:00Z"


def _at(seconds: float) -> str:
    """`EPOCH + seconds`, RFC-3339 with the `Z` the door's parser reads."""
    return (EPOCH + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _keyring() -> dict[str, dict[str, str]]:
    return {SIGNER: {KEY_ID: KEY}}


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "auction_id": "auc-gate-378",
        "store_id": SIGNER,
        "offer": {
            "product_ref": "gate-prod-1",
            "unit_price": HONEST_PRICE,
            "total_price": HONEST_PRICE,
            "discount": None,
            "commitments": [],
            "expires_at": FAR_FUTURE,
        },
        "claims": [],
        "message": "Warmest merino mid-layer on the market.",
        "agent_version": "ext-0.1.0",
        "schema_version": "1",
        "signer_id": SIGNER,
        "key_id": KEY_ID,
        "issued_at": _at(0),
        "nonce": "nonce-gate-378-0001",
    }
    payload.update(overrides)
    return payload


def _trust_snapshot_for(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The caller's eligibility read. Absent means EMPTY, so every probe must supply it."""
    store_id = payload.get("store_id")
    if not isinstance(store_id, str) or not store_id:
        return {}
    return {store_id: {"store_id": store_id, "score": 0.9, "blacklisted": False}}


def _list_prices_for(payload: Mapping[str, Any]) -> dict[str, Any]:
    """An HONEST roster — it prices the offer at exactly what the offer states.

    The price wall is silent because the bid is truthful, not because the wall was loosened.
    Without it every probe here is refused `price_unreconcilable:...:list_price_unavailable`
    long before the replay gate, and would grade nothing at all.
    """
    offer = payload.get("offer")
    ref = offer.get("product_ref") if isinstance(offer, Mapping) else None
    unit = offer.get("unit_price") if isinstance(offer, Mapping) else None
    if not isinstance(ref, str) or not isinstance(unit, (int, float)):
        return {}
    return {ref: {"list_price": float(unit), "max_discount_pct": 100.0}}


class _Queue:
    """Records what the door hands to the next stage. A refusal must never reach it."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def __call__(self, item: Any) -> None:
        self.items.append(item)

    @property
    def count(self) -> int:
        return len(self.items)


def _present(payload: Mapping[str, Any], *, store: Any, now: str, deadline: Any) -> tuple:
    """Offer one signed bid at the real door. Returns `(receipt, queue)`."""
    from store_agent.external import receive_bid, sign_bid

    queue = _Queue()
    receipt = receive_bid(
        payload,
        sign_bid(payload, KEY),
        _keyring(),
        queue=queue,
        nonce_store=store,
        now=now,
        auction_deadline=deadline,
        trust_snapshot=_trust_snapshot_for(payload),
        list_prices=_list_prices_for(payload),
    )
    return receipt, queue


# ---------------------------------------------------------------------------------------
# 1. The reproduction: admissions accumulate and nothing ever sweeps them.
# ---------------------------------------------------------------------------------------


def test_admissions_do_not_accumulate_in_the_replay_memory_without_a_deadline() -> None:
    """The measured repro. 200 admitted bids left 200 entries; nothing purged them.

    `auction_deadline=None` is the DEFAULT deployment — `_auction_terms` answers `(None, None)`
    for any auction the exchange cannot look up — and it is the worst case for retention,
    because a `None` retention used to mean "keep forever".
    """
    from store_agent.external import NonceStore

    store = NonceStore()
    admitted = 0
    #: One bid every 10 seconds. The default freshness window is 300s, so at any instant only
    #: ~30 of these are still replayable and the rest are refusable as stale by gate 4a.
    for index in range(200):
        moment = index * 10
        payload = _payload(issued_at=_at(moment), nonce=f"nonce-378-flood-{index:04d}")
        receipt, queue = _present(payload, store=store, now=_at(moment + 1), deadline=None)
        assert receipt.accepted is True, (
            f"arming: bid {index} must be ADMITTED or this test grades nothing — {receipt!r}"
        )
        assert queue.count == 1
        admitted += 1

    assert admitted == 200, "arming: all 200 bids must have been admitted"
    # 200 bids spread over 2000 seconds. At most `window / 10 + a couple` can still be
    # replayable at the last instant; anything beyond that is memory the store cannot justify.
    assert len(store) <= 64, (
        f"the replay memory retained {len(store)} of 200 admitted nonces. Only the ~30 issued "
        f"inside the last freshness window can still be replayed; the rest are already refused "
        f"`stale_submission` by gate 4a and are memory held for nothing. Unbounded growth with "
        f"process lifetime on an admitted-seller channel (T-378)."
    )


def test_admissions_do_not_accumulate_once_their_auction_has_closed() -> None:
    """The same property when the exchange DOES supply a deadline: closed auctions are dropped."""
    from store_agent.external import NonceStore

    store = NonceStore()
    for index in range(200):
        moment = index * 10
        payload = _payload(issued_at=_at(moment), nonce=f"nonce-378-closed-{index:04d}")
        receipt, queue = _present(
            payload,
            store=store,
            now=_at(moment + 1),
            # Each auction closes 20 seconds after the bid is issued, so by the time the next
            # two bids arrive this one's auction is shut and gate 4b refuses every replay.
            deadline=_at(moment + 20),
        )
        assert receipt.accepted is True, (
            f"arming: bid {index} must be ADMITTED or this test grades nothing — {receipt!r}"
        )
        assert queue.count == 1

    assert len(store) <= 8, (
        f"the replay memory retained {len(store)} of 200 nonces whose auctions have all closed "
        f"but the last two. `purge_expired` exists and nothing calls it (T-378)."
    )


# ---------------------------------------------------------------------------------------
# 2. The other half: eviction must not re-open the hole the store exists to close.
# ---------------------------------------------------------------------------------------


def test_a_replay_inside_its_window_is_still_refused_after_eviction_has_run() -> None:
    """A long-lived nonce survives a flood that evicts everything around it.

    This is the half a naive cap (or an LRU) gets wrong: bounding the store by throwing away
    the OLDEST entry evicts precisely the nonce whose auction is still open.
    """
    from store_agent.external import NonceStore

    store = NonceStore()

    # The nonce that must be remembered: its auction runs for an hour.
    protected = _payload(issued_at=_at(0), nonce="nonce-378-protected")
    receipt, queue = _present(protected, store=store, now=_at(1), deadline=_at(3600))
    assert receipt.accepted is True, f"arming: the protected bid must be admitted — {receipt!r}"
    assert queue.count == 1

    # ...and 240 short-auction bids around it, each auction closing two seconds after issue.
    for index in range(240):
        moment = 1 + index
        flood = _payload(issued_at=_at(moment), nonce=f"nonce-378-noise-{index:04d}")
        noise, noise_queue = _present(flood, store=store, now=_at(moment), deadline=_at(moment + 2))
        assert noise.accepted is True, f"arming: noise bid {index} must be admitted — {noise!r}"
        assert noise_queue.count == 1

    assert len(store) <= 16, (
        f"memory half: {len(store)} entries survived a flood of 240 closed auctions (T-378)"
    )

    # BOTH halves. The protected nonce is still remembered...
    assert store.seen(SIGNER, "nonce-378-protected") is True, (
        "the replay half: the protected nonce was EVICTED while its auction was still open. "
        "Bounding the store by dropping entries that are still inside their replay window does "
        "not bound the replay memory, it removes it."
    )
    # ...and the door still refuses a replay of it, before the queue is touched.
    replay, replay_queue = _present(protected, store=store, now=_at(240), deadline=_at(3600))
    assert replay.accepted is not True, (
        f"the replay half: a replay of the protected bid was ADMITTED at t+240s, inside its "
        f"300s freshness window and 3600s auction — {replay!r}"
    )
    from store_agent.external.door import REASON_REPLAYED_NONCE

    assert REASON_REPLAYED_NONCE in replay.reasons, (
        f"a replay must be refused AS a replay, not as a side effect of some other gate: "
        f"{replay.reasons!r}"
    )
    assert replay_queue.count == 0, "a refused replay must never reach the queue"


def test_a_replay_is_refused_at_every_instant_across_the_eviction_boundary() -> None:
    """The safety claim, graded directly: the door never admits a replay, evicted or not.

    One nonce, no auction deadline, walked second by second from admission out past the
    freshness horizon at which its entry is dropped. Before the boundary the refusal is
    `replayed_nonce`; after it, `stale_submission`. There must be no instant in between at
    which the submission is admitted — that instant would be the replay hole an eviction
    policy can open.
    """
    from store_agent.external import NonceStore
    from store_agent.external.door import REASON_REPLAYED_NONCE, REASON_STALE_SUBMISSION

    store = NonceStore()
    original = _payload(issued_at=_at(0), nonce="nonce-378-boundary")

    first, first_queue = _present(original, store=store, now=_at(1), deadline=None)
    assert first.accepted is True, f"arming: the first presentation must be admitted — {first!r}"
    assert first_queue.count == 1

    admitted_at: list[float] = []
    reasons_seen: set[str] = set()
    # Well past the 300s default window, in 5s steps, so the eviction boundary (whatever
    # instant the implementation picks) is crossed inside the sweep rather than assumed.
    for moment in range(2, 601, 5):
        # Each step also admits a fresh nonce, so the store is being actively swept rather
        # than sitting idle — eviction that only runs on a quiet store is not eviction.
        churn = _payload(issued_at=_at(moment), nonce=f"nonce-378-churn-{moment:04d}")
        _present(churn, store=store, now=_at(moment), deadline=None)

        replay, replay_queue = _present(original, store=store, now=_at(moment), deadline=None)
        if replay.accepted is True or replay_queue.count:
            admitted_at.append(moment)
        reasons_seen.update(replay.reasons)

    assert not admitted_at, (
        f"a replay of an already-spent nonce was ADMITTED at t+{admitted_at} — the eviction "
        f"policy forgot the nonce at an instant when no earlier gate refuses it, which is the "
        f"replay hole the store exists to close"
    )
    # Arming: the sweep must actually have crossed the boundary, or "never admitted" is a
    # statement about a store that never evicted anything.
    assert {REASON_REPLAYED_NONCE, REASON_STALE_SUBMISSION} <= reasons_seen, (
        f"arming: the walk must cross the eviction boundary — a refusal as a REPLAY before it "
        f"and as STALE after it. Saw only {sorted(reasons_seen)!r}"
    )
    assert len(store) <= 96, (
        f"memory half: {len(store)} entries after 120 churn admissions across 600 seconds"
    )


# ---------------------------------------------------------------------------------------
# 3. The ceiling. Time-based eviction bounds rate x window, not count.
# ---------------------------------------------------------------------------------------


def test_the_store_has_a_named_finite_ceiling_on_how_many_pairs_it_holds() -> None:
    """Expiry alone bounds `rate x window`, which is not a bound. There must be a hard cap."""
    from store_agent.external.nonces import MAX_TRACKED_NONCES, NonceStore

    assert isinstance(MAX_TRACKED_NONCES, int) and not isinstance(MAX_TRACKED_NONCES, bool)
    assert MAX_TRACKED_NONCES > 0
    assert NonceStore().max_entries == MAX_TRACKED_NONCES, (
        "a default store must be bounded by the published constant, not by a private literal"
    )


def test_a_ceiling_that_is_not_a_ceiling_is_refused_at_construction() -> None:
    """`max_entries=0` or `-1` must not quietly mean "unbounded" — that is the defect itself."""
    from store_agent.external.nonces import NonceStore

    for bad in (0, -1, -32_768):
        with pytest.raises(ValueError):
            NonceStore(max_entries=bad)
    for wrong_type in (None, 1.5, "32768", True):
        with pytest.raises(TypeError):
            NonceStore(max_entries=wrong_type)  # type: ignore[arg-type]


def test_a_full_store_refuses_rather_than_evicting_a_nonce_that_can_still_be_replayed() -> None:
    """Fail CLOSED. Making room by dropping a live nonce trades a bound for a replay."""
    from store_agent.external.nonces import NonceStore, NonceStoreFull

    store = NonceStore(max_entries=8)
    for index in range(8):
        assert store.consume(SIGNER, f"live-{index}", FAR_FUTURE) is True
    assert len(store) == 8

    with pytest.raises(NonceStoreFull):
        store.consume(SIGNER, "one-too-many", FAR_FUTURE)

    assert len(store) == 8, "a refused insert must not have grown the store"
    for index in range(8):
        assert store.seen(SIGNER, f"live-{index}") is True, (
            f"live-{index} was EVICTED to make room. Every one of these is retained until "
            f"{FAR_FUTURE}; dropping one admits a replay of it."
        )
    assert store.seen(SIGNER, "one-too-many") is False, (
        "the refused nonce must not be recorded as spent — the submitter was never admitted"
    )


def test_a_full_replay_memory_makes_the_door_refuse_cleanly_rather_than_raise() -> None:
    """The door is an unauthenticated surface: a full store is a 4xx refusal, never a 500."""
    from store_agent.external.door import REASON_REPLAY_MEMORY_EXHAUSTED
    from store_agent.external.nonces import NonceStore

    store = NonceStore(max_entries=4)
    for index in range(4):
        assert store.consume(f"other-signer-{index}", "n", FAR_FUTURE) is True

    fresh = _payload(issued_at=_at(0), nonce="nonce-378-no-room")
    receipt, queue = _present(fresh, store=store, now=_at(1), deadline=_at(3600))

    assert receipt.accepted is not True, (
        f"a submission the store has no room to remember must NOT be admitted: admitting it "
        f"would enqueue a bid whose nonce can be replayed without limit — {receipt!r}"
    )
    assert queue.count == 0, "a refusal must never reach the queue"
    assert REASON_REPLAY_MEMORY_EXHAUSTED in receipt.reasons, (
        f"the refusal must say the replay memory is full, not `door_failed_closed` (which is "
        f"what an uncaught exception produces) and not `replayed_nonce` (which blames the "
        f"submitter for our own capacity): {receipt.reasons!r}"
    )
    assert len(store) == 4, "the door must not have evicted a live entry to make room"


# ---------------------------------------------------------------------------------------
# 4. The greppable half of the ticket: `purge_expired` must have a production caller.
# ---------------------------------------------------------------------------------------


def test_the_door_is_a_production_caller_of_purge_expired() -> None:
    """`purge_expired` had zero production callers. The door must be one, at its own clock.

    Asserted behaviourally rather than by grepping the source: the store records the instants
    it was asked to purge at, and the door must ask at the instant it is judging the rest of
    the submission by — not at the process's wall clock, which would purge entries a caller's
    injected `now` still considers live.
    """
    from contracts.boundary import parse_timestamp
    from store_agent.external import NonceStore, receive_bid, sign_bid

    class _Recording(NonceStore):
        def __init__(self) -> None:
            super().__init__()
            self.purges: list[Any] = []

        def purge_expired(self, as_of: Any) -> int:
            self.purges.append(as_of)
            return super().purge_expired(as_of)

    store = _Recording()
    payload = _payload(issued_at=_at(0), nonce="nonce-378-callsite")
    queue = _Queue()
    receipt = receive_bid(
        payload,
        sign_bid(payload, KEY),
        _keyring(),
        queue=queue,
        nonce_store=store,
        now=_at(7),
        auction_deadline=_at(3600),
        trust_snapshot=_trust_snapshot_for(payload),
        list_prices=_list_prices_for(payload),
    )
    assert receipt.accepted is True, f"arming: the bid must be admitted — {receipt!r}"
    assert store.purges, (
        "`NonceStore.purge_expired` still has no production caller: the door admitted a bid "
        "and never once asked the replay memory to forget anything (T-378)"
    )
    assert any(parse_timestamp(moment) == parse_timestamp(_at(7)) for moment in store.purges), (
        f"the door must purge at the instant it judges the submission by (`now`), not at the "
        f"process wall clock — a store swept against real time would drop every entry a test "
        f"or a replaying deployment injected a past `now` for. Purged at {store.purges!r}"
    )
