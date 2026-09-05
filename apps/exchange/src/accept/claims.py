"""The acceptance claim — one auction, one mint, taken *before* the merchant is asked (T-158).

The one-accept guard this module replaces read a field off the auction object it was handed
and wrote it back to that same object. Nothing durable and nothing serialised sat between the
read and the write, so two concurrent ``POST /auctions/{id}/accept`` both passed it. Measured
through the real served route, on a store wrapper giving ``load``/``save`` a 2 ms round trip —
which is what :class:`~apps.exchange.src.auction.state.RedisAuctionStore` actually is, two
separate network calls — 3 runs out of 3 answered ``HTTP 200`` twice::

    req1: HTTP 200  code PSX-LIVE-0001
    req2: HTTP 200  code PSX-LIVE-0002
    merchant POST /codes calls : 2
    live single-use codes minted: ['PSX-LIVE-0001', 'PSX-LIVE-0002']

Two live single-use discount codes for one purchase, and the second one is not the buyer's
problem to give back — it is the seller's money.

Where the guard had to move to, and why "after" is not good enough
------------------------------------------------------------------

The ticket text said to put the guard on the state machine's ``ACCEPTED`` transition, "which
is already serialised". It was not serialised — see
:mod:`apps.exchange.src.auction.state`'s docstring for the measurement — and T-158 is
therefore two repairs, not one:

1. that transition is now genuinely atomic, through :meth:`AuctionStore.reserve`; and
2. **the claim is taken before the mint**, which is what this module is.

The second one is the money fix and the first one alone would not have been. Even with a
perfectly atomic transition, the route's order was *check, mint, transition* — so the loser of
the race lost it only after ``POST /codes`` had already issued a live discount. It would have
answered 409 while the second code sat in the merchant's account: a correct HTTP status over a
double spend. The reproduction shows exactly that outcome (``req2: HTTP 409`` with
``merchant POST /codes calls: 2``) and it is still a defect.

The three implementations, and which one guarantees what
--------------------------------------------------------

:class:`StoreAcceptanceClaims`
    the real one, and what :func:`~.routes.configure_accept` wires. It puts the claim in the
    same durable store the auction record lives in, through that store's atomic
    :meth:`~apps.exchange.src.auction.state.AuctionStore.reserve`. Against
    ``RedisAuctionStore`` that is one ``SET key value NX EX``, so the guarantee reaches across
    processes, across app instances and across hosts — which is the only kind of guarantee
    worth having here, because the exchange is served by more than one worker.

:class:`InMemoryAcceptanceClaims`
    thread-safe, **process-local**, and offered for a single-process deployment or a test that
    wants the port's semantics without a store. It is deliberately **not a default** — see
    "why there is no default" below — and the served route never uses it.

Anything you inject
    the port is two methods. A deployment with its own idempotency table — a unique index on
    ``(auction_id)`` in Postgres is the same constraint by another name — passes it to
    :func:`~.routes.configure_accept` and this module gets out of the way.

Why there is no default, and what an unwired ``accept()`` still promises
------------------------------------------------------------------------

Nothing is in force unless a caller put it there. :func:`~.offer.accept` with no claim table
falls back to the object-scoped ``accepted_bid_ref`` stamp alone — the pre-T-158 guarantee,
which refuses a second accept on the same auction *object* and nothing more. Every served
accept has a table (:func:`~.routes.accept_bid` opens a scope over its own app's store on every
request), so this fallback is what a *direct* call gets: the simulator and the e2e flow, which
mint against recording doubles rather than a merchant.

The tempting default here is a process-lifetime table, and it is wrong for a reason that is
measured rather than aesthetic. An auction id in production is ``auction-{uuid4()}`` and is
never seen twice; an auction id in a test suite is ``auction-1`` and names a different auction
in dozens of tests inside one process. Defaulting to a process-lifetime table makes the second
test to touch ``auction-1`` fail as "already accepted", and that is not a hypothetical: with
one wired in, the frozen acceptance suite went from ``120 passed`` to ``5 failed, 115 passed``
— ``test_redirect_and_shopify_modes_emit_identical_event_kinds``,
``test_double_accept_on_one_auction_is_rejected``,
``test_accept_gate_refuses_a_bid_whose_store_became_ineligible_after_bidding``,
``test_both_eligibility_gates_require_a_versioned_seller_eligibility_interface`` and
``test_spec_criteria.py::test_offdomain_checkout_url_is_refused`` — each of them a legitimate
first accept refused by a *previous test's* claim.

It is also the wrong shape on its own merits, and the ticket's own reproduction says why: an
in-process double cannot tell process memory from durable state, so a process-lifetime ledger
turns an in-process test green while the second uvicorn worker mints the second code. The guard
has to live where the auction lives. That is :class:`StoreAcceptanceClaims`, and it is what
wiring an app gets you without asking.

**What this costs, stated exactly rather than overstated.** ``test_repro_open_tickets.py``'s
``test_t158_a_second_accept_on_a_reloaded_auction_record_mints_no_second_code`` calls bare
``accept()`` with nothing wired and requires the second call on one auction id to be refused,
so it stays red. That gate is not *unsatisfiable* alongside the frozen suite — it was made
green simultaneously with all 120 by keying the process-lifetime table on "did the caller pass
a real ``registered_domains``", which happens to separate the two suites. It is not shipped
because ``registered_domains`` answers "is this checkout URL on the store's registered host"
and carries no information about whether this auction was already accepted; because it would
let any caller disable the money guard by omitting one unrelated argument, and the caller who
omits it is already the *less* verified one (T-169); and because it depends on process-global
wiring nothing at the call site controls — arm that rule and also call
``use_registered_domains(<real registry>)`` in the same process and five frozen goals go red.
The honest statement is that the gate is satisfiable only by keying a money guard on an
unrelated argument and on global wiring state, which is worse than leaving it red.

Why a *claim* and not a lock
-----------------------------

Nothing here blocks, nothing is held for a duration, and there is nothing to time out. A
claim is won or it is not, and the loser is told who won. The only lifetime involved is the
auction's own fifteen-minute TTL budget, which the reservation shares — see
:data:`~apps.exchange.src.auction.state.RESERVATION_KEY_TEMPLATE` on why a reservation in fact
expires slightly *earlier* than its record — so a crashed process cannot wedge an auction for
longer than the auction was going to live anyway.

:meth:`AcceptanceClaims.release` exists because A5 says a refused accept re-offers the next
slot rather than ending the auction. A mint that fails gives the claim back; a mint that
succeeded keeps it, which is what "one auction, one code" means — and "succeeded" is decided
by whether a code EXISTS, not by whether this accept returned one. A refusal carrying
:attr:`~apps.exchange.src.checkout.provider.OrphanedCheckoutCode.orphan` is a refusal that
happened after ``POST /codes`` already issued a live discount, so it keeps the claim: A5's
argument for re-offering the next slot does not extend to re-minting against an auction that
already cost the seller a code. (On ``main`` that release was unconditional and a second
sequential accept on an orphaned auction minted a second live code — byte-identical on both
trees, so this is a repair rather than a regression, but it is a repair.)
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "ACCEPTANCE_RESERVATION",
    "AcceptanceClaims",
    "ClaimOutcome",
    "InMemoryAcceptanceClaims",
    "StoreAcceptanceClaims",
    "acceptance_claims_scope",
    "platform_acceptance_claims",
    "use_acceptance_claims",
]

#: The reservation name an acceptance claim takes on the auction store. It is deliberately
#: NOT ``exit:closed`` — the state machine's own reservation — because the claim is taken
#: before the mint and the transition after it, and two different moments must not compete
#: for one name or the second would refuse the first's own follow-up.
ACCEPTANCE_RESERVATION = "accept"


@dataclass(frozen=True)
class ClaimOutcome:
    """Did this caller get the auction, and if not, who has it?

    ``holder`` is the ``bid_ref`` the winner claimed with. It is carried so the refusal can
    say *which* bid the auction went to rather than only that it went — the difference
    between a buyer who double-clicked (same ref: their own code is already on its way) and a
    buyer whose auction was accepted on somebody else's bid.
    """

    won: bool
    holder: str | None = None


class AcceptanceClaims(Protocol):
    """At most one caller may ever be told it won ``auction_id``."""

    def claim(self, auction_id: str, bid_ref: str) -> ClaimOutcome: ...

    def release(self, auction_id: str, bid_ref: str) -> None: ...


class StoreAcceptanceClaims:
    """The durable claim: the auction's own store holds it, atomically.

    ``store`` is an :class:`~apps.exchange.src.auction.state.AuctionStore` — anything with
    the ``reserve``/``release`` pair. Nothing about the auction *record* is read or written
    here; the claim is a constraint that lives beside the record under the same key prefix
    and the same TTL, so it cannot outlive the auction it protects and cannot lapse while
    that auction is still live.
    """

    def __init__(self, store: Any) -> None:
        reserve = getattr(store, "reserve", None)
        release = getattr(store, "release", None)
        if not callable(reserve) or not callable(release):
            raise TypeError(
                f"{type(store).__name__} cannot hold an acceptance claim: it exposes no "
                f"atomic reserve(auction_id, name, token)/release(...) pair, so two "
                f"concurrent accepts on one auction could both be told they won"
            )
        self._store = store

    def claim(self, auction_id: str, bid_ref: str) -> ClaimOutcome:
        held = self._store.reserve(str(auction_id), ACCEPTANCE_RESERVATION, str(bid_ref))
        if held is None:
            return ClaimOutcome(won=True, holder=str(bid_ref))
        return ClaimOutcome(won=False, holder=str(held))

    def release(self, auction_id: str, bid_ref: str) -> None:
        self._store.release(str(auction_id), ACCEPTANCE_RESERVATION, str(bid_ref))


class InMemoryAcceptanceClaims:
    """Thread-safe, process-local, and **not** a cross-process guarantee. See the docstring.

    Two OS processes each hold their own copy of this dict and therefore agree on nothing, so
    this is the right table only where "the process" and "the deployment" are the same thing.
    It is never a default and never what the served route runs on; wire it deliberately or not
    at all.
    """

    def __init__(self) -> None:
        self._held: dict[str, str] = {}
        self._lock = threading.Lock()

    def claim(self, auction_id: str, bid_ref: str) -> ClaimOutcome:
        with self._lock:
            held = self._held.get(str(auction_id))
            if held is not None:
                return ClaimOutcome(won=False, holder=held)
            self._held[str(auction_id)] = str(bid_ref)
            return ClaimOutcome(won=True, holder=str(bid_ref))

    def release(self, auction_id: str, bid_ref: str) -> None:
        with self._lock:
            if self._held.get(str(auction_id)) == str(bid_ref):
                del self._held[str(auction_id)]

    def holder(self, auction_id: str) -> str | None:
        with self._lock:
            return self._held.get(str(auction_id))

    def reset(self) -> None:
        with self._lock:
            self._held.clear()


#: The deployment's acceptance-claim table, wired once at application configuration, exactly
#: as `_platform_domains` is and for the same reason: a port every call site must remember to
#: pass is a port some call site will forget.
_platform_claims: Any | None = None

#: The claim table for the request being served, and how the route reaches
#: :func:`~.offer.accept` **without widening** :func:`~.gate.accept_offer`'s signature.
#:
#: Two reasons it is a context variable rather than a fourth injected parameter on that
#: function, and neither is convenience:
#:
#: * ``accept_offer``'s published parameter list is *contractually* partitioned — data
#:   parameters and injected collaborators, each collaborator driven by a hostile-input sweep
#:   (``test_accept_denials.py::test_the_injected_collaborator_leak_sweep_is_armed``). Adding a
#:   collaborator there without a sweep of its own is exactly the drift that gate exists to
#:   catch, and the claim table is not a value a *client* can influence — it is a wiring
#:   decision the route makes about itself.
#: * a module global would be wrong for the opposite reason: two apps in one process (which is
#:   every test session) would fight over one slot, and a request to one app could take its
#:   claim in the other app's store.
#:
#: A context variable is the exact scope of the thing: one request, one app, one store. The
#: repo has the precedent — ``checkout/codes.py``'s ``minting_ledger`` is the same choice for
#: the same reason.
_request_claims: ContextVar[Any | None] = ContextVar("exchange_acceptance_claims", default=None)


@contextmanager
def acceptance_claims_scope(claims: Any | None) -> Iterator[None]:
    """Bind ``claims`` for everything called inside this block, on this task only."""
    token = _request_claims.set(claims)
    try:
        yield
    finally:
        _request_claims.reset(token)


def use_acceptance_claims(source: Any | None) -> Any | None:
    """Wire the acceptance-claim table for this process; return the previous one.

    .. warning::
       This module is reachable under **two** dotted names, and module state does not cross
       that boundary by itself — see :mod:`._spellings`. ``accept/__init__.py`` binds both
       spellings to one module object, which is what makes this global single-valued; wire
       through ``exchange.accept``, the spelling :mod:`exchange.main` builds the app from.
    """
    global _platform_claims
    previous = _platform_claims
    _platform_claims = source
    return previous


def platform_acceptance_claims() -> Any | None:
    """The claim table in force: this request's, else this process's wiring, else ``None``.

    Narrowest scope first. ``None`` — nothing in force — is a real and deliberate answer, not
    a missing default; see the module docstring on why a process-lifetime fallback is the one
    shape this guard must not have.
    """
    scoped = _request_claims.get()
    if scoped is not None:
        return scoped
    return _platform_claims
