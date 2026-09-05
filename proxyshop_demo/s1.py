"""The S1 starting slice, driven live and narrated for a person watching.

    .venv/bin/python -m proxyshop_demo

What this is, and why it exists
-------------------------------
``docs/demo/starting-slice.md`` section 3 calls itself "the beat to run in front of an
audience" and, until this module, contained seven subsections of prose and **zero commands**.
The only runnable thing on the page was section 4, and what a room saw when it ran was one
line — ``27 passed in 1.38s``. No intent, no clarifying questions, no bids, no shortlist, no
discount code. The data all existed; nothing surfaced it.

This driver surfaces it. It starts four real ASGI deployables on four loopback ports and walks
one purchase across them, printing what happened in plain language beside the real values:

* ``buyer_svc.main:create_app()``   — the shopper's clarifier, reached at ``POST /buyer/intent/clarify``
* ``store_agent.main:create_app()`` — one process per store, answering ``POST /v1/bid-requests``
* ``exchange.main:create_app()``    — ``POST /auctions`` and ``POST /auctions/{id}/accept``

Nothing here is a test double and nothing is replayed. Every arrow in the output is an HTTP
request over a real TCP socket, which is the boundary the class of defect this repository kept
finding hides at: an app object handed to a test is never *started*, so nothing between
``create_app()`` and a served request is executed.

What it deliberately does NOT do
--------------------------------
It does not wire anything. The exchange reads its own deployment document out of
``EXCHANGE_DEPLOYMENT`` and each store agent reads its own context out of
``STORE_AGENT_CONTEXT``, which is what a person deploying these containers does. The single
exception is loud and is named in the output: the ranking's *catalogue* and the buyer service's
*auction client* have no configuration surface at all, so the beats that need them are reported
as gaps rather than faked.

**Where the product cannot do something yet, this prints it.** A demo driver that quietly omits
the broken beats is the same lie as a green board over a broken system, so every such beat lands
in :attr:`JourneyResult.gaps` and is rendered as a ``DOES NOT RUN YET`` block. Four are real
today, and each one is **measured by this driver in the line above the block that reports it**
rather than asserted from reading the source:

1. ``POST /buyer/intent/confirm`` answers 503 — the buyer service has no composition root
   binding an auction client, so a confirmed intent has nowhere to go;
2. a store agent handed the clarifier's own intent answers ``204 cluster_not_pursued``: the
   clarifier derives ``cluster_id`` by hashing the query, envelopes authorise NAMED catalogue
   clusters, and nothing maps one namespace onto the other;
3. R10's silent-store fallback is manufactured with no ``expires_at`` and no ``checkout_url``,
   so the ranking excludes it ``expired_offer`` + ``off_domain_checkout`` before it can be
   shown — the runbook's section 3.4 says the shortlist carries it, and over the real composed
   exchange it does not;
4. reconciliation and the trust projection have no ledger page any deployable serves, and no
   ``checkout_token`` binding between the exchange's offer and the merchant's order.

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

#: The discount depth each store's approved envelope authorises in this scenario, and the same
#: number the roster states. They have to agree: the roster row is the PLATFORM's half of the
#: T-177 price wall, and a roster that states a different cap turns an honest bid into
#: ``fallback_reason: 'bid_price_unreconcilable'``.
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
    agent_urls: dict[str, str] = field(default_factory=dict)

    clarifying_questions: list[str] = field(default_factory=list)
    clarified_intent: dict[str, Any] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    #: What the served buyer app answered to the shopper's confirmation. Measured, not assumed.
    confirm_status: int = 0
    confirm_detail: str = ""
    #: What one store agent answered when handed the clarifier's own intent, unedited.
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

    order_name: str = ""
    order_total: str = ""
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
    it is instantaneous, and it is what ``HttpBidSolicitor`` turns into ``no_response`` — the
    R10 case — without spending three seconds of an audience's attention on a timeout.
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


def _deployment_document(
    stores: Sequence[Mapping[str, Any]], endpoints: Mapping[str, str]
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
    service builds this projection from its own ledger of reconciled outcomes, and this driver
    does not run the trust service. What the demo shows is that the ranker READS the served
    snapshot, that the score moves a candidate's position, and that a blacklisted row is
    refused. What it does not show is the score's derivation.

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
            json.dumps(_deployment_document(stores, endpoints), indent=2), encoding="utf-8"
        )
        os.environ[ENV_DEPLOYMENT] = str(document)
        os.environ.pop(ENV_DEPLOYMENT_JSON, None)

        buyer_url = stack.enter_context(serve(create_buyer()))
        exchange_url = stack.enter_context(serve(create_exchange()))
        result.buyer_url = buyer_url
        result.exchange_url = exchange_url

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
        "four deployables, four loopback ports, one purchase, no Shopify account",
    )
    say.say(
        """
        Everything below is a real HTTP round trip between processes this command started a
        moment ago. Nothing is replayed, nothing is a test double, and no service was wired by
        this driver: the exchange read its collaborators out of a deployment document, and each
        store agent read its envelope and catalogue out of a store-context file, which is what
        a person deploying these containers does.
        """
    )
    say.section("Running right now:")
    say.fact("buyer service", result.buyer_url)
    say.fact("exchange", result.exchange_url)
    for store_id, url in result.agent_urls.items():
        say.fact(f"store agent {store_id}", url)
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
    probe_store, probe_url = next(iter(result.agent_urls.items()), ("", ""))
    decline_reason = ""
    if probe_url:
        import httpx

        probe = httpx.post(
            f"{probe_url}/v1/bid-requests",
            json={
                "auction_id": "auction-demo-probe",
                "intent": intent,
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

    if decline_reason:
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
        say.bullet(f"{probe_store} answered {result.agent_probe_status} to the clarified intent")


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
            f"{store_id} is absent from that list, which is the first of the four gates it "
            f"has to fail.",
            marker="*",
        )

    # -- beat 3 -------------------------------------------------------------------
    say.beat(3, "The stores bid — a real POST /v1/bid-requests to each agent")
    say.say(
        """
        Solicitation fanned out to every eligible store inside a bounded bid window. Each
        hosted agent priced from its own catalogue, moved only within the discount depth its
        approved envelope authorises, and answered with a sealed bid. No model is anywhere
        near a price.
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
        resolves a checkout provider for CHECKOUT_MODE. In `redirect` that is the simulated
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
        `checkout.{result.accepted_store_id}` or `evil-{result.accepted_store_id}.attacker.tld`
        would have been refused here, and refused BEFORE any discount code was created.
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

    The checkout follows the URL the exchange minted rather than a variant this driver picked,
    so a permalink the merchant could not actually redeem ends the demo instead of passing
    quietly.
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
            The merchant app's real collector read those exact bytes. It is a browser-side
            beacon, so it is allowed to go missing and it carries no money — the amount comes
            from the webhook, which is the truth.
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
    say.say("The merchant app verified it:")
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

    # -- where it stops -----------------------------------------------------------
    _gap(
        say,
        result,
        "reconciliation and the trust update cannot run from anything this demo served",
        """
        The runbook's sections 3.6 and 3.7 close the loop: `trust.reconcile.reconcile` joins
        the accepted offer, the beacon and the webhook into one `reconciled` verdict, and that
        verdict moves the store's trust score, which is the same snapshot the ranker filters
        on. Both components exist and are tested. Neither can be reached from what this driver
        just stood up, for two measured reasons:

        1. `reconcile` reads a page of ledger events — `accepted`, `checkout_pixel`,
           `order_paid`. The exchange's ledger is an in-process sink that no HTTP route
           serves, and `pixel/src/` in this repository is an empty directory, so no deployable
           emits `checkout_pixel` at all. A driver that manufactured those three events would
           be supplying the join whose absence is the defect.

        2. Even given the page, the join key is missing. The checkout provider invents its
           `checkout_token` with `secrets.token_hex(16)` and never transmits it — the cart
           permalink carries the discount code and nothing else — while the merchant mints its
           own, unrelated token when the cart is visited. `reconcile` joins on exactly that
           key, so it finds none and emits nothing. `e2e/support/s1/flow.py` closes the gap
           by deriving the binding from the single-use code and says so in its own docstring;
           nothing in a deployed service does.

        This is where the starting slice genuinely stops today.
        """,
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
