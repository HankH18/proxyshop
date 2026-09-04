"""HTTP surface of entity resolution. Owned by T-022.

Mounted by the frozen entrypoint ``services/ingest/src/main.py``, which globs
``services/ingest/src/*/routes.py`` and includes the module-level :data:`router` it finds
here. That glob is the production call path: ``ingest.main.create_app()`` imports
``ingest.er.routes`` and calls ``app.include_router(router)``, so every handler below — and
through them :func:`~ingest.er.matching.match` — is reachable in a running
``proxyshop-ingest`` process with no wiring anywhere else and no edit to a file this ticket
does not own.

==========================================  =============================================
``POST /er/match``                          score one pair and report the decision, the
                                            method that produced it and the evidence.
``POST /er/resolve``                        block, score and return the ``SAME_AS`` edges a
                                            batch of records justifies. Proposes; writes
                                            nothing.
``GET  /er/config``                         the published resolution policy: the default
                                            threshold, the weights, the ceilings.
==========================================  =============================================

Three decisions worth stating.

**Resolution here proposes edges; it does not write them.** Writing is
:func:`~ingest.er.linking.link_resolved`, which needs an open Neo4j session *and* a real
:class:`~ingest.graph.model.Source` — ``SAME_AS`` is a material-fact edge, so
``provenance_violations()`` audits it and an edge whose provenance was invented by an HTTP
handler is a defect the audit is designed to catch. A caller that wants the edges persisted
supplies the provenance, and that belongs in the scheduled refresh path (T-024), not here.

**A bad threshold is a 422, not a default.** :func:`~ingest.er.matching.match` refuses a
threshold outside ``(0.0, 1.0]`` because both ends of that range fail *silently* — ``0.0``
links the whole catalog together and ``90`` (a ``0.9`` someone wrote as a percentage) links
nothing at all, forever, with a green test suite. Pydantic enforces the same bound at the
edge so the caller is told which of its numbers was wrong.

**The batch is bounded.** Resolution is quadratic inside a block, so an unbounded request body
is an unbounded amount of work for one HTTP call. The cap is a stated limit rather than a
timeout that fires after the work is already done.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .identity import normalize_gtin
from .linking import (
    DEFAULT_MAX_BLOCK_SIZE,
    MIN_BLOCK_TOKEN_LENGTH,
    edges_as_json,
    resolve,
)
from .matching import (
    BRAND_CONFLICT_CEILING,
    CORROBORATION_WEIGHT,
    DEFAULT_MATCH_THRESHOLD,
    MAX_SIMILARITY_CONFIDENCE,
    NAME_WEIGHT,
    MatchDecision,
    match,
)
from .similarity import TOKEN_WEIGHT

__all__ = ["MAX_RESOLVE_RECORDS", "MatchRequest", "ResolveRequest", "router"]

router = APIRouter(prefix="/er", tags=["ingest", "entity-resolution"])

#: The most records one ``/er/resolve`` call will consider. Blocking keeps the comparison
#: count far below ``n(n-1)/2``, but the worst case is still quadratic, and a limit that is
#: published is a limit a caller can page around.
MAX_RESOLVE_RECORDS = 500


class MatchRequest(BaseModel):
    """Two catalog records to score against each other."""

    model_config = ConfigDict(extra="forbid")

    left: dict[str, Any] = Field(
        ..., description="A record: product_id, gtin, canonical_name, brand."
    )
    right: dict[str, Any] = Field(..., description="The other record, same shape.")
    threshold: float = Field(
        DEFAULT_MATCH_THRESHOLD,
        gt=0.0,
        le=1.0,
        description="The SAME_AS floor. 0.0 would link everything and is refused.",
    )


class ResolveRequest(BaseModel):
    """A catalog slice to resolve into SAME_AS edges."""

    model_config = ConfigDict(extra="forbid")

    records: list[dict[str, Any]] = Field(..., max_length=MAX_RESOLVE_RECORDS)
    threshold: float = Field(DEFAULT_MATCH_THRESHOLD, gt=0.0, le=1.0)
    max_block_size: int = Field(DEFAULT_MAX_BLOCK_SIZE, ge=2, le=10_000)


def _decision_json(decision: MatchDecision) -> dict[str, Any]:
    """One pairwise decision, with the evidence that produced it.

    The evidence is in the response on purpose: a resolver that answers "linked: false" and
    nothing else cannot be debugged against a merchant who insists the two listings are the
    same product, and a confidence with no components behind it is an oracle, not a score.
    """
    return {
        "linked": decision.linked,
        "confidence": decision.confidence,
        "method": decision.method,
        "threshold": decision.threshold,
        "pair": list(decision.pair),
        "evidence": dict(decision.evidence),
    }


@router.get("/config")
def read_config() -> dict[str, Any]:
    """The published resolution policy, so a caller need not hard-code the threshold."""
    return {
        "default_threshold": DEFAULT_MATCH_THRESHOLD,
        "name_weight": NAME_WEIGHT,
        "corroboration_weight": CORROBORATION_WEIGHT,
        "token_weight": TOKEN_WEIGHT,
        "brand_conflict_ceiling": BRAND_CONFLICT_CEILING,
        "max_similarity_confidence": MAX_SIMILARITY_CONFIDENCE,
        "max_block_size": DEFAULT_MAX_BLOCK_SIZE,
        "min_block_token_length": MIN_BLOCK_TOKEN_LENGTH,
        "max_resolve_records": MAX_RESOLVE_RECORDS,
    }


@router.post("/match")
def match_pair(payload: MatchRequest) -> dict[str, Any]:
    """Score one pair of catalog records.

    The canonical GTIN each side reduced to is echoed back, because "these two GTINs are the
    same number written differently" and "one of these GTINs failed its check digit" are the
    two questions a caller debugging an unexpected answer actually has.
    """
    decision = match(payload.left, payload.right, payload.threshold)
    body = _decision_json(decision)
    body["normalized_gtin"] = {
        "left": normalize_gtin(payload.left.get("gtin")),
        "right": normalize_gtin(payload.right.get("gtin")),
    }
    return body


@router.post("/resolve")
def resolve_records(payload: ResolveRequest) -> dict[str, Any]:
    """Block, score and return the SAME_AS edges a batch of records justifies.

    ``dropped_blocks`` is in the response for the same reason it is in the report: a pass that
    skipped an over-large block found less than it could have, and a caller that cannot see
    that cannot tell it from a catalog with no duplicates in it.
    """
    report = resolve(payload.records, payload.threshold, max_block_size=payload.max_block_size)
    return {
        "threshold": report.threshold,
        "records": report.records,
        "compared": report.compared,
        "dropped_blocks": list(report.dropped_blocks),
        "edges": edges_as_json(report.edges),
    }
