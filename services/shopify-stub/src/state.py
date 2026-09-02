"""The stub's in-memory world: configuration, carts, orders, codes, and the event logs.

Everything the stub knows lives in one :class:`StubState` instance held by the ASGI app.
Two consequences worth stating, because both are deliberate:

* **No datastore.** The stub is started per test on an ephemeral port (D41) and each test
  owns its own state. Reaching for Postgres or Redis here would drag the stub into the
  compose stack and into the per-worker isolation rules (D39/D40) for no benefit.
* **No global singleton beyond the module-level ``app``.** ``create_app()`` builds a fresh
  state; the module-level ``app`` in :mod:`shopify_stub.app` is one instance of that, which
  is what the root ``shopify_stub_url`` fixture serves.

Money is :class:`~decimal.Decimal` in here and a fixed 2-place string on the wire, because
that is what Shopify sends: ``"total_price": "42.00"``, never ``42.0``. A float here would
reintroduce the rounding bug the string format exists to avoid.
"""

from __future__ import annotations

import itertools
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from shopify_stub.codes import DiscountCode, RejectionReason
from shopify_stub.permalink import _assert_bare_host

#: What the stub calls itself when nothing overrides it.
DEFAULT_SHOP_DOMAIN = "proxyshop-demo.myshopify.com"

#: The Admin API version the stub answers on: the current stable version at the time the
#: recorded fixtures were authored, and the value echoed in every webhook's
#: ``X-Shopify-API-Version`` header and in ``webPixelCreate``'s ``apiVersion.handle``.
#:
#: It is also the **only** version segment ``/admin/api/{version}/graphql.json`` answers on.
#: A request naming any other one — a retired version, a future one, a typo — is refused
#: with **404**, the status real Shopify uses for a version outside its published set. This
#: comment used to say "callers may use any version segment in the URL", which was true of
#: the route that captured ``{version}`` and discarded it and has not been true since
#: ``shopify_stub.app.admin_graphql`` started gating on it; ``2025-01`` answered ``200``
#: with real data then and answers ``404`` now. Held to the code by
#: ``test_stub_contract.test_the_documented_version_refusal_is_the_one_the_route_gives``.
DEFAULT_API_VERSION = "2026-07"

#: Fake Admin API access token. Requests must present it in ``X-Shopify-Access-Token``.
DEFAULT_ACCESS_TOKEN = "shpat_stubtoken000000000000000000"

#: Fake app client secret, used as the webhook HMAC key.
DEFAULT_WEBHOOK_SECRET = "shpss_stubsecret0000000000000000"

MONEY_QUANTUM = Decimal("0.01")


def money(value: Decimal | str | int) -> str:
    """Format a money amount the way Shopify does: a 2-place decimal **string**."""
    amount = Decimal(value) if not isinstance(value, Decimal) else value
    return str(amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP))


class PixelMode(StrEnum):
    """Whether the web pixel fires at all.

    ``ON``
        The pixel is installed and firing; individual events may still be lost to
        :attr:`StubConfig.pixel_drop_rate`.
    ``OFF``
        The **documented non-firing mode**. The pixel is not installed, is blocked by a
        content blocker, or consent was denied — so *no* event is ever emitted, whatever
        the drop rate says. This is not the same condition as a 100% drop rate and the
        stub keeps them distinguishable (:class:`SuppressionReason`), because "the pixel is
        lossy" and "the pixel is absent" call for different reconciliation behaviour in
        T-061.
    ``PARTIAL``
        The pixel fires but the beacon leaves before the order id and the discount
        allocation are known, so it reports ``orderId: null`` and
        ``discountApplications: null``. This is the second documented non-firing case: the
        event exists, the *join keys* do not. It is modelled because the frozen collector
        contract already describes exactly this degraded event
        (``.swarm-loop/acceptance/test_e5_merchant.py:481-484`` builds it by nulling those
        two members of the clean event) and requires the collector to record a visible gap
        rather than crash.
    """

    ON = "on"
    OFF = "off"
    PARTIAL = "partial"


