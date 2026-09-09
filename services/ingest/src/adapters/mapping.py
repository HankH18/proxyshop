"""The snapshot -> graph-upsert mapping **both** catalog adapters share (T-023, C6).

SPEC C6 puts catalog ingestion behind one adapter interface, and T-023 acceptance 2 requires
the MCP adapter's "mapping to graph upserts" to *match ``signed_fetch`` semantics*. Two
implementations of the same mapping cannot be relied on to match — they match on the day they
are written and drift on the next change to either — so the mapping exists once, here, and
both :class:`~ingest.adapters.signed_fetch.SignedFetchAdapter` and
:class:`~ingest.adapters.catalog_mcp.CatalogMCPAdapter` call it. "Matches" is then structural
rather than a claim a test has to keep re-proving.

Everything in this module is **pure**: no network, no clock, no session, no randomness. The
only inputs are the snapshot and the caller's provenance identity, so the same snapshot always
produces the same ops in the same order. That is what lets ``to_upserts`` be asserted
byte-for-byte and what makes "unchanged content produced zero work" checkable by counting.

Two identity rules live here rather than in either adapter, because they are what makes the
seam a seam:

``product_id_for`` / ``variant_id_for``
    A product read over MCP and the same product read off the storefront resolve to the *same*
    graph node, because both IDs are derived from ``(store_id, the store's own identifier)``
    and nothing else. If each adapter minted its own IDs, ingesting one store through both
    adapters would silently double every product in the graph.

``composite_hash``
    A product's change-detection digest covers every surface it was built from. The parts are
    joined with a separator rather than concatenated so that a one-surface adapter and a
    two-surface adapter cannot collide on the same digest by accident.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit

from ..graph.model import (
    AttributeValue,
    Category,
    MediaAsset,
    Offer,
    Product,
    Source,
    Store,
    Variant,
    canonical_text,
    slug,
)
from .base import AttributeRecord, CatalogSnapshot, ImageRecord, UpsertOp
from .hashing import content_hash

__all__ = [
    "AVAILABILITY_VOCABULARY",
    "MEDIA_ENV",
    "MEDIA_PER_PRODUCT_LIMIT",
    "build_upserts",
    "catalog_source",
    "coerce_availability",
    "coerce_price",
    "composite_hash",
    "image_records",
    "option_attributes",
    "media_asset_id_for",
    "media_enabled",
    "native_key",
    "native_product_key",
    "on_seller_domain",
    "price_is_stated",
    "product_id_for",
    "safe_host",
    "safe_split",
    "stable_id",
    "variant_id_for",
]

#: How many image records one product may contribute to the graph. Merchant-controlled and
#: therefore bounded here rather than trusted: see :func:`image_records` for the measurement
#: behind the number (96.4% of the recorded corpus kept, 2.8% of its products truncated,
#: worst real product 81 images).
MEDIA_PER_PRODUCT_LIMIT = 12

#: How many attribute readings one product may contribute to the graph. Merchant-controlled
#: and therefore bounded here rather than trusted, exactly as the gallery above is.
#:
#: Measured over the nineteen recorded storefronts (4,903 products, 23,122 readings after the
#: sentinel drop and the variant join): mean 6.7 per product, median 5, p90 11, p95 18, p99 31,
#: **max 104**. A limit of 48 sits well above p99 and bites 27 products — 0.55% of the corpus
#: — while keeping 93.7% of every reading.
#:
#: The tail is where the junk is and that is the second reason for a bound. The worst product
#: in the corpus, at 104 readings, is ``branchfurniture``'s ``XCover Protection Plan``, a
#: warranty SKU. ``ingest.graph.reembed.embedding_text`` composes a product's embedded text
#: from its attributes among other things and has no length cap of its own, so an uncapped
#: read would embed a document dominated by colour names rather than by what the product is.
ATTRIBUTES_PER_PRODUCT_LIMIT = 48

#: Shopify writes ``options: [{"name": "Title", "values": ["Default Title"]}]`` for a product
#: that declares no options at all. It is a placeholder, not a fact about the product, and it
#: accounts for 1,460 of the corpus's option readings. Matched case-insensitively on BOTH
#: halves, so a real option named ``Title`` carrying a real value is still read.
_OPTION_SENTINEL = ("title", "default title")

#: How many option slots a variant can state. Shopify's variant record carries exactly
#: ``option1``, ``option2`` and ``option3``, so an ``options[]`` entry that binds to no slot in
#: this range binds to nothing a variant can confirm. Measured over the nineteen recorded
#: storefronts: 0 of 4,903 products publish a fourth option.
_MAX_OPTION_SLOTS = 3

#: The environment name that turns media writes off. Unset means ON, which is what every
#: load in this repository has always done — see :func:`media_enabled`.
MEDIA_ENV = "PROXYSHOP_INGEST_MEDIA"

#: The values that mean "off". Anything else, including an unset variable and an empty
#: string, means on: a typo must not silently stop writing a third of the graph.
MEDIA_OFF_VALUES = frozenset({"0", "false", "no", "off"})


def media_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether a load writes ``MediaAsset`` nodes and ``HAS_MEDIA`` edges.

    WHAT MEDIA COSTS, measured on the ten-store recorded corpus (3,093 products, one
    process): 17,520 ``MediaAsset`` merges and 17,520 ``HAS_MEDIA`` merges — 15.0% of the
    234,119 statements — plus the 17,520 existence probes those media ops were the only
    reason for. Media is the single largest kind of write in the load.

    WHY IT IS STILL ON BY DEFAULT. Nothing in this repository *reads* it — searched for
    ``MediaAsset``, ``HAS_MEDIA`` and ``asset_id`` across every app, service and package,
    and every match is in ``services/ingest`` (this mapping, the upsert, the model, and
    their tests); ``exchange.retrieval.catalogue`` builds its snapshot out of
    ``ingest.graph.query.catalogue_entry``, whose Cypher names no media at all. But
    ``ingest`` writes it deliberately, for a downstream verified-primary media rule that is
    designed and not yet built (see ``services/ingest/tests/test_catalog_images.py``), and a
    loader that silently stopped recording 17,520 real image records would be a much worse
    defect than the round-trips it saves. So this is a switch an operator throws, per load,
    and its default is exactly what the code did before it existed.

    Args:
        env: the environment to read; the process environment when ``None``.

    Returns:
        ``False`` only when the variable is set to one of :data:`MEDIA_OFF_VALUES`
        (case-insensitively).
    """
    source = os.environ if env is None else env
    return str(source.get(MEDIA_ENV, "") or "").strip().lower() not in MEDIA_OFF_VALUES


