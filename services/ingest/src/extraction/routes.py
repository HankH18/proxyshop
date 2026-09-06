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
across two requests to a running service rather than only within one call. It is a
**bounded** LRU (T-367): ``/extraction/policy-pages`` is unauthenticated and its caller
chooses both the ledger key (``url``) and the retained value (whatever ``body`` extracts
to), so an unbounded ledger grew one permanent entry per request on ordinary traffic. The
ceilings live with the structure in :mod:`ingest.extraction.differential`; the ceilings on
what a single request may *hand* it live here, because they are ceilings on a request.

**Oversized input is refused here rather than by a validator, and the list of what to
refuse is derived rather than written.** Every caller-supplied string is length-checked in
the handler and refused with :class:`~fastapi.HTTPException`, not with Pydantic's
``max_length``, because FastAPI's validation-error response includes the offending ``input``
verbatim: a 5,000-character field rejected by ``max_length`` came back as a 5,142-byte error
body, measured. That turns a refusal into an amplifier and hands an anonymous caller a
mirror. The refusals below name the limit and the observed size and quote nothing.

*Which* fields get checked is the part that had to change. The first version of this bound
listed two field names — ``url`` and ``body``, the two T-367 happened to name — in a tuple
inside the refusal function. ``observed_at`` was a third caller-chosen string on the same
model, unbounded, and retained on the page-level provenance *and* on every claim inside the
``ExtractionResult`` the ledger keeps; a 30,000-character stamp was accepted, retained three
times over per entry, charged nothing by the ledger's own byte budget, and echoed back in a
121,877-byte response. The hole T-367 exists to close was still open through a name nobody
had written down. So the request models now inherit :class:`BoundedTextRequest`, which
derives the checked list from ``model_fields`` and raises at import time if a string field
declares no ceiling. A future field is covered because it exists, not because someone
remembered it.

