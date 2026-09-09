"""The signed page fetcher: a `CatalogAdapter` over a real storefront (T-020, C6, A1).

What this adapter is for, from SPEC A1: *dev stores are password-protected, so they are
absent from Shopify's Global Catalog and their public endpoints sit behind the storefront
password.* The MCP adapter cannot see them at all. This one can, because it does what a
browser does — establishes a storefront-password session — and then reads the two surfaces
every Shopify storefront publishes:

* ``/products.json`` — the paginated catalog, authoritative for variants, SKUs and prices.
* the product pages' ``application/ld+json`` blocks — schema.org ``Product``/``Offer``,
  authoritative for brand, currency and availability, and present even when a theme has
  customised everything else.

Read together they cover more than either alone, and disagreements are resolved in favour
of ``products.json`` (a machine endpoint) over JSON-LD (theme-authored, more often stale).

Three properties are load-bearing and each is enforced structurally rather than by care:

**It cannot be pointed at your own network.** Every request goes through
:class:`~ingest.adapters.transport.SafeHTTPClient`, whose default policy refuses anything
not publicly routable and re-checks every redirect hop. There is no code path in this module
that opens a socket itself.

**It cannot be made to crawl forever.** Every request is charged to a
:class:`~ingest.adapters.budgets.CrawlLedger` before it is sent, pagination stops on the
budget as well as on the data, and product pages are fetched at depth 1 only.

**Unchanged content does no downstream work.** Every resource is hashed as it arrives and
compared against ``request.known_hashes``; a product whose page and catalog entry both hash
to what we already have is marked ``changed=False``, and :meth:`SignedFetchAdapter.to_upserts`
emits nothing at all for it. The zero is structural — ``to_upserts`` iterates
``snapshot.changed_products`` — not a conditional someone could forget.

Everything fetched is untrusted store input (C10). It is parsed, coerced and bounded here;
it is never interpolated into a query, a prompt, or a URL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urljoin

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
from .hashing import canonical_json_hash, has_changed
from .mapping import (
    MEDIA_PER_PRODUCT_LIMIT,
    build_upserts,
    coerce_availability,
    coerce_price,
    composite_hash,
    image_records,
    native_product_key,
    price_is_stated,
    product_id_for,
    safe_host,
    safe_split,
    variant_id_for,
)
from .netguard import FetchRefused
from .robots import USER_AGENT, may_fetch, robots_url, robots_verdict_for_status
from .transport import HTTPResult, HTTPTransport, RequestSigner, SafeHTTPClient, TransportError

__all__ = ["SignedFetchAdapter"]

ADAPTER_NAME = "signed_fetch"
EXTRACTOR_VERSION = "signed_fetch@1.0.0"
PRODUCTS_PATH = "/products.json"
PASSWORD_PATH = "/password"
_PAGE_SIZE = 250


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SignedFetchAdapter:
    """Ingest a storefront over HTTP, including a password-protected dev store.

    Args:
        user_agent: the identified crawler agent string (C6).
        signer: optional HMAC signer, so a merchant can verify a crawl is really ours.
        client: a pre-built transport satisfying
            :class:`~ingest.adapters.transport.HTTPTransport`; one is constructed per request
            when omitted, which is what keeps the per-request SSRF policy in
            :class:`CatalogRequest` effective. This is the seam
            :class:`~ingest.adapters.recorded.RecordedTransport` uses to replay a recorded
            crawl without opening a socket — everything above ``client.fetch`` stays the
            live path.
        clock: returns the ISO-8601 observation timestamp. Injectable for determinism.
        session_password_path: the storefront password form path.
    """

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        signer: RequestSigner | None = None,
        client: HTTPTransport | None = None,
        clock=_now,
        session_password_path: str = PASSWORD_PATH,
        include_media: bool | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.signer = signer
        self._client = client
        self._clock = clock
        self.session_password_path = session_password_path
        #: Emit ``media`` ops. ``None`` defers to
        #: :func:`~ingest.adapters.mapping.media_enabled`, i.e. the environment, i.e. on.
        self.include_media = include_media

    # -- CatalogAdapter ------------------------------------------------------------------

    def fetch_catalog(self, request: CatalogRequest) -> CatalogSnapshot:
        """Read ``request.base_url``'s catalog under its budgets, guards and robots rules.

        Never raises for an ordinary crawl outcome. A blown budget, a refused host or a
        transport failure ends the crawl and is reported in ``snapshot.warnings`` with
        whatever was gathered before it — a partial catalog is useful and an exception
        halfway through a 200-page crawl is not.
        """
        observed_at = self._clock()
        ledger = CrawlLedger(request.budget)
        client = self._client or SafeHTTPClient(
            policy=request.policy, user_agent=self.user_agent, signer=self.signer
        )
        base = str(request.base_url or "").rstrip("/")
        if safe_split(base) is None:
            # `urlsplit("http://[")` raises. Every URL this crawl builds is derived from the
            # base, so one that does not parse has to end the crawl here rather than as a
            # ValueError out of the transport's host check.
            return CatalogSnapshot(
                store_id=request.store_id,
                base_url=base,
                observed_at=observed_at,
                adapter=ADAPTER_NAME,
                usage=ledger.snapshot(),
                extractor_version=EXTRACTOR_VERSION,
                warnings=(f"base_url {request.base_url!r} does not parse; crawl abandoned",),
            )
        host = safe_host(base)
        allowed = tuple(dict.fromkeys((host, *request.allowed_hosts)))

        resources: list[FetchedResource] = []
        warnings: list[str] = []
        products: list[ProductRecord] = []

        def get(url: str, **kwargs) -> HTTPResult | None:
            try:
                return client.fetch(url, allowed_hosts=allowed, ledger=ledger, **kwargs)
            except (FetchRefused, TransportError) as exc:
                warnings.append(f"{url}: {exc}")
                return None

        def record(result: HTTPResult) -> FetchedResource:
            known = request.known_hashes.get(result.url) or request.known_hashes.get(
                result.final_url
            )
            resource = FetchedResource(
                url=result.final_url,
                status=result.status,
                content_hash=result.content_hash,
                media_type=result.media_type,
                changed=has_changed(known, result.content_hash),
                snapshot_ref=result.snapshot_ref,
                bytes_downloaded=result.bytes_downloaded,
                redirect_chain=result.redirect_chain,
            )
            resources.append(resource)
            return resource

        robots_text = ""
        try:
            robots = get(robots_url(base))
            posture = robots_verdict_for_status(robots.status if robots else None)
            if robots is not None:
                record(robots)
            if posture == "disallow-all":
                warnings.append("robots.txt unavailable or forbidding; crawl abandoned")
                return CatalogSnapshot(
                    store_id=request.store_id,
                    base_url=base,
                    observed_at=observed_at,
                    adapter=ADAPTER_NAME,
                    resources=tuple(resources),
                    usage=ledger.snapshot(),
                    extractor_version=EXTRACTOR_VERSION,
                    warnings=tuple(warnings),
                )
            if posture == "use" and robots is not None:
                robots_text = robots.text()

            if request.storefront_password:
                self._unlock(client, base, request, ledger, robots_text, warnings)

            products = self._read_catalog(
                client, base, request, ledger, robots_text, allowed, get, record, warnings
            )
        except BudgetExceeded as exc:
            warnings.append(f"budget: {exc}")

        return CatalogSnapshot(
            store_id=request.store_id,
            base_url=base,
            observed_at=observed_at,
            adapter=ADAPTER_NAME,
            products=tuple(products),
            resources=tuple(resources),
            usage=ledger.snapshot(),
            robots_txt=robots_text,
            extractor_version=EXTRACTOR_VERSION,
            warnings=tuple(warnings),
        )

    def to_upserts(self, snapshot: CatalogSnapshot) -> list[UpsertOp]:
        """Map a snapshot to graph writes, in dependency order. Pure — no I/O.

        Delegates to :func:`ingest.adapters.mapping.build_upserts`, which is the *one*
        snapshot-to-graph mapping both catalog adapters use (C6, T-023 acceptance 2): the MCP
        adapter matching this adapter's upsert semantics is then structural rather than a
        second implementation someone has to keep in step.

        Only ``snapshot.changed_products`` are visited, which is where the "unchanged content
        produces zero re-extraction work" guarantee actually lives: for a crawl whose every
        hash matched, this returns ``[]`` without inspecting a single product.
        """
        return build_upserts(
            snapshot, extractor_version=EXTRACTOR_VERSION, include_media=self.include_media
        )

    # -- storefront password (A1) ---------------------------------------------------------

    def _unlock(
        self,
        client: HTTPTransport,
        base: str,
        request: CatalogRequest,
        ledger: CrawlLedger,
        robots_text: str,
        warnings: list[str],
    ) -> None:
        """Establish a storefront-password session.

        Posts the same form a browser posts. The session cookie the store sets is kept by
        the transport's jar, bound to the host that issued it, so it rides on subsequent
        catalog requests and on nothing else.
        """
        url = urljoin(base + "/", self.session_password_path.lstrip("/"))
        if not may_fetch(robots_text, url, self.user_agent):
            warnings.append(f"robots.txt disallows {url}; storefront stays locked")
            return
        payload = urlencode(
            {
                "form_type": "storefront_password",
                "utf8": "✓",
                "password": request.storefront_password or "",
            }
        ).encode("utf-8")
        try:
            result = client.fetch(
                url,
                method="POST",
                body=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                allowed_hosts=(safe_host(base),),
                ledger=ledger,
            )
        except (FetchRefused, TransportError) as exc:
            warnings.append(f"storefront password POST failed: {exc}")
            return
        if result.status >= 400:
            warnings.append(f"storefront password rejected with HTTP {result.status}")

    # -- catalog ---------------------------------------------------------------------------

    def _read_catalog(
        self,
        client: HTTPTransport,
        base: str,
        request: CatalogRequest,
        ledger: CrawlLedger,
        robots_text: str,
        allowed: tuple[str, ...],
        get,
        record,
        warnings: list[str],
    ) -> list[ProductRecord]:
        raw: list[tuple[dict, str, str]] = []  # (payload, source_url, page_hash)
        page = 1
        page_size = min(_PAGE_SIZE, request.max_products)
        stopped_at_product_ceiling = False
        # The first budget refusal of the crawl, as text; empty until one happens. It is the
        # flag AND the message: once a budget has refused a fetch, this method issues no
        # further requests of ANY kind, because the budget refused *the crawl*, not the loop
        # it happened to be raised in. See the product-page phase below for what that is
        # worth in requests.
        budget_refusal = ""
        while len(raw) < request.max_products:
            query = urlencode({"limit": page_size, "page": page})
            url = f"{base}{PRODUCTS_PATH}?{query}"
            if not may_fetch(robots_text, url, self.user_agent):
                warnings.append(f"robots.txt disallows {url}")
                break
            if not ledger.may_enqueue(0):
                break
            try:
                result = get(url)
            except BudgetExceeded as exc:
                # NOT re-raised to `fetch_catalog`. It used to be, and the products already
                # read were lost with the stack: measured on `fixtures/real-catalogs-broad`,
                # `cotopaxi.com` blew the per-response ceiling on page 5 and the crawl
                # reported ZERO products having already parsed 1,000 of them across pages
                # 1-4. This adapter's own contract is that a blown budget "ends the crawl and
                # is reported in snapshot.warnings with whatever was gathered before it", and
                # `catalog_mcp` has always done exactly that; only this path threw the
                # gathered half away. The warning states the loss so the count is never
                # mistaken for the catalogue.
                warnings.append(
                    f"budget: {exc}; abandoned {url} after {len(raw)} product(s) — this store's "
                    f"product list is what was read before the ceiling, not its catalogue"
                )
                budget_refusal = str(exc)
                break
            if result is None or result.status != 200:
                if result is not None and result.status != 200:
                    warnings.append(f"{url}: HTTP {result.status}")
                break
            record(result)
            try:
                payload = json.loads(result.text())
            except ValueError as exc:
                warnings.append(f"{url}: products.json did not parse ({exc})")
                break
            entries = payload.get("products") if isinstance(payload, dict) else None
            if not isinstance(entries, list) or not entries:
                break
            full_page = len(entries) >= page_size
            for position, entry in enumerate(entries):
                if isinstance(entry, dict):
                    raw.append((entry, result.final_url, canonical_json_hash(entry)))
                if len(raw) >= request.max_products:
                    # Entries left unread on this page, or a page that came back full and so
                    # implies another: either way it is the ceiling that ended the read, not
                    # the catalogue running out.
                    stopped_at_product_ceiling = position + 1 < len(entries) or full_page
                    break
            if not full_page:
                break
            page += 1

        if stopped_at_product_ceiling:
            # This warning is new because the truncation was silent: the loop simply stopped,
            # and the caller got a number with no way to tell a 250-product store from the
            # first 250 products of a 3,805-product one. `StoreTarget.max_products` defaults
            # to 250 and `taylorstitch.com` in `fixtures/real-catalogs-broad` really does have
            # 3,805, so the difference is not hypothetical.
            warnings.append(
                f"{base}{PRODUCTS_PATH}: stopped at max_products={request.max_products} while "
                f"the catalogue was still answering with full pages; this is the first "
                f"{len(raw)} product(s), not the whole catalogue"
            )

        products: list[ProductRecord] = []
        truncated_galleries = 0
        skipped_product_pages = 0
        for entry, page_url, entry_hash in raw:
            if not native_product_key(entry):
                # `product_id_for` derives the node id from the store's own identifier, and an
                # entry naming none of `id`, `product_id` or `handle` yields the empty key —
                # so EVERY unidentifiable entry from this store would hash to the same
                # `prod_…` and two different products would merge into one graph node.
                # `catalog_mcp` already skips these; the two adapters share one mapping (C6,
                # T-023) and a divergence here is a divergence in what reaches the graph, which
                # is the half an interface check over `inspect.signature` cannot see.
                warnings.append(f"{page_url}: catalog entry carries no id or handle; skipped")
                continue
            handle = str(entry.get("handle") or "").strip()
            page_hash = ""
            jsonld: dict[str, Any] = {}
            product_url = urljoin(base + "/", f"products/{handle}") if handle else ""
            if request.fetch_product_pages and handle and ledger.may_enqueue(1):
                if budget_refusal:
                    # A budget has already refused a fetch on this crawl, so no request is
                    # made for this product's page. Breaking the pagination loop alone was
                    # not enough: control falls straight into this loop, which used to
                    # attempt a fetch PER PRODUCT and merely warn when each was refused. A
                    # store serving one oversized catalogue page therefore went from 3
                    # charged requests to the whole page budget (measured: 40 of 40, 37 of
                    # them product pages) and from 1 warning to 214 — 213 of them the same
                    # sentence. `max_response_bytes` is a per-response ceiling raised inside
                    # the transport, so the crawl still had pages, bytes and time left and
                    # nothing else stopped it. The entries already in `raw` are still turned
                    # into records below — throwing those away is the defect this loop's
                    # pagination handler exists to prevent — they simply carry their catalog
                    # entry without the page's JSON-LD, which the summary warning states.
                    skipped_product_pages += 1
                elif may_fetch(robots_text, product_url, self.user_agent):
                    try:
                        ledger.charge_depth(1)
                        result = get(product_url)
                    except BudgetExceeded as exc:
                        # First refusal in this phase ends the fetching for the rest of it,
                        # rather than being reported once per remaining product.
                        budget_refusal = str(exc)
                        skipped_product_pages += 1
                        result = None
                    if result is not None and result.status == 200:
                        resource = record(result)
                        page_hash = resource.content_hash
                        jsonld = self._json_ld_product(result.text(), warnings, product_url)
                else:
                    warnings.append(f"robots.txt disallows {product_url}")

            # The product's identity digest covers both surfaces it was built from, and is
            # keyed under the `product:` namespace rather than under its page URL — that URL
            # is also a *resource* key carrying the page's own body digest, and letting the
            # two share a key makes every product look changed on every crawl for ever.
            composite = composite_hash(entry_hash, page_hash)
            product_id = self._product_id(request.store_id, entry)
            known = request.known_hashes.get(f"product:{product_id}")
            # NOT `record`: that name is the resource-recording closure above, and shadowing
            # it here made every product-page fetch raise `'ProductRecord' object is not
            # callable` — measured across 21 tests.
            product_record, published_images = self._to_record(
                request.store_id,
                entry,
                jsonld,
                product_url or page_url,
                composite,
                changed=has_changed(known, composite),
            )
            products.append(product_record)
            if published_images > len(product_record.images):
                truncated_galleries += 1

        if skipped_product_pages:
            # One warning for the crawl, not one per product — the same reason the gallery
            # warning below is aggregated. It names the count because that is the operator's
            # question: these products are in the graph, and this many of them were built
            # from `products.json` alone.
            warnings.append(
                f"budget: {budget_refusal}; fetched no product page for "
                f"{skipped_product_pages} product(s) — the budget refused a request and the "
                f"crawl stopped fetching, so those products carry their catalog entry "
                f"without the JSON-LD their page states"
            )

        if truncated_galleries:
            # Aggregated, not per product: 88 of the 3,093 recorded products publish more
            # than the limit, and 88 warnings would bury the ones that mean something.
            warnings.append(
                f"{truncated_galleries} product(s) published more than "
                f"{MEDIA_PER_PRODUCT_LIMIT} images; kept the first {MEDIA_PER_PRODUCT_LIMIT} "
                f"by catalogue position"
            )
        return products

    @staticmethod
    def _product_id(store_id: str, entry: Mapping[str, Any]) -> str:
        """The graph's stable ID for a storefront product.

        Derived from the store and the store's own identifier for the product, so the same
        product keeps the same node across crawls (and a re-crawl of unchanged content
        cannot churn the graph through ID drift).
        """
        return product_id_for(store_id, entry)

    def _to_record(
        self,
        store_id: str,
        entry: Mapping[str, Any],
        jsonld: Mapping[str, Any],
        source_url: str,
        digest: str,
        *,
        changed: bool,
    ) -> tuple[ProductRecord, int]:
        """Merge one ``products.json`` entry with its page's JSON-LD into one record.

        ``products.json`` wins on anything it states; JSON-LD fills the gaps (brand,
        currency, availability) that the machine endpoint does not carry.

        "States" is decided by :func:`~ingest.adapters.mapping.price_is_stated` for the price,
        not by whether the value survived coercion: a variant whose ``products.json`` price is
        hostile keeps no price, rather than inheriting the page's. Otherwise a store could
        choose which of its surfaces prices the product by making the other unusable.

        Returns:
            ``(record, images_published)`` — the second is how many images the catalogue
            listed, before :data:`~ingest.adapters.mapping.MEDIA_PER_PRODUCT_LIMIT` bounded
            them, so the caller can report a truncated gallery instead of hiding it.
        """
        native = native_product_key(entry)
        product_id = self._product_id(store_id, entry)
        brand = str(entry.get("vendor") or "").strip() or self._brand(jsonld)
        title = str(entry.get("title") or jsonld.get("name") or "").strip()
        ld_offers = self._offers(jsonld)
        default_currency = next(
            (o.get("priceCurrency") for o in ld_offers if o.get("priceCurrency")), "USD"
        )
        default_availability = next(
            (o.get("availability") for o in ld_offers if o.get("availability")), None
        )

        by_sku = {str(o.get("sku")): o for o in ld_offers if o.get("sku")}
        variants: list[VariantRecord] = []
        for item in entry.get("variants") or []:
            if not isinstance(item, dict):
                continue
            sku = str(item.get("sku") or "").strip()
            matched = by_sku.get(sku, {})
            native_variant = str(item.get("id") or sku or item.get("title") or "").strip()
            # `products.json` wins on anything it STATES, and a refused statement is still a
            # statement. `coerce_price` answers None both for "no price here" and for
            # "-5.00" / NaN / true / unreadable text, and reading the second as the first
            # would hand the store the choice of which of its two surfaces prices the
            # product: write a hostile number in the machine endpoint and the theme-authored
            # JSON-LD takes over. So the JSON-LD offer fills a GAP only.
            stated = item.get("price")
            price = coerce_price(stated)
            if price is None and not price_is_stated(stated):
                price = coerce_price(matched.get("price"))
            available = item.get("available")
            availability = (
                coerce_availability(available)
                if available is not None
                else coerce_availability(matched.get("availability") or default_availability)
            )
            variants.append(
                VariantRecord(
                    variant_id=variant_id_for(store_id, native, native_variant),
                    seller_sku=sku,
                    name=str(item.get("title") or "").strip(),
                    price=price,
                    currency=str(matched.get("priceCurrency") or default_currency or "USD"),
                    availability=availability,
                    status="active",
                )
            )

        categories = tuple(c for c in [str(entry.get("product_type") or "").strip()] if c)
        images, published = image_records(entry)
        return ProductRecord(
            product_id=product_id,
            canonical_name=title,
            brand=brand,
            status=str(entry.get("status") or "active").strip() or "active",
            handle=str(entry.get("handle") or "").strip(),
            source_url=source_url,
            content_hash=digest,
            changed=changed,
            variants=tuple(variants),
            categories=categories,
            images=images,
        ), published

    # -- JSON-LD ---------------------------------------------------------------------------

    @staticmethod
    def _blocks(html: str) -> Iterable[Any]:
        """Every parsed ``application/ld+json`` block in a page, bad ones skipped."""
        try:
            from bs4 import BeautifulSoup

            try:
                soup = BeautifulSoup(html, "lxml")
            except Exception:
                soup = BeautifulSoup(html, "html.parser")
            scripts = [
                tag.string or tag.get_text()
                for tag in soup.find_all("script")
                if str(tag.get("type", "")).strip().lower() == "application/ld+json"
            ]
        except Exception:
            scripts = re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html,
                re.DOTALL | re.IGNORECASE,
            )
        for raw in scripts:
            if not raw or not raw.strip():
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                continue

    def _json_ld_product(self, html: str, warnings: list[str], url: str) -> dict[str, Any]:
        """The first schema.org ``Product`` on a page, flattened out of ``@graph`` if needed."""
        for block in self._blocks(html):
            for node in self._flatten(block):
                types = node.get("@type")
                types = types if isinstance(types, list) else [types]
                if any(str(t).lower() == "product" for t in types if t):
                    return node
        if html.strip():
            warnings.append(f"{url}: no schema.org Product in JSON-LD")
        return {}

    @staticmethod
    def _flatten(block: Any) -> Iterable[dict[str, Any]]:
        stack = [block]
        while stack:
            node = stack.pop(0)
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                graph = node.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)

    @staticmethod
    def _brand(jsonld: Mapping[str, Any]) -> str:
        brand = jsonld.get("brand")
        if isinstance(brand, dict):
            return str(brand.get("name") or "").strip()
        return str(brand or "").strip()

    @staticmethod
    def _offers(jsonld: Mapping[str, Any]) -> list[dict[str, Any]]:
        offers = jsonld.get("offers")
        if isinstance(offers, dict):
            if str(offers.get("@type", "")).lower() == "aggregateoffer":
                nested = offers.get("offers")
                if isinstance(nested, list):
                    return [o for o in nested if isinstance(o, dict)]
            return [offers]
        if isinstance(offers, list):
            return [o for o in offers if isinstance(o, dict)]
        return []


def _satisfies_catalog_adapter() -> bool:
    """Structural check that this really is the interface both adapters share (C6).

    Asserted at runtime by ``test_signed_fetch.py`` rather than trusted: a protocol nobody
    verifies against is a comment.
    """
    return isinstance(SignedFetchAdapter(), _CatalogAdapter)
