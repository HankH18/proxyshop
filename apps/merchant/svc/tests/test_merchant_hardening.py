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
from datetime import UTC, datetime
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


async def test_a_headerless_relabelled_replay_cannot_destroy_the_genuine_delivery(
    install_env: dict[str, str], install_app_url: str, install_inbox: WebhookInbox
) -> None:
    """The reported exploit, end to end: relabel, then watch the real delivery disappear.

    The name says *headerless* because that is exactly what this closes, and an earlier
    name that did not say so overclaimed: the variant where the attacker also supplies a
    matching topic header is NOT closed, and is pinned as a known residual by
    ``test_the_residual_a_header_bearing_replay_that_arrives_first_still_wins`` below.

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


async def test_the_residual_a_header_bearing_replay_that_arrives_first_still_wins(
    install_env: dict[str, str], install_app_url: str, install_inbox: WebhookInbox
) -> None:
    """The acknowledged limit, pinned so it is a known residual and not a rediscovery.

    Shopify's HMAC covers the body and nothing else. No header and no path segment is
    authenticated, so an attacker who already holds a validly-signed body, supplies a topic
    header that agrees with their chosen path, and arrives BEFORE Shopify's own delivery
    still files that body under their topic — and the genuine delivery is then refused as
    the cross-topic replay of theirs.

    What the fix removed is the *free* version (no header at all, see the test above) and
    the multiplication (one body can never become three events). Closing this one needs a
    topic the signature covers, which the vendor does not provide; the honest thing is to
    assert the behaviour rather than imply it is gone.

    If this test ever starts failing because the replay is refused, that is an IMPROVEMENT
    — re-read it before changing anything.
    """
    body = json.dumps({"id": 6161, "checkout_token": "tok-residual"}).encode("utf-8")

    async with httpx.AsyncClient() as client:
        attacker = await client.post(
            f"{install_app_url}{WEBHOOK_PATH_PREFIX}/refunds/create",
            content=body,
            headers=_signed_headers(body, topic="refunds/create", webhook_id="w-first"),
        )
        genuine = await client.post(
            f"{install_app_url}{WEBHOOK_PATH_PREFIX}/orders/paid",
            content=body,
            headers=_signed_headers(body, topic="orders/paid", webhook_id="w-second"),
        )

    assert attacker.status_code == 200
    assert attacker.json()["duplicate"] is False
    assert genuine.json()["duplicate"] is True
    # It is at least VISIBLE now: the response and the log name the topic that owns the body,
    # where before this was answered exactly like Shopify's own honest retry.
    assert genuine.json()["recorded_as"] == "refunds/create"
    assert [event.topic for event in install_inbox.events()] == ["refunds/create"]


# ======================================================================================
# Adversarial-verification follow-ups — defects found in the fixes themselves
# ======================================================================================
async def test_an_unreachable_token_endpoint_is_a_502_not_a_500(
    install_env: dict[str, str], install_app_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``exchange_code`` raises ``httpx.ConnectError``, which is not ``OAuthCallbackRejected``.

    The callback handler caught only the domain exception, so a token endpoint that was
    unreachable or slow — the failure that actually happens in production — escaped as an
    unhandled transport error and became a 500. Every uptime check and every operator reads
    a 500 as "this app is broken" rather than "Shopify was unreachable", and the comment
    added alongside the C9 ``base_url`` change claimed this case produced a 502 when it did
    not.

    Rejected here and only here: a 5xx that is not 502 on an unreachable token endpoint.
    """
    monkeypatch.setenv("SHOPIFY_STUB_URL", "http://127.0.0.1:1")

    async with httpx.AsyncClient(follow_redirects=False) as client:
        started = await client.get(f"{install_app_url}/install", params={"shop": SHOP})
        issued = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        params = {"shop": SHOP, "code": "c", "state": issued}
        params["hmac"] = sign_callback(params, install_env["SHOPIFY_API_SECRET"])
        answer = await client.get(f"{install_app_url}/install/callback", params=params)

    assert answer.status_code == 502, f"answered {answer.status_code}: {answer.text[:200]}"
    assert answer.json()["error"] == "token-exchange-failed"


