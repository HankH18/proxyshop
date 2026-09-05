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
    "ingestor",
    "registry",
    "router",
    "runner",
]

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

#: The process-wide refresh runner. Module state on purpose — see the module docstring.
runner = CatalogRefreshRunner(registry=registry)

#: The process-wide policy-page ingestor. It holds the extraction ledger, which is what makes
#: "an unchanged policy page performs no extraction" observable across two refreshes rather
#: than only within one.
ingestor = PolicyPageIngestor(confidence_floor=DEFAULT_CONFIDENCE_FLOOR)


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

    report: CatalogRefreshReport | None = None
    if PRODUCTS in sections:
        try:
            report = runner.refresh(store_id, force=force)
        except UnknownStore as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    if POLICIES in sections:
        try:
            target = registry.get(store_id)
        except UnknownStore as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if force:
            ingestor.ledger.hashes.clear()
        policy_report = ingestor.run(
            store_id=target.store_id,
            base_url=target.base_url,
            allowed_hosts=target.allowed_hosts,
        )
        # Through the runner rather than a second copy of the session handling: a policy
        # page's writes and a product's writes are the same `UpsertOp` shape and must reach
        # the graph the same way, including failing the same way when it is unreachable.
        runner.apply(ingestor.to_upserts(policy_report))

    job_id = report.job_id if report is not None else f"crawl-{store_id}-policies"
    return {
        "store_id": store_id,
        "job_id": job_id,
        "provenance": _provenance_of(report, store_id),
    }
