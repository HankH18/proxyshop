"""The seller's pitch against the seller's OWN live product page — a cross-surface check.

D55, and it is the *sponsored* side that is checked here, because that is the side carrying
the seller's motive. :func:`claim_verification.verify` grades a pitch against a catalogue
snapshot the exchange holds. This module grades the same pitch against the store's own live
product page, and the two are different evidence about different failures.

**Be clear about what this buys, because a docstring that oversells it is worse than the
check being absent.** A store's own product page is STILL THE STORE'S OWN WORD. This check
catches exactly two things:

*Drift.* The exchange's snapshot is old. The price moved, the item sold out, the variant was
delisted. The pitch was graded ``verified`` against a document that stopped being true, and
the store's live page says so. Drift is most of what actually goes wrong.

*Cross-surface contradiction.* The purchased message says one thing and the seller's own
public page says another — the same seller, two surfaces, two answers. That is not proof of
which one is false, and this module never claims it is; it is proof that ONE of them is, and
that is evidence about the seller.

**It does not catch a store lying consistently everywhere.** A store whose catalogue feed,
whose pitch and whose product page all say "24 month warranty" on a product with a 12 month
warranty passes this check completely, and would pass a hundred more of it. Truly independent
evidence about a seller is the transaction record — what shipped, what was charged, what came
back — which is what the promise ledger holds and what
``contracts.TrustDimension.catalog_claim_accuracy`` is graded from over time. This module is
cheap and catches the common case; it is not a substitute for that and must never be
described as one.

Absence is not guilt (the rule this module is shaped around)
------------------------------------------------------------
A page that 404s, times out, blocks the crawler, redirects away, or simply carries no
structured data yields **no verdict** — never a contradiction. A store with a slow server, a
theme with no JSON-LD, or a robots.txt that says no is not a store that lied, and a check that
conflated the two would price server quality as honesty. Every "we could not tell" path in
this module lands on :data:`NO_VERDICT` with the reason recorded, and only a page that states
something DIFFERENT produces :data:`CONTRADICTED`.

That rule is why the price comparison below is asymmetric, and the asymmetry is not a
softening — it is the same rule applied to the one direction that is genuinely undecidable.
See :func:`price_disagreement`.

Purity
------
No clock, no randomness, no I/O, no model, no network. :func:`read_product_page` takes bytes
that somebody else fetched, and :func:`check_pitch_against_page` takes that reading. Fetching
lives in the caller (``buyer_svc.livecheck``) precisely so this half stays a pure function of
its inputs and can be driven from a recorded corpus with no transport at all.

Untrusted input (C10)
---------------------
Page bytes are a seller's, and are treated exactly as ``ingest.extraction`` treats a policy
page: parsed, bounded, coerced, and never interpolated into a query, a prompt or a URL. The
parser is the standard library's :mod:`html.parser` rather than BeautifulSoup — not a taste
decision: ``apps/buyer/Dockerfile``'s pip layer installs neither ``beautifulsoup4`` nor
``lxml``, so a parser dependency here would import in a checkout and be absent in the shipped
image, which is the defect class ``proxyshop_support/tests/test_artifact_copyset.py`` exists
to catch. Every bound below (:data:`MAX_PAGE_BYTES`, :data:`MAX_LD_JSON_BLOCKS`,
:data:`MAX_JSON_NODES`) is a bound on somebody else's document.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from .comparators import FIELD_TOLERANCES, compare, tolerance_for
from .normalize import normalize_text

__all__ = [
    "AGREES",
    "CONTRADICTED",
    "IN_STOCK_TOKENS",
    "LIVE_PAGE_SURFACE",
    "MAX_JSON_NODES",
    "MAX_LD_JSON_BLOCKS",
    "MAX_PAGE_BYTES",
    "NO_VERDICT",
    "OUT_OF_STOCK_TOKENS",
    "SURFACE_JSONLD",
    "SURFACE_MICRODATA",
    "SURFACE_OPENGRAPH",
    "SURFACE_PRECEDENCE",
    "LivePageCheck",
    "LiveReading",
    "PageReading",
    "availability_disagreement",
    "check_pitch_against_page",
    "page_vocabulary",
    "price_disagreement",
    "read_product_page",
    "unreadable_page",
]

# ==============================================================================================
# Vocabulary
# ==============================================================================================

#: The check agreed: every comparison this page could decide came back the same as the pitch.
AGREES = "agrees"

#: The page states something DIFFERENT. The only outcome that costs the store anything.
CONTRADICTED = "contradicted"

#: Nothing was decidable — unreachable, unparseable, or no structured data. Never a cost.
NO_VERDICT = "no_verdict"

#: Stamped on every reading this module produces, so a consumer reading a stored verdict can
#: tell WHICH surface decided it. A verdict from the seller's own page is a weaker fact than
#: one from the exchange's snapshot and a much weaker one than a transaction record, and a
#: record that did not name its surface would let all three be read as the same evidence.
LIVE_PAGE_SURFACE = "seller_live_page"

SURFACE_JSONLD = "schema.org/ld+json"
SURFACE_MICRODATA = "schema.org/microdata"
SURFACE_OPENGRAPH = "opengraph"

#: Most authoritative first. JSON-LD is the machine-readable block a storefront emits for
#: search engines and is what ``ingest.adapters.signed_fetch`` already reads; microdata is
#: theme markup; OpenGraph is a social-preview tag that is frequently a rounded or
#: currency-free number. A key present on two surfaces takes the earlier one, and the surface
#: that won is recorded, because "the price came from an og: tag" is a caveat a reader of the
#: verdict needs.
SURFACE_PRECEDENCE: tuple[str, ...] = (SURFACE_JSONLD, SURFACE_MICRODATA, SURFACE_OPENGRAPH)

#: The most page bytes this module will parse. A product page is tens of kilobytes; this is
#: two orders of magnitude above that and is a guard against a seller answering a fetch with a
#: gigabyte, not against a verbose theme. Over it, the page is UNREADABLE and yields no
#: verdict — the same answer as a 404, and deliberately not a contradiction.
MAX_PAGE_BYTES = 512 * 1024

#: The most ``application/ld+json`` blocks that are parsed. A theme emits one or two
#: (``Product``, ``BreadcrumbList``); a page emitting hundreds is not one this check needs to
#: read to the end.
MAX_LD_JSON_BLOCKS = 24

#: The most JSON nodes walked while looking for a ``Product``. A bound on a document a seller
#: wrote: without it, a deeply nested or hugely wide ``@graph`` is unbounded work on a path
#: that must never be able to cost the platform more than the page cost the seller.
MAX_JSON_NODES = 20_000

#: schema.org availability tokens that mean the thing can be bought right now.
#: ``LimitedAvailability`` is here on purpose — "only a few left" is in stock.
IN_STOCK_TOKENS: frozenset[str] = frozenset(
    {"instock", "instoreonly", "onlineonly", "limitedavailability"}
)

#: Tokens that mean it cannot. ``Discontinued`` and ``SoldOut`` are the ones that matter for
#: drift: an auction serving an offer for a discontinued product is the failure this catches.
OUT_OF_STOCK_TOKENS: frozenset[str] = frozenset({"outofstock", "soldout", "discontinued"})

#: Deliberately in NEITHER set: ``PreOrder``, ``PreSale``, ``BackOrder``, ``MadeToOrder``.
#: They say "you can order it and it is not here yet", which neither confirms nor denies "in
#: stock" — so they set no ``in_stock`` value at all and decide nothing. Absence is not guilt,
#: and a pre-order badge is not a lie about stock.
_UNDECIDED_AVAILABILITY_TOKENS: frozenset[str] = frozenset(
    {"preorder", "presale", "backorder", "madetoorder", "sololongasstockslast"}
)


# ==============================================================================================
# The reading
# ==============================================================================================


@dataclass(frozen=True, slots=True)
class PageReading:
    """What one live product page states, in the catalogue's own key vocabulary.

    ``values`` is keyed the way a catalogue snapshot's ``attributes``/``offer`` blocks are
    keyed (``unit_price``, ``currency``, ``in_stock``, ``canonical_name``, ``sku`` …) rather
    than the way schema.org is, so :func:`check_pitch_against_page` can hand a value straight
    to :func:`claim_verification.compare` — the SAME comparator, with the same published
    tolerances, that grades the pitch against the exchange's snapshot. Two comparators for one
    kind of comparison is how two surfaces start disagreeing for reasons that are not the
    seller's.

    ``reason`` is non-empty exactly when nothing was readable, and it is the sentence that
    goes into the record. "This page could not be read" is a statement about the *platform's*
    reach, and a record that does not say so reads as a statement about the store.
    """

    url: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)
    #: key -> which of :data:`SURFACE_PRECEDENCE` the value was read from.
    surfaces: Mapping[str, str] = field(default_factory=dict)
    #: ``sku`` / ``gtin`` / ``canonical_name`` as the page states them. Not compared against
    #: anything here; carried so a reader of a disagreement can check the page really was the
    #: product the pitch was about.
    identity: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def readable(self) -> bool:
        """Whether this page decided anything at all."""
        return bool(self.values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "values": dict(self.values),
            "surfaces": dict(self.surfaces),
            "identity": dict(self.identity),
            "reason": self.reason,
        }


def unreadable_page(url: str, reason: str) -> PageReading:
    """A page that yielded nothing, and why. The honest answer for every failure mode.

    A refused fetch, a 404, a timeout, a blocked User-Agent, a redirect off the registered
    domain and a page with no structured data all arrive here, and they all produce the same
    thing: a reading that decides nothing. The reason differs and is recorded; the VERDICT is
    identical, because none of them is evidence about the seller.
    """
    return PageReading(url=str(url or ""), reason=str(reason))


@dataclass(frozen=True, slots=True)
class LiveReading:
    """One key compared across the two surfaces, and what the comparison said.

    Both readings are carried — ``pitched_value`` (what the auction served) and
    ``observed_value`` (what the page states) — because a disagreement nobody can re-examine
    is an accusation rather than evidence. ``compared`` names WHAT was compared in words, so
    the record is legible to somebody who was not holding this module when they read it.
    """

    key: str
    status: str
    pitched_value: Any
    observed_value: Any
    surface: str
    compared: str
    reason: str
    claim_ref: str = ""
    tolerance: float = FIELD_TOLERANCES["default"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "pitched_value": self.pitched_value,
            "observed_value": self.observed_value,
            "surface": self.surface,
            "compared": self.compared,
            "reason": self.reason,
            "claim_ref": self.claim_ref,
            "tolerance": self.tolerance,
        }


@dataclass(frozen=True, slots=True)
class LivePageCheck:
    """The whole outcome of checking one pitch against one live page.

    ``outcome`` is one of :data:`AGREES`, :data:`CONTRADICTED`, :data:`NO_VERDICT`, and it is
    :data:`CONTRADICTED` if and only if at least one reading is. ``readings`` carries every
    comparison that was actually made, agreements included: a record showing only the
    disagreements cannot be audited for what it declined to compare.
    """

    store_id: str = ""
    product_ref: str = ""
    url: str = ""
    outcome: str = NO_VERDICT
    readings: tuple[LiveReading, ...] = ()
    reason: str = ""
    page: PageReading = field(default_factory=PageReading)

    @property
    def contradictions(self) -> tuple[LiveReading, ...]:
        return tuple(row for row in self.readings if row.status == CONTRADICTED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "product_ref": self.product_ref,
            "url": self.url,
            "surface": LIVE_PAGE_SURFACE,
            "outcome": self.outcome,
            "reason": self.reason,
            "readings": [row.to_dict() for row in self.readings],
            "page": self.page.to_dict(),
        }


# ==============================================================================================
# Parsing a page
# ==============================================================================================


class _StructuredDataParser(HTMLParser):
    """Collect the three structured-data surfaces, and nothing else, from a page.

    Deliberately not a DOM. Three flat collections come out — the ``ld+json`` script bodies,
    the ``<meta>`` name/content pairs, and the microdata ``itemprop`` attributes — because
    that is everything the comparison needs and because a parser that builds no tree cannot be
    made to consume memory proportional to a hostile page's nesting depth.

    ``convert_charrefs`` is left at its default ``True`` so ``&amp;`` in a title is text
    rather than a separate event. :meth:`error` is overridden to do nothing: the base class
    raises on malformed markup in some Python versions, and a badly-written product page must
    yield NO VERDICT rather than an exception on a live path.
    """

    def __init__(self) -> None:
        super().__init__()
        self.ld_json: list[str] = []
        self.meta: list[tuple[str, str]] = []
        self.microdata: list[tuple[str, str]] = []
        self._in_ld_json = False
        self._pending_itemprop: str | None = None
        self._pending_text: list[str] = []

    # `HTMLParser.error` was removed in 3.5+ but is still called by some paths in the C
    # tokenizer's fallbacks; keeping it inert is free and removes a crash mode.
    def error(self, message: str) -> None:  # pragma: no cover - defensive
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        table = {name.lower(): (value or "") for name, value in attrs}
        if tag == "script":
            kind = table.get("type", "").strip().lower()
            self._in_ld_json = (
                kind == "application/ld+json" and len(self.ld_json) < MAX_LD_JSON_BLOCKS
            )
            return
        # `itemprop` is checked BEFORE the meta branch, and the order is load-bearing:
        # microdata's canonical spelling for a machine-readable value is
        # `<meta itemprop="price" content="29.97">`, so a meta branch that returned first
        # swallowed every microdata price on the page and reported "no structured data".
        itemprop = table.get("itemprop", "").strip().lower()
        if tag == "meta" and not itemprop:
            # Both spellings: OpenGraph uses `property`, the HTML spec uses `name`, and real
            # storefronts emit each.
            name = table.get("property") or table.get("name") or ""
            content = table.get("content") or ""
            if name and content and len(self.meta) < 512:
                self.meta.append((name.strip().lower(), content.strip()))
            return
        if not itemprop or len(self.microdata) >= 512:
            return
        # Microdata's value is `content` on a meta, `href` on a link, and the element text
        # otherwise. The first two are read here; the third is captured by `handle_data`.
        value = table.get("content") or table.get("href") or ""
        if value:
            self.microdata.append((itemprop, value.strip()))
            self._pending_itemprop = None
        else:
            self._pending_itemprop = itemprop
            self._pending_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_ld_json = False
            return
        if self._pending_itemprop is not None:
            text = "".join(self._pending_text).strip()
            if text and len(self.microdata) < 512:
                self.microdata.append((self._pending_itemprop, text[:512]))
            self._pending_itemprop = None
            self._pending_text = []

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self.ld_json.append(data)
        elif self._pending_itemprop is not None and len(self._pending_text) < 32:
            self._pending_text.append(data)


def _decoded(body: Any, encoding: str) -> str | None:
    """``body`` as text, or ``None`` when it is not something to parse."""
    if body is None:
        return None
    if isinstance(body, str):
        return body
    if isinstance(body, (bytes, bytearray, memoryview)):
        raw = bytes(body)
        try:
            return raw.decode(encoding or "utf-8", errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")
    return None


def _number(raw: Any) -> float | None:
    """A price as a finite float, or ``None``. Never raises on a seller's string.

    Currency symbols, thousands separators and a trailing currency code are stripped, because
    real themes emit ``"$1,299.00"`` and ``"1299.00 USD"`` in a ``content`` attribute and
    reading either as unparseable would silently turn a decidable page into no verdict.
    ``bool`` is refused before the conversion: ``True`` is not one dollar.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if math.isfinite(value) else None
    text = str(raw).strip()
    if not text:
        return None
    kept = []
    for char in text:
        if char.isdigit() or char in ".-":
            kept.append(char)
        elif char == "," and kept:
            # A thousands separator, dropped. A decimal comma ("1299,00") would be dropped
            # too and read as 129900 — so it is only dropped when a '.' also appears, which
            # is the unambiguous case.
            if "." in text:
                continue
            kept.append(".")
    candidate = "".join(kept).strip(".-")
    if not candidate:
        return None
    try:
        value = float(candidate)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _availability(raw: Any) -> bool | None:
    """A schema.org availability token as a boolean, or ``None`` when it decides nothing.

    ``None`` for ``PreOrder`` and friends, and ``None`` for anything unrecognised. An
    unrecognised token is a vocabulary this module has not been taught, which is the
    platform's gap and not the seller's — see :data:`_UNDECIDED_AVAILABILITY_TOKENS`.
    """
    if isinstance(raw, bool):
        return raw
    text = normalize_text(raw)
    if not text:
        return None
    # `https://schema.org/InStock`, `http://schema.org/OutOfStock`, or the bare token.
    token = text.rsplit("/", 1)[-1].replace(" ", "").replace("_", "").replace("-", "")
    if token in IN_STOCK_TOKENS:
        return True
    if token in OUT_OF_STOCK_TOKENS:
        return False
    if token in _UNDECIDED_AVAILABILITY_TOKENS:
        return None
    if token in {"true", "yes"}:
        return True
    if token in {"false", "no"}:
        return False
    return None


