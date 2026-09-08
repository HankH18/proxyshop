"""Where a post-purchase order reference comes from, and why it cannot come from anywhere else.

THE DEFECT THIS CLOSES
----------------------
R14's prompt is *post-purchase*. The shopper journey ended at the checkout handoff: the buyer
accepted a slot, was handed the exchange's permalink, and left holding ``auction_id``,
``bid_ref`` and a URL. **None of those is an order.** So the only feedback prompt the product
could show was a fixture, and so was the input to the whole learning loop.

The obvious repair — mint an "order reference" at accept and hand it over — is the one that
must not be made. A code minted *before* a purchase is not evidence a purchase happened; a
reference issued at accept would be issued identically to a shopper who closed the tab. R14's
clause is a NEGATIVE guarantee ("only network-routed buyers"), and a reference that exists for
every accept grades exactly as many stores as it should refuse.

WHERE AN ORDER REFERENCE HONESTLY ORIGINATES
--------------------------------------------
At the merchant's signed webhook, and nowhere earlier. Measured through the tree:

1. Shopify POSTs ``orders/paid`` to ``apps/merchant/svc/src/install/routes.py``. The body is
   verified with HMAC-SHA256 over the raw bytes against ``SHOPIFY_API_SECRET``
   (``install/webhooks.py``: ``sign``/``verify``, constant-time compare) — an unsigned or
   mis-signed delivery is a 401 and reaches no ledger.
2. ``ReceivedWebhook.order_ref`` reads ``admin_graphql_api_order_id`` first, then
   ``admin_graphql_api_id`` *only when it carries the ``gid://shopify/Order/`` prefix* (a
   ``refunds/create`` body's is a Refund gid and joined to nothing), then the numeric id. That
   value — ``gid://shopify/Order/5500000000001`` — is the first moment an order reference
   exists anywhere in this system.
3. ``merchant_svc.composition.ledger_payload`` projects it to trust's ``POST /events`` as
   ``kind: "order_paid"``, carrying ``discount_codes: [{"code": "PSX-…"}]``.
4. ``trust.reconcile.engine.reconcile`` joins that webhook to the exchange's ``accepted``
   promise. It cannot join on a checkout token — the exchange mints its own with
   ``secrets.token_hex(16)`` and transmits it nowhere, while the store mints a different one
   when the cart is visited — so it bridges on the single-use ``PSX-`` discount code, the one
   value that genuinely crossed the wire, namespaced into its own key space and scoped by
   store. ``trust.reconcile.routes.resolve_store_aliases`` is what lets the exchange's
   ``store-a`` and the merchant's ``store-a.myshopify.com`` meet at all.
5. ``reconcile`` emits nothing for an order it cannot pair with an ``accepted`` event
   (``if accepted is None: continue``). **That is the whole gate, already built and already
   authoritative.** A ``reconciled`` record therefore IS the network's own statement that it
   routed this buyer to this store and this order became of it.

So the order reference is obtained LATER, from the network, and the buyer holds nothing at
accept but the handle it already had.

WHAT THE BUYER HOLDS, AND WHY IT IS ENOUGH
------------------------------------------
``POST /buyer/shortlist/accept`` already publishes ``auction_id`` and ``bid_ref`` — both
required fields of the response the buyer already gets today. Nothing new is minted at accept
and no contract on that path changes.

``bid_ref`` is the join, and it is the network's value on both sides:

* the exchange mints it as ``ranking.candidates.mint_bid_id(auction_id, store_id)`` —
  literally ``f"{auction_id}:{store_id}"`` — and stamps it into the ``accepted`` event's
  published body (``contracts.LEDGER_PAYLOAD_SHAPES["accepted"] == ("bid_ref",
  "checkout_token", "offer")``);
* ``reconcile`` carries it onto the ``reconciled`` payload, read off *that same* ``accepted``
  record — the one that supplied the promise being graded.

Because the mint is ``auction_id`` + ``":"`` + ``store_id``, the auction is recoverable from
the network's own record (:func:`auction_of`) rather than from anything the caller said. That
matters more than it looks: ``routing()`` refuses an order that names no auction, and an
auction id taken from the request body would be the caller supplying the evidence for the gate
that is supposed to judge them.

WHAT THIS MODULE IS NOT
-----------------------
**It is not a second decider, and the distinction is the point.**
``buyer_svc.feedback.routing.routing`` decides — once — whether R14 permits a prompt and a
write. ``packages/contracts/src/openapi.py`` names a second decider as the failure to avoid,
and ``apps/buyer/app/feedback/feedback.ts`` names the same one from the client side: two
places that decide who may leave feedback disagree the first time the rule changes.

This module decides nothing. It answers one factual question — *does the network's own
reconciled record name an order for this checkout?* — and hands back an order record for
``routing()`` to judge, or ``None``. Every refusal in this file is a refusal to ANSWER, never a
refusal to admit; the ``routed`` flag it sets is transcribed from the existence of a
``reconciled`` record, and no other field of the answer is invented.

**It reads no reference for its provenance, and must never start.**
Which references are earned and which were manufactured elsewhere is settled outside this
service, on the ``order_ref`` value itself, and is sealed by
``trust.ledger.canonical.compute_event_hash`` when ``submit_feedback`` copies that field onto
the ``LedgerEvent``. This package's own rule is that it cannot tell one caller from another —
there is a test that fails the build for a served feedback module that learns to, and it
scans this very file — and a prefix test here would be exactly that: a second, softer copy of
a distinction the chain already holds, and one somebody can argue about.

What this module contributes to that separation is structural rather than lexical, and it
contributes it without inspecting a single character: a reference the network cannot pair
with an ``accepted`` promise and an HMAC-verified ``order_paid`` gets no ``reconciled``
record, so the network vouches for nothing, and the ``routed`` flag it declines to set is the
one R14's gate reads. Vouched and unvouched are therefore different by evidence, and both
facts stay readable from the ledger rather than from a string comparison performed here.

THE RESIDUALS, STATED
---------------------
* ``GET /reconcile`` serialises at most :data:`MAX_VERDICT_PAGE` verdicts per read. Past that
  bound this answers "the network does not vouch for this order" for an order it might have
  vouched for one page further on. That is the fail-CLOSED direction — a missed prompt, never
  an admitted one — and it is why the bound is a named constant rather than a literal.
* A trust service that is down, slow or unparseable is likewise "no answer", not an
  exception: :meth:`TrustReconciledOrders.order_for` never raises. A feedback prompt that
  500s because an audit service hiccuped would be a worse product than one that says it has
  nothing to ask about yet.
* This reads the fold, not the appended verdicts, so it is correct before anybody has run
  ``POST /reconcile``. ``GET /reconcile`` is the published door for exactly that — "what the
  chain says about every purchase in it. Appends nothing."
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from typing import Any

from ._reading import read, text

_log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LOOKUP_TIMEOUT_SECONDS",
    "MAX_VERDICT_PAGE",
    "RECONCILED_KIND",
    "RECONCILE_PATH",
    "ROUTED_ORDERS_ATTR",
    "TrustReconciledOrders",
    "auction_of",
    "find_routed_order",
    "order_from_reconciled",
    "routed_orders_for",
]

#: The ledger kind ``trust.reconcile.engine`` emits, one per paid order it could pair with an
#: offer. Spelled here rather than imported for the reason :mod:`buyer_svc.feedback.submission`
#: spells its three trust constants: the buyer service does not depend on the trust service's
#: package, it reaches it over HTTP.
RECONCILED_KIND = "reconciled"

#: Trust's read-only fold. ``GET`` appends nothing to the chain — the ``POST`` twin is what
#: lands verdicts — so asking whether an order reconciled can never itself write history.
RECONCILE_PATH = "/reconcile"

#: The ceiling ``trust.reconcile.routes`` puts on one page of serialised verdicts
#: (``MAX_VERDICT_PAGE``). Asked for, so one read sees as much as the door will show.
MAX_VERDICT_PAGE = 2000

#: How long one lookup may hold the request thread. A prompt the shopper is waiting on must not
#: hang because an audit service is thinking; a trust service that has not answered in this long
#: is down, and "down" is an absent vouching rather than an error.
DEFAULT_LOOKUP_TIMEOUT_SECONDS = 1.0

#: Where a composition root may put its own resolver. Anything exposing
#: ``order_for(*, order_ref, auction_id, bid_ref)`` will do — a test's double, a cache, a
#: reader for a deployment whose trust service lives somewhere unusual.
ROUTED_ORDERS_ATTR = "routed_orders"

#: The environment variable naming the trust service. Same spelling
#: :mod:`proxyshop_support.trust_ledger` writes through, so one variable names the service
#: everywhere in this deployment.
_ENV_TRUST_URL = "TRUST_URL"

#: Cached on the app beside the resolver so a deployment with no trust service pays for one
#: failed build rather than one per request.
_RESOLVED_ATTR = "routed_orders_resolved"


def auction_of(bid_ref: Any, store_id: Any) -> str:
    """The auction ``bid_ref`` was minted for, or ``""`` when it does not say.

    ``ranking.candidates.mint_bid_id`` is ``f"{auction_id}:{store_id}"``, so the store the
    reconciled record names is stripped off the end rather than guessed at: splitting on the
    FIRST colon would truncate a ``gid://``-shaped auction id, and splitting on the last one
    would keep a colon that belonged to the store. The store-suffix reading is exact whenever
    the two sides agree, and ``rpartition`` is the honest fallback for a ``bid_ref`` minted by
    something other than this exchange.

    ``""`` when there is no separator at all, and that is deliberately not a guess:
    :func:`~buyer_svc.feedback.routing.routing` refuses an order naming no auction, so an
    unreadable ``bid_ref`` costs a prompt rather than producing a trust observation attributed
    to nothing.
    """
    reference = text(bid_ref)
    store = text(store_id)
    suffix = f":{store}"
    if store and reference.endswith(suffix) and len(reference) > len(suffix):
        return reference[: -len(suffix)]
    head, separator, _ = reference.rpartition(":")
    return head if separator else ""


def order_from_reconciled(event: Any) -> dict[str, Any] | None:
    """One ``reconciled`` ledger event as the order record :mod:`.routing` reads, or ``None``.

    ``None`` for anything that is not a reconciled record naming both an order and the bid it
    grades. The three fields that must be present are the three the gate needs, and every one
    of them is the NETWORK's:

    * ``order_ref`` — the merchant's ``gid://shopify/Order/…``, from a body Shopify signed;
    * ``store_id`` — the platform's store, after ``resolve_store_aliases`` has reconciled the
      merchant's ``.myshopify.com`` spelling with the exchange's;
    * ``bid_ref`` — read by ``reconcile`` off the exchange's own ``accepted`` event, which is
      the promise being graded.

    ``routed`` is ``True`` and it is a transcription, not a judgement:
    ``trust.reconcile.engine.reconcile`` emits nothing for an order it could not pair with an
    ``accepted`` event, so the existence of this record IS the statement that the network
    routed this buyer here. Nothing else in the answer is invented — there is no ``status``,
    because a ``reconciled`` record does not carry a lifecycle, and inventing ``"delivered"``
    would be this module deciding the very thing ``routing()`` is there to decide.
    """
    if not isinstance(event, Mapping):
        return None
    if text(read(event, "kind")) != RECONCILED_KIND:
        return None
    payload = read(event, "payload")
    payload = payload if isinstance(payload, Mapping) else {}

    order_ref = text(read(event, "order_ref")) or text(read(payload, "order_ref"))
    store_id = text(read(event, "store_id")) or text(read(payload, "store_id"))
    bid_ref = text(read(payload, "bid_ref"))
    auction_id = auction_of(bid_ref, store_id)
    if not order_ref or not bid_ref or not auction_id:
        return None
    return {
        "order_ref": order_ref,
        "store_id": store_id,
        "auction_id": auction_id,
        "bid_ref": bid_ref,
        "routed": True,
    }


def find_routed_order(
    events: Iterable[Any],
    *,
    order_ref: str = "",
    auction_id: str = "",
    bid_ref: str = "",
) -> dict[str, Any] | None:
    """The one reconciled order matching every hint given, or ``None``.

    Every non-empty hint must match — ``and``, never ``or``. A lookup that returned the first
    record matching *any* hint would let a caller who knows one real order reference collect
    the prompt for a different auction by naming both, which is the shape of the ownership hole
    ``FeedbackNotYours`` exists for.

    A hint that is empty is simply not applied, so a caller holding only its accept handle and
    a caller holding only a reference both get an answer, and a caller holding both gets the
    stricter one.

    **Ambiguity is refused rather than resolved.** Two reconciled records matching the same
    hints means the fold saw two orders this buyer's checkout could be, and picking one would
    be attributing a trust observation to a coin toss. ``None`` costs a prompt; the wrong
    answer costs a store its score.
    """
    wanted_order = text(order_ref)
    wanted_auction = text(auction_id)
    wanted_bid = text(bid_ref)
    if not (wanted_order or wanted_auction or wanted_bid):
        return None

    found: dict[str, Any] | None = None
    for event in events:
        candidate = order_from_reconciled(event)
        if candidate is None:
            continue
        if wanted_order and candidate["order_ref"] != wanted_order:
            continue
        if wanted_auction and candidate["auction_id"] != wanted_auction:
            continue
        if wanted_bid and candidate["bid_ref"] != wanted_bid:
            continue
        if found is not None and found != candidate:
            _log.warning(
                "two reconciled orders match one feedback lookup (auction=%s); refusing to "
                "choose between them",
                candidate["auction_id"],
            )
            return None
        found = candidate
    return found


class TrustReconciledOrders:
    """Reads trust's published ``GET /reconcile`` and answers what it says about one checkout.

    Not a ledger sink and not shaped like one: this only ever reads, and the door it reads is
    the one whose published summary is "what the chain says about every purchase in it.
    Appends nothing." A resolver that could write would be a feedback path that could
    manufacture the evidence for its own gate.

    ``httpx`` is imported on first use, following the convention
    :class:`proxyshop_support.trust_ledger.TrustLedgerPublisher` already sets, so composing a
    buyer service opens no sockets and a test that never looks an order up pays for neither.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_LOOKUP_TIMEOUT_SECONDS,
        limit: int = MAX_VERDICT_PAGE,
    ) -> None:
        self._base_url = str(base_url).rstrip("/")
        self._timeout = float(timeout)
        self._limit = int(limit)
        self._client: Any = None

    @property
    def url(self) -> str:
        """The exact door this resolver reads. Named so a wiring log line can say it."""
        return f"{self._base_url}{RECONCILE_PATH}"

    def reconciled(self) -> list[Any]:
        """Every reconciled verdict the fold serialises, or ``[]`` when it cannot be read.

        **Never raises.** A trust service that is down, slow, or answering something other than
        the published body is an absent vouching — the fail-closed direction — and turning it
        into a 500 would make the shopper's prompt fail for a reason the shopper cannot act on.
        The failure is logged with its type and message and never with a body.
        """
        try:
            client = self._http_client()
            response = client.get(self.url, params={"limit": self._limit})
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - see the docstring; an outage is not an error
            _log.warning(
                "could not read %s, so no order can be vouched for right now (%s: %s)",
                self.url,
                type(exc).__name__,
                exc,
            )
            return []
        events = body.get("events") if isinstance(body, Mapping) else None
        return list(events) if isinstance(events, list) else []

    def order_for(
        self, *, order_ref: str = "", auction_id: str = "", bid_ref: str = ""
    ) -> dict[str, Any] | None:
        """The network's own order record for this checkout, or ``None``. Never raises."""
        return find_routed_order(
            self.reconciled(),
            order_ref=order_ref,
            auction_id=auction_id,
            bid_ref=bid_ref,
        )

    def _http_client(self) -> Any:
        if self._client is None:
            import httpx  # noqa: PLC0415 - deferred so composing opens no sockets

            self._client = httpx.Client(timeout=self._timeout)
        return self._client


