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
    InstallResult,
    InvalidShopDomain,
    OAuthCallbackRejected,
    OnlineTokenRefused,
    ProtectedScopeRequested,
    ReceivedWebhook,
    ShopNotInstalled,
    WebhookDecision,
    WebhookInbox,
    assert_scopes_allowed,
    assert_topics_allowed,
    authorize_url,
    callback_signing_bytes,
    exchange_code,
    handle_delivery,
    install,
    normalize_scopes,
    normalize_shop_domain,
    read_callback,
    set_webhook_sink,
    sign,
    sign_callback,
    unauthorized_scopes,
    verify,
    verify_callback_hmac,
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
    # Corrected, not loosened. This line asserted "/webhooks/shopify/{resource}/{action}",
    # which pinned a route the FROZEN contract contradicts: `packages/contracts` —
    # `PINNED_ROUTES` and `openapi/merchant.openapi.json` — pins
    # `POST /webhooks/shopify/{topic}`, ONE path parameter. The contract wins, so the
    # implementation moved and this assertion follows it. It would have been wrong with or
    # without that change: the contract predates this file. `test_merchant_hardening.py`
    # now compares the whole served table to the contract, which is what should have caught
    # the divergence instead of a single hand-written path string.
    assert "/webhooks/shopify/{topic}" in schema["paths"]
    assert "/pixel/collect" in schema["paths"]


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


# ======================================================================================
# Hardening — every guard below is stated as "what does only this reject, and what does it
# still admit", because a guard that cannot name a rejected input is not a guard.
# ======================================================================================
async def test_a_non_ascii_signature_is_refused_rather_than_answered_with_a_500(
    install_env: dict[str, str],
    install_app_url: str,
    install_inbox: WebhookInbox,
) -> None:
    """One non-ASCII byte in a signature must be a hard refusal, not an exception.

    ``hmac.compare_digest`` raises ``TypeError`` when a ``str`` argument is not ASCII-only,
    and both digests this app compares arrive from the network: the
    ``X-Shopify-Hmac-Sha256`` header on a public webhook endpoint, and the ``hmac`` query
    parameter on the install callback. Comparing them as ``str`` turned a one-byte
    malformed signature into an unhandled exception that FastAPI answered with a 500 —
    an unauthenticated crash on both of this app's trust boundaries, reachable by anyone
    who can send an HTTP request.

    Rejected here and only here: a signature carrying a byte outside ASCII. Still admitted:
    the correct signature, asserted below so the fix cannot be "refuse everything".
    """
    body = json.dumps({"id": 4242, "checkout_token": "tok-crash"}).encode("utf-8")
    good = sign(body, DEFAULT_WEBHOOK_SECRET)

    assert verify(body, DEFAULT_WEBHOOK_SECRET, good) is True
    assert verify(body, DEFAULT_WEBHOOK_SECRET, "é" + good[1:]) is False
    assert verify(body, DEFAULT_WEBHOOK_SECRET, "\udcc3" + good[1:]) is False

    signed = {"shop": SHOP, "code": "auth-code-1", "state": "nonce-1"}
    signed["hmac"] = sign_callback(signed, DEFAULT_WEBHOOK_SECRET)
    assert verify_callback_hmac(signed, DEFAULT_WEBHOOK_SECRET) is True
    assert verify_callback_hmac(dict(signed, hmac="é" * 64), DEFAULT_WEBHOOK_SECRET) is False

    async with httpx.AsyncClient(follow_redirects=False) as client:
        # A raw non-ASCII byte on the wire, not a str httpx might sanitise for us.
        delivery = await client.post(
            f"{install_app_url}/webhooks/shopify/orders/paid",
            content=body,
            headers=[
                (b"content-type", b"application/json"),
                (b"x-shopify-topic", b"orders/paid"),
                (b"x-shopify-hmac-sha256", b"\xc3\xa9not-a-signature"),
                (b"x-shopify-shop-domain", SHOP.encode("ascii")),
            ],
        )
        assert delivery.status_code == 401, delivery.text
        assert delivery.json()["error"] == "bad-signature"
        assert install_inbox.events() == ()

        started = await client.get(f"{install_app_url}/install", params={"shop": SHOP})
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        callback = await client.get(
            f"{install_app_url}/install/callback",
            params={"shop": SHOP, "code": "c", "state": state, "hmac": "é" * 64},
        )
        assert callback.status_code == 401, callback.text
        assert callback.json()["error"] == "callback-rejected"


