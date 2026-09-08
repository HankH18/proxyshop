"""The auction store is BOUNDED, and an operator can choose WHICH store it is.

Run it on its own::

    PROXYSHOP_WORKER=9 .venv/bin/python -m pytest \\
        apps/exchange/tests/test_auction_store_bounds.py -q

Two defects, fixed together and pinned together, because either one alone leaves the other
one live.

**The store had no bound.** ``InMemoryAuctionStore`` was two plain ``dict``s that forgot
nothing — no capacity, no TTL, no eviction — and ``POST /auctions`` is unauthenticated: one
record plus up to :data:`RESERVATION_NAMES_PER_AUCTION` reservations per call, retained for
the life of a process ``apps/exchange/compose.yaml`` limits to 256 MiB. Every eviction
assertion below goes through the real route (``create_app()`` + ``TestClient``) rather than
through the class, because the route is the door the caller actually has.

**No deployment could move off it.** ``exchange.auction.state`` called ``RedisAuctionStore``
the DESIGN-pinned store and said "a deployment that cares runs" it; there was no key in
``Deployment``, no branch in ``configure_exchange`` and no environment name through which
anybody could — so that sentence described a choice nobody could make, and *every* served
exchange ran the unbounded process-local store. The seam tests drive that choice end to end —
document, environment variable, and no document at all — against a fake ``worker_redis``, so
they need no live Redis and touch no network.

Nothing here sleeps: the TTL is driven with an injected ``clock``.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

import pytest
from exchange.accept.claims import ACCEPTANCE_RESERVATION
from exchange.accept.routes import CHECKOUT_MODE_ENV
from exchange.auction.ledger import InMemoryLedgerSink
from exchange.auction.routes import MAX_IDENTIFIER_LENGTH, configure_auctions
from exchange.auction.state import (
    ACCEPTANCE_RESERVATION_NAME,
    ACCEPTED,
    AUCTION_STORE_MEMORY,
    AUCTION_STORE_REDIS,
    AUCTION_STORE_WORDS,
    AUCTION_TTL_SECONDS,
    CLOSED,
    CREATED,
    DEFAULT_AUCTION_CAPACITY,
    ENV_AUCTION_STORE,
    OPEN,
    RESERVATION_NAMES_PER_AUCTION,
    AuctionRecord,
    AuctionStateMachine,
    AuctionStoreUnavailable,
    InMemoryAuctionStore,
    RedisAuctionStore,
    UnknownAuction,
    absence_of,
    auction_store_from_env,
    build_auction_store,
    creation_reservation,
    exit_reservation,
)
from exchange.composition import (
    AUCTION_STORE_KEY,
    ENV_DEPLOYMENT,
    ENV_DEPLOYMENT_JSON,
    ENV_MERCHANT_URL,
    DeploymentConfigurationError,
    bind_auction_machine,
    ensure_configured,
    parse_deployment,
)
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from fastapi.testclient import TestClient

#: A trust service guaranteed not to answer, so the ledger sink every bind installs costs one
#: refused loopback connection instead of a DNS lookup. The sink swallows it by design.
UNREACHABLE_TRUST_URL = "http://127.0.0.1:1"

#: The token the memory store's EVICTION message carries and its expiry message does not.
#: Capitalised in the source on purpose — an operator greps for it — so it is the only thing
#: here that tells the two causes apart without matching on prose.
EVICTION_WORD = "EVICTED"

#: The two spellings of "your TTL ran out" that must NOT appear in an eviction's 404.
TTL_CLAIMS = ("TTL expired", "TTL has expired")


@pytest.fixture(autouse=True)
def _no_ambient_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here may pass or fail because of what the developer's shell exports.

    The last two names are not about the store: ``ensure_configured`` binds the code creator
    BEFORE the auction machine, and an ambient ``CHECKOUT_MODE`` that mints on a merchant this
    process has no address for is a 503 on every served post below, for an unrelated reason.
    """
    for name in (
        ENV_DEPLOYMENT,
        ENV_DEPLOYMENT_JSON,
        ENV_AUCTION_STORE,
        CHECKOUT_MODE_ENV,
        ENV_MERCHANT_URL,
    ):
        monkeypatch.delenv(name, raising=False)


# =====================================================================================
# Builders
# =====================================================================================
def _auction_body() -> dict[str, Any]:
    """A well-formed ``POST /auctions`` body, with CONSTANT identifiers.

    Constant on purpose: the retained-bytes test compares one burst against another, and an
    ``intent-1`` / ``intent-10`` difference would be digits of noise inside the measurement.
    """
    return {
        "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"},
        "roster": [
            {"store_id": "store-a", "tier": 1, "product_ref": "product-1", "list_price": 120.0}
        ],
        "bid_timeout_seconds": 0.01,
    }


def _served_exchange(capacity: int) -> tuple[Any, TestClient, InMemoryAuctionStore]:
    """The real app with a small-capacity store, bound through the published route seam.

    Bound BEFORE the first request, so ``bind_auction_machine``'s "unless it has one" check
    leaves it alone and this really is the store the served route writes into.
    """
    app = create_app()
    store = InMemoryAuctionStore(capacity=capacity)
    configure_auctions(app, machine=AuctionStateMachine(store=store, ledger=InMemoryLedgerSink()))
    return app, TestClient(app), store


def _post_auction(client: TestClient) -> str:
    posted = client.post("/auctions", json=_auction_body())
    assert posted.status_code == 201, (
        f"POST /auctions answered {posted.status_code}; a refused post retains nothing, so it "
        f"measures nothing"
    )
    return str(posted.json()["auction_id"])


def _retained_bytes(store: InMemoryAuctionStore) -> int:
    """The JSON blobs this store holds, summed — the thing the caller was sizing."""
    blobs = list(store._records.values())
    assert all(isinstance(blob, str) for blob in blobs), (
        "`_records` is no longer `{auction_id: json_blob}`, so summing `len()` over it is no "
        "longer summing retained characters and this measurement is vacuous"
    )
    return sum(len(blob) for blob in blobs)


