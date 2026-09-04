"""The Catalog MCP adapter: the production-primary catalog read path (T-023, C6, A1).

SPEC C6 names two ``CatalogAdapter`` implementations. This is the first one — the catalog MCP
server, which is what production reads for an ordinary public store: it serves the merchant's
catalog as structured JSON, so there is no HTML to parse, no robots.txt to obey and no socket
for an SSRF guard to police.

SPEC A1 is why it is not the *only* one: **dev stores are password-protected, so they are
absent from the Global Catalog and this adapter cannot see them at all.** That is a fact about
the world, not a gap to paper over, so :meth:`CatalogMCPAdapter.fetch_catalog` says so out
loud in ``snapshot.warnings`` when it is handed a request carrying a storefront password
rather than quietly returning an empty catalog that reads like "this store has no products".
Fixtures ingest through :mod:`ingest.adapters.signed_fetch` for exactly this reason.

**Nothing here talks to Shopify.** A1 requires this adapter to be *contract-tested against a
recorded mock*, so the transport is a plain :class:`MCPSession` handed in by the caller, and
the one this repo ships is :class:`RecordedMCPSession` — a replayer over a recorded cassette
in ``fixtures/mcp/`` that **refuses any call it has no recording for**. A missing recording is
an error, never a live call and never an empty result, so a contract test cannot pass by
accident and ``make verify`` cannot start depending on a network.

The recorded contract
---------------------

One tool, called until the server says there is no next page::

    tool       catalog.list_products
    arguments  {"shop_domain": "<host>", "limit": <int>, "cursor": "<opaque>"}   (cursor omitted on page 1)
    result     an MCP CallToolResult:
               {"isError": false,
                "structuredContent": {"products": [...], "page_info": {...}},
                "content": [{"type": "text", "text": "<the same JSON>"}]}

``structuredContent`` is preferred and the ``content`` text blocks are the fallback, which is
the order a real MCP client uses and the order these two fields were added to the protocol in.
``isError`` is checked **before** either of them: an error result carries explanatory text in
the same ``content`` field a successful one carries data in, so a decoder that reaches for the
payload first will happily parse a failure into a catalog.

A product entry is Shopify-shaped but MCP-flavoured — ``gid://`` identifiers, money as
``{"amount", "currency_code"}`` — and every field of it is **untrusted merchant input** (C10).
Prices are coerced through :func:`ingest.adapters.mapping.coerce_price` (which refuses NaN,
infinity and negatives), availability through the graph's closed vocabulary, and
``online_store_url`` is accepted only when it is an ``http(s)`` URL on the shop's own host —
otherwise the provenance URL falls back to the ``mcp://`` resource this entry actually came
from, because a ``Source.url`` is stored in the graph and shown to people.

Identity and mapping are **shared with the page fetcher**, not reimplemented: product IDs,
variant IDs and the snapshot-to-upsert mapping all come from
:mod:`ingest.adapters.mapping`. That is what makes T-023 acceptance 2 ("mapping to graph
upserts matches ``signed_fetch`` semantics") structurally true — the same store read through
either adapter lands on the same nodes, in the same order, rather than on two parallel
sub-graphs that agree only until someone edits one of them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

from .base import CatalogAdapter as _CatalogAdapter
from .base import (
    CatalogRequest,
    CatalogSnapshot,
    FetchedResource,
    ProductRecord,
    UpsertOp,
    VariantRecord,
)
from .budgets import BudgetExceeded, CrawlLedger
from .hashing import canonical_json_hash, has_changed, snapshot_ref
from .mapping import (
    build_upserts,
    coerce_availability,
    coerce_price,
    composite_hash,
    native_key,
    native_product_key,
    product_id_for,
    safe_host,
    safe_split,
    variant_id_for,
)

__all__ = [
    "CatalogMCPAdapter",
    "MCPError",
    "MCPSession",
    "MCPToolError",
    "RecordedCall",
    "RecordedMCPSession",
    "UnrecordedMCPCall",
    "decode_tool_result",
    "mcp_resource_url",
]

ADAPTER_NAME = "catalog_mcp"
EXTRACTOR_VERSION = "catalog_mcp@1.0.0"
LIST_PRODUCTS_TOOL = "catalog.list_products"

#: Products requested per tool call. Bounded because the page ends up in memory and its digest
#: is the change-detection unit: one enormous page makes every re-read look changed.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 250

#: A `Source.url` is written to the graph and rendered to buyers. Anything longer than this is
#: not a link, it is a payload.
MAX_SOURCE_URL_LENGTH = 2048


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(value: Any) -> str:
    """Canonical JSON for cassette matching: key order and whitespace must not decide a match.

    Keys whose value is ``None`` are dropped, so an adapter that always sends ``cursor`` and a
    recording written without one still match on page 1. That is a normalisation of *absence*,
    which is the one thing two encodings of the same call disagree about; every other
    difference still misses.
    """
    if isinstance(value, Mapping):
        value = {k: v for k, v in value.items() if v is not None}
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def mcp_resource_url(shop: str, tool: str, cursor: str | None = None) -> str:
    """A stable URL naming one MCP tool result, for provenance and change detection.

    There is no HTTP URL to point at, but a ``Source`` must name *what* was read and a
    ``known_hashes`` entry must be keyed on something that does not move between runs. A
    page's cursor is part of its identity, so it is in the URL.

    Caveat worth knowing rather than engineering around: MCP cursors are opaque and a server
    is free to rotate them between runs, in which case page two's key moves and its
    ``FetchedResource.changed`` reads ``True`` on every run. Nothing downstream depends on
    that — change detection for *products* is keyed on ``product:<product_id>``, which no
    cursor touches, so the "unchanged content produces zero upserts" guarantee is unaffected.
    """
    suffix = f"?cursor={quote(str(cursor), safe='')}" if cursor else ""
    return f"mcp://{shop}/{tool}{suffix}"


# -----------------------------------------------------------------------------------------
# The session seam
# -----------------------------------------------------------------------------------------


class MCPError(RuntimeError):
    """Anything that went wrong talking to an MCP server."""


class MCPToolError(MCPError):
    """The server answered, and the answer was a failure or was not intelligible."""


class UnrecordedMCPCall(MCPError):
    """A replaying session was asked for a call the cassette does not contain.

    Raised rather than returning an empty result on purpose: "no recording" and "the store has
    no products" are the same value to every caller downstream, and a contract test that can
    pass by asking for something nobody recorded is not testing a contract.
    """


@runtime_checkable
class MCPSession(Protocol):
    """The one thing this adapter needs from an MCP client: call a tool, get a result back."""

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """Invoke ``name`` with ``arguments`` and return the raw ``CallToolResult``."""
        ...


def decode_tool_result(result: Any, *, tool: str = "") -> dict[str, Any]:
    """Unwrap an MCP ``CallToolResult`` into the JSON object it carries.

    Args:
        result: whatever the session returned.
        tool: the tool name, for the error message only.

    Returns:
        The payload object.

    Raises:
        MCPToolError: the result is an error, is not an MCP result at all, or carries no
            JSON object.
    """
    label = tool or "mcp tool"
    if not isinstance(result, Mapping):
        raise MCPToolError(
            f"{label}: expected an MCP tool result object, got {type(result).__name__}"
        )

    # FIRST. An error result carries its explanation in the same `content` field a successful
    # one carries data in; reaching for the payload before checking this parses a failure into
    # a catalog.
    if result.get("isError") or result.get("is_error"):
        raise MCPToolError(f"{label}: server reported an error: {_error_text(result)}")

    for key in ("structuredContent", "structured_content"):
        structured = result.get(key)
        if isinstance(structured, Mapping):
            return dict(structured)

    content = result.get("content")
    if isinstance(content, Sequence) and not isinstance(content, str | bytes):
        texts = [
            block["text"]
            for block in content
            if isinstance(block, Mapping)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        if texts:
            try:
                payload = json.loads("".join(texts))
            except ValueError as exc:
                raise MCPToolError(f"{label}: content block is not JSON ({exc})") from exc
            if isinstance(payload, Mapping):
                return dict(payload)
            raise MCPToolError(
                f"{label}: content block carried a {type(payload).__name__}, not a JSON object"
            )

    raise MCPToolError(
        f"{label}: result carries neither `structuredContent` nor a text `content` block; "
        "refusing to guess what it meant"
    )


def _error_text(result: Mapping[str, Any]) -> str:
    """Whatever explanation an error result carried, flattened to one line."""
    content = result.get("content")
    if isinstance(content, Sequence) and not isinstance(content, str | bytes):
        parts = [
            str(block.get("text"))
            for block in content
            if isinstance(block, Mapping) and isinstance(block.get("text"), str)
        ]
        if parts:
            return " ".join(" ".join(parts).split())[:500]
    for key in ("error", "message"):
        value = result.get(key)
        if value:
            return str(value)[:500]
    return "no detail given"


@dataclass(frozen=True)
class RecordedCall:
    """One call a :class:`RecordedMCPSession` was asked to make."""

    tool: str
    arguments: str  # canonical JSON, so two spellings of one call compare equal


class RecordedMCPSession:
    """Replays a recorded MCP cassette, and refuses everything it has no recording for (A1).

    A cassette is either a list of interactions or an object with an ``interactions`` list::

        {"contract_version": "1.0.0",
         "interactions": [{"tool": ..., "arguments": {...}, "response": {...}}, ...]}

    Args:
        interactions: the recordings.
        name: what to call this cassette in error messages.

    Raises:
        MCPError: an interaction is malformed, or two interactions record the same call — an
            ambiguous cassette would replay whichever one loaded last, silently.
    """

    def __init__(
        self, interactions: Iterable[Mapping[str, Any]], *, name: str = "<inline>"
    ) -> None:
        self.name = name
        self._responses: dict[tuple[str, str], Any] = {}
        self._calls: list[RecordedCall] = []
        self._replayed: set[tuple[str, str]] = set()
        for index, item in enumerate(interactions):
            if not isinstance(item, Mapping):
                raise MCPError(f"{name}: interaction {index} is not an object")
            tool = str(item.get("tool") or "").strip()
            if not tool:
                raise MCPError(f"{name}: interaction {index} names no tool")
            if "response" not in item:
                raise MCPError(f"{name}: interaction {index} ({tool}) records no response")
            arguments = item.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, Mapping):
                raise MCPError(
                    f"{name}: interaction {index} ({tool}) records "
                    f"{type(arguments).__name__} arguments, not an object"
                )
            key = (tool, _canonical(arguments))
            if key in self._responses:
                raise MCPError(
                    f"{name}: interaction {index} re-records {tool} {key[1]}; a cassette with "
                    "two answers to one call replays whichever loaded last"
                )
            self._responses[key] = item["response"]

    @classmethod
    def from_path(cls, path: str | Path) -> RecordedMCPSession:
        """Load a cassette from disk.

        Raises:
            MCPError: the file is not a cassette.
        """
        source = Path(path)
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MCPError(f"{source}: cassette could not be read ({exc})") from exc
        interactions = data.get("interactions") if isinstance(data, Mapping) else data
        if not isinstance(interactions, list):
            raise MCPError(f"{source}: cassette carries no `interactions` list")
        return cls(interactions, name=str(source))

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """Replay the recorded answer to this exact call.

        Raises:
            UnrecordedMCPCall: nothing was recorded for it.
        """
        key = (str(name), _canonical(dict(arguments or {})))
        self._calls.append(RecordedCall(tool=key[0], arguments=key[1]))
        if key not in self._responses:
            raise UnrecordedMCPCall(
                f"{self.name}: no recording for {key[0]} {key[1]}; "
                f"recorded calls are {sorted(t for t, _ in self._responses)}"
            )
        self._replayed.add(key)
        return self._responses[key]

    @property
    def calls(self) -> tuple[RecordedCall, ...]:
        """Every call attempted, in order — including the ones that had no recording."""
        return tuple(self._calls)

    @property
    def unplayed(self) -> tuple[str, ...]:
        """Recordings nothing asked for. A non-empty tuple means the adapter stopped early."""
        return tuple(
            f"{tool} {args}" for tool, args in self._responses if (tool, args) not in self._replayed
        )


# -----------------------------------------------------------------------------------------
# The adapter
# -----------------------------------------------------------------------------------------


class CatalogMCPAdapter:
    """Read a store's catalog from a catalog MCP server (C6, production primary).

    Args:
        session: the MCP client. Constructor state rather than a ``fetch_catalog`` parameter,
            so a caller downstream of ingestion can hold a bare ``CatalogAdapter``.
        clock: returns the ISO-8601 observation timestamp. Injectable for determinism.
        page_size: products requested per call, clamped to :data:`MAX_PAGE_SIZE`.
        tool: the catalog tool's name, for a server that publishes it under another one.
        extractor_version: the provenance version stamped on this adapter's snapshots.
    """

    def __init__(
        self,
        *,
        session: MCPSession | None = None,
        clock: Any = _now,
        page_size: int = DEFAULT_PAGE_SIZE,
        tool: str = LIST_PRODUCTS_TOOL,
        extractor_version: str = EXTRACTOR_VERSION,
    ) -> None:
        self.session = session
        self._clock = clock
        self.page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        self.tool = str(tool).strip() or LIST_PRODUCTS_TOOL
        self.extractor_version = str(extractor_version).strip() or EXTRACTOR_VERSION

    @classmethod
    def from_cassette(cls, path: str | Path, **kwargs: Any) -> CatalogMCPAdapter:
        """Build an adapter that replays a recorded cassette. No network, ever."""
        return cls(session=RecordedMCPSession.from_path(path), **kwargs)

    # -- CatalogAdapter ------------------------------------------------------------------

    def fetch_catalog(self, request: CatalogRequest) -> CatalogSnapshot:
        """Read ``request``'s catalog from the MCP server, under the request's budgets.

        Never raises for an ordinary read outcome. A server error, a blown budget, an
        unintelligible envelope or a store that cannot be reached at all ends the read and is
        reported in ``snapshot.warnings`` along with whatever was gathered first — a partial
        catalog is useful, and an exception halfway through a 40-page read is not.
        """
        observed_at = self._clock()
        ledger = CrawlLedger(request.budget)
        base = str(request.base_url or "").rstrip("/")
        shop = safe_host(base)
        warnings: list[str] = []
        resources: list[FetchedResource] = []
        products: list[ProductRecord] = []

        if request.storefront_password:
            # A1, stated rather than swallowed: a password-protected dev store is absent from
            # the Global Catalog, so an empty result here means "cannot see it", not "empty".
            warnings.append(
                "catalog MCP cannot reach a password-protected dev store (A1); the storefront "
                "password was ignored — ingest this store through signed_fetch instead"
            )

        max_products = max(0, int(request.max_products))
        if self.session is None:
            warnings.append("no MCP session configured; nothing was read")
        elif safe_split(base) is None:
            warnings.append(f"base_url {request.base_url!r} does not parse; nothing was read")
        elif not shop:
            warnings.append(f"base_url {request.base_url!r} names no host; nothing was read")
        elif max_products <= 0:
            warnings.append("request.max_products is not positive; nothing was read")
        else:
            entries = self._read_pages(
                self.session, request, shop, ledger, resources, warnings, max_products
            )
            products = self._to_records(request, shop, entries, warnings)

        return CatalogSnapshot(
            store_id=request.store_id,
            base_url=base,
            observed_at=observed_at,
            adapter=ADAPTER_NAME,
            products=tuple(products),
            resources=tuple(resources),
            usage=ledger.snapshot(),
            extractor_version=self.extractor_version,
            warnings=tuple(warnings),
        )

    def to_upserts(self, snapshot: CatalogSnapshot) -> list[UpsertOp]:
        """Map a snapshot to graph writes. Pure — no session, no clock, no network.

        The mapping is :func:`ingest.adapters.mapping.build_upserts`, the same function
        ``signed_fetch`` maps through, so "matches ``signed_fetch`` semantics" (T-023
        acceptance 2) is a property of the code rather than a coincidence two suites police.
        """
        return build_upserts(snapshot, extractor_version=self.extractor_version)

    # -- reading -------------------------------------------------------------------------

    def _read_pages(
        self,
        session: MCPSession,
        request: CatalogRequest,
        shop: str,
        ledger: CrawlLedger,
        resources: list[FetchedResource],
        warnings: list[str],
        max_products: int,
    ) -> list[tuple[dict[str, Any], str]]:
        """Page through the catalog tool. Returns ``(entry, resource_url)`` pairs."""
        entries: list[tuple[dict[str, Any], str]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while len(entries) < max_products:
            url = mcp_resource_url(shop, self.tool, cursor)
            arguments: dict[str, Any] = {
                "shop_domain": shop,
                "limit": max(1, min(self.page_size, max_products - len(entries))),
            }
            if cursor is not None:
                arguments["cursor"] = cursor

            try:
                ledger.charge_page()
            except BudgetExceeded as exc:
                warnings.append(f"budget: {exc}")
                break

            try:
                payload = decode_tool_result(
                    session.call_tool(self.tool, dict(arguments)), tool=self.tool
                )
            except MCPError as exc:
                warnings.append(f"{url}: {exc}")
                break
            except Exception as exc:  # noqa: BLE001 - an injected session may raise anything
                warnings.append(f"{url}: MCP session failed: {type(exc).__name__}: {exc}")
                break

            digest = canonical_json_hash(payload)
            size = len(_canonical(payload).encode("utf-8"))
            resources.append(
                FetchedResource(
                    url=url,
                    status=200,
                    content_hash=digest,
                    media_type="application/json",
                    changed=has_changed(request.known_hashes.get(url), digest),
                    snapshot_ref=snapshot_ref(url, digest),
                    bytes_downloaded=size,
                )
            )
            over_budget = False
            try:
                ledger.charge_bytes(size, response_total=size)
            except BudgetExceeded as exc:
                warnings.append(f"budget: {exc}")
                over_budget = True

            listed = payload.get("products")
            if not isinstance(listed, list):
                warnings.append(f"{url}: tool result carries no `products` list")
                break
            for entry in listed:
                if not isinstance(entry, Mapping):
                    warnings.append(f"{url}: skipped a catalog entry that is not an object")
                    continue
                entries.append((dict(entry), url))
                if len(entries) >= max_products:
                    break

            if over_budget:
                break
            cursor = self._next_cursor(payload, url, seen_cursors, warnings)
            if cursor is None:
                break
        return entries

    @staticmethod
    def _next_cursor(
        payload: Mapping[str, Any],
        url: str,
        seen: set[str],
        warnings: list[str],
    ) -> str | None:
        """The cursor for the next page, or ``None`` when the read is over.

        Refuses to follow a cursor it has already followed. A server that keeps handing back
        the same cursor — hostile, or merely buggy — would otherwise be read until the page
        budget ran out, and every page after the first would be a duplicate.
        """
        info = payload.get("page_info")
        if not isinstance(info, Mapping):
            info = payload.get("pageInfo")
        if not isinstance(info, Mapping):
            return None
        has_next = info.get("has_next_page")
        if has_next is None:
            has_next = info.get("hasNextPage")
        if not has_next:
            return None
        cursor = info.get("end_cursor")
        if cursor is None:
            cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor.strip():
            warnings.append(f"{url}: page_info claims a next page but names no cursor; stopping")
            return None
        if cursor in seen:
            warnings.append(f"{url}: server repeated cursor {cursor!r}; stopping to avoid a loop")
            return None
        seen.add(cursor)
        return cursor

    # -- mapping to records ----------------------------------------------------------------

    def _to_records(
        self,
        request: CatalogRequest,
        shop: str,
        entries: list[tuple[dict[str, Any], str]],
        warnings: list[str],
    ) -> list[ProductRecord]:
        """Turn raw catalog entries into records, in the order the server listed them.

        Source order is kept deliberately: it is the only ordering the server actually stated,
        and re-sorting on a store-supplied field would let the store's data decide the order
        its own products reach the graph in.
        """
        records: list[ProductRecord] = []
        seen: set[str] = set()
        for entry, url in entries:
            if not native_product_key(entry):
                warnings.append(f"{url}: catalog entry carries no id or handle; skipped")
                continue
            product_id = product_id_for(request.store_id, entry)
            if product_id in seen:
                warnings.append(
                    f"{url}: duplicate catalog entry for {product_id}; kept the first one"
                )
                continue
            seen.add(product_id)
            records.append(self._to_record(request, shop, entry, url, product_id, warnings))
        return records

    def _to_record(
        self,
        request: CatalogRequest,
        shop: str,
        entry: Mapping[str, Any],
        url: str,
        product_id: str,
        warnings: list[str],
    ) -> ProductRecord:
        """One MCP catalog entry as a `ProductRecord`. Every field is untrusted (C10)."""
        digest = composite_hash(canonical_json_hash(entry))
        known = request.known_hashes.get(f"product:{product_id}")
        native = native_product_key(entry)
        currency = _text(entry.get("currency") or entry.get("currency_code")) or ""

        return ProductRecord(
            product_id=product_id,
            canonical_name=_text(entry.get("title") or entry.get("name")),
            brand=_brand(entry),
            status=_text(entry.get("status")).lower() or "active",
            handle=_text(entry.get("handle")),
            source_url=self._source_url(entry, shop, request, url, warnings),
            content_hash=digest,
            changed=has_changed(known, digest),
            variants=self._variants(request.store_id, native, entry, currency, url, warnings),
            categories=_categories(entry),
        )

    def _variants(
        self,
        store_id: str,
        native: str,
        entry: Mapping[str, Any],
        default_currency: str,
        url: str,
        warnings: list[str],
    ) -> tuple[VariantRecord, ...]:
        """The purchasable variants of one entry, deduplicated on the graph's variant ID."""
        listed = entry.get("variants")
        if listed is None:
            return ()
        if not isinstance(listed, list):
            warnings.append(f"{url}: `variants` on {native or '<unnamed>'} is not a list; ignored")
            return ()

        out: list[VariantRecord] = []
        seen: set[str] = set()
        for item in listed:
            if not isinstance(item, Mapping):
                warnings.append(f"{url}: skipped a variant that is not an object")
                continue
            sku = _text(item.get("sku"))
            native_variant = (
                native_key(item.get("id") or item.get("variant_id"))
                or sku
                or _text(item.get("title"))
            )
            if not native_variant:
                warnings.append(f"{url}: variant of {native or '<unnamed>'} has no identifier")
                continue
            variant_id = variant_id_for(store_id, native, native_variant)
            if variant_id in seen:
                warnings.append(f"{url}: duplicate variant {variant_id}; kept the first one")
                continue
            seen.add(variant_id)
            price, currency = _money(item, default_currency)
            if price is None and item.get("price") is not None:
                warnings.append(
                    f"{url}: variant {variant_id} published an unusable price "
                    f"{item.get('price')!r}; no offer recorded"
                )
            out.append(
                VariantRecord(
                    variant_id=variant_id,
                    seller_sku=sku,
                    name=_text(item.get("title") or item.get("name")),
                    price=price,
                    currency=currency,
                    availability=_availability_of(item),
                    status=_text(item.get("status")).lower() or "active",
                )
            )
        return tuple(out)

    @staticmethod
    def _source_url(
        entry: Mapping[str, Any],
        shop: str,
        request: CatalogRequest,
        fallback: str,
        warnings: list[str],
    ) -> str:
        """The storefront URL for this product, when the merchant published a usable one.

        A ``Source.url`` is written to the graph and shown to buyers, and this one is a string
        the merchant chose, so it is accepted only when it is an ordinary ``http(s)`` URL on a
        host we are already reading. ``javascript:``, ``file://``, a link-local metadata
        address, an embedded credential and a host belonging to somebody else all fall back to
        the ``mcp://`` resource the entry actually arrived in.
        """
        raw = entry.get("online_store_url") or entry.get("onlineStoreUrl") or entry.get("url")
        candidate = _text(raw)
        if not candidate:
            return fallback
        if len(candidate) > MAX_SOURCE_URL_LENGTH:
            warnings.append(f"{fallback}: product URL is {len(candidate)} chars; ignored")
            return fallback
        split = safe_split(candidate)
        if split is None:
            warnings.append(f"{fallback}: product URL {candidate!r} does not parse; ignored")
            return fallback
        if split.scheme.lower() not in ("http", "https"):
            warnings.append(f"{fallback}: product URL {candidate!r} is not http(s); ignored")
            return fallback
        if split.username or split.password:
            warnings.append(f"{fallback}: product URL carries credentials; ignored")
            return fallback
        host = (split.hostname or "").lower()
        allowed = {str(h).lower() for h in (shop, *request.allowed_hosts) if h}
        if not host or not (host in allowed or any(host.endswith(f".{a}") for a in allowed)):
            warnings.append(f"{fallback}: product URL host {host!r} is not this store's; ignored")
            return fallback
        return candidate


