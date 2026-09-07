"""S2's second half on the SERVED path: does the delisted store disappear from a shortlist?

A trust score that falls while the store still sits on the buyer's shortlist is not the
property S2 names — it is the half that matters to a buyer, and the half most likely to be
missing. So this module takes the delisting the simulation **sealed into its own ledger** and
drives it all the way to the route a buyer's request actually traverses:

    POST /auctions  ->  body["shortlist"]["slots"]      (exchange.main:create_app)

Everything in that path is the real thing, on real loopback sockets (D40: port 0, reported
back), never a ``TestClient``:

=========================  ===========================================================
``trust.main:create_app``  serves ``GET /snapshot`` over the SIMULATION's own trust
                           observations and the registry its sealed delisting implies
one ASGI store agent       answers the published ``POST /v1/bid-requests`` door per
per rostered store         rostered store, so there is a real bid to shortlist
``exchange.main``          reads one deployment document, binds R12 to the trust
``:create_app``            service (``"eligibility": "trust"``), solicits, ranks,
                           shortlists
=========================  ===========================================================

THE ONE HOP THAT IS STILL A STAND-IN
------------------------------------
:func:`registry_from_sealed_delistings` is not product code and must not be mistaken for it.
See :data:`UNWIRED_HOP`: today nothing in ``apps/``, ``packages/`` or ``services/`` turns a
sealed ``blacklisted`` ledger event into a row in the blacklist registry that
``trust.snapshot.routes.blacklist_for`` reads, so ``GET /snapshot`` reports
``blacklisted: false`` for a store the same engine scored at 0.07 against a 0.35 threshold —
measured on this tree. This function is that missing writer, standing in a test, and it is
built **out of the sealed event's own payload** rather than from a hand-typed identity: the
real writer, when it lands, has to do exactly this and nothing more.

``e2e/test_dishonest.py::test_the_run_seals_the_delisting_a_registry_writer_would_consume``
is the marker that keeps this from being a silent gap — it pins the payload fields the writer
consumes, so the seam cannot quietly change shape while nobody is reading it.

Why the buyer's intent states no hard constraints
--------------------------------------------------
``hard_constraints: []`` — the published shape's "this buyer stated no must-haves". It is not
laziness and it is not the manifest's own two constraints: with no must-haves, **every store
that is solicited and bids reaches the shortlist**, so the adversary's absence from the
shortlist is attributable to trust and to nothing else. Stating the manifest's ``roast_level``
and ``net_weight`` constraints against bid doubles that carry no claims would exclude the
whole roster ``undecidable_hard_constraint`` (R19) and the run would "pass" with an empty
shortlist for a reason that has nothing to do with S2.
"""

from __future__ import annotations

__all__ = [
    "UNWIRED_HOP",
    "ServedShortlistError",
    "denied_store_ids",
    "open_shortlist_over",
    "ranked_store_ids",
    "registry_from_sealed_delistings",
    "shortlist_store_ids",
    "solicited_store_ids",
]

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from proxyshop_support.asgi_server import serve

from .criterion import S2Criterion
from .episode import DELISTING_KIND, DishonestEpisodeRun

#: The one hop between the trust engine's delisting and the served R12 answer that no product
#: code makes yet. Named here, quoted in this file's assertions, so a reader of a red test can
#: tell "the platform does not catch the store" from "the platform catches it and the decision
#: never reaches a registry row".
UNWIRED_HOP = (
    "no product writer turns trust.snapshot's `delistings` — sealed into the ledger by "
    "sim.runner (T-303 a) — into a row in the blacklist registry that "
    "trust.snapshot.routes.blacklist_for reads. Until one exists, "
    "e2e.support.dishonest.served.registry_from_sealed_delistings stands in for it, built "
    "from the sealed event's own payload."
)

#: How long a served request may take before the test would rather fail than hang.
REQUEST_TIMEOUT_SECONDS = 30.0