class FakeClock:
    """A clock a test moves by hand. No test in this file sleeps."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeRedis:
    """A dict standing in for ``WorkerRedis``: ``get``/``set``/``delete``, ``nx=``, ``ex=``.

    ``reserve`` is ``SET key token NX EX ttl`` and reads the return value as "did I win", so
    ``nx`` must refuse an existing key rather than overwrite it — that refusal is the whole of
    the at-most-once constraint (T-158).
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: list[Any] = []

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: Any, *, nx: bool = False, ex: Any = None) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = str(value)
        self.expiries.append(ex)
        return True

    def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0


def _fake_worker_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Make ``build_auction_store('redis')`` succeed with no server. Returns the fake client."""
    client = FakeRedis()
    monkeypatch.setattr("proxyshop_support.redis_client.worker_redis", lambda *a, **k: client)
    return client


def _worker_redis_that_cannot_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("no redis on this host")

    monkeypatch.setattr("proxyshop_support.redis_client.worker_redis", explode)


def _redis_document() -> dict[str, Any]:
    return {AUCTION_STORE_KEY: AUCTION_STORE_REDIS, "trust_url": UNREACHABLE_TRUST_URL}


class LosingStore(InMemoryAuctionStore):
    """A bounded store whose capacity is taken by OTHER traffic the instant one record lands.

    This is what a cap being reached mid-request looks like from inside a single served
    ``POST /auctions``: the auction is written, ``capacity + 1`` unauthenticated posts arrive
    on other worker threads, and the record this request is still working on is gone before
    the request touches it again. Simulated with a subclass rather than with real threads
    because the property under test is the ROUTE's answer, not the store's locking — pinned
    on its own in ``test_concurrent_writers_...`` above.

    ``at_state`` picks which of the two windows ``auction/routes.py`` guards is driven: the
    narrow ``create`` -> ``open`` one, and the wide ``open`` -> ``close`` one that spans the
    whole bid window.
    """

    def __init__(self, *, at_state: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._at_state = at_state
        self._burst_sent = False

    def save(self, record: AuctionRecord) -> None:
        super().save(record)
        if record.state == self._at_state and not self._burst_sent:
            self._burst_sent = True
            for index in range(self.capacity + 1):
                super().save(
                    AuctionRecord(auction_id=f"other-{index}", intent_id="i", cluster_id="c")
                )


def _exchange_that_loses_its_auction(at_state: str) -> tuple[TestClient, LosingStore]:
    """The real app on a :class:`LosingStore`, with every collaborator in this process.

    ``eligibility`` is stated rather than left to the composition root's default, and that is
    not decoration: the default reads the trust service's snapshot over HTTP, and this file
    connects to nothing. A stated ``eligible`` also keeps the fan-out on the path, so the
    ``open`` -> ``close`` window really is the whole bid window and not a shortcut through it.
    """
    app = create_app()
    store = LosingStore(at_state=at_state, capacity=2)
    configure_auctions(
        app,
        machine=AuctionStateMachine(store=store, ledger=InMemoryLedgerSink()),
        eligibility=StaticSellerEligibility({"store-a": ELIGIBLE}),
    )
    return TestClient(app), store


# =====================================================================================
# The bound, driven through the served route
# =====================================================================================
def test_a_burst_of_unauthenticated_posts_never_makes_the_served_store_exceed_its_cap() -> None:
    """The defect itself: an anonymous caller deciding how many records this process keeps."""
    capacity = 8
    _app, client, store = _served_exchange(capacity)

    held = []
    for _ in range(capacity * 4):
        _post_auction(client)
        held.append(len(store))

    assert max(held) <= capacity, (
        f"the served store held {max(held)} records against a capacity of {capacity}; "
        f"`POST /auctions` is unauthenticated, so anything past the cap is memory a caller takes"
    )
    assert len(store) == capacity, (
        f"{capacity * 4} posts left {len(store)} records; the cap is a plateau, not a ceiling "
        f"nothing reaches"
    )
    assert store.evicted == capacity * 3, (
        f"{capacity * 4} posts into a store of {capacity} evicted {store.evicted}; the counter "
        f"is how an operator sees the cap firing at all"
    )
    assert store.expired == 0, (
        "records were dropped as EXPIRED during a burst that took no time; the cap and the TTL "
        "are two different bounds and must not be confused in the counters"
    )


def test_the_404_for_an_auction_the_cap_took_away_says_evicted_and_does_not_blame_the_ttl() -> None:
    """The served body carries the STORE's own account, so nobody debugs the wrong bound.

    An operator handed "your 15-minute TTL ran out" about a 30-second-old auction goes looking
    at the clock instead of at the capacity. That is the reason this message exists.
    """
    capacity = 4
    _app, client, store = _served_exchange(capacity)

    first = _post_auction(client)
    for _ in range(capacity):
        _post_auction(client)

    read = client.get(f"/auctions/{first}")

    assert read.status_code == 404, f"an evicted auction answered {read.status_code}"
    assert EVICTION_WORD in read.text, (
        f"the 404 body does not say {EVICTION_WORD!r}: {read.text[:300]}. This body is the only "
        f"account of the eviction anybody outside the process gets"
    )
    for claim in TTL_CLAIMS:
        assert claim not in read.text, (
            f"the 404 for an EVICTED auction claims {claim!r}; the TTL had not run out, and a "
            f"404 that blames it sends the operator to the wrong bound"
        )
    assert store.evicted >= 1, "nothing was evicted, so the 404 above is not the one being graded"


def test_an_auction_forgotten_further_back_than_the_store_remembers_still_blames_no_ttl() -> None:
    """The store's memory of what it dropped is itself capped, and must degrade honestly.

    ``_gone`` holds the last ``capacity`` ids, so an auction buried by 24 later evictions can
    no longer be named as one (measured at capacity 8, 4N posts). What it must NOT do then is
    fall back to blaming a TTL that has not run out.

    The ring's own bound is asserted here, and it has to be: without it this node passes on
    the WRONG BRANCH. An unbounded ``_gone`` would still hold ``first``, ``absence`` would
    take the definitive "was EVICTED" path, and every assertion below would be green while
    grading a sentence this test is not about — and the ring would be the unbounded table the
    whole file exists to remove, one dict over from the one it removed.
    """
    capacity = 8
    _app, client, store = _served_exchange(capacity)

    first = _post_auction(client)
    for _ in range(capacity * 4):
        _post_auction(client)

    assert len(store._gone) <= store.capacity, (
        f"the tombstone ring holds {len(store._gone)} ids against a capacity of "
        f"{store.capacity}; `POST /auctions` is unauthenticated, so a ring that grows with the "
        f"eviction count is the same memory an anonymous caller can drive that the record "
        f"table used to be"
    )
    assert first not in store._gone, (
        f"the store still remembers {first!r} after {store.evicted} evictions, so the branch "
        f"this test names — an auction forgotten FURTHER BACK than the ring reaches — is not "
        f"the branch it is grading"
    )

    read = client.get(f"/auctions/{first}")

    assert read.status_code == 404
    for claim in TTL_CLAIMS:
        assert claim not in read.text, (
            f"an auction the cap took away {store.evicted} evictions ago is reported as "
            f"{claim!r}: {read.text[:300]}"
        )
    assert f"{store.evicted} evicted" in read.text, (
        f"the 404 for a long-forgotten auction reports no eviction count, so nothing in it "
        f"points at the cap that took the record: {read.text[:300]}"
    )


def test_an_auction_created_and_read_back_before_the_cap_is_reached_is_still_served() -> None:
    """The positive control: the bound evicts the oldest, not everything."""
    _app, client, _store = _served_exchange(8)

    auction_id = _post_auction(client)
    read = client.get(f"/auctions/{auction_id}")

    assert read.status_code == 200, (
        f"an auction created one request ago answered {read.status_code}; a bound that also "
        f"loses live auctions is not a bound, it is a broken store"
    )
    assert read.json()["auction_id"] == auction_id


def test_the_bytes_the_served_store_retains_plateau_rather_than_grow_with_the_post_count() -> None:
    """Memory measured, not asserted structurally: retained bytes at 2N posts against 4N.

    Unbounded, this number is linear in the request count with no plateau — the shape
    ``test_hostile_identifiers_retain_no_more_of_the_process_than_honest_ones`` measured at
    3.690 MiB over 64 requests.
    """
    capacity = 8
    _app, client, store = _served_exchange(capacity)

    for _ in range(capacity * 2):
        _post_auction(client)
    at_2n, records_2n = _retained_bytes(store), len(store)

    for _ in range(capacity * 2):
        _post_auction(client)
    at_4n, records_4n = _retained_bytes(store), len(store)

    assert at_2n > 0, "the store retained nothing after 2N posts, so this comparison is vacuous"
    assert records_2n == capacity and records_4n == capacity, (
        f"records held went {records_2n} -> {records_4n} across 2N and 4N posts, capacity "
        f"{capacity}"
    )
    assert at_4n <= at_2n * 1.05, (
        f"{capacity * 2} posts retained {at_2n} characters and {capacity * 4} retained {at_4n}: "
        f"doubling the requests moved the retained bytes, so the store still grows with the "
        f"caller's request count (unbounded, this would read about {at_2n * 2})"
    )


def test_an_evicted_auction_reaches_the_state_machine_as_the_stores_own_explanation() -> None:
    """``AuctionStateMachine.get`` must not restate a cause it cannot know."""
    store = InMemoryAuctionStore(capacity=1)
    machine = AuctionStateMachine(store=store, ledger=InMemoryLedgerSink())
    machine.create("auction-1", intent_id="intent-1", cluster_id="cluster-1")
    machine.create("auction-2", intent_id="intent-1", cluster_id="cluster-1")

    with pytest.raises(UnknownAuction) as raised:
        machine.get("auction-1")

    message = str(raised.value)
    assert EVICTION_WORD in message, (
        f"the state machine reported an evicted auction as {message}; it is answering with its "
        f"own generic sentence instead of the store's account of where the record went"
    )
    for claim in TTL_CLAIMS:
        assert claim not in message


def test_evicting_a_record_drops_the_reservations_that_belonged_to_it() -> None:
    """A reservation outliving its record refuses a legitimate move on a later auction.

    ``create`` reserves the id before it writes anything (T-158), so a ``create`` reservation
    left behind by an eviction makes that id permanently un-creatable in this process — an
    availability regression whose message blames a collision that never happened.
    """
    store = InMemoryAuctionStore(capacity=2)
    machine = AuctionStateMachine(store=store, ledger=InMemoryLedgerSink())
    machine.create("auction-1", intent_id="intent-1", cluster_id="cluster-1")
    machine.open("auction-1", now=1.0)
    assert store.reserve("auction-1", creation_reservation(), "probe") is not None, (
        "the `create` reservation is not held, so this test is not proving it gets dropped"
    )

    machine.create("auction-2", intent_id="intent-2", cluster_id="cluster-1")
    machine.create("auction-3", intent_id="intent-3", cluster_id="cluster-1")

    assert store.load("auction-1") is None, "capacity 2 did not evict the oldest of three records"
    left_behind = [pair for pair in store._reservations if pair[0] == "auction-1"]
    assert left_behind == [], (
        f"the evicted record left {left_behind} behind; those reservations constrain an auction "
        f"this store no longer holds"
    )

    reused = machine.create("auction-1", intent_id="intent-reused", cluster_id="cluster-1")
    assert reused.intent_id == "intent-reused", (
        "an id the cap evicted could not be created again, so eviction turned a bounded store "
        "into a permanent refusal for every id it ever dropped"
    )


def test_a_post_whose_auction_the_cap_takes_before_it_opens_answers_503_and_not_500() -> None:
    """The narrow window: ``machine.create`` writes, and ``machine.open`` re-reads.

    Before the store had a cap, an id ``create`` had just written could not stop existing, so
    ``open`` could not fail this way and nothing on the route guarded it. With a cap it can,
    and unguarded it surfaced as an unhandled ``KeyError`` — an unauthenticated 500 on a door
    that had just answered 201.
    """
    client, store = _exchange_that_loses_its_auction(CREATED)

    posted = client.post("/auctions", json=_auction_body())

    assert posted.status_code == 503, (
        f"POST /auctions answered {posted.status_code} for an auction the store dropped "
        f"between `create` and `open`. 500 is the pre-repair answer and it is wrong twice: it "
        f"says the caller hit a bug when they hit a capacity, and it says nothing an operator "
        f"can act on"
    )
    assert "in-memory auction store" in posted.text, (
        f"the 503 body does not name the store that could not keep the auction: "
        f"{posted.text[:300]}. The store's own message is what names the two fixes — a larger "
        f"cap, or {ENV_AUCTION_STORE}={AUCTION_STORE_REDIS}"
    )
    assert store.evicted >= 1, "nothing was evicted, so this is not the refusal being graded"


def test_a_post_whose_auction_the_cap_takes_during_the_bid_window_answers_503_and_not_500() -> None:
    """The wide window: ``open`` to ``close`` spans the whole solicitation, seconds of I/O.

    This is the one a real burst reaches. The auction is opened, the fan-out runs, and the
    record is gone by the time the close applies — ``DEFAULT_AUCTION_CAPACITY + 1`` cheap
    unauthenticated posts inside the window is all it takes.
    """
    client, store = _exchange_that_loses_its_auction(OPEN)

    posted = client.post("/auctions", json=_auction_body())

    assert posted.status_code == 503, (
        f"POST /auctions answered {posted.status_code} for an auction the store dropped during "
        f"the bid window; the caller did nothing wrong and the deployment did, which is what "
        f"503 says and 500 does not"
    )
    assert "in-memory auction store" in posted.text, (
        f"the 503 body does not name the store: {posted.text[:300]}"
    )
    assert store.evicted >= 1, "nothing was evicted, so this is not the refusal being graded"


def test_reading_an_auction_id_longer_than_the_door_keeps_is_refused_without_echoing_it() -> None:
    """A 404 that quotes the id back is a refusal whose size the caller chooses.

    Measured before the guard: a 4,000-character path parameter came back verbatim inside a
    4,611-character detail. Refused rather than truncated, so the bound reads the same way as
    the one on every caller-chosen identifier in the request BODY.
    """
    _app, client, _store = _served_exchange(8)
    oversized = "a" * 4_000

    refused = client.get(f"/auctions/{oversized}")

    assert refused.status_code == 422, (
        f"a {len(oversized)}-character auction id answered {refused.status_code}; the cap on "
        f"this path parameter is what stops the refusal growing with what it refused"
    )
    assert len(refused.content) < 4_096, (
        f"the 422 body is {len(refused.content)} bytes for a {len(oversized)}-character id; a "
        f"refusal that scales with the request is the amplifier the guard exists to remove"
    )
    assert "a" * 200 not in refused.text, (
        f"the refusal echoes the id it refused: {refused.text[:300]}"
    )

    at_the_ceiling = client.get(f"/auctions/{'b' * MAX_IDENTIFIER_LENGTH}")

    assert at_the_ceiling.status_code == 404, (
        f"an id of exactly {MAX_IDENTIFIER_LENGTH} characters answered "
        f"{at_the_ceiling.status_code}; {MAX_IDENTIFIER_LENGTH} is the ceiling this door keeps "
        f"and the auction ids it mints itself are read back through this same path. A 422 here "
        f"would mean the bound is some smaller number nobody wrote down"
    )


# =====================================================================================
# The TTL, on an injected clock
# =====================================================================================
def test_a_record_at_exactly_the_ttl_is_gone_and_says_expired_rather_than_evicted() -> None:
    """``>=``, not ``>``: at exactly ``ttl_seconds`` the record is already gone."""
    clock = FakeClock()
    store = InMemoryAuctionStore(capacity=64, ttl_seconds=30.0, clock=clock)
    store.save(AuctionRecord(auction_id="auction-1", intent_id="intent-1", cluster_id="cluster-1"))

    clock.now += 29.5
    assert store.load("auction-1") is not None, (
        "a record half a second inside its 30s TTL was dropped; the TTL is a deadline, not an "
        "approximation of one"
    )

    clock.now = 1_700_000_000.0 + 30.0
    assert store.load("auction-1") is None, (
        "a record at exactly its 30s TTL was still served; `ranking.serving.ShortlistStore.get` "
        "picks the same boundary, and a shortlist must not outlive its auction by an instant"
    )
    assert store.expired == 1, f"the expiry counter reads {store.expired} after one expiry"
    assert store.evicted == 0, "an expiry was counted as an eviction; they are different bounds"

    note = store.absence("auction-1")
    assert "TTL expired" in note, f"the expiry was not reported as one: {note}"
    assert EVICTION_WORD not in note, (
        f"a record the TTL took away is reported as {EVICTION_WORD}: {note}. The store was "
        f"nowhere near its capacity, so that sends an operator to the wrong bound"
    )


def test_a_record_the_cap_reached_that_had_also_aged_out_is_reported_expired_not_evicted() -> None:
    """The defect this node was written for, in as many words: ``absence`` used to lie.

    ``load`` is the only other reaper and nothing re-reads a finished auction, so a store that
    has served more than ``capacity`` auctions fills with records whose TTL ran out minutes
    ago. Every one of them was then dropped by the cap and reported as an EVICTION — and the
    404 said, verbatim, "Its 900s TTL had not run out" about a record 10,000 seconds old. An
    operator handed that sentence raises ``DEFAULT_AUCTION_CAPACITY`` or moves to Redis, and
    neither does anything, because the record was never displaced by anybody: it aged out.

    Driven on an injected clock, so the TTL is crossed without a single second passing.
    """
    capacity, ttl = 4, 100.0
    clock = FakeClock()
    store = InMemoryAuctionStore(capacity=capacity, ttl_seconds=ttl, clock=clock)

    for index in range(capacity):
        store.save(AuctionRecord(auction_id=f"old-{index}", intent_id="i", cluster_id="c"))
    assert len(store) == capacity and store.expired == 0

    clock.now += ttl * 100  # 10,000 seconds: every held record is long past its deadline
    for index in range(capacity):
        store.save(AuctionRecord(auction_id=f"new-{index}", intent_id="i", cluster_id="c"))

    assert store.evicted == 0, (
        f"{store.evicted} records were counted as EVICTED, but every one of them was "
        f"{ttl * 100:g}s old against a {ttl:g}s TTL. Nothing was displaced to make room — the "
        f"cap merely happened to be the code path that noticed"
    )
    assert store.expired == capacity, (
        f"the expiry counter reads {store.expired} where {capacity} records aged out; the two "
        f"counters are what an operator uses to tell the cap and the clock apart"
    )
    assert len(store) == capacity, f"the cap did not hold: {len(store)} records"

    note = store.absence("old-0")
    assert "TTL expired" in note, (
        f"a record {ttl * 100:g}s past its {ttl:g}s TTL is not reported as expired: {note}"
    )
    assert EVICTION_WORD not in note, (
        f"the 404 for a record that AGED OUT claims {EVICTION_WORD}: {note}. That sends the "
        f"operator to raise the capacity, which changes nothing about a record the clock took"
    )


def test_a_save_refreshes_both_the_ttl_and_the_eviction_position_of_the_record_it_writes() -> None:
    """An auction is written on every transition, so the record evicted is the stalest one."""
    clock = FakeClock(0.0)
    store = InMemoryAuctionStore(capacity=2, ttl_seconds=1_000.0, clock=clock)

    def write(auction_id: str) -> None:
        store.save(AuctionRecord(auction_id=auction_id, intent_id="i", cluster_id="c"))

    write("auction-a")
    clock.now = 100.0
    write("auction-b")
    clock.now = 600.0
    write("auction-a")  # a transition on what was the OLDEST record
    clock.now = 700.0
    write("auction-c")  # over the cap: something has to go

    assert store.load("auction-b") is None, (
        "the record evicted was not the one that had gone longest without moving; eviction is "
        "oldest-WRITE first, so a re-saved record must not be the one dropped"
    )
    assert store.load("auction-a") is not None, (
        "the record re-saved most recently was evicted anyway, so a transition does not refresh "
        "a record's eviction position"
    )
    assert store.load("auction-c") is not None
    assert store.evicted == 1, f"one write over the cap evicted {store.evicted} records"

    clock.now = 1_550.0  # 1550s after auction-a was first written, 950s after its re-save
    assert store.load("auction-a") is not None, (
        "the re-saved record aged out on its FIRST write time, so `save` does not refresh the "
        "TTL the way `RedisAuctionStore.save`'s `ex=` does on every write"
    )


def test_concurrent_writers_leave_the_store_at_or_under_capacity_and_raise_nothing() -> None:
    """``save`` is a pop, an insert and an eviction loop — three steps, not one assignment.

    Two threads interleaving those can drop a record that is not over the cap, or leave the
    record table and its write-time column disagreeing. The served app's pool is this workload.

    **This node grades ``InMemoryAuctionStore._lock``, and it does so only because of the
    switch interval it sets.** At CPython's default 5 ms interval the threads never once
    interleave inside the critical section — the GIL serialises them — and the whole test is a
    measurement of the GIL rather than of the lock. Measured: with ``_lock`` replaced by a
    no-op context manager this node was GREEN 10/10 at 8 workers × 50 operations, and still
    green 10/10 at 32 × 300. Dropping the interval to a microsecond for the duration of the
    run makes the interleaving real, and the same no-op lock then goes RED 10/10 —
    ``RuntimeError: OrderedDict mutated during iteration`` out of ``_evict``'s victim scan,
    with the eviction counter reading 215 where 1,584 was owed. The interval is restored in
    ``finally``; it is process-global, so nothing may be left holding it.

    The two table assertions are set equality rather than length equality on purpose: two
    tables of the same SIZE holding different keys is exactly what a torn ``_forget`` leaves
    behind, and ``len(a) == len(b)`` cannot see it.
    """
    capacity, workers, per_worker = 16, 8, 200
    store = InMemoryAuctionStore(capacity=capacity)
    failures: list[BaseException] = []
    ready = threading.Barrier(workers)

    def write(worker: int) -> None:
        try:
            ready.wait()
            for index in range(per_worker):
                auction_id = f"auction-{worker}-{index}"
                store.reserve(auction_id, creation_reservation(), auction_id)
                store.save(AuctionRecord(auction_id=auction_id, intent_id="i", cluster_id="c"))
                store.load(auction_id)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append(exc)

    threads = [threading.Thread(target=write, args=(worker,)) for worker in range(workers)]
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)

    assert failures == [], f"concurrent writers raised: {failures!r}"
    assert len(store) == capacity, (
        f"{workers * per_worker} concurrent saves left {len(store)} records against a capacity "
        f"of {capacity}; a bound two threads can step over is not a bound"
    )
    assert len(store._records) == store.capacity, (
        f"the record table itself holds {len(store._records)} against a capacity of "
        f"{store.capacity}; `__len__` reads the same table, so this is the same number said "
        f"without the accessor in between"
    )
    assert store.evicted == workers * per_worker - capacity, (
        f"the eviction counter reads {store.evicted}; every save past the cap evicts exactly "
        f"one record, so interleaved writers must not lose or double-count evictions"
    )
    assert set(store._records) == set(store._written_at), (
        f"the record table and its write-time column hold different ids "
        f"({sorted(set(store._records) ^ set(store._written_at))[:4]} disagree), so a "
        f"concurrent `save` left either a record whose age nothing can compute — `_aged_out` "
        f"reads 0.0 for it and reaps it on the next write — or a timestamp for a record that "
        f"is gone"
    )
    assert len(store._reservations) <= capacity * RESERVATION_NAMES_PER_AUCTION


# =====================================================================================
# The acceptance claim — the one record eviction may not take
# =====================================================================================
def test_the_two_spellings_of_the_acceptance_reservation_name_are_the_same_name() -> None:
    """One name, spelled in two modules because the import would be a cycle.

    ``exchange.accept.claims`` imports ``exchange.auction.state``, so the store cannot import
    the claim's name back. It restates it as ``ACCEPTANCE_RESERVATION_NAME``, and this node is
    what holds the two spellings together — the same arrangement, and the same kind of node,
    that ``proxyshop_support.postgres``'s ``ROLE_PASSWORD_ENV`` already uses.
    """
    assert ACCEPTANCE_RESERVATION_NAME == ACCEPTANCE_RESERVATION, (
        f"the store protects reservations named {ACCEPTANCE_RESERVATION_NAME!r} while "
        f"`accept/claims.py` takes them out under {ACCEPTANCE_RESERVATION!r}. If these two "
        f"diverge, eviction stops protecting the record a merchant is minting against: "
        f"`accept/routes.py` claims the auction, spends a network round trip asking the "
        f"merchant for a single-use discount code, and comes back to a record the cap dropped "
        f"while it was away — a MINTED CODE with no accepted auction behind it, no `accepted` "
        f"event for trust's reconciler, and an HTTP 500 for the buyer who paid for it"
    )


def test_eviction_steps_over_the_record_a_merchant_is_minting_against() -> None:
    """The claimed record survives a save over the cap; the next-oldest goes instead.

    The window this protects is real and it is the money path: ``accept/routes.py`` takes the
    claim, asks the merchant to mint, and only then applies the ``accepted`` transition. A cap
    that fired inside that window was measured producing ``merchant mints: 1`` and an HTTP 500.
    """
    capacity = 4
    clock = FakeClock()
    store = InMemoryAuctionStore(capacity=capacity, ttl_seconds=1_000.0, clock=clock)
    for index in range(capacity):
        store.save(AuctionRecord(auction_id=f"auction-{index}", intent_id="i", cluster_id="c"))

    assert store.reserve("auction-0", ACCEPTANCE_RESERVATION_NAME, "bid-1") is None, (
        "the acceptance claim on the OLDEST record was not won, so nothing below is grading "
        "what eviction does to a claimed record"
    )
    store.save(AuctionRecord(auction_id="auction-over-the-cap", intent_id="i", cluster_id="c"))

    assert store.load("auction-0") is not None, (
        "the record a merchant is minting against was evicted; the code is already spent and "
        "the auction it was minted for no longer exists"
    )
    assert store.load("auction-1") is None, (
        "the claimed record was skipped but the NEXT-oldest was not taken, so the cap did not "
        "hold — a store that can be pinned open by holding claims is the unbounded store again"
    )
    assert len(store) == capacity and store.evicted == 1

    # Every record claimed, the one about to be written included: the cap still wins, and the
    # oldest goes anyway. The incoming id is claimed BEFORE its save because `reserve` needs no
    # record — leaving it unclaimed makes it the only candidate and the store evicts the record
    # it just wrote, which holds the cap while grading nothing about the claim.
    last = "auction-past-every-claim"
    for auction_id in [*store._records, last]:
        store.reserve(auction_id, ACCEPTANCE_RESERVATION_NAME, "bid-1")
    store.save(AuctionRecord(auction_id=last, intent_id="i", cluster_id="c"))

    assert len(store) == capacity, (
        f"{len(store)} records held against a capacity of {capacity} once every candidate was "
        f"claimed; the cap is the property that must hold, so an all-claimed store evicts its "
        f"oldest rather than growing — otherwise `POST /auctions/{{id}}/accept` is an "
        f"unauthenticated way to switch the bound off"
    )
    assert store.load("auction-0") is None, (
        "with every record claimed the store kept the oldest anyway, so something other than "
        "the oldest write was dropped"
    )


def test_the_reservation_backstop_drops_orphans_and_never_a_live_records_exit_claim() -> None:
    """A reservation with no record behind it constrains nothing. One with a record is T-158.

    The reservation table's own cap is a backstop that must never fire on a live auction: a
    ``exit:closed`` reservation dropped while its record still sits in ``closed`` hands the
    next caller the move that makes ``accept`` happen at most once. Measured on the first
    draft, which popped the oldest reservation outright: ``reserve`` answered ``None`` to a
    second caller where it owed ``'accepted'``.
    """
    capacity = 4
    store = InMemoryAuctionStore(capacity=capacity)
    machine = AuctionStateMachine(store=store, ledger=InMemoryLedgerSink())
    machine.create("auction-live", intent_id="intent-1", cluster_id="cluster-1")
    machine.open("auction-live", now=1.0)
    machine.close("auction-live", now=2.0)

    for index in range(200):
        store.reserve(f"no-record-{index}", creation_reservation(), f"token-{index}")

    ceiling = capacity * RESERVATION_NAMES_PER_AUCTION
    assert len(store._reservations) <= ceiling, (
        f"the reservation table holds {len(store._reservations)} against a ceiling of "
        f"{ceiling}; `reserve` is reachable from an unauthenticated door, so a table with no "
        f"bound is the record table's defect one dict over"
    )
    still_held = {
        name: store.reserve("auction-live", name, "a-probe")
        for name in (creation_reservation(), exit_reservation(CREATED), exit_reservation(OPEN))
    }
    assert all(holder is not None for holder in still_held.values()), (
        f"the backstop dropped a LIVE auction's reservations while 200 orphans were arriving: "
        f"{still_held}. `create` becomes re-winnable and a state this auction has already left "
        f"becomes leavable again — the exact at-most-once property T-158 exists to hold"
    )

    assert store.reserve("auction-live", exit_reservation(CLOSED), "accepted") is None, (
        "the first caller out of `closed` did not win its exit reservation"
    )
    assert store.reserve("auction-live", exit_reservation(CLOSED), "expired") == "accepted", (
        "a second caller won the single exit from `closed` too, so two requests both believe "
        "they moved this auction — the double-accept the reservation exists to refuse"
    )


# =====================================================================================
# The regression node: an UNCONFIGURED store is still bounded
# =====================================================================================
def test_an_in_memory_store_nobody_configured_is_bounded_rather_than_plain_dicts() -> None:
    """The node that goes red if this store ever reverts to dicts that forget nothing.

    The cap has to be there with NO arguments, because "every served exchange runs this class"
    was the measured state of the world before the seam below existed.
    """
    store = InMemoryAuctionStore()

    assert store.capacity == DEFAULT_AUCTION_CAPACITY, (
        f"an unconfigured store's capacity is {store.capacity!r}, not "
        f"{DEFAULT_AUCTION_CAPACITY}; a bound that has to be passed in is a bound nobody gets"
    )
    assert isinstance(store.capacity, int) and store.capacity > 0
    assert store.ttl_seconds == AUCTION_TTL_SECONDS

    for index in range(DEFAULT_AUCTION_CAPACITY + 1):
        store.save(AuctionRecord(auction_id=f"auction-{index}", intent_id="i", cluster_id="c"))

    assert len(store) == DEFAULT_AUCTION_CAPACITY, (
        f"{DEFAULT_AUCTION_CAPACITY + 1} records left {len(store)} held; the default capacity "
        f"is declared but never enforced"
    )
    assert store.evicted == 1
    assert store.load("auction-0") is None
    assert EVICTION_WORD in store.absence("auction-0")


def test_a_state_machine_nobody_handed_a_store_still_gets_a_bounded_one() -> None:
    """``AuctionStateMachine()``'s default is the door every unwired exchange went through."""
    store = AuctionStateMachine().store

    assert isinstance(store, InMemoryAuctionStore)
    assert store.capacity == DEFAULT_AUCTION_CAPACITY, (
        f"the machine's default store has capacity {store.capacity!r}; `auction/routes.py` and "
        f"`accept/routes.py` both build one lazily, so this IS the store an unconfigured "
        f"exchange serves from"
    )


