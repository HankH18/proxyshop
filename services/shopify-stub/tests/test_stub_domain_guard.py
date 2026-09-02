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

import ast
import inspect
import json
import re
import traceback
import unicodedata
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.responses import JSONResponse, RedirectResponse
from shopify_stub import app as app_module
from shopify_stub import orders as orders_module
from shopify_stub import permalink as permalink_module
from shopify_stub import telemetry as telemetry_module
from shopify_stub.app import create_app
from shopify_stub.orders import create_order_from_checkout, order_webhook_payload
from shopify_stub.permalink import (
    _FORBIDDEN_IN_PATH,
    PermalinkError,
    _assert_bare_host,
    build_permalink,
    store_url,
)
from shopify_stub.state import (
    DEFAULT_SHOP_DOMAIN,
    Checkout,
    StubConfig,
    StubState,
    Variant,
)
from shopify_stub.telemetry import checkout_completed_payload
from shopify_stub.testing import (
    NON_BARE_HOSTS,
    RESPONSE_SPLITTING_HOSTS,
    SEED_VARIANT,
    StubClient,
)

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


# ---------------------------------------------------------------------------------------
# T-118 (d): the Raises clause, pinned character by character
# ---------------------------------------------------------------------------------------
#
# The five cases above are a *sample*. A sample is what let `store_url`'s docstring and
# `_FORBIDDEN_IN_PATH` drift apart in two directions at once while every test stayed green:
#
#   * the docstring said the path raises when it "carries a control character", and said
#     nothing about the space — but the space (U+0020, Unicode category `Zs`, not a control
#     character at all) was refused; and
#   * `[\x00-\x20\x7f]` covered the C0 block and stopped there, so U+0085 NEL — a control
#     character by every definition — was *accepted*. Had one reached the cart route's
#     `Location`, Starlette's latin-1 header encoding would have put a bare 0x85 byte on
#     the wire; none could, because every `store_url` path is built from a hex UUID or a
#     digit run (T-129, `test_no_caller_supplied_value_reaches_a_store_url_path`).
#
# The three parametrizations below are the whole ASCII range plus the characters that used
# to escape, one case each, so the prose can only drift again by turning a test red.

#: Everything the constant's comment claims is refused alongside CR and LF: the C0 block,
#: the space, and DEL. Written as code points so no raw control byte appears in this file.
_C0_SPACE_AND_DEL = [chr(cp) for cp in range(0x00, 0x21)] + ["\x7f"]

#: The characters the old deny-list missed, written as escapes so no exotic byte sits
#: literally in this source file. Every one of them was ACCEPTED by ``[\x00-\x20\x7f]``.
_OLD_DENY_LIST_ESCAPEES = [
    "\u0080",  # C1 PAD
    "\u0085",  # C1 NEL - historically a line terminator to some parsers
    "\u009f",  # C1 APC
    "\u00a0",  # NBSP
    "\u2028",  # LINE SEPARATOR - a JS line terminator inside a <script> JSON literal
    "\u2029",  # PARAGRAPH SEPARATOR
    "\u202e",  # RIGHT-TO-LEFT OVERRIDE - display spoofing in a live link
    "\u200b",  # ZERO WIDTH SPACE
    "\ufeff",  # BOM / ZWNBSP
    "\u4e2d",  # a plain CJK ideograph: legal text, but not latin-1, so the ASGI
    #            server raised UnicodeEncodeError three layers from the caller
    #            instead of this function raising PermalinkError at the call site
]

#: The positive control. Every printable ASCII graphic character is still legal in a path,
#: so the guard cannot be "fixed" by refusing everything.
_PRINTABLE_ASCII = [chr(cp) for cp in range(0x21, 0x7F)]


@pytest.mark.parametrize("char", _C0_SPACE_AND_DEL, ids=lambda c: f"U+{ord(c):04X}")
def test_store_url_refuses_every_c0_control_the_space_and_del(char: str) -> None:
    """The full set `_FORBIDDEN_IN_PATH`'s comment claims, not a sample of it."""
    with pytest.raises(PermalinkError):
        store_url(shop_domain=OTHER_DOMAIN, path=f"/checkouts/abc{char}")