#: ``1,234`` and ``1,234,567.89`` are prices; ``12,50`` and ``1,2345`` are not. A comma that
#: does not group digits in threes is a decimal comma or a typo, and either way the number it
#: appears in cannot be read without guessing which.
_THOUSANDS_GROUPED = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")

#: What a storefront actually writes in a price field, once commas are resolved. Deliberately
#: narrower than ``float``: no underscores, no exponent, no leading ``+``, no ``inf``/``nan``
#: spellings. A price outside this shape is refused rather than coerced into a wrong number.
_DECIMAL = re.compile(r"-?\d+(?:\.\d*)?|-?\.\d+")

#: schema.org / storefront availability tokens -> the vocabulary `ingest.graph` stores on an
#: Offer. One table for every adapter: a second copy would drift, and an availability string
#: the graph does not recognise is indistinguishable downstream from "we never looked".
AVAILABILITY_VOCABULARY: dict[str, str] = {
    "instock": "in_stock",
    "in_stock": "in_stock",
    "onlineonly": "in_stock",
    "available": "in_stock",
    "limitedavailability": "limited",
    "limited": "limited",
    "outofstock": "out_of_stock",
    "out_of_stock": "out_of_stock",
    "soldout": "out_of_stock",
    "discontinued": "discontinued",
    "preorder": "preorder",
    "presale": "preorder",
    "backorder": "backorder",
}


def coerce_availability(value: Any) -> str:
    """Fold a store's availability token into the graph's closed vocabulary.

    Anything unrecognised becomes ``"unknown"`` rather than passing through: a free-text
    availability string stored on an Offer would be read downstream as a state nobody handles.
    """
    if isinstance(value, bool):
        return "in_stock" if value else "out_of_stock"
    token = str(value or "").strip().lower().rsplit("/", 1)[-1].replace(" ", "")
    return AVAILABILITY_VOCABULARY.get(token, "unknown")


