"""The served accept is what feeds the bandit — the third break in R16's learning loop.

WHAT WAS MEASURED
-----------------
``POST /internal/outcomes`` (``policy/routes.py``) is served, contract-published and tested,
and it had **no production producer anywhere in this repository**: the only callers were
tests and ``e2e/``. So in a real deployment ``InMemoryBanditPosteriors.record`` was never
called, ``bandit.update`` never ran, and every ``(cluster, store)`` posterior sat on its
trust-seeded prior for the whole life of the process. The Thompson sampler ran on every
served auction and always sampled the same priors, so exposure could not move with results —
a learning loop with a door at one end, a model at the other, and nothing walking between
them.

WHY THE ACCEPT IS THE HONEST PRODUCER
-------------------------------------
``POST /auctions/{auction_id}/accept`` is the only place inside this service that knows all
three things an outcome needs at the instant they are all true: the auction's ``cluster_id``
(off the ``AuctionRecord`` the handler already loaded), the store the buyer chose (off the
checkout port's own ``accepted`` event, the same value the ledger is stamped with), and the
stores that were shown and passed over — off the SHORTLIST this auction stored, which is the
only record of what the shopper was actually put in front of.

SHOWN IS NOT ELIGIBLE, AND THAT DISTINCTION IS A DEFECT THIS FILE NOW GUARDS
----------------------------------------------------------------------------
The first version of this fold read the BID BOOK, and the bid book is not the shortlist.
``auction/routes.py`` fills it through ``collected_bid_records`` with every candidate the
ranking found ELIGIBLE; the shortlist is capped at ``ranking.shortlist.MAX_SLOTS`` — four —
and everything past the cut is benched and never rendered. Measured over the real
``POST /auctions`` then ``POST /auctions/{id}/accept`` with six eligible stores: four slots,
six records in the book, and both benched stores took a loss for an offer no shopper saw.

That is a ratchet rather than a rounding error, and it points straight at R12: a benched store
accrues beta forever and never alpha, so its posterior falls, so the exploration slice reading
that same posterior benches it again. ``test_a_store_the_ranking_benched_takes_no_loss_at_all``
is the test that refuses it, and it is driven over the real ``POST /auctions`` because only the
real ranker can bench anybody.

BOTH DIRECTIONS, WHICH IS THE POINT
-----------------------------------
A buyer accepting one shortlisted offer is one **win** for the accepted store in that cluster
and one **loss** for every other store the same auction showed. Recording only the win would
raise every posterior that ever appeared on a shortlist — alpha climbing everywhere, beta
never moving — and a sampler over posteriors that all rise together cannot discriminate. That
is why :func:`test_a_store_that_was_shown_and_passed_over_takes_a_loss` is not a nicety: a fix
that recorded only wins would pass the win test and be worthless.

WHAT IS ASSERTED, AND HOW
-------------------------
Every test here drives the real ``POST /auctions/{auction_id}/accept`` over HTTP through a
``TestClient`` against ``create_app()``. Nothing calls ``record_conversion`` or
``_record_auction_outcomes`` directly — a fold that only happens when a test calls it is the
defect this file exists to close, not the fix. The posteriors are read back off
``app.state.bandit_posteriors.state()``, which is the only reader there is: no HTTP route
exposes a posterior, and adding one to make this test easier would publish model internals to
close a test's convenience gap.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from contracts.labels import LABEL_UNVERIFIED
from contracts.protocol import Shortlist, ShortlistSlot, ShortlistSlotName
from exchange.accept import use_registered_domains
from exchange.accept.routes import InMemoryAuctionBids, configure_accept
from exchange.auction.routes import configure_auctions
from exchange.auction.state import AuctionStateMachine
from exchange.checkout import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.policy.routes import InMemoryBanditPosteriors, configure_outcomes
from exchange.ranking.serving import configure_ranking, shortlist_store
from exchange.ranking.shortlist import MAX_SLOTS
from fastapi.testclient import TestClient

from .test_accept_routes import (  # the wiring this path is already tested through
    PLATFORM_DOMAINS,
    RecordingCodeCreator,
    honest_bid,
)
from .test_ranking_served import (  # the served POST /auctions this file has to drive for real
    Bidders,
    _bid,
    _catalog,
    _domain,
    _intent,
    _rostered,
)

CLUSTER = "cluster-1"
AUCTION_ID = "auction-bandit"

#: What ``bandit.initial_state`` seeds a pair with when the trust snapshot says nothing about
#: the store — ``score`` defaults to 0.5, ``confidence`` to 0.0, so ``PRIOR_WEIGHT * 0.0``
#: contributes nothing and both parameters are 1.0. Written down because the cold-start test
#: asserts absolute numbers against it, and a reader has to be able to check the arithmetic:
#: one win is ``(2.0, 1.0)``, one loss is ``(1.0, 2.0)``.
PRIOR = (1.0, 1.0)


@pytest.fixture
def unwired() -> Iterator[None]:
    """Leave the process-wide registered-domain registry exactly as this test found it."""
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


def store_shortlist(app: Any, auction_id: str, bids: list[dict[str, Any]]) -> dict[str, Any]:
    """Put the shortlist a real close would have stored for ``bids`` into the app's store.

    Not decoration. ``auction/routes.py`` writes the shortlist at
    ``shortlist_store(request.app).put(auction_id, shortlist, now=closed_at)`` on the close and
    records the bid book seventy lines further down in the same request, so **a served auction
    whose book has records always has a stored shortlist** — a closed auction with bids and no
    shortlist is a state ``POST /auctions`` cannot produce. Building the machine and the book by
    hand and stopping there modelled exactly that impossible state, and the fold reads the
    shortlist now, so the fixture has to carry it.

    Built through the pinned ``Shortlist``/``ShortlistSlot`` models rather than as a loose dict,
    for the reason ``ranking.serving._with_offer_fields`` gives: what the store holds is a fixed
    point of ``Shortlist.model_validate(...).model_dump(mode="json")``, and a fixture in some
    other spelling would be testing a shape no route ever stores.

    One slot per bid, in order, capped at ``MAX_SLOTS`` exactly as ``ranking.shortlist.build``
    caps it — so passing more bids than there are slots benches the tail here too.
    """
    slots = [
        ShortlistSlot(
            slot=ShortlistSlotName(name),
            bid_ref=str(bid["bid_id"]),
            fit_score=1.0 - index / 100.0,
            # The ranker's own summary shape (`ranking.shortlist.trust_summary`), which is
            # deliberately NOT what the fold reads the store off — see `_shown_stores`.
            trust_summary={"store_id": str(bid["store_id"]), "available": False},
            provenance_labels=[LABEL_UNVERIFIED],
        )
        for index, (bid, name) in enumerate(
            zip(bids[:MAX_SLOTS], list(ShortlistSlotName), strict=False)
        )
    ]
    shortlist = Shortlist(auction_id=auction_id, slots=slots).model_dump(mode="json")
    shortlist_store(app).put(auction_id, shortlist, now=time.time())
    return shortlist


def wired_app(
    bids: list[dict[str, Any]],
    *,
    cluster_id: str = CLUSTER,
    shortlist: bool = True,
) -> tuple[Any, str]:
    """A booted exchange holding one ``closed`` auction, with an empty posterior book wired.

    ``shortlist=False`` withholds the stored shortlist — the "evicted, expired, or never
    stored" case, which is a real deployment state (the store is capped and TTL'd) and not
    only a fixture convenience. Nothing may be folded for it, and
    :func:`test_an_unreadable_shortlist_folds_nothing_at_all_not_even_the_win` is the
    assertion.

    The book is wired explicitly rather than left to ``_posteriors``' create-on-first-use, so
    that "nothing was recorded" is distinguishable from "the book was never built": a test
    reading ``None`` off the app state cannot tell those apart, and only one of them is a
    passing accept.
    """
    machine = AuctionStateMachine()
    machine.create(AUCTION_ID, intent_id="intent-1", cluster_id=cluster_id, roster=[])
    machine.open(AUCTION_ID, now=1_700_000_000.0)
    machine.close(AUCTION_ID, now=1_700_000_001.0)
    book = InMemoryAuctionBids()
    book.record(AUCTION_ID, bids)
    app = create_app()
    configure_accept(
        app,
        machine=machine,
        bids=book,
        code_creator=RecordingCodeCreator(),
        checkout_mode="shopify",
        registered_domains=StaticRegisteredDomains(PLATFORM_DOMAINS),
        eligibility=StaticSellerEligibility({"store-a": ELIGIBLE, "store-b": ELIGIBLE}),
    )
    configure_outcomes(app, posteriors=InMemoryBanditPosteriors())
    if shortlist:
        store_shortlist(app, AUCTION_ID, bids)
    return app, AUCTION_ID


def shortlist_of_two() -> list[dict[str, Any]]:
    """Two stores in one auction: the one the buyer takes, and the one it passes over."""
    return [honest_bid(), honest_bid("bid-b", "store-b")]


def pair(app: Any, store_id: str, cluster_id: str = CLUSTER) -> tuple[float, float] | None:
    """``(alpha, beta)`` for one ``(cluster, store)``, or ``None`` if the book has no such pair.

    ``app.state.bandit_posteriors.state()`` is ``None`` until the first outcome is recorded,
    which is itself the measurement in :func:`test_before_the_accept_the_book_is_empty`.
    """
    state = app.state.bandit_posteriors.state()
    if state is None:
        return None
    posterior = state.posteriors.get(cluster_id, {}).get(store_id)
    if posterior is None:
        return None
    return (float(posterior.alpha), float(posterior.beta))


def warm(app: Any) -> dict[str, tuple[float, float]]:
    """Fold one win and one loss for each store so every assertion has a real BEFORE number.

    Without this the "before" of a cold book is ``None`` and "strictly more alpha" has nothing
    to be strictly more than. It also makes the test stronger than a cold one on its own: the
    accept has to ADD to learning that is already there, which is what
    :meth:`InMemoryBanditPosteriors.record`'s carry-over across its re-seed is for. A pair that
    was reset to its prior by the accept would fail these tests, and reading ``2.0`` on a
    cold-started book would not have caught it.
    """
    book = app.state.bandit_posteriors
    for store in ("store-a", "store-b"):
        book.record(store, CLUSTER, True)
        book.record(store, CLUSTER, False)
    return {store: pair(app, store) or PRIOR for store in ("store-a", "store-b")}


def accept(app: Any, bid_ref: str = "bid-a") -> Any:
    return TestClient(app).post(f"/auctions/{AUCTION_ID}/accept", json={"bid_ref": bid_ref})


# =====================================================================================
# the control: nothing has been folded before the accept
# =====================================================================================
def test_before_the_accept_the_book_is_empty(unwired: None) -> None:
    """A wired-but-untouched book holds no state at all.

    The positive control for every assertion below. Without it, a test that read a moved
    posterior could be reading one some other part of the boot had already written.
    """
    app, _auction_id = wired_app(shortlist_of_two())

    assert app.state.bandit_posteriors.state() is None
    assert pair(app, "store-a") is None and pair(app, "store-b") is None


# =====================================================================================
# the fold itself — one win, one loss, in the auction's own cluster
# =====================================================================================
def test_the_accepted_store_takes_a_win(unwired: None) -> None:
    """``alpha`` for ``(cluster-1, store-a)`` is strictly greater after the served accept."""
    app, _auction_id = wired_app(shortlist_of_two())
    before = warm(app)

    response = accept(app)
    assert response.status_code == 200, response.text

    after = pair(app, "store-a")
    assert after is not None, "the accept folded nothing for the store the buyer chose"
    assert after[0] > before["store-a"][0], (
        f"alpha for (cluster-1, store-a) did not move: {before['store-a'][0]} -> {after[0]}"
    )
    # And it is a WIN, not a nudge in both directions: beta is untouched.
    assert after[1] == before["store-a"][1], (
        f"the accepted store also took a loss: beta {before['store-a'][1]} -> {after[1]}"
    )


def test_a_store_that_was_shown_and_passed_over_takes_a_loss(unwired: None) -> None:
    """``beta`` for ``(cluster-1, store-b)`` is strictly greater after the served accept.

    The half that makes the bandit a bandit. A fix that recorded only the win would pass the
    test above and leave every posterior rising together, which is a counter of shortlist
    appearances rather than evidence about a store.
    """
    app, _auction_id = wired_app(shortlist_of_two())
    before = warm(app)

    response = accept(app)
    assert response.status_code == 200, response.text

    after = pair(app, "store-b")
    assert after is not None, "the accept folded nothing for the store that was passed over"
    assert after[1] > before["store-b"][1], (
        f"beta for (cluster-1, store-b) did not move: {before['store-b'][1]} -> {after[1]}"
    )
    assert after[0] == before["store-b"][0], (
        f"the rejected store also took a win: alpha {before['store-b'][0]} -> {after[0]}"
    )


def test_a_cold_book_lands_exactly_one_win_and_exactly_one_loss(unwired: None) -> None:
    """From the seeded prior, absolute numbers: ``(2.0, 1.0)`` and ``(1.0, 2.0)``.

    The arithmetic stated rather than implied, so a fold that recorded the same outcome twice
    — a plausible shape if the win were also recorded through the shown-store loop — is a
    failure here rather than an unnoticed doubling of every signal.
    """
    app, _auction_id = wired_app(shortlist_of_two())

    assert accept(app).status_code == 200

    assert pair(app, "store-a") == (PRIOR[0] + 1.0, PRIOR[1]), pair(app, "store-a")
    assert pair(app, "store-b") == (PRIOR[0], PRIOR[1] + 1.0), pair(app, "store-b")


def test_the_outcome_lands_in_the_auctions_own_cluster_and_no_other(unwired: None) -> None:
    """Exposure is decided WITHIN a cluster, so the fold must not touch a second one."""
    app, _auction_id = wired_app(shortlist_of_two())

    assert accept(app).status_code == 200

    state = app.state.bandit_posteriors.state()
    assert state is not None
    assert set(state.posteriors) == {CLUSTER}, (
        f"the accept moved clusters it never happened in: {sorted(state.posteriors)}"
    )


# =====================================================================================
# what must NOT move it
# =====================================================================================
def test_a_denied_accept_moves_nothing(unwired: None) -> None:
    """A 409 is not an outcome: nobody converted and nobody was passed over for anybody."""
    app, _auction_id = wired_app(shortlist_of_two())
    before = warm(app)

    response = accept(app, bid_ref="bid-nowhere")
    assert response.status_code == 409, response.text
    assert response.json()["denial_reason"].startswith("unknown_bid")

    assert pair(app, "store-a") == before["store-a"], "a refused accept fed the bandit"
    assert pair(app, "store-b") == before["store-b"], "a refused accept fed the bandit"


def test_a_second_accept_on_the_same_auction_folds_nothing_further(unwired: None) -> None:
    """One purchase is one outcome, however many times the button is pressed.

    The guard that makes this true is not in the fold — it is the legality check at the top of
    ``accept_bid``: ``TRANSITIONS[ACCEPTED]`` is empty, so once the record is stamped
    ``accepted`` every later accept is refused before reaching the stamp, let alone the fold.
    This test is what keeps that reasoning honest if the guard ever moves.
    """
    app, _auction_id = wired_app(shortlist_of_two())

    assert accept(app).status_code == 200
    after_first = {store: pair(app, store) for store in ("store-a", "store-b")}

    second = accept(app, bid_ref="bid-b")
    assert second.status_code == 409, second.text

    assert {store: pair(app, store) for store in ("store-a", "store-b")} == after_first, (
        "a refused second accept folded a second outcome for the same purchase"
    )


def test_an_auction_with_no_cluster_folds_nothing_and_still_serves_the_buyer(
    unwired: None,
) -> None:
    """No cluster, no outcome — and the buyer still gets the permalink they are owed.

    Both halves matter and they pull in opposite directions. Pooling a cluster-less outcome
    into a shared bucket would move clusters it never happened in (which is why
    ``policy/routes.py`` answers 400 rather than inventing a default), and failing the accept
    over a posterior write would cost a buyer a live discount code for a learning signal.
    """
    app, _auction_id = wired_app(shortlist_of_two(), cluster_id="")

    response = accept(app)

    assert response.status_code == 200, response.text
    assert response.json()["permalink_url"], "the buyer lost their permalink to a bandit write"
    assert app.state.bandit_posteriors.state() is None, (
        "an outcome with no cluster was folded into some bucket anyway: "
        f"{app.state.bandit_posteriors.state()}"
    )


# =====================================================================================
# SHOWN, not merely ELIGIBLE — driven over the real `POST /auctions`, because only the
# real ranker can bench anybody
# =====================================================================================
SERVED_STORES: tuple[str, ...] = tuple(f"s{index}" for index in range(1, 7))


def served_auction(
    stores: tuple[str, ...] = SERVED_STORES,
) -> tuple[Any, str, list[str], list[str]]:
    """A REAL auction: solicited, ranked, shortlisted and stored by ``POST /auctions`` itself.

    Nothing here hands the exchange a shortlist. The roster goes in over HTTP, every store
    answers, the served ranker decides who fills the four slots, and the two things this file
    needs — who was SHOWN and who was BENCHED — are read back off the objects the request
    produced rather than assumed: the slots off ``ShortlistStore``, the eligible set off the
    bid book ``collected_bid_records`` wrote. That is the whole point. A fixture that decided
    for itself which stores were benched could not have caught the defect, because the defect
    was precisely that the two sets were being treated as one.

    Prices descend with the store index so the ordering is deterministic and the bench is
    stable, but no test asserts WHICH stores land where — only that the ones that landed off
    the shortlist are left alone.

    Returns:
        ``(app, auction_id, shown, benched)`` with ``shown`` in slot order.
    """
    app = create_app()
    configure_auctions(
        app,
        solicitor=Bidders(
            {store: _bid(store, 100.0 - index) for index, store in enumerate(stores)}
        ),
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={store: {"blacklisted": False, "score": 0.6} for store in stores},
        registered_domains=StaticRegisteredDomains({store: _domain(store) for store in stores}),
        catalog=_catalog(stores),
    )
    configure_accept(
        app,
        code_creator=RecordingCodeCreator(),
        checkout_mode="shopify",
        registered_domains=StaticRegisteredDomains({store: _domain(store) for store in stores}),
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_outcomes(app, posteriors=InMemoryBanditPosteriors())

    created = TestClient(app).post(
        "/auctions",
        json={
            "intent": _intent(),
            "roster": [_rostered(store, 500.0) for store in stores],
            "bid_timeout_seconds": 2.0,
        },
    )
    assert created.status_code == 201, created.text
    auction_id = str(created.json()["auction_id"])

    slots = (shortlist_store(app).get(auction_id) or {}).get("slots") or []
    # The join the fold itself makes: slot `bid_ref` -> the bid record's own `store_id`.
    by_ref = {
        str(bid["bid_id"]): str(bid["store_id"])
        for bid in app.state.auction_bids.bids_for(auction_id)
    }
    shown = [by_ref[str(slot["bid_ref"])] for slot in slots]
    benched = [store for store in by_ref.values() if store not in shown]
    return app, auction_id, shown, benched


def served_accept(app: Any, auction_id: str, store_id: str) -> Any:
    """Accept the slot belonging to ``store_id``, by the exchange's own minted reference."""
    slots = (shortlist_store(app).get(auction_id) or {}).get("slots") or []
    by_ref = {
        str(bid["bid_id"]): str(bid["store_id"])
        for bid in app.state.auction_bids.bids_for(auction_id)
    }
    refs = [str(slot["bid_ref"]) for slot in slots if by_ref.get(str(slot["bid_ref"])) == store_id]
    assert refs, f"{store_id!r} filled no slot in {auction_id}"
    return TestClient(app).post(f"/auctions/{auction_id}/accept", json={"bid_ref": refs[0]})


