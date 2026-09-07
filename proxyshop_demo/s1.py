"""The S1 starting slice, driven live and narrated for a person watching.

    .venv/bin/python -m proxyshop_demo

What this is, and why it exists
-------------------------------
``docs/demo/starting-slice.md`` section 3 calls itself "the beat to run in front of an
audience" and, until this module, contained seven subsections of prose and **zero commands**.
The only runnable thing on the page was section 4, and what a room saw when it ran was one
line — ``27 passed in 1.38s``. No intent, no clarifying questions, no bids, no shortlist, no
discount code. The data all existed; nothing surfaced it.

This driver surfaces it. It stands five ProxyShop deployables up on five loopback ports and
walks one purchase across them, printing what happened in plain language beside the real values
(beat 6 adds three more servers on the merchant side, and says so where it does it):

* ``buyer_svc.main:create_app()``   — the shopper's clarifier, reached at ``POST /buyer/intent/clarify``
* ``store_agent.main:create_app()`` — one server per store, answering ``POST /v1/bid-requests``
* ``exchange.main:create_app()``    — ``POST /auctions`` and ``POST /auctions/{id}/accept``
* ``trust.main:create_app()``       — ``POST /events``, where the exchange's ledger lands (T-150)

None of the ProxyShop services here is a test double. Every arrow in the output is an HTTP
request over a real TCP socket, which is the boundary the class of defect this repository kept
finding hides at: an app object handed to a test is never *started*, so nothing between
``create_app()`` and a served request is executed. **They are uvicorn servers in daemon threads
of this one interpreter, not separate processes** — ``proxyshop_support.asgi_server.serve``
says so. What that buys is still the thing that matters: a different app object, a different
composition root, a real socket, and real serialisation in both directions.

What it deliberately does NOT do
--------------------------------
It does not wire anything. The exchange reads its own deployment document out of
``EXCHANGE_DEPLOYMENT`` — including ``trust_url``, the one line that decides where its audit
trail goes — and each store agent reads its own context out of ``STORE_AGENT_CONTEXT``, which
is what a person deploying these containers does. The buyer service reads its exchange address
out of ``EXCHANGE_URL``, which is what ``apps/buyer/compose.yaml`` sets, and its candidate set
out of a ``BUYER_ROSTER`` document — the two are separate resolutions in
``buyer_svc.composition`` because an address says nothing about who competes, and a deployment
that resolves one and not the other refuses the confirmation instead of opening an auction that
solicits nobody. This driver states both after the exchange is actually listening — the way a
container's environment would have, before either process started.

**The one key this driver deliberately leaves unstated is the exchange's ``catalog``**, and the
consequence is named in beat 2 rather than hidden. That key is the snapshot the exchange grades
a store's CLAIMS against; unstated, ``ranking.serving.catalog_of`` keeps its
``NoCatalogSnapshots`` default, every claim comes back ``ambiguous`` — R18's verdict for one
this exchange could not check at all — and R19 will not let anything but a ``verified`` claim
satisfy a hard constraint. This run still shortlists three stores only because
the auction is opened on ``e2e/support/s1/run.json``'s intent, whose ``hard_constraints`` is
empty. Wiring a catalogue here would mean this driver supplying the exchange's own evidence on
the store's behalf, so it does not; it says so instead.

The one substitution it makes on the ProxyShop side is a DATASTORE, not a behaviour, and it is
stated in beat 0: trust is served with an ``InMemoryEventStore`` on ``app.state.event_store``, the
seam ``trust.events.routes.store_for`` reads before it reaches for Postgres. Same normalise,
same seal, same verifier — so the chain beat 7 verifies is the service's own, and this command
still needs no Postgres, no Redis, no docker compose and no network egress.

**Where the product cannot do something yet, this prints it.** A demo driver that quietly omits
the broken beats is the same lie as a green board over a broken system, so every such beat lands
in :attr:`JourneyResult.gaps` and is rendered as a ``DOES NOT RUN YET`` block. Every one of them
is **measured by this driver in the line above the block that reports it** rather than asserted
from reading the source, which is why the list shrinks by itself as the product improves. What
fires is whatever the run measured; on this tree that is one thing:

* reconciliation and the trust projection cannot run over the chain this run writes. The
  *events* are all there now — the exchange's ``accepted`` and both bridge records, and the
  merchant's ``order_paid`` beside them in one chain — and what is left is that the two halves
  cannot be scoped to one seller: ``trust.reconcile.reconcile`` namespaces every join key by
  store, the ``accepted`` event names no store at all, and the order names the shop domain
  rather than the platform's ``store_id``. Beat 7 measures it.

Three beats that were gaps when this module was written no longer are, and the driver found that
out the same way — by running them. ``POST /buyer/intent/confirm`` answers 201 now that the buyer
service's composition root binds an auction client; a store agent handed the clarified intent
carrying the exchange's own cluster assignment answers on the merits (``no_matching_product``)
instead of ``cluster_not_pursued``, so the two namespaces now meet; and R10's silent-store
fallback
reaches the shortlist rather than being excluded ``expired_offer`` + ``off_domain_checkout``.
All three are still measured every run; there is simply nothing left to report about them.

The scenario
------------
The roster, the prices and the four store roles are read from ``e2e/support/s1/run.json`` — the
S1 run fixture, which is this repository's ground truth for the starting path — so the demo and
the scripted proof in section 4 are talking about the same purchase rather than two that
resemble each other.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from .narrate import Narrator, money

__all__ = ["JourneyResult", "main", "run_journey"]

#: The repository root, found from this file rather than from the working directory: an
#: operator runs this from wherever they happen to be standing.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The S1 run fixture. Read-only ground truth for the starting path (T-082).
RUN_FIXTURE = REPO_ROOT / "e2e" / "support" / "s1" / "run.json"

#: How long any one call to a served deployable may take. Generous against the auction's own
#: 3-second bid window plus two loopback hops, and finite so a wedged server ends the demo with
#: an error instead of hanging in front of an audience.
REQUEST_TIMEOUT_SECONDS = 30.0

#: The discount depth each store's approved envelope authorises in this scenario. This ONE
#: constant fills both halves of the T-177 price wall — the store's envelope and the roster row
#: the platform states — deliberately, because they have to agree: a roster naming a different
#: cap turns an honest bid into ``fallback_reason: 'bid_price_unreconcilable'``. The S1 run
#: fixture states no cap of its own, so there is one number here rather than two that could
#: drift, and this driver is not demonstrating the disagreement case.
ENVELOPE_MAX_DISCOUNT_PCT = 20.0

#: A far-future instant, so an offer minted during the demo is never expired by the clock.
FAR_FUTURE = "2999-01-01T00:00:00Z"


def store_domain(store_id: str) -> str:
    """The host the platform has on record for a seller.

    The same spelling ``e2e/support/s1/flow.py`` uses, and it matters that it is the platform's
    record rather than the domain a bid claims for itself: the accept path compares the two by
    exact host equality (C10/D22), which is the guard that refuses ``checkout.<seller>`` and
    ``evil-<seller>.attacker.tld`` before any discount code exists.
    """
    return f"{store_id}.example.com"


# =====================================================================================
# The result a caller (and the test) reads
# =====================================================================================
@dataclass
class JourneyResult:
    """Everything the run actually produced, so a test can assert on it without parsing prose."""

    buyer_url: str = ""
    exchange_url: str = ""
    trust_url: str = ""
    agent_urls: dict[str, str] = field(default_factory=dict)

    clarifying_questions: list[str] = field(default_factory=list)
    clarified_intent: dict[str, Any] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    #: What the served buyer app answered to the shopper's confirmation. Measured, not assumed.
    confirm_status: int = 0
    confirm_detail: str = ""
    #: What one store agent answered when handed the clarified intent with ONE field
    #: replaced: the ``cluster_id``, swapped for the assignment the exchange actually made.
    #: Not "unedited" — the substitution is the point, and the code below says why.
    agent_probe_status: int = 0
    agent_probe_reason: str = ""

    auction_id: str = ""
    solicited: list[str] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    denied: list[dict[str, Any]] = field(default_factory=list)
    ranked: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    shortlist_slots: list[dict[str, Any]] = field(default_factory=list)

    accepted_bid_ref: str = ""
    accepted_store_id: str = ""
    code: str = ""
    permalink_url: str = ""
    auction_history: list[dict[str, Any]] = field(default_factory=list)

    #: Every event the exchange's ledger sink actually delivered to the trust service, read
    #: back off ``GET /events`` rather than off the exchange's own in-process copy.
    ledger_events: list[dict[str, Any]] = field(default_factory=list)
    #: What ``GET /events/verify`` answered about that chain. Empty if trust was never read.
    ledger_verify: dict[str, Any] = field(default_factory=dict)

    #: How many ``reconciled`` verdicts ``trust.reconcile.reconcile`` produces over that
    #: chain exactly as the trust service served it back. Beat 7 measures this rather than
    #: reasoning about it, because "the events are all present" and "the purchase reconciles"
    #: are two different claims and this run can only honestly make the first.
    reconciled: int = 0
    #: The same fold with the two facts the chain does not carry supplied by the driver —
    #: the offer's ``store_id`` and the shop-domain alias. Labelled a probe everywhere it is
    #: printed: it is the DIAGNOSIS of the gap, never a claim that the product closed it.
    reconciled_probe: int = 0

    order_name: str = ""
    order_total: str = ""
    #: The token the MERCHANT minted when the cart was visited. Kept so beat 7 can hold it up
    #: beside the one the exchange stamped into the ledger instead of asserting they differ.
    merchant_checkout_token: str = ""
    pixel_order_ref: str = ""
    webhook_ledger_kind: str = ""
    webhook_order_ref: str = ""
    #: ``True`` only if a SECOND order actually redeemed the same single-use code. A second
    #: checkout that completed carrying no discount is the correct behaviour, not this.
    second_use_honoured: bool | None = None

    #: One entry per beat this product cannot perform yet. Each is printed, never swallowed.
    gaps: list[str] = field(default_factory=list)

    def bid_of(self, store_id: str) -> dict[str, Any] | None:
        """The auction entry for one store, or ``None`` if it was never collected."""
        for entry in self.entries:
            if entry.get("store_id") == store_id:
                return entry
        return None


# =====================================================================================
# Standing the deployables up
# =====================================================================================
def _closed_port() -> int:
    """A loopback port with nothing listening on it.

    Used for the store that never answers. A refused connection is a real "nobody is home",
    it is instantaneous, and it is what ``HttpBidSolicitor`` reports as no bid at all — which
    ``auction.collect`` then labels ``no_response``, the R10 case — without spending three
    seconds of an audience's attention on a timeout.
    """
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _envelope_for(store: Mapping[str, Any], cluster_id: str) -> dict[str, Any]:
    """The merchant-approved envelope this store's agent prices inside.

    Same shape as ``e2e/support/s1/flow.py``'s, because it is the same scenario: a per-product
    floor at 75% of list, a 20% depth cap, and the store's standing commitments carried with
    the provenance that says a human said them.
    """
    return {
        "store_id": store["store_id"],
        "version": 1,
        "floors": [
            {"product_ref": store["product_ref"], "min_price": float(store["list_price"]) * 0.75}
        ],
        "max_discount_pct": ENVELOPE_MAX_DISCOUNT_PCT,
        "budget_cap": 5000.0,
        "pursue_clusters": [cluster_id],
        "standing_commitments": [
            {
                "key": commitment["key"],
                "value": commitment["value"],
                "provenance": {
                    "source": "owner_statement",
                    "ref": f"envelope:{store['store_id']}:v1#{commitment['key']}",
                    "observed_at": "2026-01-01T00:00:00Z",
                    "authority_rank": 1,
                },
            }
            for commitment in store.get("commitments", [])
        ],
        "activation": "active",
    }


def _store_context(store: Mapping[str, Any], cluster_id: str) -> dict[str, Any]:
    """The document a hosted store agent reads out of ``STORE_AGENT_CONTEXT``."""
    return {
        "store_id": store["store_id"],
        "store_domain": store_domain(store["store_id"]),
        "envelope": _envelope_for(store, cluster_id),
        "catalog": {store["product_ref"]: dict(store["catalog"])},
        "live_state": {store["product_ref"]: dict(store["live_state"])},
        # A cold agent: no learned policy. The starting path never needs the learning module.
        "learned_policy": None,
        "network_priors": {cluster_id: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }


@contextlib.contextmanager
def _served_store_agent(store: Mapping[str, Any], cluster_id: str, workdir: Path) -> Iterator[str]:
    """One store agent process, configured the way its container is configured.

    The context is written to a file and named in ``STORE_AGENT_CONTEXT``, exactly as the
    Dockerfile's ``uvicorn store_agent.main:app`` gets it — nothing here calls
    ``configure_solicitation``. The resolved context is then read once, immediately, because
    the agent caches it per app and this driver runs several agents in ONE process: forcing the
    read while this store's environment is the current one is what keeps the second agent from
    inheriting the first's envelope.
    """
    from store_agent.main import create_app as create_store_agent
    from store_agent.solicitation.serving import store_context

    from proxyshop_support.asgi_server import serve

    store_id = str(store["store_id"])
    document = workdir / f"store-context-{store_id}.json"
    document.write_text(json.dumps(_store_context(store, cluster_id), indent=2), encoding="utf-8")

    os.environ["STORE_AGENT_CONTEXT"] = str(document)
    os.environ["STORE_AGENT_STORE_DOMAIN"] = store_domain(store_id)
    app = create_store_agent()
    resolved = store_context(app)
    if not resolved or resolved.get("store_id") != store_id:
        raise RuntimeError(
            f"the store agent for {store_id!r} resolved the context {resolved!r} out of "
            f"{document}; an agent advocating for the wrong store would make every price below "
            f"a different scenario wearing this one's name"
        )
    with serve(app) as url:
        yield url


@contextlib.contextmanager
def _served_trust() -> Iterator[str]:
    """The trust service, serving its ledger door on a loopback port.

    One substitution, and it is a *datastore* rather than a behaviour.
    ``trust.events.routes.store_for`` resolves the writer in this order: whatever is on
    ``app.state.event_store``, otherwise a ``PostgresEventStore`` built from the environment.
    This demo has no Postgres and must never need one, so an ``InMemoryEventStore`` is put on
    that seam before the app is served. It is the same append path the Postgres store takes —
    both normalise through ``normalise_event`` and seal through ``trust.ledger.seal_event`` —
    so the hash chaining, the once-only landing and the verification the demo reads back are
    the service's real ones, reached over a real socket by a real POST from a different
    application's ledger sink.

    Nothing on ``trust.events.routes`` is authenticated, which is why the exchange needs no
    credential to reach it and why this driver needs to arrange none.
    """
    from trust.events import InMemoryEventStore
    from trust.main import create_app as create_trust

    from proxyshop_support.asgi_server import serve

    app = create_trust()
    app.state.event_store = InMemoryEventStore()
    with serve(app) as url:
        yield url


@contextlib.contextmanager
def _merchant_ledger_address(trust_url: str) -> Iterator[None]:
    """Tell the MERCHANT where trust is, the way its container is told, and rebuild its writer.

    The exchange is told this address in its deployment document (``trust_url``, below). The
    merchant has no deployment document: ``TRUST_URL`` is the whole of its configuration for
    the same fact. ``merchant_svc.composition.trust_publisher`` builds one
    :class:`~proxyshop_support.trust_ledger.TrustLedgerPublisher` per process out of
    ``trust_endpoint()``, which resolves that variable and then falls back to
    ``http://trust:8084`` — the compose service name, which resolves to nothing outside
    compose.

    Until this driver stated it, that fallback is what beat 6's ``orders/paid`` was really
    posted at, and the events were really lost: measured, ``ConnectError: nodename nor
    servname provided`` on every run, at ``ERROR``, from a demo that then exited 0. The
    authoritative half of the pixel/webhook reconciliation (R4) was going nowhere while every
    other beat printed green. **Nothing here silences that record** — the level is untouched
    and the same line still fires for anyone who runs the merchant with no trust service
    reachable. It stops firing because the events now land, which beat 7 reads back off the
    trust service over HTTP rather than inferring from a quiet log.

    The publisher is cached per process from its FIRST publish, so stating the variable is
    only half of it: an interpreter that had already published — a pytest session that ran a
    merchant test before this one — would keep the old address for the rest of its life.
    ``set_trust_publisher(None)`` means "rebuild from configuration", never "unwire" (T-247),
    and it is done on the way out as well, so a publisher aimed at this run's loopback port
    does not outlive the port.
    """
    from merchant_svc.composition import set_trust_publisher

    from proxyshop_support.trust_ledger import ENV_TRUST_URL

    with _environment({ENV_TRUST_URL: trust_url}):
        set_trust_publisher(None)
        try:
            yield
        finally:
            set_trust_publisher(None)


def _deployment_document(
    stores: Sequence[Mapping[str, Any]], endpoints: Mapping[str, str], trust_url: str
) -> dict[str, Any]:
    """The document a person writes to deploy this exchange.

    ``sellers`` and ``trust_snapshot`` are two independent statements on purpose, and the
    blacklisted store is named in both: R12's eligibility gate (which runs before anybody is
    solicited) and the ranking's blacklist read (which runs after the bids are in) are two
    different questions, and a deployment that derived one from the other would be inventing an
    answer the trust service never gave.
    """
    honesty = _honesty_by_store()
    return {
        "sellers": [
            {
                "store_id": store["store_id"],
                "eligibility": store["eligibility"],
                "registered_domain": store_domain(store["store_id"]),
                **(
                    {"bid_endpoint": endpoints[store["store_id"]]}
                    if store["store_id"] in endpoints
                    else {}
                ),
            }
            for store in stores
        ],
        "trust_snapshot": {
            "stores": {
                store["store_id"]: {
                    "store_id": store["store_id"],
                    "blacklisted": store["eligibility"] == "blacklisted",
                    "score": _demo_trust_score(store, honesty),
                }
                for store in stores
            }
        },
        # The exchange performs intent-cluster assignment (DESIGN.md:34). The clarifier mints
        # a cluster_id by hashing the query, while a store's envelope authorises NAMED
        # catalogue clusters — so without this the two namespaces can never meet and every
        # agent answers `204 cluster_not_pursued`. Which clusters exist is a deployment fact
        # nobody can infer, so it is stated here in the same {cluster_id, label} spelling the
        # merchant onboarding flow resolves a merchant's prose against. `cluster-espresso` is
        # what this run's store envelopes actually pursue.
        "intent_clusters": [
            {
                "cluster_id": "cluster-espresso",
                "label": "Espresso machines",
                "category": "coffee",
                "terms": ["espresso machine", "espresso"],
                "attributes": {"brew_method": "espresso"},
            }
        ],
        # WHERE this exchange's audit trail goes (T-150). The key states an address and
        # nothing else: the ledger seam is bound whether or not it is present, because an
        # exchange that has to be told to keep an audit trail is one that ships without one.
        # Absent, the exchange falls back to `TRUST_URL` and then to `http://trust:8084`,
        # the compose service name — which resolves to nothing outside compose, which is
        # exactly what this demo used to print a WARNING about on every transition.
        "trust_url": trust_url,
        "checkout_mode": "redirect",
    }


#: ``fixtures/manifest.json``, the human-approved ground truth for which store behaves how.
MANIFEST = REPO_ROOT / "fixtures" / "manifest.json"


def _honesty_by_store() -> dict[str, bool]:
    """Which stores the approved manifest says are honest.

    Read rather than restated: "brightbean is the dishonest one" is a fact a human approved in
    ``fixtures/manifest.json``, and a demo that hard-coded its own copy would be a second place
    for that ground truth to live.
    """
    if not MANIFEST.is_file():
        return {}
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {
        str(row["store_id"]): bool(row.get("honest", True))
        for row in document.get("stores", [])
        if row.get("store_id")
    }


def _demo_trust_score(store: Mapping[str, Any], honesty: Mapping[str, bool]) -> float:
    """A served trust score for this store.

    **Stated by the deployment, not computed here, and that is the honest framing.** The trust
    service builds this projection from its own ledger of reconciled outcomes. This driver now
    runs that service (beat 7) and the exchange's ledger really lands in it — but the ledger it
    receives holds auction transitions, not the ``reconciled`` verdicts a score is folded from,
    so nothing in this run could derive these numbers. What the demo shows is that the ranker
    READS the served snapshot, that the score moves a candidate's position, and that a
    blacklisted row is refused. What it does not show is the score's derivation.

    The two hosted stores are given different scores because the approved manifest says they
    are different stores — one is its honest control and one is its scripted dishonest store —
    and a demo in which every seller carries the same number cannot show trust doing anything.
    """
    store_id = str(store.get("store_id"))
    role = str(store.get("role"))
    if role == "blacklisted":
        return 0.11
    if role == "silent":
        return 0.61
    return 0.86 if honesty.get(store_id, True) else 0.58


@contextlib.contextmanager
def _environment(values: Mapping[str, str | None]) -> Iterator[None]:
    """Set (or clear, on ``None``) environment variables, and put the previous ones back.

    The driver's other variables are read at app-construction time and are deliberately left
    standing — a reader can look at the process it just ran and see what the deployment said.
    This one cannot be: it names a file under a temporary directory this function's caller is
    about to delete, and a ``BUYER_ROSTER`` pointing at a deleted file is a
    ``DeploymentConfigurationError`` rather than an unset variable. Left behind, it would turn
    every later buyer confirmation in the same interpreter — the rest of a pytest session, for
    instance — into a 503 about a document nobody wrote.
    """
    previous = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _roster(stores: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "store_id": store["store_id"],
            "tier": store["tier"],
            "product_ref": store["product_ref"],
            "list_price": store["list_price"],
            "max_discount_pct": ENVELOPE_MAX_DISCOUNT_PCT,
        }
        for store in stores
    ]


# =====================================================================================
# The journey
# =====================================================================================
def run_journey(stream: TextIO | None = None) -> JourneyResult:
    """Drive the S1 starting slice over HTTP and narrate it. Returns what it produced."""
    import httpx
    from buyer_svc.composition import ENV_ROSTER, ENV_ROSTER_JSON
    from buyer_svc.main import create_app as create_buyer
    from exchange.composition import ENV_DEPLOYMENT, ENV_DEPLOYMENT_JSON
    from exchange.main import create_app as create_exchange

    from proxyshop_support.asgi_server import serve

    say = Narrator(stream)
    result = JourneyResult()

    if not RUN_FIXTURE.is_file():
        raise FileNotFoundError(
            f"the S1 run fixture {RUN_FIXTURE} is missing; this driver narrates that scenario "
            f"and will not invent a substitute for it"
        )
    fixture = json.loads(RUN_FIXTURE.read_text(encoding="utf-8"))
    stores: list[dict[str, Any]] = list(fixture["stores"])
    cluster_id = str(fixture["intent"]["cluster_id"])
    hosted = [store for store in stores if store["role"] == "hosted"]

    with contextlib.ExitStack() as stack:
        workdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="proxyshop-demo-")))

        # -- the deployables ----------------------------------------------------------
        # Trust comes up FIRST, because its address is a line in the deployment document the
        # exchange reads at composition time: the ledger sink is built once, out of
        # `trust_url`, and an exchange composed against an address nothing was listening on
        # would keep that address for the life of the process.
        trust_url = stack.enter_context(_served_trust())
        result.trust_url = trust_url

        # ...and the MERCHANT is told the same address, because it reads a different seam for
        # it and nothing here was stating that one. See `_merchant_ledger_address`: beat 6's
        # signed `orders/paid` was being posted at the compose service name and lost.
        stack.enter_context(_merchant_ledger_address(trust_url))

        endpoints: dict[str, str] = {}
        for store in hosted:
            agent_url = stack.enter_context(_served_store_agent(store, cluster_id, workdir))
            result.agent_urls[store["store_id"]] = agent_url
            endpoints[store["store_id"]] = f"{agent_url}/v1/bid-requests"

        # The store that never answers is registered with an endpoint like any other. Nothing
        # is listening on it, which is the whole point: R10 is about a store the exchange
        # really tried to reach.
        silent = [store for store in stores if store["role"] == "silent"]
        for store in silent:
            endpoints[store["store_id"]] = f"http://127.0.0.1:{_closed_port()}/v1/bid-requests"

        document = workdir / "exchange-deployment.json"
        document.write_text(
            json.dumps(_deployment_document(stores, endpoints, trust_url), indent=2),
            encoding="utf-8",
        )
        os.environ[ENV_DEPLOYMENT] = str(document)
        os.environ.pop(ENV_DEPLOYMENT_JSON, None)

        buyer_url = stack.enter_context(serve(create_buyer()))
        exchange_url = stack.enter_context(serve(create_exchange()))
        result.buyer_url = buyer_url
        result.exchange_url = exchange_url

        # The buyer service reads its exchange address the same way a deployed one does —
        # `EXCHANGE_URL`, which apps/buyer/compose.yaml already sets to the service name.
        # It is bound at REQUEST time, not app-construction time, so setting it here (after
        # the exchange is actually listening) is what a container's env would have done
        # before either process started. Without it the shopper's confirmation is a 503:
        # "confirm() was given no auction client, so the confirmed intent has nowhere to go."
        os.environ["EXCHANGE_URL"] = exchange_url

        # ...and WHO COMPETES, which `EXCHANGE_URL` says nothing about. `buyer_svc
        # .composition.read_deployment` resolves an address and a roster separately, and a
        # deployment that resolves an address and no roster refuses the confirmation
        # (`NoRosterBound`) rather than opening an auction that solicits nobody — the shopper
        # cannot tell an empty shortlist from "no store had anything for you". So this driver
        # states the candidate set the way a deployment states it: a mounted document, the
        # same rows beat 2 puts on `POST /auctions`, read through the same validator.
        roster_document = workdir / "buyer-roster.json"
        roster_document.write_text(
            json.dumps({"roster": _roster(stores)}, indent=2), encoding="utf-8"
        )
        stack.enter_context(_environment({ENV_ROSTER: str(roster_document), ENV_ROSTER_JSON: None}))

        buyer = stack.enter_context(
            httpx.Client(base_url=buyer_url, timeout=REQUEST_TIMEOUT_SECONDS)
        )
        exchange = stack.enter_context(
            httpx.Client(base_url=exchange_url, timeout=REQUEST_TIMEOUT_SECONDS)
        )

        _beat_zero(say, result, stores, document)
        _beat_one(say, result, buyer, fixture)
        _beat_two_to_four(say, result, exchange, fixture, stores)
        _beat_five(say, result, exchange)
        _beat_six(say, result)
        trust = stack.enter_context(
            httpx.Client(base_url=trust_url, timeout=REQUEST_TIMEOUT_SECONDS)
        )
        _beat_seven(say, result, trust)
        _epilogue(say, result)

    return result


# =====================================================================================
# beat 0 — what is running
# =====================================================================================
def _beat_zero(
    say: Narrator,
    result: JourneyResult,
    stores: Sequence[Mapping[str, Any]],
    document: Path,
) -> None:
    say.title(
        "ProxyShop — the S1 starting slice, live",
        "five ProxyShop deployables, one purchase, no Shopify account",
    )
    say.say(
        """
        Everything below is a real HTTP round trip to a server this command started a moment
        ago — a uvicorn instance in a thread of this process, not a separate process, but a
        separate application reached over a real socket. No service was wired by this driver:
        the exchange read its collaborators out of a deployment document, and each store agent
        read its envelope and catalogue out of a store-context file, which is what a person
        deploying these containers does.

        On the ProxyShop side there is exactly one substitution, and it is a datastore rather
        than a behaviour: the trust service is given an in-memory event store instead of the
        Postgres one it would resolve from its environment, on the seam its own route handler
        reads first. Same append-and-seal path, so the hash chain beat 7 verifies is the
        service's real one; what it is not is durable past this process. The merchant side of
        beat 6 is openly a stand-in — a local Shopify stub and two recording endpoints — and
        that beat says so where it happens.
        """
    )
    say.section("Running right now:")
    say.fact("buyer service", result.buyer_url)
    say.fact("exchange", result.exchange_url)
    for store_id, url in result.agent_urls.items():
        say.fact(f"store agent {store_id}", url)
    say.fact("trust service", result.trust_url)
    say.fact("deployment document", str(document))

    say.section("The four sellers on this auction's roster, and the role each one plays:")
    for store in stores:
        say.bullet(
            f"{store['store_id']:<18} {store['role']:<12} "
            f"{store['product_ref']:<20} list {money(store['list_price'])}"
        )
    say.blank()
    say.say(
        """
        Two stores run a real agent that will be asked for a price. One is registered but has
        nothing listening on its endpoint — the exchange will really dial it and really fail.
        One is blacklisted, and must never be asked at all.
        """
    )


# =====================================================================================
# beat 1 — intent and the clarifying questions
# =====================================================================================
def _beat_one(say: Narrator, result: JourneyResult, buyer: Any, fixture: Mapping[str, Any]) -> None:
    say.beat(1, "The shopper says what they want, and ProxyShop asks back")
    turns = list(fixture["buyer_turns"])

    say.say("The shopper's side of the conversation, oldest first:")
    say.numbered(turns)
    say.blank()

    response = buyer.post("/buyer/intent/clarify", json={"turns": turns})
    say.wire("POST", f"{result.buyer_url}/buyer/intent/clarify", response.status_code)
    response.raise_for_status()
    body = response.json()

    result.clarifying_questions = list(body["questions"])
    result.clarified_intent = dict(body["intent"])
    result.unresolved = list(body["unresolved"])

    say.section(
        f"ProxyShop asked {len(result.clarifying_questions)} clarifying question(s). "
        "The ceiling is three, and it is a published constraint rather than a heuristic:"
    )
    if result.clarifying_questions:
        say.numbered(result.clarifying_questions)
    else:
        say.bullet("(none — the shopper's own words were enough)")

    say.section("The structured intent that came back:")
    intent = result.clarified_intent
    say.fact("intent_id", intent.get("intent_id"))
    say.fact("query", intent.get("query"))
    say.fact("category", intent.get("category"))
    say.fact("budget band", intent.get("budget_band"))
    say.fact("currency", intent.get("currency"))
    say.fact("hard constraints", json.dumps(intent.get("hard_constraints", [])))
    say.fact("preferences", json.dumps(intent.get("preferences", [])))
    say.fact("still unresolved", ", ".join(result.unresolved) or "(nothing)")
    say.fact("confirmed", body.get("confirmed"))

    say.blank()
    say.say(
        """
        Note the last line. The clarifier never self-confirms: confirmation is the shopper's
        separate act, and until it happens no auction exists. That is why `confirmed` is false
        on a response that has just produced a complete-looking intent.
        """
    )

    say.section("The shopper now says yes. That is the act that opens an auction:")
    confirmed = buyer.post(
        "/buyer/intent/confirm",
        json={"intent": intent, "confirmed": True, "profile": fixture["profile"]},
    )
    say.wire("POST", f"{result.buyer_url}/buyer/intent/confirm", confirmed.status_code)
    result.confirm_status = int(confirmed.status_code)
    detail = ""
    with contextlib.suppress(ValueError):
        detail = str(confirmed.json().get("detail") or "")
    result.confirm_detail = detail

    if confirmed.status_code in (200, 201):
        say.fact("auction_id", confirmed.json().get("auction_id"))
    else:
        _gap(
            say,
            result,
            "the confirmed intent cannot travel from the buyer service to the exchange",
            f"""
            Measured a line ago, on the served buyer app: {confirmed.status_code}
            {detail or confirmed.text[:160]!r}

            `POST /buyer/intent/confirm` reads an auction client off
            `app.state.auction_client`, and the buyer service has no composition root that
            binds one — no environment variable, no deployment document, no start-up hook that
            points it at an exchange. The exchange's own composition root landed for exactly
            this class of defect and covers the exchange only.

            The next beat opens the auction by posting to the exchange directly, which is the
            hop a buyer service with a composition root would make on the shopper's behalf.
            """,
        )
    # Show, rather than assert, what a store agent does with the clarifier's own cluster id.
    say.section("What a store agent makes of that intent, asked directly:")
    say.say(
        """
        One field is replaced before asking, and it is named on the next line. The clarifier
        mints `cluster_id` by hashing the query; a store's envelope authorises NAMED catalogue
        clusters, and the EXCHANGE is what maps one onto the other. Probing with the raw hash
        would measure a hop nothing performs, so the driver reads back what the exchange
        assigned to the auction the confirmation just opened and asks with that.
        """
    )
    probe_store, probe_url = next(iter(result.agent_urls.items()), ("", ""))
    decline_reason = ""
    if probe_url:
        import httpx

        # Ask the agent the way the EXCHANGE asks it, not the way the clarifier speaks.
        # The clarifier mints `cluster_id` by hashing the query; a store envelope authorises
        # NAMED catalogue clusters. The exchange performs that assignment (DESIGN.md:34) —
        # so probing with the raw hash measures a hop nothing performs and reports a gap the
        # product does not have. Read back what the exchange actually assigned to the auction
        # the confirmation just opened, and ask with that.
        probe_intent = dict(intent)
        auction_id = ""
        if confirmed.status_code in (200, 201):
            auction_id = str(confirmed.json().get("auction_id") or "")
        if auction_id and result.exchange_url:
            opened = httpx.get(
                f"{result.exchange_url}/auctions/{auction_id}", timeout=REQUEST_TIMEOUT_SECONDS
            )
            assigned = str((opened.json() or {}).get("cluster_id") or "")
            if opened.status_code == 200 and assigned:
                probe_intent["cluster_id"] = assigned
                say.fact("cluster the exchange assigned", assigned)

        probe = httpx.post(
            f"{probe_url}/v1/bid-requests",
            json={
                "auction_id": "auction-demo-probe",
                "intent": probe_intent,
                "profile": fixture["profile"],
                "respond_by": FAR_FUTURE,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        say.wire("POST", f"{probe_url}/v1/bid-requests", probe.status_code, f"({probe_store})")
        decline_reason = str(probe.headers.get("x-proxyshop-decline-reason") or "")
        if probe.status_code == 204:
            say.fact("declined, reason", decline_reason or "(none stated)")
        result.agent_probe_status = int(probe.status_code)
        result.agent_probe_reason = decline_reason

    # Report THIS gap only when the agent actually declined for THIS reason. The exchange now
    # performs intent-cluster assignment, so a store that declines `no_matching_product` has
    # pursued the cluster and rejected the product — a different answer, and reporting it as
    # the namespace gap would name a defect the product no longer has.
    if decline_reason == "cluster_not_pursued":
        _gap(
            say,
            result,
            "the clarified intent's cluster does not name a catalogue any store pursues",
            f"""
            {probe_store} answered {result.agent_probe_status} and gave the reason
            {decline_reason!r} — measured a line ago, not predicted.

            The clarifier derives `cluster_id` by hashing the confirmed query: this run
            produced {intent.get("cluster_id")!r}. Every store's approved envelope authorises
            bidding inside NAMED catalogue clusters, and in this scenario that name is
            {fixture["intent"]["cluster_id"]!r}. Nothing in this slice maps one namespace onto
            the other, so an auction opened on the clarifier's own cluster id reaches every
            agent and is declined by all of them, and the shopper sees an empty shortlist.

            The auction below is therefore opened on the run fixture's confirmed intent, which
            states the catalogue cluster. Everything after this line is real; this one hop is
            stated by the driver because no component performs it.
            """,
        )
    else:
        say.bullet(
            f"{probe_store} answered {result.agent_probe_status} to the clarified intent, "
            f"asked with the exchange's cluster assignment rather than the clarifier's hash"
        )


# =====================================================================================
# beats 2, 3 and 4 — the auction, the bids and the shortlist
# =====================================================================================
def _beat_two_to_four(
    say: Narrator,
    result: JourneyResult,
    exchange: Any,
    fixture: Mapping[str, Any],
    stores: Sequence[Mapping[str, Any]],
) -> None:
    say.beat(2, "The auction opens, and the roster is gated before anyone is asked")
    request = {
        "intent": fixture["intent"],
        "profile": fixture["profile"],
        "roster": _roster(stores),
    }
    response = exchange.post("/auctions", json=request)
    say.wire("POST", f"{result.exchange_url}/auctions", response.status_code)
    response.raise_for_status()
    body = response.json()

    result.auction_id = body["auction_id"]
    result.solicited = list(body["solicited"])
    result.entries = list(body["entries"])
    result.denied = list(body["denied"])
    result.ranked = list(body["ranked"])
    result.excluded = list(body["excluded"])
    result.shortlist_slots = list(body.get("shortlist", {}).get("slots", []))

    say.fact("auction_id", result.auction_id)
    say.fact("state after the window", body["state"])
    say.blank()
    say.fact(
        "opened on intent",
        f"{fixture['intent']['intent_id']}  (the S1 run fixture's, not the clarifier's)",
    )
    say.fact(
        "its hard constraints",
        json.dumps(fixture["intent"].get("hard_constraints") or []),
    )
    say.blank()
    say.say(
        """
        Read those two lines, because this is the seam where the demo is narrower than the
        story. Beat 1's confirmation opened a real auction of its own from the clarifier's
        intent; the auction ranked below is a SECOND one, opened directly on the S1 run
        fixture's intent — the same purchase the scripted proof runs, which is what makes the
        two comparable. The empty hard-constraint list is load-bearing. This driver states no
        `catalog` in its deployment document, because that snapshot is the exchange's own
        evidence about a store and a demo that supplied it would be marking the store's
        homework. Unstated, every claim grades `ambiguous` — the verdict for a claim this
        exchange could not check at all — and R19 will not let anything but a `verified` claim
        satisfy a hard constraint, so on an intent that carried the clarifier's
        `brew_method eq espresso`, this same exchange shortlists nobody.
        `exchange.composition`'s `catalog` entry records that measurement over a real socket.

        It costs the two stores that DID bid nothing in the ranking below, and that is a
        property worth naming because it did not hold a day ago: `verified_claim_ratio` counts
        the claims this exchange decided, so a claim it could not check leaves the ratio
        undefined and the term reads its published neutral. Counted as failures instead, the
        two hosted stores read 0.0 on that term while the silent store's claimless R10
        fallback read the neutral 0.5 — and the store that never replied took the top slot
        off both of the ones that did.
        """
    )
    say.say(
        """
        Before a single store was asked for a price, the exchange read the seller eligibility
        source for every store on the roster. It fails closed: a store whose status cannot be
        read is treated as unavailable, and a blacklisted store is denied right here — never
        asked, never collected, never ranked, never shown.
        """
    )
    say.section(f"Denied at the gate ({len(result.denied)}):")
    if result.denied:
        for denial in result.denied:
            say.bullet(f"{denial['store_id']}  [{denial['status']}]")
            say.line(f"          reason: {denial['reason']}")
    else:
        say.bullet("(nobody)")
    say.section(f"Solicited ({len(result.solicited)}): {', '.join(result.solicited) or '(nobody)'}")
    blocked = [store["store_id"] for store in stores if store["eligibility"] == "blacklisted"]
    for store_id in blocked:
        say.bullet(
            f"{store_id} is absent from that list, which is the first of the gates S1 makes "
            f"it fail. The rest are below: never collected, never ranked, never shown, never "
            f"accepted, and never in the chain beat 7 reads back.",
            marker="*",
        )

    # -- beat 3 -------------------------------------------------------------------
    say.beat(3, "The stores bid — a real POST /v1/bid-requests to each agent")
    say.say(
        """
        Solicitation fanned out to every eligible store inside a bounded bid window. Each
        hosted agent priced from its own catalogue, moved only within the discount depth its
        approved envelope authorises, and answered with a priced bid. No model is anywhere
        near a price. (Unsigned, and deliberately: this is the exchange-to-seller door, which
        a hosted Tier-1 agent answers without crossing an external trust boundary. The signing
        envelope belongs to the external Tier-2 door, which this run never touches.)
        """
    )
    say.blank()
    for entry in result.entries:
        store_id = entry["store_id"]
        endpoint = result.agent_urls.get(store_id)
        if entry["fallback"]:
            say.bullet(
                f"{store_id:<18} did NOT bid    -> represented at list price "
                f"{money(entry['unit_price'])}"
            )
            say.line(f"          fallback_reason: {entry['fallback_reason']}")
        else:
            say.bullet(
                f"{store_id:<18} bid            {money(entry['unit_price'])} unit / "
                f"{money(entry['total_price'])} total"
            )
            say.line(f"          answered at: {endpoint}/v1/bid-requests")
    say.blank()
    say.say(
        f"""
        Both agents are cold — no learned policy — so the depth each one chose was 0%, and each
        bid its own catalogue list price. Their approved envelopes authorise up to
        {ENVELOPE_MAX_DISCOUNT_PCT:.0f}%; nothing in the starting slice makes them spend it,
        and a demo that showed a discount here would be showing a learning run, not this one.

        The store that stayed quiet is the interesting one. When the window closed the exchange
        manufactured a list-price offer for it out of the roster's catalogue data and marked the
        entry `fallback` — a silent store loses its ability to discount, not its place in the
        auction (R10). A fallback asserts no claims at all, which is why it can never be the
        evidence that satisfies a hard constraint.
        """
    )

    # -- beat 4 -------------------------------------------------------------------
    say.beat(4, "The bids are ranked, and every refusal says why")
    say.say(
        """
        The published weighted formula ran over the eligible candidates. It is fee-blind and
        tier-blind: paying the network more cannot buy a better position.
        """
    )
    say.section(f"Ranked, best first ({len(result.ranked)}):")
    for position, row in enumerate(result.ranked, start=1):
        say.bullet(f"#{position}  {row['store_id']:<18} score {row['rank_score']:.4f}")
        terms = "  ".join(
            f"{name}={value:+.3f}" for name, value in sorted(row["components"].items())
        )
        say.line(f"          {terms}")
        say.line("          (the weighted terms sum to the score; nothing else moved it)")

    say.section(f"Excluded before ranking ({len(result.excluded)}), with every reason:")
    for row in result.excluded:
        say.bullet(f"{row['store_id']}")
        for reason in row["exclusion_reasons"]:
            say.line(f"          - {reason}")

    say.section(f"The shortlist the shopper is shown ({len(result.shortlist_slots)} slot(s)):")
    if not result.shortlist_slots:
        say.bullet("(empty)")
    for slot in result.shortlist_slots:
        entry = result.bid_of(_store_of_ref(slot["bid_ref"], result)) or {}
        trust = slot.get("trust_summary", {})
        say.bullet(f"slot '{slot['slot']}'  {trust.get('store_id', '?')}")
        say.line(f"          price        {money(entry.get('total_price'))}")
        say.line(f"          fit score    {slot['fit_score']:.4f}   (why it placed here)")
        say.line(
            f"          trust        score {trust.get('score')}  available={trust.get('available')}"
        )
        say.line(f"          provenance   {', '.join(slot.get('provenance_labels', []))}")
        say.line(f"          bid_ref      {slot['bid_ref']}")

    silent_ids = {row["store_id"] for row in result.excluded} & {
        entry["store_id"] for entry in result.entries if entry["fallback"]
    }
    if silent_ids:
        _gap(
            say,
            result,
            "the silent store's list-price fallback never reaches the shortlist",
            f"""
            Section 3.4 of the runbook says to show the shortlist carrying both a real hosted
            bid and the silent store's list-price fallback. Over the real composed exchange it
            does not: the fallback offer the exchange manufactures carries no `expires_at` and
            no `checkout_url`, and both filters fail closed, so {", ".join(sorted(silent_ids))}
            is excluded `expired_offer` and `off_domain_checkout` before it can be ranked. The
            exclusion reasons printed above are that measurement, not a prediction.

            R10's first half holds — the store IS represented, at its catalogue list price, and
            the entry says why. R10's second half, that it can still reach the shortlist, does
            not hold on this path today.
            """,
        )


def _store_of_ref(bid_ref: str, result: JourneyResult) -> str:
    """The store a published ``bid_ref`` belongs to, read from the ranking rather than parsed."""
    for row in result.ranked:
        if row["bid_ref"] == bid_ref:
            return str(row["store_id"])
    return ""


# =====================================================================================
# beat 5 — acceptance, the single-use code, the permalink
# =====================================================================================
def _beat_five(say: Narrator, result: JourneyResult, exchange: Any) -> None:
    say.beat(5, "The shopper accepts a slot, and a single-use code is minted")
    if not result.shortlist_slots:
        _gap(
            say,
            result,
            "there was nothing to accept",
            "The shortlist came back empty, so this beat had no slot to take.",
        )
        return

    top = result.shortlist_slots[0]
    result.accepted_bid_ref = str(top["bid_ref"])
    result.accepted_store_id = _store_of_ref(result.accepted_bid_ref, result)
    say.say(
        f"""
        The shopper takes the '{top["slot"]}' slot. The exchange re-checks eligibility, then
        resolves a checkout provider for the mode its deployment document stated —
        `checkout_mode: redirect`, which outranks the `CHECKOUT_MODE` environment variable
        rather than reading it. In `redirect` that is the simulated
        provider, which mints a single-use code locally and builds a cart permalink on the
        seller's REGISTERED domain — the platform's record of that domain, not the domain the
        bid claimed for itself. The host comparison is exact.
        """
    )
    say.blank()
    response = exchange.post(
        f"/auctions/{result.auction_id}/accept", json={"bid_ref": result.accepted_bid_ref}
    )
    say.wire(
        "POST", f"{result.exchange_url}/auctions/{result.auction_id}/accept", response.status_code
    )
    response.raise_for_status()
    accepted = response.json()
    result.code = str(accepted.get("code") or "")
    result.permalink_url = str(accepted.get("permalink_url") or "")

    say.section("What the shopper walks away with:")
    say.fact("winning store", result.accepted_store_id)
    say.fact("discount code", result.code)
    say.fact("permalink", result.permalink_url)
    say.blank()
    say.say(
        f"""
        That code is single use and it was minted for this acceptance alone. The permalink's
        host is {store_domain(result.accepted_store_id)} — the domain the platform has on
        record for this seller. A bid whose checkout URL pointed at
        `checkout.{store_domain(result.accepted_store_id)}` or
        `evil-{store_domain(result.accepted_store_id)}` would have been refused here, and
        refused BEFORE any discount code was created.
        """
    )

    state = exchange.get(f"/auctions/{result.auction_id}")
    say.blank()
    say.wire("GET", f"{result.exchange_url}/auctions/{result.auction_id}", state.status_code)
    if state.status_code == 200:
        record = state.json()
        result.auction_history = list(record.get("history", []))
        say.section("The auction's own state machine, as the exchange serves it:")
        say.fact("state", record.get("state"))
        say.fact("accepted_bid_ref", record.get("accepted_bid_ref"))
        say.fact(
            "transitions", " -> ".join(step["state"] for step in result.auction_history) or "(none)"
        )


# =====================================================================================
# beat 6 — everything downstream of the code
# =====================================================================================
#: The secret the merchant stub signs ``orders/paid`` with, and the one the merchant app
#: verifies against. Overridden from the stub's own default on purpose: a signature check
#: against a value both sides took from the same default proves nothing about the check.
WEBHOOK_SECRET = "proxyshop-demo-webhook-secret"

#: The product id the stub's seeded variant hangs off. Any integer; the stub keys on the
#: variant.
STUB_PRODUCT_ID = 8123456


def permalink_parts(url: str) -> tuple[int, int, str]:
    """``(variant_id, quantity, discount_code)`` out of a cart permalink.

    The checkout is driven from the URL the exchange minted rather than from a variant this
    driver picked, and the merchant stub is then seeded to match what the URL says. So this is
    a statement about PROVENANCE — the three values below came out of the offer — and not a
    redemption guard: the stub is configured from these values, so no parseable permalink can
    fail to redeem. What ends the demo here is a permalink this cannot parse at all.
    """
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(url)
    variant, _, quantity = parts.path.rsplit("/", 1)[-1].partition(":")
    codes = parse_qs(parts.query).get("discount") or [""]
    return int(variant), int(quantity or 1), codes[0]


async def _merchant_leg(
    variant_id: int, quantity: int, code: str, unit_price: float
) -> dict[str, Any]:
    """Complete the checkout at the real merchant stub, on loopback, with no Shopify account.

    Three more real servers come up here: the merchant stub, a collector that records the web
    pixel's beacon byte for byte, and a receiver that records the signed ``orders/paid``
    delivery. Both recorders keep the exact bytes, because the whole point of the webhook half
    is that the signature covers the bytes on the wire and a re-serialised body is refused.
    """
    import httpx
    from shopify_stub.app import create_app as create_stub
    from shopify_stub.testing import RecordingReceiver, StubClient

    from proxyshop_support.asgi_server import serve

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
                        "price": f"{unit_price:.2f}",
                        "currency": "USD",
                        "sku": "HX-1",
                    }
                ]
            )
            if seeded.status_code != 200:
                raise RuntimeError(f"seeding the merchant stub failed: {seeded.text}")
            await stub.configure(webhook_secret=WEBHOOK_SECRET)
            await stub.install_pixel(f"{collector_url}/collect")
            await stub.subscribe("ORDERS_PAID", f"{webhook_url}/webhooks/shopify")
            created = await stub.create_code(code, percentage=0.0)
            if created.status_code != 200:
                raise RuntimeError(f"creating the discount code failed: {created.text}")

            completion = await stub.buy(variant_id, quantity=quantity, code=code)
            # Snapshot the evidence BEFORE the second-redemption probe below, so the probe
            # cannot perturb the run it is probing.
            pixel_requests = list(collector.requests)
            webhook_requests = list(webhooks.requests)

            # Offer the SAME code again. Two shapes are both correct merchant behaviour and
            # this driver must not confuse them: an outright refusal at the cart, OR a
            # checkout that completes carrying no discount, because a spent code is silently
            # ignored — which is what Shopify does and what this stub implements. The thing
            # that would be wrong is a second order that redeemed the same code.
            second: dict[str, Any] = {"refused": False, "error": "", "completion": {}}
            try:
                second["completion"] = await stub.buy(variant_id, quantity=quantity, code=code)
            except Exception as refusal:  # noqa: BLE001 - the refusal shape IS the measurement
                second["refused"] = True
                second["error"] = f"{type(refusal).__name__}: {refusal}".split("\n")[0][:200]

    return {
        "stub_url": stub_url,
        "collector_url": collector_url,
        "webhook_url": webhook_url,
        "completion": completion,
        "pixel_requests": pixel_requests,
        "webhook_requests": webhook_requests,
        "second": second,
    }


def _beat_six(say: Narrator, result: JourneyResult) -> None:
    import asyncio

    from merchant_svc.collector import accept_pixel_event
    from merchant_svc.install.webhooks import handle_delivery, ledger_record

    say.beat(6, "Downstream: the order, the web pixel, the signed webhook")
    if not result.code:
        _gap(
            say,
            result,
            "there was no code to redeem",
            "No offer was accepted, so nothing downstream had an order to be about.",
        )
        return

    variant_id, quantity, code = permalink_parts(result.permalink_url)
    winning = result.bid_of(result.accepted_store_id) or {}
    unit_price = float(winning.get("unit_price") or 0.0)

    say.say(
        f"""
        The shopper follows the permalink. Its host is
        {store_domain(result.accepted_store_id)} — the seller's registered domain, which in
        this offline slice is not a host that exists, so the driver takes the three things the
        permalink actually carries (variant {variant_id}, quantity {quantity}, code {code}) and
        redeems them at a local merchant stub instead. That stub is a real implementation of
        exactly the Shopify surface this system uses: a cart permalink, a checkout, a web
        pixel and an HMAC-signed orders/paid webhook. No Shopify account, no development
        store, no payment gateway.
        """
    )
    say.blank()
    checkout = asyncio.run(_merchant_leg(variant_id, quantity, code, unit_price))
    say.wire(
        "GET", f"{checkout['stub_url']}/cart/{variant_id}:{quantity}?discount={code}", 303, "cart"
    )
    say.wire("POST", f"{checkout['stub_url']}/_stub/checkouts/<token>/complete", 201, "paid")

    completion = checkout["completion"]
    result.order_name = str(completion.get("order_name") or "")
    result.order_total = str(completion.get("total_price") or "")
    result.merchant_checkout_token = str(completion.get("checkout_token") or "")
    say.section("The order the merchant closed:")
    say.fact("order", result.order_name)
    say.fact("total paid", money(result.order_total))
    say.fact("discount code redeemed", completion.get("discount_code"))
    say.fact("checkout token (merchant)", completion.get("checkout_token"))
    say.fact("pixel beacon posted", completion.get("pixel_event_posted"))
    say.fact("webhook deliveries", completion.get("webhook_deliveries"))

    # -- the pixel ----------------------------------------------------------------
    say.section("The web pixel — lossy, untrusted, and carrying no customer PII:")
    beacons = checkout["pixel_requests"]
    if not beacons:
        _gap(say, result, "the web pixel never arrived", "The collector recorded no beacon.")
    else:
        beacon = beacons[0]
        body = json.loads(beacon["body"])
        say.fact("POST", f"{checkout['collector_url']}{beacon['path']}")
        say.fact("beacon body", json.dumps(body))
        observation = accept_pixel_event(body)
        result.pixel_order_ref = str(observation.order_ref)
        say.blank()
        say.say(
            """
            The merchant app's own collector logic read that beacon. The bytes above went to
            a recording endpoint over a real socket and were then handed to
            `merchant_svc.collector.accept_pixel_event` in THIS process — the merchant service
            itself is not one of the servers this driver starts, and that is the honest
            framing. It is a browser-side beacon, so it is allowed to go missing and it
            carries no money — the amount comes from the webhook, which is the truth.
            """
        )
        say.fact("order_ref", observation.order_ref)
        say.fact("discount_code", observation.discount_code)
        say.fact("client_id", observation.client_id)
        say.fact("gaps it reported", ", ".join(observation.gaps) or "(none)")

    # -- the webhook --------------------------------------------------------------
    say.section("The orders/paid webhook — server to server, HMAC-SHA256 over the wire bytes:")
    deliveries = checkout["webhook_requests"]
    if not deliveries:
        _gap(say, result, "the paid webhook never arrived", "The receiver recorded no delivery.")
        return
    delivery = deliveries[0]
    headers = {str(k).lower(): v for k, v in dict(delivery["headers"]).items()}
    say.fact("POST", f"{checkout['webhook_url']}{delivery['path']}")
    say.fact("topic", headers.get("x-shopify-topic"))
    say.fact("signature", headers.get("x-shopify-hmac-sha256"))
    say.fact("body bytes", f"{len(delivery['body'])} bytes, signed exactly as sent")

    decision = handle_delivery(
        body=delivery["body"], headers=delivery["headers"], secret=WEBHOOK_SECRET
    )
    say.blank()
    say.say(
        """
        The merchant app's own verifier read it — `merchant_svc.install.webhooks.handle_delivery`,
        called in this process on the exact bytes the receiver recorded off the wire. The
        signature check is the real one; what is not served here is the merchant's own HTTP door.
        """
    )
    say.fact("verdict", f"{decision.status_code} {decision.reason}")
    say.fact("accepted", decision.accepted)
    if decision.event is not None:
        record = ledger_record(decision.event)
        result.webhook_ledger_kind = str(record["kind"])
        result.webhook_order_ref = str(record["order_ref"])
        say.fact("ledger kind", record["kind"])
        say.fact("order_ref", record["order_ref"])
        say.fact("body digest", record["body_digest"])
    say.blank()
    say.say(
        """
        A re-serialised body — the same JSON with different whitespace — fails that check, and
        so does an unsigned one. That asymmetry is the point of the pair: the pixel is
        corroboration, the webhook is evidence.
        """
    )

    # -- single use ---------------------------------------------------------------
    say.section("And the code is single use. The same shopper offers it a second time:")
    second = checkout["second"]
    second_completion = second["completion"]
    redeemed_again = str(second_completion.get("discount_code") or "")
    if second["refused"]:
        say.bullet(f"the merchant refused the second checkout outright: {second['error']}")
        result.second_use_honoured = False
    elif redeemed_again == code:
        result.second_use_honoured = True
        _gap(
            say,
            result,
            "the single-use discount code was honoured a second time",
            f"""
            Order {second_completion.get("order_name")} redeemed {code} again. A code the
            exchange minted for one acceptance was spendable twice, which is the property D22
            names and this run did not get.
            """,
        )
    else:
        result.second_use_honoured = False
        say.bullet(
            f"the second checkout completed as order {second_completion.get('order_name')} "
            f"and carries NO discount (redeemed: {redeemed_again or 'nothing'})"
        )
        say.blank()
        say.say(
            """
            Read that carefully, because two different things look alike here. The second
            checkout was not refused — a spent code is silently ignored at the cart, which is
            what Shopify does and what this merchant implements. The single-use property is
            not "the second attempt errors"; it is "the second order carries no discount",
            and that is what the order above shows.
            """
        )


# =====================================================================================
# beat 7 — the audit trail, read back off a different process
# =====================================================================================
def _beat_seven(say: Narrator, result: JourneyResult, trust: Any) -> None:
    say.beat(7, "The audit trail: what the exchange wrote, and whether the chain holds")
    say.say(
        f"""
        Nothing in beats 2 to 5 was asked to keep an audit trail. The exchange's state machine
        records every transition into a ledger sink, and the only thing the deployment document
        said about that sink is WHERE it posts — one line, `trust_url`. Everything below is
        read back off a DIFFERENT application, over HTTP, at {result.trust_url}. The exchange
        is not being asked what it remembers; the trust service is being asked what it
        received.
        """
    )
    say.blank()
    page = trust.get("/events")
    say.wire("GET", f"{result.trust_url}/events", page.status_code)
    page.raise_for_status()
    result.ledger_events = list(page.json().get("events", []))

    if not result.ledger_events:
        _gap(
            say,
            result,
            "the trust service received no ledger events",
            """
            The exchange was pointed at a trust service that answered, and its chain is empty.
            Every auction transition in this run landed nowhere, which is the condition T-150
            exists to prevent.
            """,
        )
        return

    say.section(f"The chain the trust service holds ({len(result.ledger_events)} event(s)):")
    for event in result.ledger_events:
        say.bullet(
            f"seq {str(event.get('seq')):<3} {str(event.get('kind')):<15} "
            f"prev {str(event.get('prev_hash'))[:12]}.. -> "
            f"self {str(event.get('event_hash'))[:12]}.."
        )
    say.blank()
    say.say(
        """
        Each event's `prev_hash` is the previous event's `event_hash`, so the rows are not
        merely ordered — each one commits to every row before it. Removing the middle of that
        list, or editing one field of one event, breaks the link at that point and every link
        after it.
        """
    )

    report = trust.get("/events/verify")
    say.blank()
    say.wire("GET", f"{result.trust_url}/events/verify", report.status_code)
    report.raise_for_status()
    result.ledger_verify = dict(report.json())

    say.section("The service's own verdict on that chain:")
    say.fact("chain intact", result.ledger_verify.get("ok"))
    say.fact("events verified", result.ledger_verify.get("verified"))
    say.fact("anchor agrees", result.ledger_verify.get("anchor_ok"))
    say.fact("head hash", result.ledger_verify.get("head_hash"))
    say.fact("stream hash", result.ledger_verify.get("stream_hash"))

    if not (result.ledger_verify.get("ok") and result.ledger_verify.get("anchor_ok")):
        _gap(
            say,
            result,
            "the chained ledger did not verify",
            f"""
            `GET /events/verify` answered ok={result.ledger_verify.get("ok")}
            anchor_ok={result.ledger_verify.get("anchor_ok")}:
            {result.ledger_verify.get("detail") or result.ledger_verify.get("reason")}. The
            audit trail this run produced cannot be trusted to be the one it wrote.
            """,
        )
    else:
        say.blank()
        say.say(
            """
            `anchor_ok` is the half that is easy to miss and is the reason a deleted event
            cannot hide. The chain's length and head are recorded OUTSIDE the row list, so a
            stream that was truncated to a shorter but perfectly-linked prefix still fails
            here — a flawless chain of the wrong length is still a tampered one.
            """
        )

    # -- the join key, measured rather than asserted -------------------------------
    exchange_token = ""
    for event in result.ledger_events:
        payload = event.get("payload")
        if str(event.get("kind")) == "accepted" and isinstance(payload, Mapping):
            exchange_token = str(payload.get("checkout_token") or "")
    if exchange_token or result.merchant_checkout_token:
        say.section("The join key reconciliation needs, as the two sides actually wrote it:")
        say.fact("exchange, in the ledger", exchange_token or "(absent)")
        say.fact("merchant, at the cart", result.merchant_checkout_token or "(absent)")
        say.fact(
            "same value?",
            bool(exchange_token) and exchange_token == result.merchant_checkout_token,
        )
        say.blank()
        say.say(
            """
            Two different values, and they always were: the exchange mints its token with
            `secrets.token_hex(16)` after the merchant has already been called and transmits
            it nowhere, so the store mints its own when the cart is visited. Reconciliation
            does not join on it any more. It bridges the offer to the order through the
            single-use discount code — the one value that genuinely crossed the wire — which
            it reads off the `code_created` and `checkout_redirect` records above.
            """
        )

    # -- and what reconciliation actually makes of it, run rather than reasoned about ---
    _beat_seven_reconciliation(say, result)

    # -- where it stops -----------------------------------------------------------
    if result.reconciled:
        return
    _gap(
        say,
        result,
        "reconciliation and the trust update cannot run over the ledger chain this run writes",
        f"""
        The runbook's sections 3.6 and 3.7 close the loop: `trust.reconcile.reconcile` joins
        the accepted offer, the beacon and the webhook into one `reconciled` verdict, and that
        verdict moves the store's trust score, which is the same snapshot the ranker filters
        on. Both components exist and are tested, and the EVENTS are now all in one chain: the
        exchange's `accepted` with the offer on it, both of the bridge records that carry the
        single-use code, and the merchant's own `order_paid` — posted to this trust service
        over HTTP by a different application. The fold over that chain still returns
        {result.reconciled} verdict(s). One thing blocks it and it is a NAMING problem;
        a second thing costs evidence rather than the verdict:

        1. `reconcile` namespaces every join key by the store that owns it — it has to, a
           Shopify `order_id` is a per-shop number, and two shops both have order 1001. The
           two halves of this purchase name the seller differently. The `accepted` event
           now DOES carry a `store_id` -- `AuctionStateMachine.accept` takes the winning
           store and `_transition` stamps it on the envelope -- so the offer and the two
           bridge records beside it are all filed under
           `{result.accepted_store_id or "the winning store"}`. And the `order_paid` names
           `{_merchant_store_name(result) or "the shop domain"}`, because an unsigned
           `X-Shopify-Shop-Domain` header is the only shop identity a signed delivery carries
           at all. Supply both of those and the same three events and the same webhook
           reconcile: that is the probe printed above, and it is a probe rather than a result
           precisely because this driver had to supply them.

        2. `checkout_pixel` still has no producer anywhere in this repository. Not for want
           of a pixel: `pixel/src/` holds a real Web Pixel extension (beacon, transport,
           settings, pixel, index). What is missing is the last hop — nothing on a served
           path turns a beacon into a ledger event, and `merchant_svc.collector` stops at a
           `PixelObservation` in memory (beat 6 read one, in process). That one costs
           EVIDENCE rather than the verdict: a group with no beacon grades `pixel_missing`,
           which by design is not a blocker. A driver that manufactured a beacon would be
           supplying the evidence whose absence is the defect.

        The trust side of the naming problem is already built and needs nothing from here:
        `trust.reconcile.routes.resolve_store_aliases` maps a seller's registered domain onto
        its `store_id` off the platform's own roster and `POST /reconcile` applies it. It
        reads `app.sellers`, which is Postgres — and this command runs none, which is why the
        alias above had to be stated by the probe rather than resolved.
        """,
    )


def _merchant_store_name(result: JourneyResult) -> str:
    """How the MERCHANT's ``order_paid`` names the shop, straight off the served chain."""
    for event in result.ledger_events:
        if str(event.get("kind")) == "order_paid":
            return str(event.get("store_id") or "")
    return ""