# =====================================================================================
# The seam: which store a deployment runs
# =====================================================================================
def test_build_auction_store_gives_the_bounded_in_memory_store_for_the_memory_word() -> None:
    store = build_auction_store(AUCTION_STORE_MEMORY)

    assert isinstance(store, InMemoryAuctionStore)
    assert store.capacity == DEFAULT_AUCTION_CAPACITY


def test_build_auction_store_names_the_legal_words_rather_than_defaulting_to_memory() -> None:
    """A word with no implementation must not be guessed through to a process-local store."""
    with pytest.raises(AuctionStoreUnavailable) as raised:
        build_auction_store("postgres")

    message = str(raised.value)
    for word in AUCTION_STORE_WORDS:
        assert word in message, (
            f"the refusal does not name {word!r}: {message}. The operator has to be told what "
            f"they were allowed to say"
        )


def test_an_environment_that_names_no_store_selects_none_rather_than_the_memory_store() -> None:
    """``None`` and "memory" are different answers: the first defers to a document."""
    assert auction_store_from_env({}) is None
    assert auction_store_from_env({ENV_AUCTION_STORE: "   "}) is None, (
        "an empty EXCHANGE_AUCTION_STORE was read as a deliberate choice of a store"
    )


def test_an_environment_that_names_the_memory_store_gets_the_memory_store() -> None:
    store = auction_store_from_env({ENV_AUCTION_STORE: AUCTION_STORE_MEMORY})

    assert isinstance(store, InMemoryAuctionStore), (
        f"{ENV_AUCTION_STORE}={AUCTION_STORE_MEMORY} built {type(store).__name__}"
    )


