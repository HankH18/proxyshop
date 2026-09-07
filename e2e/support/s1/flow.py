"""The scripted S1 starting-path run (T-082): one pass through the real components.

This module *drives*; ``e2e/test_s1_flow.py`` *asserts*. Keeping them apart is what makes
the assertions readable as the acceptance criteria they are, and it keeps the one expensive
thing — the run — behind a single session fixture.

What is real, stage by stage
----------------------------
Every stage below calls the product's own entry point. Nothing here re-implements a stage,
and nothing here is a stand-in for a component that exists.

======================================  ==========================================================
buyer intent -> ≤3 clarifications       ``buyer_svc.intent.clarify`` with the offline LLM double
buyer confirms -> auction created       ``buyer_svc.intent.confirm`` -> ``POST /auctions`` on the
                                        real exchange ASGI app (``exchange.main.create_app``)
auction opened / roster gated / closed  ``exchange.auction.AuctionStateMachine`` +
                                        ``exchange.orchestration.solicit_bids``, through the route
store agents bid                        ``store_agent.runtime.bid`` — the real hosted agent, once
                                        per hosted store, answering the real ``BidRequest``
a silent store is represented           ``exchange.auction.collect_bids``' R10 list-price fallback,
                                        manufactured by the exchange because the store said nothing
claims verified                         ``exchange.ranking.verification`` inside the route —
                                        ``claim_verification.verify`` against the catalogue
                                        snapshot the EXCHANGE was wired with, attested with a
                                        MAC, and announced as ``claim_verified``
ranking + shortlist                     ``exchange.ranking.serving.rank_auction`` inside the
                                        route -> the served ``shortlist.slots``, read back
                                        through ``GET /auctions/{auction_id}/shortlist``
acceptance + code + redirect            ``exchange.accept.accept`` in ``CHECKOUT_MODE=redirect``,
                                        i.e. ``SimulatedRedirectProvider``, with the registered-
                                        domain guard armed
checkout at the merchant                ``shopify_stub`` — the real service, served in-process on
                                        loopback (D41), driven over HTTP by ``StubClient``
pixel                                   the SERVED ``POST /pixel/collect`` on a real
                                        ``merchant_svc.main.create_app``, which the stub's web
                                        pixel really beacons to, and whose route publishes the
                                        ``checkout_pixel`` row itself
webhook                                 ``merchant_svc.install.webhooks.handle_delivery`` over the
                                        bytes the stub really delivered, HMAC verified
reconciliation                          ``trust.reconcile.reconcile``
trust projection                        ``trust.scoring.score`` + ``trust.snapshot.build_snapshot``
======================================  ==========================================================

What this module used to do instead, and no longer does
------------------------------------------------------
This driver used to carry a SECOND IMPLEMENTATION of the auction's own middle, and the S1
suite was green because of it rather than in spite of it. It built the ranker's candidate
list itself (``_candidates``), ran ``claim_verification.verify`` itself over a pitch and a
snapshot it held privately, called ``rank()`` itself, and emitted ``bid_placed``, ``shown``
and ``claim_verified`` itself. Meanwhile the SERVED ``POST /auctions`` it had just driven
answered ``ranked: 0, excluded: 3, shortlist.slots: 0`` — every candidate refused
``blacklist_unreadable`` and ``off_domain_checkout``, because this module computed the
catalogue, the trust snapshot and the domain registry and then never bound any of them into
the app. Roughly 150 lines that made a spine which served nobody look healthy.

All of it is gone. The collaborators are bound with ``configure_ranking`` and
``configure_auctions``; the exchange verifies, ranks, shortlists and writes its own ledger
legs; and what is left here READS what the served request produced — the response body, the
published ``GET /auctions/{auction_id}/shortlist``, the exchange's own bid book, and the
``claim_verified`` events its ranker wrote.

What this module emits itself: nothing. And how the last one was found
----------------------------------------------------------------------
This docstring used to say that ONE ledger kind in the S1 chain had **no production emitter
anywhere in the tree** — ``checkout_pixel`` — on the grounds that ``pixel/src/`` held a real
Web Pixel extension but nothing on a served path turned its beacon into a ledger event, and
``merchant_svc.collector`` stopped at a ``PixelObservation``. So the run called
``accept_pixel_event`` on the stub's beacon body and built the row itself.

**That was false, and had been for some time.** The emitter is
``merchant_svc.composition.publish_pixel_observation``, called by
``collector/routes.collect_pixel_event`` — a served ``POST /pixel/collect``, mounted by the
frozen ``merchant_svc.main.create_app`` at ``install.config.COLLECTOR_PATH``, which is the
same constant the install writes into the web pixel's ``collectorUrl``. It appends a real
``checkout_pixel`` row to the chained ledger. The gap this file described had been closed and
this file went on describing it.

**The part worth keeping is why nothing went red.** The old docstring's own safety argument
was that "the exact multiset turns red the moment a production emitter lands and starts
double-counting", and ``e2e/test_s1_flow.py`` restated it as a test. Both were satisfied and
both were empty: the driver called the collector's *library function* and wrote the row
itself, so the served route was never invoked and there was never a second writer to
double-count. A multiset over what a run produced cannot see a producer the run does not
drive. (The AST emitter search in ``test_s1_flow.py`` was blind too, for a second, independent
reason — it only recognised a dict literal that was the argument of an ``append(...)``, and
this emitter *returns* the dict and publishes it on the next line. Two gates, aimed at the
same claim, both vacuous.)

So the run now stands up a real merchant on loopback and lets the stub's web pixel beacon at
it, and this module builds no ledger event for the pixel at all. What was already true stays
true: the beacon is never faked — the stub really posts it — and what changed is that the
ledger *write* is the merchant's, not this file's.
``test_the_pixel_row_was_written_by_the_served_collector_and_not_by_the_run`` holds it there
by recomputing the row's DERIVED event id (``composition.pixel_ledger_event`` digests the
projected body; this module's only builder mints a ``uuid4``), which is a claim about
provenance that no count could have made.

The two other seams this docstring used to list, re-measured against HEAD rather than repeated:

* **the exchange's ``checkout_token`` and the merchant's are unrelated values** — still true,
  still a product defect, still measured by
  ``test_the_checkout_token_seam_has_no_production_binding``. The permalink the exchange mints
  carries the discount code and nothing else, so the merchant never learns the exchange's
  token. What the run no longer does is *bridge* it: ``trust.reconcile`` joins an order to its
  offer through the single-use code, with ``code_created`` / ``checkout_redirect`` as bridges,
  so every token in this run's ledger is now the value its own emitter wrote. See
  :func:`require_redeemed_code`.
* **"the exchange has no client for trust's served ``GET /snapshot``"** — FALSE at HEAD, and
  it was the same shape of staleness as the pixel claim above. ``exchange.composition``'s
  ``HttpTrustSnapshot`` / ``LiveTrustSnapshot`` (T-303) read that endpoint, and
  ``exchange.eligibility.trust_backed.snapshot_rows`` is the published unwrap; the served
  route does not even return the envelope any more. :func:`_trust_snapshot` now calls the
  product's unwrap rather than keeping a copy of it, and says there what this run still does
  not exercise.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_FIXTURE = Path(__file__).resolve().parent / "run.json"

#: A fixed epoch for the whole run. Nothing here reads a wall clock: the auction is handed its
#: deadline and trust its ``as_of``. (``T_FUTURE`` is gone with ``_candidates``: the offer's
#: expiry is now the hosted agent's own ``respond_by``-derived instant, or the exchange's own
#: for a fallback, rather than a far-future constant this module wrote onto every candidate.)
T_NOW = 1_700_000_000.0
AS_OF = "2026-01-01T00:00:00Z"

#: The stub's seeded product. The VARIANT is not pinned here: the run reads it out of the
#: permalink the exchange actually minted and seeds that, so the checkout follows the URL the
#: buyer was handed rather than one the test chose. Its price is the winning offer's, so the
#: order the stub creates and the offer the exchange promised are the same money.
STUB_PRODUCT_ID = 8123456

#: The merchant's webhook secret. The stub signs with it and ``handle_delivery`` verifies
#: with it; a mismatch is a 401, which is the point of checking it here rather than trusting
#: the delivery log.
WEBHOOK_SECRET = "s1-e2e-webhook-secret"


def load_run_fixture() -> dict[str, Any]:
    """The run's scenario. Read once; never mutated by the flow."""
    return json.loads(RUN_FIXTURE.read_text(encoding="utf-8"))


