#!/usr/bin/env python
"""Manufacture the merchant dashboard the demo renders when nobody has used the system yet.

WHY THIS EXISTS. ``GET /stores/{store_id}/dashboard`` is R9's whole merchant page, it is
correct, it is served, and on a fresh deployment it is empty — because every panel on it is
downstream of a shopper who does not exist yet. Its trust panel is a projection of the trust
ledger, its loss panel is the exchange's report, and its bid journal records solicitations
nobody has made. A demo of the merchant surface shows an operator six panels saying "no
observations yet", which is honest and demonstrates nothing.

So this script runs the page once, against real code, and stores what it produced. The output
is TRACKED IN THE TREE and the demo replays these bytes; it does not re-run this script.

WHAT IT WRITES, into ``deploy/demo-seed/merchant-dashboard/``:

* ``dashboard-page.json``  -- one complete ``DashboardPage``
* ``collection.json``      -- its provenance record, with a sha256 over the payload and the
                              marker a reader needs, panel by panel

NOTHING HERE HAND-WRITES THE PAYLOAD. It is what
``merchant_svc.dashboard.routes.read_dashboard`` returned, driven in-process over
``httpx.ASGITransport``: the real envelope algebra, the real approval digest, the real
narrowing of a whole-market trust snapshot down to one store, and a REAL ``store_agent``
answering three REAL ``POST /v1/bid-requests`` in two activation states.

WHERE THE TRUST NUMBERS COME FROM. ``services/sim/seed-data/feedback-population/`` -- a real
hash-chained ledger produced by driving the real ``POST /buyer/feedback`` route with simulated
shoppers. :func:`seed.store.load` verifies its digests before this script reads a byte of it,
and :func:`seed.store.replay_postures` recomputes every posture from the chain offline. Every
observation in it carries ``order_ref`` beginning ``sim-fb-`` INSIDE the hash chain, which is
the one marker here that cannot be forged. The ``marker`` block of ``collection.json`` says,
panel by panel, where that is true and where it is not.

WHY THE STORE IS A SIMULATED COFFEE STORE AND NOT ONE OF THE FOUR DEMO STOREFRONTS. The only
real feedback chain in this tree belongs to the five simulated stores of the seeded population
(``store-brightbean`` ... ``store-slowreply``), and its cluster is ``coffee``. Writing
``gaiaherbs.com`` at the top of a page whose trust events are sealed under
``store-secondchance`` would forge the only unforgeable thing in the artifact. The four
supplement storefronts of ``deploy/demo/`` have no feedback chain because nobody has ever left
them feedback; that is a true fact about them and this file does not paper over it.

Usage::

    ./.venv/bin/python scripts/build_seed_dashboard.py
    ./.venv/bin/python scripts/build_seed_dashboard.py --check   # regenerate + diff, no write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / ".pkgroot"))

# Module scope, not function scope, and that is load-bearing: `from __future__ import
# annotations` makes every annotation a STRING, and FastAPI resolves a handler's annotations
# against the module globals. A `Request` imported inside the function that declares the
# handler is invisible there, so FastAPI read the stub's `request: Request` as a QUERY
# PARAMETER and answered 422 -- which reached the page as `losses: upstream_refused`.
from fastapi import FastAPI, Request  # noqa: E402 - after the sys.path bootstrap
from fastapi.responses import JSONResponse  # noqa: E402 - after the sys.path bootstrap
from seed import provenance  # noqa: E402 - after the sys.path bootstrap
from seed import store as seed_store  # noqa: E402 - after the sys.path bootstrap
from seed.shoppers import SIMULATED_ORDER_PREFIX  # noqa: E402 - after the bootstrap

__all__ = [
    "ARTIFACT_KIND",
    "ARTIFACT_VERSION",
    "COLLECTION_FILE",
    "DASHBOARD_FILE",
    "DASHBOARD_ROOT",
    "DEFAULT_STORE",
    "OBSERVED_AT",
    "LoadedPanel",
    "SeedPanelError",
    "build_dashboard_page",
    "build_panel",
    "load_panel",
    "main",
]

#: Bumped when the on-disk layout changes in a way a reader must notice. Semantic, and read by
#: :func:`load_panel`, so a panel written by a future layout is refused with a sentence rather
#: than parsed into nonsense by a reader that predates it. Same rule as
#: :data:`seed.store.ARTIFACT_VERSION`, and for the same reason.
ARTIFACT_VERSION = "1.0.0"

#: What this corpus IS, in one machine-readable token. A directory of JSON is otherwise
#: indistinguishable from any other directory of JSON, and the one thing a stranger must not
#: have to guess about this one is that the market in it was manufactured.
ARTIFACT_KIND = "simulated-merchant-dashboard-page"

COLLECTION_FILE = "collection.json"
DASHBOARD_FILE = "dashboard-page.json"

DASHBOARD_ROOT = REPO_ROOT / "deploy" / "demo-seed" / "merchant-dashboard"

#: The store this page is about, and the reason it is not ``gaiaherbs.com`` is in the module
#: docstring. It is one of the five stores of the seeded feedback population, and the one whose
#: buyers answered POSITIVELY: seven observations, ``feedback_match`` at Beta(8.0, 3.5), a
#: posterior mean of 0.696 against the neutral 0.5 prior. That is the page worth shipping --
#: a store whose delivered goods match its pitch and which is still losing auctions on price.
DEFAULT_STORE = "store-secondchance"

#: The intent cluster this market has. Not chosen here: ``seed.population`` addresses every
#: simulated auction to ``cluster_id = seed_category`` and ``fixtures/manifest.json`` states
#: that category as ``coffee``. Naming a different one would put the envelope, the loss report
#: and the chain in three different markets.
CLUSTER_ID = "coffee"

#: A cluster this store does NOT pursue, used once to make its agent answer
#: ``cluster_not_pursued`` in its own words. It is the compose demo's supplement cluster
#: (``scripts/build_demo_deployment.py``), which a coffee roaster is exactly right to decline.
UNPURSUED_CLUSTER_ID = "cluster-liver-support"

#: A fixed observation stamp, exactly as ``scripts/build_demo_deployment.py`` uses one. Every
#: document built here must be byte-identical on every machine, so nothing reads a clock -- see
#: :func:`_frozen_service_clocks` for the three places a clock would otherwise get in.
#:
#: The value is deliberately AFTER the seeded corpus's ``as_of`` (2026-09-07T22:50:37.358Z): a
#: page whose trust panel carries those events cannot honestly claim to predate them.
OBSERVED_AT = "2026-09-08T00:00:00+00:00"

#: The loss window the page is generated for: the thirty days ending at :data:`OBSERVED_AT`,
#: passed explicitly so the route echoes it back instead of defaulting off ``time.time()``.
LOSS_WINDOW_DAYS = 30.0

#: The bearer the dashboard routes are driven with here. A local, throwaway string for an
#: in-process ASGI transport that opens no socket, and it appears in no output.
ADMIN_TOKEN = "seed-panel-admin-token"

#: The bearer the stub exchange resolves to this store, same shape as the real report auth.
REPORT_TOKEN = "seed-panel-report-token"

EXCHANGE_URL = "http://exchange:8083"
TRUST_URL = "http://trust:8084"
STORE_AGENT_URL = "http://store-agent:8086"


class SeedPanelError(RuntimeError):
    """The seed panel is missing, malformed, unmarked, or does not match its own digests."""


# ======================================================================================
# The stated half: everything a storefront does not publish and a person therefore says
# ======================================================================================
#: The merchant's own walls. Deployment configuration in the same sense as
#: ``scripts/build_demo_deployment.py``'s discount depths: an envelope is the merchant's
#: authorisation and no catalogue publishes one, so a person states it.
ENVELOPE_DOCUMENT: dict[str, Any] = {
    "store_id": DEFAULT_STORE,
    "version": 1,
    "floors": [
        {"product_ref": None, "min_price": 12.0},
        {"product_ref": "prod-secondchance-altura", "min_price": 16.5},
    ],
    "max_discount_pct": 15.0,
    "budget_cap": 750.0,
    "pursue_clusters": [CLUSTER_ID],
    "standing_commitments": [
        {
            "key": "free_returns",
            "value": "30 day return window",
            "provenance": {
                "source": "owner_statement",
                "ref": f"envelope:{DEFAULT_STORE}:1#free_returns",
                "observed_at": OBSERVED_AT,
                "authority_rank": 1,
            },
        }
    ],
    "activation": "shadow",
}

#: The merchant's SECOND version of those walls, and the reason the page has a history to show.
#:
#: R9 asks for envelope editing *(versioned)*, and a page carrying one version cannot show a
#: merchant what they changed. The one edit is the discount cap, 15% -> 18%, which is the answer
#: this store's own loss report argues for: six of its nine losses were on price. So the shipped
#: page's ``envelope.versions`` is a real four-row history -- v1 shadow, v1 active, v2 shadow,
#: v2 active -- produced by the service's own version algebra rather than typed here.
ENVELOPE_VERSION_2: dict[str, Any] = {
    **ENVELOPE_DOCUMENT,
    "version": 2,
    "max_discount_pct": 18.0,
}

#: The store's catalogue, as its own agent reads it. Two products from the seed category's own
#: product families (``fixtures/catalog/coffee.json``), priced above the envelope's floors so
#: the agent has something inside its walls to offer.
CATALOG: dict[str, dict[str, Any]] = {
    "prod-secondchance-altura": {
        "product_ref": "prod-secondchance-altura",
        "list_price": 22.0,
        "title": "Altura Washed Single Origin, 12 oz",
        "brand": "Second Chance Coffee",
        "product_type": "whole_bean",
        "currency": "USD",
    },
    "prod-secondchance-harbour": {
        "product_ref": "prod-secondchance-harbour",
        "list_price": 19.0,
        "title": "Harbour House Espresso Blend, 12 oz",
        "brand": "Second Chance Coffee",
        "product_type": "whole_bean",
        "currency": "USD",
    },
}

#: The exchange's loss report for this store. STATED, and the only panel on the page with no
#: marker of any kind in it -- ``LossReport`` is ``additionalProperties: false``, so there is
#: nowhere to put one. See the ``marker`` block: this is named as unmarked rather than glossed.
#:
#: The four reasons are the whole closed vocabulary (``contracts.LossReasons``, ``extra
#: ="forbid"``) and there is no "wins" field anywhere in a ``LossReport`` -- the exchange
#: reports what a store LOST and why, and never what a rival gained.
#:
#: The numbers tell the story the trust panel makes possible: nine losses in the one cluster
#: this market has, six of them on price, none of them on trust -- a store whose buyers say it
#: delivers what it pitched and which is being undercut anyway.
LOSS_REPORT: dict[str, Any] = {
    "store_id": DEFAULT_STORE,
    "by_cluster": [
        {
            "cluster_id": CLUSTER_ID,
            "lost": 9,
            "reasons": {"fit": 1, "price": 6, "commitments": 2, "trust": 0},
            "unmet_criteria": [
                "roast_date within 14 days",
                "free shipping over $35",
            ],
        }
    ],
}

#: The approver of record for the stated envelope. An obviously-synthetic address at the
#: simulated store's own identity, on the reserved ``.invalid`` TLD, because
#: ``ApprovalArtifact.parse`` refuses an absent approver and a placeholder that looked like a
#: real person would be a claim about a real person.
APPROVER = f"owner@{DEFAULT_STORE}.simulated.invalid"


# ======================================================================================
# Determinism: the three clocks and the one id generator that would otherwise get in
# ======================================================================================
class _FrozenClock:
    """A stand-in for :class:`datetime.datetime` that always reports :data:`OBSERVED_AT`.

    ``merchant_svc.dashboard.routes`` reads a clock twice -- once for ``generated_at`` and once
    for the solicitation's ``respond_by``, which the agent echoes back as the offer's
    ``expires_at``. Both would move on every run, so ``--check`` would report drift on a
    generator whose inputs had not changed, exactly as a wall-clock ``decayed_at`` would in
    ``scripts/build_demo_deployment.py``.
    """

    @staticmethod
    def now(tz: Any = None) -> datetime:
        moment = datetime.fromisoformat(OBSERVED_AT)
        return moment if tz is None else moment.astimezone(tz)


class _MarkedIds:
    """A stand-in for :mod:`uuid` whose ``uuid4`` is deterministic and carries the marker.

    The dashboard mints its rehearsal ids as ``f"dashboard-rehearsal-{uuid.uuid4()}"``, so the
    id is the route's to choose and not the caller's. This is the seam the producer uses to
    make that choice, in exactly the discipline :mod:`seed.shoppers` uses for ``order_ref``:
    every id this hands back begins with :data:`SIMULATED_ORDER_PREFIX`, and a real ``uuid4``
    cannot produce that substring because ``s``, ``i`` and ``m`` are not hex digits.

    It is a RESERVED PREFIX and not a hash-chain fact -- nothing seals a solicitation row --
    and the ``marker`` block says so in those words rather than implying otherwise.
    """

    _NAMESPACE = uuid.UUID("8b6f6a3e-0f7d-5f52-9a44-3a8f1f5a0b21")

    def __init__(self) -> None:
        self._issued = 0

    def uuid4(self) -> str:
        self._issued += 1
        minted = uuid.uuid5(self._NAMESPACE, f"seed-panel-{self._issued:04d}")
        return f"{SIMULATED_ORDER_PREFIX}{minted}"


@contextmanager
def _frozen_service_clocks() -> Iterator[None]:
    """Freeze every clock and id generator the dashboard write path reads, then put them back.

    Restoring matters more here than in a test: this module is imported by
    ``scripts/tests/test_seed_dashboard.py``, and a producer that left a frozen clock behind in
    ``merchant_svc`` would hand the next test in the session a service that thinks it is always
    midnight.
    """
    import merchant_svc.dashboard.journal as journal
    import merchant_svc.dashboard.routes as routes

    saved = (routes.datetime, routes.uuid, journal._now)
    routes.datetime = _FrozenClock  # type: ignore[misc, assignment]
    routes.uuid = _MarkedIds()  # type: ignore[assignment]
    journal._now = lambda: OBSERVED_AT
    try:
        yield
    finally:
        routes.datetime, routes.uuid, journal._now = saved  # type: ignore[misc]


@contextmanager
def _stated_environment() -> Iterator[None]:
    """The deployment variables the merchant service reads, set for the duration and restored.

    Set rather than assumed: ``dashboard_config`` answers ``not_configured`` and names the
    variable when one is missing, and a panel that said "set TRUST_URL" would be a seed panel
    demonstrating a misconfiguration rather than a market.
    """
    stated = {
        "MERCHANT_ADMIN_TOKEN": ADMIN_TOKEN,
        "EXCHANGE_URL": EXCHANGE_URL,
        "TRUST_URL": TRUST_URL,
        "STORE_AGENT_URL": STORE_AGENT_URL,
        "MERCHANT_REPORT_TOKENS_JSON": json.dumps({DEFAULT_STORE: REPORT_TOKEN}),
        # D20's default. Named rather than inherited so the pitch the agent writes is the
        # offline double's deterministic prose and never a paid model's, which would put a
        # different sentence in this artifact on every run and a bill on somebody's card.
        "LLM_PROVIDER": "double",
    }
    before = {name: os.environ.get(name) for name in stated}
    os.environ.update(stated)
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ======================================================================================
# The seeded chain
# ======================================================================================
def _seeded_chain(store_id: str) -> tuple[Any, list[dict[str, Any]], dict[str, Any]]:
    """The verified seed corpus, this store's events, and every store's published posture.

    :func:`seed.store.load` verifies every digest the corpus records before returning, so a
    corpus whose bytes and whose provenance disagree stops this script rather than becoming a
    panel that looks accounted for.
    """
    artifact = seed_store.load()
    postures = seed_store.replay_postures(artifact)
    if store_id not in postures:
        raise SeedPanelError(
            f"the seeded feedback corpus at {artifact.root} holds no store {store_id!r}; it "
            f"holds {sorted(postures)}. A dashboard for a store with no chain would have a "
            "trust panel nobody could check."
        )
    mine = [dict(event) for event in artifact.events if event.get("store_id") == store_id]
    if not mine:
        raise SeedPanelError(
            f"{store_id!r} is rostered in the corpus but has no ledger events, so this page's "
            "trust_events panel would be empty and its marker would mark nothing"
        )
    unmarked = [
        str(event.get("event_id")) for event in mine if not provenance.is_seeded_event(event)
    ]
    if unmarked:
        raise SeedPanelError(
            f"{len(unmarked)} of {store_id}'s ledger events carry no {SIMULATED_ORDER_PREFIX!r} "
            f"marker ({unmarked[:3]}). Shipping them in a seed panel would put unmarked "
            "observations in a file that claims every observation in it is manufactured."
        )
    return artifact, mine, postures


# ======================================================================================
# The upstreams the page is assembled from
# ======================================================================================
def _stub_exchange() -> Any:
    """A stand-in for the exchange's ``GET /reports/losses``, with its real auth shape.

    A stub rather than the real exchange because the loss report is STATED here (no auction was
    ever run for this store on this tree), and because the bearer resolving the store is the
    property the dashboard must not undo -- so the stub enforces it exactly as
    ``exchange.reports.routes`` does, and the page is built through that check rather than
    around it.
    """
    app = FastAPI()

    @app.get("/reports/losses")
    def losses(request: Request, start: float, end: float) -> Any:
        header = request.headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or supplied.strip() != REPORT_TOKEN:
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        body = dict(LOSS_REPORT)
        body["window"] = {"start": start, "end": end}
        return JSONResponse(content=body)

    return app


def _stub_trust(snapshot: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> Any:
    """A stand-in for the trust service's ``GET /snapshot`` and ``GET /events``.

    It serves the WHOLE market -- all five stores' postures and the whole chain -- because the
    narrowing to one store is the dashboard's own job (R9: a rival's row must never reach the
    browser), and a stub that pre-narrowed would be a stub agreeing with itself.
    """
    app = FastAPI()
    rows = [dict(event) for event in events]

    @app.get("/snapshot")
    def snap() -> Any:
        return JSONResponse(content=dict(snapshot))

    @app.get("/events")
    def read_events(store_id: str | None = None, limit: int = 50, after_seq: int = 0) -> Any:
        mine = [row for row in rows if store_id is None or row.get("store_id") == store_id]
        page = mine[:limit]
        return JSONResponse(
            content={
                "events": page,
                "count": len(page),
                "limit": limit,
                "truncated": len(page) < len(mine),
                "next_after_seq": after_seq,
                "is_chain": False,
            }
        )

    return app


def _store_agent_app(activation: str, terms: Mapping[str, Any] = ENVELOPE_DOCUMENT) -> Any:
    """The REAL ``store_agent`` app, advocating for this store, in one activation state.

    Real and not a stub, for the reason ``apps/merchant/svc/tests/_fixtures_dashboard.py``
    gives: the kill switch is the one control whose proof must not be a stub agreeing with
    itself. Every decline token in the shipped page came out of this agent's own contract path,
    and the one offer came out of its own bidding runtime inside the stated walls.
    """
    from store_agent.main import create_app
    from store_agent.solicitation.serving import configure_solicitation

    envelope = dict(terms)
    envelope["activation"] = activation
    context = {
        "store_id": DEFAULT_STORE,
        "store_domain": f"{DEFAULT_STORE}.myshopify.com",
        "envelope": envelope,
        "catalog": CATALOG,
        "live_state": {ref: {"in_stock": True, "units_left": 9} for ref in CATALOG},
        "learned_policy": None,
        "network_priors": {CLUSTER_ID: {"depth_buckets": [0.0, 0.05, 0.1, 0.15]}},
    }
    app = create_app()
    configure_solicitation(app, context=context)
    return app


# ======================================================================================
# Driving the real route
# ======================================================================================
async def _drive_dashboard(store_id: str, snapshot: Mapping[str, Any], chain: Any) -> Any:
    """Onboard, probe, approve, bid, decline, edit, re-approve -- then read the page back.

    Every step is the served route, in the order an operator would perform them, and that is
    what makes the shipped ``bids`` panel evidence rather than decoration. Three solicitations
    against a real agent, in two activation states:

    ============================  ==============================================================
    state at the probe            what the agent answered
    ============================  ==============================================================
    envelope in ``shadow``        ``204 envelope_not_activated`` -- nothing a merchant says in
                                  an interview activates anything; the approval does
    envelope ``active``           ``200`` with an offer inside the stated floors and cap
    a cluster it does not pursue  ``204 cluster_not_pursued``
    ============================  ==============================================================

    **Why there is no ``store_killed`` row, stated rather than left as a gap.** R9's kill
    switch works and is observable -- but in this build it is TERMINAL, so a page that
    demonstrated it could only be the page of a permanently dead store. Measured on
    ``merchant_svc.envelope.store.EnvelopeVersions``: ``kill`` files the current version as
    ``killed``; ``activate`` then refuses with *"it is not reactivated by an approval -- publish
    a new version and approve that"*; and ``put`` reaches that new version through
    ``edit_envelope``, which carries the head's activation forward, so v2 is born ``killed`` and
    ``activate`` refuses it in the same words. The service's own stated remedy does not work,
    which is a defect in ``apps/merchant`` and not something this producer papers over by
    shipping a dead store as the demo's resting state. ``deploy/demo-seed/README.md`` says what
    that row would look like.
    """
    import httpx
    import merchant_svc.bidding.gate as gate
    import merchant_svc.dashboard.journal as journal
    import merchant_svc.dashboard.routes as routes
    import merchant_svc.dashboard.upstream as upstream
    import merchant_svc.onboarding.routes as onboarding
    from merchant_svc.envelope.store import EnvelopeVersions

    finish = datetime.fromisoformat(OBSERVED_AT).timestamp()
    begin = finish - timedelta(days=LOSS_WINDOW_DAYS).total_seconds()
    headers = {"authorization": f"Bearer {ADMIN_TOKEN}"}

    registry: dict[str, Any] = {
        EXCHANGE_URL: _stub_exchange(),
        TRUST_URL: _stub_trust(snapshot, chain),
        STORE_AGENT_URL: _store_agent_app("shadow"),
    }

    def open_client(base_url: str, *, timeout: float = 5.0) -> Any:
        app = registry.get(base_url.rstrip("/"))
        if app is None:
            raise SeedPanelError(f"nothing is registered at {base_url!r}")
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=base_url, timeout=timeout
        )

    versions = EnvelopeVersions()
    saved = (
        routes.ENVELOPES,
        onboarding.ENVELOPES,
        gate.ENVELOPES,
        routes.SOLICITATIONS,
        upstream.open_client,
    )
    routes.ENVELOPES = versions
    onboarding.ENVELOPES = versions
    gate.ENVELOPES = versions
    routes.SOLICITATIONS = journal.SolicitationJournal()
    upstream.open_client = open_client
    try:
        from merchant_svc.main import create_app

        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://merchant") as client:

            async def read_page() -> dict[str, Any]:
                answer = await client.get(
                    f"/stores/{store_id}/dashboard",
                    headers=headers,
                    params={"start": begin, "end": finish},
                )
                if answer.status_code != 200:
                    raise SeedPanelError(
                        f"the dashboard refused the read: {answer.status_code} {answer.text}"
                    )
                page: dict[str, Any] = answer.json()
                return page

            async def solicit(expected: str, body: dict[str, Any]) -> dict[str, Any]:
                answer = await client.post(
                    f"/stores/{store_id}/bids/solicit", headers=headers, json=body
                )
                if answer.status_code != 200:
                    raise SeedPanelError(
                        f"the solicitation was refused: {answer.status_code} {answer.text}"
                    )
                row: dict[str, Any] = answer.json()
                got = row["outcome"] if row["outcome"] == "bid" else str(row.get("decline_reason"))
                if got != expected:
                    raise SeedPanelError(
                        f"the store's own agent answered {got!r} where the page needs "
                        f"{expected!r}; shipping the row anyway would put a state in the "
                        "artifact that the README describes wrongly"
                    )
                return row

            async def publish(terms: Mapping[str, Any]) -> None:
                written = await client.put(
                    f"/stores/{store_id}/envelope", headers=headers, json=dict(terms)
                )
                if written.status_code != 200:
                    raise SeedPanelError(
                        f"the envelope was refused: {written.status_code} {written.text}"
                    )

            async def approve_current() -> None:
                """Sign the artifact the page is offering, and activate the version it covers.

                Read off the page each time rather than computed here: the artifact arrives
                carrying ONLY ``envelope_hash`` and ``needs: [approver, approved_at]``, and
                minting a digest in this script would be re-implementing
                ``merchant_svc.envelope.digest.approval_digest`` outside the service -- the
                exact gap the onboarding panel exists to close.
                """
                offered = (await read_page())["onboarding"]["approval"]
                if not offered:
                    raise SeedPanelError(
                        "the onboarding panel offered no approval artifact, so this envelope "
                        "cannot be activated the way a merchant would activate it"
                    )
                artifact = dict(offered["artifact"])
                artifact["approver"] = APPROVER
                artifact["approved_at"] = OBSERVED_AT
                signed = await client.put(
                    f"/stores/{store_id}/envelope",
                    headers={**headers, offered["header"]: json.dumps(artifact)},
                    json={"activation": "active"},
                )
                if signed.status_code != 200:
                    raise SeedPanelError(
                        f"the approval was refused: {signed.status_code} {signed.text}"
                    )

            query = {"query": "a 12 oz bag of washed single-origin whole bean"}

            await publish(ENVELOPE_DOCUMENT)
            await solicit("envelope_not_activated", query)
            await approve_current()
            registry[STORE_AGENT_URL] = _store_agent_app("active")
            await solicit("bid", query)
            await solicit("cluster_not_pursued", {**query, "cluster_id": UNPURSUED_CLUSTER_ID})

            # The merchant's answer to their own loss report: six of nine losses on price, so
            # the discount cap goes to 18%. A new version is born in shadow whatever the body
            # says, and needs its own approval -- an approval collected against v1 does not
            # activate v2, because v2's terms hash differently.
            await publish(ENVELOPE_VERSION_2)
            await approve_current()
            registry[STORE_AGENT_URL] = _store_agent_app("active", ENVELOPE_VERSION_2)
            return await read_page()
    finally:
        (
            routes.ENVELOPES,
            onboarding.ENVELOPES,
            gate.ENVELOPES,
            routes.SOLICITATIONS,
            upstream.open_client,
        ) = saved


def build_dashboard_page(store_id: str = DEFAULT_STORE) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(page, facts)`` -- one complete ``DashboardPage`` and what its provenance needs."""
    import asyncio

    from trust.snapshot.routes import published_entry

    artifact, mine, postures = _seeded_chain(store_id)
    published = {sid: published_entry(entry) for sid, entry in postures.items()}
    chain = [dict(event) for event in artifact.events]

    with _stated_environment(), _frozen_service_clocks():
        page = asyncio.run(_drive_dashboard(store_id, published, chain))

    _refuse_an_unmarked_page(page, store_id=store_id, chain_for_this_store=mine)

    seeded, organic = provenance.partition_events(chain)
    dims = page["trust"]["snapshot"]["dims"]
    facts = {
        "store_id": store_id,
        "corpus": str(artifact.root.relative_to(REPO_ROOT)),
        "as_of": artifact.as_of,
        "chain_events_for_this_store": len(mine),
        "chain_events_total": len(chain),
        "seeded_events_total": len(seeded),
        "organic_events_total": len(organic),
        "first_event_hash": str(mine[0].get("event_hash") or ""),
        "last_event_hash": str(mine[-1].get("event_hash") or ""),
        "dimensions": sorted(dims),
        "moved_dimensions": sorted(
            name for name, state in dims.items() if (state["alpha"], state["beta"]) != (2.0, 2.0)
        ),
        "solicitations": len(page["bids"].get("entries") or ()),
        "outcomes": [
            row["outcome"] if row["outcome"] == "bid" else str(row.get("decline_reason"))
            for row in (page["bids"].get("entries") or ())
        ],
    }
    return page, facts