@pytest.mark.parametrize("char", _OLD_DENY_LIST_ESCAPEES, ids=lambda c: f"U+{ord(c):04X}")
def test_store_url_refuses_the_characters_outside_ascii(char: str) -> None:
    """The other half of the drift: the docstring promised these and the code allowed them."""
    assert re.compile(r"[\x00-\x20\x7f]").search(char) is None, (
        "this case is only interesting because the old deny-list let it through"
    )
    with pytest.raises(PermalinkError):
        store_url(shop_domain=OTHER_DOMAIN, path=f"/checkouts/abc{char}")


@pytest.mark.parametrize("char", _PRINTABLE_ASCII, ids=lambda c: f"U+{ord(c):04X}")
def test_store_url_accepts_every_printable_ascii_character(char: str) -> None:
    """The positive control: refusing everything is not a fix."""
    assert store_url(shop_domain=OTHER_DOMAIN, path=f"/checkouts/abc{char}") == (
        f"https://{OTHER_DOMAIN}/checkouts/abc{char}"
    )


def test_a_c1_control_slipped_the_old_deny_list() -> None:
    """The reproduction, kept executable so the regression is a fact and not a memory.

    ``U+0085`` is Unicode category ``Cc`` — a control character — and the old
    ``[\\x00-\\x20\\x7f]`` did not match it. Had such a URL reached a ``Location`` header,
    Starlette's latin-1 encoding would have put a raw ``0x85`` byte on the wire — the
    ``rendered_by_the_old_code`` line below is that conditional, not a report of something
    this service ever served. Nothing could reach it: see T-129's
    ``test_no_caller_supplied_value_reaches_a_store_url_path``.
    """
    escaped = "/checkouts/abc\x85"
    assert unicodedata.category("\x85") == "Cc", "U+0085 is a control character"
    assert re.compile(r"[\x00-\x20\x7f]").search(escaped) is None, (
        "the old deny-list did not match U+0085 — this is the defect"
    )
    rendered_by_the_old_code = f"https://{OTHER_DOMAIN}{escaped}"
    assert rendered_by_the_old_code.encode("latin-1").endswith(b"\x85"), (
        "and a bare 0x85 is what a latin-1 header encoding would have put on the wire"
    )
    with pytest.raises(PermalinkError, match="printable ASCII"):
        store_url(shop_domain=OTHER_DOMAIN, path=escaped)


def test_the_raises_clause_and_the_guard_describe_the_same_set() -> None:
    """Anti-drift: the docstring must keep naming the boundary the code enforces."""
    doc = store_url.__doc__ or ""
    assert "0x21" in doc and "0x7E" in doc, (
        "store_url's Raises clause must state the printable-ASCII boundary it enforces"
    )
    for code_point in (0x20, 0x7F, 0x85, 0x2028):
        char = chr(code_point)
        assert _FORBIDDEN_IN_PATH.search(char), f"U+{code_point:04X} must be forbidden"
    for code_point in (0x21, 0x2F, 0x7E):
        char = chr(code_point)
        assert not _FORBIDDEN_IN_PATH.search(char), f"U+{code_point:04X} must be allowed"


def test_the_response_splitting_subset_is_named_not_counted() -> None:
    """`NON_BARE_HOSTS`'s comment used to point at the wrong rows, and nothing noticed.

    It said "the last two are the response-splitting pair" and then described the trailing
    LF/CRLF pair — which sit at positions 15 and 16 of 18, so "the last two" actually named
    `embedded lf` and `header injection`. The table had grown past the sentence.

    So the subset is named in `RESPONSE_SPLITTING_HOSTS` and held to the table here, in
    both directions: a new CR/LF entry that is not named, or a named key that stops
    carrying one, is a red test rather than a quietly wrong comment.
    """
    carries_a_line_break = {
        key for key, host in NON_BARE_HOSTS.items() if "\n" in host or "\r" in host
    }
    assert carries_a_line_break == set(RESPONSE_SPLITTING_HOSTS), (
        "every CR/LF-bearing entry must be named in RESPONSE_SPLITTING_HOSTS and vice versa"
    )
    assert RESPONSE_SPLITTING_HOSTS <= set(NON_BARE_HOSTS), "a named key must exist in the table"
    # And the reason the subset is interesting at all: every one of them is refused.
    for key in RESPONSE_SPLITTING_HOSTS:
        with pytest.raises(PermalinkError):
            _assert_bare_host(NON_BARE_HOSTS[key])