def test_a_document_naming_a_store_with_no_implementation_is_refused_at_parse_time() -> None:
    """A typo has to fail once, at parse, not once per served ``POST /auctions``."""
    with pytest.raises(DeploymentConfigurationError) as raised:
        parse_deployment({AUCTION_STORE_KEY: "nonsense"}, source="x")

    message = str(raised.value)
    assert AUCTION_STORE_KEY in message
    for word in AUCTION_STORE_WORDS:
        assert word in message, f"the parse refusal does not name {word!r}: {message}"


def test_a_deployment_document_states_where_its_auctions_live() -> None:
    assert parse_deployment({AUCTION_STORE_KEY: "redis"}, source="x").auction_store == "redis"
    assert parse_deployment({}, source="x").auction_store is None, (
        "a document that says nothing about its store must not be read as having chosen one"
    )


def test_an_operator_selects_the_durable_store_through_the_deployment_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE headline: a document reaches ``RedisAuctionStore``, with no live Redis anywhere.

    Driven through ``ensure_configured`` — the composition root's real entry point, the one
    both served write routes call — because the defect was never that the binder could not
    build a store: it was that nothing on the path a deployment takes ever asked for one.
    """
    _fake_worker_redis(monkeypatch)
    app = create_app()

    bound = ensure_configured(app, {ENV_DEPLOYMENT_JSON: json.dumps(_redis_document())})

    assert "auction_machine" in bound, (
        f"a deployment stating {AUCTION_STORE_KEY}={AUCTION_STORE_REDIS!r} bound {bound}; the "
        f"store the document named was never wired"
    )
    store = app.state.auction_machine.store
    assert isinstance(store, RedisAuctionStore), (
        f"a document stating {AUCTION_STORE_KEY}={AUCTION_STORE_REDIS!r} was served by "
        f"{type(store).__name__}: the operator asked for auctions that outlive one process and "
        f"reservations that constrain every replica, and got neither"
    )


def test_the_durable_store_a_document_selected_runs_a_whole_auction_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting the store is worth nothing if the selected store cannot hold an auction."""
    fake = _fake_worker_redis(monkeypatch)
    app = create_app()
    ensure_configured(app, {ENV_DEPLOYMENT_JSON: json.dumps(_redis_document())})
    machine = app.state.auction_machine

    machine.create("auction-1", intent_id="intent-1", cluster_id="cluster-1")
    machine.open("auction-1", now=1.0)
    machine.close("auction-1", now=2.0)
    accepted = machine.accept("auction-1", "bid-1", now=3.0)

    assert accepted.state == ACCEPTED
    assert machine.get("auction-1").accepted_bid_ref == "bid-1", (
        "the accepted bid did not survive a round trip through the store the document chose"
    )
    assert RedisAuctionStore.key("auction-1") in fake.values, (
        f"nothing was written to {RedisAuctionStore.key('auction-1')}; the client saw "
        f"{sorted(fake.values)}"
    )
    assert all(expiry == AUCTION_TTL_SECONDS for expiry in fake.expiries), (
        f"a write reached Redis without the DESIGN-pinned TTL: {fake.expiries}"
    )