class SuppressionReason(StrEnum):
    """Why a pixel event that *should* have described a checkout does not exist."""

    #: Lost to the configured drop rate — the pixel is installed and firing.
    DROPPED = "dropped"
    #: The pixel is not firing at all (:attr:`PixelMode.OFF`).
    NOT_FIRING = "not_firing"


class WebhookTopic(StrEnum):
    """The three topics this system subscribes to. Nothing else is modelled."""

    ORDERS_PAID = "orders/paid"
    ORDERS_FULFILLED = "orders/fulfilled"
    REFUNDS_CREATE = "refunds/create"


@dataclass
class StubConfig:
    """Runtime knobs. Mutated through the ``/_stub/config`` control-plane route.

    Attributes:
        shop_domain: the host the stub claims to be; also the host the permalink route
            validates against.
        api_version: Admin API version echoed in webhook headers.
        access_token: the token Admin API calls must present.
        webhook_secret: HMAC key for webhook signatures.
        pixel_drop_rate: probability in ``[0.0, 1.0]`` that an individual pixel event is
            dropped. ``0.0`` (the default) means lossless.
        pixel_mode: see :class:`PixelMode`.
        pixel_seed: seed for the drop-rate RNG. Set it and the drop pattern is exactly
            reproducible, which is what lets a test assert an exact count instead of a
            statistical band.
        has_active_automatic_discount: whether the shop has an order-level **automatic**
            discount running. This is the condition that makes a code *conflicting* rather
            than merely invalid: a code whose ``combinesWith.orderDiscounts`` is ``false``
            cannot be applied on top of one, and Shopify drops it rather than erroring.

            The stub models this discount's **combinability effect only**, not its money.
            Nothing in this system reads an automatic discount's value — the offer's price
            is what it compares against — and modelling the allocation would mean inventing
            a second ``discount_applications`` entry whose shape no consumer needs. That
            narrowing is deliberate and is stated here rather than left to be discovered.
    """

    shop_domain: str = DEFAULT_SHOP_DOMAIN
    api_version: str = DEFAULT_API_VERSION
    access_token: str = DEFAULT_ACCESS_TOKEN
    webhook_secret: str = DEFAULT_WEBHOOK_SECRET
    pixel_drop_rate: float = 0.0
    pixel_mode: PixelMode = PixelMode.ON
    pixel_seed: int | None = None
    has_active_automatic_discount: bool = False

    def __post_init__(self) -> None:
        """Refuse a configuration that cannot be *served* — not merely one that looks odd.

        ``shop_domain`` is interpolated into four live ``https://`` URLs (the cart route's
        303 ``Location``, ``order_status_url`` on the order webhook, the pixel event's
        ``document.location.href``, and :func:`~shopify_stub.permalink.build_permalink`), so
        the set of legal values here is exactly the set
        :func:`~shopify_stub.permalink._assert_bare_host` allows and nothing wider. The check
        this replaces tested ``if not self.shop_domain`` — non-emptiness — which admitted
        ``good.example.com@attacker.tld`` and made the stub answer a real 303 to
        ``attacker.tld``.

        Validating in ``__post_init__`` rather than only in :meth:`validate` matters because
        it makes an invalid :class:`StubConfig` **unconstructable**: a caller that builds one
        directly, or a future control-plane route that forgets to call
        :meth:`validate`, cannot get a bad domain into a running stub.
        """
        self.validate()

    def validate(self) -> None:
        """Raise :class:`ValueError` on a nonsensical configuration.

        Called again by ``PUT /_stub/config`` after it has mutated a *candidate* copy —
        ``dataclasses.replace`` runs ``__post_init__`` on the copy while it still holds the
        old (valid) domain, so the post-init check alone would not see the incoming one.

        :class:`~shopify_stub.permalink.PermalinkError` is a :class:`ValueError`, so the
        control plane's ``except ValueError`` turns a refused domain into the same 400 every
        other bad knob gets.
        """
        if not 0.0 <= self.pixel_drop_rate <= 1.0:
            raise ValueError(f"pixel_drop_rate must be in [0,1], got {self.pixel_drop_rate}")
        _assert_bare_host(self.shop_domain)


