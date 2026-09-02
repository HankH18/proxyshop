"""Content-hash-driven re-extraction: unchanged pages cost nothing (T-021, C6).

The ticket's third acceptance criterion is a claim about a *count*: "re-run on unchanged
pages performs no LLM calls (double counts)". That is only checkable if the decision to
extract is taken before the extractor is reached, and if the extractor keeps a counter. So
the decision lives here, in front of everything, and it is a pure hash comparison:
:func:`needs_extraction` opens no socket, calls no model, and reads no clock.

The hash function is T-020's :func:`ingest.adapters.hashing.content_hash` — re-exported,
not reimplemented. Two reasons that matters. The digest carries its ``"sha256:"`` prefix,
so a stored hash names its own algorithm and a future migration fails loudly instead of
silently reporting "unchanged". And the fetcher already stamps every ``FetchedResource``
and ``HTTPResult`` with exactly this digest, so a hash the crawler recorded and a hash the
extractor computes are comparable without anyone converting between two conventions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..adapters.hashing import content_hash, has_changed, snapshot_ref
from .claims import ExtractionResult

__all__ = [
    "ExtractionLedger",
    "content_hash",
    "has_changed",
    "needs_extraction",
    "snapshot_ref",
]


def needs_extraction(previous_hash: str | None, content: str | bytes) -> bool:
    """Whether ``content`` must be (re-)extracted, given what was last seen.

    Args:
        previous_hash: the digest recorded the last time this page was extracted, or
            ``None``/empty when it has never been seen.
        content: the page as fetched now.

    Returns:
        ``False`` only when the content hashes to exactly what was recorded before —
        which is the "zero re-extraction work" case. ``True`` for a page never seen: with
        nothing to compare against, the only safe answer is to do the work.
    """
    return has_changed(previous_hash, content_hash(content))


@dataclass
class ExtractionLedger:
    """What has already been extracted, keyed by page reference.

    A scheduler (T-024) will persist this; in-process it is a dict. Either way the
    contract is the same three questions: what hash did we last see for this page, does
    the current content differ, and what did the last run produce.

    Attributes:
        hashes: ``{page_ref: content_hash}`` for every page recorded.
        results: the last :class:`~ingest.extraction.claims.ExtractionResult` per page.
            Held so an unchanged page can be answered from cache rather than re-extracted
            — the cache hit *is* the zero-work guarantee, and returning the old claims is
            what makes it usable rather than merely cheap.
    """

    hashes: dict[str, str] = field(default_factory=dict)
    results: dict[str, ExtractionResult] = field(default_factory=dict)

    def known_hash(self, page_ref: str) -> str | None:
        """The digest last recorded for ``page_ref``, or ``None``."""
        return self.hashes.get(str(page_ref))

    def needs_extraction(self, page_ref: str, content: str | bytes) -> bool:
        """Whether ``content`` differs from what was last recorded for ``page_ref``."""
        return needs_extraction(self.known_hash(page_ref), content)

    def cached(self, page_ref: str, digest: str) -> ExtractionResult | None:
        """The recorded result for ``page_ref``, but only if ``digest`` still matches.

        Guarding on the digest is what stops a stale cache from answering for changed
        content: the result is keyed by the page, and validated by the hash.
        """
        if has_changed(self.known_hash(page_ref), digest):
            return None
        return self.results.get(str(page_ref))

    def record(self, page_ref: str, digest: str, result: ExtractionResult | None = None) -> None:
        """Record that ``page_ref`` was extracted at ``digest``."""
        self.hashes[str(page_ref)] = str(digest)
        if result is not None:
            self.results[str(page_ref)] = result

    def forget(self, page_ref: str) -> None:
        """Drop everything recorded for ``page_ref``, forcing the next run to extract."""
        self.hashes.pop(str(page_ref), None)
        self.results.pop(str(page_ref), None)

    def hash_index(self) -> dict[str, str]:
        """The recorded hashes, shaped for a ``CatalogRequest.known_hashes``."""
        return dict(self.hashes)