def test_the_served_auction_really_does_bench_somebody(unwired: None) -> None:
    """The positive control for the test below: six eligible stores, four slots, two benched.

    Without this, ``test_a_store_the_ranking_benched_takes_no_loss_at_all`` would pass
    vacuously the day the cap changed or the ranker stopped excluding anybody — an assertion
    about the benched store's posterior is worth nothing if there is no benched store.
    """
    app, auction_id, shown, benched = served_auction()

    assert len(shown) == MAX_SLOTS, f"the shortlist did not fill its {MAX_SLOTS} slots: {shown}"
    assert len(benched) == len(SERVED_STORES) - MAX_SLOTS, (
        f"six eligible stores and {MAX_SLOTS} slots should bench two: shown={shown} benched={benched}"
    )
    # ELIGIBLE is strictly larger than SHOWN, which is the whole distinction under test.
    book = [str(bid["store_id"]) for bid in app.state.auction_bids.bids_for(auction_id)]
    assert set(shown) < set(book), (
        f"the bid book is not a superset of the shortlist: {book} vs {shown}"
    )


def test_a_store_the_ranking_benched_takes_no_loss_at_all(unwired: None) -> None:
    """A store past the shortlist cap was shown to nobody, so it was passed over by nobody.

    **This is the regression test for the measured defect.** The fold used to read the bid
    book, which ``collected_bid_records`` fills with every ELIGIBLE candidate, and charged a
    loss to stores the shopper never saw. Six eligible, four slots, and both benched stores
    came out of the accept holding ``(alpha=1.0, beta=2.0)``.

    The assertion is the strongest available one: the benched store has NO ENTRY IN THE BOOK
    AT ALL. Anything the fold touched would have created one, so "untouched" needs no
    tolerance and no comparison against a prior. The two halves that keep it honest are here
    too — the store that WAS shown and passed over still takes its beta, and the accepted
    store still takes its alpha — because a fix that simply stopped folding would satisfy the
    benched assertion and destroy the feature.
    """
    app, auction_id, shown, benched = served_auction()
    winner, passed_over = shown[0], shown[1]

    response = served_accept(app, auction_id, winner)
    assert response.status_code == 200, response.text

    posteriors = (app.state.bandit_posteriors.state()).posteriors.get(CLUSTER, {})
    for store in benched:
        assert store not in posteriors, (
            f"{store!r} was benched past the shortlist cap and never shown to this shopper, "
            f"but the accept folded an outcome for it: "
            f"alpha={posteriors[store].alpha} beta={posteriors[store].beta}"
        )

    assert pair(app, passed_over) == (PRIOR[0], PRIOR[1] + 1.0), (
        f"{passed_over!r} filled a slot and was not taken, so it owes exactly one loss: "
        f"{pair(app, passed_over)}"
    )
    assert pair(app, winner) == (PRIOR[0] + 1.0, PRIOR[1]), (
        f"the accepted store {winner!r} did not take its win: {pair(app, winner)}"
    )