@dataclass
class Variant:
    """A product variant in the stub's catalog.

    Cart permalinks are **variant-scoped** (D25), and D25 also rules that this stub exposes
    no product→variant query. So a variant is something a seed puts here by id, and the
    permalink route resolves by id; there is deliberately no lookup by product, handle, SKU
    or title.
    """

    variant_id: int
    product_id: int
    title: str
    price: Decimal
    currency: str = "USD"
    sku: str = ""
    available: bool = True


@dataclass
class WebPixel:
    """A pixel installed through ``webPixelCreate``."""

    id: int
    settings: dict[str, object]
    created_at: datetime


@dataclass
class LineItem:
    """One line on an order."""

    variant_id: int
    product_id: int
    title: str
    quantity: int
    price: Decimal
    sku: str = ""

    @property
    def line_total(self) -> Decimal:
        return self.price * self.quantity


@dataclass
class Checkout:
    """A cart permalink that has been visited but not yet completed.

    Attributes:
        token: Shopify's ``checkout_token`` — the pixel↔webhook join key.
        client_id: the web-pixel client id for this browsing session. Real Shopify does not
            put this on the order webhook, so the stub propagates it the way a real app
            would: as a ``note_attributes`` entry (see :mod:`shopify_stub.orders`).
        applied_code: the code that was actually applied, or ``None``.
        rejection: why the requested code was not applied. ``None`` when a code applied or
            when none was requested. Recorded for *test observability only* — the shopper
            never sees it (acceptance criterion 2: silent no-op).
    """

    token: str
    client_id: str
    variant_id: int
    quantity: int
    requested_code: str | None
    applied_code: str | None
    rejection: RejectionReason | None
    created_at: datetime
    completed: bool = False
    order_id: int | None = None


@dataclass
class Fulfillment:
    """One fulfillment on an order."""

    id: int
    order_id: int
    status: str
    created_at: datetime
    tracking_company: str | None = None
    tracking_number: str | None = None


@dataclass
class Refund:
    """One refund on an order."""

    id: int
    order_id: int
    created_at: datetime
    amount: Decimal
    currency: str
    note: str | None = None


@dataclass
class Order:
    """An order in the stub's ledger."""

    id: int
    order_number: int
    checkout_token: str
    cart_token: str
    client_id: str
    created_at: datetime
    processed_at: datetime
    updated_at: datetime
    currency: str
    line_items: list[LineItem]
    discount_code: str | None
    discount_amount: Decimal
    financial_status: str = "paid"
    fulfillment_status: str | None = None
    fulfillments: list[Fulfillment] = field(default_factory=list)
    refunds: list[Refund] = field(default_factory=list)
    test: bool = True

    # There is deliberately **no customer field on this model.** SPEC C5 says the app asks
    # for no protected-customer-data scopes, and the frozen collector contract rejects
    # `email`, `phone`, `first_name`, `last_name`, `address`, `customer_id` and `customerId`
    # outright (`.swarm-loop/acceptance/test_e5_merchant.py:444-452`). A stub that carries
    # PII it is entitled to omit invites a consumer to read it, and that consumer then
    # breaks against the real store where the field is absent. Every payload builder emits
    # `"customer": null`, which is what a real guest checkout sends.

    @property
    def name(self) -> str:
        """Shopify's human order name, e.g. ``"#1001"``."""
        return f"#{self.order_number}"

    @property
    def subtotal_price(self) -> Decimal:
        return sum((item.line_total for item in self.line_items), Decimal("0"))

    @property
    def total_price(self) -> Decimal:
        total = self.subtotal_price - self.discount_amount
        return total if total > 0 else Decimal("0")

    @property
    def total_refunded(self) -> Decimal:
        return sum((refund.amount for refund in self.refunds), Decimal("0"))