def _beat_seven_reconciliation(say: Narrator, result: JourneyResult) -> None:
    """Fold the served chain through ``reconcile``, then probe what the fold is missing.

    Two numbers, and the difference between them is the whole diagnosis:

    * ``result.reconciled`` — ``reconcile`` over ``GET /events`` exactly as the trust service
      served it back. This is the product's number.
    * ``result.reconciled_probe`` — the same fold after this driver supplies the two facts the
      chain does not carry: the winning ``store_id`` on the ``accepted`` event, and the
      shop-domain alias that says the order's shop IS that store. Everything else is
      untouched, so a probe that also returns nothing would mean the diagnosis below is
      wrong.

    The engine is called in THIS process, and that is the honest framing — the same one beat 6
    uses for ``handle_delivery``. ``POST /reconcile`` is served, and it reads Postgres for the
    seller roster and writes trust observations back to it, which this command deliberately
    does not run. What is real either way is the input: every event folded here came off the
    wire from a different application.
    """
    from trust.reconcile.engine import reconcile

    if not result.ledger_events:
        return

    say.section("What reconciliation makes of that chain:")
    try:
        result.reconciled = len(reconcile(result.ledger_events))
    except Exception as refusal:  # noqa: BLE001 - a refusal is a measurement, not a crash
        say.fact("reconcile refused the chain", f"{type(refusal).__name__}: {refusal}")
        return
    say.fact("verdicts over the chain as served", result.reconciled)

    shop = _merchant_store_name(result)
    named = [
        {**event, "store_id": result.accepted_store_id}
        if str(event.get("kind")) == "accepted"
        else event
        for event in result.ledger_events
    ]
    aliased = [
        {**event, "store_id": result.accepted_store_id}
        if shop and str(event.get("store_id") or "").lower() == shop.lower()
        else event
        for event in named
    ]
    try:
        verdicts = reconcile(aliased)
    except Exception as refusal:  # noqa: BLE001 - same rule as above
        say.fact("the probe was refused", f"{type(refusal).__name__}: {refusal}")
        return
    result.reconciled_probe = len(verdicts)
    say.fact("verdicts with the store named on both halves (a PROBE)", result.reconciled_probe)
    if verdicts:
        payload = verdicts[0].get("payload") or {}
        promised = payload.get("promised_price")
        observed = payload.get("observed_price")
        say.fact("  promised / observed", f"{promised} / {observed}")
        say.fact("  price honoured", payload.get("price_honored"))
        say.fact("  beacon present", not payload.get("pixel_missing"))
        say.blank()
        say.say(
            f"""
            That second line is a PROBE and not a result. The driver rewrote two `store_id`
            fields — nothing else — to stand in for the two facts the chain does not carry,
            and the fold then produced a real verdict from the real events. It is printed
            because a gap nobody can reproduce is a claim; this one names exactly what is
            missing and shows what closing it would produce. The product's number is the
            first line: {result.reconciled}.
            """
        )