def test_bind_auction_machine_reaches_the_redis_store_from_a_deployment_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The binder alone, so a failure here separates it from the composition root above."""
    _fake_worker_redis(monkeypatch)
    app = create_app()
    deployment = parse_deployment(_redis_document(), source="a-test-document")

    assert bind_auction_machine(app, deployment, {}) is True
    assert isinstance(app.state.auction_machine.store, RedisAuctionStore)


def test_a_served_exchange_holds_its_auctions_in_redis_when_its_document_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the door an operator has: environment, boot, ``POST /auctions``.

    No ``configure_*`` call from the test side — the app reads the document for itself, which
    is what a person running ``uvicorn exchange.main:app`` does.
    """
    fake = _fake_worker_redis(monkeypatch)
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(_redis_document()))
    app = create_app()
    client = TestClient(app)

    auction_id = _post_auction(client)

    store = app.state.auction_machine.store
    assert isinstance(store, RedisAuctionStore), (
        f"a served exchange whose document states {AUCTION_STORE_KEY}={AUCTION_STORE_REDIS!r} "
        f"is holding its auctions in {type(store).__name__}"
    )
    assert RedisAuctionStore.key(auction_id) in fake.values, (
        "the served auction was not written to the durable store the document selected"
    )
    read = client.get(f"/auctions/{auction_id}")
    assert read.status_code == 200 and read.json()["auction_id"] == auction_id


