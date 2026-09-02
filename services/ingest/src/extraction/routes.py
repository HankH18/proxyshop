"""HTTP surface of policy-page claim extraction. Owned by T-021.

Mounted by the frozen entrypoint ``services/ingest/src/main.py``, which globs
``services/ingest/src/*/routes.py`` and includes the module-level :data:`router` it finds
here. That glob is the production call path: ``ingest.main.create_app()`` imports
``ingest.extraction.routes`` and calls ``app.include_router(router)``, so every handler
below is reachable in a running ``proxyshop-ingest`` process with no wiring anywhere else.

==========================================  =============================================
``POST /extraction/policy-pages``           extract claims from a page body the caller
                                            already has. Honours ``known_hash``: an
                                            unchanged page returns ``reused: true`` and
                                            ``model_calls: 0`` without extracting.
``POST /extraction/stores/{store_id}``      crawl a storefront's policy pages through the
                                            guarded fetcher and extract each one.
``GET  /extraction/config``                 the published extraction policy: confidence
                                            floor, extractor version, claim vocabulary.
==========================================  =============================================

Three decisions worth stating.

**The handlers are ``def``, not ``async def``.** The fetch path uses the synchronous
``SafeHTTPClient`` and the graph path uses the synchronous Neo4j driver. Written
``async def``, both would block the event loop; Starlette runs a sync handler in a worker
thread instead, so concurrent crawls actually run concurrently.

**Quarantined claims are in the response, not swallowed.** C10 requires low-confidence
extraction to be quarantined rather than upserted; a caller that cannot see what was held
back cannot tell "this page states nothing" from "this page states things we did not
believe".

**The ledger is per-process and lives on the app.** T-024 owns the durable, scheduled
version of this (``services/ingest/src/scheduler/**``, ``depends_on: [T-021, T-023]``);
until it lands, the in-process ledger is what makes the differential guarantee observable
across two requests to a running service rather than only within one call.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .claims import (
    DEFAULT_CONFIDENCE_FLOOR,
    EXTRACTOR_VERSION,
    ExtractedClaim,
    ExtractionResult,
)
from .differential import ExtractionLedger, content_hash, needs_extraction
from .pages import page_kind_for, page_text
from .pipeline import PolicyIngestReport, PolicyPageIngestor, extract_claims
from .rules import CLAIM_TYPES

__all__ = ["ExtractRequest", "StoreCrawlRequest", "ledger", "router"]

router = APIRouter(prefix="/extraction", tags=["ingest", "extraction"])

#: The process-wide differential ledger. Module state on purpose: the "unchanged content
#: performs no extraction" guarantee is about two *requests*, and a per-request ledger
#: could never demonstrate it. T-024 replaces this with a persisted one.
ledger = ExtractionLedger()


class ExtractRequest(BaseModel):
    """One page handed to the extractor directly."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, description="The page's URL; provenance points at it.")
    body: str = Field(..., description="The page as fetched. HTML or plain text.")
    kind: str = Field("", description="PolicyPage.kind; derived from the URL when absent.")
    known_hash: str | None = Field(
        None, description="The digest recorded last time. An unchanged page re-extracts nothing."
    )
    observed_at: str | None = None
    confidence_floor: float = Field(DEFAULT_CONFIDENCE_FLOOR, ge=0.0, le=1.0)


class StoreCrawlRequest(BaseModel):
    """A storefront whose policy pages should be fetched and extracted."""

    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    allowed_hosts: list[str] = Field(default_factory=list)
    confidence_floor: float = Field(DEFAULT_CONFIDENCE_FLOOR, ge=0.0, le=1.0)


def _claim_json(claim: ExtractedClaim) -> dict[str, Any]:
    """One claim, with its provenance inline. DESIGN ``Claim{key, value, provenance}``."""
    return {
        "key": claim.key,
        "value": claim.value,
        "unit": claim.unit,
        "claim_type": claim.claim_type,
        "confidence": claim.confidence,
        "evidence": claim.evidence,
        "span": list(claim.span),
        "quarantine_reason": claim.quarantine_reason,
        "provenance": {
            "source": claim.provenance.source,
            "ref": claim.provenance.ref,
            "observed_at": claim.provenance.observed_at,
            "authority_rank": claim.provenance.authority_rank,
        },
        "supported_by": claim.supported_by.as_properties(),
    }