async def test_a_failed_token_exchange_still_spends_the_state(
    install_env: dict[str, str], install_app_url: str
) -> None:
    """Single use means single use, including when the step AFTER it failed.

    Releasing the state on a failed token exchange looks like the kind thing to do — the
    merchant's retry then works instead of answering ``unknown-state`` — and it was written,
    tested and reverted. The callback URL carries ``code``, ``hmac`` and ``state`` together,
    so it leaks as a unit through browser history, a ``Referer`` or a proxy log, and a FAILED
    exchange is exactly the case where Shopify has not yet consumed the ``code``. Re-opening
    the state hands whoever holds that URL a live ticket for the shop's offline token. A
    merchant restarting at ``GET /install`` is recoverable; that is not.

    Rejected here and only here: a second redemption of a state, whatever happened after the
    first. Still admitted: a fresh install started from ``GET /install``.
    """
    async with httpx.AsyncClient(follow_redirects=False) as client:
        started = await client.get(f"{install_app_url}/install", params={"shop": SHOP})
        issued = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        params = {"shop": SHOP, "code": "c", "state": issued}
        params["hmac"] = sign_callback(params, install_env["SHOPIFY_API_SECRET"])

        first = await client.get(f"{install_app_url}/install/callback", params=params)
        assert first.status_code == 502
        retry = await client.get(f"{install_app_url}/install/callback", params=params)

        assert retry.status_code == 400, "a spent install state was redeemable again"
        assert retry.json()["error"] == "unknown-state"

        # The documented recovery: start over. It works, and it costs the merchant a click.
        restarted = await client.get(f"{install_app_url}/install", params={"shop": SHOP})
        fresh = parse_qs(urlparse(restarted.headers["location"]).query)["state"][0]
        assert fresh != issued
        again = {"shop": SHOP, "code": "c", "state": fresh}
        again["hmac"] = sign_callback(again, install_env["SHOPIFY_API_SECRET"])
        recovered = await client.get(f"{install_app_url}/install/callback", params=again)

    assert recovered.status_code == 502
    assert recovered.json()["error"] == "token-exchange-failed"


def test_the_redeemed_state_window_survives_concurrent_callbacks() -> None:
    """``finish_install`` is a sync ``def``, so FastAPI runs it in a threadpool.

    The prune loop iterated the module-global dict while another thread inserted into it,
    which raised ``RuntimeError: dictionary changed size during iteration`` and answered a
    LEGITIMATE install a 500. A replay guard that fails a real merchant is worse than the
    replay it guards against.

    Rejected here and only here: a state consumed twice, under any interleaving. Still
    admitted: every first consumption.
    """
    import threading

    from merchant_svc.install.routes import _consume_state

    states = [f"concurrent-state-{index}" for index in range(400)]
    accepted: list[str] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def hammer(offset: int) -> None:
        try:
            for index in range(len(states)):
                state = states[(index + offset) % len(states)]
                if _consume_state(state):
                    with lock:
                        accepted.append(state)
        except BaseException as exc:  # noqa: BLE001 - the point is that nothing escapes
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=hammer, args=(offset,)) for offset in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == [], f"_consume_state raised under concurrency: {failures[:3]}"
    assert sorted(accepted) == sorted(states), "a state was consumed twice, or lost"


def test_an_install_state_tolerates_replica_clock_skew_but_not_a_forged_future() -> None:
    """Signing the state was justified by making the flow multi-replica; skew is the cost.

    Two hosts are never exactly in step, so refusing every state whose issue time is one
    second ahead would have handed back the single-replica constraint the redesign removed.
    The allowance is bounded: a state dated hours ahead is not a clock, it is somebody
    minting one that never expires.
    """
    from datetime import timedelta

    from merchant_svc.install.oauth import (
        INSTALL_STATE_CLOCK_SKEW_SECONDS,
        InstallStateRejected,
        issue_install_state,
        read_install_state,
    )

    now = datetime.now(UTC)
    skewed = issue_install_state(SHOP, secret=SECRET, now=now + timedelta(seconds=30))
    assert read_install_state(skewed, secret=SECRET, now=now) == SHOP

    forged = issue_install_state(
        SHOP, secret=SECRET, now=now + timedelta(seconds=INSTALL_STATE_CLOCK_SKEW_SECONDS + 60)
    )
    with pytest.raises(InstallStateRejected):
        read_install_state(forged, secret=SECRET, now=now)


