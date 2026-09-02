"""Policy-page claim extraction with provenance (T-021, R8/C6/C10).

What this package promises, and where each promise is kept::

    differential.py    content_hash / needs_extraction — unchanged pages cost nothing
    pages.py           HTML -> text, and the untrusted-instruction detector (C10)
    rules.py           the deterministic rule table: atomic claims + confidence
    llm_extractor.py   the Haiku-class extractor, behind a strict validated schema
    claims.py          Claim / Provenance / Source bridging, and the quarantine partition
    pipeline.py        fetch -> hash -> extract -> provenance -> graph writes
    routes.py          the HTTP surface the frozen ``ingest.main`` entrypoint mounts

The three invariants, stated once:

1. **Unchanged content re-extracts nothing.** :func:`needs_extraction` is a local hash
   comparison in front of everything; it opens no socket and calls no model.
2. **Every claim carries provenance.** An :class:`ExtractedClaim` cannot be constructed
   without a :class:`ClaimProvenance` naming a snapshot ref, and every graph write goes
   through ``ingest.graph.upsert``, which takes ``source`` as a required argument.
3. **Low confidence is quarantined, not upserted.** :func:`extract_claims` returns two
   collections whose union is everything it read. Only ``claims`` becomes graph writes;
   ``quarantined`` is kept, with a reason, so a held-back claim is inspectable rather than
   invisible.

Typical use::

    from ingest.extraction import PolicyPageIngestor, apply_policy_upserts

    ingestor = PolicyPageIngestor()
    report = ingestor.run(store_id="store-1", base_url="https://dev.example.com")
    apply_policy_upserts(session, ingestor.to_upserts(report))   # [] when nothing changed
"""

from __future__ import annotations

from .claims import (
    DEFAULT_AUTHORITY_RANK,
    DEFAULT_CONFIDENCE_FLOOR,
    EXTRACTOR_VERSION,
    QUARANTINE_BELOW_FLOOR,
    QUARANTINE_SCHEMA,
    QUARANTINE_UNTRUSTED,
    ClaimProvenance,
    ExtractedClaim,
    ExtractionResult,
    RawClaim,
    normalise_provenance,
    partition_by_confidence,
)
from .differential import (
    ExtractionLedger,
    content_hash,
    has_changed,
    needs_extraction,
    snapshot_ref,
)
from .llm_extractor import (
    EXTRACTION_CONTRACT,
    FENCE_CLOSE,
    FENCE_OPEN,
    LLMClaimExtractor,
    SchemaViolation,
    build_extract_client,
    build_prompt,
    infer_claim_type,
    parse_model_claims,
)
from .pages import (
    INJECTION_PATTERNS,
    POLICY_PAGE_KINDS,
    injection_markers,
    page_kind_for,
    page_text,
)
from .pipeline import (
    DEFAULT_POLICY_PATHS,
    PolicyDocument,
    PolicyIngestReport,
    PolicyPageFetcher,
    PolicyPageIngestor,
    apply_policy_upserts,
    extract_claims,
    extract_policy_page,
    policy_page_id,
    to_upserts,
)
from .rules import CLAIM_TYPES, HEDGE_DISCOUNT, HEDGE_TERMS, RULES, PolicyRule, RuleExtractor

__all__ = [
    "CLAIM_TYPES",
    "DEFAULT_AUTHORITY_RANK",
    "DEFAULT_CONFIDENCE_FLOOR",
    "DEFAULT_POLICY_PATHS",
    "EXTRACTION_CONTRACT",
    "EXTRACTOR_VERSION",
    "FENCE_CLOSE",
    "FENCE_OPEN",
    "HEDGE_DISCOUNT",
    "HEDGE_TERMS",
    "INJECTION_PATTERNS",
    "POLICY_PAGE_KINDS",
    "QUARANTINE_BELOW_FLOOR",
    "QUARANTINE_SCHEMA",
    "QUARANTINE_UNTRUSTED",
    "RULES",
    "ClaimProvenance",
    "ExtractedClaim",
    "ExtractionLedger",
    "ExtractionResult",
    "LLMClaimExtractor",
    "PolicyDocument",
    "PolicyIngestReport",
    "PolicyPageFetcher",
    "PolicyPageIngestor",
    "PolicyRule",
    "RawClaim",
    "RuleExtractor",
    "SchemaViolation",
    "apply_policy_upserts",
    "build_extract_client",
    "build_prompt",
    "content_hash",
    "extract_claims",
    "extract_policy_page",
    "has_changed",
    "infer_claim_type",
    "injection_markers",
    "needs_extraction",
    "normalise_provenance",
    "page_kind_for",
    "page_text",
    "parse_model_claims",
    "partition_by_confidence",
    "policy_page_id",
    "snapshot_ref",
    "to_upserts",
]
