"""T-050 — installing the app wires the pixel and the webhooks, against the real stub.

What the green in this file is evidence **of**, stated up front because a passing suite is
not self-explaining:

* Every "install" test drives :func:`merchant_svc.install.flow.install` through a real
  :class:`~merchant_svc.install.admin.AdminGraphQLClient` over HTTP against a real
  ``services/shopify-stub`` process. The mutations are parsed by the stub's own GraphQL
  parser, authenticated with the stub's access token, and answered from the stub's own
  state. Nothing here asserts against a double of Shopify.
* "Receivable" is proven end to end: the merchant service itself is served on a loopback
  port through the frozen ``merchant_svc.main.create_app``, the install registers *that*
  URL, and the stub then delivers signed ``orders/paid`` / ``orders/fulfilled`` /
  ``refunds/create`` payloads to it, which the app authenticates by HMAC over the raw
  bytes before recording.
* What it is **not** evidence of: genuine Shopify conformance (SPEC A4 — no credential
  exists here), scope enforcement (the stub does not model scopes at all, so C5 compliance
  is asserted against the constant and the guard, not observed), and OAuth token exchange
  (``services/shopify-stub`` implements no ``/admin/oauth/*`` route; the one test that
  covers :func:`~merchant_svc.install.oauth.exchange_code` asserts the *request shape*
  through an ``httpx`` transport double and says so in its own docstring).
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from merchant_svc.install import (
    PROTECTED_CUSTOMER_DATA_SCOPES,
    REQUIRED_SCOPES,
    WEBHOOK_TOPICS,
    AdminAPIError,
    AdminGraphQLClient,
    ForbiddenWebhookTopic,
    InMemoryOfflineTokenStore,
    InvalidShopDomain,
    OAuthCallbackRejected,
    OnlineTokenRefused,
    ProtectedScopeRequested,
    ShopNotInstalled,
    WebhookInbox,
    assert_scopes_allowed,
    assert_topics_allowed,
    authorize_url,
    exchange_code,
    install,
    normalize_shop_domain,
    read_callback,
    sign,
    sign_callback,
    verify,
)
from shopify_stub.state import DEFAULT_ACCESS_TOKEN, DEFAULT_SHOP_DOMAIN, DEFAULT_WEBHOOK_SECRET
from shopify_stub.testing import SEED_VARIANT, RecordingReceiver, StubClient

SHOP = DEFAULT_SHOP_DOMAIN
VARIANT_ID = int(SEED_VARIANT["variant_id"])
Collector = tuple[RecordingReceiver, str]


def _admin(stub_url: str, token: str = DEFAULT_ACCESS_TOKEN) -> AdminGraphQLClient:
    """An Admin client pointed at the running stub."""
    return AdminGraphQLClient(shop_domain=SHOP, access_token=token, base_url=stub_url)


# ======================================================================================
# C5 — the scope list, and the guard that can actually refuse one
# ======================================================================================
def test_required_scopes_are_the_c5_minimum() -> None:
    """``read_orders`` is present, and no protected-customer-data scope is."""
    scopes = {scope.strip().lower() for scope in REQUIRED_SCOPES}
    assert "read_orders" in scopes
    assert not scopes & PROTECTED_CUSTOMER_DATA_SCOPES
    # Every scope in the list is one an operation in this repo needs.
    assert scopes == {"read_orders", "write_discounts", "write_pixels"}


def test_the_scope_guard_refuses_an_input_nothing_else_would(
    install_env: dict[str, str], install_stub_url: str, install_tokens: InMemoryOfflineTokenStore
) -> None:
    """A protected scope is refused at every door a caller can put one through.

    The refusal is proven by consequence, not by the exception alone: after the refused
    install the stub has **no** subscription, so a completed checkout delivers nothing.
    """
    poisoned = [*REQUIRED_SCOPES, "read_customers"]

    with pytest.raises(ProtectedScopeRequested) as refused:
        assert_scopes_allowed(poisoned)
    assert refused.value.offending == ("read_customers",)

    with pytest.raises(ProtectedScopeRequested):
        authorize_url(
            SHOP, client_id="k", redirect_uri="https://app.example/cb", state="s", scopes=poisoned
        )

    with _admin(install_stub_url) as admin:
        with pytest.raises(ProtectedScopeRequested):
            install(
                SHOP,
                admin,
                access_token=DEFAULT_ACCESS_TOKEN,
                tokens=install_tokens,
                scopes=poisoned,
            )


async def test_a_refused_install_leaves_the_shop_unwired(
    install_env: dict[str, str],
    install_stub: StubClient,
    install_stub_url: str,
    install_tokens: InMemoryOfflineTokenStore,
) -> None:
    """The negative control for every guard: nothing was registered, so nothing arrives."""
    with _admin(install_stub_url) as admin:
        with pytest.raises(ProtectedScopeRequested):
            install(SHOP, admin, tokens=install_tokens, scopes=[*REQUIRED_SCOPES, "read_customers"])
        with pytest.raises(ForbiddenWebhookTopic):
            install(SHOP, admin, tokens=install_tokens, topics=["checkouts/create"])

    result = await install_stub.buy(VARIANT_ID)
    assert result["webhook_deliveries"] == []
    assert install_tokens.shops() == ()


def test_only_the_three_c5_topics_can_be_subscribed() -> None:
    """C5 makes the web pixel the only checkout-observation path; webhooks may not add one."""
    assert assert_topics_allowed(WEBHOOK_TOPICS) == WEBHOOK_TOPICS
    assert assert_topics_allowed(["ORDERS_PAID"]) == ("orders/paid",)
    for forbidden in ("checkouts/create", "carts/update", "orders/create", "fulfillments/create"):
        with pytest.raises(ForbiddenWebhookTopic) as refused:
            assert_topics_allowed([*WEBHOOK_TOPICS, forbidden])
        assert refused.value.offending == (forbidden,)


# ======================================================================================
# The shop domain is an API host, so it is validated like one
# ======================================================================================
@pytest.mark.parametrize(
    "malformed",
    [
        "good.myshopify.com@attacker.tld",
        "https://good.myshopify.com",
        "good.myshopify.com:8443",
        "good.myshopify.com/evil",
        "good.myshopify.com?x=1",
        "good.myshopify.com#f",
        "good.myshopify.com evil.tld",
        "good..myshopify.com",
        ".good.myshopify.com",
        "good-.myshopify.com",
        "good.myshopify.com\n",
        "attacker.tld",
        "myshopify.com",
        "",
    ],
)
def test_only_a_bare_myshopify_host_is_installable(malformed: str) -> None:
    with pytest.raises(InvalidShopDomain):
        normalize_shop_domain(malformed)


def test_a_well_formed_shop_is_normalized_not_refused() -> None:
    assert normalize_shop_domain("  Acceptance-Store.MyShopify.com ") == (
        "acceptance-store.myshopify.com"
    )


# ======================================================================================
# The offline token, stored per shop
# ======================================================================================
def test_the_token_store_is_keyed_per_shop() -> None:
    store = InMemoryOfflineTokenStore()
    assert store.get(SHOP) is None
    with pytest.raises(ShopNotInstalled):
        store.require(SHOP)

    store.save(SHOP, "shpat_one", scopes=REQUIRED_SCOPES)
    store.save("other-store.myshopify.com", "shpat_two")
    assert store.require(SHOP).access_token == "shpat_one"
    assert store.require("Other-Store.MyShopify.com").access_token == "shpat_two"
    assert store.shops() == ("other-store.myshopify.com", SHOP)

    assert store.forget(SHOP) is True
    assert store.forget(SHOP) is False
    assert store.get(SHOP) is None


def test_an_online_token_is_refused_because_it_dies_with_the_staff_session() -> None:
    store = InMemoryOfflineTokenStore()
    with pytest.raises(OnlineTokenRefused):
        store.save(SHOP, "shpua_per_user_token")
    with pytest.raises(ValueError):
        store.save(SHOP, "   ")
    assert store.shops() == ()
    # Positive control: the offline spelling of the same credential is stored.
    assert store.save(SHOP, "shpat_offline_token").access_token == "shpat_offline_token"


def test_a_stored_token_is_not_printed_by_its_own_redaction() -> None:
    token = InMemoryOfflineTokenStore().save(SHOP, DEFAULT_ACCESS_TOKEN)
    assert DEFAULT_ACCESS_TOKEN not in token.redacted()


# ======================================================================================
# OAuth: an offline authorize URL, and a callback that is refused unless it is signed
# ======================================================================================
def test_the_authorize_url_asks_for_an_offline_token() -> None:
    """No ``grant_options[]`` — that omission is what makes the token offline."""
    url = authorize_url(
        SHOP,
        client_id="api-key",
        redirect_uri="https://merchant.example/install/callback",
        state="nonce-1",
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == SHOP
    assert parsed.path == "/admin/oauth/authorize"
    query = parse_qs(parsed.query)
    assert query["client_id"] == ["api-key"]
    assert query["state"] == ["nonce-1"]
    assert query["scope"] == [",".join(REQUIRED_SCOPES)]
    assert not any(key.startswith("grant_options") for key in query)


def test_a_tampered_callback_is_refused_and_a_signed_one_is_read() -> None:
    secret = "app-client-secret"
    params = {"shop": SHOP, "code": "auth-code-1", "state": "nonce-1", "timestamp": "1767225600"}
    params["hmac"] = sign_callback(params, secret)

    callback = read_callback(params, secret=secret, expected_state="nonce-1")
    assert callback.shop_domain == SHOP
    assert callback.code == "auth-code-1"

    # One byte of the signed material changed, signature untouched.
    tampered = dict(params, shop="attacker.myshopify.com")
    with pytest.raises(OAuthCallbackRejected):
        read_callback(tampered, secret=secret)

    with pytest.raises(OAuthCallbackRejected):
        read_callback(params, secret=secret, expected_state="a-different-nonce")
    with pytest.raises(OAuthCallbackRejected):
        read_callback({k: v for k, v in params.items() if k != "hmac"}, secret=secret)
    with pytest.raises(OAuthCallbackRejected):
        read_callback(params, secret="the-wrong-secret")

    # A perfectly signed callback naming a host this app may not talk to is still refused.
    hostile = {"shop": "attacker.tld", "code": "c", "state": "nonce-1"}
    hostile["hmac"] = sign_callback(hostile, secret)
    with pytest.raises(OAuthCallbackRejected):
        read_callback(hostile, secret=secret)


def test_exchange_code_posts_the_documented_token_request() -> None:
    """A **request-shaping** test, not a conformance test.

    ``services/shopify-stub`` implements the Admin API, not the authorization server: it
    has no ``/admin/oauth/*`` route, so there is nothing offline to exchange a code
    against. What this pins is the request this app would send — URL, method and body —
    and that a reply without an ``access_token`` is a refusal rather than an empty string.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"access_token": "shpat_exchanged", "scope": "read_orders"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token = exchange_code(
            SHOP, "auth-code-1", client_id="key", client_secret="secret", client=client
        )
    assert token == "shpat_exchanged"
    assert seen["url"] == f"https://{SHOP}/admin/oauth/access_token"
    assert seen["method"] == "POST"
    assert seen["body"] == {"client_id": "key", "client_secret": "secret", "code": "auth-code-1"}

    def tokenless(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scope": "read_orders"})

    with httpx.Client(transport=httpx.MockTransport(tokenless)) as client:
        with pytest.raises(OAuthCallbackRejected):
            exchange_code(SHOP, "c", client_id="k", client_secret="s", client=client)


# ======================================================================================
# Acceptance 1 + 2 — the install runs against the stub and the pixel carries its settings
# ======================================================================================
async def test_install_against_the_stub_registers_the_pixel_and_the_three_webhooks(
    install_env: dict[str, str],
    install_stub: StubClient,
    install_stub_url: str,
    install_app_url: str,
    install_tokens: InMemoryOfflineTokenStore,
) -> None:
    """Acceptance 1: the flow completes against the stub and the offline token is stored."""
    with _admin(install_stub_url) as admin:
        result = install(SHOP, admin, access_token=DEFAULT_ACCESS_TOKEN, tokens=install_tokens)

    # Ids the stub minted — a double would have returned nothing to read here.
    assert result.web_pixel_id is not None
    assert result.web_pixel_id.startswith("gid://shopify/WebPixel/")
    assert result.topics == WEBHOOK_TOPICS
    for registration in result.webhooks:
        assert registration.subscription_id is not None
        assert registration.subscription_id.startswith("gid://shopify/WebhookSubscription/")
        assert registration.callback_url == (
            f"{install_app_url}/webhooks/shopify/{registration.topic}"
        )
        assert registration.already_registered is False

    stored = install_tokens.require(SHOP)
    assert stored.access_token == DEFAULT_ACCESS_TOKEN
    assert stored.scopes == REQUIRED_SCOPES
    assert install_tokens.shops() == (SHOP,)
    assert result.offline_token_stored is True
    assert result.warnings == ()


async def test_the_installed_pixel_beacons_to_the_settings_it_was_created_with(
    install_env: dict[str, str],
    install_stub: StubClient,
    install_stub_url: str,
    install_collector: Collector,
    install_tokens: InMemoryOfflineTokenStore,
) -> None:
    """Acceptance 2: ``webPixelCreate``'s settings payload is what the stub then uses.

    The assertion is not "the mutation was called". It is that the ``collectorUrl`` inside
    the settings object reached the stub, was stored, and is where a completed checkout's
    pixel event actually lands.
    """
    receiver, collector_url = install_collector
    with _admin(install_stub_url) as admin:
        result = install(
            SHOP,
            admin,
            access_token=DEFAULT_ACCESS_TOKEN,
            tokens=install_tokens,
            collector=collector_url,
        )
    assert result.pixel_settings["collectorUrl"] == collector_url
    assert result.pixel_settings["shopDomain"] == SHOP
    assert "email" not in json.dumps(result.pixel_settings).lower()

    completed = await install_stub.buy(VARIANT_ID)
    assert completed["pixel_event_emitted"] is True
    assert completed["pixel_event_posted"] is True

    assert len(receiver.requests) == 1
    beacon = json.loads(receiver.requests[0]["body"])
    assert beacon["checkoutToken"] == completed["checkout_token"]
    assert beacon["clientId"] == completed["client_id"]


async def test_re_running_an_install_is_idempotent_not_a_second_subscription(
    install_env: dict[str, str],
    install_stub: StubClient,
    install_stub_url: str,
    install_tokens: InMemoryOfflineTokenStore,
    install_inbox: WebhookInbox,
) -> None:
    """A merchant who reinstalls must not get every order delivered twice."""
    with _admin(install_stub_url) as admin:
        install(SHOP, admin, access_token=DEFAULT_ACCESS_TOKEN, tokens=install_tokens)
        again = install(SHOP, admin, access_token=DEFAULT_ACCESS_TOKEN, tokens=install_tokens)

    assert [registration.already_registered for registration in again.webhooks] == [True] * 3

    completed = await install_stub.buy(VARIANT_ID)
    paid = [d for d in completed["webhook_deliveries"] if d["topic"] == "orders/paid"]
    assert len(paid) == 1, "the second install created a second subscription"
    assert len(install_inbox.events("orders/paid")) == 1


async def test_a_wrong_offline_token_stops_the_install_at_the_admin_api(
    install_env: dict[str, str],
    install_stub: StubClient,
    install_stub_url: str,
    install_tokens: InMemoryOfflineTokenStore,
) -> None:
    """The stored token is load-bearing: the stub answers 401 without the right one."""
    with _admin(install_stub_url, token="shpat_not_the_right_token") as admin:
        with pytest.raises(AdminAPIError) as refused:
            install(SHOP, admin, access_token="shpat_not_the_right_token", tokens=install_tokens)
    assert refused.value.status_code == 401

    result = await install_stub.buy(VARIANT_ID)
    assert result["webhook_deliveries"] == []


# ======================================================================================
# Acceptance 3 — the registered subscriptions are receivable by the merchant app itself
# ======================================================================================
async def test_the_three_topics_are_delivered_to_the_merchant_app_and_authenticated(
    install_env: dict[str, str],
    install_stub: StubClient,
    install_stub_url: str,
    install_app_url: str,
    install_tokens: InMemoryOfflineTokenStore,
    install_inbox: WebhookInbox,
) -> None:
    """Acceptance 3, end to end through the deployed route table.

    The receiver is ``merchant_svc.main.create_app()`` on a loopback port — the same app
    object the service runs — reached over HTTP by the stub's dispatcher.
    """
    with _admin(install_stub_url) as admin:
        install(SHOP, admin, access_token=DEFAULT_ACCESS_TOKEN, tokens=install_tokens)

    completed = await install_stub.buy(VARIANT_ID)
    order_id = completed["order_id"]
    await install_stub.fulfil(order_id, tracking_number="TRK-1")
    await install_stub.refund(order_id, amount="10.00")

    deliveries = await install_stub.deliveries()
    assert {d["topic"] for d in deliveries} == set(WEBHOOK_TOPICS)
    for delivery in deliveries:
        assert delivery["delivered"] is True, delivery
        assert delivery["status_code"] == 200
        assert delivery["attempts"] == 1
        assert delivery["callback_url"].startswith(install_app_url)

    assert tuple(event.topic for event in install_inbox.events()) == (
        "orders/paid",
        "orders/fulfilled",
        "refunds/create",
    )
    paid = install_inbox.events("orders/paid")[0]
    assert paid.shop_domain == SHOP
    assert paid.checkout_token == completed["checkout_token"]
    assert paid.order_ref == f"gid://shopify/Order/{order_id}"
    assert paid.webhook_id


async def test_a_forged_or_replayed_delivery_is_refused_or_deduplicated(
    install_env: dict[str, str],
    install_app_url: str,
    install_inbox: WebhookInbox,
) -> None:
    """The signature is the only reason a delivery is believed, and ids are not re-counted."""
    body = json.dumps({"id": 9001, "checkout_token": "tok-forged"}).encode("utf-8")
    url = f"{install_app_url}/webhooks/shopify/orders/paid"

    def headers(signature: str, webhook_id: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Shopify-Topic": "orders/paid",
            "X-Shopify-Hmac-Sha256": signature,
            "X-Shopify-Shop-Domain": SHOP,
            "X-Shopify-Webhook-Id": webhook_id,
        }

    async with httpx.AsyncClient() as client:
        forged = await client.post(url, content=body, headers=headers("not-a-signature", "w-1"))
        assert forged.status_code == 401
        assert install_inbox.events() == ()

        good = sign(body, DEFAULT_WEBHOOK_SECRET)
        first = await client.post(url, content=body, headers=headers(good, "w-2"))
        assert first.status_code == 200
        assert first.json()["duplicate"] is False

        replay = await client.post(url, content=body, headers=headers(good, "w-2"))
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True
        assert len(install_inbox.events()) == 1

        mismatched = await client.post(
            f"{install_app_url}/webhooks/shopify/orders/fulfilled",
            content=body,
            headers=headers(good, "w-3"),
        )
        assert mismatched.status_code == 400

        unsubscribed = await client.post(
            f"{install_app_url}/webhooks/shopify/checkouts/create",
            content=body,
            headers={**headers(good, "w-4"), "X-Shopify-Topic": "checkouts/create"},
        )
        assert unsubscribed.status_code == 404


def test_the_raw_body_is_what_is_signed_not_a_reserialisation() -> None:
    """Re-encoding a parsed payload changes the digest; that is the classic fail-open bug."""
    wire = b'{"id":1,"checkout_token":"t"}'
    reserialised = json.dumps(json.loads(wire), indent=2).encode("utf-8")
    assert verify(wire, DEFAULT_WEBHOOK_SECRET, sign(wire, DEFAULT_WEBHOOK_SECRET))
    assert not verify(reserialised, DEFAULT_WEBHOOK_SECRET, sign(wire, DEFAULT_WEBHOOK_SECRET))
    assert not verify(wire, "", sign(wire, DEFAULT_WEBHOOK_SECRET))
    assert not verify(wire, DEFAULT_WEBHOOK_SECRET, None)


# ======================================================================================
# The HTTP door the merchant clicks
# ======================================================================================
async def test_the_install_route_redirects_to_shopify_and_refuses_a_bad_shop(
    install_env: dict[str, str], install_app_url: str
) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as client:
        good = await client.get(f"{install_app_url}/install", params={"shop": SHOP})
        assert good.status_code == 302
        target = urlparse(good.headers["location"])
        assert target.netloc == SHOP
        assert target.path == "/admin/oauth/authorize"
        query = parse_qs(target.query)
        assert query["scope"] == [",".join(REQUIRED_SCOPES)]
        assert query["client_id"] == [install_env["SHOPIFY_API_KEY"]]
        assert query["redirect_uri"] == [f"{install_env['MERCHANT_APP_URL']}/install/callback"]

        bad = await client.get(
            f"{install_app_url}/install", params={"shop": "good.myshopify.com@attacker.tld"}
        )
        assert bad.status_code == 400
        assert bad.json()["error"] == "invalid-shop"

        unknown_state = await client.get(
            f"{install_app_url}/install/callback",
            params={"shop": SHOP, "code": "c", "state": "never-issued", "hmac": "x"},
        )
        assert unknown_state.status_code == 400
        assert unknown_state.json()["error"] == "unknown-state"


async def test_the_service_mounts_the_install_router(install_app_url: str) -> None:
    """The routes are reached through the frozen entrypoint's discovery, not a test app."""
    async with httpx.AsyncClient() as client:
        schema = (await client.get(f"{install_app_url}/openapi.json")).json()
    assert "/install" in schema["paths"]
    assert "/webhooks/shopify/{resource}/{action}" in schema["paths"]


# ======================================================================================
# app_url is a destination, not a suggestion
# ======================================================================================
async def test_a_caller_supplied_app_url_is_where_the_pixel_actually_beacons(
    install_env: dict[str, str],
    install_stub: StubClient,
    install_stub_url: str,
    install_pixel_origin: Collector,
    install_tokens: InMemoryOfflineTokenStore,
) -> None:
    """``install(..., app_url=X)`` puts the pixel on ``X``, not on the configured origin.

    Regression for a silent-drop: ``install`` forwarded ``app_url`` when it built the three
    webhook callback URLs but not when it built the pixel's ``collectorUrl``, so a caller
    who named a destination got webhooks on their origin and checkout events on somebody
    else's — installed cleanly, no error, and indistinguishable afterwards from a shop
    whose shoppers simply never check out.

    The assertion is end to end, because the settings dict alone cannot tell them apart:
    ``MERCHANT_APP_URL`` is set (by ``install_env``) to a *third*, live origin, so a pixel
    that ignored ``app_url`` would still be pointed somewhere real. What is checked is
    where a completed checkout's beacon physically lands.
    """
    receiver, origin = install_pixel_origin
    configured = install_env["MERCHANT_APP_URL"]
    assert origin != configured, "the fixture must not hand back the configured origin"

    with _admin(install_stub_url) as admin:
        result = install(
            SHOP,
            admin,
            access_token=DEFAULT_ACCESS_TOKEN,
            tokens=install_tokens,
            app_url=origin,
        )

    assert result.pixel_settings["collectorUrl"] == f"{origin}/pixel/collect"
    assert not result.pixel_settings["collectorUrl"].startswith(configured)
    # The pixel and the webhooks now agree on one origin — the caller's.
    for registration in result.webhooks:
        assert registration.callback_url.startswith(f"{origin}/")

    completed = await install_stub.buy(VARIANT_ID)
    assert completed["pixel_event_posted"] is True

    beacons = [request for request in receiver.requests if request["path"] == "/pixel/collect"]
    assert len(beacons) == 1, (
        "the checkout event did not reach the origin the caller asked for; paths seen: "
        + repr([request["path"] for request in receiver.requests])
    )
    assert json.loads(beacons[0]["body"])["checkoutToken"] == completed["checkout_token"]


def test_an_explicit_collector_still_outranks_app_url(
    install_env: dict[str, str],
    install_stub_url: str,
    install_tokens: InMemoryOfflineTokenStore,
) -> None:
    """Precedence, stated once: the full URL wins, the origin is the fallback, then config.

    Without this the fix above could have been written as "``app_url`` wins", which would
    have broken every existing caller that passes ``collector=``.
    """
    with _admin(install_stub_url) as admin:
        both = install(
            SHOP,
            admin,
            access_token=DEFAULT_ACCESS_TOKEN,
            tokens=install_tokens,
            app_url="https://origin.example",
            collector="https://collector.example/collect",
        )
        neither = install(SHOP, admin, access_token=DEFAULT_ACCESS_TOKEN, tokens=install_tokens)

    assert both.pixel_settings["collectorUrl"] == "https://collector.example/collect"
    assert neither.pixel_settings["collectorUrl"] == (
        f"{install_env['MERCHANT_APP_URL']}/pixel/collect"
    )