#: schema.org type tokens, CASEFOLDED. ``normalize_text`` casefolds, and a comparison against
#: the mixed-case spelling silently matched nothing — measured: every JSON-LD block on every
#: fixture read as "no structured data", which under "absence is not guilt" is a silent
#: no-verdict rather than a failure anybody would see.
_PRODUCT_TYPE = "product"
_AGGREGATE_OFFER_TYPE = "aggregateoffer"


def _types_of(node: Mapping[str, Any]) -> set[str]:
    """Every ``@type`` this node declares, casefolded and stripped of its schema.org prefix."""
    raw = node.get("@type") or node.get("type")
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    return {normalize_text(value).rsplit("/", 1)[-1] for value in values if value is not None}


def _walk(root: Any) -> Iterable[Mapping[str, Any]]:
    """Every mapping inside a decoded JSON-LD document, breadth-first and BOUNDED.

    Breadth-first so a page whose ``Product`` sits beside a deeply-nested ``BreadcrumbList``
    still finds it within :data:`MAX_JSON_NODES`, and bounded because the document was written
    by the party this check exists to hold to account.
    """
    queue: list[Any] = [root]
    seen = 0
    while queue and seen < MAX_JSON_NODES:
        node = queue.pop(0)
        seen += 1
        if isinstance(node, Mapping):
            yield node
            queue.extend(node.values())
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            queue.extend(node)