**``observed_at`` is additionally parsed, not merely capped.** It is a timestamp, and a cap
answers only "how much of this will we keep". A 40-character string that is not a date
passes any cap and then makes every downstream freshness and ordering comparison
meaningless, because this value becomes ``Source.observed_at`` in the graph. The cap runs
first (so no parser is ever pointed at a hostile string) and the parse runs second.
"""

from __future__ import annotations

from types import UnionType
from typing import Any, ClassVar, Union, get_args, get_origin

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..adapters.budgets import CrawlBudget
from .claims import (
    DEFAULT_CONFIDENCE_FLOOR,
    EXTRACTOR_VERSION,
    MAX_OBSERVED_AT_CHARS,
    ExtractedClaim,
    ExtractionResult,
    is_timestamp,
)
from .differential import ExtractionLedger, content_hash, needs_extraction
from .pages import page_kind_for, page_text
from .pipeline import PolicyIngestReport, PolicyPageIngestor, extract_claims
from .rules import CLAIM_TYPES

__all__ = [
    "MAX_ALLOWED_HOSTS",
    "MAX_HOST_CHARS",
    "MAX_KNOWN_HASH_CHARS",
    "MAX_PAGE_BODY_CHARS",
    "MAX_PAGE_KIND_CHARS",
    "MAX_PAGE_URL_CHARS",
    "MAX_STORE_ID_CHARS",
    "BoundedTextRequest",
    "ExtractRequest",
    "StoreCrawlRequest",
    "ledger",
    "router",
]

router = APIRouter(prefix="/extraction", tags=["ingest", "extraction"])

#: Longest ``url`` this endpoint will accept, in characters.
#:
#: Where the number comes from: 2,048 is the de-facto ceiling every HTTP intermediary
#: already enforces — it is under nginx's default 8 KiB ``large_client_header_buffers`` line
#: and at the limit browsers have shipped for two decades — so no reachable policy page has
#: a longer URL, while the T-367 reproduction drove a 30,000-character one. The url is what
#: the ledger is *keyed* by, so this is also the per-entry key cost the ledger's own
#: capacity is multiplied against.
MAX_PAGE_URL_CHARS = 2048

#: Largest ``body`` this endpoint will accept, in characters.
#:
#: Where the number comes from: it is the crawler's own single-response ceiling,
#: ``CrawlBudget.max_response_bytes`` (4 MiB, ``ingest/adapters/budgets.py``). The two doors
#: into this extractor should not disagree about how big a policy page may be: a page the
#: guarded fetcher would refuse to download must not be acceptable when a caller pastes it
#: in instead. Reusing the constant means the answer stays one number when it is retuned.
#: Characters rather than bytes because the parsed field is already a ``str``; a UTF-8
#: character is at least one byte, so the character count never *under*-states the payload.
MAX_PAGE_BODY_CHARS = CrawlBudget().max_response_bytes

#: Longest ``kind`` this endpoint will accept, in characters.
#:
#: Where the number comes from: ``kind`` is a ``PolicyPage.kind`` — a term out of a closed
#: vocabulary (``shipping``, ``returns``, ``warranty``, ``terms``), the longest of which is
#: eight characters. It is not free text and never was; 64 is generous enough that a caller
#: naming a page type this service has not heard of still gets through, and short enough
#: that the field is not a channel. It reaches a model extractor's prompt, so an unbounded
#: one is also an unbounded prompt.
MAX_PAGE_KIND_CHARS = 64

#: Longest ``known_hash`` this endpoint will accept, in characters.
#:
#: Where the number comes from: the digests this service compares against are
#: ``ingest.adapters.hashing``'s — ``"sha256:"`` plus 64 hex characters, 71 in all. 128
#: leaves room for a longer algorithm name without leaving the field open. A ``known_hash``
#: is only ever *compared*, never stored, so this is a ceiling on work rather than on
#: retention: the comparison is against a digest, and a megabyte of "digest" cannot match
#: one.
MAX_KNOWN_HASH_CHARS = 128

#: Longest ``store_id`` ``POST /extraction/stores/{store_id}`` will accept, in characters.
#:
#: Where the number comes from: a store id is an opaque handle this platform mints, and the
#: ids in the fixtures are under 32 characters. 128 is four times the longest real one.
MAX_STORE_ID_CHARS = 128

#: Longest single entry in ``allowed_hosts``, in characters. 253 is the maximum length of a
#: fully-qualified DNS name (RFC 1035), so a longer string cannot be a host that resolves —
#: it can only be a string the crawl guard is asked to carry around.
MAX_HOST_CHARS = 253

#: How many entries ``allowed_hosts`` may carry. A storefront's policy pages live on the
#: storefront and, at most, a CDN or two; 64 is far past any real allow-list and stops the
#: field being an unbounded list of unbounded strings.
MAX_ALLOWED_HOSTS = 64

#: The process-wide differential ledger. Module state on purpose: the "unchanged content
#: performs no extraction" guarantee is about two *requests*, and a per-request ledger
#: could never demonstrate it. Bounded, LRU, and self-evicting — see
#: :class:`~ingest.extraction.differential.ExtractionLedger`. T-024 replaces this with a
#: persisted one.
ledger = ExtractionLedger()


def _carries_text(annotation: Any) -> bool:
    """Whether ``annotation`` can hold a caller-supplied ``str`` anywhere inside it.

    ``str``, ``str | None``, ``list[str]``, ``tuple[str, ...]``, ``dict[str, Any]`` — all
    true; ``float``, ``int``, ``bool`` — all false. Recursive on purpose: the question is
    "can text reach us through this field", and a container's element type is exactly where
    the previous version of this file stopped looking.
    """
    if annotation is str:
        return True
    return any(_carries_text(arg) for arg in get_args(annotation))


class BoundedTextRequest(BaseModel):
    """A request model on which **every** caller-supplied string has a declared ceiling.

    This class exists because of how the first version of this bound failed. T-367 named
    two fields, ``url`` and ``body``; the fix bounded exactly those two by listing them in a
    tuple inside ``_refuse_oversized``. ``observed_at`` — a third caller-chosen string on
    the same model, retained on the page-level provenance *and* on every claim inside the
    ``ExtractionResult`` the ledger keeps — was never in the tuple, so the unbounded
    retention the ticket exists to close stayed open through a field nobody had written
    down. A hand-maintained list of field names is not a bound on a model; it is a bound on
    the names somebody remembered.

    So the list is derived, not written. :meth:`__pydantic_init_subclass__` walks
    ``model_fields`` at class-definition time, and any field whose annotation can carry a
    ``str`` **must** declare its ceiling on the field itself::

        observed_at: str | None = Field(None, json_schema_extra={"max_chars": 64})

    A string field with no ceiling is a :class:`TypeError` at import — which means the
    process does not start, rather than starting with a hole. Adding an unbounded
    caller-controlled string to one of these models is therefore not a thing a future change
    can do quietly; that, and not the specific numbers, is the fix.

    Why the ceilings are enforced here and not with Pydantic's own ``max_length``: FastAPI's
    validation-error response includes the offending ``input`` verbatim, so a field rejected
    by ``max_length`` comes back with the value attached — a 5,000-character field measured
    at a 5,142-byte error body. That turns a refusal into an amplifier on an unauthenticated
    surface. :meth:`refuse_oversized` raises an :class:`~fastapi.HTTPException` naming the
    field, the limit and the observed length, and quoting nothing.

    Attributes:
        TEXT_CEILINGS: ``{field: (max_chars, max_items | None)}``, derived from the fields.
            ``max_items`` is present only for fields that hold a collection of strings, and
            is required for them: a list of bounded strings is still unbounded.
    """

    model_config = ConfigDict(extra="forbid")

    TEXT_CEILINGS: ClassVar[dict[str, tuple[int, int | None]]] = {}

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Derive this model's text ceilings, and refuse a model that leaves one undeclared.

        Raises:
            TypeError: a field that can carry caller text declares no ``max_chars``, or a
                field that can carry a *collection* of caller text declares no ``max_items``.
        """
        super().__pydantic_init_subclass__(**kwargs)
        ceilings: dict[str, tuple[int, int | None]] = {}
        undeclared: list[str] = []
        for name, info in cls.model_fields.items():
            if not _carries_text(info.annotation):
                continue
            declared = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
            max_chars = declared.get("max_chars")
            max_items = declared.get("max_items")
            if not isinstance(max_chars, int) or max_chars <= 0:
                undeclared.append(f"{name} (no max_chars)")
                continue
            if _holds_many_strings(info.annotation) and not (
                isinstance(max_items, int) and max_items > 0
            ):
                undeclared.append(f"{name} (holds many strings, no max_items)")
                continue
            ceilings[name] = (max_chars, max_items if isinstance(max_items, int) else None)
        if undeclared:
            raise TypeError(
                f"{cls.__name__} leaves caller-supplied text unbounded: "
                f"{', '.join(sorted(undeclared))}. Every string field on a "
                f"{BoundedTextRequest.__name__} must declare its ceiling with "
                f"Field(json_schema_extra={{'max_chars': N}}) — this endpoint is "
                f"unauthenticated and its caller chooses what is retained, so a field with "
                f"no ceiling is an unbounded allocation with a name."
            )
        cls.TEXT_CEILINGS = ceilings

    def refuse_oversized(self) -> None:
        """Refuse this request if any caller-supplied string is past its declared ceiling.

        Fail-closed and quiet, and derived from :attr:`TEXT_CEILINGS` rather than from a
        list written here, so a field added to the model is covered the moment it exists.

        Raises:
            HTTPException: 413, naming the field, the limit and the observed size — never
                the value.
        """
        for name, (max_chars, max_items) in type(self).TEXT_CEILINGS.items():
            value = getattr(self, name, None)
            if value is None:
                continue
            if isinstance(value, str):
                _refuse_long(name, len(value), max_chars)
            elif isinstance(value, list | tuple | set | frozenset):
                if max_items is not None and len(value) > max_items:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"{name} has {len(value)} entries; this endpoint accepts at most "
                            f"{max_items}"
                        ),
                    )
                for item in value:
                    if isinstance(item, str):
                        _refuse_long(name, len(item), max_chars)