def store_domain(store_id: str) -> str:
    return f"{store_id}.example.com"


def permalink_parts(url: str) -> tuple[int, int, str]:
    """``(variant_id, quantity, discount_code)`` out of a cart permalink.

    The checkout follows the URL the exchange minted rather than a variant the test picked,
    so a permalink the merchant could not actually redeem fails the run instead of passing it.
    """
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(url)
    variant, _, quantity = parts.path.rsplit("/", 1)[-1].partition(":")
    codes = parse_qs(parts.query).get("discount") or [""]
    return int(variant), int(quantity or 1), codes[0]


# --------------------------------------------------------------------------------------
# the hosted store agent, wired as the exchange's bid solicitor
# --------------------------------------------------------------------------------------
def _envelope(store: dict[str, Any], cluster_id: str) -> dict[str, Any]:
    """The store's approved envelope, as the hosted agent reads it."""
    return {
        "store_id": store["store_id"],
        "version": 1,
        "floors": [{"product_ref": store["product_ref"], "min_price": store["list_price"] * 0.75}],
        "max_discount_pct": 20.0,
        "budget_cap": 5000.0,
        "pursue_clusters": [cluster_id],
        "standing_commitments": [
            {
                "key": commitment["key"],
                "value": commitment["value"],
                "provenance": {
                    "source": "owner_statement",
                    "ref": f"envelope:{store['store_id']}:v1#{commitment['key']}",
                    "observed_at": AS_OF,
                    "authority_rank": 1,
                },
            }
            for commitment in store["commitments"]
        ],
        "activation": "active",
    }


