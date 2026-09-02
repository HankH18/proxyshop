"""Policy pages in, provenanced claims in the graph out (T-021).

The whole ticket, in order:

1. **Fetch** the store's shipping / returns / warranty pages through T-020's guarded
   transport — SSRF guard, robots obedience, crawl budget, identified bot. Not
   reimplemented here; :class:`PolicyPageFetcher` is a thin, honest caller of
   ``ingest.adapters``.
2. **Hash** each body with T-020's ``content_hash`` and short-circuit on it. An unchanged
   page never reaches an extractor, which is where "re-run performs no LLM calls" actually
   lives.
3. **Extract** atomic claims — the deterministic rule table by default, a Haiku-class
   model behind a strict schema when one is configured.
4. **Provenance** every claim with the snapshot it came from, and **partition** the batch
   at the confidence floor: at or above it is upsertable, below it is quarantined.
5. **Write** the survivors as ``(PolicyPage)-[:STATES]->(AttributeValue)``, each node
   carrying ``SUPPORTED_BY -> (:Source{source_class:'scraped'})``.

Step 5 goes through ``ingest.graph.upsert`` rather than around it, because that library
takes ``source`` as a required keyword-only argument on every material-fact write. There
is no code path here that could write a claim without naming where it came from, and
``ingest.graph.upsert.assert_provenance_complete`` audits the resulting graph rather than
these call sites.

Why the writes are described as :class:`~ingest.adapters.base.UpsertOp` records instead of
performed: it is the same seam T-020 chose, for the same reason — a caller can count the
work before doing any of it, which is how "unchanged content produced zero work" gets
asserted directly instead of inferred from a log.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..adapters.base import UpsertOp
from ..adapters.budgets import BudgetExceeded, CrawlBudget, CrawlLedger
from ..adapters.hashing import content_hash, has_changed, snapshot_ref
from ..adapters.netguard import FetchPolicy, FetchRefused
from ..adapters.robots import USER_AGENT, may_fetch, robots_url, robots_verdict_for_status
from ..adapters.transport import SafeHTTPClient, TransportError
from ..graph.model import PolicyPage
from .claims import (
    DEFAULT_CONFIDENCE_FLOOR,
    EXTRACTOR_VERSION,
    QUARANTINE_SCHEMA,
    QUARANTINE_UNTRUSTED,
    ClaimProvenance,
    ExtractedClaim,
    ExtractionResult,
    normalise_provenance,
    partition_by_confidence,
)
from .differential import ExtractionLedger
from .llm_extractor import SchemaViolation
from .pages import injection_markers, page_kind_for, page_text
from .rules import RuleExtractor

__all__ = [
    "DEFAULT_POLICY_PATHS",
    "PolicyDocument",
    "PolicyPageFetcher",
    "PolicyPageIngestor",
    "apply_policy_upserts",
    "extract_claims",
    "extract_policy_page",
    "policy_page_id",
    "to_upserts",
]

#: Where a Shopify-shaped storefront keeps the pages this ticket is about. Ordered so the
#: three the objective names — shipping, returns, guarantee — are tried first.
DEFAULT_POLICY_PATHS: tuple[str, ...] = (
    "/policies/shipping-policy",
    "/policies/refund-policy",
    "/pages/shipping",
    "/pages/returns",
    "/pages/warranty",
    "/policies/terms-of-service",
)

_TEXTUAL_MEDIA = ("text/html", "text/plain", "application/xhtml+xml", "")


def _now() -> str:
    """UTC now, in the shape T-020's adapter stamps observations with."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def policy_page_id(url: str) -> str:
    """The stable ``PolicyPage.page_id`` for a URL.

    Keyed on the URL and **not** on the content hash: the same page observed twice is one
    node whose ``hash`` and ``snapshot_ref`` move forward, not two nodes. A content-keyed
    id would make "the shipping policy changed" unrepresentable.
    """
    digest = hashlib.sha256(str(url).strip().encode("utf-8")).hexdigest()[:32]
    return f"page_{digest}"


# ---------------------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------------------


