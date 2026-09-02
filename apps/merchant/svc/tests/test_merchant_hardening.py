"""Wave-6 hardening of the merchant install/webhook surface.

Every test here reproduces a defect an adversarial audit found in the T-050/T-051 surface
before it was fixed, and each one was watched fail against the code as it shipped. They are
grouped by the defect they witness, not by the module they touch, because several of the
defects are a *pair* of decisions in different files that are only wrong together.

What the green here is evidence of, and what it is not:

* The HMAC verification itself is **not** re-tested here — the audit confirmed it sound
  across thirteen adversarial cases and ``test_install.py`` already pins it. What is tested
  is everything that surrounds a correct signature check: which topic a correctly-signed
  body is filed under, who may read the installed-shop list, whether an anonymous request
  can evict somebody else's in-flight install, whether the URL the pixel is pointed at is
  served at all, whether the served route table matches the frozen contract, and whether an
  authenticated delivery is handed anywhere.
* Nothing here proves genuine Shopify conformance (SPEC A4). The signer is
  ``services/shopify-stub``'s secret and the receiver is the real service app.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from contracts.openapi import PINNED_ROUTES
from merchant_svc.install.config import COLLECTOR_PATH, WEBHOOK_PATH_PREFIX, collector_url
from merchant_svc.install.oauth import sign_callback
from merchant_svc.install.routes import ADMIN_TOKEN_ENV
from merchant_svc.install.webhooks import (
    WebhookDecision,
    WebhookInbox,
    delivery_digest,
    handle_delivery,
    sign,
)
from shopify_stub.state import DEFAULT_SHOP_DOMAIN, DEFAULT_WEBHOOK_SECRET

SHOP = DEFAULT_SHOP_DOMAIN
SECRET = DEFAULT_WEBHOOK_SECRET

#: The HTTP methods an OpenAPI path item may carry; everything else in it is metadata.
_HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
)


def _signed_headers(body: bytes, *, topic: str | None, webhook_id: str) -> dict[str, str]:
    """A delivery header block with a genuine signature, and optionally no topic at all."""
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Hmac-Sha256": sign(body, SECRET),
        "X-Shopify-Shop-Domain": SHOP,
        "X-Shopify-Webhook-Id": webhook_id,
    }
    if topic is not None:
        headers["X-Shopify-Topic"] = topic
    return headers


# ======================================================================================
# Finding 1 — a topic relabel that also destroys the genuine delivery
# ======================================================================================
async def test_an_unsigned_url_path_cannot_name_the_topic_a_delivery_is_filed_under(
    install_env: dict[str, str], install_app_url: str, install_inbox: WebhookInbox
) -> None:
    """The topic came from the URL path whenever ``X-Shopify-Topic`` was absent.

    ``handle_delivery`` did ``topic = topic or wanted``, so a delivery that simply omitted
    the header was filed under whatever the route path said. The path is not covered by the
    HMAC — it is chosen by whoever made the request — so that made the *label* on an
    authenticated body attacker-chosen: one captured ``orders/paid`` became a refund by
    being POSTed to a different URL.

    Rejected here and only here: a delivery that names no topic in the one place a topic
    may be read from. Still admitted: every delivery that carries the header, including one
    whose path agrees with it.
    """
    body = json.dumps({"id": 4242, "checkout_token": "tok-relabel"}).encode("utf-8")

    async with httpx.AsyncClient() as client:
        relabelled = await client.post(
            f"{install_app_url}{WEBHOOK_PATH_PREFIX}/refunds/create",
            content=body,
            headers=_signed_headers(body, topic=None, webhook_id="w-relabel"),
        )

    assert relabelled.status_code == 400, (
        "a validly-signed body with no topic header was accepted under the topic its URL "
        f"named; the service answered {relabelled.status_code} {relabelled.text}"
    )
    assert relabelled.json()["error"] == "missing-topic"
    assert install_inbox.events() == (), "the relabelled delivery was recorded anyway"


async def test_a_relabelled_replay_cannot_destroy_the_genuine_delivery(
    install_env: dict[str, str], install_app_url: str, install_inbox: WebhookInbox
) -> None:
    """The whole exploit, end to end: relabel, then watch the real delivery disappear.

    The two halves compounded. The topic was taken from the unsigned path, and the replay
    guard keyed on the body digest *alone* — so a replay to a chosen topic path both filed
    the body under the attacker's topic AND consumed the identity the genuine delivery
    would arrive under, which was then answered 200 "duplicate" and dropped on the floor.
    A merchant's paid order became a refund and no paid order was ever recorded.

    Rejected here and only here: the relabelled replay. Still admitted: the genuine
    delivery, under its own topic, counted once.
    """
    body = json.dumps({"id": 5150, "checkout_token": "tok-exploit"}).encode("utf-8")

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{install_app_url}{WEBHOOK_PATH_PREFIX}/refunds/create",
            content=body,
            headers=_signed_headers(body, topic=None, webhook_id="w-attack"),
        )
        genuine = await client.post(
            f"{install_app_url}{WEBHOOK_PATH_PREFIX}/orders/paid",
            content=body,
            headers=_signed_headers(body, topic="orders/paid", webhook_id="w-genuine"),
        )

    assert genuine.status_code == 200
    assert genuine.json()["duplicate"] is False, (
        "the genuine orders/paid delivery was swallowed as a duplicate of the replay"
    )
    assert [event.topic for event in install_inbox.events()] == ["orders/paid"]


def test_a_signed_body_is_bound_to_the_topic_it_was_first_recorded_under() -> None:
    """The inbox must be able to say WHICH topic a digest is already spoken for by.

    Keying on the digest alone was right — the topic is unsigned, so folding it into the
    freshness key lets one captured body count three times — but the guard could not report
    the collision, so a cross-topic replay was indistinguishable in the logs and in the
    response from Shopify's own honest retry. The binding is now explicit and reported.

    Rejected here and only here: the same signed body under a second topic. Still admitted:
    the first arrival, and any genuinely different body under any topic.
    """
    inbox = WebhookInbox()
    body = json.dumps({"id": 77, "checkout_token": "tok-bound"}).encode("utf-8")

    def deliver(topic: str) -> WebhookDecision:
        return handle_delivery(
            body=body,
            headers=_signed_headers(body, topic=topic, webhook_id=f"w-{topic}"),
            secret=SECRET,
            path_topic=topic,
            inbox=inbox,
        )

    first = deliver("orders/paid")
    assert first.duplicate is False
    assert inbox.bound_topic(delivery_digest(body)) == "orders/paid"

    crossed = deliver("refunds/create")
    assert crossed.duplicate is True, "one signed body became two events"
    assert crossed.detail.get("recorded_as") == "orders/paid", (
        "a cross-topic replay must say which topic already owns the body: " f"{crossed.detail}"
    )
    assert len(inbox.events()) == 1


# ======================================================================================
# Finding 2 — the installed-shop list was world-readable
# ======================================================================================
async def test_the_installed_shop_list_is_not_readable_by_an_anonymous_caller(
    install_env: dict[str, str],
    install_app_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /install/shops`` enumerated every customer to anybody who asked.

    ``create_app()`` installs no middleware and the route carried no guard, so an anonymous
    request returned the complete list of shops the app is installed on — the merchant
    customer list, which is commercially sensitive on its own and a target list for
    everything else.

    Rejected here and only here: a request with no credential, and one with the wrong
    credential. Still admitted: a request bearing the configured administrative token.
    """
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "s3cret-admin-token")

    async with httpx.AsyncClient() as client:
        anonymous = await client.get(f"{install_app_url}/install/shops")
        assert anonymous.status_code == 401, (
            "the installed-shop list was served to an anonymous caller: " + anonymous.text
        )
        assert "shops" not in anonymous.json()

        wrong = await client.get(
            f"{install_app_url}/install/shops",
            headers={"Authorization": "Bearer not-the-token"},
        )
        assert wrong.status_code == 401

        allowed = await client.get(
            f"{install_app_url}/install/shops",
            headers={"Authorization": "Bearer s3cret-admin-token"},
        )
        assert allowed.status_code == 200
        assert isinstance(allowed.json()["shops"], list)


