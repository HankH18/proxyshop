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
pixel                                   ``merchant_svc.collector.accept_pixel_event`` over the
                                        bytes the stub's web pixel really POSTed
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

What this module still has to emit itself, and why
--------------------------------------------------
ONE ledger kind in the S1 chain still has **no production emitter anywhere in the tree**, so
the run constructs it from the real upstream data rather than pretending it appeared:

* ``checkout_pixel`` — ``pixel/src/`` holds a real Web Pixel extension, but nothing on a
  served path turns its beacon into a ledger event and ``merchant_svc.collector`` stops at a
  ``PixelObservation``. Emitted here from the observation the real collector parsed out of the
  stub's real beacon.

The behaviour is never faked — the beacon really was posted by the stub. It is the ledger
*write* that has no owner yet. ``e2e/test_s1_flow.py`` states this again as a test, so the gap
is visible in the suite's output rather than only in this docstring, and so the exact multiset
turns red the moment a production emitter lands and starts double-counting.

Two seams the run still bridges, each reported as a defect by a test of its own:
``authorized_checkout_token`` (the exchange's ``checkout_token`` and the merchant's are
unrelated values) and ``_trust_snapshot``'s ``["stores"]`` unwrap (the exchange has no client
for trust's served ``GET /snapshot``).
"""

from __future__ import annotations

import asyncio
import copy
import json
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
    checkout = asyncio.run(_drive_merchant(run))
    run.completion = checkout["completion"]
    # The exact bytes the stub signed, and the headers it signed them with. Kept verbatim so
    # the tests can hand the merchant's verifier a delivery it must REFUSE; a suite that only
    # ever sees a valid signature accepted cannot tell `verify` from `lambda *_: True`.
    run.webhook_delivery = dict(checkout["webhook_requests"][0])
    run.second_redemption = dict(checkout["second_redemption"])
    pixel_event, order_paid_event, run.pixel_observation, run.webhook_decision = _merchant_events(
        run, checkout, build_event
    )
    sink.emit(pixel_event)
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

    **The `["stores"]` unwrap is a seam defect this run reports, not a convenience.**
    ``build_snapshot`` returns the served document
    ``{version, score_version, dimensions, as_of, stores: {...}}``, and
    ``exchange.ranking.filters.trust_row`` (apps/exchange/src/ranking/filters.py:108) reads a
    FLAT ``{store_id: row}`` mapping with ``snapshot.get(store_id)``. Measured: handing the
    ranker the served document denies every store with ``blacklist_unreadable ... failing
    closed (R12)`` -- the whole auction, not just the dishonest store. Nothing in the tree
    performs this unwrap, because the exchange has no client for trust's ``GET /snapshot``
    at all. Until one exists, the unwrap is the run's, and it is one line so it stays visible.
    """
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
    return dict(served["stores"])


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


async def _drive_merchant(run: S1Run) -> dict[str, Any]:
    """Complete the checkout at the real shopify-stub, in-process on loopback (D41)."""
    import httpx
    from shopify_stub.app import create_app as create_stub
    from shopify_stub.testing import RecordingReceiver, StubClient

    from proxyshop_support.asgi_server import serve

    variant_id, quantity, code = permalink_parts(run.permalink_url)
    if code != run.minted_code:
        raise AssertionError(
            f"the minted permalink carries {code!r}, not the code the ledger recorded "
            f"({run.minted_code!r})"
        )
    collector = RecordingReceiver()
    webhooks = RecordingReceiver()
    with (
        serve(create_stub()) as stub_url,
        serve(collector) as collector_url,
        serve(webhooks) as webhook_url,
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
            await stub.install_pixel(f"{collector_url}/collect")
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
            second = await _second_redemption(stub, variant_id, quantity, code)
    return {
        "completion": completion,
        "variant_id": variant_id,
        "quantity": quantity,
        "pixel_requests": pixel_requests,
        "webhook_requests": webhook_requests,
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


def authorized_checkout_token(run: S1Run, completion: dict[str, Any]) -> str:
    """The token of the checkout the EXCHANGE authorized, for the order the merchant closed.

    **This hop no longer needs a binding here, and this docstring is the record of that
    changing.** ``CheckoutProvider.checkout`` still invents its ``checkout_token`` with
    ``secrets.token_hex(16)`` (apps/exchange/src/checkout/provider.py:828) and still never
    transmits it: the cart permalink it builds carries the discount code and nothing else
    (provider.py:1072), and the merchant still mints its own, unrelated token when the cart is
    visited. So ``accepted.payload.checkout_token`` and the merchant's own token remain two
    different values for one checkout — that half has not changed and
    ``test_the_checkout_token_seam_has_no_production_binding`` still measures it.

    What changed is the consumer. ``trust.reconcile.reconcile`` used to join an order to its
    offer on those tokens alone, so it found no shared key and emitted nothing: measured on
    this tree, without this binding the run produced ZERO ``reconciled`` events with every
    other stage green. It now also joins on the single-use discount code, reading
    ``code_created`` / ``checkout_redirect`` as bridges, and the same measurement produces
    ONE. The binding below is therefore no longer load-bearing and can be deleted — which
    also means deleting the two ``checkout_token`` assertions in
    ``test_the_checkout_token_seam_has_no_production_binding``, so it is left standing here
    for a lane that owns that file.

    The binding is *derived*, never invented, and its premise is the same value the
    production join now uses. The single-use discount code IS the exchange's handle on the
    checkout: the exchange minted it, put it in the permalink, and the merchant's order came
    back carrying it. Matching the order's ``discount_code`` to the minted code therefore
    establishes that this order is the completion of that authorized checkout, and its token
    is the one the ledger already recorded. The platform's own token is kept alongside under
    ``platform_checkout_token`` so nothing is lost.

    Raises:
        AssertionError: the order does not carry the code the exchange minted, in which case
            no binding exists and the run must fail rather than guess.
    """
    redeemed = str(completion.get("discount_code") or "")
    if redeemed != run.minted_code:
        raise AssertionError(
            f"the merchant's order redeemed {redeemed!r}, not the single-use code the "
            f"exchange minted ({run.minted_code!r}); there is nothing to reconcile it to"
        )
    return str(_event_of(run.accept_result, "accepted")["payload"]["checkout_token"])


def _merchant_events(
    run: S1Run, checkout: dict[str, Any], build_event: Any
) -> tuple[dict[str, Any], dict[str, Any], Any, Any]:
    """The pixel beacon and the paid webhook, each read by the real merchant code."""
    from merchant_svc.collector import accept_pixel_event
    from merchant_svc.install.webhooks import handle_delivery, ledger_record

    completion = checkout["completion"]
    store_id = run.fixture["expected"]["winning_store"]
    total_price = float(completion["total_price"])
    authorized_token = authorized_checkout_token(run, completion)

    (beacon,) = checkout["pixel_requests"]
    observation = accept_pixel_event(json.loads(beacon["body"]))
    pixel_event = build_event(
        "checkout_pixel",
        auction_id=run.auction_id,
        store_id=store_id,
        order_ref=str(completion["order_id"]),
        payload={
            "checkout_token": authorized_token,
            "platform_checkout_token": observation.checkout_token,
            "client_id": observation.client_id,
            # The stub's beacon carries no money on purpose (a web pixel is lossy and
            # untrusted); the amount is the order's, which is the webhook's truth.
            "total_price": total_price,
        },
    )

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
            "checkout_token": authorized_token,
            "platform_checkout_token": record["checkout_token"],
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
    return pixel_event, order_paid_event, observation, decision


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
        "merchant_svc.install.webhooks",
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