def _store_context(store: dict[str, Any], cluster_id: str) -> dict[str, Any]:
    return {
        "store_id": store["store_id"],
        # The merchant's OWN statement of where its checkout lives. Without it
        # `AuctionContext.checkout_url_for` answers `None`, the agent bids an offer carrying no
        # `checkout_url`, and `exchange.ranking.filters.domain_reason` excludes the candidate
        # `off_domain_checkout` — measured over the served route before this line existed:
        # `POST /auctions` answered `ranked: 0, slots: 0` with every hosted store refused
        # "the offer carries no usable checkout URL". It is the STORE's word, not the
        # platform's: the exchange compares it against the registry it was wired with
        # (`StaticRegisteredDomains` below), so a store naming somebody else's host is refused
        # by a real comparison rather than passing a tautology.
        "store_domain": store_domain(store["store_id"]),
        "envelope": _envelope(store, cluster_id),
        "catalog": {store["product_ref"]: dict(store["catalog"])},
        "live_state": {store["product_ref"]: dict(store["live_state"])},
        # R10's cold agent: no learned policy. The starting path never needs the learning module.
        "learned_policy": None,
        "network_priors": {cluster_id: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }


def _catalog_snapshot(store: dict[str, Any]) -> dict[str, Any]:
    """The catalogue snapshot the verifier grades this store's claims against.

    Built from the same catalogue rows the agent read, which is what makes a *false* claim
    detectable: the agent asserts what its catalogue says, the verifier reads the catalogue
    independently, and a divergence is a contradiction rather than a matter of opinion.
    """
    attributes = {
        key: {"value": value} for key, value in store["catalog"].items() if key != "product_ref"
    }
    attributes.update({key: {"value": value} for key, value in store["live_state"].items()})
    for commitment in store["commitments"]:
        attributes[commitment["key"]] = {"value": commitment["value"]}
    return {
        "snapshot_id": f"snap-{store['store_id']}",
        "captured_at": AS_OF,
        "store_id": store["store_id"],
        "products": [
            {
                "product_ref": store["product_ref"],
                "canonical_name": store["product_ref"],
                "evidence_ref": f"snap-{store['store_id']}#{store['product_ref']}",
                "observed_at": AS_OF,
                "attributes": attributes,
                "offer": {
                    "unit_price": store["list_price"],
                    "currency": "USD",
                    "availability": "in_stock",
                },
            }
        ],
    }


class HostedAgentSolicitor:
    """The exchange's bid solicitor, backed by the real hosted store agent.

    ``solicit_bids`` calls this once per rostered store inside the bid window. A hosted store
    answers with whatever ``store_agent.runtime.bid`` returns; the silent store answers
    ``None``, which is what makes the exchange manufacture its list-price fallback rather
    than this module hand-writing one.
    """

    def __init__(self, fixture: dict[str, Any]) -> None:
        self._fixture = fixture
        self._by_id = {store["store_id"]: store for store in fixture["stores"]}
        self.bids: dict[str, dict[str, Any]] = {}
        self.asked: list[str] = []

    def solicit(self, store: Any) -> dict[str, Any] | None:
        from store_agent.runtime import Decline
        from store_agent.runtime import bid as run_agent

        store_id = str(store["store_id"])
        self.asked.append(store_id)
        row = self._by_id[store_id]
        if row["role"] != "hosted":
            return None

        request = {
            "auction_id": self._fixture["auction_id"],
            "intent": self._fixture["intent"],
            "profile": self._fixture["profile"],
            "respond_by": "2999-01-01T00:00:00Z",
        }
        answer = run_agent(request, _store_context(row, self._fixture["intent"]["cluster_id"]))
        # `bid` returns `Bid | Decline` and never raises — a store agent that cannot price
        # declines rather than blowing up. A hosted store that declines here would be
        # collected as a silent store's list-price fallback, which is a DIFFERENT scenario
        # wearing this one's name, so it fails the run loudly instead. `isinstance` rather
        # than `is_decline`, whose `TypeGuard` narrows only the positive branch.
        if isinstance(answer, Decline):
            raise AssertionError(
                f"the hosted agent for {store_id!r} declined to bid ({answer.reason}); the "
                f"run needs a real hosted pitch here, not a manufactured fallback"
            )
        payload = answer.model_dump(mode="json")
        self.bids[store_id] = payload
        return {"store_id": store_id, "received_at": T_NOW, "bid": payload}

    __call__ = solicit


class _ExchangeAuctionClient:
    """What the buyer hands :func:`confirm`: the exchange's real ``POST /auctions``.

    ``confirm`` looks for one of ``AUCTION_CLIENT_METHODS`` on this object; ``create_auction``
    is the first of them. The body it builds is the route's request model, so the buyer's
    confirmation reaches the exchange over the same door a deployed buyer would use.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.response: dict[str, Any] | None = None

    def create_auction(self, body: Any) -> dict[str, Any]:
        response = self._client.post("/auctions", json=_jsonable(body))
        if response.status_code != 201:
            raise AssertionError(f"POST /auctions returned {response.status_code}: {response.text}")
        self.response = response.json()
        return self.response


def _jsonable(value: Any) -> Any:
    """The confirmation body as JSON. ``confirm`` may hand over models or plain dicts."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


# --------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------
@dataclass
class S1Run:
    """Everything one pass through the starting path produced, for the tests to read."""

    fixture: dict[str, Any]
    clarification: Any = None
    auction_id: str = ""
    auction_response: dict[str, Any] = field(default_factory=dict)
    solicited: list[str] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    denied: list[dict[str, Any]] = field(default_factory=list)
    bids: dict[str, Any] = field(default_factory=dict)
    #: One record per claim the EXCHANGE announced a verdict for — read straight off the
    #: `claim_verified` events its ranker wrote, never re-decided here.
    asserted_claims: list[dict[str, Any]] = field(default_factory=list)
    #: The claims a store made that the exchange announced nothing for, by difference.
    unmapped_claims: list[dict[str, Any]] = field(default_factory=list)
    #: The bids the exchange recorded for this auction, read back off its own book.
    candidates: list[dict[str, Any]] = field(default_factory=list)
    ranked: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    shortlist: dict[str, Any] = field(default_factory=dict)
    #: The same object read back through the published `GET /auctions/{id}/shortlist`.
    served_shortlist: dict[str, Any] = field(default_factory=dict)
    #: `bid_ref -> store_id`, off the auction's own published ranking.
    store_by_bid_ref: dict[str, str] = field(default_factory=dict)
    accept_result: Any = None
    minted_code: str = ""
    permalink_url: str = ""
    completion: dict[str, Any] = field(default_factory=dict)
    pixel_observation: Any = None
    webhook_decision: Any = None
    #: The RAW ``orders/paid`` delivery the stub sent — the exact bytes it signed and the
    #: headers it signed them with. Kept so the tests can drive the merchant's verifier with
    #: a delivery it should REFUSE. Without this the suite can only ever observe a valid
    #: signature being accepted, which is the one thing an always-true verifier also does.
    webhook_delivery: dict[str, Any] = field(default_factory=dict)
    #: The outcome of redeeming the SAME single-use code a second time at the same merchant,
    #: taken after the run's own evidence was snapshotted so it cannot perturb it.
    second_redemption: dict[str, Any] = field(default_factory=dict)
    reconciled: list[dict[str, Any]] = field(default_factory=list)
    trust_before: dict[str, Any] = field(default_factory=dict)
    trust_after: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    chain: dict[str, Any] = field(default_factory=dict)
    module_files: dict[str, str] = field(default_factory=dict)

    @property
    def kind_counts(self) -> dict[str, int]:
        from collections import Counter

        return dict(Counter(str(event["kind"]) for event in self.events))

    def entry(self, store_id: str) -> dict[str, Any]:
        for row in self.entries:
            if row["store_id"] == store_id:
                return row
        raise KeyError(store_id)

    def entry_for_bid_ref(self, bid_ref: str) -> dict[str, Any]:
        """The collected entry a published ``bid_ref`` names.

        Resolved through :attr:`store_by_bid_ref`, which the run reads off the auction's own
        ``ranked``/``excluded`` lists — the exchange's attribution of a reference to a store,
        not a join this module reconstructs.
        """
        store_id = self.store_by_bid_ref.get(str(bid_ref))
        if store_id is None:
            raise KeyError(bid_ref)
        return self.entry(store_id)


def run_s1_flow() -> S1Run:
    """Drive the whole starting path once and return everything it produced."""
    from contracts.ledger import validate_ledger_payload  # noqa: F401  (used by the tests)
    from exchange.accept import accept
    from exchange.auction import (
        AuctionStateMachine,
        InMemoryAuctionStore,
        InMemoryLedgerSink,
        LedgerRecorder,
    )
    from exchange.auction.ledger import build_event
    from exchange.auction.routes import configure_auctions
    from exchange.checkout.sellers import StaticRegisteredDomains
    from exchange.eligibility import StaticSellerEligibility
    from exchange.main import create_app
    from exchange.ranking.serving import configure_ranking
    from exchange.ranking.verification import StaticCatalogSnapshots
    from fastapi.testclient import TestClient
    from trust.scoring import claim_dimension

    from proxyshop_support.llm_double import LLMDouble

    fixture = load_run_fixture()
    run = S1Run(fixture=fixture)
    run.module_files = _resolved_module_files()

    sink = InMemoryLedgerSink()
    recorder = LedgerRecorder(sink)
    machine = AuctionStateMachine(InMemoryAuctionStore(), recorder)
    eligibility = StaticSellerEligibility(
        {store["store_id"]: store["eligibility"] for store in fixture["stores"]}
    )
    solicitor = HostedAgentSolicitor(fixture)

    # -- 1. the buyer clarifies, then confirms ------------------------------------------
    run.clarification = _clarify(fixture, LLMDouble())

    app = create_app()
    configure_auctions(app, machine=machine, solicitor=solicitor, eligibility=eligibility)
    # The ranking's collaborators, BOUND INTO THE APP rather than held privately by this module.
    # Until this call existed the run computed all of them and then ranked with them itself, so
    # the SERVED route ranked with the fail-closed defaults instead: measured, `POST /auctions`
    # answered `ranked: 0, excluded: 3, shortlist.slots: 0`, every candidate refused
    # `blacklist_unreadable` (no trust snapshot) and `off_domain_checkout` (no registry), while
    # the suite stayed green because this file re-did the work. Each one is a deployment fact
    # this run stands in for, and each is the exchange's own to read: the catalogue it grades
    # claims against, the trust rows it filters on, the platform's store->domain registry, and
    # the human-approved claim_type -> dimension routing it announces a verdict under (which it
    # may not hold itself — `exchange.ranking.serving.claim_dimensions_of` says why).
    run.trust_before = _trust_snapshot(fixture)
    configure_ranking(
        app,
        catalog=StaticCatalogSnapshots(
            {store["store_id"]: _catalog_snapshot(store) for store in fixture["stores"]}
        ),
        trust_snapshot=run.trust_before,
        registered_domains=StaticRegisteredDomains(
            {store["store_id"]: store_domain(store["store_id"]) for store in fixture["stores"]}
        ),
        claim_dimensions=claim_dimension,
    )
    with TestClient(app) as client:
        auction_client = _ExchangeAuctionClient(client)
        _confirm(fixture, run.clarification, auction_client)
        response = auction_client.response or {}
        run.auction_id = str(response["auction_id"])
        # The published door onto the same object, driven rather than assumed. `POST /auctions`
        # returns the shortlist inline and `GET /auctions/{id}/shortlist` re-validates it
        # through the pinned `Shortlist`; reading both is how the run shows the buyer-facing
        # route actually serves what the auction produced.
        served = client.get(f"/auctions/{run.auction_id}/shortlist")
        if served.status_code != 200:
            raise AssertionError(
                f"GET /auctions/{run.auction_id}/shortlist returned {served.status_code}: "
                f"{served.text}"
            )
        run.served_shortlist = served.json()
        # The bids the exchange itself recorded for this auction, read back off the book
        # `POST /auctions` wrote (`collected_bid_records`). This is the run's ONLY source of
        # candidates: it used to build its own list here, which is how a spine that shortlisted
        # nobody could look healthy.
        book = app.state.auction_bids
        run.candidates = [dict(record) for record in book.bids_for(run.auction_id)]

    run.auction_response = response
    run.solicited = list(response["solicited"])
    run.entries = [dict(entry) for entry in response["entries"]]
    run.denied = [dict(denial) for denial in response["denied"]]
    run.bids = dict(solicitor.bids)
    run.ranked = [dict(row) for row in response["ranked"]]
    run.excluded = [dict(row) for row in response["excluded"]]
    run.shortlist = dict(response["shortlist"])
    # `bid_ref -> store_id`, off the exchange's own published ranking. Every candidate the
    # auction collected appears in exactly one of `ranked` and `excluded`, so this is the
    # auction's attribution rather than a join this module invents.
    run.store_by_bid_ref = {
        str(row["bid_ref"]): str(row["store_id"]) for row in (*run.ranked, *run.excluded)
    }

    # -- 2. the verdicts the EXCHANGE minted, read back off the ledger it wrote them to -----
    # This module used to run `claim_verification.verify` here itself, over a pitch it built
    # from the store's bid and a snapshot it held privately, and then emit one `claim_verified`
    # event per verdict. All of that is the exchange's job and the exchange now does it:
    # `exchange.ranking.verification.attest_candidate_claims` verifies against the catalogue
    # bound above and announces each verdict as it is minted. What is left here is READING what
    # the auction produced.
    run.asserted_claims = [
        {
            "store_id": str(event.get("store_id") or ""),
            **{
                key: value
                for key, value in (event.get("payload") or {}).items()
                if key in ("claim_ref", "status", "dim", "claim_type")
            },
        }
        for event in sink.events
        if str(event["kind"]) == "claim_verified"
    ]
    # The claims a store made that the exchange announced NOTHING for. Derived by DIFFERENCE
    # against what the ledger carries rather than by re-deciding anything: `claim_ref` is
    # positional (`{store_id}#{index}` — `exchange.ranking.verification.claim_ref_for`), so a
    # ref the ledger does not carry names the store's own claim at that position. The exempt
    # set is asserted to be exactly the agent's policy telemetry, so a genuinely unroutable
    # PRODUCT claim cannot hide in it.
    announced = {str(claim["claim_ref"]) for claim in run.asserted_claims}
    run.unmapped_claims = [
        {"store_id": store_id, "claim_ref": ref, **dict(claim)}
        for store_id, bid in sorted(run.bids.items())
        for index, claim in enumerate(bid.get("claims") or ())
        if (ref := f"{store_id}#{index}") not in announced
    ]

    # -- 5. acceptance -> code -> simulated redirect ------------------------------------
    winner = run.shortlist["slots"][0]
    auction_record = _auction_record(run, winner["bid_ref"])
    domains = StaticRegisteredDomains(
        {store["store_id"]: store_domain(store["store_id"]) for store in fixture["stores"]}
    )
    run.accept_result = accept(
        auction_record,
        winner["bid_ref"],
        _RecordingCodeCreator(),
        fixture["checkout_mode"],
        registered_domains=domains,
    )
    for event in run.accept_result.events:
        sink.emit(dict(event))
    # The port's `code_created` payload spells the code `discount_code`; the published D34
    # shape spells it `code` (contracts/src/ledger.py:70). Read both rather than pick one --
    # the divergence is a product defect this run reports, not one it should depend on.
    code_payload = _event_of(run.accept_result, "code_created")["payload"]
    run.minted_code = str(code_payload.get("code") or code_payload["discount_code"])
    run.permalink_url = str(
        _event_of(run.accept_result, "checkout_redirect")["payload"]["permalink_url"]
    )

    # -- 6. the merchant leg: stub checkout, pixel, webhook -----------------------------
    # The sink goes in because the merchant's own ledger writes come back out through it: the
    # beacon reaches the SERVED `POST /pixel/collect`, and `publish_pixel_observation` appends
    # the `checkout_pixel` row from inside that route, before this call returns.
    checkout = asyncio.run(_drive_merchant(run, sink))
    run.completion = checkout["completion"]
    # The exact bytes the stub signed, and the headers it signed them with. Kept verbatim so
    # the tests can hand the merchant's verifier a delivery it must REFUSE; a suite that only
    # ever sees a valid signature accepted cannot tell `verify` from `lambda *_: True`.
    run.webhook_delivery = dict(checkout["webhook_requests"][0])
    run.second_redemption = dict(checkout["second_redemption"])
    order_paid_event, run.pixel_observation, run.webhook_decision = _merchant_events(
        run, checkout, build_event
    )
    sink.emit(order_paid_event)

    # -- 7. reconciliation --------------------------------------------------------------
    run.reconciled = _reconcile(run, sink)
    for event in run.reconciled:
        sink.emit(dict(event))

    # -- 8. the trust projection --------------------------------------------------------
    run.trust_after = _project_trust(run)

    run.events = [dict(event) for event in sink.events]
    run.chain = _chain(run.events)
    return run


# --------------------------------------------------------------------------------------
# the stages, one helper each
# --------------------------------------------------------------------------------------
def _clarify(fixture: dict[str, Any], llm: Any) -> Any:
    from buyer_svc.intent import clarify

    outcome = clarify(fixture["buyer_turns"], llm, intent_id=fixture["intent"]["intent_id"])
    return outcome


def _confirm(fixture: dict[str, Any], clarification: Any, auction_client: Any) -> Any:
    from buyer_svc.intent import confirm, reset_confirmations

    reset_confirmations()
    roster = [
        {
            "store_id": store["store_id"],
            "tier": store["tier"],
            "product_ref": store["product_ref"],
            "list_price": store["list_price"],
            "currency": "USD",
        }
        for store in fixture["stores"]
    ]
    return confirm(
        fixture["intent"],
        auction_client,
        confirmed=True,
        profile=fixture["profile"],
        roster=roster,
        bid_timeout_seconds=2.0,
    )


def _seeded_blacklist(fixture: dict[str, Any]) -> Any:
    """The blacklist the exchange's gates read, seeded from the run's scenario."""
    from trust.scoring import Blacklist

    blacklist = Blacklist()
    for store in fixture["stores"]:
        if store["eligibility"] == "blacklisted":
            blacklist.add(
                business_identity=store["store_id"],
                reason_code="repeated_offer_integrity_failures",
            )
    return blacklist


def _trust_snapshot(fixture: dict[str, Any]) -> dict[str, Any]:
    """The trust snapshot the ranker filters on, built by the TRUST ENGINE.

    ``trust.snapshot.build_snapshot`` is the real producer, so the blacklist flag the ranker
    reads is the trust engine's own answer rather than a row this run wrote. Before the
    auction there is no history to fold in, so every store carries the published new-store
    prior; what is *not* a prior is ``blacklisted``, which comes from the seeded blacklist.

    **The envelope unwrap is the EXCHANGE's own, and this docstring used to claim there was
    no such thing.** ``build_snapshot`` returns
    ``{version, score_version, dimensions, as_of, stores: {...}, delistings: ...}`` while
    ``exchange.ranking.filters.trust_row`` (apps/exchange/src/ranking/filters.py:140) reads a
    FLAT ``{store_id: row}`` mapping with ``snapshot.get(store_id)``; handing the ranker the
    envelope denies every store ``blacklist_unreadable``, failing closed (R12) — the whole
    auction, not merely the dishonest store. That much is unchanged. What changed is that the
    bridge exists and ships: ``exchange.eligibility.trust_backed.snapshot_rows`` unwraps
    either published shape, and ``exchange.composition``'s ``HttpTrustSnapshot`` /
    ``LiveTrustSnapshot`` (T-303) are a real client for trust's served ``GET /snapshot`` —
    which, separately, no longer serves the envelope at all
    (apps/trust/src/snapshot/routes.py:813 returns ``{store_id: published_entry(entry)}`` and
    puts the version in ``ETag``). So the run calls the product's unwrap instead of writing
    its own copy of it, and the previous claim that "the exchange has no client for trust's
    ``GET /snapshot`` at all" is deleted because it is false.

    What this run still does NOT exercise, stated so it is not mistaken for covered: it builds
    the document in-process and hands ``configure_ranking`` a plain mapping, where a deployment
    hands it ``LiveTrustSnapshot(HttpTrustSnapshot(...))`` over the served endpoint. The HTTP
    reader, its cache, and its fail-closed behaviour on an outage are not driven here.
    """
    from exchange.eligibility.trust_backed import snapshot_rows
    from trust.snapshot import build_snapshot

    served = build_snapshot(
        [
            {
                "store_id": store["store_id"],
                "business_identity": store["store_id"],
                "observations": [],
            }
            for store in fixture["stores"]
        ],
        blacklist=_seeded_blacklist(fixture),
        as_of=AS_OF,
    )
    rows = snapshot_rows(served)
    if rows is None:
        raise AssertionError(f"build_snapshot produced something that is not a snapshot: {served}")
    return dict(rows)


def _auction_record(run: S1Run, winning_bid_ref: str) -> dict[str, Any]:
    return {
        "auction_id": run.auction_id,
        "intent_id": run.fixture["intent"]["intent_id"],
        "cluster_id": run.fixture["intent"]["cluster_id"],
        "accepted_bid_ref": None,
        "now": T_NOW,
        "shortlist": run.shortlist,
        # The exchange's OWN book, handed back verbatim. `collected_bid_records` already
        # assembled exactly the four fields `accept()` reads — the minted `bid_id`, the
        # exchange-attributed `store_id`, the PLATFORM's `store_domain` and the whitelisted
        # offer — so re-projecting them here would be this module having a second opinion
        # about a record the auction already wrote.
        "bids": [dict(candidate) for candidate in run.candidates],
    }


class _RecordingCodeCreator:
    """The merchant ``POST /codes`` client, recording rather than answering.

    In ``redirect`` mode ``SimulatedRedirectProvider`` mints locally and never reaches for a
    code creator, so ``calls`` staying empty is itself an assertion the tests make — and the
    reason this object records instead of raising.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def create_code(self, store_id: str, offer: Any) -> dict[str, Any]:
        self.calls.append((store_id, offer))
        return {"code": "PSX-SHOULD-NOT-BE-USED", "permalink_url": ""}

    __call__ = create_code


class _RecordingCollector:
    """The merchant service, with the exact bytes of every request it is sent kept alongside.

    Raw ASGI and not a framework, for the same reason ``shopify_stub.testing
    .RecordingReceiver`` is: the beacon has to be observable as the bytes that were on the
    wire, and a wrapper that parsed and re-serialised them would make this module the author
    of what the collector read.

    It records and then **forwards**, which is the whole difference between this and the
    ``RecordingReceiver`` that used to stand where the merchant now stands. The recording is
    evidence; the forward is the point. ``receive`` is replayed once with the body this
    wrapper already drained and then reports a disconnect, which is what an ASGI app expects
    after a complete request body.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        self.requests.append(
            {
                "path": scope["path"],
                "headers": {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in scope["headers"]
                },
                "body": body,
            }
        )
        replayed = False

        async def replay() -> dict[str, Any]:
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


class _TrustLedgerEvents:
    """``POST /events`` — the door ``apps/trust`` serves, over the run's own ledger sink.

    ``merchant_svc.composition.publish_pixel_observation`` writes through a
    :class:`~proxyshop_support.trust_ledger.TrustLedgerPublisher`, which is an HTTP client
    for exactly one endpoint. That address is a deployment fact, resolved from ``TRUST_URL``,
    and this run stands in for it the same way :func:`configure_ranking` stands in for the
    catalogue and the domain registry: it serves the endpoint on loopback and hands what
    arrives to ``InMemoryLedgerSink``, whose own docstring already calls itself "the trust
    API stub". Nothing about the *production* half is stood in for — the route, the
    projection, the derived event id, the publisher and the POST are all the shipped code.

    A body is appended verbatim. This is not the place to validate one: ``_chain`` already
    replays every event of the run through ``trust.events.InMemoryEventStore``, which refuses
    an unknown kind and an unknown top-level field, so a malformed row fails the run there
    rather than being quietly rejected at a door the producer never checks the answer of.
    """

    def __init__(self, sink: Any, path: str) -> None:
        self.sink = sink
        self.path = path
        self.requests: list[bytes] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        status = 404
        if scope.get("method") == "POST" and scope.get("path") == self.path:
            self.requests.append(body)
            self.sink.emit(json.loads(body))
            status = 201
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})