def test_the_environment_variable_alone_selects_the_store_with_no_deployment_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``EXCHANGE_SHOP_ROSTER`` trap, pinned: a named variable that does nothing alone.

    ``apps/exchange/compose.yaml`` forwards named variables into a container whose deployment
    document is generated by a script, so a compose operator has no document to edit. A seam
    that works only alongside a document is unreachable from the shipped stack.
    """
    _fake_worker_redis(monkeypatch)
    app = create_app()

    bound = ensure_configured(app, {ENV_AUCTION_STORE: AUCTION_STORE_REDIS})

    assert bound == (), "a document was found; this test must drive the branch with none"
    machine = getattr(app.state, "auction_machine", None)
    assert machine is not None, (
        f"{ENV_AUCTION_STORE}={AUCTION_STORE_REDIS} with no deployment document bound no "
        f"auction machine at all, so the variable does nothing on its own"
    )
    assert isinstance(machine.store, RedisAuctionStore), (
        f"{ENV_AUCTION_STORE}={AUCTION_STORE_REDIS} with no deployment document was served by "
        f"{type(machine.store).__name__}"
    )


def test_a_named_redis_store_that_cannot_be_built_refuses_and_never_falls_back_to_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degrading to the process-local store is the failure the whole seam exists to prevent."""
    _worker_redis_that_cannot_connect(monkeypatch)
    app = create_app()

    with pytest.raises(DeploymentConfigurationError) as raised:
        ensure_configured(app, {ENV_AUCTION_STORE: AUCTION_STORE_REDIS})

    assert AUCTION_STORE_REDIS in str(raised.value), (
        f"the refusal does not name the store that could not be built: {raised.value}"
    )
    machine = getattr(app.state, "auction_machine", None)
    assert machine is None, (
        f"the failed bind quietly installed {type(getattr(machine, 'store', None)).__name__} — "
        f"a process-local store on the money path for a deployment that asked for a durable one"
    )