async def test_a_whitespace_only_secret_is_not_a_configured_secret(
    install_env: dict[str, str], install_app_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare truthiness check treats ``SHOPIFY_API_SECRET=" "`` as configured.

    It is not: states signed with it verify against nothing an operator meant, and the
    deployment looks healthy while refusing every real delivery. The route already applied
    ``.strip()`` to the admin token; the app-configured gate did not.
    """
    monkeypatch.setenv("SHOPIFY_API_SECRET", "   ")

    async with httpx.AsyncClient(follow_redirects=False) as client:
        started = await client.get(f"{install_app_url}/install", params={"shop": SHOP})

    assert started.status_code == 503
    assert started.json()["error"] == "app-not-configured"


# ======================================================================================
# Collector — the allowlist checks shapes, not just names
# ======================================================================================
def test_an_allowlisted_key_cannot_smuggle_a_structured_value() -> None:
    """The first allowlist checked field NAMES and coerced every value with ``str()``.

    So a published key holding an object carried the whole object in and stored it verbatim:
    ``{"clientId": {"email": "…", "name": "…"}}`` was accepted, and the collector that exists
    to hold no customer data held one. A join key is a scalar or it is not a join key.

    Rejected here and only here: a join key whose value is structured. Still admitted: every
    scalar spelling, including a numeric order id.
    """
    from merchant_svc.collector import PixelEventRejected, accept_pixel_event

    for smuggled in (
        {"checkoutToken": "ck-1", "clientId": {"email": "shopper@example.com"}},
        {"checkoutToken": "ck-1", "orderId": ["shopper@example.com"]},
        {"checkoutToken": {"email": "shopper@example.com"}},
    ):
        with pytest.raises(PixelEventRejected) as refused:
            accept_pixel_event(smuggled)
        assert "shopper@example.com" not in str(refused.value)

    accepted = accept_pixel_event(
        {"checkoutToken": "ck-1", "clientId": "cid-1", "orderId": 4242, "code": "PS-1"}
    )
    assert accepted.order_ref == "4242"


def test_the_one_nested_field_is_validated_entry_by_entry() -> None:
    """``discountApplications`` was carried as an opaque blob, which is a hole in the wall.

    A name allowlist that stops at the top level lets the one structured field it does admit
    carry anything at all. Each entry is now checked against the published set.

    Rejected here and only here: an entry with a field outside the set, a non-object entry,
    and an implausible number of them. Still admitted: the shape the pixel really sends.
    """
    from merchant_svc.collector import (
        MAX_DISCOUNT_APPLICATIONS,
        PixelEventRejected,
        accept_pixel_event,
    )

    real = accept_pixel_event(
        {
            "checkoutToken": "ck-1",
            "clientId": "cid-1",
            "orderId": "gid://shopify/Order/1",
            "discountApplications": [{"code": "PS-1", "value": 10.0, "type": "code"}],
        }
    )
    assert real.discount_code == "PS-1"
    assert real.gaps == ()

    for bad in (
        {"checkoutToken": "ck-1", "discountApplications": [{"code": "X", "email": "a@b.com"}]},
        {"checkoutToken": "ck-1", "discountApplications": ["not-an-object"]},
        {"checkoutToken": "ck-1", "discountApplications": "not-a-list"},
        {
            "checkoutToken": "ck-1",
            "discountApplications": [{"code": "X"}] * (MAX_DISCOUNT_APPLICATIONS + 1),
        },
    ):
        with pytest.raises(PixelEventRejected) as refused:
            accept_pixel_event(bad)
        assert "a@b.com" not in str(refused.value)


def test_a_non_finite_total_price_is_dropped_rather_than_stored() -> None:
    """``float("1e999")`` is ``inf`` and survives ``float()``; it does not survive JSON.

    A stored ``inf``/``nan`` breaks the round-trip downstream and silently poisons R4's
    "was the price honoured" comparison, where every comparison against ``nan`` is False.
    """
    from merchant_svc.collector import accept_pixel_event

    for poison in ("1e999", "-1e999", "nan", float("inf"), float("nan")):
        observation = accept_pixel_event({"checkoutToken": "ck-1", "total_price": poison})
        assert observation.total_price is None, f"{poison!r} was stored"

    assert accept_pixel_event({"checkoutToken": "ck-1", "total_price": "44.10"}).total_price == 44.1


async def test_the_collector_refusal_echoes_neither_the_value_nor_the_key(
    install_env: dict[str, str], install_app_url: str
) -> None:
    """A key is attacker-chosen text too, and the first refusal listed every one of them.

    ``{"shopper@example.com": 1}`` produced ``400 … refused unknown fields
    (shopper@example.com)`` — the address in the response body and the log of a service whose
    whole purpose is to hold no addresses — and 2000 unknown keys produced a 34 KB response,
    which is an amplifier on an unauthenticated route.

    Rejected here and only here: a refusal body that quotes attacker-chosen text, or that
    grows with the size of the offending payload.
    """
    async with httpx.AsyncClient() as client:
        leaky = await client.post(
            f"{install_app_url}{COLLECTOR_PATH}",
            json={"checkoutToken": "ck-1", "shopper@example.com": 1},
        )
        assert leaky.status_code == 400
        assert "shopper@example.com" not in leaky.text

        many = await client.post(
            f"{install_app_url}{COLLECTOR_PATH}",
            json={"checkoutToken": "ck-1", **{f"unknown-{i}": i for i in range(2000)}},
        )

    assert many.status_code == 400
    assert len(many.text) < 512, f"the refusal grew with the payload: {len(many.text)} bytes"
    assert many.json()["refused_fields"] == 2000


# ======================================================================================
# Both unauthenticated routes cap the body they will buffer
# ======================================================================================
async def test_neither_anonymous_route_will_buffer_an_unbounded_body(
    install_env: dict[str, str], install_app_url: str
) -> None:
    """``/pixel/collect`` needs no credential, and the webhook must read before it can verify.

    The signature is computed over the body bytes, so there is no ordering in which the
    webhook route authenticates first — both routes read from an anonymous sender. Both
    buffered whatever arrived, which is a memory-exhaustion primitive costing the sender one
    connection.

    Rejected here and only here: a body over the cap. Still admitted: everything real, which
    is three orders of magnitude smaller.
    """
    from merchant_svc.http_limits import MAX_REQUEST_BODY_BYTES

    oversized = b"x" * (MAX_REQUEST_BODY_BYTES + 4096)

    async with httpx.AsyncClient() as client:
        pixel = await client.post(
            f"{install_app_url}{COLLECTOR_PATH}",
            content=oversized,
            headers={"Content-Type": "application/json"},
        )
        webhook = await client.post(
            f"{install_app_url}{WEBHOOK_PATH_PREFIX}/orders/paid",
            content=oversized,
            headers=_signed_headers(oversized, topic="orders/paid", webhook_id="w-big"),
        )

    assert pixel.status_code == 413, pixel.text[:200]
    assert webhook.status_code == 413, webhook.text[:200]


def test_a_duplicated_topic_header_resolves_the_same_way_in_both_layers() -> None:
    """Starlette's ``dict(request.headers)`` keeps the FIRST value; this now does too.

    The route collapsed duplicates first-wins and ``handle_delivery`` would have taken the
    last if it were ever handed a real multidict, so the two halves of one code path
    disagreed about which ``X-Shopify-Topic`` a delivery carried. Harmless today only because
    the sender also controls the URL; it is a header-desync hazard behind any proxy.
    """
    from merchant_svc.install.webhooks import handle_delivery

    body = json.dumps({"id": 1, "checkout_token": "t"}).encode("utf-8")

    class _Duplicated(dict):
        def items(self):  # noqa: ANN204 - a minimal multidict stand-in
            return [
                ("X-Shopify-Topic", "refunds/create"),
                ("X-Shopify-Topic", "orders/paid"),
                ("X-Shopify-Hmac-Sha256", sign(body, SECRET)),
            ]

    decision = handle_delivery(
        body=body,
        headers=_Duplicated(),
        secret=SECRET,
        path_topic="orders/paid",
        inbox=WebhookInbox(),
    )
    assert decision.status_code == 400
    assert decision.detail["header_topic"] == "refunds/create", decision.detail