# ---------------------------------------------------------------------------------------
# T-129 (stub 1): the documented set vs the ENFORCED set, not two substrings
# ---------------------------------------------------------------------------------------
#
# `test_the_raises_clause_and_the_guard_describe_the_same_set` above is T-118 (d)'s
# permanent fix and it cannot do what its name says. It reads `store_url.__doc__` for the
# single purpose of asserting that the substrings "0x21" and "0x7E" are present somewhere
# in it; every other assertion it makes is `_FORBIDDEN_IN_PATH` against itself. A Raises
# clause rewritten to "the path raises when it carries a control character, 0x21 and 0x7E
# excepted" — the exact prose the ticket existed to remove — keeps both substrings and
# stays green.
#
# The three tests below close that. Each derives the DOCUMENTED set from the live prose,
# derives the ENFORCED set by calling `store_url`, and compares the two. Neither half is
# read from the other, so prose drift and code drift are each a red test.

#: A stated boundary, in either of the two spellings permalink.py uses: ``0x21``-``0x7E``
#: inside the Raises clause and ``(0x21-0x7E)`` inside `_FORBIDDEN_IN_PATH`'s comment.
_STATED_BOUNDARY = re.compile(r"0x(?P<low>[0-9A-Fa-f]{2})(?:``)?-(?:``)?0x(?P<high>[0-9A-Fa-f]{2})")

#: A stated ``U+XXXX``-``U+XXXX`` range, as the Raises clause writes the C1 block.
_STATED_UNICODE_RANGE = re.compile(r"U\+(?P<low>[0-9A-F]{4})``-``U\+(?P<high>[0-9A-F]{4})")

#: The code points the enforced set is measured over: the whole of Latin-1 and the two
#: blocks past it that carry the interesting separators and format characters, plus a
#: sample from further up so "ASCII only" is distinguished from "BMP only".
_PROBE_CODE_POINTS = sorted(
    set(range(0x00, 0x300))
    | {0x2028, 0x2029, 0x202E, 0x200B, 0xFEFF, 0x4E2D, 0xFFFD, 0x10000, 0x1F600, 0x10FFFF}
)


def _stated_boundaries(text: str) -> set[tuple[int, int]]:
    """Every printable-ASCII boundary `text` states, as ``(low, high)`` code-point pairs."""
    return {
        (int(match.group("low"), 16), int(match.group("high"), 16))
        for match in _STATED_BOUNDARY.finditer(text)
    }


def _enforced_allowed() -> set[int]:
    """The code points `store_url` actually accepts in a path. Measured, never read."""
    allowed = set()
    for code_point in _PROBE_CODE_POINTS:
        try:
            store_url(shop_domain=OTHER_DOMAIN, path=f"/checkouts/abc{chr(code_point)}")
        except PermalinkError:
            continue
        allowed.add(code_point)
    return allowed


def test_the_documented_boundary_is_the_boundary_store_url_enforces() -> None:
    """The docstring's set and the guard's set, compared as sets.

    The documented set is `low..high` parsed out of the live Raises clause; the enforced
    set is whatever `store_url` does not raise on. Widening either one alone is red.
    """
    stated = _stated_boundaries(store_url.__doc__ or "")
    assert len(stated) == 1, (
        f"store_url's Raises clause must state exactly one code-point boundary, found {stated}"
    )
    low, high = stated.pop()
    documented_allowed = {cp for cp in _PROBE_CODE_POINTS if low <= cp <= high}
    assert documented_allowed, "a boundary that documents an empty allow-list is not a boundary"
    assert _enforced_allowed() == documented_allowed, (
        "store_url's Raises clause and _FORBIDDEN_IN_PATH describe different sets"
    )


def test_every_boundary_the_module_states_is_the_same_boundary() -> None:
    """The prose says it twice — the Raises clause and `_FORBIDDEN_IN_PATH`'s comment.

    Updating one and not the other is exactly how T-118 (d)'s drift started, one line
    below the comment warning against it.
    """
    source = inspect.getsource(permalink_module)
    stated = _stated_boundaries(source)
    assert len(stated) == 1, f"the module states more than one boundary: {sorted(stated)}"
    low, high = stated.pop()
    occurrences = len(_STATED_BOUNDARY.findall(source))
    assert occurrences >= 2, (
        f"the boundary is stated in both the constant's comment and the Raises clause; "
        f"found {occurrences} statement(s) — the parser has stopped seeing one of them"
    )
    assert _enforced_allowed() == {cp for cp in _PROBE_CODE_POINTS if low <= cp <= high}