@contextmanager
def _merchant_ledger_at(url: str) -> Iterator[None]:
    """Point the merchant service's ledger writes at ``url`` for the duration.

    ``TRUST_URL`` and not an injected publisher, so the run drives
    ``proxyshop_support.trust_ledger.trust_endpoint``'s own resolution rather than reaching
    past it — the default it would otherwise resolve to is ``http://trust:8084``, which the
    session's socket guard refuses because it is not loopback, so a run that forgot this
    would fail loudly rather than silently publishing nowhere.

    ``set_trust_publisher(None)`` on both edges is a REBUILD and never an unwiring — the rule
    that function's own docstring states — so the publisher this process holds is discarded
    and the next publish resolves the address above, and the process is left exactly as it
    was found.
    """
    from merchant_svc.composition import set_trust_publisher

    from proxyshop_support.trust_ledger import ENV_TRUST_URL

    previous = os.environ.get(ENV_TRUST_URL)
    os.environ[ENV_TRUST_URL] = url
    set_trust_publisher(None)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ENV_TRUST_URL, None)
        else:
            os.environ[ENV_TRUST_URL] = previous
        set_trust_publisher(None)


async def _drive_merchant(run: S1Run, sink: Any) -> dict[str, Any]:
    """Complete the checkout at the real shopify-stub, in-process on loopback (D41).

    The merchant is real here too, and served: ``merchant_svc.main.create_app`` on loopback,
    with the stub's web pixel installed against the merchant's OWN
    :data:`~merchant_svc.install.config.COLLECTOR_PATH`. So the beacon the stub fires is a
    real HTTP request to the real ``POST /pixel/collect``, and the ``checkout_pixel`` row in
    ``sink`` is the one ``merchant_svc.composition.publish_pixel_observation`` wrote from
    inside that route. Nothing in this module builds it.
    """
    import httpx
    from merchant_svc.collector import PIXEL_INBOX
    from merchant_svc.install.config import COLLECTOR_PATH
    from merchant_svc.main import create_app as create_merchant
    from shopify_stub.app import create_app as create_stub
    from shopify_stub.testing import RecordingReceiver, StubClient

    from proxyshop_support.asgi_server import serve
    from proxyshop_support.trust_ledger import TRUST_EVENTS_PATH

    variant_id, quantity, code = permalink_parts(run.permalink_url)
    if code != run.minted_code:
        raise AssertionError(
            f"the minted permalink carries {code!r}, not the code the ledger recorded "
            f"({run.minted_code!r})"
        )
    collector = _RecordingCollector(create_merchant())
    ledger = _TrustLedgerEvents(sink, TRUST_EVENTS_PATH)
    #: Where the SECOND-redemption probe's beacon goes. The probe must not perturb the run it
    #: is probing, and a live collector makes that a live concern: the probe completes a
    #: second checkout, whose beacon is a second real POST and would be a second
    #: ``checkout_pixel`` row in the ledger the exact multiset grades. So the pixel is
    #: re-pointed here before the probe runs, which records the beacon and publishes nothing.
    probe_collector = RecordingReceiver()
    webhooks = RecordingReceiver()
    already_observed = len(PIXEL_INBOX.observations())
    with (
        serve(create_stub()) as stub_url,
        serve(ledger) as trust_url,
        serve(collector) as collector_url,
        serve(probe_collector) as probe_collector_url,
        serve(webhooks) as webhook_url,
        _merchant_ledger_at(trust_url),
    ):
        async with httpx.AsyncClient(base_url=stub_url, follow_redirects=False) as http:
            stub = StubClient(http, stub_url)
            seeded = await stub.seed(
                [
                    {
                        "variant_id": variant_id,
                        "product_id": STUB_PRODUCT_ID,
                        "title": "Heat-exchange espresso machine",
                        # The WINNING offer's price, off the exchange's own record of the
                        # bid the accept door was handed — not `candidates[0]`, which was the
                        # first row of a list this module used to build for itself and only
                        # happened to be the winner. The order the stub creates and the offer
                        # the exchange promised have to be the same money.
                        "price": f"{accepted_unit_price(run):.2f}",
                        "currency": "USD",
                        "sku": "HX-1",
                    }
                ]
            )
            assert seeded.status_code == 200, seeded.text
            await stub.configure(webhook_secret=WEBHOOK_SECRET)
            # The merchant's own collector path, off the merchant's own constant. The stub's
            # `webPixel` settings therefore carry the URL a real install would carry, and a
            # path this run spelled by hand could not drift from the service's.
            await stub.install_pixel(f"{collector_url}{COLLECTOR_PATH}")
            await stub.subscribe("ORDERS_PAID", f"{webhook_url}/webhooks/shopify")
            created = await stub.create_code(run.minted_code, percentage=0.0)
            assert created.status_code == 200, created.text
            completion = await stub.buy(variant_id, quantity=quantity, code=code)
            deliveries = await stub.deliveries()
            # Everything the run is graded on is snapshotted HERE, before the second
            # redemption below, so that probe cannot perturb the run it is probing —
            # `_merchant_events` unpacks exactly one webhook delivery.
            pixel_requests = list(collector.requests)
            webhook_requests = list(webhooks.requests)
            pixel_observations = list(PIXEL_INBOX.observations()[already_observed:])
            # ONE ledger write reached the trust door while the merchant was pointed at it,
            # and it came from the collector route. Checked here rather than left implicit
            # because this window is the only one in which the merchant's publisher is
            # addressable at all: `handle_delivery`'s default sink also publishes (an
            # `order_paid`, through the same publisher), and `_merchant_events` calls it after
            # `_merchant_ledger_at` has put the address back, so that write goes to the
            # unreachable deployment default and this run's `order_paid` row stays the one
            # `_merchant_events` builds. If that ever changes, the count below moves first.
            if len(ledger.requests) != 1:
                raise AssertionError(
                    f"{len(ledger.requests)} ledger event(s) reached the trust door during the "
                    "merchant leg; the run's checkout produced exactly one beacon and the "
                    "served collector publishes exactly one row for it"
                )
            # …and the beacon is re-pointed away from the served collector for the same
            # reason, because that snapshot cannot protect a ledger the probe writes to.
            await stub.install_pixel(f"{probe_collector_url}/collect")
            second = await _second_redemption(stub, variant_id, quantity, code)
    return {
        "completion": completion,
        "variant_id": variant_id,
        "quantity": quantity,
        "pixel_requests": pixel_requests,
        "pixel_observations": pixel_observations,
        "webhook_requests": webhook_requests,
        "ledger_requests": list(ledger.requests),
        "deliveries": deliveries,
        "stub_url": stub_url,
        "second_redemption": second,
    }


