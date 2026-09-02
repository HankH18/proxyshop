"""``install(shop, admin_client)`` — what actually happens when a merchant installs the app.

R6 says a merchant onboarding registers *that store's outcome-observation surface*. Under
the Shopify adapter that is four Admin GraphQL mutations and one stored credential:

1. the shop's **offline** access token is stored against the shop (:mod:`.tokens`), which
   is what lets everything after the install run with no staff session present;
2. ``webPixelCreate`` installs the web pixel extension with the settings it needs to find
   this app's collector — C5's only checkout-observation path;
3. ``webhookSubscriptionCreate`` subscribes ``orders/paid``, ``orders/fulfilled`` and
   ``refunds/create`` — and nothing else, which :func:`~.webhooks.assert_topics_allowed`
   enforces before the first request goes out.

The Admin client is **injected**, never constructed here. That is what makes one function
serve the production path (an :class:`~.admin.AdminGraphQLClient` pointed at the shop),
every offline run (the same client pointed at ``services/shopify-stub``), and the frozen
acceptance suite (a recording double), with no branch anywhere that asks which it is.

Reading the responses is deliberately defensive. A client double may answer with anything,
and an install that crashed on an unexpected reply shape would fail for a reason that has
nothing to do with whether the shop got its pixel — so every field read is guarded, and the
only thing that *stops* an install is a `userErrors` entry that is not idempotent.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from merchant_svc.install.admin import WEB_PIXEL_CREATE, WEBHOOK_SUBSCRIPTION_CREATE
from merchant_svc.install.config import web_pixel_settings, webhook_callback_url
from merchant_svc.install.scopes import REQUIRED_SCOPES, assert_scopes_allowed
from merchant_svc.install.shop import normalize_shop_domain
from merchant_svc.install.tokens import TOKENS, OfflineToken, OfflineTokenStore
from merchant_svc.install.webhooks import (
    TOPIC_ENUM,
    WEBHOOK_TOPICS,
    assert_topics_allowed,
)

#: ``userErrors`` messages that mean "this was already done", not "this failed". Re-running
#: an install must be safe: Shopify refuses a duplicate ``(topic, address)`` subscription
#: rather than creating a second one that would double-deliver every order.
IDEMPOTENT_USER_ERRORS: tuple[str, ...] = ("has already been taken",)


class InstallFailed(RuntimeError):
    """A mutation came back with ``userErrors`` the install cannot treat as already-done."""

    def __init__(self, operation: str, errors: Any) -> None:
        self.operation = operation
        self.errors = errors
        super().__init__(f"{operation} returned userErrors: {errors!r}")


@dataclass(frozen=True)
class WebhookRegistration:
    """One registered (or already-registered) webhook subscription."""

    topic: str
    callback_url: str
    subscription_id: str | None = None
    already_registered: bool = False


@dataclass(frozen=True)
class InstallResult:
    """What an install left behind, in the order it was done."""

    shop_domain: str
    scopes: tuple[str, ...]
    pixel_settings: dict[str, Any]
    web_pixel_id: str | None = None
    webhooks: tuple[WebhookRegistration, ...] = ()
    offline_token: OfflineToken | None = None
    app_url: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def topics(self) -> tuple[str, ...]:
        """The topics this install subscribed."""
        return tuple(registration.topic for registration in self.webhooks)

    @property
    def offline_token_stored(self) -> bool:
        """Whether this install put an offline token on file for the shop."""
        return self.offline_token is not None


def _executor(admin_client: Any) -> Callable[..., Any]:
    """The callable that runs one GraphQL operation on ``admin_client``.

    ``execute`` when the client has one, otherwise the client itself. Nothing here probes
    for a menu of optional capabilities: a client double answers *every* attribute lookup,
    so a ``hasattr`` chain would silently pick whichever name came first in the chain
    rather than the one the client really implements.
    """
    execute = getattr(admin_client, "execute", None)
    if callable(execute):
        return execute
    if callable(admin_client):
        return admin_client
    raise TypeError(
        "the admin client must expose execute(document, variables) or be callable; got "
        f"{type(admin_client).__name__}"
    )


def _payload(response: Any, root_field: str) -> Mapping[str, Any]:
    """The mutation payload inside a response, whatever envelope it arrived in.

    Accepts both a full GraphQL envelope (``{"data": {...}}``, which is what a raw client
    returns) and an already-unwrapped ``data`` block (what
    :meth:`~.admin.AdminGraphQLClient.execute` returns).
    """
    node: Any = response
    if isinstance(node, Mapping) and "data" in node:
        node = node["data"]
    if not isinstance(node, Mapping):
        return {}
    payload = node.get(root_field)
    return payload if isinstance(payload, Mapping) else {}


def _blocking_user_errors(payload: Mapping[str, Any]) -> list[Any]:
    """``userErrors`` minus the ones that mean the resource already exists."""
    errors = payload.get("userErrors")
    if not isinstance(errors, Iterable) or isinstance(errors, str | bytes | Mapping):
        return []
    blocking = []
    for error in errors:
        message = ""
        if isinstance(error, Mapping):
            message = str(error.get("message", ""))
        else:
            message = str(error)
        if any(marker in message.lower() for marker in IDEMPOTENT_USER_ERRORS):
            continue
        blocking.append(error)
    return blocking


def _already_registered(payload: Mapping[str, Any]) -> bool:
    """Whether the only ``userErrors`` say the subscription already exists."""
    errors = payload.get("userErrors")
    if not isinstance(errors, Iterable) or isinstance(errors, str | bytes | Mapping):
        return False
    for error in errors:
        message = str(error.get("message", "")) if isinstance(error, Mapping) else str(error)
        if any(marker in message.lower() for marker in IDEMPOTENT_USER_ERRORS):
            return True
    return False


def _text(node: Any, *keys: str) -> str | None:
    """The first string found at ``node[key]``, or ``None``. Never raises."""
    if not isinstance(node, Mapping):
        return None
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def install(
    shop_domain: str,
    admin_client: Any,
    *,
    app_url: str | None = None,
    collector: str | None = None,
    access_token: str | None = None,
    tokens: OfflineTokenStore | None = None,
    scopes: Iterable[str] = REQUIRED_SCOPES,
    topics: Iterable[str] = WEBHOOK_TOPICS,
    now: datetime | None = None,
) -> InstallResult:
    """Register this shop's outcome-observation surface (R6/C5).

    Args:
        shop_domain: the ``<name>.myshopify.com`` host being installed on.
        admin_client: anything exposing ``execute(document, variables)``, or callable with
            that signature.
        app_url: this app's public origin. Every URL this install hands Shopify is
            built from it — the three webhook callback URLs *and* the pixel's
            ``collectorUrl``, which must agree or the shop reports its checkouts to a
            different host than its orders. Defaults to ``MERCHANT_APP_URL``.
        collector: the pixel's ``collectorUrl`` in full; wins over ``app_url`` when
            both are given.
        access_token: the shop's offline token. When given it is stored against the shop,
            which is the step that makes the install survive the staff session that started
            it. When omitted, an already-stored token is reported instead.
        tokens: the token store; defaults to the process-wide :data:`~.tokens.TOKENS`.
        scopes: the granted scope list, checked against C5.
        topics: the webhook topics to subscribe, checked against C5.
        now: the instant recorded on a stored token.

    Returns:
        :class:`InstallResult`.

    Raises:
        InvalidShopDomain: ``shop_domain`` is not a bare ``.myshopify.com`` host.
        ProtectedScopeRequested: a protected-customer-data scope was granted/requested.
        ForbiddenWebhookTopic: a topic outside the three C5 allows was asked for.
        InstallFailed: a mutation returned blocking ``userErrors``.
        AdminAPIError: the Admin API refused the call (e.g. a bad access token).
    """
    shop = normalize_shop_domain(shop_domain)
    granted = assert_scopes_allowed(scopes)
    wanted_topics = assert_topics_allowed(topics)
    store = tokens if tokens is not None else TOKENS
    execute = _executor(admin_client)

    stored: OfflineToken | None
    if access_token:
        stored = store.save(shop, access_token, scopes=granted, now=now)
    else:
        stored = store.get(shop)

    settings = web_pixel_settings(shop, collector=collector, app_url=app_url)
    pixel_response = execute(WEB_PIXEL_CREATE, {"webPixel": {"settings": settings}})
    pixel_payload = _payload(pixel_response, "webPixelCreate")
    blocking = _blocking_user_errors(pixel_payload)
    if blocking:
        raise InstallFailed("webPixelCreate", blocking)
    web_pixel_id = _text(pixel_payload.get("webPixel"), "id")

    registrations: list[WebhookRegistration] = []
    for topic in wanted_topics:
        callback = webhook_callback_url(topic, app_url)
        response = execute(
            WEBHOOK_SUBSCRIPTION_CREATE,
            {
                "topic": TOPIC_ENUM[topic],
                "webhookSubscription": {"uri": callback, "format": "JSON"},
            },
        )
        payload = _payload(response, "webhookSubscriptionCreate")
        blocking = _blocking_user_errors(payload)
        if blocking:
            raise InstallFailed(f"webhookSubscriptionCreate({topic})", blocking)
        registrations.append(
            WebhookRegistration(
                topic=topic,
                callback_url=callback,
                subscription_id=_text(payload.get("webhookSubscription"), "id"),
                already_registered=_already_registered(payload),
            )
        )

    warnings: list[str] = []
    if stored is None:
        warnings.append(
            f"no offline access token is on file for {shop}; the pixel and the webhooks "
            f"are registered but nothing can call the Admin API for this shop afterwards"
        )

    return InstallResult(
        shop_domain=shop,
        scopes=granted,
        pixel_settings=settings,
        web_pixel_id=web_pixel_id,
        webhooks=tuple(registrations),
        offline_token=stored,
        app_url=(app_url or "").rstrip("/"),
        warnings=tuple(warnings),
    )