def test_a_served_exchange_whose_named_store_cannot_be_built_answers_503_and_binds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal reaches the caller as a misconfiguration, not as a working auction."""
    _worker_redis_that_cannot_connect(monkeypatch)
    monkeypatch.setenv(ENV_AUCTION_STORE, AUCTION_STORE_REDIS)
    app = create_app()
    client = TestClient(app)

    posted = client.post("/auctions", json=_auction_body())

    assert posted.status_code == 503, (
        f"POST /auctions answered {posted.status_code} for an exchange whose auction store "
        f"could not be built; a 201 means the auction was held somewhere the operator refused"
    )
    assert getattr(app.state, "auction_machine", None) is None, (
        "the failed bind left an auction machine on the app anyway, so the next request is "
        "served by whatever store happened to be installed"
    )


# =====================================================================================
# `absence` — a store may only explain what it actually does
# =====================================================================================
def test_the_redis_stores_absence_does_not_blame_an_eviction_it_never_performs() -> None:
    """``RedisAuctionStore`` drops nothing of its own accord — which is why one selects it."""
    note = RedisAuctionStore(FakeRedis()).absence("auction-1")

    assert EVICTION_WORD not in note, (
        f"the Redis store reports {EVICTION_WORD}: {note}. It has no capacity to reach, so "
        f"claiming a record was displaced sends an operator hunting a bound that is not there"
    )
    assert "evicts nothing" in note, f"the Redis absence does not say what it does NOT do: {note}"
    assert "auction:auction-1" in note, f"the absence does not name the key it looked at: {note}"


def test_absence_of_a_store_that_cannot_explain_itself_does_not_invent_an_eviction() -> None:
    """``absence`` is an OPTIONAL hook; every in-repository test double implements none."""

    class BareStore:
        def load(self, auction_id: str) -> None:
            return None

        def save(self, record: AuctionRecord) -> None:
            return None

        def reserve(self, auction_id: str, name: str, token: str) -> str | None:
            return None

        def release(self, auction_id: str, name: str, token: str) -> None:
            return None

    note = absence_of(BareStore(), "auction-1")

    assert "evict" not in note.lower(), (
        f"a store with no `absence` hook was described as possibly evicting: {note}. It evicts "
        f"nothing, so that is the same species of wrong answer as blaming a TTL"
    )
    assert "never created" in note
    assert str(AUCTION_TTL_SECONDS) in note
