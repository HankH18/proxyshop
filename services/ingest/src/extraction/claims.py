"""The claim record, its provenance, and the batch an extraction run produces (T-021).

DESIGN pins two shapes that are the *same fact* seen from two sides:

* ``Claim{key, value, provenance: Provenance}`` — what crosses a wire or a function
  boundary, with ``Provenance{source, ref, observed_at, authority_rank}``.
* ``Source{source_id, url, content_hash, observed_at, extractor_version, confidence,
  source_class}`` — the graph node every material fact hangs a ``SUPPORTED_BY`` edge on
  (DESIGN: "Source ≡ our Provenance"; ``source_class`` carries the provenance enum).

:class:`ClaimProvenance` is the bridge. It carries the four fields the claim side names
*and* the three extra fields the node side requires, so :meth:`ClaimProvenance.as_source`
can always build a valid :class:`ingest.graph.model.Source` — which matters because
``ingest.graph.upsert`` takes ``source`` as a **required** keyword-only argument and
refuses blank fields. Provenance is therefore not something a later ticket bolts on: an
:class:`ExtractedClaim` cannot be constructed without it.

Why a per-claim ``Source`` rather than one per page: ``Source.confidence`` is the only
place the graph records how much the extractor believed a reading, and C10's quarantine
rule ("low-confidence extraction is quarantined, not upserted") is only auditable after
the fact if the confidence that made the decision is written next to the fact it admitted.
One Source per page would have to pick a single number for five different readings.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlunsplit

from ..adapters.hashing import HASH_PREFIX, content_hash, snapshot_ref
from ..adapters.netguard import safe_split
from ..graph.model import SOURCE_CLASSES, AttributeValue, Source

__all__ = [
    "DEFAULT_AUTHORITY_RANK",
    "DEFAULT_CONFIDENCE_FLOOR",
    "EXTRACTOR_VERSION",
    "QUARANTINE_BELOW_FLOOR",
    "QUARANTINE_SCHEMA",
    "QUARANTINE_UNTRUSTED",
    "ClaimProvenance",
    "ExtractedClaim",
    "ExtractionResult",
    "RawClaim",
    "normalise_provenance",
    "partition_by_confidence",
]

#: Stamped onto every ``Source`` this module writes. Bump it when the extraction rules
#: change in a way that would produce different claims from identical bytes — a stored
#: claim then names the code that produced it and a re-extraction is explainable.
EXTRACTOR_VERSION = "policy_extraction@1.0.0"

#: C10's confidence floor. Anything strictly below it is quarantined rather than upserted.
#: Deliberately a module constant, not a magic literal at the call site: the number is a
#: published policy, and a caller that wants a different one passes it explicitly.
DEFAULT_CONFIDENCE_FLOOR = 0.6

#: DESIGN ``Provenance.authority_rank``. A scraped policy page is the store's own
#: published statement — authoritative for what the store *says*, which is what a claim is.
DEFAULT_AUTHORITY_RANK = 1

QUARANTINE_BELOW_FLOOR = "below_confidence_floor"
QUARANTINE_SCHEMA = "schema_violation"
QUARANTINE_UNTRUSTED = "untrusted_instruction_in_source"

_SNAPSHOT_RE = re.compile(r"^snapshot://(?P<authority>[^/@]+)(?P<path>[^@]*)@(?P<digest>.+)$")


def _now() -> str:
    """UTC now, in the same second-resolution shape the fetch adapter stamps."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stable_id(*parts: str) -> str:
    """A short, deterministic id from its parts. Same construction as T-020's adapter."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


def _read(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off a mapping or an object, tolerating either container.

    Callers hand us ``packages.contracts.Provenance``, a graph ``Source``, or a plain
    dict, depending on which tickets have landed. The field is the contract; the box is
    not.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    if value is None:
        return default
    return getattr(value, "value", value)  # unwrap enum-ish values


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


@dataclass(frozen=True)
class ClaimProvenance:
    """DESIGN ``Provenance`` plus the fields a graph ``Source`` node needs to be traceable.

    Attributes:
        source: the provenance enum (``"scraped"`` for a fetched policy page). Named
            ``source`` because that is what DESIGN's ``Provenance`` calls it; the graph
            node calls the same value ``source_class``.
        ref: the snapshot ref — ``snapshot://host/path@sha256:…`` — naming *which version*
            of the page was read.
        observed_at: when it was read.
        authority_rank: DESIGN's tie-break rank between provenance classes.
        source_id: stable id of the ``Source`` node.
        url: the page the claim came from.
        content_hash: the digest the snapshot ref pins.
        extractor_version: the code that produced the reading.
        confidence: how much the extractor believed it.
    """

    source: str
    ref: str
    observed_at: str
    authority_rank: int = DEFAULT_AUTHORITY_RANK
    source_id: str = ""
    url: str = ""
    content_hash: str = ""
    extractor_version: str = EXTRACTOR_VERSION
    confidence: float = 1.0

    def __post_init__(self) -> None:
        """Fill the derivable fields and refuse a provenance record that points at nothing.

        Raises:
            ValueError: ``source`` is outside the DESIGN vocabulary, ``ref`` or
                ``observed_at`` is blank, or ``confidence`` is outside ``[0, 1]``.
        """
        if self.source not in SOURCE_CLASSES:
            raise ValueError(
                f"ClaimProvenance.source {self.source!r} is not one of "
                f"{sorted(SOURCE_CLASSES)} (DESIGN Provenance.source)"
            )
        for name in ("ref", "observed_at"):
            if not _text(getattr(self, name)):
                raise ValueError(
                    f"ClaimProvenance.{name} must be non-empty: a claim whose provenance "
                    f"names no snapshot is indistinguishable from an unprovenanced claim"
                )
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(
                f"ClaimProvenance.confidence must be in [0, 1], got {self.confidence!r}"
            )

        url, digest = _parts_from_ref(self.ref)
        if not _text(self.url):
            object.__setattr__(self, "url", url or self.ref)
        if not _text(self.content_hash):
            object.__setattr__(self, "content_hash", digest or content_hash(self.ref))
        if not _text(self.extractor_version):
            object.__setattr__(self, "extractor_version", EXTRACTOR_VERSION)
        if not _text(self.source_id):
            object.__setattr__(self, "source_id", f"src_{_stable_id(self.url, self.content_hash)}")

    def for_claim(self, key: str, *, confidence: float) -> ClaimProvenance:
        """This provenance, specialised to one claim's key and extraction confidence.

        The ``source_id`` folds the key in so two readings off the same snapshot get two
        ``Source`` nodes and each keeps its own confidence.
        """
        return replace(
            self,
            confidence=float(confidence),
            source_id=f"src_{_stable_id(self.url, self.content_hash, key)}",
        )

    def as_source(self) -> Source:
        """The ``Source`` node a ``SUPPORTED_BY`` edge points at."""
        return Source(
            source_id=self.source_id,
            url=self.url,
            content_hash=self.content_hash,
            observed_at=self.observed_at,
            extractor_version=self.extractor_version,
            confidence=float(self.confidence),
            source_class=self.source,
        )


def _parts_from_ref(ref: str) -> tuple[str, str]:
    """Recover ``(url, digest)`` from a snapshot ref, or ``("", "")`` if it is not one.

    ``snapshot_ref`` drops the scheme (a snapshot of a page is the same snapshot however
    it was fetched), so the reconstruction assumes https. That only ever feeds the
    ``Source.url`` property; the ref itself stays the authoritative pointer.
    """
    match = _SNAPSHOT_RE.match(_text(ref))
    if match is None:
        return ("", "")
    path = match.group("path") or "/"
    # `_SNAPSHOT_RE` happily matches an authority containing `[`, and `urlsplit` raises on it.
    # A ref is provenance: failing to read one back must not raise out of whatever is holding
    # it. Latent today (no route feeds a caller-supplied ref here), which is when it is cheap.
    split = safe_split(f"//{match.group('authority')}{path}")
    if split is None:
        return ("", "")
    query = split.query
    url = urlunsplit(("https", split.netloc, split.path or "/", query, ""))
    digest = _text(match.group("digest"))
    return (url, digest)


def normalise_provenance(
    descriptor: Any,
    *,
    text: str = "",
    url: str = "",
    observed_at: str | None = None,
    default_source: str = "scraped",
) -> ClaimProvenance:
    """Coerce whatever a caller passed as "the source" into a :class:`ClaimProvenance`.

    Accepts a :class:`ClaimProvenance`, a ``packages.contracts.Provenance``, a graph
    :class:`~ingest.graph.model.Source`, a plain mapping, or ``None``. Anything the
    descriptor does not name is derived from the page itself, so the result is always
    complete enough to write a ``Source`` node.

    Args:
        descriptor: the caller's source record, in any of the shapes above.
        text: the page text, used to derive a content hash when the descriptor has none.
        url: the page URL, used when the descriptor has none.
        observed_at: observation timestamp to fall back to; defaults to now.
        default_source: provenance class to assume when the descriptor does not say.

    Returns:
        A complete :class:`ClaimProvenance`.

    Raises:
        ValueError: the descriptor names a provenance class outside DESIGN's vocabulary.
    """
    if isinstance(descriptor, ClaimProvenance):
        return descriptor

    source = _text(_read(descriptor, "source")) or _text(_read(descriptor, "source_class"))
    source = source or default_source
    resolved_url = _text(_read(descriptor, "url")) or _text(url)
    digest = _text(_read(descriptor, "content_hash"))
    ref = _text(_read(descriptor, "ref")) or _text(_read(descriptor, "snapshot_ref"))
    stamp = _text(_read(descriptor, "observed_at")) or _text(observed_at) or _now()

    if not digest:
        # Derive from the ref when it carries one, else hash the text we were handed.
        _, from_ref = _parts_from_ref(ref)
        digest = from_ref or (content_hash(text) if text else "")
    if digest and not digest.startswith(HASH_PREFIX) and re.fullmatch(r"[0-9a-f]{64}", digest):
        digest = HASH_PREFIX + digest
    if not ref:
        if not resolved_url:
            raise ValueError(
                "cannot build a snapshot ref: the source descriptor names neither a `ref` "
                "nor a `url`, so the claim would have no traceable provenance"
            )
        ref = snapshot_ref(resolved_url, digest or content_hash(text))

    rank = _read(descriptor, "authority_rank", DEFAULT_AUTHORITY_RANK)
    extractor_version = _text(_read(descriptor, "extractor_version")) or EXTRACTOR_VERSION

    return ClaimProvenance(
        source=source,
        ref=ref,
        observed_at=stamp,
        authority_rank=int(rank),
        source_id=_text(_read(descriptor, "source_id")),
        url=resolved_url,
        content_hash=digest,
        extractor_version=extractor_version,
        confidence=float(_read(descriptor, "confidence", 1.0)),
    )


@dataclass(frozen=True)
class RawClaim:
    """One atomic reading, before provenance is bound to it.

    This is the extractors' output type. Keeping it separate from
    :class:`ExtractedClaim` is what makes "every claim carries provenance" structural: an
    extractor *cannot* emit a finished claim, because it is not given the snapshot it
    would need to provenance one.
    """

    key: str
    value: Any
    claim_type: str
    confidence: float
    span: tuple[int, int] = (0, 0)
    evidence: str = ""
    unit: str | None = None

    def __post_init__(self) -> None:
        """Validate the fields every downstream consumer indexes on.

        Raises:
            ValueError: the key or claim type is blank, or the confidence is outside [0, 1].
        """
        if not _text(self.key):
            raise ValueError("RawClaim.key must be non-empty: an unkeyed claim cannot be upserted")
        if not _text(self.claim_type):
            raise ValueError(
                f"RawClaim.claim_type must be non-empty for {self.key!r}: the claim_type is "
                f"what routes a verification outcome to a trust dimension (DESIGN)"
            )
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(
                f"RawClaim.confidence must be in [0, 1], got {self.confidence!r} for {self.key!r}"
            )


@dataclass(frozen=True)
class ExtractedClaim:
    """DESIGN ``Claim{key, value, provenance}`` plus what extraction learned about it.

    Attributes:
        key: the claim key, e.g. ``"shipping.dispatch_window_days"``.
        value: the reading.
        claim_type: a term from DESIGN's published ``claim_type`` vocabulary, which is
            what routes a later verification outcome to a trust dimension.
        confidence: the extractor's belief in ``[0, 1]``. This is what C10's floor
            compares against.
        provenance: where the reading came from. Never optional.
        span: character offsets of the evidence inside the page text.
        evidence: the sentence the reading was taken from.
        unit: the unit ``value`` is expressed in, when it has one.
        quarantine_reason: empty for an upsertable claim; set when the claim was held back.
    """

    key: str
    value: Any
    claim_type: str
    confidence: float
    provenance: ClaimProvenance
    span: tuple[int, int] = (0, 0)
    evidence: str = ""
    unit: str | None = None
    quarantine_reason: str = ""

    @property
    def supported_by(self) -> Source:
        """The ``Source`` node this claim's ``SUPPORTED_BY`` edge points at."""
        return self.provenance.as_source()

    @property
    def snapshot_ref(self) -> str:
        """The snapshot this claim was read out of."""
        return self.provenance.ref

    def quarantine(self, reason: str) -> ExtractedClaim:
        """A copy of this claim marked as held back, with the reason recorded."""
        return replace(self, quarantine_reason=reason)

    def as_attribute(self) -> AttributeValue:
        """The ``AttributeValue`` node a policy page ``STATES``.

        The value is written into whichever typed slot matches it, which is what lets the
        graph's candidate query compare a number as a number rather than as text.
        """
        value = self.value
        if isinstance(value, bool):
            return AttributeValue(key=self.key, value_bool=value, unit=self.unit)
        if isinstance(value, int | float):
            return AttributeValue(key=self.key, value_number=float(value), unit=self.unit)
        return AttributeValue(key=self.key, value_string=str(value), unit=self.unit)

    def as_claim(self) -> dict[str, Any]:
        """The DESIGN ``Claim`` projection: ``{key, value, provenance}`` and nothing else."""
        return {"key": self.key, "value": self.value, "provenance": self.provenance}