def coerce_price(value: Any) -> float | None:
    """Read a price out of untrusted store input, or ``None`` when there is no usable one.

    ``None`` means "no offer" — :func:`build_upserts` emits an ``Offer`` only for a variant
    that has a price, so a refused value costs the offer rather than poisoning the graph.

    Two classes of input are refused rather than coerced, and both are reachable from a store
    that simply serves what it likes (C10):

    * **Non-finite.** ``json`` parses the bare tokens ``NaN``, ``Infinity`` and ``-Infinity``
      by default, and ``float("nan")`` accepts the string form too. A NaN price compares false
      against *everything*, so one NaN offer silently corrupts every price filter, sort and
      price-honoured trust observation that touches it — and it never raises.
    * **Negative.** A negative price is not a discount, it is nonsense that would win any
      cheapest-offer ranking outright.

    Booleans are refused too: ``float(True)`` is ``1.0``, and a store that serves
    ``"price": true`` should not be quoted a price of one.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if "," in text:
        # Commas are a THOUSANDS separator or they are nothing. Stripping them unconditionally
        # read the European decimal ``"12,50"`` as ``1250.0`` — a hundredfold overcharge that
        # reached the graph as a price, and that T-249 then made unrecoverable by refusing to
        # let the page's correct JSON-LD figure fill in. Only the grouped form is accepted.
        if not _THOUSANDS_GROUPED.fullmatch(text):
            return None
        text = text.replace(",", "")
    if not _DECIMAL.fullmatch(text):
        # ``float`` accepts things no storefront writes and every one of them is a misread
        # rather than a price: ``"1_000"`` -> 1000.0 (Python literal underscores),
        # ``"1e3"`` -> 1000.0, ``"infinity"``, ``"nan"``, ``"+5"``. Refusing them costs the
        # offer, which is the documented answer for a price we cannot read.
        return None
    try:
        price = float(text)
    except (TypeError, ValueError):
        return None
    if price != price or price in (float("inf"), float("-inf")):  # NaN, ±Infinity
        return None
    if price < 0:
        return None
    return price


def price_is_stated(value: Any) -> bool:
    """Whether the store said anything at all in this price field.

    :func:`coerce_price` answers ``None`` for two situations a consumer must not confuse:
    *"there is no price here"* and *"there is a price here and it is unusable"*. A merge that
    reads both as "not stated" lets a store choose which of its surfaces prices a product by
    making the other one unusable — write ``-5.00`` in ``products.json`` and the theme's
    JSON-LD wins, which is the opposite of the documented precedence.

    Stated means the field carries something. Absent, JSON ``null`` and whitespace are the
    three ways a store says nothing; a hostile number, a boolean and unparseable text are all
    *statements*, and a statement we refuse costs the offer rather than promoting another
    surface's number in its place.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping | list | tuple | set):
        # A *shape* is not a statement of this kind. The catalog MCP adapter reads the money
        # object ``{"amount": "12.00", "currency_code": "USD"}`` through its own ``_money``
        # unwrapper; ``coerce_price`` cannot read it at all, so treating it as "the store
        # stated a price we refuse" would suppress the JSON-LD gap-fill and silently drop the
        # Offer for a variant whose price the OTHER adapter reads perfectly well. Measured
        # through a real crawl before this clause: no ``offer`` op emitted at all.
        # The T-249 trade is about hostile SCALARS; it must not extend to shapes.
        return False
    return True


def safe_split(url: Any) -> SplitResult | None:
    """``urlsplit`` that answers ``None`` instead of raising on a URL that will not parse.

    ``urlsplit("http://[")`` raises ``ValueError: Invalid IPv6 URL``, and the strings this
    package splits include ones a *merchant* chose (a product's ``online_store_url``) and ones
    a caller passed in. An adapter whose contract is "a bad read is a warning, never an
    exception" cannot have a store-supplied string that aborts the whole run.
    """
    try:
        return urlsplit(str(url or ""))
    except ValueError:
        return None


def safe_host(url: Any) -> str:
    """The lower-cased hostname of ``url``, or ``""`` when there is not one to be had."""
    split = safe_split(url)
    return (split.hostname or "").lower() if split is not None else ""


def stable_id(*parts: Any) -> str:
    """A deterministic 32-hex digest over ``parts``, separated so they cannot run together.

    ``("ab", "c")`` and ``("a", "bc")`` must not hash alike: the unit separator between parts
    is what keeps two different identities from colliding into one graph node.
    """
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


def composite_hash(*parts: str) -> str:
    """The digest of everything one record was built from, in order.

    A missing surface is an empty part rather than an omitted one, so "catalog entry, no page"
    and "catalog entry, page that hashed to nothing" stay distinguishable.
    """
    return content_hash("|".join("" if p is None else str(p) for p in parts))


def native_key(value: Any) -> str:
    """One store-supplied identifier, reduced to the form every adapter agrees on.

    Shopify states the *same* identifier two ways depending on which surface you read:
    ``products.json`` serves ``8123456`` and the catalog MCP server serves
    ``gid://shopify/Product/8123456``. Both name one product, so both have to reduce to one
    key — otherwise ingesting a store through both adapters writes two ``Product`` nodes for
    every product it sells, each supported by its own ``Source``, and nothing downstream can
    tell they are the same thing. A GID's identity is its final path segment; a query string
    or fragment is not part of it.

    Booleans are refused rather than stringified: ``"True"`` is not an identifier, and letting
    one through would give every such record the same node.
    """
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if text.lower().startswith("gid://"):
        tail = text.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1].strip()
        return tail or text
    return text