def _first_offer(product: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The first ``Offer``-ish mapping under a ``Product``'s ``offers``.

    ``offers`` is legitimately an object, a list, or an ``AggregateOffer`` — all three appear
    on real storefronts. An ``AggregateOffer``'s ``lowPrice`` is deliberately NOT read as a
    price: it is the cheapest of several variants, and comparing one variant's asking price
    against the cheapest of all of them manufactures disagreements out of a product having
    more than one size.
    """
    raw = product.get("offers")
    candidates = raw if isinstance(raw, (list, tuple)) else [raw]
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        if _AGGREGATE_OFFER_TYPE in _types_of(candidate):
            continue
        return candidate
    return None


def _from_json_ld(blocks: Sequence[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(values, identity)`` from the page's ``ld+json`` blocks, or two empty dicts."""
    values: dict[str, Any] = {}
    identity: dict[str, Any] = {}
    for block in blocks[:MAX_LD_JSON_BLOCKS]:
        text = block.strip()
        if not text:
            continue
        try:
            document = json.loads(text)
        except (ValueError, RecursionError):
            # A theme emitting invalid JSON is not a seller lying about a price.
            continue
        for node in _walk(document):
            if _PRODUCT_TYPE not in _types_of(node):
                continue
            name = node.get("name")
            if isinstance(name, str) and name.strip():
                identity.setdefault("canonical_name", name.strip()[:256])
                values.setdefault("canonical_name", name.strip()[:256])
            for source, target in (("sku", "sku"), ("gtin", "gtin"), ("gtin13", "gtin")):
                raw = node.get(source)
                if isinstance(raw, (str, int)) and str(raw).strip():
                    identity.setdefault(target, str(raw).strip()[:64])
            brand = node.get("brand")
            if isinstance(brand, Mapping):
                brand = brand.get("name")
            if isinstance(brand, str) and brand.strip():
                values.setdefault("brand", brand.strip()[:128])
            offer = _first_offer(node)
            if offer is None:
                continue
            price = _number(offer.get("price"))
            if price is not None:
                values.setdefault("unit_price", price)
            currency = offer.get("priceCurrency")
            if isinstance(currency, str) and currency.strip():
                values.setdefault("currency", currency.strip().upper()[:8])
            stock = _availability(offer.get("availability"))
            if stock is not None:
                values.setdefault("in_stock", stock)
            if values.get("unit_price") is not None:
                # The first Product with a price wins; a page listing "you may also like"
                # products must not have its recommendations read as this product's offer.
                return values, identity
    return values, identity


_MICRODATA_KEYS: Mapping[str, str] = {
    "price": "unit_price",
    "pricecurrency": "currency",
    "availability": "in_stock",
    "name": "canonical_name",
    "sku": "sku",
    "gtin": "gtin",
    "gtin13": "gtin",
    "brand": "brand",
}

_META_KEYS: Mapping[str, str] = {
    "product:price:amount": "unit_price",
    "og:price:amount": "unit_price",
    "product:price:currency": "currency",
    "og:price:currency": "currency",
    "product:availability": "in_stock",
    "og:availability": "in_stock",
    "og:title": "canonical_name",
}


def _from_pairs(
    pairs: Sequence[tuple[str, str]], table: Mapping[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    values: dict[str, Any] = {}
    identity: dict[str, Any] = {}
    for name, raw in pairs:
        key = table.get(name)
        if key is None or key in values:
            continue
        if key == "unit_price":
            price = _number(raw)
            if price is not None:
                values[key] = price
        elif key == "in_stock":
            stock = _availability(raw)
            if stock is not None:
                values[key] = stock
        elif key == "currency":
            token = raw.strip().upper()[:8]
            if token:
                values[key] = token
        else:
            text = raw.strip()[:256]
            if text:
                values[key] = text
                if key in {"sku", "gtin", "canonical_name"}:
                    identity.setdefault(key, text)
    return values, identity


def read_product_page(
    body: Any,
    *,
    url: str = "",
    encoding: str = "utf-8",
    max_bytes: int = MAX_PAGE_BYTES,
) -> PageReading:
    """The structured data one live product page states, or an unreadable reading and why.

    Args:
        body: the page bytes (or text) somebody else fetched. **Never fetched here.**
        url: the URL the bytes came from, carried onto the reading for the record.
        encoding: the charset to decode ``body`` with; errors are replaced, never raised.
        max_bytes: the ceiling. Over it the page is unreadable — see :data:`MAX_PAGE_BYTES`.

    Returns:
        A :class:`PageReading`. ``values`` is empty and ``reason`` is set for every failure
        mode, and there is no failure mode that produces a contradiction: a page that could
        not be read decides nothing.
    """
    if body is None:
        return unreadable_page(url, "no page body was fetched")
    size = len(body) if isinstance(body, (bytes, bytearray, memoryview, str)) else 0
    if size > int(max_bytes):
        return unreadable_page(
            url,
            f"the page is {size} bytes, over this check's {int(max_bytes)}-byte ceiling; "
            f"it was not parsed, and an unparsed page decides nothing",
        )
    text = _decoded(body, encoding)
    if text is None:
        return unreadable_page(url, f"the page body is a {type(body).__name__}, not bytes or text")
    if not text.strip():
        return unreadable_page(url, "the page is empty")

    parser = _StructuredDataParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - a seller's markup must never raise on this path
        return unreadable_page(
            url, f"the page could not be parsed as HTML ({exc.__class__.__name__})"
        )

    values: dict[str, Any] = {}
    surfaces: dict[str, str] = {}
    identity: dict[str, Any] = {}

    ld_values, ld_identity = _from_json_ld(parser.ld_json)
    micro_values, micro_identity = _from_pairs(parser.microdata, _MICRODATA_KEYS)
    meta_values, meta_identity = _from_pairs(parser.meta, _META_KEYS)

    for surface, block, ident in (
        (SURFACE_JSONLD, ld_values, ld_identity),
        (SURFACE_MICRODATA, micro_values, micro_identity),
        (SURFACE_OPENGRAPH, meta_values, meta_identity),
    ):
        for key, value in block.items():
            if key not in values:
                values[key] = value
                surfaces[key] = surface
        for key, value in ident.items():
            identity.setdefault(key, value)

    if not values:
        return unreadable_page(
            url,
            "the page carries no schema.org JSON-LD, no product microdata and no product "
            "meta tags, so there is nothing on it to compare a pitch against",
        )
    return PageReading(url=str(url or ""), values=values, surfaces=surfaces, identity=identity)


def page_vocabulary(reading: PageReading) -> frozenset[str]:
    """The keys this page can decide a claim on at all.

    The live-page twin of :func:`claim_verification.verifier.catalog_keys`, and it exists for
    the same reason that one does: a caller decomposing a pitch wants the key spellings the
    GRADER can actually resolve, so an honest sentence lands on a key the evidence carries
    instead of on one nothing does. Empty is a real answer and the common one.
    """
    return frozenset(str(key) for key in reading.values)


# ==============================================================================================
# The comparisons
# ==============================================================================================


def price_disagreement(
    asking_price: Any,
    reading: PageReading,
    *,
    currency: Any = None,
    key: str = "unit_price",
) -> LiveReading | None:
    """Compare what the store is ASKING in the auction against its own live page price.

    ``None`` when the comparison cannot be made at all — no price on the page, no asking
    price, or two different currencies. Every one of those is absence, and absence is not
    guilt.

    **The asymmetry, which is the whole design of this function.** A store asking LESS than
    its own public page is not contradicting itself: a profile-conditioned discount is the
    product (D22/D55 — what a shop buys by joining is the right to condition a discount on
    this shopper), and every honest sponsored bid in this system quotes below list. Grading
    that as a contradiction would mint a penalty for exactly the behaviour the platform
    exists to broker, and it would do it to every discounting store on every auction.

    A store asking MORE than its own public page is a different statement. The shopper was
    shown a number, the store's own site shows a smaller one, and whichever of the two is
    stale the shopper is worse off for having come through the platform. That is a
    cross-surface contradiction in the one direction that is decidable and that matters, and
    it is the drift case the check was built for: a snapshot that stopped being true.

    The threshold is ``FIELD_TOLERANCES["unit_price"]`` — the published half-percent, taken
    rather than restated, so a currency-rounding difference is never a contradiction and a
    tolerance argued about elsewhere is argued about once.
    """
    asking = _number(asking_price)
    observed = _number(reading.values.get("unit_price"))
    surface = reading.surfaces.get("unit_price", "")
    if asking is None or observed is None:
        return None
    if observed <= 0:
        # A page stating a zero price is a theme that has not rendered, not a free product.
        return None
    page_currency = normalize_text(reading.values.get("currency"))
    asked_currency = normalize_text(currency)
    if page_currency and asked_currency and page_currency != asked_currency:
        return LiveReading(
            key=key,
            status="unsupported",
            pitched_value=asking,
            observed_value=observed,
            surface=surface,
            compared=(
                f"the asking price ({asked_currency.upper()}) against the page's price "
                f"({page_currency.upper()})"
            ),
            reason=(
                "the page quotes a different currency, so the two numbers are not comparable "
                "and this check decides nothing about them"
            ),
            tolerance=tolerance_for(key),
        )

    tolerance = tolerance_for(key)
    ceiling = observed * (1.0 + tolerance)
    compared = "the price the auction served against the price on the store's own product page"
    if asking <= ceiling:
        return LiveReading(
            key=key,
            status="verified",
            pitched_value=asking,
            observed_value=observed,
            surface=surface,
            compared=compared,
            reason=(
                "the store is asking at or below its own live page price, which is what a "
                "discount looks like and is never a contradiction"
            ),
            tolerance=tolerance,
        )
    return LiveReading(
        key=key,
        status=CONTRADICTED,
        pitched_value=asking,
        observed_value=observed,
        surface=surface,
        compared=compared,
        reason=(
            f"the auction served {asking} while the store's own product page states "
            f"{observed}; the shopper was quoted more than this store's public price, beyond "
            f"the published {tolerance:.3%} tolerance"
        ),
        tolerance=tolerance,
    )


def availability_disagreement(reading: PageReading, *, offered: bool = True) -> LiveReading | None:
    """The store's own page says the offered product cannot be bought.

    ``None`` unless the page states an availability this module recognises AND that
    availability is negative: a page that says nothing about stock, or says ``PreOrder``,
    decides nothing. ``offered`` exists so a caller with no live offer (there is nothing being
    sold, so nothing to contradict) can turn the comparison off without special-casing it.
    """
    if not offered:
        return None
    stock = reading.values.get("in_stock")
    if not isinstance(stock, bool) or stock:
        return None
    return LiveReading(
        key="in_stock",
        status=CONTRADICTED,
        pitched_value=True,
        observed_value=False,
        surface=reading.surfaces.get("in_stock", ""),
        compared="a live offer served for this product against the page's own availability",
        reason=(
            "the auction served a live offer for this product while the store's own product "
            "page states it is not available to buy"
        ),
        tolerance=tolerance_for("in_stock"),
    )


def _claim_readings(claims: Any, reading: PageReading) -> list[LiveReading]:
    """Every pitch claim the page can decide, compared with the published comparator."""
    rows: list[LiveReading] = []
    if claims is None or isinstance(claims, (str, bytes)):
        return rows
    try:
        entries = list(claims)
    except TypeError:
        return rows
    for index, claim in enumerate(entries):
        if isinstance(claim, Mapping):
            key = claim.get("key")
            claimed = claim.get("value")
            op = claim.get("op")
            claim_ref = claim.get("claim_ref") or claim.get("key") or f"claim-{index}"
        else:
            key = getattr(claim, "key", None)
            claimed = getattr(claim, "value", None)
            op = getattr(claim, "op", None)
            claim_ref = getattr(claim, "claim_ref", None) or key or f"claim-{index}"
        name = str(key) if key is not None else ""
        if not name or name not in reading.values:
            # The page says nothing about this key. Not evidence, so not recorded as a
            # reading at all — a record listing every key the page could not decide would be
            # mostly noise and would read as a list of things the store failed to prove.
            continue
        if name == "unit_price":
            # Price is decided by `price_disagreement`, which knows about discounts. Running
            # the plain equality comparator here as well would contradict every honest
            # discounted bid, which is the trap `claim_verification.pitch` documents.
            continue
        outcome = compare(claimed, reading.values[name], key=name, op=op)
        rows.append(
            LiveReading(
                key=name,
                status=outcome.status,
                pitched_value=claimed,
                observed_value=outcome.observed,
                surface=reading.surfaces.get(name, ""),
                compared=f"the pitch's {name!r} against the store's own product page",
                reason=outcome.reason,
                claim_ref=str(claim_ref),
                tolerance=tolerance_for(name),
            )
        )
    return rows


def check_pitch_against_page(
    reading: PageReading,
    *,
    claims: Any = None,
    asking_price: Any = None,
    currency: Any = None,
    store_id: Any = "",
    product_ref: Any = "",
    offered: bool = True,
) -> LivePageCheck:
    """One pitch, one live page: what the two surfaces agree and disagree about.

    Args:
        reading: :func:`read_product_page`'s output. An unreadable one short-circuits to
            :data:`NO_VERDICT` carrying its own reason.
        claims: the pitch's atomic claims (``claim_verification.decompose_pitch``'s output, or
            the bid's structured ``claims``). Each is compared with :func:`compare` — the same
            comparator, the same published tolerances — and only for keys the page carries.
        asking_price: what the auction served for this slot. Compared by
            :func:`price_disagreement`, which is discount-aware and deliberately asymmetric.
        currency: the currency ``asking_price`` is in, so a cross-currency page decides
            nothing rather than contradicting everything.
        store_id, product_ref: carried onto the record. Not compared.
        offered: whether there is a live offer at all, for :func:`availability_disagreement`.

    Returns:
        A :class:`LivePageCheck`. :data:`CONTRADICTED` **only** when a reading contradicts;
        :data:`AGREES` when at least one comparison was made and none did; :data:`NO_VERDICT`
        when nothing was comparable, which is the ordinary outcome for most pages and costs
        the store nothing.
    """

    def answer(outcome: str, reason: str, rows: tuple[LiveReading, ...] = ()) -> LivePageCheck:
        """Every return from this function, so no branch can forget a field it must carry."""
        return LivePageCheck(
            store_id=str(store_id or ""),
            product_ref=str(product_ref or ""),
            url=reading.url,
            page=reading,
            outcome=outcome,
            readings=rows,
            reason=reason,
        )

    if not reading.readable:
        return answer(NO_VERDICT, reading.reason or "this page decided nothing")

    # The claim readings are computed FIRST so the offer-level comparisons can stand down on a
    # key a claim already decided. Without that, a pitch saying "in stock" against a sold-out
    # page produces TWO contradictions about one fact — the offer's and the claim's — and a
    # caller minting one penalty per contradiction charges the store twice for one page. The
    # claim reading is the one kept, because it names a `claim_ref` and the offer-level one
    # does not. Measured: the served suite's sold-out case emitted two `claim_verified` events.
    claim_rows = _claim_readings(claims, reading)
    decided = {row.key for row in claim_rows}

    readings: list[LiveReading] = []
    price = price_disagreement(asking_price, reading, currency=currency)
    if price is not None and price.key not in decided:
        readings.append(price)
    stock = availability_disagreement(reading, offered=offered)
    if stock is not None and stock.key not in decided:
        readings.append(stock)
    readings.extend(claim_rows)

    graded = [row for row in readings if row.status in {"verified", CONTRADICTED}]
    if any(row.status == CONTRADICTED for row in graded):
        contradicted = sorted({row.key for row in graded if row.status == CONTRADICTED})
        return answer(
            CONTRADICTED,
            "the store's own live product page states something different about "
            f"{', '.join(contradicted)}",
            tuple(readings),
        )
    if graded:
        return answer(
            AGREES,
            "the store's own live product page agrees on "
            f"{', '.join(sorted({row.key for row in graded}))}",
            tuple(readings),
        )
    return answer(
        NO_VERDICT,
        "the page was read but carries nothing this pitch asserted, so there was nothing to "
        "compare",
        tuple(readings),
    )
