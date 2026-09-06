"""HTTP surface of catalog refresh — the door behind ``POST /refresh/{store_id}`` (T-236).

Mounted by the frozen entrypoint ``services/ingest/src/main.py``, which globs
``services/ingest/src/*/routes.py`` and includes the module-level :data:`router` it finds
here. That glob is the production call path: ``ingest.main.create_app()`` imports this module
and calls ``app.include_router(router)``, so the handler below — and therefore
:func:`ingest.scheduler.catalog.build_catalog_adapter`, the one place a ``CatalogAdapter`` is
actually constructed — is reachable in a running ``proxyshop-ingest`` process.

``POST /refresh/{store_id}`` is the whole published surface of this service in
``packages/contracts/openapi/ingest.openapi.json``, and before this module it was answered by
nothing (measured: 404). The response is exactly the contract's: ``{store_id, job_id,
provenance}``, and nothing else, because that schema is ``additionalProperties: false`` —
a handler that helpfully returned its warnings alongside would be a handler that no longer
matches the document clients are generated from.

Three decisions worth stating.

**The handler is ``def``, not ``async def``.** It drives the synchronous ``SafeHTTPClient``
and the synchronous Neo4j driver. Written ``async def`` it would block the event loop for the
length of a crawl; Starlette runs a sync handler in a worker thread instead, so two stores
refresh concurrently.

**A caller cannot name a URL.** The body carries ``force`` and ``sections`` and no address.
The store's base URL comes from the process's own :class:`~ingest.scheduler.catalog.StoreRegistry`,
configured out of the environment, so this endpoint cannot be turned into a request forwarder
pointed at an operator's private network — the SSRF guard downstream is the second line of
that defence, not the first.

**The runner is module state, and deliberately.** The differential guarantee — "unchanged
content re-extracts nothing" — is a statement about two *requests*. A runner constructed per
request would remember no hashes and could never demonstrate it. T-024 owns the durable,
cadence-driven version of this; until it lands, the in-process runner is what makes the
guarantee observable across two calls to a running service.
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..extraction.claims import DEFAULT_CONFIDENCE_FLOOR
from ..extraction.pipeline import PolicyPageIngestor
from .catalog import (
    AUTHORITY_RANK,
    SOURCE_CLASS,
    CatalogRefreshReport,
    CatalogRefreshRunner,
    StoreRegistry,
    UnknownStore,
    observed_now,
)

__all__ = [
    "POLICIES",
    "PRODUCTS",
    "RefreshRequest",
    "ingestor_for",
    "registry",
    "router",
    "runner",
]

_log = logging.getLogger(__name__)

router = APIRouter(tags=["ingest", "scheduler"])

#: The two things a refresh can re-read. ``products`` is the catalog (C6's adapter seam);
#: ``policies`` is the policy-page claim extraction T-021 owns. The contract publishes the
#: field as a free array of strings, so an unknown section is refused loudly rather than
#: silently ignored — a caller who asks for ``"policys"`` and gets a 202 has been told the
#: work happened when it did not.
PRODUCTS = "products"
POLICIES = "policies"
SECTIONS: tuple[str, ...] = (PRODUCTS, POLICIES)

#: The stores this process may refresh, read from ``PROXYSHOP_INGEST_STORES`` at import.
#: Import-time configuration warnings are kept so a misconfigured store is diagnosable
#: without re-reading the environment.
registry_warnings: list[str] = []
registry = StoreRegistry.from_env(warnings=registry_warnings)
for _problem in registry_warnings:
    # Logged, not merely collected. Held only in a module list, a skipped store was invisible:
    # `/refresh/{id}` answered 404 with no hint that the entry had been REJECTED rather than
    # never configured, which is the difference between a typo and a missing deployment step.
    _log.warning("ingest store configuration: %s", _problem)

#: The process-wide refresh runner. Module state on purpose — see the module docstring.
runner = CatalogRefreshRunner(registry=registry)

#: One policy-page ingestor PER STORE, built on first use. Each holds its own extraction
#: ledger, which is what makes "an unchanged policy page performs no extraction" observable
#: across two refreshes rather than only within one.
#:
#: Per store rather than one for the process, because `force` has to be able to drop what a
#: store remembers. `ExtractionLedger.hashes` is keyed by page ref across every store, so
#: clearing it on one shared ingestor discarded EVERY store's ledger: measured, forcing a
#: refresh of store A threw away all six of store B's remembered pages, and B's next refresh
#: re-extracted work it had already done — silently, and at the cost of real model calls once
#: a real extractor is configured.
_ingestors: dict[str, PolicyPageIngestor] = {}
_ingestors_guard = Lock()


def ingestor_for(store_id: str) -> PolicyPageIngestor:
    """The policy-page ingestor for one store, created on first use."""
    with _ingestors_guard:
        existing = _ingestors.get(str(store_id))
        if existing is None:
            existing = PolicyPageIngestor(confidence_floor=DEFAULT_CONFIDENCE_FLOOR)
            _ingestors[str(store_id)] = existing
        return existing


class RefreshRequest(BaseModel):
    """The published request body of ``POST /refresh/{store_id}``.

    ``extra="forbid"`` mirrors the contract's ``additionalProperties: false``: a client that
    sends a field this service does not implement is told so, rather than having it dropped.
    """

    model_config = ConfigDict(extra="forbid")

    force: bool = Field(
        False,
        description=(
            "Ignore what the previous refresh hashed, so every page counts as changed. "
            "Without it, unchanged content produces no re-extraction and no graph write."
        ),
    )
    sections: list[str] = Field(
        default_factory=list,
        description="Which surfaces to re-read: 'products', 'policies'. Empty means both.",
    )


def _sections_for(payload: RefreshRequest | None) -> tuple[str, ...]:
    """Validate the requested sections, defaulting to everything this service can refresh.

    Raises:
        HTTPException: 422 naming the sections that are not implemented.
    """
    asked = tuple(payload.sections) if payload and payload.sections else SECTIONS
    unknown = [section for section in asked if section not in SECTIONS]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown section(s) {unknown}; this service refreshes {list(SECTIONS)}",
        )
    return tuple(dict.fromkeys(asked))


def _provenance_of(report: CatalogRefreshReport | None, store_id: str) -> dict[str, Any]:
    """DESIGN ``Provenance`` for the facts this refresh will have written.

    A policies-only refresh has no catalog report to take a snapshot ref from, so the ref
    names the run itself. It is never blank: the contract requires the field, and a
    provenance record pointing at nothing satisfies a provenance audit without providing
    provenance.
    """
    if report is not None:
        return report.provenance
    return {
        "source": SOURCE_CLASS,
        "ref": f"snapshot://refresh/{store_id}",
        "observed_at": observed_now(),
        "authority_rank": AUTHORITY_RANK,
    }


def _log_warnings(store_id: str, section: str, warnings: Any) -> None:
    """Surface what a refresh could not read.

    The published 202 is ``additionalProperties: false`` and carries no warnings field, so
    without this there is NO channel at all: a refresh that read 0 of 6 policy pages returned
    a response byte-identical to one that read all 6. Changing the contract is a decision for
    whoever owns it; logging what happened is not, and silence is the one option that is
    certainly wrong.
    """
    for warning in warnings or ():
        _log.warning("refresh store=%s section=%s: %s", store_id, section, warning)


@router.post("/refresh/{store_id}", status_code=202)
def refresh_store_catalog(store_id: str, payload: RefreshRequest | None = None) -> dict[str, Any]:
    """Re-crawl and re-extract one store's catalog and policy pages.

    Runs the store's :class:`~ingest.adapters.base.CatalogAdapter` through
    ``fetch_catalog`` -> ``to_upserts`` -> ``apply_upserts``, which is the ingestion pipeline
    end to end. Answers 202 with the job identifier and the provenance every fact from this
    run carries, per ``packages/contracts/openapi/ingest.openapi.json``.

    Raises:
        HTTPException: 404 when no target is registered for ``store_id``; 422 when the body
            names a section this service does not refresh.
    """
    sections = _sections_for(payload)
    force = bool(payload.force) if payload else False

    # Resolved once, through the runner's registry — the same object the products branch uses.
    # Two references to "the registry" is how the two halves of one request end up with
    # different views of which stores exist.
    try:
        target = runner.registry.get(store_id)
    except UnknownStore as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # One store's whole refresh is ONE critical section. The handler is `def`, so Starlette
    # runs it in a worker thread: without this, two concurrent requests for the same store
    # both crawl the merchant and both write the graph, because each reads the differential
    # ledger before either updates it.
    with runner.lock_for(target.store_id):
        report: CatalogRefreshReport | None = None
        if PRODUCTS in sections:
            report = runner.refresh(target.store_id, force=force)

        if POLICIES in sections:
            ingestor = ingestor_for(target.store_id)
            if force:
                ingestor.ledger.hashes.clear()
            # The runner's posture, not the transport default: an operator crawling a dev
            # store on a private address configures ONE policy for the process, and a refresh
            # whose two halves obeyed different SSRF postures would read the catalog and
            # silently refuse every policy page on the same host.
            policy_report = ingestor.run(
                store_id=target.store_id,
                base_url=target.base_url,
                allowed_hosts=target.allowed_hosts,
                policy=runner.policy,
                budget=runner.budget,
                # A1: a password-protected dev store redirects every page to /password.
                # Without this the catalog half of one refresh unlocked the store and read
                # both products while the policy half read ZERO of six pages and reported six
                # `redirect-loop` refusals — and the 202 looked identical either way.
                storefront_password=target.storefront_password,
            )
            # Through the runner rather than a second copy of the session handling: a policy
            # page's writes and a product's writes are the same `UpsertOp` shape and must
            # reach the graph the same way, including failing the same way when it is
            # unreachable.
            runner.apply(ingestor.to_upserts(policy_report))
            _log_warnings(target.store_id, "policies", policy_report.warnings)

    if report is not None:
        _log_warnings(target.store_id, "products", report.warnings)

    job_id = report.job_id if report is not None else f"crawl-{store_id}-policies"
    return {
        "store_id": store_id,
        "job_id": job_id,
        "provenance": _provenance_of(report, store_id),
    }