def native_product_key(entry: Mapping[str, Any]) -> str:
    """The store's own identifier for a product, whatever field it publishes it under.

    ``products.json`` and the MCP catalog both call it ``id``; a store that publishes neither
    is identified by its ``handle``.
    """
    for key in ("id", "product_id", "handle"):
        text = native_key(entry.get(key))
        if text:
            return text
    return ""


def product_id_for(store_id: str, entry: Mapping[str, Any]) -> str:
    """The graph's stable ID for a storefront product.

    Derived from the store and the store's own identifier for the product, so the same product
    keeps the same node across crawls (a re-crawl of unchanged content cannot churn the graph
    through ID drift) **and** across adapters (C6: one seam, one node).

    An entry that names *no* identifier is refused rather than hashed. The empty key is one
    value, so hashing it gives every unidentifiable entry from a store the **same** ``prod_``
    id: two different products merge into one graph node, and a store's whole unnamed catalog
    collapses into a single product, silently, on the first real write. Both adapters skip such
    an entry with a warning before reaching here; this raise is what stops a third one from
    reintroducing the collision by forgetting to.

    Raises:
        ValueError: ``entry`` names no ``id``, ``product_id`` or ``handle``.
    """
    native = native_product_key(entry)
    if not native:
        raise ValueError(
            "product_id_for needs the store's own identifier: an entry naming no id, "
            "product_id or handle would hash the empty key, and every such entry from this "
            "store would land on that one product node"
        )
    return f"prod_{stable_id(store_id, native)}"


def variant_id_for(store_id: str, native_product: str, native_variant: str) -> str:
    """The graph's stable ID for one purchasable variant of one store's product."""
    return f"var_{stable_id(store_id, native_product, native_variant)}"


def media_asset_id_for(store_id: str, product_id: str, native_image: str) -> str:
    """The graph's stable ID for one image record of one store's product.

    Keyed on the *graph* product id rather than the store's raw product key, because
    :func:`product_id_for` already folds (store, native key) into it — so this stays stable
    across adapters for exactly the reason a variant id does, without a second copy of the
    native-key rule. ``native_image`` falls back to the image URL upstream, which is what
    keeps a re-crawl idempotent for a store that numbers no images.
    """
    return f"mda_{stable_id(store_id, product_id, native_image)}"