def _refuse_an_unmarked_page(
    page: Mapping[str, Any], *, store_id: str, chain_for_this_store: Sequence[Mapping[str, Any]]
) -> None:
    """Refuse to write a page whose manufactured rows are not marked as manufactured.

    The same refusal :class:`seed.population._Services` makes before the first byte leaves, and
    for the same reason: an unmarked manufactured observation is the one thing no later repair
    can undo, because a ledger has no delete. Checked on the BUILT page rather than on the
    inputs, so a narrowing that silently dropped rows is caught here and not by a reader.
    """
    for panel in ("onboarding", "envelope", "losses", "trust", "trust_events", "bids"):
        state = page.get(panel, {}).get("state")
        if state != "ok":
            raise SeedPanelError(
                f"the {panel!r} panel came back {state!r}, not 'ok'. A refusal state is a "
                "sentence about this producer's configuration, not a market, and shipping one "
                "would seed the demo with a misconfiguration."
            )

    snapshot = page["trust"].get("snapshot")
    if not isinstance(snapshot, Mapping) or snapshot.get("store_id") != store_id:
        raise SeedPanelError(
            f"the trust panel carries {snapshot!r} rather than {store_id}'s own row"
        )
    from trust.scoring.dimensions import TRUST_DIMENSIONS

    missing = sorted(set(TRUST_DIMENSIONS) - set(snapshot.get("dims") or ()))
    if missing:
        raise SeedPanelError(
            f"the trust snapshot names no {missing}; D53 says EXACTLY six dimensions and the "
            "schema refuses a partial one, which scores differently from the one served"
        )

    events = page["trust_events"].get("events") or []
    if len(events) != len(chain_for_this_store):
        raise SeedPanelError(
            f"the page carries {len(events)} trust events and the chain holds "
            f"{len(chain_for_this_store)} for {store_id!r}; the narrowing dropped rows the "
            "marker would have been counted over"
        )
    unmarked = [row for row in events if not provenance.is_seeded_event(row)]
    if unmarked:
        raise SeedPanelError(
            f"{len(unmarked)} events on the built page carry no {SIMULATED_ORDER_PREFIX!r} "
            "marker, so the page cannot claim every observation on it was manufactured"
        )

    rows = page["bids"].get("entries") or []
    unmarked_rows = [row for row in rows if SIMULATED_ORDER_PREFIX not in str(row["auction_id"])]
    if unmarked_rows:
        raise SeedPanelError(
            f"{len(unmarked_rows)} solicitation rows carry no {SIMULATED_ORDER_PREFIX!r} in "
            "their auction_id, so a reader could not tell a rehearsal this script made from "
            "one a merchant made"
        )