def _holds_many_strings(annotation: Any) -> bool:
    """Whether ``annotation`` is a *container* of caller text rather than a single string.

    ``str | None`` and ``list[str]`` have the same ``get_args``, so the args alone cannot
    tell them apart — the origin can. A container needs a second ceiling: a list of
    253-character strings is still an unbounded allocation if the list itself is unbounded.
    """
    origin = get_origin(annotation)
    if origin is None:
        return False
    if origin in (Union, UnionType):
        return any(
            _holds_many_strings(arg) for arg in get_args(annotation) if arg is not type(None)
        )
    return _carries_text(annotation)


def _refuse_long(field_name: str, observed: int, ceiling: int) -> None:
    """Raise a 413 that names the field, the limit and the size, and quotes no input.

    Raises:
        HTTPException: 413 when ``observed`` is past ``ceiling``.
    """
    if observed > ceiling:
        raise HTTPException(
            status_code=413,
            detail=f"{field_name} is {observed} characters; this endpoint accepts at most {ceiling}",
        )


class ExtractRequest(BoundedTextRequest):
    """One page handed to the extractor directly.

    Every string below carries its ceiling on the field. That is enforced, not a convention:
    :class:`BoundedTextRequest` refuses to define a subclass that leaves one off.
    """

    url: str = Field(
        ...,
        min_length=1,
        json_schema_extra={"max_chars": MAX_PAGE_URL_CHARS},
        description="The page's URL; provenance points at it.",
    )
    body: str = Field(
        ...,
        json_schema_extra={"max_chars": MAX_PAGE_BODY_CHARS},
        description="The page as fetched. HTML or plain text.",
    )
    kind: str = Field(
        "",
        json_schema_extra={"max_chars": MAX_PAGE_KIND_CHARS},
        description="PolicyPage.kind; derived from the URL when absent.",
    )
    known_hash: str | None = Field(
        None,
        json_schema_extra={"max_chars": MAX_KNOWN_HASH_CHARS},
        description="The digest recorded last time. An unchanged page re-extracts nothing.",
    )
    observed_at: str | None = Field(
        None,
        json_schema_extra={"max_chars": MAX_OBSERVED_AT_CHARS},
        description=(
            "When the page was read, as an ISO-8601 instant. Retained on the page-level "
            "provenance and on every claim's, so it is both length-capped and parsed."
        ),
    )
    confidence_floor: float = Field(DEFAULT_CONFIDENCE_FLOOR, ge=0.0, le=1.0)