def option_attributes(
    entry: Mapping[str, Any], *, limit: int = ATTRIBUTES_PER_PRODUCT_LIMIT
) -> tuple[AttributeRecord, ...]:
    """Read a catalogue entry's ``options[]`` block into product attribute readings.

    Shared by both adapters for the same reason :func:`image_records` is (C6): a surface read
    two ways is a surface that lands two ways.

    Why this surface, and only this surface
    ---------------------------------------
    ``options[]`` is ``{name, position, values[]}`` — machine-structured, merchant-filled in a
    typed form, positionally bound to each variant's ``option1/2/3``, and re-checkable against
    the same URL the crawl read. The platform's assertion is exactly *"this product publishes
    value V under option named K"*, which is literally what the page says.

    ``options[].values`` IS THE PICKER'S DOMAIN, NOT THE PRODUCT'S FACTS
    -------------------------------------------------------------------
    That is the one thing this surface does *not* say, and reading it as if it did was an R19
    and D55 defect with a driven consequence. ``values[]`` is the set of choices the option
    control can display — a merchant who shares one option block across a product family, or
    who retires a variant without pruning the picker, leaves values in it this product does not
    have. The witness in ``fixtures/real-catalogs-demo`` is ``nemoequipment.com``'s ``Astro™
    Non-Insulated Lightweight Sleeping Pad``: its block declares
    ``Insulation: ["Insulated", "Non-Insulated"]`` and its only two variants are
    ``Non-Insulated / Regular`` and ``Non-Insulated / Long Wide``. Written unjoined, that pad
    satisfied ``HardCriterion(field="insulation", op="eq", value="Insulated")`` and its
    pushdown's ``value_string="insulated"`` matched ``_FILTER_AND_RETURN``'s
    ``a.canonical_value_string = f.value_string``, so ``candidate_products`` returned the
    **non-insulated** pad for a shopper who required insulation.

    So a value is written only where **at least one variant of that product carries it**, and
    the join is the positional one: ``options[]`` entry *n* binds to ``variants[].option``\\ *n*,
    compared under :func:`~ingest.graph.model.canonical_text` — the same fold
    ``AttributeValue.canonical_value_string`` stores and the candidate query compares, so the
    join can neither keep a reading retrieval cannot match nor drop one over a stray capital.

    WHICH JOIN, measured rather than assumed. Shopify states a variant's values twice: in
    ``option1/2/3``, and again in ``title`` as those values joined with ``" / "``. Over the
    nineteen storefronts (28,134 variant records):

    * ``option1/2/3`` — **0** variants state none of them, **0** options declare a ``position``
      disagreeing with their index, **0** products publish a fourth option, and **0** declared
      option slots go unstated by every variant.
    * ``title`` — **2,560 of 28,134** titles do not decompose into the number of values their
      variant states, because a value may itself contain ``" / "``; a title join would drop
      **2,418** readings on 112 products, 2,335 of them true. Where a title does decompose it
      agrees with the positional slots 28,134 times out of 28,134.

    The positional join is therefore the source and the title is at best its confirmation.
    Where the join cannot be made — no variant carries the value, the option binds to no slot
    a variant can state, the entry publishes no variants at all — **nothing is written**. That
    is the D55 answer: a dropped true attribute costs a retrieval, an asserted false one sells
    a shopper the wrong product.

    Measured over the nineteen recorded storefronts, **after** the join: **21,667 readings on
    3,443 of 4,903 products (70.2%), 4,090 distinct ``AttributeValue`` ids under 147 keys**,
    led by ``size`` (2,129 products), ``style`` (803) and ``color`` (754). The join refuses 83
    of the 21,750 readings the unjoined read produced (0.38%, on 35 products) — capacity 40,
    gender 22, color 11, width 4, silhouette 2, length 2, insulation 2 — and **0** of them is a
    value any variant of its product carries, so its cost in true readings on this corpus is
    zero. All 83 are on one host, which is a fact about this corpus's composition and not
    evidence the unjoined surface was safe.

    The join costs no coverage either, measured on the same corpus: the product count is
    unchanged at 3,443 (no product loses its last reading), and so are the 4,090 distinct
    ``AttributeValue`` ids and 147 keys — every refused value is genuinely carried by some
    *other* product, which is precisely why it looked plausible on the one it was not.

    Three neighbouring surfaces are deliberately NOT read, each for its own reason:

    * ``tags`` — 40,853 applications across 4,516 distinct tags, and a tag is a bare token
      with **no key**, so promoting one means the platform inventing the attribute name. The
      corpus's most common are ERP codes and theme flags: ``Prop65``, ``Els PW 8602``,
      ``all items``, ``YBlocklist``, ``Discount %|0``, ``Not Searchable``.
    * ``product_type`` — already promoted, as the ``category`` op and
      ``(Product)-[:IN_CATEGORY]->(Category)``. A second copy on a different node type is two
      records free to disagree about one fact.
    * ``variants[].available`` / ``price`` — already the graph ``Offer``, with its own
      ``observed_at`` and its own ``Source``. ``.swarm-loop/decisions.md`` rejects this one by
      name.

    THE KEY IS SLUGGED HERE, at the write, and that is a deliberate choice
    ---------------------------------------------------------------------
    The three consumers of an attribute key do not agree on spelling.
    :meth:`~exchange.retrieval.criteria.HardCriterion.canonical_field` and
    ``AttributeFilter.as_parameter`` both fold through :func:`~ingest.graph.model.slug`, while
    ``claim_verification.verifier._lookup_attribute`` compares ``str(key)`` with **no**
    normalisation against the raw spelling ``graph/query.py`` returns. So writing the
    merchant's ``options[].name`` verbatim would give the verifier a vocabulary it cannot
    match, and would keep the corpus's ``Bag Size`` (33 products) and ``Bag size`` (32) as two
    keys. Slugging at the write makes ``key`` and ``canonical_key`` the same string for every
    consumer, and it is the spelling the intent corpus already uses — ``color``, ``size``,
    ``material``.

    The cost is stated rather than hidden: the merchant's display casing is not kept. The
    VALUE is verbatim, which is what a shopper reads and what a claim is graded against.

    Args:
        entry: one catalogue entry, exactly as the storefront published it. Untrusted (C10).
        limit: the per-product ceiling; see :data:`ATTRIBUTES_PER_PRODUCT_LIMIT`.

    Returns:
        The readings, in catalogue order (option position, then value order), bounded by
        ``limit`` — and only those a variant of this entry actually carries.
    """
    options = entry.get("options")
    if not isinstance(options, list):
        return ()
    carried = _variant_option_values(entry)
    out: list[AttributeRecord] = []
    for index, option in enumerate(options):
        if not isinstance(option, Mapping):
            continue
        name = str(option.get("name") or "").strip()
        key = slug(name)
        if not key:
            continue
        values = option.get("values")
        if not isinstance(values, list):
            continue
        slot = _option_slot(option, index)
        stated = carried.get(slot) if slot is not None else None
        if not stated:
            # The option binds to a slot no variant of this product states, to no slot at all
            # (a fourth option, and a Shopify variant has three), or to a slot the merchant
            # named two different ways. Nothing here is checkable, so nothing here is written.
            continue
        for raw in values:
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                continue
            value = str(raw).strip()
            if not value:
                continue
            if (name.casefold(), value.casefold()) == _OPTION_SENTINEL:
                continue
            if canonical_text(value) not in stated:
                # The picker offers it; no variant was built from it. See the docstring:
                # writing this is what let a non-insulated sleeping pad satisfy an
                # "insulated" hard constraint.
                continue
            out.append(AttributeRecord(key=key, value=value))
            if len(out) >= limit:
                return tuple(out)
    return tuple(out)