async def test_a_replay_cannot_buy_a_second_count_by_renaming_its_unsigned_webhook_id(
    install_env: dict[str, str],
    install_app_url: str,
    install_inbox: WebhookInbox,
) -> None:
    """The replay guard must key on something the sender cannot vary at will.

    Shopify's HMAC covers the request **body** and nothing else, so every header on a
    delivery — ``X-Shopify-Webhook-Id`` included — is chosen by whoever made the request.
    De-duplicating on that id alone therefore refused only a replay that volunteered to
    reuse its id: renaming it, or dropping the header entirely (which the id rule never
    de-duplicated at all), got the same signed ``orders/paid`` counted again, once per
    replay, and one purchase reconciled as many.

    Rejected here and only here: the same signed body arriving again under any id, or none.
    Still admitted: a different signed body, asserted at the end so the guard cannot be
    "refuse the second delivery".
    """
    body = json.dumps({"id": 9001, "checkout_token": "tok-replay", "total_price": "119.00"})
    encoded = body.encode("utf-8")
    url = f"{install_app_url}/webhooks/shopify/orders/paid"

    def headers(webhook_id: str | None, payload: bytes = encoded) -> dict[str, str]:
        sent = {
            "Content-Type": "application/json",
            "X-Shopify-Topic": "orders/paid",
            "X-Shopify-Hmac-Sha256": sign(payload, DEFAULT_WEBHOOK_SECRET),
            "X-Shopify-Shop-Domain": SHOP,
        }
        if webhook_id is not None:
            sent["X-Shopify-Webhook-Id"] = webhook_id
        return sent

    async with httpx.AsyncClient() as client:
        first = await client.post(url, content=encoded, headers=headers("w-genuine"))
        assert first.status_code == 200
        assert first.json()["duplicate"] is False

        for attempt in range(4):
            renamed = await client.post(url, content=encoded, headers=headers(f"forged-{attempt}"))
            assert renamed.status_code == 200
            assert renamed.json()["duplicate"] is True, f"replay {attempt} was counted again"

        for _ in range(4):
            headerless = await client.post(url, content=encoded, headers=headers(None))
            assert headerless.status_code == 200
            assert headerless.json()["duplicate"] is True, "a replay with no id was counted"

        # The shop a delivery is attributed to is an unsigned header too, so a replay was
        # also a way to file a real order against somebody else's shop.
        stolen = dict(headers("forged-shop"), **{"X-Shopify-Shop-Domain": "victim.myshopify.com"})
        misattributed = await client.post(url, content=encoded, headers=stolen)
        assert misattributed.json()["duplicate"] is True

        assert len(install_inbox.events()) == 1
        assert install_inbox.events()[0].shop_domain == SHOP

        # Positive control: a genuinely different order is still a fresh delivery.
        other = json.dumps({"id": 9002, "checkout_token": "tok-other"}).encode("utf-8")
        fresh = await client.post(url, content=other, headers=headers("w-second", other))
        assert fresh.json()["duplicate"] is False

    assert len(install_inbox.events()) == 2


def test_the_replay_guard_outlives_the_event_ring_rolling_over() -> None:
    """Filling the bounded event log must not amnesty the deliveries it evicts.

    The seen-set used to be pruned in lockstep with the ring: popping the oldest event
    dropped its id, so a delivery older than ``INBOX_CAPACITY`` was accepted a second time.
    The ring is a display buffer and an attacker fills it with traffic, which is the one
    resource an attacker always has — so the replay window is now bounded on its own clock.

    Rejected here and only here: a delivery that has already been recorded, however much
    unrelated traffic arrived since. Still admitted: a delivery never seen before.
    """
    inbox = WebhookInbox(capacity=4)

    def deliver(marker: str, webhook_id: str) -> WebhookDecision:
        payload = json.dumps({"id": marker, "checkout_token": marker}).encode("utf-8")
        return handle_delivery(
            body=payload,
            headers={
                "X-Shopify-Topic": "orders/paid",
                "X-Shopify-Hmac-Sha256": sign(payload, DEFAULT_WEBHOOK_SECRET),
                "X-Shopify-Webhook-Id": webhook_id,
                "X-Shopify-Shop-Domain": SHOP,
            },
            secret=DEFAULT_WEBHOOK_SECRET,
            path_topic="orders/paid",
            inbox=inbox,
        )

    assert deliver("order-1", "w-1").duplicate is False
    for index in range(inbox.capacity + 3):
        assert deliver(f"filler-{index}", f"w-f{index}").duplicate is False
    assert len(inbox.events()) == inbox.capacity, "the event ring must still be bounded"

    assert deliver("order-1", "w-1").duplicate is True
    assert deliver("order-1", "w-1-renamed").duplicate is True
    assert deliver("order-99", "w-99").duplicate is False