@dataclass
class WebhookSubscription:
    """A registered webhook endpoint."""

    id: int
    topic: WebhookTopic
    callback_url: str
    created_at: datetime


@dataclass
class WebhookDelivery:
    """The record of one **delivery**, kept whether or not it succeeded.

    Acceptance criterion 3 is "webhooks always delivered" — so a delivery the receiver
    rejects is *retried*, and the outcome lands here either way. A test asserting delivery
    reads this log, not the receiver, so a receiver bug cannot be mistaken for a stub bug.

    One record per ``(dispatch, subscription)`` pair, **not** one per attempt. This
    docstring used to say "the record of one delivery attempt … every attempt lands here",
    which reads as a row per attempt and is not what
    :meth:`~shopify_stub.webhooks.WebhookDispatcher.dispatch` writes: it retries inside one
    record and appends it once, after the loop. Three attempts produce one row with
    ``attempts == 3``, which is why ``attempts`` is an ``int`` and not a list. Pinned by
    ``test_stub_webhooks.test_the_delivery_log_holds_one_row_per_delivery_not_per_attempt``.

    Attributes:
        attempts: how many POSTs were made, 1..``MAX_ATTEMPTS``.
        delivered: whether the last attempt was accepted (2xx).
        status_code: the **last** attempt's status, or ``None`` when the last attempt
            raised before a response (a connection error, a timeout).
        error: the last attempt's failure, or ``None`` when it was accepted. An earlier
            failure that a later attempt recovered from leaves no trace here beyond
            ``attempts`` being greater than one.
    """

    id: int
    topic: WebhookTopic
    callback_url: str
    payload: dict[str, object]
    webhook_id: str
    hmac: str
    attempts: int
    delivered: bool
    status_code: int | None
    error: str | None
    sent_at: datetime


@dataclass
class PixelEvent:
    """One emitted web-pixel event (the Web Pixels API shape, camelCase)."""

    id: str
    name: str
    timestamp: datetime
    client_id: str
    payload: dict[str, object]


@dataclass
class SuppressedEvent:
    """One pixel event that was *not* emitted, and why.

    This log has no counterpart in real Shopify — a dropped beacon leaves no trace anywhere,
    which is precisely the problem T-061 exists to solve. The stub keeps it so a test can
    prove the loss was the configured loss rather than a bug in the emitter.
    """

    checkout_token: str
    client_id: str
    reason: SuppressionReason
    at: datetime