async def test_an_unconfigured_admin_token_refuses_rather_than_opens(
    install_env: dict[str, str],
    install_app_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment that never set the token must serve nobody, not everybody.

    The failure mode that matters for a guard read out of the environment is the one where
    the environment is empty: a check written as ``if supplied == configured`` opens the
    route to a caller sending an empty credential.
    """
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)

    async with httpx.AsyncClient() as client:
        unset = await client.get(f"{install_app_url}/install/shops")
        assert unset.status_code == 503
        assert "shops" not in unset.json()

        blank = await client.get(
            f"{install_app_url}/install/shops", headers={"Authorization": "Bearer"}
        )
        assert blank.status_code == 503
        assert "shops" not in blank.json()


# ======================================================================================
# Finding 3 — 256 anonymous requests broke every in-flight install
# ======================================================================================
async def test_anonymous_install_starts_cannot_evict_an_in_flight_merchants_state(
    install_env: dict[str, str], install_app_url: str
) -> None:
    """``GET /install`` is unauthenticated and used to write into a 256-slot FIFO.

    ``_remember_state`` evicted the oldest pending state once the map hit its bound, so 256
    anonymous requests — the cheapest thing on the internet — flushed every merchant who
    was mid-install and answered their callback ``unknown-state``. The state is now
    self-authenticating (an HMAC over shop and issue time keyed by the app secret), so
    there is no shared slot to evict and nothing to flood.

    Rejected here and only here: nothing new. Still admitted: a merchant's own callback,
    after any volume of unrelated traffic.
    """
    async with httpx.AsyncClient(follow_redirects=False) as client:
        started = await client.get(f"{install_app_url}/install", params={"shop": SHOP})
        assert started.status_code == 302
        issued = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

        for index in range(300):
            flood = await client.get(
                f"{install_app_url}/install",
                params={"shop": f"flood-{index}.myshopify.com"},
            )
            assert flood.status_code == 302

        params = {"shop": SHOP, "code": "auth-code", "state": issued}
        params["hmac"] = sign_callback(params, install_env["SHOPIFY_API_SECRET"])
        finished = await client.get(f"{install_app_url}/install/callback", params=params)

    body = finished.json()
    assert body.get("error") != "unknown-state", (
        "300 anonymous requests evicted a merchant's in-flight install state"
    )
    # The callback gets as far as the token exchange, which no stub implements (SPEC A4):
    # what matters here is that it was *believed*, not that Shopify answered.
    assert finished.status_code == 502, body


async def test_an_install_state_is_single_use_and_bound_to_its_own_shop(
    install_env: dict[str, str], install_app_url: str
) -> None:
    """Dropping the server-side map must not drop what the map was for.

    A stateless, signed state has to keep the two properties the map provided: it names one
    shop and it may be redeemed once. Otherwise "fixed the denial of service" would mean
    "made every issued state a permanent, replayable install ticket for any shop".

    Rejected here and only here: a state redeemed twice, a state presented for a different
    shop than it was issued for, and a forged state. Still admitted: the first redemption.
    """
    async with httpx.AsyncClient(follow_redirects=False) as client:
        started = await client.get(f"{install_app_url}/install", params={"shop": SHOP})
        issued = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

        def signed(params: dict[str, str]) -> dict[str, str]:
            return {**params, "hmac": sign_callback(params, install_env["SHOPIFY_API_SECRET"])}

        wrong_shop = await client.get(
            f"{install_app_url}/install/callback",
            params=signed({"shop": "other-store.myshopify.com", "code": "c", "state": issued}),
        )
        assert wrong_shop.status_code == 400
        assert wrong_shop.json()["error"] == "shop-mismatch"

        forged = await client.get(
            f"{install_app_url}/install/callback",
            params=signed({"shop": SHOP, "code": "c", "state": "forged.state"}),
        )
        assert forged.status_code == 400
        assert forged.json()["error"] == "unknown-state"

        first = await client.get(
            f"{install_app_url}/install/callback",
            params=signed({"shop": SHOP, "code": "c", "state": issued}),
        )
        assert first.status_code == 502, first.text

        replayed = await client.get(
            f"{install_app_url}/install/callback",
            params=signed({"shop": SHOP, "code": "c", "state": issued}),
        )
        assert replayed.status_code == 400, "an install state was redeemable twice"
        assert replayed.json()["error"] == "unknown-state"


# ======================================================================================
# Finding 4 — the pixel was registered against a URL the service does not serve
# ======================================================================================
async def test_the_collector_url_the_install_registers_is_actually_served(
    install_env: dict[str, str], install_app_url: str
) -> None:
    """``web_pixel_settings`` pointed every installed pixel at a 404.

    ``collectorUrl`` was built as ``{app_url}/pixel/collect``, but the collector package was
    empty, so ``create_app()``'s ``*/routes.py`` discovery found no router for it. A pixel
    that installs cleanly and beacons into a 404 is indistinguishable from a shop whose
    shoppers never check out — the exact failure ``config.py`` warns about, shipped.

    Rejected here and only here: nothing. This is the positive control the whole collector
    exists for.
    """
    registered = collector_url(install_app_url)
    assert registered == f"{install_app_url}{COLLECTOR_PATH}"

    beacon = {
        "clientId": "cid-collector-1",
        "checkoutToken": "tok-collector-1",
        "orderId": "gid://shopify/Order/1001",
        "discountApplications": [{"code": "PS-1", "value": 10.0, "type": "code"}],
    }
    async with httpx.AsyncClient() as client:
        posted = await client.post(registered, json=beacon)

    assert posted.status_code == 204, (
        f"the URL every installed pixel beacons to answered {posted.status_code}"
    )


async def test_the_collector_route_refuses_a_payload_carrying_pii(
    install_env: dict[str, str], install_app_url: str
) -> None:
    """C5 grants no protected-customer-data scope, so the collector must hold none.

    Rejected here and only here: a beacon carrying a field outside the published join-key
    set. Still admitted: the join keys themselves.
    """
    async with httpx.AsyncClient() as client:
        refused = await client.post(
            f"{install_app_url}{COLLECTOR_PATH}",
            json={
                "clientId": "cid-1",
                "checkoutToken": "tok-1",
                "email": "shopper@example.com",
            },
        )

    assert refused.status_code == 400
    assert "shopper@example.com" not in refused.text, "the refusal echoed the PII back"


# ======================================================================================
# Finding 5 — the served route table diverged from the frozen contract
# ======================================================================================
def test_the_served_routes_match_the_pinned_contract() -> None:
    """Nothing compared the route table to ``packages/contracts``, so it drifted.

    The contract pins ``POST /webhooks/shopify/{topic}`` — one path parameter — and the
    service served ``POST /webhooks/shopify/{resource}/{action}``. Every consumer generated
    from the contract would have called a path this service does not answer, and no test in
    the suite could see it. This is that test.

    The install's own OAuth pair is deliberately absent from the contract: it is a
    Shopify-facing browser redirect, not a cross-domain service API, so it is listed here as
    an explicit exemption rather than silently ignored.
    """
    from merchant_svc.main import create_app

    schema = create_app().openapi()
    served = {
        (method.lower(), path)
        for path, item in schema["paths"].items()
        for method in item
        if method.lower() in _HTTP_METHODS
    }
    pinned = {
        (route.method.lower(), route.path)
        for route in PINNED_ROUTES
        if route.domain == "merchant"
    }
    exempt = {
        ("get", "/install"),
        ("get", "/install/callback"),
        ("get", "/install/shops"),
    }

    divergent = sorted(served - pinned - exempt)
    assert divergent == [], (
        "the service serves routes the frozen merchant contract does not pin; the contract "
        f"wins: {divergent}"
    )
    assert ("post", "/webhooks/shopify/{topic}") in served
    assert ("post", "/pixel/collect") in served


# ======================================================================================
# Finding 6 — authenticated deliveries were received and then discarded
# ======================================================================================
_FRESH_PROCESS_PROBE = textwrap.dedent(
    """
    import asyncio, json, sys
    import httpx
    from merchant_svc.install import webhooks
    from merchant_svc.main import create_app

    app = create_app()
    installed = webhooks.webhook_sink()
    body = json.dumps({"id": 31337, "checkout_token": "tok-sink"}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Topic": "orders/paid",
        "X-Shopify-Hmac-Sha256": webhooks.sign(body, "probe-secret"),
        "X-Shopify-Shop-Domain": "probe-store.myshopify.com",
        "X-Shopify-Webhook-Id": "w-probe",
    }

    async def drive():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
            return await client.post(
                "/webhooks/shopify/orders/paid", content=body, headers=headers
            )

    response = asyncio.run(drive())
    print(json.dumps({
        "sink_installed": installed is not None,
        "status": response.status_code,
        "handed_on": [record["order_ref"] for record in webhooks.HANDOFF.records()],
    }))
    """
)


def test_create_app_installs_a_real_webhook_sink_in_a_fresh_process() -> None:
    """``set_webhook_sink`` had no production caller, so ``_sink`` was ``None`` in the app.

    Every authenticated order webhook the deployed service received was verified, recorded
    in a bounded in-memory ring, and handed to nobody — the reconciliation input DESIGN
    calls authoritative, terminating in a display buffer.

    This runs in a **fresh interpreter** on purpose. ``_sink`` is process-global and other
    tests in this suite install and clear their own, so asserting on it in-process would
    measure test ordering rather than what a deployment gets. A subprocess is the only way
    to observe the state ``create_app()`` actually starts in.
    """
    env = dict(os.environ, SHOPIFY_API_SECRET="probe-secret")
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _FRESH_PROCESS_PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr

    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["sink_installed"] is True, (
        "create_app() left the webhook sink unwired; authenticated deliveries go nowhere"
    )
    assert result["status"] == 200, result
    assert result["handed_on"] == ["gid://shopify/Order/31337"], result