def extract_claims(
    text: str,
    source: Any = None,
    *,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    extractor: Any = None,
    url: str = "",
    kind: str = "",
    observed_at: str | None = None,
) -> ExtractionResult:
    """Decompose one page into atomic, provenanced claims and partition them.

    Args:
        text: the page's text. HTML is accepted and reduced to its readable text first.
        source: the provenance descriptor — a ``ClaimProvenance``, a
            ``contracts.Provenance``, a graph ``Source``, or a mapping with any of
            ``source``/``source_class``, ``ref``, ``url``, ``content_hash``,
            ``observed_at``. Whatever it does not name is derived from the page.
        confidence_floor: C10's floor. A claim at or above it may be upserted; below it
            the claim is quarantined.
        extractor: the claim extractor. Defaults to the deterministic rule table, which
            is what makes the approved expectation file (ticket acceptance 1) meaningful.
        url: the page URL, when the source descriptor does not carry one.
        kind: the ``PolicyPage.kind``, passed to a model extractor as context.
        observed_at: observation timestamp; defaults to now.

    Returns:
        An :class:`~ingest.extraction.claims.ExtractionResult` whose ``claims`` are the
        upsert batch and whose ``quarantined`` holds everything kept out of it. Every
        claim on both sides carries a ``provenance`` with a snapshot ``ref``.

    Raises:
        ValueError: the source descriptor names neither a ``ref`` nor a ``url``, so no
            traceable provenance could be built.
    """
    body = page_text(text) if "<" in str(text or "") else str(text or "")
    digest = content_hash(body)
    provenance = normalise_provenance(source, text=body, url=url, observed_at=observed_at)
    markers = injection_markers(body)
    engine = extractor if extractor is not None else RuleExtractor()

    before = int(getattr(engine, "calls", 0))
    warnings: list[str] = []
    schema_failure = ""
    try:
        raw_claims = list(engine.extract(body, url=provenance.url, kind=kind))
    except SchemaViolation as violation:
        # The whole page is quarantined rather than partially trusted: a reply that broke
        # the schema tells us nothing about which of its claims were sound.
        raw_claims = []
        schema_failure = str(violation)
        warnings.append(f"schema violation: {violation}")
    calls = int(getattr(engine, "calls", 0)) - before

    if markers:
        warnings.append(
            "untrusted instruction text in page (ignored, not followed): " + "; ".join(markers[:3])
        )

    built: list[ExtractedClaim] = []
    for raw in raw_claims:
        claim = ExtractedClaim(
            key=raw.key,
            value=raw.value,
            claim_type=raw.claim_type,
            confidence=float(raw.confidence),
            provenance=provenance.for_claim(raw.key, confidence=float(raw.confidence)),
            span=raw.span,
            evidence=raw.evidence,
            unit=raw.unit,
        )
        if raw.evidence and injection_markers(raw.evidence):
            # The reading was taken out of a sentence that is trying to be an instruction.
            # It is recorded — a page attempting injection is a fact — but never upserted.
            claim = claim.quarantine(QUARANTINE_UNTRUSTED)
        built.append(claim)

    claims, quarantined = partition_by_confidence(tuple(built), confidence_floor)
    return ExtractionResult(
        claims=claims,
        quarantined=quarantined,
        content_hash=provenance.content_hash or digest,
        provenance=provenance,
        confidence_floor=float(confidence_floor),
        extractor=str(getattr(engine, "name", type(engine).__name__)),
        extractor_version=EXTRACTOR_VERSION,
        model_calls=calls if bool(getattr(engine, "uses_model", False)) else 0,
        reused=False,
        warnings=tuple(warnings) + ((QUARANTINE_SCHEMA,) if schema_failure else ()),
    )


# ---------------------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDocument:
    """One fetched policy page: its bytes' digest, its snapshot ref, and its text.

    ``changed`` is the answer the crawl already computed against ``known_hashes``; the
    extractor never has to recompute it, and never extracts when it is ``False``.
    """

    url: str
    kind: str
    text: str
    content_hash: str
    snapshot_ref: str
    observed_at: str
    status: int = 200
    changed: bool = True
    media_type: str = "text/html"

    @property
    def page_id(self) -> str:
        """The stable ``PolicyPage.page_id`` for this URL."""
        return policy_page_id(self.url)

    def as_policy_page(self) -> PolicyPage:
        """The graph node for this observation."""
        return PolicyPage(
            page_id=self.page_id,
            kind=self.kind,
            hash=self.content_hash,
            snapshot_ref=self.snapshot_ref,
        )

    def provenance(self) -> ClaimProvenance:
        """The page-level provenance every claim read out of it specialises."""
        return ClaimProvenance(
            source="scraped",
            ref=self.snapshot_ref,
            observed_at=self.observed_at,
            url=self.url,
            content_hash=self.content_hash,
            extractor_version=EXTRACTOR_VERSION,
        )