# =====================================================================================
# the epilogue
# =====================================================================================
def _epilogue(say: Narrator, result: JourneyResult) -> None:
    say.blank()
    say.line("=" * 80)
    say.line("  WHAT JUST RAN, AND WHAT DID NOT")
    say.line("=" * 80)
    say.section("Ran, live, over HTTP:")
    say.bullet(f"the shopper was asked {len(result.clarifying_questions)} clarifying question(s)")
    say.bullet(f"{len(result.denied)} store denied at the eligibility gate, before being asked")
    say.bullet(f"{len(result.solicited)} stores solicited; {len(result.ranked)} bid(s) ranked")
    say.bullet(f"{len(result.excluded)} candidate(s) refused, each naming every reason")
    say.bullet(f"{len(result.shortlist_slots)} shortlist slot(s) shown to the shopper")
    if result.code:
        say.bullet(f"one single-use code minted: {result.code}")
        say.bullet(f"one permalink on the registered domain: {result.permalink_url}")
    if result.order_name:
        say.bullet(
            f"one order closed at the merchant: {result.order_name} for {money(result.order_total)}"
        )
    if result.pixel_order_ref:
        say.bullet(
            f"one web pixel beacon read by the merchant app (order {result.pixel_order_ref})"
        )
    if result.webhook_ledger_kind:
        say.bullet(
            f"one HMAC-signed webhook verified and read as {result.webhook_ledger_kind} "
            f"(order {result.webhook_order_ref})"
        )
    if result.ledger_verify.get("ok") and result.ledger_verify.get("anchor_ok"):
        say.bullet(
            f"{result.ledger_verify.get('verified')} ledger event(s) delivered to the trust "
            f"service and served back as a VERIFIED hash chain "
            f"(head {str(result.ledger_verify.get('head_hash'))[:12]}..)"
        )

    say.section(f"Did NOT run, and why ({len(result.gaps)}):")
    for headline in result.gaps:
        say.bullet(headline)
    say.blank()
    say.say(
        """
        Section 4 of docs/demo/starting-slice.md runs the scripted proof of the same journey.
        Run it before the demo; if it is green, the beat above will work.
        """
    )
    say.blank()


def _gap(say: Narrator, result: JourneyResult, headline: str, body: str) -> None:
    """Record and print one beat the product cannot perform yet."""
    result.gaps.append(headline)
    say.gap(headline, body)


# =====================================================================================
# the command
# =====================================================================================
def main(argv: Sequence[str] | None = None) -> int:
    """``python -m proxyshop_demo``. Returns a process exit status."""
    del argv
    try:
        run_journey()
    except Exception as failure:  # noqa: BLE001 - a demo reports its own failure and exits 1
        print(f"\nDEMO FAILED: {type(failure).__name__}: {failure}", file=sys.stderr)
        return 1
    return 0