def test_the_c1_range_the_clause_names_is_refused_and_really_is_c1() -> None:
    """The clause's other measurable claim, taken from the prose rather than restated here.

    It says the **C1 controls** ``U+0080``-``U+009F`` are control characters that used to
    be accepted. Both halves are checked against the live range in the docstring: every
    code point in it is Unicode category ``Cc``, the old deny-list did not match it, and
    `store_url` refuses it now.
    """
    doc = store_url.__doc__ or ""
    ranges = {
        (int(match.group("low"), 16), int(match.group("high"), 16))
        for match in _STATED_UNICODE_RANGE.finditer(doc)
    }
    assert ranges == {(0x80, 0x9F)}, (
        f"the Raises clause must name the C1 block as U+0080-U+009F, found {sorted(ranges)}"
    )
    low, high = ranges.pop()
    old_deny_list = re.compile(r"[\x00-\x20\x7f]")
    for code_point in range(low, high + 1):
        char = chr(code_point)
        assert unicodedata.category(char) == "Cc", (
            f"U+{code_point:04X} is documented as a C1 control but is not category Cc"
        )
        assert old_deny_list.search(char) is None, (
            f"U+{code_point:04X} is documented as having been accepted by the old deny-list"
        )
        with pytest.raises(PermalinkError):
            store_url(shop_domain=OTHER_DOMAIN, path=f"/checkouts/abc{char}")


# ---------------------------------------------------------------------------------------
# T-129 (stub 4): what the C1 gap actually was, and what it actually would have done
# ---------------------------------------------------------------------------------------
#
# `_FORBIDDEN_IN_PATH`'s replacement comment asserted, as observed fact, that U+0085
# "passed straight through into a `Location` header, where Starlette's latin-1 header
# encoding put a bare 0x85 byte on the wire" — and that sentence is what turned a
# documentation ticket into a behaviour change. It is not true of this service. The two
# tests below hold the corrected sentence to the code: the gap was LATENT (no caller can
# put anything into a `store_url` path), and the CONSEQUENCE is real but conditional
# (measured against the live response class, not assumed).

#: Every expression a ``store_url(path=...)`` f-string in this package is allowed to
#: interpolate, with why each is safe. Anything else is a caller-reachable path, which is
#: the situation `_FORBIDDEN_IN_PATH`'s comment used to claim already existed.
_SAFE_PATH_INTERPOLATIONS = {
    "checkout.token": "orders.new_token() -> uuid.uuid4().hex",
    "order.checkout_token": "the same token, carried onto the order",
    "variant": "build_permalink refuses it unless str.isdigit()",
    "quantity": "build_permalink refuses it unless >= 1, and it is an int",
}

#: The modules that call ``store_url``. Named rather than discovered so that a new caller
#: in a module nobody added here is caught by the count assertion below.
_STORE_URL_CALLERS = (app_module, orders_module, permalink_module, telemetry_module)


def _store_url_path_arguments() -> list[tuple[str, int, ast.expr]]:
    """Every ``path=`` argument passed to ``store_url`` anywhere in the package."""
    found: list[tuple[str, int, ast.expr]] = []
    for module in _STORE_URL_CALLERS:
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "store_url":
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            assert "path" in keywords, (
                f"{module.__name__}:{node.lineno} calls store_url without a keyword path; "
                "this test can no longer see what it renders"
            )
            found.append((module.__name__, node.lineno, keywords["path"]))
    return found