async def _second_redemption(
    stub: Any, variant_id: int, quantity: int, code: str
) -> dict[str, Any]:
    """Redeem the SAME code a second time at the same merchant, and report what happened.

    D22 mints a **single-use** code, and the stub creates it with ``usageLimit: 1``. This
    probe is what turns that adjective into a checked claim. It asserts nothing itself: the
    outcome is handed to the tests, which decide, because "the cart refused it" and "the cart
    accepted it and applied no discount" are both legitimate merchant behaviours and only the
    tests should say which this stub does.

    Never raises. A ``buy`` that fails is a *result* here, not a broken run.
    """
    try:
        completion = await stub.buy(variant_id, quantity=quantity, code=code)
    except Exception as exc:  # noqa: BLE001 - the refusal shape is the measurement
        return {
            "refused": True,
            "error": f"{type(exc).__name__}: {exc}",
            "completion": {},
            "code": code,
        }
    return {"refused": False, "error": "", "completion": completion, "code": code}


def accepted_bid(run: S1Run) -> dict[str, Any]:
    """The exchange's own record of the bid this run accepted.

    Read out of :attr:`S1Run.candidates`, which is the bid book ``POST /auctions`` wrote — so
    this is the same record the accept door itself looked the reference up in.
    """
    winning_bid_ref = str(run.shortlist["slots"][0]["bid_ref"])
    for candidate in run.candidates:
        if str(candidate.get("bid_id") or candidate.get("bid_ref") or "") == winning_bid_ref:
            return candidate
    raise AssertionError(
        f"the exchange recorded no bid under {winning_bid_ref!r}, the reference its own "
        f"shortlist published as slot 0: {[c.get('bid_id') for c in run.candidates]}"
    )