#: The list price every rostered store is asked to bid against, and the price its agent
#: double answers with. Flat on purpose: this run is about who is ON the shortlist, and a
#: price spread would let a cheap store crowd a dear one out of a slot for a reason that is
#: not trust.
LIST_PRICE = 18.0
BID_UNIT_PRICE = 15.0


class ServedShortlistError(AssertionError):
    """The served exchange answered something this module cannot read as a shortlist."""


def registry_from_sealed_delistings(
    sealed: Iterable[Mapping[str, Any]],
) -> tuple[Any, frozenset[str]]:
    """The blacklist registry the run's SEALED delistings imply, and the stores they name.

    See :data:`UNWIRED_HOP` — this is the missing writer, standing in a test. It reads
    ``payload.business_identity`` and ``payload.reason_code`` off the ledger event itself, so
    a run that sealed nothing produces an EMPTY registry and the served shortlist keeps the
    adversary. That is what makes the served assertion able to fail.

    Returns:
        ``(Blacklist, {store_id, ...})``.
    """
    from trust.scoring import Blacklist

    registry = Blacklist()
    delisted: set[str] = set()
    for event in sealed:
        if str(event.get("kind")) != DELISTING_KIND:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise ServedShortlistError(
                f"a sealed {DELISTING_KIND} event carries no payload: {event!r}"
            )
        identity = str(payload.get("business_identity") or "").strip()
        store_id = str(payload.get("store_id") or "").strip()
        if not identity or not store_id:
            raise ServedShortlistError(
                f"a sealed {DELISTING_KIND} event names no store/identity to bind a registry "
                f"row to: {dict(payload)!r}. {UNWIRED_HOP}"
            )
        registry.add(
            business_identity=identity,
            reason_code=str(payload.get("reason_code") or "trust_score_below_threshold"),
            status="active",
            expires_at=payload.get("expires_at"),
        )
        delisted.add(store_id)
    return registry, frozenset(delisted)


