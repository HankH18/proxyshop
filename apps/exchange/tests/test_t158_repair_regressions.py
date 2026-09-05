"""The four defects an adversarial review found *in the T-158 fix itself* (b89bdd5).

``test_t158_acceptance_claim.py`` proves the guard the ticket asked for: one auction, one
mint, across threads and across OS processes. It does not touch any of the four repairs
below, and neither does anything else in the repository — which was measured rather than
assumed. Reverting any one of the four hunks in ``b89bdd5`` left ``make verify`` at
``OK: all`` (5662 passed, 0 failed) and ``.swarm-loop/acceptance/run.py --count-passing`` at
``120``. A money guard whose repairs no gate can see is a money guard one refactor away from
being un-repaired, so this file is those four properties as tests.

Every test here was driven against **26d9a03** — the parent of ``b89bdd5``, the tree that has
the T-158 fix but not its repairs — in a read-only detached worktree, and every one of them
goes red there. The measured failure is quoted in each docstring, because a test whose red
nobody has seen is a test nobody knows the shape of.

The four properties, and why each is a separate test
----------------------------------------------------

**A — a reservation is a constraint on a move that HAPPENED.** ``_transition`` reserves the
exit from a state and ``create`` reserves the id, and a move that raised before ``store.save``
landed did not happen. Split three ways: ``close``, ``create``, and the same pair under
``KeyboardInterrupt``, because the release is written ``except BaseException`` and an
``except Exception`` would pass the first two tests and fail the last two.

**B — the shape of a claim table's answer is as untrusted as the call.** ``AcceptanceClaims``
is a ``Protocol``, so nothing runtime-checks an injected table.

**C — a claim taken in front of a raise is a claim nobody gives back.**

**D — "the mint succeeded" is decided by whether a code EXISTS**, not by whether this accept
returned one. The orphan path keeps the claim; every other refusal still releases it, which
is A5's re-offer and is the half that must not regress while fixing the other.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from exchange.accept import platform_registered_domains, use_registered_domains
from exchange.accept.claims import ACCEPTANCE_RESERVATION, ClaimOutcome, StoreAcceptanceClaims
from exchange.accept.offer import accept
from exchange.accept.routes import InMemoryAuctionBids, configure_accept
from exchange.auction.state import (
    AuctionRecord,
    AuctionStateMachine,
    creation_reservation,
    exit_reservation,
)
from exchange.checkout import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from fastapi.testclient import TestClient

SELLER_DOMAIN = "store-a.example.com"
RIVAL_DOMAIN = "attacker.tld"
PLATFORM_DOMAINS = {"store-a": SELLER_DOMAIN, "store-b": "store-b.example.com"}
ELIGIBILITY = {"store-a": ELIGIBLE, "store-b": ELIGIBLE}

T_NOW = 1_700_000_000.0
T_FUTURE = 2_000_000_000.0


# =====================================================================================
# Doubles
# =====================================================================================
class RecordingAuctionStore:
    """A full :class:`~exchange.auction.state.AuctionStore` whose reservation table is public.

    ``reserve``/``release`` are re-implemented here rather than delegated, for one reason:
    every assertion in this file is then on the store's *own* table through a public
    attribute, instead of reaching into ``InMemoryAuctionStore``'s private dict. The
    semantics are the documented ones and nothing weaker — ``reserve`` is at-most-once and
    names the holder to every later caller, and ``release`` is a no-op unless the caller
    still holds the reservation with exactly its own token. ``InMemoryAuctionStore``'s
    atomicity under threads is pinned separately, in
    ``test_t158_acceptance_claim.py::test_many_threads_racing_one_reservation_produce_exactly_one_winner``.

    ``fail_next_save`` arms ONE ``save`` to raise and then disarms itself, which is what a
    dropped Redis connection is: transient. The second attempt is the interesting one.
    """

    def __init__(self) -> None:
        self.records: dict[str, str] = {}
        self.reservations: dict[tuple[str, str], str] = {}
        self.fail_next_save: BaseException | None = None
        self.save_calls = 0

    def load(self, auction_id: str) -> AuctionRecord | None:
        blob = self.records.get(auction_id)
        return AuctionRecord.from_json(blob) if blob is not None else None

    def save(self, record: AuctionRecord) -> None:
        self.save_calls += 1
        failure, self.fail_next_save = self.fail_next_save, None
        if failure is not None:
            raise failure
        self.records[record.auction_id] = record.to_json()

    def reserve(self, auction_id: str, name: str, token: str) -> str | None:
        held = self.reservations.get((str(auction_id), str(name)))
        if held is not None:
            return held
        self.reservations[(str(auction_id), str(name))] = str(token)
        return None

    def release(self, auction_id: str, name: str, token: str) -> None:
        key = (str(auction_id), str(name))
        if self.reservations.get(key) == str(token):
            del self.reservations[key]


class BareBooleanClaims:
    """A deployment's own idempotency table, answering the WRONG SHAPE: a bare ``bool``.

    Not a strawman. ``claims.py`` invites exactly this table — "a unique index on
    ``(auction_id)`` in Postgres is the same constraint by another name" — and
    ``AcceptanceClaims`` is a ``Protocol``, so a table that answers ``True``/``False``
    instead of a :class:`~exchange.accept.claims.ClaimOutcome` is wired without a murmur.
    """

    def __init__(self) -> None:
        self.claim_calls: list[tuple[str, str]] = []
        self.release_calls: list[tuple[str, str]] = []

    def claim(self, auction_id: str, bid_ref: str) -> bool:
        self.claim_calls.append((str(auction_id), str(bid_ref)))
        return True

    def release(self, auction_id: str, bid_ref: str) -> None:
        self.release_calls.append((str(auction_id), str(bid_ref)))


class MintingMerchant:
    """``POST /codes``. Every call issues a REAL live single-use code and logs it.

    Being *asked* is being *minted*: the merchant has no way to un-issue one, so
    :attr:`issued` is the seller's money and is the only number the D tests grade on.

    ``permalink_host`` is what makes the post-mint refusal reachable. Pointed at a host the
    platform registry does not hold for this store, the permalink fails step 5 of
    ``CheckoutProvider.checkout`` — the one check that cannot be hoisted ahead of the mint,
    because the permalink does not exist until the provider has run — and the refusal comes
    back as ``OrphanedCheckoutCode`` with a live code attached.
    """

    def __init__(self, *, permalink_host: str) -> None:
        self.issued: list[str] = []
        self._permalink_host = permalink_host

    def create_code(self, store_id: Any, offer: Any) -> dict[str, Any]:
        code = f"PSX-LIVE-{len(self.issued) + 1:04d}"
        self.issued.append(code)
        return {
            "code": code,
            "permalink_url": f"https://{self._permalink_host}/cart/1:1?discount={code}",
        }

    __call__ = create_code


def honest_bid(bid_ref: str = "bid-a", store_id: str = "store-a") -> dict[str, Any]:
    """A bid whose checkout URL is on the host the platform registry has for its store."""
    domain = PLATFORM_DOMAINS[store_id]
    return {
        "bid_id": bid_ref,
        "store_id": store_id,
        "store_domain": domain,
        "offer": {
            "product_ref": "product-1",
            "unit_price": 100.0,
            "total_price": 100.0,
            "checkout_url": f"https://{domain}/cart/1:1",
            "expires_at": T_FUTURE,
        },
    }


def offdomain_bid(bid_ref: str = "bid-liar") -> dict[str, Any]:
    """A bid refused by step 2 — BEFORE the mint — so its refusal carries no orphan."""
    bid = honest_bid(bid_ref, "store-a")
    bid["offer"]["checkout_url"] = f"https://{RIVAL_DOMAIN}/cart/1:1"
    return bid


# =====================================================================================
# Shared wiring
# =====================================================================================
@pytest.fixture(autouse=True)
def restored_registry_for_repair_tests() -> Iterator[None]:
    """``configure_accept`` writes a process-wide seller registry; put back what we found.

    Leaked wiring would let a test in another file pass on this file's registry, which is
    what ``test_accept_routes.py``'s ``unwired`` fixture exists to prevent.
    """
    previous = platform_registered_domains()
    try:
        yield
    finally:
        use_registered_domains(previous)


def opened_auction(store: RecordingAuctionStore, auction_id: str) -> AuctionStateMachine:
    """One auction in ``open`` — the state ``close`` moves out of."""
    machine = AuctionStateMachine(store)
    machine.create(auction_id, intent_id="intent-1", cluster_id="cluster-1", roster=[])
    machine.open(auction_id, now=T_NOW)
    return machine


def served_app(bids: list[dict[str, Any]], merchant: Any, auction_id: str, **extra: Any) -> Any:
    """The real exchange over one ``closed`` auction, wired as ``test_accept_routes.py`` does."""
    machine = AuctionStateMachine()
    machine.create(auction_id, intent_id="intent-1", cluster_id="cluster-1", roster=[])
    machine.open(auction_id, now=T_NOW)
    machine.close(auction_id, now=T_NOW + 1.0)
    book = InMemoryAuctionBids()
    book.record(auction_id, bids)
    app = create_app()
    configure_accept(
        app,
        machine=machine,
        bids=book,
        code_creator=merchant,
        checkout_mode="shopify",
        registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS),
        eligibility=StaticSellerEligibility(ELIGIBILITY),
        **extra,
    )
    return app


def denial_of(response: Any) -> str:
    """The ``denial_reason`` a 409 carries, or ``''`` for any other answer."""
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("denial_reason") or "") if isinstance(body, dict) else ""


# =====================================================================================
# A. A reservation is a constraint on a move that HAPPENED
# =====================================================================================
def test_a_close_whose_save_raises_once_is_still_retryable() -> None:
    """One ``ConnectionError`` out of ``save`` must not wedge the auction for its whole TTL.

    ``_transition`` reserves ``exit:{state}`` and then writes. Between those two lines is a
    network call, and a store that answers the reservation and then drops the connection
    used to leave the exit from ``open`` held by a move that never landed — permanently on
    ``InMemoryAuctionStore``, which has no TTL, and for fifteen minutes against Redis. The
    absent ``exit:open`` entry below is the sharpest signal available: the reservation is not
    merely ignorable on the retry, it is *gone*.

    Measured at 26d9a03, this test's own store::

        close attempt 1: RAISED ConnectionError: redis went away
        close attempt 2 (RETRY): RAISED IllegalAuctionTransition: auction 'a1' has already
            left 'open' for 'closed'; a concurrent request won that move, so 'closed' is
            not applied

    A retryable I/O error turned into a permanent refusal whose message blames a collision
    that never happened — so it would have been misdiagnosed as a race, not as an outage.
    """
    store = RecordingAuctionStore()
    machine = opened_auction(store, "a1")
    reserved_before = dict(store.reservations)

    store.fail_next_save = ConnectionError("redis went away")
    with pytest.raises(ConnectionError):
        machine.close("a1", now=T_NOW + 1.0)

    assert machine.state_of("a1") == "open", "a failed close moved the auction anyway"
    assert ("a1", exit_reservation("open")) not in store.reservations, (
        "the exit from 'open' is still reserved by a close that raised before it landed; "
        "every retry will be refused as a concurrent request that never existed"
    )
    assert store.reservations == reserved_before, (
        "the failed close left the reservation table changed"
    )

    # THE assertion: the same call, again, on the same auction.
    machine.close("a1", now=T_NOW + 2.0)
    assert machine.state_of("a1") == "closed"
    assert store.reservations[("a1", exit_reservation("open"))] == "closed"


def test_a_create_whose_save_raises_once_is_still_retryable() -> None:
    """``create`` reserves the id before it looks, so it has to give the id back too.

    Same shape as the transition and a worse symptom: the refusal is ``already exists`` for
    an auction id whose ``store.load(...)`` is ``None``, so an operator reading the message
    goes looking for a record that was never written.

    Measured at 26d9a03::

        create attempt 1: RAISED ConnectionError: redis went away
        store.load('a2')  : None
        create attempt 2 (RETRY): RAISED IllegalAuctionTransition: auction 'a2' already exists
        record actually in store after both attempts: None
    """
    store = RecordingAuctionStore()
    machine = AuctionStateMachine(store)

    store.fail_next_save = ConnectionError("redis went away")
    with pytest.raises(ConnectionError):
        machine.create("a2", intent_id="intent-1", cluster_id="cluster-1", roster=[])

    assert store.load("a2") is None, "a create that raised out of save left a record behind"
    assert ("a2", creation_reservation()) not in store.reservations, (
        "the id 'a2' is still reserved by a create that never wrote a record, so every "
        "retry answers 'already exists' for an auction that does not exist"
    )

    record = machine.create("a2", intent_id="intent-1", cluster_id="cluster-1", roster=[])
    assert record.auction_id == "a2"
    assert store.load("a2") is not None, "the retry reported success and stored nothing"


def test_a_keyboard_interrupt_out_of_save_also_gives_the_close_reservation_back() -> None:
    """``except BaseException``, not ``except Exception`` — and this is the difference.

    A ``KeyboardInterrupt``, a ``SystemExit`` or a cancelled asyncio task arriving between
    the reservation and the write wedges the auction exactly as a dropped connection does,
    and none of the three is an ``Exception``. An ``except Exception`` release passes the two
    tests above and fails this one, which is the whole reason it is asserted separately.

    Measured at 26d9a03::

        close attempt 1: RAISED KeyboardInterrupt:
        close attempt 2 (RETRY): RAISED IllegalAuctionTransition: auction 'a1' has already
            left 'open' for 'closed'; a concurrent request won that move, so 'closed' is
            not applied
    """
    store = RecordingAuctionStore()
    machine = opened_auction(store, "a1")

    store.fail_next_save = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        machine.close("a1", now=T_NOW + 1.0)

    assert ("a1", exit_reservation("open")) not in store.reservations, (
        "an interrupt between the reservation and the write left the exit from 'open' held; "
        "the release is catching Exception rather than BaseException"
    )

    machine.close("a1", now=T_NOW + 2.0)
    assert machine.state_of("a1") == "closed"


def test_a_keyboard_interrupt_out_of_save_also_gives_the_create_reservation_back() -> None:
    """The ``create`` half of the ``BaseException`` property.

    Measured at 26d9a03::

        create attempt 1: RAISED KeyboardInterrupt:
        create attempt 2 (RETRY): RAISED IllegalAuctionTransition: auction 'a2' already exists
        record actually in store after both attempts: None
    """
    store = RecordingAuctionStore()
    machine = AuctionStateMachine(store)

    store.fail_next_save = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        machine.create("a2", intent_id="intent-1", cluster_id="cluster-1", roster=[])

    assert ("a2", creation_reservation()) not in store.reservations, (
        "an interrupt left the id reserved with no record behind it; the release is "
        "catching Exception rather than BaseException"
    )

    machine.create("a2", intent_id="intent-1", cluster_id="cluster-1", roster=[])
    assert store.load("a2") is not None


# =====================================================================================
# B. The SHAPE of a claim table's answer is as untrusted as the call
# =====================================================================================
def test_a_claim_table_answering_a_bare_bool_is_a_409_and_not_a_500() -> None:
    """``outcome.won`` read outside the ``try`` that guarded the call was guarding half of it.

    Driven through the real served route, because the defect is only a defect at the
    deployment boundary: the ``AttributeError`` came out of the ASGI app as an HTTP 500 with
    the claim taken and never released. ``raise_server_exceptions=False`` is deliberate — it
    makes this client answer the way a real uvicorn worker would, instead of re-raising the
    exception into the test and hiding what a client actually receives.

    Measured at 26d9a03::

        HTTP status       : 500
        body              : Internal Server Error
        claim() calls     : [('auction-route-1', 'bid-a')]
        release() calls   : []

    The last two lines are the cost: a 500 is a status a client retries, and the retry meets
    a claim the first attempt took and no code path will ever give back.

    The two negative assertions are the T-264/T-326 shape held on this new refusal: the
    exception's TYPE goes into the client-visible, persisted ``denial_reason`` and nothing
    else — no ``repr`` of an injected object (which is where the ``0x`` would come from), and
    no discount code.
    """
    merchant = MintingMerchant(permalink_host=SELLER_DOMAIN)
    table = BareBooleanClaims()
    auction_id = "auction-route-1"
    app = served_app(
        [honest_bid("bid-a"), honest_bid("bid-b", "store-b")],
        merchant,
        auction_id,
        claims=table,
    )
    assert app.state.acceptance_claims is table, "the injected table was not the one wired"

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-a"})

    assert response.status_code == 409, (
        f"a claim table answering a bare bool served HTTP {response.status_code}: {response.text}"
    )
    reason = denial_of(response)
    assert reason.startswith("unrecordable_acceptance"), reason
    assert table.claim_calls == [(auction_id, "bid-a")], "the table was not actually consulted"

    # Fail CLOSED: an acceptance that cannot be recorded cannot refuse the second accept
    # either, so the FIRST one is refused rather than minting a code nobody can make
    # single-use.
    assert merchant.issued == [], (
        f"a code was minted behind an unreadable claim answer: {merchant.issued}"
    )
    assert "0x" not in reason, f"a memory address reached the published denial: {reason}"
    assert "PSX-LIVE" not in response.text, f"a discount code reached the 409 body: {response.text}"
    assert "Traceback" not in response.text


# =====================================================================================
# C. A claim taken in front of a raise is a claim nobody gives back
# =====================================================================================
def test_an_auction_whose_now_will_not_parse_takes_no_claim() -> None:
    """The ``CheckoutRequest`` is built BEFORE the claim, and that ordering is the repair.

    ``CheckoutRequest`` carries ``now=float(_read(auction, "now") or 0.0)``, so an auction
    whose ``now`` will not parse raises ``ValueError`` on a perfectly ordinary code path. The
    ``ValueError`` itself is unchanged and is not the subject: what changed is whether the
    claim was already taken when it fired. A claim taken in front of a raise is held for the
    auction's whole TTL by a deployment's own bad field, and nothing ever releases it —
    ``offer.py`` only releases inside the ``except`` around ``provider.checkout``, which this
    never reaches.

    Measured at 26d9a03::

        accept() RAISED     : ValueError: could not convert string to float: 'not-a-number'
        reservations after  : [(('auction-repair-c', 'accept'), 'bid-a')]
        claim table EMPTY   : False

    At b89bdd5 the same ``ValueError`` is raised from ``offer.py``'s ``CheckoutRequest``
    construction, which now sits *above* the claim, and the table is untouched.
    """
    store = RecordingAuctionStore()
    claims = StoreAcceptanceClaims(store)
    merchant = MintingMerchant(permalink_host=SELLER_DOMAIN)
    auction_id = "auction-repair-c"
    auction: dict[str, Any] = {
        "auction_id": auction_id,
        "bids": [honest_bid("bid-a"), honest_bid("bid-b", "store-b")],
        "accepted_bid_ref": None,
        "now": "not-a-number",
    }

    with pytest.raises(ValueError, match="could not convert string to float"):
        accept(
            auction,
            "bid-a",
            merchant,
            "shopify",
            registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS),
            claims=claims,
        )

    assert store.reservations == {}, (
        f"the claim was taken in front of the raise and nothing gives it back: "
        f"{sorted(store.reservations.items())}"
    )
    assert (auction_id, ACCEPTANCE_RESERVATION) not in store.reservations
    assert merchant.issued == []

    # The positive form of "empty": the auction is still acceptable, so a deployment's bad
    # field costs one request rather than the auction.
    assert claims.claim(auction_id, "bid-later") == ClaimOutcome(won=True, holder="bid-later")


# =====================================================================================
# D. "The mint succeeded" means a code EXISTS, not that this accept returned one
# =====================================================================================
def test_the_orphan_path_keeps_the_claim_so_a_second_accept_mints_no_second_code() -> None:
    """A refusal that happened AFTER ``POST /codes`` must not reopen the auction for a re-mint.

    The merchant here mints a real code and answers a permalink on a host the platform never
    registered, while the offer's own ``checkout_url`` is on-domain — so every pre-mint check
    passes and the refusal is step 5's, which is the one check that cannot be hoisted ahead
    of the mint. A5's argument for re-offering the next slot does not extend to re-minting
    against an auction that already cost the seller a live discount, so this refusal keeps
    the claim and the second accept is refused before it reaches the merchant.

    Measured at 26d9a03, three sequential accepts on ONE auction::

        accept #1 (bid-a) : HTTP 409  checkout_refused: OrphanedOffDomainCheckout: ...
        accept #2 (bid-a) : HTTP 409  checkout_refused: OrphanedOffDomainCheckout: ...
        accept #3 (bid-b) : HTTP 409  checkout_refused: OrphanedOffDomainCheckout: ...
        codes the merchant ACTUALLY issued : ['PSX-LIVE-0001', 'PSX-LIVE-0002',
                                              'PSX-LIVE-0003']  (3)

    Three live single-use discount codes for one purchase, each refusal correctly telling the
    buyer no — the HTTP status was right every time and the seller's account was wrong. Note
    that ``main`` behaves identically, so this is a repair rather than a regression; it is
    here because nothing else in the repository asserts it.

    The auction is unacceptable for the rest of its TTL afterwards, and that is the
    deliberate trade (``claims.py``): a buyer whose merchant orphaned a code gets neither
    this slot nor the next, which is the fail-closed direction on a money path.
    """
    merchant = MintingMerchant(permalink_host=RIVAL_DOMAIN)
    auction_id = "auction-orphan-1"
    app = served_app([honest_bid("bid-a"), honest_bid("bid-b", "store-b")], merchant, auction_id)
    client = TestClient(app)

    first = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-a"})
    assert first.status_code == 409, first.text
    assert denial_of(first).startswith("checkout_refused"), denial_of(first)
    assert len(merchant.issued) == 1, (
        f"the post-mint refusal is not reachable — the merchant issued {merchant.issued}; "
        f"without a minted code this test grades nothing"
    )

    second = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-a"})
    assert second.status_code == 409, second.text
    assert denial_of(second).startswith("already_accepted"), (
        f"the second accept was refused as {denial_of(second)!r} rather than by the claim, "
        f"which means the orphan refusal released it"
    )

    third = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-b"})
    assert third.status_code == 409, third.text
    assert denial_of(third).startswith("already_accepted"), denial_of(third)

    assert merchant.issued == ["PSX-LIVE-0001"], (
        f"three accepts on one orphaned auction issued {merchant.issued} — one purchase, "
        f"more than one live single-use discount code"
    )
    # T-215: the live code is carried on the orphan and filed in the `code_created` event,
    # never published in the prose a client reads.
    bodies = json.dumps([first.text, second.text, third.text])
    assert "PSX-LIVE-0001" not in bodies


def test_an_ordinary_pre_mint_refusal_still_releases_the_claim_for_the_next_slot() -> None:
    """A5: keeping the claim on the orphan path must not turn EVERY refusal into a wedge.

    This is the half that must not regress while the other half is fixed. An off-domain
    ``checkout_url`` on the bid is refused by step 2, before the merchant is asked for
    anything, so no code exists, nothing has cost the seller money, and the auction has to be
    claimable again for the next slot to be offered. A claim kept here would be worse than
    the bug it replaced: one bad bid would wedge the auction for its whole 15-minute TTL and
    the buyer would get neither this slot nor the next.

    Green at 26d9a03 as well as at b89bdd5 — the repair narrowed the release to
    ``orphan is None`` rather than removing it, and this test is what pins that it was
    narrowed and not removed.
    """
    merchant = MintingMerchant(permalink_host=SELLER_DOMAIN)
    auction_id = "auction-a5-1"
    app = served_app([offdomain_bid("bid-liar"), honest_bid("bid-a")], merchant, auction_id)
    client = TestClient(app)

    refused = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-liar"})
    assert refused.status_code == 409, refused.text
    assert denial_of(refused).startswith("checkout_refused"), denial_of(refused)
    assert merchant.issued == [], "a pre-mint refusal asked the merchant for a code"

    accepted = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": "bid-a"})
    assert not denial_of(accepted).startswith("already_accepted"), (
        "the next slot was refused by a claim a costless refusal should have released"
    )
    assert accepted.status_code == 200, accepted.text
    assert merchant.issued == ["PSX-LIVE-0001"], merchant.issued
    body = accepted.json()
    assert body["code"] == "PSX-LIVE-0001"
    assert body["permalink_url"].startswith(f"https://{SELLER_DOMAIN}/")