def _raw_location_header_sites(module: Any) -> int:
    """How many responses in `module` are built with a literal ``Location`` header entry.

    The distinction the prose turns on: a raw header dict is written to the wire latin-1,
    while `RedirectResponse` percent-encodes the url it is given.
    """
    tree = ast.parse(inspect.getsource(module))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "headers" or not isinstance(keyword.value, ast.Dict):
                continue
            keys = [
                key.value
                for key in keyword.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
            if any(key.lower() == "location" for key in keys):
                count += 1
    return count


def test_no_caller_supplied_value_reaches_a_store_url_path() -> None:
    """The corrected claim: the C1 gap was latent, because no path is caller-supplied.

    `store_url` renders ``https://{shop_domain}{path}``. `shop_domain` is guarded by
    `_LABEL`, an allow-list of DNS labels, so a control character cannot arrive that way
    whatever `_FORBIDDEN_IN_PATH` says. `path` is the other half, and every one of the
    four call sites builds it from a hex UUID or a digit run — so a bad byte could not
    reach a rendered URL through this service at all, under the old deny-list or the new
    allow-list.

    Checked structurally rather than by driving inputs, because "no input reaches it" is a
    claim about every input. A route added tomorrow that interpolates a query parameter
    would make the comment's original sentence true; that route is what turns this red.
    """
    call_sites = _store_url_path_arguments()
    assert len(call_sites) == 4, (
        f"the comment names four call sites; found {len(call_sites)}: "
        f"{[(module, line) for module, line, _ in call_sites]}"
    )
    for module_name, lineno, path in call_sites:
        where = f"{module_name}:{lineno}"
        if isinstance(path, ast.Constant):
            assert isinstance(path.value, str), f"{where}: a non-string constant path"
            continue
        assert isinstance(path, ast.JoinedStr), (
            f"{where}: path is {ast.unparse(path)!r}, which this test cannot vouch for; "
            "a store_url path must be a literal or an f-string over known-safe values"
        )
        for part in path.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            expression = ast.unparse(part.value)
            assert expression in _SAFE_PATH_INTERPOLATIONS, (
                f"{where} interpolates {expression!r} into a rendered URL path. If that "
                "value can carry anything a client sent, the C1 gap stops being latent — "
                f"add it here with a reason, or stop interpolating it. Known safe: "
                f"{sorted(_SAFE_PATH_INTERPOLATIONS)}"
            )


def test_what_a_bad_byte_does_to_a_location_header() -> None:
    """The consequence half, measured against the real response class, not assumed.

    The comment states two outcomes for a character that got past the path guard: a
    ``U+0085`` becomes a bare ``0x85`` byte, because Starlette encodes header values
    latin-1, and anything above ``U+00FF`` raises ``UnicodeEncodeError`` from the response
    constructor. Both are framework behaviour the prose cannot keep true on its own — a
    Starlette that percent-encoded ``Location`` instead would silently falsify it, which
    is how a comment ends up describing a world that no longer exists.
    """
    nel = chr(0x85)
    response = JSONResponse(
        status_code=303,
        content={},
        headers={"Location": f"https://{OTHER_DOMAIN}/checkouts/abc{nel}"},
    )
    location = dict(response.raw_headers)[b"location"]
    assert location.endswith(b"\x85"), (
        f"latin-1 header encoding must put the bare byte on the wire, got {location!r}"
    )
    assert location.decode("latin-1").endswith(nel)

    # And the other half: above U+00FF there is no latin-1 byte at all, so the response
    # constructor raises rather than rendering. `starlette/responses.py` is named in the
    # assertion because "a 500 from somewhere" is the part the comment gets specific about
    # — the old wording said "inside the ASGI server", which is a different place.
    line_separator = chr(0x2028)
    with pytest.raises(UnicodeEncodeError) as excinfo:
        JSONResponse(
            status_code=303,
            content={},
            headers={"Location": f"https://{OTHER_DOMAIN}/checkouts/abc{line_separator}"},
        )
    frames = [frame.filename for frame in traceback.extract_tb(excinfo.tb)]
    assert any("starlette/responses.py" in filename for filename in frames), (
        f"the raise is documented as coming from Response.__init__; frames were {frames}"
    )
    assert "latin-1" in str(excinfo.value)

    # Both outcomes belong to the emission style app.py actually uses — a raw
    # `headers={"Location": ...}`. `RedirectResponse` percent-encodes its url, so neither
    # the bare byte nor the UnicodeEncodeError happens there. Asserting the contrast
    # rather than only the outcome is what stops this test from reading as a fact about
    # Starlette in general, which it is not.
    quoted = RedirectResponse(url=f"https://{OTHER_DOMAIN}/checkouts/abc{nel}", status_code=303)
    assert dict(quoted.raw_headers)[b"location"].endswith(b"%C2%85"), (
        "RedirectResponse quotes; the claim is about the raw header the cart route sets"
    )
    assert b"\x85" not in dict(quoted.raw_headers)[b"location"]
    assert _raw_location_header_sites(app_module) == 1, (
        "the cart route must still set Location as a raw header dict for that claim to "
        "hold; if it moves to RedirectResponse (which quotes) the comment in permalink.py "
        "has to move with it"
    )