def _result_json(result: ExtractionResult) -> dict[str, Any]:
    """One extraction run, with both sides of the confidence floor."""
    return {
        "content_hash": result.content_hash,
        "snapshot_ref": result.provenance.ref if result.provenance else "",
        "confidence_floor": result.confidence_floor,
        "extractor": result.extractor,
        "extractor_version": result.extractor_version,
        "model_calls": result.model_calls,
        "reused": result.reused,
        "warnings": list(result.warnings),
        "claims": [_claim_json(claim) for claim in result.claims],
        "quarantined": [_claim_json(claim) for claim in result.quarantined],
    }


@router.get("/config")
def read_config() -> dict[str, Any]:
    """The published extraction policy, so a caller need not hard-code the floor."""
    return {
        "confidence_floor": DEFAULT_CONFIDENCE_FLOOR,
        "extractor_version": EXTRACTOR_VERSION,
        "claim_types": sorted(CLAIM_TYPES),
        "pages_known": len(ledger.hashes),
    }


@router.post("/policy-pages")
def extract_policy_page_body(payload: ExtractRequest) -> dict[str, Any]:
    """Extract atomic, provenanced claims from one policy page.

    The content hash is compared **before** any extractor runs, so a caller replaying an
    unchanged page gets ``reused: true`` and ``model_calls: 0``.

    Raises:
        HTTPException: 422 when the body has no readable text.
    """
    text = page_text(payload.body)
    if not text.strip():
        raise HTTPException(status_code=422, detail="the page body has no readable text")

    digest = content_hash(text)
    if not needs_extraction(payload.known_hash, text):
        cached = ledger.cached(payload.url, digest)
        if isinstance(cached, ExtractionResult):
            return _result_json(cached.reused_as(floor=payload.confidence_floor))
        return {
            "content_hash": digest,
            "snapshot_ref": "",
            "confidence_floor": payload.confidence_floor,
            "extractor": "",
            "extractor_version": EXTRACTOR_VERSION,
            "model_calls": 0,
            "reused": True,
            "warnings": [],
            "claims": [],
            "quarantined": [],
        }

    result = extract_claims(
        text,
        None,
        url=payload.url,
        kind=payload.kind or page_kind_for(payload.url),
        observed_at=payload.observed_at,
        confidence_floor=payload.confidence_floor,
    )
    ledger.record(payload.url, digest, result)
    return _result_json(result)


@router.post("/stores/{store_id}")
def crawl_store(store_id: str, payload: StoreCrawlRequest) -> dict[str, Any]:
    """Fetch a storefront's policy pages through the guarded fetcher and extract each.

    Raises:
        HTTPException: 400 when the path and body disagree about the store.
    """
    if payload.store_id != store_id:
        raise HTTPException(
            status_code=400,
            detail=f"store_id in the path ({store_id!r}) and body ({payload.store_id!r}) differ",
        )
    ingestor = PolicyPageIngestor(ledger=ledger, confidence_floor=payload.confidence_floor)
    report = ingestor.run(
        store_id=store_id,
        base_url=payload.base_url,
        allowed_hosts=tuple(payload.allowed_hosts),
    )
    return _report_json(report, ingestor)


def _report_json(report: PolicyIngestReport, ingestor: PolicyPageIngestor) -> dict[str, Any]:
    """One crawl, page by page, plus the writes it would make."""
    return {
        "store_id": report.store_id,
        "base_url": report.base_url,
        "observed_at": report.observed_at,
        "model_calls": report.model_calls,
        "reused": list(report.reused),
        "warnings": list(report.warnings),
        "hash_index": report.hash_index,
        "upserts": len(ingestor.to_upserts(report)),
        "pages": [
            {
                "url": document.url,
                "kind": document.kind,
                "page_id": document.page_id,
                "changed": document.changed,
                **_result_json(result),
            }
            for document, result in zip(report.documents, report.results, strict=False)
        ],
    }