# ======================================================================================
# Provenance
# ======================================================================================
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_revision() -> dict[str, Any]:
    """``{commit, dirty}`` for the checkout that produced this panel, best-effort.

    The seed corpus's own helper, reused rather than reimplemented, and for the reason it gives:
    this payload is a record of what the real merchant service, the real envelope algebra and
    the real store agent did, so a reader comparing bytes needs to know which revision to
    compare at. An unavailable git is recorded as ``""`` rather than guessed.
    """
    return dict(seed_store._source_revision())


def _producer_command(store_id: str) -> str:
    """The exact command that reproduces these bytes, with every default resolved.

    Synthesised rather than echoed from ``sys.argv``: a record that quoted the argv of a run
    made with no arguments would tell the next reader nothing about which store it was for.
    """
    return f"python scripts/build_seed_dashboard.py --store {store_id}"


def _marker(facts: Mapping[str, Any]) -> dict[str, Any]:
    """How a reader tells this manufactured page from an earned one -- panel by panel.

    The per-panel table is the point. Three different strengths of marker sit on one page, and
    collapsing them into a single ``simulated: true`` would overclaim for four of the six.
    """
    return {
        "field": provenance.SEED_MARKER_FIELD,
        "prefix": provenance.SEED_MARKER_PREFIX,
        "reader": "seed.provenance.is_seeded_event",
        "audit": "seed.provenance.posture_split",
        "why": (
            "The observations behind this page's trust panels were manufactured, and the marker "
            "for them is inside the hash chain: the buyer service copies `order_ref` verbatim "
            "onto the sealed event, so a seeded observation cannot be un-marked and an earned "
            "one cannot be marked without breaking GET /events/verify. That is true of the two "
            "trust panels and of nothing else on this page, which is what `panels` is for."
        ),
        "panels": {
            "trust_events": {
                "strength": "hash-chain fact",
                "how": (
                    "every event carries `order_ref` beginning "
                    f"`{provenance.SEED_MARKER_PREFIX}`, and that field is covered by "
                    "`event_hash`, which `prev_hash` links to the event before it"
                ),
                "events": facts["chain_events_for_this_store"],
                "first_event_hash": facts["first_event_hash"],
                "last_event_hash": facts["last_event_hash"],
            },
            "trust": {
                "strength": "hash-chain fact, inherited",
                "how": (
                    "recomputed from those events by seed.store.replay_postures through the "
                    "platform's own scorer at the corpus's `as_of`, then projected onto the "
                    "published property set by trust.snapshot.routes.published_entry. The "
                    "snapshot itself carries NO marker -- a posture is a number, and it is "
                    "seeded only because the events under it are"
                ),
                "as_of": facts["as_of"],
                "dimensions": facts["dimensions"],
                "moved_by_the_corpus": facts["moved_dimensions"],
                "note": (
                    "five of the six dimensions sit at the neutral Beta(2,2) prior because the "
                    "only observations in this corpus are post-purchase feedback. That is the "
                    "corpus being honest, not the panel being incomplete"
                ),
            },
            "bids": {
                "strength": "reserved id prefix -- WEAKER, and nothing seals it",
                "how": (
                    "each `auction_id` is `dashboard-rehearsal-"
                    f"{provenance.SEED_MARKER_PREFIX}<uuid5>`. A live dashboard mints that id "
                    "from `uuid.uuid4()`, which cannot produce this substring, so the prefix "
                    "does separate these rows from a merchant's own rehearsals. It is NOT a "
                    "ledger row: nothing hashes it, no chain links it, and an edit to one of "
                    "these rows is detectable only by the sha256 in this file"
                ),
                "entries": facts["solicitations"],
                "outcomes": facts["outcomes"],
            },
            "losses": {
                "strength": "none -- stated, and unmarked",
                "how": (
                    "the loss report is deployment configuration stated in "
                    "scripts/build_seed_dashboard.py. `LossReport` is additionalProperties: "
                    "false, so there is nowhere in it to put a marker and none is smuggled "
                    "into a cluster id. A reader tells it from a real report by the fact that "
                    "it is in this file"
                ),
            },
            "envelope": {
                "strength": "none -- stated walls, real algebra",
                "how": (
                    "the terms are stated here; the version history, the activation state and "
                    "the approval digest over them are the merchant service's own output"
                ),
                "approver": APPROVER,
            },
            "onboarding": {
                "strength": "none -- the service's own script",
                "how": (
                    "merchant_svc.onboarding.script authored every question; the answers "
                    "carried in the envelope above are stated"
                ),
            },
        },
        "seeded_events": facts["seeded_events_total"],
        "organic_events": facts["organic_events_total"],
        "corpus": facts["corpus"],
    }