def test_the_accepted_store_takes_exactly_one_win_on_the_served_path(unwired: None) -> None:
    """One accept, one win, and it lands on the buyer's own choice and nowhere else.

    Absolute numbers off a cold book, so a fold that recorded the win twice — once directly
    and once through the shown loop — fails here rather than doubling every conversion signal
    invisibly. The other three shown stores are checked to hold exactly one loss each, which
    is what makes "exactly one" a statement about the whole fold rather than about one pair.
    """
    app, auction_id, shown, _benched = served_auction()
    winner = shown[0]

    assert served_accept(app, auction_id, winner).status_code == 200

    assert pair(app, winner) == (PRIOR[0] + 1.0, PRIOR[1]), pair(app, winner)
    for store in shown[1:]:
        assert pair(app, store) == (PRIOR[0], PRIOR[1] + 1.0), (store, pair(app, store))


# =====================================================================================
# what a shortlist that cannot be read must do — which is nothing
# =====================================================================================
def test_an_unreadable_shortlist_folds_nothing_at_all_not_even_the_win(unwired: None) -> None:
    """No stored shortlist, no outcome — and specifically no lone win either.

    ``ShortlistStore`` is capped and TTL'd, so "the shortlist is gone" is a state a live
    deployment reaches on its own, not a fixture contrivance. The exchange then cannot say
    what it put in front of this shopper, and the tempting half-measure — fold the win, skip
    the losses, we know who was accepted — is the same monotonic ratchet as the defect this
    file was opened for, only pointed the other way: one store's alpha rising on every accept
    while no rival's beta ever moves is not a comparison the sampler can learn from.

    So the book stays untouched, and the buyer still gets the permalink they are owed.
    """
    app, _auction_id = wired_app(shortlist_of_two(), shortlist=False)

    response = accept(app)

    assert response.status_code == 200, response.text
    assert response.json()["permalink_url"], "the buyer lost their permalink to a bandit write"
    assert app.state.bandit_posteriors.state() is None, (
        "an accept whose shortlist could not be read folded an outcome anyway: "
        f"{app.state.bandit_posteriors.state()}"
    )