def test_a_sink_that_refuses_a_delivery_gets_the_retry_not_a_duplicate() -> None:
    """A delivery marked seen but never handed on is a delivery nobody will send again.

    ``handle_delivery`` recorded first and called the downstream sink second, so a sink
    that raised took the whole request down with it — and Shopify's retry then matched the
    id that recording had already stored, was answered 2xx as a "duplicate", and the event
    was lost for good. A transient ledger outage became permanent data loss on the topic
    DESIGN calls authoritative for reconciliation.

    Rejected here and only here: a delivery the sink could not accept — answered non-2xx,
    left un-recorded, retried. Still admitted: the retry once the sink recovers, and a
    second copy of that retry is still a duplicate.
    """
    inbox = WebhookInbox()
    body = json.dumps({"id": 7, "checkout_token": "tok-sink"}).encode("utf-8")
    headers = {
        "X-Shopify-Topic": "orders/paid",
        "X-Shopify-Hmac-Sha256": sign(body, DEFAULT_WEBHOOK_SECRET),
        "X-Shopify-Webhook-Id": "w-retryable",
        "X-Shopify-Shop-Domain": SHOP,
    }
    handed_on: list[ReceivedWebhook] = []
    outage = {"down": True}

    def ledger(event: ReceivedWebhook) -> None:
        if outage["down"]:
            raise RuntimeError("the ledger writer is down")
        handed_on.append(event)

    def deliver() -> WebhookDecision:
        return handle_delivery(
            body=body,
            headers=headers,
            secret=DEFAULT_WEBHOOK_SECRET,
            path_topic="orders/paid",
            inbox=inbox,
        )

    set_webhook_sink(ledger)
    try:
        refused = deliver()
        assert refused.accepted is False, "a 2xx here tells Shopify to stop retrying"
        assert refused.status_code == 500
        assert refused.reason == "sink-failed"
        assert handed_on == []
        assert inbox.events() == (), "an un-handed-on delivery must not be marked seen"

        outage["down"] = False
        retry = deliver()
        assert retry.accepted is True
        assert retry.duplicate is False, "the retry was swallowed as a duplicate"
        assert [event.payload["id"] for event in handed_on] == [7]
        assert len(inbox.events()) == 1

        # Positive control: once it is handed on, a further retry is a duplicate again.
        again = deliver()
        assert again.accepted is True
        assert again.duplicate is True
        assert len(handed_on) == 1
    finally:
        set_webhook_sink(None)


# ======================================================================================
# Follow-up hardening — the identity is the signed body, and only the signed body
# ======================================================================================
def test_a_secret_that_cannot_be_utf8_encoded_is_still_a_refusal_not_a_crash() -> None:
    """Guarding the attacker's digest is not enough if the *key* can still throw.

    ``os.environ`` is decoded with ``surrogateescape``, so a single non-UTF-8 byte in
    ``SHOPIFY_API_SECRET`` reaches this code as a lone surrogate — and a strict
    ``secret.encode("utf-8")`` then raises ``UnicodeEncodeError`` on **every** delivery,
    turning an operator's typo into a 500 for every anonymous webhook POST. The same is
    true of the callback's canonical bytes, whose keys and values all came off the wire.

    Rejected here and only here: nothing — this is a totality property. What it pins is
    that no input reaches an exception. The admitted/refused pair is asserted underneath
    it, so "never raises" cannot be satisfied by refusing everything.
    """
    poisoned = "\udcc3" + DEFAULT_WEBHOOK_SECRET
    body = b'{"id":1,"checkout_token":"t"}'

    assert isinstance(sign(body, poisoned), str)
    assert verify(body, poisoned, sign(body, poisoned)) is True
    assert verify(body, poisoned, sign(body, "a-different-secret")) is False

    params = {"shop": SHOP, "code": "c", "state": "s", "\udcc3-key": "\udcff-value"}
    assert isinstance(callback_signing_bytes(params), bytes)
    params["hmac"] = sign_callback(params, poisoned)
    assert verify_callback_hmac(params, poisoned) is True
    assert verify_callback_hmac(dict(params, code="tampered"), poisoned) is False