def _roster_rows(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    """``store_id`` / ``business_identity`` / ``domain`` for every approved store."""
    rows: list[dict[str, str]] = []
    for store in manifest.get("stores") or ():
        if not isinstance(store, Mapping):
            continue
        rows.append(
            {
                "store_id": str(store["store_id"]),
                "business_identity": str(store["business_identity"]),
                "domain": str(store["domain"]),
            }
        )
    if not rows:
        raise ServedShortlistError("manifest.stores lists no store to serve an auction over")
    return rows


def _trust_service(records: Sequence[Mapping[str, Any]], registry: Any) -> FastAPI:
    """A real ``trust.main:create_app()`` serving ``GET /snapshot`` over these records.

    The only substitution is a datastore, not a behaviour: ``snapshot_stores`` and
    ``snapshot_blacklist`` are the published ``app.state`` seams the route already reads
    instead of Postgres, so the scoring, the delisting reading and the published projection
    are all the shipping ones.
    """
    from trust.events import InMemoryEventStore
    from trust.main import create_app as create_trust

    app = create_trust()
    app.state.snapshot_stores = [dict(row) for row in records]
    app.state.snapshot_blacklist = registry
    app.state.event_store = InMemoryEventStore()
    return app


def _roster_agents(rows: Sequence[Mapping[str, str]]) -> FastAPI:
    """One published bid door per rostered store, answering the ``Bid`` shape."""
    app = FastAPI(title="dishonest-roster-agent-double")

    def door_for(row: Mapping[str, str]) -> Any:
        # A closure, never a defaulted parameter: FastAPI reads a defaulted `dict` argument
        # as a SECOND embedded body field, and every store then reads as silent.
        def door(body: dict[str, Any]) -> JSONResponse:
            return JSONResponse(
                status_code=200,
                content={
                    "auction_id": str(body.get("auction_id") or ""),
                    "store_id": row["store_id"],
                    "offer": {
                        "product_ref": "prod-1",
                        "unit_price": BID_UNIT_PRICE,
                        "currency": "USD",
                        "commitments": [],
                        "total_price": BID_UNIT_PRICE,
                        "expires_at": "2999-01-01T00:00:00Z",
                        "checkout_url": f"https://{row['domain']}/cart/44352913:1",
                    },
                    "claims": [],
                    "message": None,
                    "agent_version": "dishonest-roster-agent-double/1.0.0",
                    "signature": None,
                    "schema_version": "1.0.0",
                },
            )

        return door

    for row in rows:
        app.post(f"/{row['store_id']}/v1/bid-requests")(door_for(dict(row)))
    return app


def _intent(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The buyer's request, in the published ``Intent`` shape. See the module docstring."""
    fixture = manifest.get("fixture_intent")
    fixture = fixture if isinstance(fixture, Mapping) else {}
    return {
        "intent_id": str(fixture.get("intent_id") or "intent-s2"),
        "cluster_id": str(manifest.get("seed_category") or "cluster-s2"),
        "query": str(fixture.get("text") or "a demonstration purchase"),
        "hard_constraints": [],
        "preferences": [],
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }


def open_shortlist_over(
    run: DishonestEpisodeRun,
    manifest: Mapping[str, Any],
    criterion: S2Criterion,
    *,
    deployment_dir: Path,
    setenv: Callable[[str, str], None],
    delenv: Callable[[str], None],
) -> dict[str, Any]:
    """Run one auction on the SERVED exchange, over the delisting this run sealed.

    Args:
        run: the finished simulation. Its observations become the trust service's evidence
            and its sealed ``blacklisted`` events become the blacklist registry.
        manifest: the approved manifest — the roster and the buyer's intent come from it.
        criterion: the approved S2 numbers, for the failure messages.
        deployment_dir: a writable directory for the deployment document (``tmp_path``).
        setenv / delenv: the caller's environment seam (``monkeypatch.setenv`` and a
            ``delenv`` that does not raise). Passed in so this module stays free of pytest.

    Returns:
        The parsed ``POST /auctions`` response body: ``solicited``, ``denied``, ``ranked``,
        ``excluded`` and ``shortlist.slots``.
    """
    from exchange.composition import ENV_DEPLOYMENT, ENV_DEPLOYMENT_JSON
    from exchange.main import create_app

    rows = _roster_rows(manifest)
    registry, delisted = registry_from_sealed_delistings(run.sealed)
    records = [
        {
            "store_id": row["store_id"],
            "business_identity": row["business_identity"],
            "observations": run.observations_for(row["store_id"]),
        }
        for row in rows
    ]

    if delisted != {criterion.dishonest_store_id}:
        raise ServedShortlistError(
            f"the simulation sealed delistings for {sorted(delisted)}, and this run needs it "
            f"to have sealed exactly {criterion.dishonest_store_id!r}. {criterion.describe()}"
        )

    with serve(_trust_service(records, registry)) as trust_url:
        with serve(_roster_agents(rows)) as agent_url:
            document = {
                "trust_url": trust_url,
                "sellers": [
                    {
                        "store_id": row["store_id"],
                        # The deployment states WHERE this store bids and WHICH host it owns,
                        # and leaves who may participate to the trust service. Without the
                        # word "trust" here a stated seller registry becomes the R12 answer
                        # and the trust service is never asked.
                        "eligibility": "trust",
                        "registered_domain": row["domain"],
                        "bid_endpoint": f"{agent_url}/{row['store_id']}/v1/bid-requests",
                    }
                    for row in rows
                ],
                "checkout_mode": "redirect",
            }
            path = deployment_dir / "s2-deployment.json"
            path.write_text(json.dumps(document, indent=2), encoding="utf-8")
            setenv(ENV_DEPLOYMENT, str(path))
            delenv(ENV_DEPLOYMENT_JSON)

            with serve(create_app()) as exchange_url:
                with httpx.Client(base_url=exchange_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
                    response = client.post(
                        "/auctions",
                        json={
                            "intent": _intent(manifest),
                            "profile": {"pseudonym": "psn-s2-dishonest", "buckets": {}},
                            "roster": [
                                {
                                    "store_id": row["store_id"],
                                    "tier": 1,
                                    "list_price": LIST_PRICE,
                                }
                                for row in rows
                            ],
                        },
                    )
    if response.status_code != 201:
        raise ServedShortlistError(f"POST /auctions -> {response.status_code}: {response.text}")
    body = response.json()
    if not isinstance(body, dict):
        raise ServedShortlistError(f"POST /auctions answered {type(body).__name__}, not an object")
    return body


def solicited_store_ids(body: Mapping[str, Any]) -> frozenset[str]:
    """The stores the served exchange actually asked for a bid."""
    solicited = body.get("solicited")
    if not isinstance(solicited, list):
        raise ServedShortlistError(f"POST /auctions answered no `solicited` list: {body!r}")
    return frozenset(str(store_id) for store_id in solicited)


def denied_store_ids(body: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """``{store_id: denial row}`` for the stores the R12 gate refused."""
    denied = body.get("denied")
    if not isinstance(denied, list):
        raise ServedShortlistError(f"POST /auctions answered no `denied` list: {body!r}")
    rows: dict[str, Mapping[str, Any]] = {}
    for row in denied:
        if not isinstance(row, Mapping):
            raise ServedShortlistError(f"a `denied` entry is not an object: {row!r}")
        rows[str(row.get("store_id"))] = row
    return rows


def ranked_store_ids(body: Mapping[str, Any]) -> frozenset[str]:
    """The stores that survived the ranking gate."""
    ranked = body.get("ranked")
    if not isinstance(ranked, list):
        raise ServedShortlistError(f"POST /auctions answered no `ranked` list: {body!r}")
    return frozenset(str(row.get("store_id")) for row in ranked if isinstance(row, Mapping))


def shortlist_store_ids(body: Mapping[str, Any]) -> frozenset[str]:
    """The stores a buyer is actually shown — one id per ``shortlist.slots`` entry.

    Every slot must resolve to a real store id. A slot this function cannot read raises
    rather than contributing ``None``: a set of ``{None}`` would satisfy "the adversary is
    not in the shortlist" while proving nothing at all, which is the exact shape of the
    vacuous gate this test exists to replace.
    """
    shortlist = body.get("shortlist")
    if not isinstance(shortlist, Mapping):
        raise ServedShortlistError(f"POST /auctions answered no `shortlist` object: {body!r}")
    slots = shortlist.get("slots")
    if not isinstance(slots, list):
        raise ServedShortlistError(f"`shortlist` carries no `slots` list: {shortlist!r}")
    return frozenset(_slot_store_id(slot) for slot in slots)


def _slot_store_id(slot: Any) -> str:
    """The store one shortlist slot belongs to, from both published spellings, cross-checked.

    ``bid_ref`` is ``"<auction_id>:<store_id>"`` and the slot's ``trust_summary`` names the
    store outright. Both are read and required to agree — two published spellings that
    disagree is a defect worth failing on, not a coin flip.
    """
    if not isinstance(slot, Mapping):
        raise ServedShortlistError(f"a shortlist slot is not an object: {slot!r}")

    summary = slot.get("trust_summary")
    named = ""
    if isinstance(summary, Mapping) and summary.get("store_id") is not None:
        named = str(summary["store_id"]).strip()

    reference = str(slot.get("bid_ref") or "")
    derived = reference.rsplit(":", 1)[-1].strip() if ":" in reference else ""

    if named and derived and named != derived:
        raise ServedShortlistError(
            f"a shortlist slot's trust_summary names {named!r} while its bid_ref names "
            f"{derived!r}: {dict(slot)!r}"
        )
    resolved = named or derived
    if not resolved:
        raise ServedShortlistError(
            f"a shortlist slot names no store in either published spelling "
            f"(trust_summary.store_id, bid_ref): {dict(slot)!r}"
        )
    return resolved