def _option_slot(option: Mapping[str, Any], index: int) -> int | None:
    """Which ``variants[].option``\\ *n* an ``options[]`` entry binds to, 1-based, or ``None``.

    The merchant states this binding TWICE — as ``options[].position`` and as the entry's place
    in the array — and over the nineteen recorded storefronts the two never disagree: 0 of
    4,903 products state a ``position`` other than their index plus one. So the two statements
    are treated as one fact that has to hold, not as a preference between them:

    * they agree, or no ``position`` is stated at all -> that slot;
    * they disagree -> ``None``. Which slot the option binds to is exactly what a merchant has
      just said two different things about, so there is nothing to check the values against and
      :func:`option_attributes` writes none of them (C10, D55). This also disposes of two
      options both claiming ``position: 1``: at most one of them can be first in the array, so
      the other one is a disagreement and is dropped rather than checked against a slot holding
      some other option's values.
    * the slot is outside 1..3 -> ``None``. A Shopify variant states three options; a fourth
      binds to nothing a variant can confirm.
    """
    slot = index + 1
    position = option.get("position")
    if isinstance(position, int) and not isinstance(position, bool) and position != slot:
        return None
    return slot if 1 <= slot <= _MAX_OPTION_SLOTS else None


def _variant_option_values(entry: Mapping[str, Any]) -> dict[int, set[str]]:
    """What this entry's variants ACTUALLY carry, per 1-based option slot.

    Canonically folded with :func:`~ingest.graph.model.canonical_text` so the comparison is the
    one ``AttributeValue.canonical_value_string`` and the candidate query already make: a
    merchant whose picker says ``"Deep  NAVY"`` and whose variant says ``"deep navy"`` has
    stated one value, not two, and both spellings resolve to one ``attr_id``.

    A slot absent from the result is a slot **no** variant stated — which is not the same as a
    slot every variant left blank on purpose, but is treated the same way, because neither is
    evidence a value was ever built.
    """
    variants = entry.get("variants")
    if not isinstance(variants, list):
        return {}
    carried: dict[int, set[str]] = {}
    for variant in variants:
        if not isinstance(variant, Mapping):
            continue
        for slot in range(1, _MAX_OPTION_SLOTS + 1):
            raw = variant.get(f"option{slot}")
            if raw is None or isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                continue
            text = str(raw).strip()
            if text:
                carried.setdefault(slot, set()).add(canonical_text(text))
    return carried