async def test_one_signed_body_cannot_become_three_events_by_changing_its_topic(
    install_env: dict[str, str],
    install_app_url: str,
    install_inbox: WebhookInbox,
) -> None:
    """The topic is unsigned, so it cannot be part of what makes a delivery distinct.

    Shopify's HMAC covers the body alone. Folding the topic into the de-duplication key
    therefore left one captured ``orders/paid`` replayable as three "fresh" events — the
    same signed purchase arriving again as a fabricated fulfilment and a fabricated
    refund, each one counted, because the replayer simply changed a header and a URL.

    Rejected here and only here: the same signed body under any of the three topics after
    the first. Still admitted: a different signed body, and the first arrival itself.
    """
    body = json.dumps({"id": 5150, "checkout_token": "tok-topic"}).encode("utf-8")
    signature = sign(body, DEFAULT_WEBHOOK_SECRET)

    async with httpx.AsyncClient() as client:

        async def post(topic: str) -> httpx.Response:
            return await client.post(
                f"{install_app_url}/webhooks/shopify/{topic}",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Topic": topic,
                    "X-Shopify-Hmac-Sha256": signature,
                    "X-Shopify-Shop-Domain": SHOP,
                    "X-Shopify-Webhook-Id": f"w-{topic}",
                },
            )

        first = await post("orders/paid")
        assert first.status_code == 200
        assert first.json()["duplicate"] is False

        for topic in ("orders/fulfilled", "refunds/create"):
            replay = await post(topic)
            assert replay.status_code == 200
            assert replay.json()["duplicate"] is True, f"replayed as a fresh {topic} event"

    assert len(install_inbox.events()) == 1
    assert install_inbox.events()[0].topic == "orders/paid"


def test_a_new_body_arriving_under_an_already_seen_webhook_id_is_still_recorded() -> None:
    """The webhook id must not be able to suppress a delivery it does not describe.

    Keeping the unsigned id as a secondary de-duplication key cut both ways: it dropped a
    *genuinely different* body that happened to arrive under an id already seen. Since the
    id is chosen by the sender, that is a suppression primitive, not a safety net.

    Rejected here and only here: a repeat of the same signed body. Still admitted: a new
    signed body, whatever id it carries.
    """
    inbox = WebhookInbox()

    def deliver(marker: str, webhook_id: str) -> WebhookDecision:
        payload = json.dumps({"id": marker, "checkout_token": marker}).encode("utf-8")
        return handle_delivery(
            body=payload,
            headers={
                "X-Shopify-Topic": "orders/paid",
                "X-Shopify-Hmac-Sha256": sign(payload, DEFAULT_WEBHOOK_SECRET),
                "X-Shopify-Webhook-Id": webhook_id,
                "X-Shopify-Shop-Domain": SHOP,
            },
            secret=DEFAULT_WEBHOOK_SECRET,
            path_topic="orders/paid",
            inbox=inbox,
        )

    assert deliver("order-1", "w-shared").duplicate is False
    assert deliver("order-2", "w-shared").duplicate is False, "a real order was suppressed"
    assert deliver("order-1", "w-shared").duplicate is True
    assert len(inbox.events()) == 2