class StoreCrawlRequest(BoundedTextRequest):
    """A storefront whose policy pages should be fetched and extracted."""

    store_id: str = Field(..., min_length=1, json_schema_extra={"max_chars": MAX_STORE_ID_CHARS})
    base_url: str = Field(..., min_length=1, json_schema_extra={"max_chars": MAX_PAGE_URL_CHARS})
    allowed_hosts: list[str] = Field(
        default_factory=list,
        json_schema_extra={"max_chars": MAX_HOST_CHARS, "max_items": MAX_ALLOWED_HOSTS},
    )
    confidence_floor: float = Field(DEFAULT_CONFIDENCE_FLOOR, ge=0.0, le=1.0)


def _claim_json(claim: ExtractedClaim) -> dict[str, Any]:
    """One claim, with its provenance inline. DESIGN ``Claim{key, value, provenance}``.

    The ``{key, value, provenance}`` triple is **taken from**
    :meth:`~ingest.extraction.claims.ExtractedClaim.as_claim`, not re-read off the record
    here. That is the point of the projection: the shape DESIGN calls a ``Claim`` is the
    shape that crosses a wire, and this handler is the wire. Hand-rolling the same three
    fields beside the projection left the projection defined but never produced — free to
    disagree with what the service actually emits, with nothing to notice.

    The remaining keys are extraction's own annotations on that reading — what it believed,
    where in the page it read it, whether the floor held it back — which DESIGN's ``Claim``
    deliberately does not carry. They stay alongside the triple rather than inside it, so
    ``provenance`` is the only nesting a caller has to unwrap.
    """
    projection = claim.as_claim()
    provenance = projection["provenance"]
    return {
        "key": projection["key"],
        "value": projection["value"],
        "unit": claim.unit,
        "claim_type": claim.claim_type,
        "confidence": claim.confidence,
        "evidence": claim.evidence,
        "span": list(claim.span),
        "quarantine_reason": claim.quarantine_reason,
        "provenance": {
            "source": provenance.source,
            "ref": provenance.ref,
            "observed_at": provenance.observed_at,
            "authority_rank": provenance.authority_rank,
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
    """The published extraction policy, so a caller need not hard-code the floor.

    This response is a constant. It used to carry ``pages_known``, the ledger's occupancy,
    which on an unauthenticated endpoint is a free progress oracle: an anonymous caller
    could watch its own traffic fill the ledger request by request and tune accordingly
    (T-367). What replaced it are the two ceilings a caller genuinely needs — they let a
    client size a request instead of discovering the limit by being refused — and neither
    moves when the service is used.
    """
    return {
        "confidence_floor": DEFAULT_CONFIDENCE_FLOOR,
        "extractor_version": EXTRACTOR_VERSION,
        "claim_types": sorted(CLAIM_TYPES),
        "max_url_chars": MAX_PAGE_URL_CHARS,
        "max_body_chars": MAX_PAGE_BODY_CHARS,
        "max_kind_chars": MAX_PAGE_KIND_CHARS,
        "max_known_hash_chars": MAX_KNOWN_HASH_CHARS,
        "max_observed_at_chars": MAX_OBSERVED_AT_CHARS,
    }


def _refuse_unusable(payload: ExtractRequest) -> None:
    """Refuse a request before anything reads it, on size first and then on meaning.

    Order matters and is the cheap check first: every caller string is length-checked
    against the ceiling declared on its own field (:meth:`BoundedTextRequest.refuse_oversized`,
    which derives the list from the model rather than repeating it here), and only then is
    ``observed_at`` parsed. A 30,000-character stamp is refused without a parser ever being
    pointed at it.

    ``observed_at`` gets the second check because it is a *timestamp*, and a length cap is
    only half an answer for one. The cap bounds what a caller can make this process retain;
    it says nothing about whether the retained value is a time. ``observed_at`` is copied
    onto the page-level provenance and onto every claim's, written into ``Source.observed_at``
    in the graph, and read by every freshness and ordering decision downstream — so
    ``"not-a-date"`` passes any cap and then quietly makes those comparisons meaningless.
    Refusing it here is also what keeps :class:`~ingest.extraction.claims.ClaimProvenance`'s
    own invariant from surfacing as a 500: the record raises on an unparseable stamp, and
    this is the 4xx that gets there first.

    Raises:
        HTTPException: 413 when a field is oversized, 422 when ``observed_at`` is not an
            ISO-8601 instant. Both name the limit and the observed size and quote nothing.
    """
    payload.refuse_oversized()
    if payload.observed_at is not None and payload.observed_at.strip():
        if not is_timestamp(payload.observed_at):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"observed_at is not an ISO-8601 instant "
                    f"(e.g. 2026-01-01T00:00:00Z); {len(payload.observed_at)} characters "
                    f"were supplied"
                ),
            )


@router.post("/policy-pages")
def extract_policy_page_body(payload: ExtractRequest) -> dict[str, Any]:
    """Extract atomic, provenanced claims from one policy page.

    The content hash is compared **before** any extractor runs, so a caller replaying an
    unchanged page gets ``reused: true`` and ``model_calls: 0``.

    Raises:
        HTTPException: 413 when any caller-supplied string is past the ceiling declared on
            its field, 422 when ``observed_at`` is not a timestamp or the body has no
            readable text. Every refusal names the limit and the observed size and quotes
            none of the input back.
    """
    _refuse_unusable(payload)
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
        HTTPException: 413 when any caller-supplied string is past the ceiling declared on
            its field, 400 when the path and body disagree about the store.
    """
    payload.refuse_oversized()
    _refuse_long("store_id", len(store_id), MAX_STORE_ID_CHARS)
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