def accepted_unit_price(run: S1Run) -> float:
    """What the winning offer charges for one unit, as the exchange recorded it."""
    return float(accepted_bid(run)["offer"]["unit_price"])


def require_redeemed_code(run: S1Run, completion: dict[str, Any]) -> str:
    """The single-use code the merchant's order redeemed, refusing anything but the mint's.

    **This replaces the run's ``authorized_checkout_token`` binding, which is deleted**, and
    the deletion is what the pixel change forced. That helper rewrote the merchant's
    ``order_paid`` row to carry the EXCHANGE's ``checkout_token`` instead of the merchant's,
    keeping the platform's own alongside under ``platform_checkout_token`` — a key no
    production emitter writes. It was harmless while the run also hand-built the
    ``checkout_pixel`` row and could give that the same rewritten token. It is not harmless
    now: the served ``POST /pixel/collect`` publishes the MERCHANT's token, which is the
    value D24 says the pixel and the webhook meet on, so a rewritten webhook token puts the
    two halves of the reconciliation in different join groups. Measured, with the binding
    still in place and the served collector driven: ``reconciled.payload.pixel_missing`` came
    back ``True`` on a run whose stub really posted a beacon.

    The seam it was bridging is unchanged and is still a defect. ``CheckoutProvider.checkout``
    invents its ``checkout_token`` with ``secrets.token_hex(16)``
    (apps/exchange/src/checkout/provider.py:828) and never transmits it — the cart permalink
    carries the discount code and nothing else (provider.py:1072) — and the merchant mints its
    own, unrelated token when the cart is visited.
    ``test_the_checkout_token_seam_has_no_production_binding`` measures exactly that, now off
    the two events' own untouched tokens rather than off a key this module invented.

    What makes the bridge unnecessary is the consumer: ``trust.reconcile.reconcile`` no longer
    joins an order to its offer on those tokens alone. It reads the single-use discount code,
    with ``code_created`` / ``checkout_redirect`` as bridges, and that is the exchange's real
    handle on the checkout — minted by the exchange, put in the permalink, and carried back on
    the merchant's order. So the premise below is the only thing the run still has to check,
    and it is checked rather than assumed.

    Raises:
        AssertionError: the order does not carry the code the exchange minted, in which case
            this order is not the completion of that authorized checkout and the run must
            fail rather than guess.
    """
    redeemed = str(completion.get("discount_code") or "")
    if redeemed != run.minted_code:
        raise AssertionError(
            f"the merchant's order redeemed {redeemed!r}, not the single-use code the "
            f"exchange minted ({run.minted_code!r}); there is nothing to reconcile it to"
        )
    return redeemed