class PolicyPageFetcher:
    """Fetches a store's policy pages through T-020's guarded transport.

    Every network guard C6 and C10 require is already built and is used here rather than
    re-implemented: :func:`~ingest.adapters.robots.may_fetch` for robots obedience,
    :class:`~ingest.adapters.transport.SafeHTTPClient` for the SSRF guard, address pinning
    and byte metering, and :class:`~ingest.adapters.budgets.CrawlLedger` for the ceilings.

    Args:
        client: an injected :class:`~ingest.adapters.transport.SafeHTTPClient`. When
            ``None`` a client is built per :meth:`fetch` from the policy it is given.
        user_agent: the identified bot string. Defaults to T-020's.
        paths: the paths to try, relative to the store's base URL.
        clock: observation-timestamp source, for tests.
    """

    def __init__(
        self,
        *,
        client: SafeHTTPClient | None = None,
        user_agent: str = USER_AGENT,
        paths: Sequence[str] = DEFAULT_POLICY_PATHS,
        clock: Any = _now,
    ) -> None:
        self._client = client
        self.user_agent = user_agent
        self.paths = tuple(paths)
        self._clock = clock

    def fetch(
        self,
        base_url: str,
        *,
        allowed_hosts: Sequence[str] = (),
        known_hashes: dict[str, str] | None = None,
        policy: FetchPolicy | None = None,
        budget: CrawlBudget | None = None,
        warnings: list[str] | None = None,
    ) -> tuple[PolicyDocument, ...]:
        """Read every policy page the store publishes and robots.txt permits.

        Args:
            base_url: the storefront root.
            allowed_hosts: hosts a redirect may land on, besides the storefront's own.
            known_hashes: ``{url: content_hash}`` from the previous run. A page whose
                digest matches is returned with ``changed=False`` and is never extracted.
            policy: the SSRF/fetch policy. Defaults to the transport's own.
            budget: the crawl ceilings.
            warnings: a list to append non-fatal problems to.

        Returns:
            One :class:`PolicyDocument` per page successfully read. Pages that 404, that
            robots.txt disallows, or that are not text are simply absent.
        """
        notes = warnings if warnings is not None else []
        seen = dict(known_hashes or {})
        client = self._client or SafeHTTPClient(policy=policy, user_agent=self.user_agent)
        ledger = CrawlLedger(budget or CrawlBudget())
        host = (urlsplit(base_url).hostname or "").lower()
        hosts = tuple({h for h in (*allowed_hosts, host) if h})

        robots_text = self._robots(client, base_url, hosts, ledger, notes)
        documents: list[PolicyDocument] = []
        for path in self.paths:
            url = urljoin(base_url if base_url.endswith("/") else base_url + "/", path.lstrip("/"))
            if not may_fetch(robots_text, url, self.user_agent):
                notes.append(f"robots.txt disallows {url}")
                continue
            document = self._read(client, url, hosts, ledger, seen, notes)
            if document is not None:
                documents.append(document)
        return tuple(documents)

    def _robots(
        self,
        client: SafeHTTPClient,
        base_url: str,
        hosts: Sequence[str],
        ledger: CrawlLedger,
        notes: list[str],
    ) -> str:
        """robots.txt, failing closed exactly as T-020's fetcher does."""
        try:
            result = client.fetch(robots_url(base_url), allowed_hosts=hosts, ledger=ledger)
        except (FetchRefused, TransportError, BudgetExceeded) as exc:
            notes.append(f"robots.txt unreachable ({exc}); treating the store as disallow-all")
            return "User-agent: *\nDisallow: /\n"
        verdict = robots_verdict_for_status(result.status)
        if verdict == "use":
            return result.text()
        if verdict == "allow-all":
            return ""
        notes.append(f"robots.txt returned {result.status}; treating the store as disallow-all")
        return "User-agent: *\nDisallow: /\n"

    def _read(
        self,
        client: SafeHTTPClient,
        url: str,
        hosts: Sequence[str],
        ledger: CrawlLedger,
        known: dict[str, str],
        notes: list[str],
    ) -> PolicyDocument | None:
        """One page, or ``None`` when it is missing, refused or not text."""
        try:
            result = client.fetch(url, allowed_hosts=hosts, ledger=ledger)
        except (FetchRefused, TransportError, BudgetExceeded) as exc:
            notes.append(f"policy page {url} not read: {exc}")
            return None
        if result.status != 200:
            return None
        if result.media_type not in _TEXTUAL_MEDIA:
            notes.append(f"policy page {url} is {result.media_type!r}, not text")
            return None
        digest = result.content_hash or content_hash(result.body)
        return PolicyDocument(
            url=result.final_url or url,
            kind=page_kind_for(url),
            text=page_text(result.text()),
            content_hash=digest,
            snapshot_ref=result.snapshot_ref or snapshot_ref(result.final_url or url, digest),
            observed_at=self._clock(),
            status=result.status,
            changed=has_changed(known.get(result.final_url or url) or known.get(url), digest),
            media_type=result.media_type or "text/html",
        )


