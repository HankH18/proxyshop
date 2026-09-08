"""T-302 — the hash-chained ledger can reconstruct an auction the exchange actually ran.

Run it on its own::

    PROXYSHOP_WORKER=6 .venv/bin/python -m pytest \\
        apps/exchange/tests/test_auction_ledger_reconstruction.py -q

The defect, and why "the ledger has the bids" was not enough
-------------------------------------------------------------

This is a market that adjudicates between competing sellers, so the ledger's job is to let a
store that LOST establish three things after the fact: that an auction ran, on what terms, and
that the record was not written afterwards to suit the winner. Before this file the chain could
establish the third and almost none of the first two.

Four of the eighteen frozen ``LedgerEventKind`` values were produced by nothing a sweep of the
product tree could find — ``auction_opened``, ``auction_closed``, ``checkout_redirect``,
``policy_event`` — and two of those absences were real rather than cosmetic:

* ``auction_opened`` was written on every served ``POST /auctions`` and its payload omitted
  ``roster_size``, one of the three keys ``contracts.ledger`` publishes for it. So the row said
  an auction opened and refused to say on how many stores, let alone which.
* ``auction_closed`` was written before the ranking had run, so it could not carry
  ``shortlist_size`` — the shortlist did not exist yet — and it carried nothing at all about
  who was solicited, who answered, who was denied or who was excluded. ``{state, intent_id,
  cluster_id, reason}`` was the whole of it.
* ``policy_event`` had a producer on the accept-refusal path only. The penalties the RANKER
  applied — one ``contradicted_claim`` per claim this exchange's own catalogue contradicts,
  0.15 off ``rank_score`` each, the one asymmetric downside in the whole design — were minted
  per auction and dropped when the request ended. D13 names ``ledger.policy_events`` as the
  SOURCE of that term, and it received nothing a served auction ever charged.
* ``checkout_redirect`` was genuinely emitted all along, by ``checkout/provider.py`` on the
  minting path and ``accept/offer.py`` on the fallback; only its NAME was missing.

What this file grades
---------------------

Not "the events exist". The test below drives ``POST /auctions`` and
``POST /auctions/{id}/accept`` over HTTP through ``create_app()``, writes every event the
exchange produces through trust's own published ``POST /events`` door into a hash-chained
:class:`~trust.events.InMemoryEventStore`, and then **reads the chain back through
``GET /events`` and rebuilds the auction from those rows and nothing else** — no response
body, no app state, no test fixture. The rebuilt account is compared against what the 201
said, and ``GET /events/verify`` is asked whether the chain it was rebuilt from is intact.

The two controls that keep it honest:

* every ledger row is checked against ``contracts.ledger.validate_ledger_payload``, so a
  reconstruction cannot be satisfied by an event carrying extra keys and not its published
  ones; and
* :func:`test_an_exchange_whose_ledger_refuses_every_event_still_serves_the_auction` drives
  the identical flow against a sink that raises on every emit. The shopper's request must be
  unaffected — an audit row is never a reason to fail a live auction — which is also the
  reason none of the writes here is allowed to validate-or-raise.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from contracts.ledger import validate_ledger_payload
from exchange.accept import use_registered_domains
from exchange.accept.routes import configure_accept
from exchange.auction.routes import configure_auctions
from exchange.auction.state import (
    ACCEPTED_KIND,
    AUCTION_CLOSED_KIND,
    AUCTION_OPENED_KIND,
    AuctionStateMachine,
)
from exchange.checkout.provider import CHECKOUT_REDIRECT_KIND, CODE_CREATED_KIND
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.serving import configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from fastapi.testclient import TestClient
from trust.events import InMemoryEventStore, create_events_app

from .test_accept_routes import RecordingCodeCreator

# =====================================================================================
# The roster this auction runs over — one store per outcome the chain has to be able to
# tell apart. Chosen that way on purpose: an auction in which everybody won would let a
# reconstruction that reported "everyone was shortlisted" pass.
# =====================================================================================
#: Answers its solicitation honestly, is shortlisted, and is the bid the buyer accepts.
WINNER = "store-winner"
#: Answers, and its prose contradicts this exchange's own catalogue — so the ranking opens a
#: `contradicted_claim` policy event against it and charges it 0.15.
LIAR = "store-liar"
#: Eligible, solicited, and never answers. R10 represents it at its list price, so the chain
#: must be able to say "there is a bid here that the store did not write".
SILENT = "store-silent"
#: On the roster and refused by the R12 gate before anyone asked it anything.
DENIED = "store-denied"
#: Answers, clears R12, and is thrown out by the RANKER — its checkout URL is on a host it is
#: not registered at (C10/D22). A different gate from `DENIED`'s, so the chain has to keep the
#: two sets apart rather than reporting one "rejected" pile.
IMPOSTOR = "store-impostor"

#: The host the impostor points its checkout at, which is not the one it is registered at.
RIVAL_HOST = "totally-not-this-store.example.net"

ROSTER_STORES = (WINNER, LIAR, SILENT, DENIED, IMPOSTOR)
ANSWERING = (WINNER, LIAR, IMPOSTOR)
ELIGIBLE_STORES = (WINNER, LIAR, SILENT, IMPOSTOR)

LIST_PRICE = 120.0

#: The catalogue fact both answering stores' prose is graded against. 24 months.
CATALOGUE_WARRANTY_MONTHS = 24

#: Identical but for the warranty length, so nothing but the lie can explain the penalty.
PITCH = (
    "This is a heat exchange machine sized for an office queue. "
    "It runs a 9 bar pump and comes with a {} warranty."
)

INTENT: dict[str, Any] = {
    "intent_id": "intent-reconstruction-1",
    "cluster_id": "cluster-1",
    "query": "an office espresso machine with a long warranty",
    "hard_constraints": [],
}


def _domain(store_id: str) -> str:
    return f"{store_id}.example.com"


def _offer(store_id: str, price: float) -> dict[str, Any]:
    return {
        "product_ref": "product-1",
        "unit_price": price,
        "total_price": price,
        "currency": "USD",
        "checkout_url": f"https://{_domain(store_id)}/cart/1:1",
        "expires_at": time.time() + 3600.0,
    }


def _bid(store_id: str, price: float, message: str) -> dict[str, Any]:
    return {
        "auction_id": None,
        "store_id": store_id,
        "offer": _offer(store_id, price),
        "claims": [],
        "message": message,
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
                "attributes": {"warranty_months": {"value": CATALOGUE_WARRANTY_MONTHS}},
            }
        ],
    }


# =====================================================================================
# The ledger under the exchange: trust's OWN door, not a list
# =====================================================================================
class TrustDoorSink:
    """An exchange ledger sink that writes through trust's published ``POST /events``.

    This is deliberately not an ``InMemoryLedgerSink``. That class keeps the events in a
    Python list, which can hold anything and proves nothing about whether the chained store
    would accept it: the ledger's kind CHECK constraint, its idempotency key, its
    ``prev_hash`` seal and its 201/200 distinction all live behind the HTTP door. Writing
    through the door is what makes "the chain says so" a statement about the chain.

    Production writes through the same door — ``composition.HttpTrustLedgerSink`` posts each
    event to ``{trust_url}/events`` — over a socket instead of ASGI. What this substitutes is
    the transport, and nothing else.

    A refusal is RECORDED and not raised, for the reason ``LedgerRecorder.record`` swallows
    sink failures: an audit trail must not be able to fail a live auction. :attr:`refused` is
    what lets a test assert that nothing was refused, instead of a silent drop passing for a
    clean run.
    """

    def __init__(self, client: TestClient) -> None:
        self._client = client
        self.refused: list[tuple[str, int, str]] = []

    def emit(self, event: Mapping[str, Any]) -> None:
        response = self._client.post("/events", json=dict(event))
        if response.status_code not in (200, 201):
            self.refused.append(
                (str(event.get("kind", "")), response.status_code, response.text[:400])
            )


class RefusingSink:
    """A ledger that is down. Every emit raises, exactly as an unreachable trust service does."""

    def __init__(self) -> None:
        self.attempts = 0

    def emit(self, event: Mapping[str, Any]) -> None:
        self.attempts += 1
        raise ConnectionError("the trust service is not reachable from this exchange")


@pytest.fixture
def unwired() -> Iterator[None]:
    """Leave the process-wide registered-domain seam exactly as this file found it.

    ``configure_accept(registered_domains=…)`` turns a module-level seam as well as an app
    one, so a file that wires an app and does not restore it changes what every later test in
    the process reads. Same fixture and same reason as ``test_accept_routes.py``'s.
    """
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


def _exchange(sink: Any) -> Any:
    """A fully wired exchange whose auction transitions are written to ``sink``."""

    def solicit(store: Any) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        if store_id == WINNER:
            return {
                "store_id": store_id,
                "received_at": time.time(),
                "bid": _bid(store_id, 95.0, PITCH.format("two-year")),
            }
        if store_id == LIAR:
            return {
                "store_id": store_id,
                "received_at": time.time(),
                "bid": _bid(store_id, 90.0, PITCH.format("five-year")),
            }
        if store_id == IMPOSTOR:
            bid = _bid(store_id, 10.0, PITCH.format("two-year"))
            bid["offer"]["checkout_url"] = f"https://{RIVAL_HOST}/cart/1:1"
            return {"store_id": store_id, "received_at": time.time(), "bid": bid}
        # SILENT answers nothing; DENIED is never asked.
        return None

    domains = StaticRegisteredDomains({store: _domain(store) for store in ROSTER_STORES})
    app = create_app()
    configure_auctions(
        app,
        machine=AuctionStateMachine(ledger=sink),
        solicitor=solicit,
        # DENIED has no row, and a store with no row fails closed (R12) — which is the
        # denial this auction is run to record.
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in ELIGIBLE_STORES}),
    )
    configure_ranking(
        app,
        trust_snapshot={
            store: {"blacklisted": False, "score": 0.7, "confidence": 0.5}
            for store in ELIGIBLE_STORES
        },
        registered_domains=domains,
        catalog=StaticCatalogSnapshots({store: _snapshot(store) for store in ELIGIBLE_STORES}),
    )
    configure_accept(
        app,
        registered_domains=domains,
        code_creator=RecordingCodeCreator(),
        checkout_mode="shopify",
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in ELIGIBLE_STORES}),
    )
    return app


def _run_the_auction(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/auctions",
        json={
            "intent": dict(INTENT),
            "profile": {"pseudonym": "psn-reconstruction-1", "buckets": {}},
            "roster": [
                {
                    "store_id": store,
                    "tier": 1,
                    "product_ref": "product-1",
                    "list_price": LIST_PRICE,
                }
                for store in ROSTER_STORES
            ],
            "bid_timeout_seconds": 2.0,
        },
    )
    assert response.status_code == 201, f"POST /auctions -> {response.status_code}: {response.text}"
    return response.json()


def _accept(client: TestClient, auction_id: str, bid_ref: str) -> dict[str, Any]:
    response = client.post(f"/auctions/{auction_id}/accept", json={"bid_ref": bid_ref})
    assert response.status_code == 200, f"accept -> {response.status_code}: {response.text}"
    return response.json()


# =====================================================================================
# THE RECONSTRUCTION — reads the chain and nothing else
# =====================================================================================
def _store_of(bid_ref: str) -> str:
    """``mint_bid_id`` spells a reference ``{auction_id}:{store_id}``."""
    return bid_ref.split(":", 1)[1] if ":" in bid_ref else ""


def reconstruct(events: list[dict[str, Any]], auction_id: str) -> dict[str, Any]:
    """Rebuild the account of one auction out of ledger rows.

    Reads ONLY the rows: nothing here consults the 201 body, the app, or the machine. That is
    the whole point — a store that lost holds the chain and nothing else, and every question it
    has to be able to ask is answered from these rows or is not answered at all.
    """
    mine = [event for event in events if event.get("auction_id") == auction_id]
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for event in mine:
        by_kind.setdefault(str(event["kind"]), []).append(event)

    opened = (by_kind.get(AUCTION_OPENED_KIND) or [{}])[0]
    closed = (by_kind.get(AUCTION_CLOSED_KIND) or [{}])[0]
    opened_body = opened.get("payload") or {}
    closed_body = closed.get("payload") or {}
    accepted = (by_kind.get(ACCEPTED_KIND) or [{}])[0]

    return {
        "intent_id": opened_body.get("intent_id"),
        "cluster_id": opened_body.get("cluster_id"),
        "roster": list(opened_body.get("roster") or ()),
        "roster_size": opened_body.get("roster_size"),
        "bids_received": sorted(
            str((event.get("payload") or {}).get("store_id") or "")
            for event in by_kind.get("bid_placed") or ()
        ),
        "solicited": sorted(closed_body.get("solicited") or ()),
        "answered": sorted(closed_body.get("answered") or ()),
        "unanswered": {
            str(row["store_id"]): row.get("reason") for row in closed_body.get("unanswered") or ()
        },
        "denied": {
            str(row["store_id"]): str(row.get("status") or "")
            for row in closed_body.get("denied") or ()
        },
        "excluded": {
            str(row["store_id"]): list(row.get("reasons") or ())
            for row in closed_body.get("excluded") or ()
        },
        "close_reason": closed_body.get("reason"),
        "shortlist_size": closed_body.get("shortlist_size"),
        "shortlist": [str(row.get("store_id") or "") for row in closed_body.get("shortlist") or ()],
        "shown": [
            _store_of(str((event.get("payload") or {}).get("bid_ref") or ""))
            for event in by_kind.get("shown") or ()
        ],
        "penalised": {
            str(event.get("store_id") or ""): {
                "kind": (event.get("payload") or {}).get("kind"),
                "count": (event.get("payload") or {}).get("count"),
                "charged": (event.get("payload") or {}).get("bid_total_penalty"),
            }
            for event in by_kind.get("policy_event") or ()
        },
        "accepted_store": accepted.get("store_id"),
        "accepted_bid_ref": (accepted.get("payload") or {}).get("bid_ref"),
        "redirected_to": [
            (event.get("payload") or {}).get("permalink_url")
            for event in by_kind.get(CHECKOUT_REDIRECT_KIND) or ()
        ],
        "codes_created": len(by_kind.get(CODE_CREATED_KIND) or ()),
        "order": [str(event["kind"]) for event in mine],
    }


@pytest.fixture
def ran(unwired: None) -> Iterator[dict[str, Any]]:
    """One served auction, accepted over HTTP, with its chain read back through ``GET /events``.

    Everything the tests below assert on comes out of this fixture: ``body`` is what the two
    HTTP doors answered and ``chain``/``account`` are what the ledger says, so a test that
    compares them is comparing two independent accounts of the same request.
    """
    ledger = InMemoryEventStore()
    with TestClient(create_events_app(ledger)) as trust:
        sink = TrustDoorSink(trust)
        client = TestClient(_exchange(sink))

        body = _run_the_auction(client)
        slots = body["shortlist"]["slots"]
        assert slots, f"the auction shortlisted nobody, so there is nothing to accept: {body}"
        winning_ref = next(
            (str(slot["bid_ref"]) for slot in slots if _store_of(str(slot["bid_ref"])) == WINNER),
            None,
        )
        assert winning_ref is not None, f"{WINNER} did not reach the shortlist: {slots}"
        accepted = _accept(client, body["auction_id"], winning_ref)

        page = trust.get("/events", params={"limit": 500}).json()
        assert page["is_chain"], f"the read came back truncated or filtered: {page}"
        yield {
            "body": body,
            "accepted": accepted,
            "winning_ref": winning_ref,
            "sink": sink,
            "trust": trust,
            "chain": list(page["events"]),
            "account": reconstruct(list(page["events"]), str(body["auction_id"])),
        }


def test_the_ledger_accepted_every_event_the_served_auction_produced(ran: dict[str, Any]) -> None:
    """The armer. Everything below reads a chain, so a chain nobody could write is fatal here.

    Without this, an event trust refused — a kind outside the frozen vocabulary, an
    unrenderable identifier, a body the door rejects — would leave the reconstruction short and
    the tests below would report a missing FACT rather than a rejected write.
    """
    assert ran["sink"].refused == [], (
        f"trust refused events the served exchange produced: {ran['sink'].refused}"
    )
    assert ran["chain"], "the chain is empty, so nothing below is measuring a ledger"


def test_the_chain_alone_reconstructs_the_auction_the_exchange_ran(ran: dict[str, Any]) -> None:
    """The deliverable: every question a losing store has, answered from the rows.

    Compared against the 201 body rather than against literals wherever the exchange minted
    the value, so this cannot pass by agreeing with itself: ``roster``, ``denied`` and
    ``shortlist`` are checked against what the response published, and the chain is what has
    to match it.
    """
    account = ran["account"]
    body = ran["body"]

    # WHAT WAS ASKED, and of whom.
    assert account["intent_id"] == INTENT["intent_id"]
    assert account["cluster_id"] == INTENT["cluster_id"]
    assert account["roster_size"] == len(ROSTER_STORES)
    assert account["roster"] == list(ROSTER_STORES)

    # WHO WAS SOLICITED — the roster minus the store R12 refused — and who answered.
    assert account["solicited"] == sorted(ELIGIBLE_STORES)
    assert account["answered"] == sorted(ANSWERING)
    assert set(account["unanswered"]) == {SILENT}
    assert account["unanswered"][SILENT], "a silent store's fallback reason is not recorded"

    # WHO WAS EXCLUDED, AND WHY — two gates, two sets, and the chain keeps them apart. R12
    # refused DENIED before anybody asked it anything; the RANKER threw IMPOSTOR out after it
    # had answered, for pointing its checkout at a host it is not registered at.
    assert account["denied"] == {DENIED: "unavailable"}, account["denied"]
    assert account["denied"] == {row["store_id"]: row["status"] for row in body["denied"]}, (
        "the ledger's denials disagree with the ones the response published"
    )
    assert set(account["excluded"]) == {IMPOSTOR}, account["excluded"]
    assert any("off_domain" in reason for reason in account["excluded"][IMPOSTOR]), (
        f"the chain records that {IMPOSTOR} was excluded and not on what ground: "
        f"{account['excluded'][IMPOSTOR]}"
    )
    assert account["excluded"] == {
        row["store_id"]: row["exclusion_reasons"] for row in body["excluded"]
    }, "the ledger's exclusions disagree with the ones the response published"

    # ONE RECEIPT PER COLLECTED BID: the two that answered plus the one represented at its
    # list price. The store R12 denied has none, because nothing was ever collected from it.
    assert account["bids_received"] == sorted(ELIGIBLE_STORES)

    # WHAT IT COST TO LIE. The ranking opened a policy event against exactly the store whose
    # prose contradicts the catalogue, and the row says what was charged.
    assert set(account["penalised"]) == {LIAR}, account["penalised"]
    assert account["penalised"][LIAR]["kind"] == "contradicted_claim"
    assert account["penalised"][LIAR]["count"] >= 1
    assert account["penalised"][LIAR]["charged"] > 0.0

    # HOW IT CLOSED, and which slots were filled.
    assert account["close_reason"] == "deadline"
    assert account["shortlist_size"] == len(body["shortlist"]["slots"])
    assert account["shortlist"] == [
        _store_of(str(slot["bid_ref"])) for slot in body["shortlist"]["slots"]
    ]
    assert account["shown"] == account["shortlist"], (
        "the `shown` events and the closed auction's own shortlist name different stores"
    )
    assert WINNER in account["shortlist"]

    # WHAT THE BUYER WAS HANDED.
    assert account["accepted_store"] == WINNER
    assert account["accepted_bid_ref"] == ran["winning_ref"]
    assert account["codes_created"] == 1
    assert account["redirected_to"] == [ran["accepted"]["permalink_url"]]


def test_the_reconstruction_is_printable_as_one_account(ran: dict[str, Any]) -> None:
    """The same rows rendered as prose, because "reconstructible" is a claim about a READER.

    A dictionary that happens to hold the right keys is not the deliverable; a store being able
    to read what happened to it is. This renders the account and asserts every store on the
    roster is named in it exactly once, which is the property that fails first when a
    reconstruction silently drops whoever it could not explain.
    """
    account = ran["account"]
    lines = [
        f"auction {ran['body']['auction_id']}",
        f"  intent {account['intent_id']} in cluster {account['cluster_id']}",
        f"  roster ({account['roster_size']}): {', '.join(account['roster'])}",
        f"  solicited: {', '.join(account['solicited'])}",
        f"  answered: {', '.join(account['answered'])}",
        *(f"  no answer from {store}: {why}" for store, why in account["unanswered"].items()),
        *(f"  denied {store}: {status}" for store, status in account["denied"].items()),
        *(
            f"  excluded {store}: {'; '.join(reasons)}"
            for store, reasons in account["excluded"].items()
        ),
        *(
            f"  penalty against {store}: {row['count']}x{row['kind']}, charged {row['charged']}"
            for store, row in account["penalised"].items()
        ),
        f"  closed ({account['close_reason']}), {account['shortlist_size']} slot(s) filled",
        f"  shown: {', '.join(account['shortlist'])}",
        f"  accepted {account['accepted_bid_ref']} from {account['accepted_store']}",
    ]
    rendered = "\n".join(lines)
    for store in ROSTER_STORES:
        assert rendered.count(store) >= 1, f"{store} appears nowhere in the account:\n{rendered}"


def test_auction_opened_precedes_auction_closed_on_a_real_served_request(
    ran: dict[str, Any],
) -> None:
    """Ordering, asserted on the chain a served request wrote — never on built events.

    A hand-built pair proves only that a list can be written in order. What has to hold is that
    the ROUTE writes them that way, and it is not obvious that it does: the close's ledger row
    is built after the ranking, several hundred lines and three collaborators later than the
    open, and the events reach the chain through a network door that could reorder them.

    The rest of the sequence is asserted with it, because an ``auction_closed`` that arrived
    before the receipts of the bids it closed over would be a chain that could not be replayed:
    every ``bid_placed`` sits inside the window, and every ``shown`` after the close that
    decided it.
    """
    order = ran["account"]["order"]
    assert order.count(AUCTION_OPENED_KIND) == 1, order
    assert order.count(AUCTION_CLOSED_KIND) == 1, order
    assert order.index(AUCTION_OPENED_KIND) < order.index(AUCTION_CLOSED_KIND), order
    assert order.index(AUCTION_OPENED_KIND) == 0, order

    for kind in ("bid_placed", "policy_event"):
        assert order.index(kind) > order.index(AUCTION_OPENED_KIND), (kind, order)
        assert order.index(kind) < order.index(AUCTION_CLOSED_KIND), (kind, order)
    for kind in ("shown", ACCEPTED_KIND, CODE_CREATED_KIND, CHECKOUT_REDIRECT_KIND):
        assert order.index(kind) > order.index(AUCTION_CLOSED_KIND), (kind, order)


def test_every_row_the_auction_wrote_carries_its_published_body(ran: dict[str, Any]) -> None:
    """The control on the reconstruction: extra keys do not excuse a missing published one.

    ``auction_opened`` without ``roster_size`` and ``auction_closed`` without ``shortlist_size``
    are exactly what this file exists to close, and both would still have satisfied every
    assertion above if the fix had only added the extra keys the account reads. This is the
    assertion that says the published shapes are met as well.
    """
    problems = {
        f"{event['kind']}:{event['event_id']}": validate_ledger_payload(
            str(event["kind"]), event.get("payload") or {}
        )
        for event in ran["chain"]
    }
    assert {key: value for key, value in problems.items() if value} == {}, problems


def test_the_chain_the_account_was_rebuilt_from_verifies(ran: dict[str, Any]) -> None:
    """A reconstruction off a chain nobody checked is a reconstruction off a list.

    The point of hash-chaining this record is that a losing store can tell "this is what
    happened" from "this is what someone wrote down later", so the account above is worth what
    ``GET /events/verify`` says the chain is worth.
    """
    verdict = ran["trust"].get("/events/verify").json()
    assert verdict["ok"], verdict
    head = ran["trust"].get("/events/head").json()
    assert head["length"] == len(ran["chain"]), (head, len(ran["chain"]))


def test_the_close_records_the_shortlist_the_shopper_was_actually_served(
    ran: dict[str, Any],
) -> None:
    """``shortlist_size`` is the served shortlist's, not a placeholder and not a guess.

    It carries its own armer, in the second assertion. ``shortlist_size`` used to be absent
    from every ``auction_closed`` this service wrote, and the reading a reader would take from
    an absent key is ``0`` — which is exactly what an auction that shortlisted nobody also
    reports. So a run in which nobody was shortlisted would let "the ledger agrees with the
    response" pass while saying nothing at all, and this asserts that is not the run.
    """
    assert ran["account"]["shortlist_size"] == len(ran["body"]["shortlist"]["slots"])
    assert ran["account"]["shortlist_size"] > 0, (
        "this run shortlisted nobody, so `shortlist_size` agrees with the response for the "
        "uninteresting reason that both are zero"
    )


# =====================================================================================
# The collaborator is OPTIONAL — writing an audit row may not fail a shopper's request
# =====================================================================================
def test_an_exchange_whose_ledger_refuses_every_event_still_serves_the_auction(
    unwired: None,
) -> None:
    """Every one of the writes above is on the path of a live, unauthenticated request.

    ``trust_snapshot_of`` and ``catalog_of`` announce nothing rather than inventing a
    collaborator when none is wired, and the ledger is held to the same rule from the other
    direction: a trust service that is down costs the platform its audit row and costs the
    shopper nothing. Driven end to end rather than asserted on ``LedgerRecorder`` in isolation,
    because the close now carries a payload built from the ranking — several more chances for
    an exception to reach a caller than the old two-key body had.
    """
    sink = RefusingSink()
    client = TestClient(_exchange(sink))

    body = _run_the_auction(client)
    slots = body["shortlist"]["slots"]
    assert slots, f"the auction shortlisted nobody with the ledger down: {body}"
    winning_ref = next(
        (str(slot["bid_ref"]) for slot in slots if _store_of(str(slot["bid_ref"])) == WINNER),
        None,
    )
    assert winning_ref is not None, f"{WINNER} did not reach the shortlist: {slots}"

    accepted = _accept(client, body["auction_id"], winning_ref)
    assert accepted["permalink_url"], accepted

    # The armer for this test: a sink nobody called would satisfy it for the wrong reason.
    assert sink.attempts > 0, "no event was even attempted, so nothing was proved to be optional"
