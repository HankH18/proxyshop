"""T-100: the stub can never send a shopper, a webhook or a pixel to somebody else's host.

T-013 hardened :func:`~shopify_stub.permalink.build_permalink`. It did not harden the three
*other* places that interpolate ``shop_domain`` into a live ``https://`` URL, and the
builder is the only one of the four that no shopper's browser ever follows. The observable
defect, reproduced over real HTTP against uvicorn before this module existed::

    PUT  /_stub/config  {"shop_domain": "good.example.com@attacker.tld"}   -> 200
    GET  /cart/44352913:1                                                 -> 303
         location: https://good.example.com@attacker.tld/checkouts/<token>

Everything before the ``@`` is userinfo. The browser goes to ``attacker.tld``. The builder
refusing to *build* that URL bought nothing while the server would *serve* it, and
``StubConfig.validate`` let the domain in because it only asked whether it was non-empty.

Two independent guarantees are asserted here, and the second is the one that survives a
future route:

1. **an invalid domain cannot get into the config** — :meth:`StubConfig.__post_init__` and
   :meth:`StubConfig.validate` both run ``_assert_bare_host``, so the control plane answers
   400; and
2. **an invalid domain could not be served even if it did** — the three emit sites render
   through :func:`~shopify_stub.permalink.store_url`, so tampering with the live config
   object directly (which the tests below do, bypassing every validator) produces a 500 and
   *no* ``Location`` rather than a well-formed redirect to the attacker.

The response-splitting half is the same defect one layer down: ``_LABEL`` anchored with
``$``, which in Python matches immediately before a trailing newline, so ``"example.com\\n"``
was a legal host. A bare LF in a ``Location`` header ends the header.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from shopify_stub.app import create_app
from shopify_stub.orders import create_order_from_checkout, order_webhook_payload
from shopify_stub.permalink import PermalinkError, _assert_bare_host, build_permalink, store_url
from shopify_stub.state import (
    DEFAULT_SHOP_DOMAIN,
    Checkout,
    StubConfig,
    StubState,
    Variant,
)
from shopify_stub.telemetry import checkout_completed_payload
from shopify_stub.testing import NON_BARE_HOSTS, SEED_VARIANT, StubClient

from proxyshop_support.asgi_server import serve

VARIANT_ID = int(SEED_VARIANT["variant_id"])

#: A second *legitimate* domain. Used to prove the emit sites track configuration rather
#: than a hard-coded default — a guard that only ever sees one value proves nothing.
OTHER_DOMAIN = "store-b.example.com"

_HOSTS = sorted(NON_BARE_HOSTS.items())


@pytest.fixture
def tamperable_stub() -> Iterator[tuple[str, Any]]:
    """A real uvicorn-served stub *plus* a handle on its in-process :class:`Stub`.

    The package fixture yields only a client. These tests need to reach past every validator
    and write a hostile domain straight onto the live config object, which is the only way
    to ask the question that matters: *if a bad domain somehow existed, would the route serve
    it?* ``create_app`` stashes the instance on ``app.state.stub`` for exactly this.
    """
    application = create_app()
    with serve(application) as base_url:
        yield base_url, application.state.stub


@pytest.fixture
async def guarded(tamperable_stub: tuple[str, Any]) -> AsyncIterator[tuple[StubClient, Any]]:
    """:fixture:`tamperable_stub`, seeded, with a :class:`StubClient` in front of it."""
    base_url, stub = tamperable_stub
    async with httpx.AsyncClient(base_url=base_url, follow_redirects=False) as client:
        wrapper = StubClient(client, base_url)
        response = await wrapper.seed([SEED_VARIANT])
        assert response.status_code == 200, response.text
        yield wrapper, stub


# ---------------------------------------------------------------------------------------
# 1. An invalid domain cannot get into the configuration
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "domain"), _HOSTS)
def test_stub_config_refuses_every_non_bare_host(label: str, domain: str) -> None:
    """Acceptance 1. ``StubConfig`` with a hostile domain must be *unconstructable*.

    ``validate()`` alone would not be enough: it is called by ``PUT /_stub/config`` and by
    nothing else, so any other constructor of a config — a future control-plane route, a
    fixture, a consumer embedding the stub — would be free to build one. ``__post_init__``
    removes that freedom.
    """
    with pytest.raises(PermalinkError):
        StubConfig(shop_domain=domain)


@pytest.mark.parametrize(("label", "domain"), _HOSTS)
def test_validate_refuses_every_non_bare_host_after_mutation(label: str, domain: str) -> None:
    """The mutation path, which ``__post_init__`` cannot see.

    ``PUT /_stub/config`` builds its candidate with ``dataclasses.replace`` — which runs
    ``__post_init__`` while the copy still holds the *old*, valid domain — and only then
    assigns the incoming one. So ``validate()`` has to repeat the check; if it did not, the
    control plane would be the one unguarded door.
    """
    candidate = StubConfig()
    candidate.shop_domain = domain
    with pytest.raises(PermalinkError):
        candidate.validate()


async def test_put_config_refuses_the_proven_attack_and_keeps_serving_the_real_host(
    guarded: tuple[StubClient, Any],
) -> None:
    """The verbatim reproduction from the ticket, over real HTTP, now ending in a 400.

    Asserting the ``Location`` in full — not ``endswith`` — is the point. ``endswith(
    "/checkouts/<token>")`` was true of the attack URL too, which is precisely why the
    original suite was green over it.
    """
    stub, _ = guarded
    attack = "good.example.com@attacker.tld"

    refusal = await stub.configure(shop_domain=attack)
    assert refusal.status_code == 400, refusal.text
    assert "bare host" in refusal.json()["errors"]

    # The refusal is all-or-nothing: the live config still holds the real domain.
    assert (await stub.config())["shop_domain"] == DEFAULT_SHOP_DOMAIN

    response = await stub.visit_cart(VARIANT_ID)
    assert response.status_code == 303
    token = response.json()["token"]
    assert response.headers["location"] == f"https://{DEFAULT_SHOP_DOMAIN}/checkouts/{token}"
    assert urlsplit(response.headers["location"]).hostname == DEFAULT_SHOP_DOMAIN


@pytest.mark.parametrize(("label", "domain"), _HOSTS)
async def test_put_config_refuses_every_non_bare_host_over_http(
    guarded: tuple[StubClient, Any], label: str, domain: str
) -> None:
    """Acceptance 1, end to end: every entry in the table is a 400, and none of them stick."""
    stub, _ = guarded
    response = await stub.configure(shop_domain=domain)
    assert response.status_code == 400, f"{label}: {domain!r} was accepted -> {response.text}"
    assert (await stub.config())["shop_domain"] == DEFAULT_SHOP_DOMAIN


# ---------------------------------------------------------------------------------------
# 2. Even a domain that somehow got in cannot be emitted
# ---------------------------------------------------------------------------------------


async def test_the_cart_route_cannot_emit_an_off_domain_location(
    guarded: tuple[StubClient, Any],
) -> None:
    """Acceptance 2 for ``app.py``'s 303, asserted *past* the configuration guard.

    The config validator and this are two different defences and this test deliberately
    disables the first one, by writing the hostile domain straight onto the live dataclass
    instance — no ``replace``, no ``validate``, no route. What is left is the emit site
    alone, and the requirement on it is absolute: whatever else happens, the shopper's
    browser must not be handed ``attacker.tld``.
    """
    stub, instance = guarded
    instance.state.config.shop_domain = "good.example.com@attacker.tld"

    response = await stub.visit_cart(VARIANT_ID)

    assert response.status_code >= 500, (
        f"the route answered {response.status_code}; a tampered domain must fail loudly, "
        f"not redirect"
    )
    assert response.headers.get("location") is None
    assert "attacker.tld" not in response.text


async def test_the_order_webhook_and_pixel_cannot_be_rendered_off_domain(
    guarded: tuple[StubClient, Any],
) -> None:
    """Acceptance 2 for ``orders.py``'s ``order_status_url`` and ``telemetry.py``'s ``href``.

    Same tamper, applied after a legitimate cart visit so the failure can only come from
    completion — which is where both of those URLs are built.
    """
    stub, instance = guarded
    cart = await stub.visit_cart(VARIANT_ID)
    assert cart.status_code == 303
    token = cart.json()["token"]

    instance.state.config.shop_domain = "good.example.com@attacker.tld"
    completed = await stub.complete(token)

    assert completed.status_code >= 500, completed.text
    assert "attacker.tld" not in completed.text
    # Nothing off-domain reached either log on the way to that failure.
    assert "attacker.tld" not in json.dumps(await stub.events())
    assert "attacker.tld" not in json.dumps(await stub.deliveries())


def test_the_two_payload_builders_refuse_a_hostile_domain_directly() -> None:
    """``orders.py`` and ``telemetry.py``, each pinned on its own.

    The route-level tamper above proves *some* layer of completion refuses. These two prove
    *which*: the builders themselves, called with no server in the way, so a future change
    that stops one of them being reached cannot quietly un-guard it.
    """
    state = StubState()
    state.variants[SEED_VARIANT["variant_id"]] = Variant(
        variant_id=SEED_VARIANT["variant_id"],
        product_id=SEED_VARIANT["product_id"],
        title=SEED_VARIANT["title"],
        price=Decimal(SEED_VARIANT["price"]),
    )
    now = datetime.now(UTC)
    checkout = Checkout(
        token="a" * 32,
        client_id="c" * 8,
        variant_id=SEED_VARIANT["variant_id"],
        quantity=1,
        requested_code=None,
        applied_code=None,
        rejection=None,
        created_at=now,
    )
    state.checkouts[checkout.token] = checkout
    order = create_order_from_checkout(state, checkout, now=now)
    hostile = "good.example.com@attacker.tld"

    with pytest.raises(PermalinkError):
        order_webhook_payload(order, shop_domain=hostile, state=state)
    with pytest.raises(PermalinkError):
        checkout_completed_payload(checkout=checkout, order=order, now=now, shop_domain=hostile)

    # And both render correctly for a legal domain, so the guard is not "refuse everything".
    webhook = order_webhook_payload(order, shop_domain=OTHER_DOMAIN, state=state)
    assert webhook["order_status_url"] == (
        f"https://{OTHER_DOMAIN}/orders/{checkout.token}/authenticate"
    )
    event = checkout_completed_payload(
        checkout=checkout, order=order, now=now, shop_domain=OTHER_DOMAIN
    )
    context: Any = event["context"]
    assert context["document"]["location"]["href"] == (
        f"https://{OTHER_DOMAIN}/checkouts/{checkout.token}/thank_you"
    )


async def test_every_live_url_follows_the_configured_domain(
    guarded: tuple[StubClient, Any], webhook_receiver: tuple[Any, str]
) -> None:
    """All three emit sites track configuration — proven with a *second legal* domain.

    A guard that only ever sees ``proxyshop-demo.myshopify.com`` cannot tell "reads the
    config" from "hard-codes the default", so the domain is changed to a different legal one
    and every URL is asserted in full against it.
    """
    stub, _ = guarded
    receiver, url = webhook_receiver
    assert (await stub.configure(shop_domain=OTHER_DOMAIN)).status_code == 200
    await stub.subscribe("ORDERS_PAID", url)

    cart = await stub.visit_cart(VARIANT_ID)
    assert cart.status_code == 303
    token = cart.json()["token"]
    assert cart.headers["location"] == f"https://{OTHER_DOMAIN}/checkouts/{token}"

    completed = await stub.complete(token)
    assert completed.status_code == 201, completed.text

    body = json.loads(receiver.requests[0]["body"])
    assert body["order_status_url"] == f"https://{OTHER_DOMAIN}/orders/{token}/authenticate"

    (event,) = await stub.events()
    assert event["payload"]["context"]["document"]["location"]["href"] == (
        f"https://{OTHER_DOMAIN}/checkouts/{token}/thank_you"
    )


# ---------------------------------------------------------------------------------------
# 3. The response-splitting primitive: `$` vs `\Z`
# ---------------------------------------------------------------------------------------


def test_a_host_ending_in_lf_is_refused() -> None:
    """Acceptance 3. ``_LABEL`` anchors with ``\\A``/``\\Z``, so a trailing LF is not a host.

    Python's ``$`` matches at the end of the string *or immediately before a trailing
    newline*, so the old ``^…$`` accepted ``"com\\n"`` and ``build_permalink`` returned
    ``'https://example.com\\n/cart/1:1'`` — a URL carrying a raw LF, which in a ``Location``
    header terminates the header and makes every following byte a new one.
    """
    for host in ("example.com\n", "store-a.example.com\r\n", "store-a\n.example.com"):
        with pytest.raises(PermalinkError, match="bare host"):
            _assert_bare_host(host)
        with pytest.raises(PermalinkError, match="bare host"):
            build_permalink(shop_domain=host, variant_id=1)
        with pytest.raises(PermalinkError, match="bare host"):
            StubConfig(shop_domain=host)


def test_a_trailing_root_dot_is_still_a_host() -> None:
    """The anchor change must not take the legal case with it.

    ``example.com.`` and ``example.com`` are the same name and ``host_matches`` already
    treats them as equal, so refusing the dotted form here would make a legal name
    un-buildable.
    """
    _assert_bare_host("example.com.")
    assert build_permalink(shop_domain="example.com.", variant_id=1) == (
        "https://example.com./cart/1:1"
    )
    assert StubConfig(shop_domain="example.com.").shop_domain == "example.com."


# ---------------------------------------------------------------------------------------
# The single interpolation point itself
# ---------------------------------------------------------------------------------------


def test_store_url_renders_the_three_live_urls() -> None:
    """The positive contract, so the guard cannot be "fixed" by refusing everything."""
    assert store_url(shop_domain=OTHER_DOMAIN, path="/checkouts/abc") == (
        f"https://{OTHER_DOMAIN}/checkouts/abc"
    )
    assert store_url(shop_domain=OTHER_DOMAIN, path="/orders/abc/authenticate") == (
        f"https://{OTHER_DOMAIN}/orders/abc/authenticate"
    )
    assert store_url(shop_domain=OTHER_DOMAIN, path="/checkouts/abc/thank_you") == (
        f"https://{OTHER_DOMAIN}/checkouts/abc/thank_you"
    )


@pytest.mark.parametrize(("label", "domain"), _HOSTS)
def test_store_url_refuses_every_non_bare_host(label: str, domain: str) -> None:
    with pytest.raises(PermalinkError):
        store_url(shop_domain=domain, path="/checkouts/abc")


@pytest.mark.parametrize(
    "path",
    [
        "checkouts/abc",  # relative — would graft onto the previous path segment
        "/checkouts/abc\nX-Injected: yes",  # response splitting from the path side
        "/checkouts/abc\r\nX-Injected: yes",
        "/checkouts/ abc",
        "/checkouts/abc\x00",
    ],
)
def test_store_url_refuses_a_path_that_could_split_the_response(path: str) -> None:
    """The host is not the only way a control character reaches a header value."""
    with pytest.raises(PermalinkError):
        store_url(shop_domain=OTHER_DOMAIN, path=path)