@dataclass(frozen=True)
class ExtractionResult:
    """One extraction run, partitioned into what may be upserted and what may not.

    Attributes:
        claims: the upsert batch — every claim at or above the confidence floor.
        quarantined: everything held back, with a ``quarantine_reason`` on each. Held
            back, never dropped: a discarded low-confidence claim is invisible, and C10
            wants it inspectable.
        content_hash: the digest of the text this run read.
        provenance: the page-level provenance every claim specialises.
        confidence_floor: the floor this run applied.
        extractor: the name of the extractor that produced the readings.
        model_calls: how many LLM calls the run made. Zero for a cache hit — which is the
            observable form of "re-run on unchanged pages performs no LLM calls".
        reused: True when the run short-circuited on an unchanged content hash.
        warnings: anything noteworthy, e.g. untrusted-instruction text in the page.
    """

    claims: tuple[ExtractedClaim, ...] = ()
    quarantined: tuple[ExtractedClaim, ...] = ()
    content_hash: str = ""
    provenance: ClaimProvenance | None = None
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR
    extractor: str = ""
    extractor_version: str = EXTRACTOR_VERSION
    model_calls: int = 0
    reused: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def all_claims(self) -> tuple[ExtractedClaim, ...]:
        """Everything the run extracted, on both sides of the floor."""
        return self.claims + self.quarantined

    @property
    def sources(self) -> tuple[Source, ...]:
        """The ``Source`` nodes the upsert batch needs, deduplicated, in claim order."""
        seen: dict[str, Source] = {}
        for claim in self.claims:
            source = claim.supported_by
            seen.setdefault(source.source_id, source)
        return tuple(seen.values())

    def reused_as(self, *, floor: float) -> ExtractionResult:
        """This result, re-partitioned at ``floor`` and marked as a cache hit.

        A cached run still has to answer a caller that asked for a different floor, and
        re-partitioning costs no model call — which is the whole point.
        """
        claims, quarantined = partition_by_confidence(self.all_claims, floor)
        return replace(
            self,
            claims=claims,
            quarantined=quarantined,
            confidence_floor=float(floor),
            model_calls=0,
            reused=True,
        )


def partition_by_confidence(
    claims: tuple[ExtractedClaim, ...], floor: float
) -> tuple[tuple[ExtractedClaim, ...], tuple[ExtractedClaim, ...]]:
    """Split ``claims`` into (upsertable, quarantined) at ``floor``.

    A claim already carrying a non-floor quarantine reason (a schema violation, say)
    stays quarantined whatever the floor is: the floor decides confidence, not validity.

    Args:
        claims: every claim the extractor produced.
        floor: the confidence floor, inclusive — ``confidence >= floor`` is upsertable.

    Returns:
        ``(claims, quarantined)``. Their concatenation is a permutation of the input:
        extraction loses no claim.
    """
    keep: list[ExtractedClaim] = []
    held: list[ExtractedClaim] = []
    for claim in claims:
        if claim.quarantine_reason and claim.quarantine_reason != QUARANTINE_BELOW_FLOOR:
            held.append(claim)
        elif float(claim.confidence) >= float(floor):
            keep.append(replace(claim, quarantine_reason=""))
        else:
            held.append(claim.quarantine(QUARANTINE_BELOW_FLOOR))
    return (tuple(keep), tuple(held))