def test_the_replay_window_retires_whole_deliveries_at_its_boundary() -> None:
    """Eviction must never leave half a delivery behind.

    With two keys per delivery the FIFO retired them one at a time, and they were inserted
    adjacently — so the body key went first and the id key was stranded. A delivery still
    well inside the window could then be replayed under a renamed id and accepted: the
    exact attack the guard exists to stop, reappearing at the eviction boundary. One
    identity per delivery is what makes eviction atomic.

    Rejected here and only here: a replay of any delivery still inside the window, under
    any id. Still admitted: a delivery whose identity the window has legitimately retired,
    which is the stated bound rather than a hole.
    """
    # The replay window is never allowed to be smaller than the event ring, so the ring
    # has to be the smaller of the two for this test to exercise the window's own bound.
    inbox = WebhookInbox(capacity=2, seen_capacity=3)
    assert inbox.seen_capacity == 3

    def deliver(marker: str, webhook_id: str) -> WebhookDecision:
        payload = json.dumps({"id": marker}).encode("utf-8")
        return handle_delivery(
            body=payload,
            headers={
                "X-Shopify-Topic": "orders/paid",
                "X-Shopify-Hmac-Sha256": sign(payload, DEFAULT_WEBHOOK_SECRET),
                "X-Shopify-Webhook-Id": webhook_id,
            },
            secret=DEFAULT_WEBHOOK_SECRET,
            path_topic="orders/paid",
            inbox=inbox,
        )

    assert deliver("o1", "w-1").duplicate is False
    assert deliver("o2", "w-2").duplicate is False
    # Two identities in a three-identity window: nothing may have been retired yet.
    assert deliver("o1", "renamed").duplicate is True, "o1 was stranded before the boundary"

    assert deliver("o3", "w-3").duplicate is False
    assert deliver("o4", "w-4").duplicate is False  # crosses the boundary; o1 retires
    assert deliver("o4", "renamed-again").duplicate is True, "the newest was retired instead"
    assert deliver("o3", "renamed-too").duplicate is True
    # The stated bound, asserted so the window is a window and not an unbounded promise.
    assert deliver("o1", "w-1").duplicate is False


def test_forgetting_a_delivery_this_inbox_never_recorded_disarms_nothing() -> None:
    """``forget`` releases an identity only if it was the delivery that claimed it.

    A delivery refused as a duplicate never added the identity it matched. Releasing it on
    that delivery's behalf would hand the original's replay protection to whoever sent the
    duplicate — a one-request disarm of the guard, from outside.

    Rejected here and only here: every replay, before and after the stray ``forget``.
    Still admitted: the retry of a delivery this inbox really did record and then release.
    """
    inbox = WebhookInbox()
    body = json.dumps({"id": 424242, "checkout_token": "tok-forget"}).encode("utf-8")
    headers = {
        "X-Shopify-Topic": "orders/paid",
        "X-Shopify-Hmac-Sha256": sign(body, DEFAULT_WEBHOOK_SECRET),
        "X-Shopify-Webhook-Id": "w-1",
        "X-Shopify-Shop-Domain": SHOP,
    }

    def deliver() -> WebhookDecision:
        return handle_delivery(
            body=body,
            headers=headers,
            secret=DEFAULT_WEBHOOK_SECRET,
            path_topic="orders/paid",
            inbox=inbox,
        )

    recorded = deliver()
    assert recorded.duplicate is False
    duplicate = deliver()
    assert duplicate.duplicate is True

    assert duplicate.event is not None
    inbox.forget(duplicate.event)  # never recorded: must release nothing

    assert deliver().duplicate is True, "a stray forget disarmed the replay guard"
    assert len(inbox.events()) == 1

    # Positive control: forgetting the delivery the inbox really did record does release it.
    assert recorded.event is not None
    inbox.forget(recorded.event)
    assert inbox.events() == ()
    assert deliver().duplicate is False


# ======================================================================================
# The C5 guard has to survive the shapes a real caller hands it
# ======================================================================================
def test_the_scope_guard_is_not_disarmed_by_the_shape_shopify_itself_returns() -> None:
    """A comma-joined scope string is the granted-scope spelling, and it must be checked.

    ``str`` is an ``Iterable[str]`` — of single characters — so the guard used to shred a
    bare scope name into letters, none of which is a protected scope, and pass. Worse, the
    one-entry comma-joined list is *exactly* what Shopify's token-exchange response puts in
    its ``scope`` field, so feeding the granted set back through the guard admitted
    ``read_customers`` with C5 never firing.

    REFUSED, concretely: ``["read_orders,read_customers"]`` and the bare string
    ``"read_customers"``. ADMITTED, concretely: ``["read_orders,write_pixels"]`` — which
    normalizes to both scopes rather than being refused wholesale, so the fix cannot be
    "reject anything with a comma in it".
    """
    # ADMITTED: the comma-joined form is split, not refused.
    assert normalize_scopes(["read_orders,write_pixels"]) == ("read_orders", "write_pixels")
    assert assert_scopes_allowed(["read_orders, Write_Pixels "]) == (
        "read_orders",
        "write_pixels",
    )
    assert assert_scopes_allowed(list(REQUIRED_SCOPES)) == REQUIRED_SCOPES

    # REFUSED: a protected scope hiding inside the granted-scope string is now seen.
    assert unauthorized_scopes(["read_orders,read_customers"]) == ("read_customers",)
    with pytest.raises(ProtectedScopeRequested) as refused:
        assert_scopes_allowed(["read_orders,read_customers"])
    assert refused.value.offending == ("read_customers",)

    # REFUSED: a bare string cannot be quietly iterated into harmless characters.
    for bare in ("read_customers", "read_orders"):
        with pytest.raises(TypeError):
            assert_scopes_allowed(bare)
        with pytest.raises(TypeError):
            normalize_scopes(bare)

    # And the guard still refuses every protected scope one at a time, comma-joined.
    for scope in sorted(PROTECTED_CUSTOMER_DATA_SCOPES):
        with pytest.raises(ProtectedScopeRequested):
            assert_scopes_allowed([f"read_orders,{scope}"])