# -----------------------------------------------------------------------------------------
# Untrusted-field readers
# -----------------------------------------------------------------------------------------


def _text(value: Any) -> str:
    """A trimmed string, or ``""`` — booleans and containers are not text."""
    if value is None or isinstance(value, bool | dict | list | tuple | set):
        return ""
    return str(value).strip()


def _brand(entry: Mapping[str, Any]) -> str:
    """The brand, whether the server states it flat or as a nested schema.org node."""
    for key in ("vendor", "brand"):
        value = entry.get(key)
        if isinstance(value, Mapping):
            name = _text(value.get("name"))
            if name:
                return name
            continue
        text = _text(value)
        if text:
            return text
    return ""


def _categories(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """Category names, deduplicated, in the order the server listed them."""
    names: list[str] = []
    for key in ("product_type", "productType", "category"):
        value = entry.get(key)
        name = _text(value.get("name")) if isinstance(value, Mapping) else _text(value)
        if name and name not in names:
            names.append(name)
    listed = entry.get("categories")
    if isinstance(listed, list):
        for value in listed:
            name = _text(value.get("name")) if isinstance(value, Mapping) else _text(value)
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _money(item: Mapping[str, Any], default_currency: str) -> tuple[float | None, str]:
    """A variant's price and currency, from either the money-object or the flat shape."""
    raw = item.get("price")
    if raw is None:
        raw = item.get("price_amount")
    currency = ""
    if isinstance(raw, Mapping):
        currency = _text(raw.get("currency_code") or raw.get("currencyCode") or raw.get("currency"))
        raw = raw.get("amount")
    if not currency:
        currency = _text(
            item.get("currency_code") or item.get("currencyCode") or item.get("currency")
        )
    return coerce_price(raw), (currency or default_currency or "USD")


def _availability_of(item: Mapping[str, Any]) -> str:
    """A variant's availability, preferring the boolean the catalog states outright."""
    available = item.get("available")
    if available is not None:
        return coerce_availability(available)
    for key in ("availability", "inventory_status", "inventoryStatus"):
        if item.get(key) is not None:
            return coerce_availability(item.get(key))
    return "unknown"


def _satisfies_catalog_adapter() -> bool:
    """Structural check that this really is the interface both adapters share (C6).

    Asserted at runtime by ``test_catalog_mcp.py`` rather than trusted: a protocol nobody
    verifies against is a comment.
    """
    return isinstance(CatalogMCPAdapter(), _CatalogAdapter)