def routed_orders_for(app: Any, env: Mapping[str, str] | None = None) -> Any | None:
    """The resolver this app should ask, or ``None`` when it has no network to ask.

    Resolution order, and the last step is the one worth stating:

    1. whatever a composition root put on ``app.state.routed_orders`` — a test's double, a
       deployment's own reader — which is never replaced;
    2. a :class:`TrustReconciledOrders` built from ``TRUST_URL`` when the environment names
       one, cached on the app so a lookup costs one client rather than one per request;
    3. **``None``**, and deliberately not a default address.

    Step 3 is the difference between this and
    :func:`proxyshop_support.trust_ledger.trust_endpoint`, which falls back to
    ``http://trust:8084`` because a service that has to be told where to write its audit trail
    is a service that ships not writing one. The trade is the other way around for a READ on a
    request path: an unset ``TRUST_URL`` means nobody has connected this buyer service to a
    trust service, and inventing a compose hostname would spend a shopper's request on a DNS
    failure to reach a service this deployment never had. No network to ask is not an error and
    not a refusal — it is simply no vouching, which leaves every caller exactly where it was
    before this module existed.
    """
    existing = getattr(app.state, ROUTED_ORDERS_ATTR, None)
    if existing is not None:
        return existing
    if getattr(app.state, _RESOLVED_ATTR, False):
        return None
    environ = os.environ if env is None else env
    base_url = str(environ.get(_ENV_TRUST_URL) or "").strip()
    setattr(app.state, _RESOLVED_ATTR, True)
    if not base_url:
        return None
    resolver = TrustReconciledOrders(base_url)
    setattr(app.state, ROUTED_ORDERS_ATTR, resolver)
    _log.info("buyer feedback will ask %s whether an order was network-routed", resolver.url)
    return resolver