class StubState:
    """Everything the stub knows. One instance per app.

    Ids are allocated from monotone counters seeded at realistic Shopify magnitudes so a
    consumer that (wrongly) assumes 32-bit ids fails here rather than in production.
    """

    def __init__(self, config: StubConfig | None = None) -> None:
        self.config = config or StubConfig()
        self.config.validate()
        self.codes: dict[str, DiscountCode] = {}
        """Keyed by the code string, upper-cased."""
        self.codes_by_offer: dict[str, list[str]] = {}
        """D22: codes are *stored keyed by offer_id*. Maps ``offer_id`` -> **every** code
        minted for it, upper-cased, in creation order.

        A list rather than a single code because the previous single-code map was
        last-write-wins: a second mint for one offer silently dropped the first code from the
        index while it stayed live in :attr:`codes`. See :meth:`store_code`."""
        self.variants: dict[int, Variant] = {}
        self.checkouts: dict[str, Checkout] = {}
        self.orders: dict[int, Order] = {}
        self.subscriptions: dict[int, WebhookSubscription] = {}
        self.web_pixels: dict[int, WebPixel] = {}
        self.pixel_collector_url: str | None = None
        """Where an emitted pixel event is POSTed, set by ``webPixelCreate`` settings."""
        self.deliveries: list[WebhookDelivery] = []
        self.pixel_events: list[PixelEvent] = []
        self.suppressed_events: list[SuppressedEvent] = []
        self._discount_ids = itertools.count(1_100_000_000_001)
        self._order_ids = itertools.count(5_500_000_000_001)
        self._order_numbers = itertools.count(1001)
        self._subscription_ids = itertools.count(9_900_000_000_001)
        self._web_pixel_ids = itertools.count(3_300_000_000_001)
        self._fulfillment_ids = itertools.count(7_700_000_000_001)
        self._refund_ids = itertools.count(8_800_000_000_001)
        self._delivery_ids = itertools.count(1)
        self._random = secrets.SystemRandom()

    # -- id allocation ------------------------------------------------------------------

    def next_discount_id(self) -> int:
        return next(self._discount_ids)

    def next_order_id(self) -> int:
        return next(self._order_ids)

    def next_order_number(self) -> int:
        return next(self._order_numbers)

    def next_subscription_id(self) -> int:
        return next(self._subscription_ids)

    def next_web_pixel_id(self) -> int:
        return next(self._web_pixel_ids)

    def next_fulfillment_id(self) -> int:
        return next(self._fulfillment_ids)

    def next_refund_id(self) -> int:
        return next(self._refund_ids)

    def next_delivery_id(self) -> int:
        return next(self._delivery_ids)

    # -- lookups ------------------------------------------------------------------------

    def find_code(self, candidate: str | None) -> DiscountCode | None:
        """Case-insensitive lookup, matching Shopify's redemption behaviour."""
        if not candidate:
            return None
        return self.codes.get(candidate.strip().upper())

    def store_code(self, discount: DiscountCode) -> None:
        """Store a code, and index it by ``offer_id`` when it carries one (D22).

        Two things this used to get wrong, both in one line
        (``self.codes_by_offer[offer_id] = discount.code``):

        **The key was not normalised.** ``self.codes`` is keyed by ``code.upper()`` and the
        index stored the code exactly as created, so a lowercase-created code left the two
        structures disagreeing outright — ``codes`` holding ``PSX-LOWER01`` while the index
        held ``psx-lower01``. A consumer doing the obvious ``codes[by_offer[offer_id]]``
        against ``/_stub/codes`` got a ``KeyError``. Shopify accepts a lowercase code
        (``discountCodeBasicCreate`` does not police case), so this is reachable, not
        theoretical.

        **It was last-write-wins.** Minting twice for one offer silently dropped the first
        code from the index while leaving it live and redeemable in ``codes`` — the index
        said one thing and the redemption table said another. The index is now a list, in
        creation order, and appends.

        A duplicate mint is deliberately **not** an error here. The stub exists to mirror
        Shopify and make non-conformance *visible*; Shopify has no per-offer index at all, so
        there is no platform rule to enforce. Whether an offer may hold two live codes is
        T-052's business, and the list is what lets T-052's tests see that it happened.
        """
        self.codes[discount.code.upper()] = discount
        if discount.offer_id:
            minted = self.codes_by_offer.setdefault(discount.offer_id, [])
            normalised = discount.code.upper()
            if normalised not in minted:
                minted.append(normalised)

    def subscriptions_for(self, topic: WebhookTopic) -> list[WebhookSubscription]:
        return [s for s in self.subscriptions.values() if s.topic == topic]

    def reset(self, config: StubConfig | None = None) -> None:
        """Wipe every collection. Configuration is replaced only when one is supplied."""
        keep = config or self.config
        self.__init__(keep)  # type: ignore[misc]

    def now(self) -> datetime:
        """The stub's clock. Honours ``time_machine`` freezing in tests."""
        return datetime.now(UTC)