def test_an_offline_token_never_appears_in_the_text_of_the_objects_that_hold_it() -> None:
    """The credential must not be one traceback or one ``%r`` away from the logs.

    :class:`OfflineToken` is reachable from :class:`InstallResult`, so a single
    ``logger.info("%r", result)``, an f-string, a pytest assertion diff or a traceback
    rendered with locals would have printed a live, long-lived Admin API token in full.

    REFUSED, concretely: the raw ``shpat_…`` value, in ``repr``, ``str``, f-string and the
    enclosing :class:`InstallResult`. ADMITTED, concretely: the shop, the scopes and the
    redacted prefix-and-length, which is what makes the line useful to read.
    """
    secret = "shpat_do_not_print_me_0123456789"
    token = InMemoryOfflineTokenStore().save(SHOP, secret, scopes=REQUIRED_SCOPES)

    for rendering in (repr(token), str(token), f"{token}", f"{token!r}", f"{token!s}"):
        assert secret not in rendering, f"the token leaked into {rendering!r}"

    result = InstallResult(
        shop_domain=SHOP, scopes=REQUIRED_SCOPES, pixel_settings={}, offline_token=token
    )
    assert secret not in repr(result)
    assert secret not in str(result)

    # Still useful: the shop, the scopes and enough of the token to identify it.
    assert SHOP in str(token)
    assert "read_orders" in str(token)
    assert token.redacted() in str(token)
    assert str(len(secret)) in token.redacted()
    assert token.access_token == secret, "the value itself must still be readable in code"


def test_a_body_too_deeply_nested_to_parse_is_refused_not_retried_forever() -> None:
    """An authenticated body that can never parse must terminate, not loop.

    ``json.loads`` raises ``RecursionError`` — not ``ValueError`` — on a deeply nested
    document, so a **validly signed** body escaped ``handle_delivery`` and the route
    answered 5xx. Shopify retries 5xx, the body can never parse, and the retry is
    permanent: an authenticated sender could pin one delivery in a forever-loop.

    REFUSED, concretely: 60,000-deep nesting, answered 400 ``unparseable-body`` — the same
    answer flat garbage gets. ADMITTED, concretely: an ordinary one-level payload, and
    nesting deep enough to be unusual but shallow enough to parse.
    """
    inbox = WebhookInbox()

    def deliver(payload: bytes) -> WebhookDecision:
        return handle_delivery(
            body=payload,
            headers={
                "X-Shopify-Topic": "orders/paid",
                "X-Shopify-Hmac-Sha256": sign(payload, DEFAULT_WEBHOOK_SECRET),
                "X-Shopify-Webhook-Id": f"w-{len(payload)}",
            },
            secret=DEFAULT_WEBHOOK_SECRET,
            path_topic="orders/paid",
            inbox=inbox,
        )

    nested = b'{"a":' * 60000 + b"1" + b"}" * 60000
    refused = deliver(nested)
    assert refused.status_code == 400
    assert refused.reason == "unparseable-body"
    assert refused.accepted is False

    # The pre-existing refusal is unchanged: flat garbage is still a 400, not a 500.
    assert deliver(b"not json at all").reason == "unparseable-body"
    assert deliver(b'"a bare string is not an object"').reason == "unparseable-body"

    # ADMITTED: an ordinary body, and one nested deeply enough to be odd but still legal.
    ordinary = json.dumps({"id": 1, "checkout_token": "t"}).encode("utf-8")
    assert deliver(ordinary).reason == "recorded"
    survivable = b'{"a":' * 40 + b"1" + b"}" * 40
    assert deliver(survivable).reason == "recorded"
    assert len(inbox.events()) == 2