# ---------------------------------------------------------------------------------------
# The differential run
# ---------------------------------------------------------------------------------------


def extract_policy_page(
    document: PolicyDocument,
    *,
    ledger: ExtractionLedger | None = None,
    extractor: Any = None,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> ExtractionResult:
    """Extract one page, short-circuiting when its content hash has not moved.

    This is the function ticket acceptance 3 is about. The hash comparison happens before
    ``extractor`` is touched, so an unchanged page produces ``model_calls == 0`` and
    ``reused is True`` — and the cached claims, re-partitioned at whatever floor the
    caller asked for, which costs no model call either.

    Args:
        document: the fetched page.
        ledger: what has already been extracted. ``None`` means "extract unconditionally".
        extractor: the claim extractor; defaults to the rule table.
        confidence_floor: C10's floor.

    Returns:
        The extraction result, with ``reused`` telling the caller which path it took.
    """
    if ledger is None:
        return extract_claims(
            document.text,
            document.provenance(),
            confidence_floor=confidence_floor,
            extractor=extractor,
            kind=document.kind,
        )

    page_ref = document.url
    # The short-circuit, and the only thing standing between an unchanged page and the
    # extractor. `ledger.cached` returns nothing unless the recorded digest still matches
    # the one the crawl just computed over this page's bytes.
    cached = ledger.cached(page_ref, document.content_hash)
    if cached is not None:
        return cached.reused_as(floor=confidence_floor)

    result = extract_claims(
        document.text,
        document.provenance(),
        confidence_floor=confidence_floor,
        extractor=extractor,
        kind=document.kind,
    )
    ledger.record(page_ref, document.content_hash, result)
    return result


@dataclass(frozen=True)
class PolicyIngestReport:
    """What one policy-page ingestion run did, per page and in total."""

    store_id: str
    base_url: str
    observed_at: str
    documents: tuple[PolicyDocument, ...] = ()
    results: tuple[ExtractionResult, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def claims(self) -> tuple[ExtractedClaim, ...]:
        """Every upsertable claim across every page."""
        return tuple(claim for result in self.results for claim in result.claims)

    @property
    def quarantined(self) -> tuple[ExtractedClaim, ...]:
        """Every claim held back, across every page."""
        return tuple(claim for result in self.results for claim in result.quarantined)

    @property
    def model_calls(self) -> int:
        """Total model calls. Zero on a re-run of an unchanged storefront."""
        return sum(result.model_calls for result in self.results)

    @property
    def reused(self) -> tuple[str, ...]:
        """The URLs answered from cache instead of re-extracted."""
        return tuple(
            document.url
            for document, result in zip(self.documents, self.results, strict=False)
            if result.reused
        )

    @property
    def hash_index(self) -> dict[str, str]:
        """``{url: content_hash}``, to feed straight back as the next run's known hashes."""
        return {document.url: document.content_hash for document in self.documents}


class PolicyPageIngestor:
    """Fetch → hash → extract → provenance → graph writes, for one store.

    Args:
        fetcher: the page fetcher. Defaults to a :class:`PolicyPageFetcher` over T-020's
            guarded transport.
        extractor: the claim extractor. Defaults to the deterministic rule table so that
            a verify run needs no model, no key and no socket (D3).
        ledger: the differential ledger; created if not supplied.
        confidence_floor: C10's floor.
        clock: observation-timestamp source.
    """

    def __init__(
        self,
        *,
        fetcher: PolicyPageFetcher | None = None,
        extractor: Any = None,
        ledger: ExtractionLedger | None = None,
        confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
        clock: Any = _now,
    ) -> None:
        self.fetcher = fetcher or PolicyPageFetcher(clock=clock)
        self.extractor = extractor
        self.ledger = ledger if ledger is not None else ExtractionLedger()
        self.confidence_floor = float(confidence_floor)
        self._clock = clock

    def run(
        self,
        *,
        store_id: str,
        base_url: str,
        allowed_hosts: Sequence[str] = (),
        policy: FetchPolicy | None = None,
        budget: CrawlBudget | None = None,
    ) -> PolicyIngestReport:
        """Ingest every policy page this store publishes.

        Returns:
            A :class:`PolicyIngestReport`. ``report.model_calls == 0`` on a run where no
            page's content hash moved.
        """
        warnings: list[str] = []
        documents = self.fetcher.fetch(
            base_url,
            allowed_hosts=allowed_hosts,
            known_hashes=self.ledger.hash_index(),
            policy=policy,
            budget=budget,
            warnings=warnings,
        )
        results = tuple(
            extract_policy_page(
                document,
                ledger=self.ledger,
                extractor=self.extractor,
                confidence_floor=self.confidence_floor,
            )
            for document in documents
        )
        for result in results:
            warnings.extend(result.warnings)
        return PolicyIngestReport(
            store_id=store_id,
            base_url=base_url,
            observed_at=self._clock(),
            documents=documents,
            results=results,
            warnings=tuple(warnings),
        )

    def to_upserts(self, report: PolicyIngestReport) -> list[UpsertOp]:
        """Map a report to graph writes. Pure: no network, no clock, no session."""
        ops: list[UpsertOp] = []
        for document, result in zip(report.documents, report.results, strict=False):
            if not result.claims:
                continue
            ops.extend(to_upserts(document, result))
        return ops


def to_upserts(document: PolicyDocument, result: ExtractionResult) -> list[UpsertOp]:
    """The graph writes one extracted page implies, in dependency order.

    The ``PolicyPage`` comes first because ``link_states`` refuses to attach a ``STATES``
    edge to a node that does not exist yet — ``ingest.graph.upsert`` raises rather than
    creating a placeholder behind your back.

    Args:
        document: the fetched page.
        result: its extraction, already partitioned. **Only** ``result.claims`` is
            written; quarantined claims are deliberately not graph writes.

    Returns:
        A ``policy_page`` op followed by one ``states`` op per upsertable claim.
    """
    if not result.claims:
        return []
    page_source = (result.provenance or document.provenance()).for_claim(
        document.page_id, confidence=1.0
    )
    ops: list[UpsertOp] = [
        UpsertOp(
            kind="policy_page",
            source=page_source.as_source(),
            node=document.as_policy_page(),
        )
    ]
    for claim in result.claims:
        ops.append(
            UpsertOp(
                kind="states",
                source=claim.supported_by,
                node=claim.as_attribute(),
                context={
                    "page_id": document.page_id,
                    "claim_key": claim.key,
                    "claim_type": claim.claim_type,
                    "confidence": claim.confidence,
                },
            )
        )
    return ops


def apply_policy_upserts(session: Any, ops: Iterable[UpsertOp]) -> list[str]:
    """Replay policy-page :class:`~ingest.adapters.base.UpsertOp` records against Neo4j.

    ``ingest.adapters.base.apply_upserts`` handles the catalog kinds and raises on
    anything else; ``"states"`` — the ``(PolicyPage)-[:STATES]->(AttributeValue)`` edge
    this ticket writes — is not one of them, and ``services/ingest/src/adapters/**`` is
    T-020's file. So this dispatcher handles ``"states"`` itself and hands every other
    kind straight to T-020's, rather than forking it.

    Args:
        session: an open ``neo4j.Session`` or transaction.
        ops: the writes, in order.

    Returns:
        The stable IDs written, in order.
    """
    from ..adapters.base import apply_upserts
    from ..graph import upsert as graph_upsert

    written: list[str] = []
    for op in ops:
        if op.kind == "states":
            written.append(
                graph_upsert.link_states(
                    session,
                    page_id=dict(op.context or {})["page_id"],
                    attribute=op.node,
                    source=op.source,
                )
            )
        else:
            written.extend(apply_upserts(session, [op]))
    return written