def _collection(page: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    payload = seed_store.canonical_bytes(page) + b"\n"
    store_id = str(facts["store_id"])
    return {
        "artifact_version": ARTIFACT_VERSION,
        "kind": ARTIFACT_KIND,
        # Redundant with `marker` below and stated anyway, at the top, in one word: the first
        # thing anyone opening this file must learn is that this market was manufactured.
        "simulated": True,
        # STATED, not stamped. Everything this producer writes is a pure function of the seeded
        # corpus and the constants in `scripts/build_seed_dashboard.py`, so a wall clock here
        # would make `--check` report drift on every run of a generator whose inputs had not
        # changed. The value is after the corpus's `as_of` because a page carrying those events
        # cannot honestly predate them.
        "captured_at": OBSERVED_AT,
        "captured_at_is": "stated, not a clock; see determinism.stamped_by_the_producer",
        "producer": _producer_command(store_id),
        "producer_module": "merchant_svc.dashboard.routes.read_dashboard",
        "producer_script": "scripts.build_seed_dashboard",
        "store_id": store_id,
        "source": _source_revision(),
        "marker": _marker(facts),
        "determinism": {
            "canonical_file": DASHBOARD_FILE,
            "canonical_digest": _sha256(payload),
            "reproduce": _producer_command(store_id),
            "check": "python scripts/build_seed_dashboard.py --check",
            "reproducible_files": [DASHBOARD_FILE],
            "volatile_files": [COLLECTION_FILE],
            "volatile_fields": ["source"],
            # The four places a clock or a random id would otherwise reach the payload. Named
            # so a reader comparing this page against a LIVE dashboard is not surprised: a live
            # page's `generated_at`, `recorded_at`, offer expiry and rehearsal ids all move,
            # and these do not.
            "stamped_by_the_producer": [
                "`generated_at` and each solicitation's `offer.expires_at`, from "
                "`merchant_svc.dashboard.routes`'s clock, frozen at `captured_at`",
                "each solicitation's `recorded_at`, from `merchant_svc.dashboard.journal._now`,"
                " frozen at `captured_at`",
                "each rehearsal `auction_id`, minted live from `uuid.uuid4()` and here from a "
                "deterministic uuid5 carrying the `sim-fb-` marker",
                "the loss window, passed explicitly as `start`/`end` rather than defaulted off "
                "`time.time()`",
            ],
            "scope": (
                "byte-identical across processes and machines at the same source revision; a "
                "change to the merchant service, the envelope algebra, the store agent or the "
                "trust scorer moves these bytes and is meant to. See `source`."
            ),
        },
        "files": {"page": DASHBOARD_FILE},
        "file_sha256": {"page": _sha256(payload)},
        "bytes": {"page": len(payload)},
        "notes": [
            "SIMULATED MERCHANT DASHBOARD. No merchant has ever looked at this page, and not "
            "one auction it reports was ever run.",
            "The payload is what `GET /stores/{store_id}/dashboard` returned, driven in-process "
            "over the real routes: the real envelope algebra, the real approval digest, the "
            "real narrowing of a whole-market trust snapshot down to one store, and a real "
            "store agent answering three real POST /v1/bid-requests in two activation states.",
            "The trust and trust_events panels are NOT stated. They are the seeded feedback "
            "corpus at services/sim/seed-data/feedback-population/, whose digests are verified "
            "before this producer reads it and whose postures are recomputed from the chain "
            "offline by seed.store.replay_postures.",
            "The losses panel IS stated, and carries no marker of any kind -- see `marker`.",
            "Going live changes no code on the serving path: point MERCHANT_REPORT_TOKENS, "
            "TRUST_URL and STORE_AGENT_URL at real services and the same route assembles the "
            "same page from real rows. See deploy/demo-seed/README.md.",
        ],
    }


# ======================================================================================
# Writing, loading, checking
# ======================================================================================
@dataclass(frozen=True)
class _Panel:
    """The panel directory, built in memory: what goes in it and what it says about itself."""

    root: Path
    payload_name: str
    payload: dict[str, Any]
    collection: dict[str, Any]

    def rendered(self) -> dict[str, bytes]:
        """``{filename: bytes}`` for every file this panel writes, payload first.

        The payload is written in the canonical form :func:`seed.store.canonical_bytes`
        produces -- sorted keys, tightest separators, one trailing newline -- so the digest of
        the VALUE and the digest of the BYTES are the same number and there is no gap between
        them for an edit to live in. ``collection.json`` is the readable one, and it is the one
        file never digested, exactly as ``services/sim/seed-data`` does it.
        """
        return {
            self.payload_name: seed_store.canonical_bytes(self.payload) + b"\n",
            COLLECTION_FILE: json.dumps(self.collection, indent=2, sort_keys=True).encode("utf-8")
            + b"\n",
        }


def build_panel(store_id: str = DEFAULT_STORE) -> _Panel:
    """The panel, in memory. Writes nothing."""
    page, facts = build_dashboard_page(store_id)
    return _Panel(DASHBOARD_ROOT, DASHBOARD_FILE, page, _collection(page, facts))


@dataclass(frozen=True)
class LoadedPanel:
    """One stored seed panel, loaded and digest-verified."""

    root: Path
    collection: dict[str, Any]
    payload: dict[str, Any]


def load_panel(root: Path | str | None = None) -> LoadedPanel:
    """Read the stored seed panel, verifying every digest its own record claims.

    Args:
        root: the panel directory. Defaults to :data:`DASHBOARD_ROOT`.

    Raises:
        SeedPanelError: the directory is missing, a file is absent, the layout version is newer
            than this reader, a file's bytes do not hash to the recorded value, **or no digest
            is recorded for a file at all**. The last one is not a lesser failure than a wrong
            digest -- it is the same failure with the evidence deleted, and treating it as
            "nothing to check" is the bypass that would let an edit be laundered by removing
            one line from ``collection.json``.
    """
    directory = Path(root) if root is not None else DASHBOARD_ROOT
    record = directory / COLLECTION_FILE
    if not record.is_file():
        raise SeedPanelError(
            f"no seed panel at {directory}: {COLLECTION_FILE} is not there. Produce one with "
            "`python scripts/build_seed_dashboard.py`."
        )
    try:
        collection = json.loads(record.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SeedPanelError(f"{record} is not readable JSON: {exc}") from exc
    if not isinstance(collection, dict):
        raise SeedPanelError(f"{record} is a {type(collection).__name__}, not a record")

    stored = str(collection.get("artifact_version") or "")
    if stored.split(".")[0] != ARTIFACT_VERSION.split(".")[0]:
        raise SeedPanelError(
            f"{record} declares artifact_version {stored!r}; this reader understands "
            f"{ARTIFACT_VERSION!r}. A layout this reader predates is refused rather than "
            "parsed into a plausible-looking wrong answer."
        )

    files = collection.get("files")
    files = files if isinstance(files, Mapping) else {}
    digests = collection.get("file_sha256")
    digests = digests if isinstance(digests, Mapping) else {}
    if not files:
        raise SeedPanelError(f"{record} lists no files, so it accounts for nothing")

    payloads: dict[str, bytes] = {}
    for name, filename in files.items():
        path = directory / str(filename)
        if not path.is_file():
            raise SeedPanelError(f"{record} lists {filename}, and {path} is not there")
        data = path.read_bytes()
        expected = str(digests.get(name) or "")
        if not expected:
            raise SeedPanelError(
                f"{COLLECTION_FILE} records no digest for {filename}, so nothing about these "
                "bytes can be checked. A missing digest is refused exactly as a wrong one is: "
                "deleting the digest is the cheapest way to launder an edit."
            )
        actual = _sha256(data)
        if expected != actual:
            raise SeedPanelError(
                f"{path} does not match the digest {COLLECTION_FILE} records for it (recorded "
                f"{expected}, found {actual}). A seed panel whose bytes and whose provenance "
                "disagree is worse than none: it looks accounted for."
            )
        payloads[str(name)] = data

    body = payloads[str(next(iter(files)))]
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise SeedPanelError(f"{directory} holds unreadable JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SeedPanelError(f"{directory}'s payload is a {type(payload).__name__}, not an object")
    return LoadedPanel(root=directory, collection=collection, payload=payload)


def _collection_drift(built: Mapping[str, Any], stored: Mapping[str, Any]) -> list[str]:
    """Which fields of a stored ``collection.json`` disagree with a fresh build.

    ``source`` is skipped, and it is the ONLY field skipped: it records the git revision the
    panel was produced at, so it legitimately moves with every commit. Everything else in the
    record -- the digests, the byte counts, the marker and its counts -- is compared, because a
    record that has drifted from its own payload is exactly the "looks accounted for" failure.
    """
    volatile = set(built.get("determinism", {}).get("volatile_fields") or ())
    names = (set(built) | set(stored)) - volatile
    return sorted(name for name in names if built.get(name) != stored.get(name))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        default=DEFAULT_STORE,
        help=f"the seeded store this page is about (default: {DEFAULT_STORE})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate in memory and report drift instead of writing",
    )
    args = parser.parse_args(argv)

    panel = build_panel(args.store)
    drifted: list[str] = []
    for filename, data in panel.rendered().items():
        path = panel.root / filename
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"wrote {path.relative_to(REPO_ROOT)} ({len(data):,} bytes)")
            continue
        relative = str(path.relative_to(REPO_ROOT))
        if not path.is_file():
            drifted.append(f"{relative} (absent)")
        elif filename == COLLECTION_FILE:
            fields = _collection_drift(
                panel.collection, json.loads(path.read_text(encoding="utf-8"))
            )
            if fields:
                drifted.append(f"{relative} (fields: {', '.join(fields)})")
        elif path.read_bytes() != data:
            drifted.append(relative)

    if args.check:
        if drifted:
            print("this seed panel differs from what the producer implies:", file=sys.stderr)
            for entry in drifted:
                print(f"  {entry}", file=sys.stderr)
            return 1
        print("deploy/demo-seed/merchant-dashboard is in sync with the producer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