def image_records(
    entry: Mapping[str, Any], *, limit: int = MEDIA_PER_PRODUCT_LIMIT
) -> tuple[tuple[ImageRecord, ...], int]:
    """Read ``images[]`` off a catalogue entry into bounded, ordered
    :class:`~ingest.adapters.base.ImageRecord` values.

    Shared by both adapters for the same reason the rest of this module is (C6): a gallery
    read two ways is a gallery that lands two ways.

    **The bound is not politeness, it is a ceiling on a hostile input.** ``images[]`` is
    merchant-controlled and unbounded — a store could publish ten thousand URLs and turn one
    product into ten thousand graph nodes. Measured on the recorded corpus
    (``fixtures/real-catalogs/``, 3,093 real products): mean 5.9 images, median 5, p90 10,
    p99 17, **max 81**. :data:`MEDIA_PER_PRODUCT_LIMIT` keeps 96.4% of every image in that
    corpus while truncating 2.8% of products, and caps the worst case at
    :data:`MEDIA_PER_PRODUCT_LIMIT` nodes per product no matter what a store publishes.

    Order is the catalogue's own ``position`` and the tie-break is the entry order, so the
    kept prefix is the *front* of the gallery — the primary image is never the one dropped.

    Args:
        entry: one catalogue product, as the store published it.
        limit: how many images to keep, at most.

    Returns:
        ``(records, published)`` — the bounded records and how many the catalogue published,
        so a caller can report the truncation rather than hiding it.
    """
    raw = entry.get("images")
    if not isinstance(raw, list):
        return (), 0
    candidates: list[tuple[int, int, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        src = str(item.get("src") or item.get("url") or "").strip()
        if not src:
            # No URL is no asset: it can be neither shown nor checked, and a node keyed on
            # the empty string would collapse every such image of a product onto one node.
            continue
        candidates.append((_position(item.get("position"), index), index, item))
    published = len(candidates)
    candidates.sort(key=lambda row: (row[0], row[1]))

    records: list[ImageRecord] = []
    for position, _index, item in candidates[: max(int(limit), 0)]:
        src = str(item.get("src") or item.get("url") or "").strip()
        records.append(
            ImageRecord(
                native_id=native_key(item.get("id")) or src,
                src=src,
                position=position,
                alt=str(item.get("alt") or ""),
                width=_dimension(item.get("width")),
                height=_dimension(item.get("height")),
                updated_at=str(item.get("updated_at") or ""),
            )
        )
    return tuple(records), published


def _position(value: Any, index: int) -> int:
    """The catalogue's 1-based gallery position, or the entry order when it states none."""
    try:
        position = int(value)
    except (TypeError, ValueError):
        return index + 1
    return position if position > 0 else index + 1


def _dimension(value: Any) -> int:
    """A stated pixel dimension, or ``0`` — never a guess and never a negative number."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def on_seller_domain(image_host: str, store_domain: str) -> bool:
    """Whether ``image_host`` is the store's own registered domain or a subdomain of it.

    The one leg of the owner's verified-media rule ingest can decide without a fetch. A bare
    suffix test would call ``evilgaiaherbs.com`` a subdomain of ``gaiaherbs.com``, so the
    match is exact or on a label boundary.
    """
    host = str(image_host or "").strip().lower().rstrip(".")
    domain = str(store_domain or "").strip().lower().rstrip(".")
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


def catalog_source(
    url: str,
    digest: str,
    observed_at: str,
    extractor_version: str,
    *,
    confidence: float = 0.9,
    source_class: str = "scraped",
) -> Source:
    """A ``Source`` node for one observed catalog surface — DESIGN: ``Source`` is Provenance.

    ``source_id`` is derived from the URL and the digest, so re-observing unchanged content
    produces the *same* Source rather than a new one every run.

    ``source_class`` stays ``"scraped"`` for both adapters on purpose. D29 routes the class to
    a buyer-facing label — ``scraped`` reads "from their website", while ``seller_asserted``
    carries no label and diverts the fact into the R18 verification queue. A catalog we read
    ourselves, whether off the storefront or out of the catalog MCP server, is an observation
    of the store's own published catalog, not a claim the seller submitted to us; classing the
    MCP read as ``seller_asserted`` would silently unlabel every MCP-sourced product.
    """
    return Source(
        source_id=f"src_{stable_id(url, digest)}",
        url=url,
        content_hash=digest,
        observed_at=observed_at,
        extractor_version=extractor_version,
        confidence=confidence,
        source_class=source_class,
    )


def build_upserts(
    snapshot: CatalogSnapshot,
    *,
    extractor_version: str = "",
    source_class: str = "scraped",
    include_media: bool | None = None,
) -> list[UpsertOp]:
    """Map a catalog snapshot to ordered graph writes. Pure — no I/O, no clock.

    Only ``snapshot.changed_products`` are visited, which is where the "unchanged content
    produces zero re-extraction work" guarantee actually lives: for a run whose every hash
    matched, this returns ``[]`` without inspecting a single product.

    Order is dependency order and is load-bearing — ``ingest.graph.upsert`` refuses to attach
    an ``Offer`` to a ``Variant`` that does not exist yet rather than creating a placeholder,
    so the store precedes its products, a product precedes its variants, and a variant
    precedes its offer.

    Args:
        snapshot: what the adapter observed.
        extractor_version: the fallback provenance version, used only when the snapshot does
            not carry one of its own. The snapshot's value wins so that the provenance names
            the adapter that actually did the reading.
        source_class: the provenance class for every ``Source`` minted here.
        include_media: emit ``media`` ops. ``None`` — the default everywhere — asks
            :func:`media_enabled`, which is on unless the environment turns it off, so the
            default behaviour is unchanged. Passing ``False`` drops the ``MediaAsset`` nodes
            and ``HAS_MEDIA`` edges and nothing else: no other op's identity, order or
            provenance depends on an image.

    Raises:
        ValueError: neither the snapshot nor the caller named an extractor version, which
            would produce a ``Source`` that cannot be traced back to any code.
    """
    changed = snapshot.changed_products
    if not changed:
        return []
    with_media = media_enabled() if include_media is None else bool(include_media)

    version = str(snapshot.extractor_version or "").strip() or str(extractor_version or "").strip()
    if not version:
        raise ValueError(
            "build_upserts needs an extractor version: a Source that cannot name the code "
            "that produced it satisfies the provenance audit without providing provenance"
        )

    store_domain = safe_host(snapshot.base_url)
    store_source = catalog_source(
        snapshot.base_url,
        content_hash(snapshot.base_url),
        snapshot.observed_at,
        version,
        confidence=1.0,
        source_class=source_class,
    )
    ops: list[UpsertOp] = [
        UpsertOp(
            kind="store",
            node=Store(
                store_id=snapshot.store_id,
                domain=safe_host(snapshot.base_url),
            ),
            source=store_source,
        )
    ]

    for product in changed:
        source = catalog_source(
            product.source_url or snapshot.base_url,
            product.content_hash or content_hash(product.product_id),
            snapshot.observed_at,
            version,
            source_class=source_class,
        )
        ops.append(
            UpsertOp(
                kind="product",
                node=Product(
                    product_id=product.product_id,
                    canonical_name=product.canonical_name,
                    brand=product.brand,
                    status=product.status,
                ),
                source=source,
            )
        )
        ops.append(
            UpsertOp(
                kind="sells",
                node=None,
                source=source,
                context={"store_id": snapshot.store_id, "product_id": product.product_id},
            )
        )
        # ORDERED HERE, after the product exists and before anything that depends on it.
        # `upsert_attribute` MATCHes both endpoints and raises `ProvenanceRequired` when
        # either is missing rather than creating a placeholder, so an attribute op ahead of
        # its product op fails the write.
        #
        # `AttributeValue.__post_init__` refuses a blank key or a reading with no value
        # component at all; `option_attributes` cannot produce either, and a third producer
        # that could would fail loudly here rather than writing an unreadable node.
        for reading in product.attributes:
            ops.append(
                UpsertOp(
                    kind="attribute",
                    node=AttributeValue(key=reading.key, value_string=reading.value),
                    source=source,
                    context={"product_id": product.product_id},
                )
            )
        for name in product.categories:
            ops.append(
                UpsertOp(
                    kind="category",
                    node=Category(name=name),
                    source=source,
                    context={"product_id": product.product_id},
                )
            )
        images = product.images if with_media else ()
        for image in images:
            # The asset is keyed on the store's own image identifier, NOT on the URL alone:
            # Shopify re-stamps `?v=<epoch>` on every image edit, so a URL-keyed node would
            # mint a fresh MediaAsset on every re-crawl of a store that touched its gallery,
            # and the old ones would linger, sourced and stale, forever.
            ops.append(
                UpsertOp(
                    kind="media",
                    node=MediaAsset(
                        asset_id=media_asset_id_for(
                            snapshot.store_id, product.product_id, image.native_id
                        ),
                        url=image.src,
                        catalogue_hash=content_hash("\x1f".join(image.catalogue_digest_parts)),
                        host=safe_host(image.src),
                        on_seller_domain=on_seller_domain(safe_host(image.src), store_domain),
                        position=image.position,
                        alt=image.alt,
                        width=image.width,
                        height=image.height,
                    ),
                    source=source,
                    context={"product_id": product.product_id},
                )
            )
        for variant in product.variants:
            ops.append(
                UpsertOp(
                    kind="variant",
                    node=Variant(
                        variant_id=variant.variant_id,
                        seller_sku=variant.seller_sku,
                        name=variant.name,
                        status=variant.status,
                        # The storefront's own id, carried BESIDE the graph key rather than
                        # in place of it. `offer_id` below is derived from `variant_id`, and
                        # the raw id is not unique per variant even inside one store — one
                        # recorded host publishes a single native id under two products 108
                        # times — so re-keying on it would merge those pairs, and their
                        # offers with them, under the global uniqueness constraint.
                        native_variant_id=variant.native_variant_id,
                    ),
                    source=source,
                    context={"product_id": product.product_id},
                )
            )
            if variant.price is None:
                continue
            ops.append(
                UpsertOp(
                    kind="offer",
                    node=Offer(
                        offer_id=f"off_{stable_id(snapshot.store_id, variant.variant_id)}",
                        price=float(variant.price),
                        currency=variant.currency,
                        availability=variant.availability,
                        observed_at=snapshot.observed_at,
                    ),
                    source=source,
                    context={
                        "store_id": snapshot.store_id,
                        "variant_id": variant.variant_id,
                    },
                )
            )
    return ops