def _merchant_events(
    run: S1Run, checkout: dict[str, Any], build_event: Any
) -> tuple[dict[str, Any], Any, Any]:
    """The paid webhook, read by the real merchant code, and the beacon's own observation.

    **There is no ``checkout_pixel`` here any more, and its absence is the ticket.** This
    function used to call ``merchant_svc.collector.accept_pixel_event`` on the stub's beacon
    body and then hand-build the ledger row from what came back, because no served path wrote
    one. One does now: ``collector/routes.collect_pixel_event`` calls
    ``composition.publish_pixel_observation``, and :func:`_drive_merchant` beacons at that
    route, so the row is already in the sink before this function is reached. What is
    returned here is the observation the SERVED route parsed and recorded, read back off
    ``merchant_svc.collector.PIXEL_INBOX`` — the collector's own ring, not a second parse.
    """
    from merchant_svc.install.webhooks import handle_delivery, ledger_record

    completion = checkout["completion"]
    store_id = run.fixture["expected"]["winning_store"]
    total_price = float(completion["total_price"])
    require_redeemed_code(run, completion)

    observations = checkout["pixel_observations"]
    if len(observations) != 1:
        raise AssertionError(
            f"the served collector recorded {len(observations)} observation(s) for this "
            "run's one checkout; the beacon either never arrived or arrived more than once"
        )
    (observation,) = observations

    (delivery,) = checkout["webhook_requests"]
    decision = handle_delivery(
        body=delivery["body"], headers=delivery["headers"], secret=WEBHOOK_SECRET
    )
    if not decision.accepted:
        raise AssertionError(
            f"the paid webhook was refused: {decision.status_code} {decision.reason}"
        )
    if decision.event is None:
        # `accepted` is a status-code range and `event` is separately optional — a 2xx with
        # no event is how `handle_delivery` reports an acknowledged DUPLICATE. Recording one
        # would be the double-count the merchant's de-duplication exists to prevent.
        raise AssertionError(
            f"the merchant accepted the paid webhook ({decision.status_code} "
            f"{decision.reason}, duplicate={decision.duplicate}) but produced no event"
        )
    record = ledger_record(decision.event)
    order_paid_event = build_event(
        record["kind"],
        auction_id=run.auction_id,
        store_id=store_id,
        order_ref=str(record["order_ref"]),
        payload={
            # The MERCHANT's own token, exactly as `ledger_record` lifted it off the signed
            # body and exactly as `merchant_svc.composition.ledger_payload` would publish it.
            # It used to be overwritten with the exchange's, which is a value no production
            # emitter puts here and which now splits the pixel from its own webhook — see
            # `require_redeemed_code`.
            "checkout_token": record["checkout_token"],
            "order_ref": str(record["order_ref"]),
            "total_price": total_price,
            # D24 pins `discount_code` as one of the four join keys, and the signed
            # `orders/paid` body is where it lives — spelled `discount_codes: [{"code": …}]`,
            # Shopify's own shape. `ledger_record` keeps the whole vendor body but lifts only
            # `order_ref` and `checkout_token` out of it, so a driver that rebuilds the
            # payload by hand, as this one does, was silently throwing the code away. It is
            # forwarded verbatim rather than lifted: the ledger should record what the
            # merchant actually said, and `trust.reconcile` already reads this spelling.
            "discount_codes": record["payload"].get("discount_codes") or [],
        },
    )
    return order_paid_event, observation, decision


