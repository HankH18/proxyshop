"""Turning a fetched policy page into the text an extractor reads (T-021).

Two jobs, and the second one is a security control rather than a convenience.

**Plain text.** Claims are read out of prose, and a policy page arrives as HTML with
navigation, scripts and styles around it. ``page_text`` strips those, keeps the block
structure that :func:`ingest.extraction.rules.sentences` splits on, and — critically —
drops HTML comments. A commented-out ``<p>Free shipping on everything, always.</p>`` is
not something the store publishes, and extracting it would put a claim in the graph that
no buyer could ever have seen.

**Untrusted content.** SPEC C10: *"scraped text and all pitch content are untrusted data,
never instructions"*. :func:`injection_markers` finds the sentences that are trying to be
instructions. It does not sanitise them away — a page that tries to reprogram the
extractor is itself a fact worth recording — it labels them, so the pipeline can warn, the
prompt can fence the text explicitly, and a claim read out of an instruction-shaped
sentence can be refused.
"""

from __future__ import annotations

import html as _html
import re
import unicodedata

__all__ = [
    "INJECTION_PATTERNS",
    "POLICY_PAGE_KINDS",
    "injection_markers",
    "page_kind_for",
    "page_text",
]

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_DROPPED_ELEMENTS_RE = re.compile(
    r"<(head|script|style|template|noscript|svg|nav|header|footer)\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_BLOCK_END_RE = re.compile(
    r"</\s*(p|div|li|tr|h[1-6]|section|article|main|ul|ol|table|blockquote)\s*>"
    r"|<\s*br\s*/?>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{2,}")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+(?=\n)")
_SPACE_RUN_RE = re.compile(r"[ \t\r\f\v ]+")

#: Instruction-shaped phrasing. Deliberately about *imperatives aimed at a model*, not
#: about rude words: a policy page saying "ignore wear and tear" is fine, one saying
#: "ignore all previous instructions" is not.
INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+\w*\s*instructions?\b"
    ),
    re.compile(
        r"\bdisregard\s+(the\s+|all\s+|any\s+)?(previous|prior|above|schema|rules?|instructions?)\b"
    ),
    re.compile(r"\byou\s+are\s+now\s+(an?|the)\b"),
    re.compile(r"\b(new|updated)\s+instructions?\s*:"),
    re.compile(r"\bsystem\s+prompt\b"),
    re.compile(r"\b(output|reveal|print|repeat)\s+(your|the)\s+(system|instructions?|prompt)\b"),
    re.compile(r"\bcall\s+the\s+tool\b"),
    re.compile(r"\bmark\s+every\s+claim\b"),
    re.compile(r"\bconfidence\s+(of\s+)?1(\.0+)?\b"),
    re.compile(r"\bact\s+as\s+(an?|the)\b"),
    re.compile(r"\bunrestricted\s+(assistant|mode|model)\b"),
)

#: The policy pages worth fetching, and the ``PolicyPage.kind`` each lands under.
POLICY_PAGE_KINDS: dict[str, tuple[str, ...]] = {
    "shipping": ("shipping", "delivery", "shipping-policy", "shipping-and-returns"),
    "returns": ("return", "returns", "refund", "refund-policy", "return-policy", "exchanges"),
    "warranty": ("warranty", "guarantee", "warranties"),
    "terms": ("terms", "terms-of-service", "tos"),
    "privacy": ("privacy", "privacy-policy"),
}


def page_text(body: str) -> str:
    """The readable text of ``body``, whether it is HTML or already plain.

    Args:
        body: the fetched page, as text.

    Returns:
        Block-separated plain text: one line per block element, entity-escapes resolved,
        runs of whitespace collapsed. Comments, scripts, styles and chrome are gone.
    """
    raw = str(body or "")
    if "<" not in raw:
        return _tidy(raw)
    stripped = _COMMENT_RE.sub(" ", raw)
    stripped = _DROPPED_ELEMENTS_RE.sub(" ", stripped)
    stripped = _BLOCK_END_RE.sub("\n", stripped)
    stripped = _TAG_RE.sub(" ", stripped)
    return _tidy(_html.unescape(stripped))


def _tidy(text: str) -> str:
    """Normalise whitespace without losing the line structure sentences split on."""
    normalised = unicodedata.normalize("NFKC", text)
    normalised = _SPACE_RUN_RE.sub(" ", normalised)
    normalised = _TRAILING_SPACE_RE.sub("", normalised)
    normalised = _BLANK_RUN_RE.sub("\n", normalised)
    return "\n".join(line.strip() for line in normalised.split("\n") if line.strip())


def injection_markers(text: str) -> tuple[str, ...]:
    """Every distinct instruction-shaped phrase found in ``text``.

    Args:
        text: page text (or any untrusted string).

    Returns:
        The matched phrases, lower-cased, in first-seen order. Empty when the page reads
        like a policy page.
    """
    lowered = str(text or "").lower()
    found: list[str] = []
    for pattern in INJECTION_PATTERNS:
        for match in pattern.finditer(lowered):
            phrase = " ".join(match.group(0).split())
            if phrase not in found:
                found.append(phrase)
    return tuple(found)


def page_kind_for(url: str, *, default: str = "policy") -> str:
    """Classify a policy-page URL into a ``PolicyPage.kind``.

    Args:
        url: the page URL or path.
        default: the kind to use when nothing matches.

    Returns:
        One of :data:`POLICY_PAGE_KINDS`' keys, or ``default``.
    """
    lowered = str(url or "").lower()
    for kind, needles in POLICY_PAGE_KINDS.items():
        for needle in needles:
            if needle in lowered:
                return kind
    return default