def test_the_accepted_store_is_credited_even_when_no_slot_names_it(unwired: None) -> None:
    """The buyer accepted it, which is proof it was shown, whatever the stored slots say.

    The one case where the shown list and the winner can disagree — a shortlist rewritten,
    truncated or otherwise missing the slot that was taken. The win is read off the checkout
    port's ``accepted`` event and never off the slots, so it survives; the store that IS on
    the shortlist and was not taken still takes its loss.
    """
    app, auction_id = wired_app(shortlist_of_two())
    # A stored shortlist naming only the store that was NOT accepted.
    store_shortlist(app, auction_id, [honest_bid("bid-b", "store-b")])

    assert accept(app, bid_ref="bid-a").status_code == 200

    assert pair(app, "store-a") == (PRIOR[0] + 1.0, PRIOR[1]), (
        f"the accepted store lost its win to a shortlist that did not name it: {pair(app, 'store-a')}"
    )
    assert pair(app, "store-b") == (PRIOR[0], PRIOR[1] + 1.0), pair(app, "store-b")


def test_a_shortlist_that_resolves_to_no_store_folds_nothing_either(unwired: None) -> None:
    """Read, but naming nothing the bid book can resolve — still no lone win.

    The sibling of the unreadable case, and a distinct code path: the shortlist IS there, it
    has slots, and every ``bid_ref`` on it matches no record — which is what a bid book and a
    shortlist store re-wired to two different ``bid_id`` spellings look like from inside the
    fold. The tempting half-measure is the same one and so is the answer: a win whose losses
    could not be named raises the winner's alpha against rivals that were never marked down,
    every accept, forever.
    """
    app, auction_id = wired_app(shortlist_of_two())
    # Same shape, same slot count — only the references are ones the book cannot resolve.
    store_shortlist(
        app,
        auction_id,
        [honest_bid("bid-from-another-auction", "store-a"), honest_bid("bid-elsewhere", "store-b")],
    )

    response = accept(app)

    assert response.status_code == 200, response.text
    assert response.json()["permalink_url"], "the buyer lost their permalink to a bandit write"
    assert app.state.bandit_posteriors.state() is None, (
        "a shortlist naming no resolvable store still folded an outcome: "
        f"{app.state.bandit_posteriors.state()}"
    )