def _reconcile(run: S1Run, sink: Any) -> list[dict[str, Any]]:
    """Join the accepted offer, the beacon and the webhook into one ``reconciled`` verdict."""
    from trust.reconcile import reconcile

    return reconcile(copy.deepcopy(list(sink.events)))


def _project_trust(run: S1Run) -> dict[str, Any]:
    """Fold the run's observations into the served trust snapshot.

    ``reconciled_observations`` and not ``observation_events``: the latter emits
    ``offer_integrity`` ledger events, and an honest reconciliation has no integrity finding
    to report. Same data, no event — which is why this run's ``offer_integrity`` count is 0.
    """
    from trust.reconcile import reconciled_observations
    from trust.snapshot import build_snapshot
    from trust.verification import observation_from_claim

    observations: dict[str, list[dict[str, Any]]] = {}
    for claim in run.asserted_claims:
        observations.setdefault(claim["store_id"], []).append(
            observation_from_claim(claim, claim["store_id"], observed_at=AS_OF)
        )
    for observation in reconciled_observations(run.reconciled):
        observations.setdefault(str(observation["store_id"]), []).append(dict(observation))

    return build_snapshot(
        [
            {
                "store_id": store_id,
                "business_identity": store_id,
                "observations": rows,
            }
            for store_id, rows in sorted(observations.items())
        ],
        blacklist=_seeded_blacklist(run.fixture),
        as_of=AS_OF,
    )


def _chain(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Append every event of the run to the hash-chained store and verify the chain.

    This is not decoration: ``InMemoryEventStore`` rejects an unknown kind and an unknown
    top-level field before it will chain anything, so a green ``verify()`` is the run's
    evidence that each event it produced is a well-formed ``LedgerEvent`` and that the
    sequence is tamper-evident.
    """
    from trust.events import InMemoryEventStore, append

    store = InMemoryEventStore()
    for event in events:
        append(store, {key: value for key, value in event.items() if value is not None})
    return store.verify()


def _event_of(result: Any, kind: str) -> dict[str, Any]:
    for event in result.events:
        if str(event["kind"]) == kind:
            return dict(event)
    raise KeyError(f"{kind} not among {[e['kind'] for e in result.events]}")


def _resolved_module_files() -> dict[str, str]:
    """Where every product module this run drives actually lives on disk.

    The venv's ``site-packages/_proxyshop.pth`` puts a *different* checkout on ``sys.path``
    for every process that uses it, so "the import worked" is not evidence that the code
    under test is this worktree's. The tests hold these paths to this tree.
    """
    import importlib

    names = (
        "buyer_svc.intent",
        "claim_verification",
        "contracts.ledger",
        "exchange.accept",
        "exchange.auction",
        "exchange.ranking",
        "exchange.retrieval.fit",
        "merchant_svc.collector",
        "merchant_svc.composition",
        "merchant_svc.install.webhooks",
        "merchant_svc.main",
        "shopify_stub.app",
        "store_agent.runtime",
        "trust.reconcile",
        "trust.scoring",
    )
    resolved: dict[str, str] = {}
    for name in names:
        module = importlib.import_module(name)
        resolved[name] = str(Path(module.__file__ or "").resolve())
    return resolved
