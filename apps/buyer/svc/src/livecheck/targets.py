"""Which live page, if any, this slot's pitch may be checked against — and who decided.

The URL is the whole security surface of this feature, so it is decided here, once, and
nowhere else. Two ports supply it and **neither of them is the store**:

:class:`RegisteredDomains`
    ``store_id -> the domain the PLATFORM registered for that store``. Already the shape
    ``exchange.checkout.sellers`` uses for exactly this reason: on a bid, ``store_domain`` is
    *a field the store wrote*, so a store that supplies both the URL and the domain it is
    checked against has supplied both halves of its own check. The default,
    :class:`NoRegisteredDomains`, knows nobody and therefore refuses everybody.

:class:`ProductPages`
    ``(store_id, product_ref) -> the page the platform observed that product on``. The
    default, :class:`NoProductPages`, holds no page for anybody.

A store MAY put a URL on its bid — see :func:`target_for`'s ``page_url`` — and it buys the
store nothing except the chance to be refused: the URL is accepted only when its host is the
registered domain (or a proper subdomain of it), its scheme is https, it carries no userinfo
and no non-standard port. **A seller pointing the platform at an arbitrary host is the
attack**, and it is the same attack ``exchange/checkout/domain.py`` was written for one
surface over.

Three measured corrections to the brief this module was built from, stated loudly because
each one changed the design
---------------------------------------------------------------------------------------
**1. No product-page URL arrives on a bid today.** The brief said "It arrives on a bid".
Measured on this branch: ``contracts.Offer`` carries ``checkout_url`` and nothing else, and
``ShortlistSlot.product`` is ``{product_ref, variant_ref}`` under ``additionalProperties:
false`` — so there is no field a URL could arrive in, and ``packages/contracts`` is another
lane's. :func:`target_for` therefore READS one if a slot carries it (forward-compatible, and
validated exactly as if a hostile store had sent it) and otherwise asks :class:`ProductPages`.

**2. A URL cannot be derived from a ``product_ref``.** The obvious construction —
``https://{registered domain}/products/{product_ref}`` — does not work here, because
``ingest.adapters.mapping.product_id_for`` mints ``prod_<stable hash>`` rather than the
store's handle. Measured: every ``product_ref`` in this tree is opaque. Deriving a path from
it would produce a 404 for every store on every check, which under "absence is not guilt"
is a silent no-verdict — a feature that looks wired and decides nothing, forever. So the
page must be *known*, not *guessed*, and :class:`NoProductPages` refusing is the honest
default.

**3. The store's own ``store_domain`` on the slot is not evidence.** It is carried through to
the buyer for display and it is store-written; it is never consulted here. If the platform's
registry and the slot disagree, the registry wins and the slot's value is not even read.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..accept._reading import read, text

__all__ = [
    "ALLOWED_PAGE_SCHEMES",
    "MAX_PAGE_URL_LENGTH",
    "LiveCheckTarget",
    "NoProductPages",
    "NoRegisteredDomains",
    "StaticProductPages",
    "StaticRegisteredDomains",
    "TargetRefused",
    "domain_matches",
    "normalise_domain",
    "product_pages",
    "registered_domains",
    "set_product_pages",
    "set_registered_domains",
    "target_for",
    "targets_for",
    "usable_page_url",
]

#: ``https`` only, and this is stricter than the checkout guard's list on purpose. A checkout
#: permalink is a URL the BUYER is sent to and a human can inspect; this is a URL the PLATFORM
#: fetches unattended, so a plaintext hop that a network-position attacker can rewrite into
#: whatever page they like would let them mint a contradiction against any store they can see
#: traffic for. There is no legitimate storefront in 2026 that cannot serve https.
ALLOWED_PAGE_SCHEMES: frozenset[str] = frozenset({"https"})

#: The most characters a page URL may have. A bound on a value a store may choose.
MAX_PAGE_URL_LENGTH = 2048


class NoRegisteredDomains:
    """The fail-closed default: the platform has registered nobody, so nobody is checkable.

    The same posture — and deliberately the same class name — as
    ``exchange.checkout.sellers.NoRegisteredDomains``. It is a separate class rather than an
    import because ``apps/buyer/Dockerfile``'s COPY set does not ship ``apps/exchange``, so
    importing it would resolve in a checkout and be absent in the image; and because a buyer
    service reaching into the exchange's Python would turn an HTTP seam into a code one, which
    ``buyer_svc.accept._reading`` already declines to do for the same reason.
    """

    def domain_for(self, store_id: str) -> str | None:
        return None

    __call__ = domain_for


class StaticRegisteredDomains:
    """A ``store_id -> registered domain`` table the PLATFORM wrote, not the store."""

    def __init__(self, domains: Mapping[str, str] | None = None) -> None:
        self._domains = {
            str(key): normalise_domain(value) for key, value in (domains or {}).items()
        }

    def domain_for(self, store_id: str) -> str | None:
        return self._domains.get(str(store_id)) or None

    __call__ = domain_for

    def __len__(self) -> int:
        return len(self._domains)


class NoProductPages:
    """The fail-closed default: the platform knows no product page, so it fetches none."""

    def page_url_for(
        self, store_id: str, product_ref: str, variant_ref: str | None = None
    ) -> str | None:
        return None


class StaticProductPages:
    """A ``(store_id, product_ref) -> page url`` table, from the deployment document.

    **This is an interim and it is the same interim the roster is**, so it carries the same
    warning ``buyer_svc.composition`` writes over that one: a table of product pages typed
    into a deployment document is a person transcribing the catalogue. It goes stale silently,
    and a stale ENTRY here is worse than a stale row in the roster because this one is the
    evidence a store gets penalised against.

    The non-interim answer is the crawled graph: ``ingest.graph.model``'s ``Source`` node
    carries the ``url`` a product was observed at, which is the platform's own record of where
    it read this product — no transcription, no drift, and it is the same seam as pointing the
    claim verifier at the crawled catalogue instead of at a deployment document. See this
    package's ``__init__`` for what that costs.

    Keys are accepted in two spellings, ``"store/product"`` and a nested mapping, because a
    deployment document is written by a person and one of those shapes is always the one they
    reached for.
    """

    def __init__(self, pages: Mapping[str, Any] | None = None) -> None:
        flat: dict[tuple[str, str], str] = {}
        for key, value in (pages or {}).items():
            if isinstance(value, Mapping):
                for product_ref, url in value.items():
                    flat[(str(key), str(product_ref))] = str(url)
                continue
            store_id, _, product_ref = str(key).partition("/")
            if product_ref:
                flat[(store_id, product_ref)] = str(value)
        self._pages = flat

    def page_url_for(
        self, store_id: str, product_ref: str, variant_ref: str | None = None
    ) -> str | None:
        return self._pages.get((str(store_id), str(product_ref)))

    def __len__(self) -> int:
        return len(self._pages)


def normalise_domain(value: Any) -> str:
    """A registered domain as a bare lower-case host: no scheme, no port, no trailing dot.

    An operator writes ``https://store.example.com/`` as often as ``store.example.com``, and a
    registry entry that failed to match because of a scheme would fail *open* in the sense
    that matters here — it would refuse every check for a store that is correctly registered,
    and a silent refusal is indistinguishable from a store nobody registered.
    """
    raw = text(value).lower()
    if not raw:
        return ""
    if "//" in raw:
        raw = raw.split("//", 1)[1]
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    raw = raw.rpartition("@")[2]
    if raw.startswith("["):  # bracketed IPv6 literal
        raw = raw.partition("]")[0].lstrip("[")
    else:
        raw = raw.split(":", 1)[0]
    return raw.strip().rstrip(".")


def domain_matches(host: Any, registered: Any) -> bool:
    """Exact host, or a **proper** subdomain of the registered domain. Nothing else.

    Written out rather than expressed as ``in`` / ``startswith`` / ``endswith`` because every
    one of those admits a spoof — ``store.example.com.attacker.tld`` starts with the allowed
    host and ``evilstore.example.com`` ends with it. This is
    ``ingest.adapters.netguard.host_matches_allowlist``'s rule, deliberately restated for one
    host rather than imported: that module is not in this image's COPY set (see this package's
    ``__init__`` for the lift this repository should do instead), and a guard that resolves in
    a checkout and is absent in the image is not a guard.
    """
    target = normalise_domain(host)
    allowed = normalise_domain(registered)
    if not target or not allowed:
        return False
    return target == allowed or target.endswith("." + allowed)


def usable_page_url(url: Any, registered_domain: Any) -> tuple[str, str]:
    """``(url, "")`` when this URL may be fetched, else ``("", reason it may not)``.

    Every refusal reason is returned rather than raised, because a refused URL is a target
    this check declines to make — a no-verdict — and never an error the shopper's path sees.

    The rules, and each one closes a real shape of attack rather than a style preference:

    * ``https`` only (:data:`ALLOWED_PAGE_SCHEMES`). ``file:``, ``gopher:``, ``data:`` and
      ``http:`` are all refused; the last of those is refused because a plaintext fetch is
      rewritable by anyone on the path, who could then mint a contradiction against a store.
    * no userinfo. ``https://real-store.example.com@attacker.tld/`` *looks* like the
      registered domain to a reader and connects to ``attacker.tld``.
    * default port only. A registered domain is a storefront, and ``:8080`` on it is somebody
      addressing something else on that host.
    * the host must be the registered domain or a proper subdomain of it
      (:func:`domain_matches`).
    * bounded length, and a parse failure is a refusal rather than a traceback —
      ``urlsplit("https://[")`` raises, and this value is a store's.
    """
    raw = text(url)
    if not raw:
        return "", "no product page url"
    if len(raw) > MAX_PAGE_URL_LENGTH:
        return (
            "",
            f"the product page url is {len(raw)} characters, over the {MAX_PAGE_URL_LENGTH} allowed",
        )
    registered = normalise_domain(registered_domain)
    if not registered:
        return "", "the platform holds no registered domain for this store"
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        return "", f"the product page url does not parse ({exc})"
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_PAGE_SCHEMES:
        return "", (
            f"the product page url states scheme {scheme or '(none)'!r}; only "
            f"{sorted(ALLOWED_PAGE_SCHEMES)} may be fetched unattended"
        )
    if "@" in (parts.netloc or ""):
        return "", "the product page url carries userinfo, which hides where it really connects"
    try:
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        return "", f"the product page url has an unreadable authority ({exc})"
    if not host:
        return "", "the product page url names no host"
    if port is not None and port != 443:
        return "", f"the product page url names port {port}; a storefront is served on 443"
    if not domain_matches(host, registered):
        return "", (
            f"the product page url is on {normalise_domain(host)!r}, which is not the domain "
            f"the platform registered for this store ({registered!r})"
        )
    return raw, ""


@dataclass(frozen=True, slots=True)
class LiveCheckTarget:
    """One page to check, and everything the check needs, resolved on the shopper's path.

    Deliberately a value object with no client, no session and no open socket: it is built
    while a shopper waits and consumed long after they have gone, so anything live on it
    would be a resource held across that gap.
    """

    store_id: str
    product_ref: str
    url: str
    auction_id: str = ""
    bid_ref: str = ""
    slot: str = ""
    asking_price: float | None = None
    currency: str | None = None
    #: The bid's own structured commitments, as the exchange published them.
    claims: tuple[Mapping[str, Any], ...] = ()
    #: The store's own pitch, byte for byte, decomposed at DRAIN time rather than here — see
    #: :mod:`buyer_svc.livecheck.deferred`, which explains why the regular-expression scan is
    #: kept off the shopper's path and why the live page's own vocabulary makes it better
    #: there. Deliberately absent from :meth:`to_dict`: a seller's prose is carried so it can
    #: be graded, not so a platform surface can re-serve it.
    pitch_text: str = ""
    #: Whether the auction served a live offer for this product at all.
    offered: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "product_ref": self.product_ref,
            "url": self.url,
            "auction_id": self.auction_id,
            "bid_ref": self.bid_ref,
            "slot": self.slot,
            "asking_price": self.asking_price,
            "currency": self.currency,
            "claims": [dict(claim) for claim in self.claims],
            "offered": self.offered,
        }


@dataclass(frozen=True, slots=True)
class TargetRefused:
    """No page will be checked for this slot, and the sentence saying why.

    Kept rather than dropped, and served on the record, because "we did not check" and "we
    checked and it was fine" are different answers and a screen that showed them the same way
    would be claiming evidence it does not have.
    """

    store_id: str
    product_ref: str
    reason: str
    slot: str = ""
    auction_id: str = ""
    bid_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "product_ref": self.product_ref,
            "slot": self.slot,
            "auction_id": self.auction_id,
            "bid_ref": self.bid_ref,
            "reason": self.reason,
        }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def target_for(
    slot: Any,
    *,
    auction_id: str = "",
    registered_domains: Any = None,
    product_pages: Any = None,
    claims: tuple[Mapping[str, Any], ...] = (),
) -> LiveCheckTarget | TargetRefused:
    """Resolve one shortlist slot to a page to check, or to a refusal naming why.

    The slot is a raw bid-shaped ``dict`` from across a seam, so every field is read through
    :func:`buyer_svc.accept._reading.read` and nothing here can raise on a malformed one.

    Order of resolution, and it is not arbitrary: the store's own ``product_url`` is tried
    FIRST, so that a store which names a page gets checked against the page it named — and is
    refused loudly when that page is not on its registered domain, which is the one attack
    worth catching. The platform's own :class:`ProductPages` entry is the fallback.
    """
    store_id = text(read(slot, "store_id", "")) or text(read(slot, "store", ""))
    product = read(slot, "product", None)
    product_ref = text(read(product, "product_ref", "")) if product is not None else ""
    variant_ref = text(read(product, "variant_ref", "")) if product is not None else ""
    slot_name = text(read(slot, "slot", ""))
    bid_ref = text(read(slot, "bid_ref", ""))
    refuse = lambda why: TargetRefused(  # noqa: E731 - one shape, six call sites
        store_id=store_id,
        product_ref=product_ref,
        reason=why,
        slot=slot_name,
        auction_id=auction_id,
        bid_ref=bid_ref,
    )

    if not store_id:
        return refuse("the slot names no store, so no registered domain can be looked up")
    if not product_ref:
        return refuse("the slot names no product, so there is no product page to check")

    registry = registered_domains if registered_domains is not None else NoRegisteredDomains()
    lookup = getattr(registry, "domain_for", registry)
    try:
        registered = lookup(store_id)
    except Exception:  # noqa: BLE001 - a registry is a collaborator, not a trusted callee
        return refuse("the registered-domain registry could not be consulted for this store")
    registered = normalise_domain(registered)
    if not registered:
        return refuse(
            "the platform holds no registered domain for this store, so there is no host it "
            "is allowed to fetch a page from"
        )

    candidate = text(read(slot, "product_url", "")) or (
        text(read(product, "url", "")) if product is not None else ""
    )
    source = "the store's own bid"
    if not candidate:
        pages: Any = product_pages if product_pages is not None else NoProductPages()
        finder: Any = getattr(pages, "page_url_for", pages)
        try:
            candidate = text(finder(store_id, product_ref, variant_ref or None))
        except Exception:  # noqa: BLE001 - as above
            candidate = ""
        source = "the platform's product-page table"
    if not candidate:
        return refuse(
            "neither the bid nor the platform's product-page table names a live page for "
            "this product, so there is nothing to check it against"
        )

    url, why = usable_page_url(candidate, registered)
    if not url:
        return refuse(f"{why} (url from {source})")

    price = read(slot, "price", None)
    return LiveCheckTarget(
        store_id=store_id,
        product_ref=product_ref,
        url=url,
        auction_id=auction_id or text(read(slot, "auction_id", "")),
        bid_ref=bid_ref,
        slot=slot_name,
        asking_price=_finite(read(price, "unit_price", None)) if price is not None else None,
        currency=(text(read(price, "currency", "")) or None) if price is not None else None,
        claims=tuple(claims),
        offered=price is not None,
    )


def targets_for(
    rows: Any,
    *,
    auction_id: str = "",
    registered_domains: Any = None,
    product_pages: Any = None,
    claims_by_index: Mapping[int, tuple[Mapping[str, Any], ...]] | None = None,
) -> tuple[list[LiveCheckTarget], list[TargetRefused]]:
    """Resolve every slot of one shortlist. Pure, bounded by the number of slots, no I/O."""
    targets: list[LiveCheckTarget] = []
    refusals: list[TargetRefused] = []
    if rows is None or isinstance(rows, (str, bytes)):
        return targets, refusals
    try:
        entries = list(rows)
    except TypeError:
        return targets, refusals
    for index, row in enumerate(entries):
        resolved = target_for(
            row,
            auction_id=auction_id,
            registered_domains=registered_domains,
            product_pages=product_pages,
            claims=(claims_by_index or {}).get(index, ()),
        )
        if isinstance(resolved, LiveCheckTarget):
            targets.append(resolved)
        else:
            refusals.append(resolved)
    return targets, refusals


# ==============================================================================================
# The two module-level seams
# ==============================================================================================
# These are MODULE-level rather than `app.state` for the reason `buyer_svc.pitch.writer` gives
# at length: `POST /buyer/shortlist/render` takes no `Request`, deliberately, because
# `buyer_svc.accept.routes` promises that handler *cannot reach* an exchange client — R2's
# structural guarantee that looking at a shortlist cannot become accepting one. A registry
# reached through `request.app.state` would put `app.state`, and therefore that client, back
# inside the handler's scope. Both default to their fail-closed implementations, so a process
# nobody has configured resolves no target and checks no page.

_REGISTRY_LOCK = threading.Lock()
_DOMAINS: Any = None
_PAGES: Any = None
_RESOLVED = False


def _resolve_from_deployment() -> None:
    """Build both registries from the deployment document, once, on first use.

    **This is why the feature is reachable at all**, and the reason is a measured one about
    THIS route rather than a general preference. ``buyer_svc.composition.configure_buyer``
    binds both registries — but it runs from a request-time hook, and ``POST
    /buyer/shortlist/render`` is the ONE route that deliberately does not take that hook
    (``accept/routes.py``: R2's guarantee is that the handler cannot reach an exchange
    client, and a hook needs a ``Request``, which holds ``app.state``, which holds the
    client). So a process whose only traffic is renders would enqueue nothing, forever, with
    a correctly-configured document sitting right there — a feature that looks wired and
    decides nothing, which is the exact failure this repository keeps finding.

    So the resolution is lazy and module-level, the same shape
    :func:`buyer_svc.pitch.writer.pitch_writer` uses to build a model client on a route with
    no ``app.state``. The ``composition`` import is INSIDE the function because that module
    imports this one at module scope; deferring it is what keeps the cycle from existing.

    Failure is silent in the direction that costs nothing: a malformed document leaves both
    registries fail-closed and logs, because a shortlist must never fail over an optional
    check, and a document this bad is already a 503 on every other route.
    """
    global _DOMAINS, _PAGES, _RESOLVED
    _RESOLVED = True
    try:  # noqa: PLC0415 - deferred to break the composition <-> targets import cycle
        from ..composition import read_deployment

        deployment = read_deployment()
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logging.getLogger(__name__).info(
            "live-page check: no usable deployment document to read a registered-domain "
            "table from (%s); no page will be checked",
            exc,
        )
        return
    if deployment is None:
        return
    if deployment.registered_domains and _DOMAINS is None:
        _DOMAINS = StaticRegisteredDomains(dict(deployment.registered_domains))
    if deployment.product_pages and _PAGES is None:
        _PAGES = StaticProductPages(dict(deployment.product_pages))


def registered_domains() -> Any:
    """The platform's ``store_id -> registered domain`` registry for this process."""
    with _REGISTRY_LOCK:
        if _DOMAINS is None and not _RESOLVED:
            _resolve_from_deployment()
        return _DOMAINS if _DOMAINS is not None else NoRegisteredDomains()


def set_registered_domains(registry: Any) -> None:
    """Install the registry (``None`` resets, so the next read re-reads the document)."""
    global _DOMAINS, _RESOLVED
    with _REGISTRY_LOCK:
        _DOMAINS = registry
        _RESOLVED = False


def product_pages() -> Any:
    """The platform's ``(store_id, product_ref) -> page url`` table for this process."""
    with _REGISTRY_LOCK:
        if _PAGES is None and not _RESOLVED:
            _resolve_from_deployment()
        return _PAGES if _PAGES is not None else NoProductPages()


def set_product_pages(pages: Any) -> None:
    """Install the table (``None`` resets, so the next read re-reads the document)."""
    global _PAGES, _RESOLVED
    with _REGISTRY_LOCK:
        _PAGES = pages
        _RESOLVED = False
