"""The contract with the rest of the repo, and the seeding surface T-080 needs.

This ticket has **zero acceptance tests in the frozen suite** — the alias
``services.shopify_stub`` is registered there but nothing ever imports through it — so
nothing outside this directory pins any name the stub chooses. Two things nonetheless *are*
pinned from outside, and this file exercises both against the real fixture rather than
against a copy of it:

* ``shopify_stub.app:app``, which the root ``conftest.py``'s ``shopify_stub_url`` fixture
  imports by that exact path and serves. That fixture ``pytest.skip``s when the import
  fails, so a broken app would leave a *green* suite everywhere else in the repo. The test
  below asserts the fixture actually produced a working server, which converts that silent
  skip into a failure here.
* ``services/shopify-stub/fixtures/recorded`` existing, which the frozen scaffold smoke test
  checks.

The seeding tests cover the other outward-facing obligation: T-080 depends on this ticket
and nothing else, and its acceptance says the generator seeds stores *into shopify-stub* and
that ``make demo-seed`` is **idempotent against the stub**. Idempotency is therefore a
property of this stub, not of the seeder.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

import fastapi.routing as fastapi_routing
import httpx
import pytest
from shopify_stub import app as app_module
from shopify_stub import state as state_module
from shopify_stub.state import DEFAULT_ACCESS_TOKEN
from shopify_stub.testing import SEED_VARIANT, SUBSCRIBE_MUTATION, StubClient

VARIANT_ID = int(SEED_VARIANT["variant_id"])


async def test_the_root_fixture_serves_a_working_stub(shopify_stub_url: str) -> None:
    """The pinned import path really is served, not merely importable.

    ``shopify_stub_url`` skips rather than fails when ``shopify_stub.app:app`` cannot be
    imported. A skip is indistinguishable from a pass in the metrics, so this test does the
    work the fixture declines to: it makes a request and checks the answer.
    """
    async with httpx.AsyncClient(base_url=shopify_stub_url) as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        graphql = await client.post(
            "/admin/api/2026-07/graphql.json",
            json={"query": "query { orders(first: 1) { edges { cursor } } }"},
            headers={"X-Shopify-Access-Token": DEFAULT_ACCESS_TOKEN},
        )
        assert graphql.status_code == 200
        body = graphql.json()
        assert "errors" not in body, body
        # Not `"orders" in data` — that is an is-not-None-grade check that any garbage
        # value satisfies. The connection has to be well formed.
        connection = body["data"]["orders"]
        assert isinstance(connection["edges"], list)
        assert set(connection["pageInfo"]) == {
            "hasNextPage",
            "hasPreviousPage",
            "startCursor",
            "endCursor",
        }

        # And the auth path must really be enforced on this instance, not just on the
        # per-test stubs — otherwise "the pinned entry point works" would be proven by an
        # endpoint that answers anyone.
        unauthenticated = await client.post(
            "/admin/api/2026-07/graphql.json",
            json={"query": "query { orders(first: 1) { edges { cursor } } }"},
        )
        assert unauthenticated.status_code == 401


def test_the_recorded_fixtures_directory_is_where_d21_puts_it() -> None:
    """D21 fixes this ticket's recordings at ``services/shopify-stub/fixtures/recorded/**``."""
    from shopify_stub.recordings import RECORDINGS_DIR

    assert RECORDINGS_DIR.is_dir()
    assert RECORDINGS_DIR.parts[-3:] == ("shopify-stub", "fixtures", "recorded")
    assert list(RECORDINGS_DIR.glob("*.json")), "the directory must not be empty"


async def test_two_stubs_in_one_process_do_not_share_state(stub: StubClient) -> None:
    """Per-test isolation, asserted rather than assumed.

    The root fixture promises each test owns its own stub state — the drop-rate knob and the
    discount-code table. That promise is only kept if the state hangs off the app instance
    and not off a module global, and a module global would pass every other test in this
    suite while silently leaking one test's codes into the next.
    """
    from shopify_stub.app import create_app

    from proxyshop_support.asgi_server import serve

    await stub.create_code("PSX-ISOLATE1")
    assert "PSX-ISOLATE1" in (await stub.codes())["codes"]

    with serve(create_app()) as other_url:
        async with httpx.AsyncClient(base_url=other_url) as client:
            other = StubClient(client, other_url)
            assert (await other.codes())["codes"] == {}
            await other.configure(pixel_drop_rate=1.0)
            assert (await other.config())["pixel_drop_rate"] == 1.0

    assert (await stub.config())["pixel_drop_rate"] == 0.0


async def test_reset_clears_state_but_keeps_configuration(stub: StubClient) -> None:
    await stub.configure(pixel_drop_rate=0.25, pixel_seed=7)
    await stub.create_code("PSX-RESETME1")
    await stub.buy(VARIANT_ID)

    assert (await stub.http.post("/_stub/reset")).status_code == 200
    assert (await stub.codes())["codes"] == {}
    assert await stub.events() == []
    assert (await stub.config())["pixel_drop_rate"] == 0.25, (
        "reset wipes data, not configuration: a test that configured a stub and then reset "
        "it must not silently get the defaults back"
    )


# ---------------------------------------------------------------------------------------
# Seeding — T-080's dependency
# ---------------------------------------------------------------------------------------


async def test_seeding_is_idempotent(stub: StubClient) -> None:
    """``make demo-seed`` must be safe to run twice (T-080 acceptance 3).

    An identical re-seed reports ``unchanged``, not ``created`` — so a caller can tell "the
    seed was already applied" from "the seed applied again", which is the whole content of
    the idempotency claim.
    """
    payload = [SEED_VARIANT, {**SEED_VARIANT, "variant_id": 44352914, "sku": "TR-43"}]

    first = (await stub.seed(payload)).json()
    assert first == {"created": 1, "unchanged": 1, "updated": 0}, (
        "the fixture already seeded SEED_VARIANT, so only the second is new"
    )

    second = (await stub.seed(payload)).json()
    assert second == {"created": 0, "unchanged": 2, "updated": 0}

    changed = (await stub.seed([{**SEED_VARIANT, "price": "120.00"}])).json()
    assert changed == {"created": 0, "unchanged": 0, "updated": 1}


async def test_a_seeded_variant_is_what_the_cart_prices(stub: StubClient) -> None:
    """The seed is the catalog the permalink resolves against, not decoration."""
    await stub.seed([{**SEED_VARIANT, "variant_id": 555001, "price": "42.50"}])
    cart = (await stub.visit_cart(555001, quantity=2)).json()
    assert cart["items_subtotal_price"] == "85.00"
    assert cart["items"][0]["price"] == "42.50"


async def test_an_unavailable_variant_cannot_be_bought(stub: StubClient) -> None:
    await stub.seed([{**SEED_VARIANT, "variant_id": 555002, "available": False}])
    assert (await stub.visit_cart(555002)).status_code == 404


async def test_a_malformed_seed_is_refused_without_partial_application(
    stub: StubClient,
) -> None:
    """A seed that half-applied would leave the store in a state no run can reproduce."""
    response = await stub.seed(
        [{**SEED_VARIANT, "variant_id": 555003}, {"variant_id": "not-a-number"}]
    )
    assert response.status_code == 400
    assert (await stub.visit_cart(555003)).status_code == 404, (
        "nothing from a refused seed may survive"
    )


async def test_seed_requires_a_variants_list(stub: StubClient) -> None:
    assert (await stub.http.post("/_stub/seed", json={})).status_code == 400
    assert (await stub.http.post("/_stub/seed", json={"variants": 3})).status_code == 400


# ---------------------------------------------------------------------------------------
# The Admin API version in the URL
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version",
    ["2019-04", "2025-01", "9999-99", "not-a-version", "unstable", "v1"],
)
async def test_an_admin_api_version_the_stub_does_not_serve_is_a_404(
    stub: StubClient, version: str
) -> None:
    """A version the stub does not model must not answer 200 with real data.

    ``{version}`` was captured and immediately discarded, so a consumer pinned to a retired
    version — or carrying a typo — got a full, plausible, correct-looking response here and a
    404 from Shopify, with nothing in between to tell it. That is the stub being *looser*
    than the API it stands in for, in the one direction that costs a production incident.
    """
    response = await stub.graphql(
        "query { orders(first: 1) { edges { cursor } } }", version=version
    )
    assert response.status_code == 404, response.text
    body = response.json()
    assert "data" not in body
    # Shopify's HTTP-layer errors are a bare string, not the list GraphQL-level errors use.
    assert isinstance(body["errors"], str)
    assert "2026-07" in body["errors"], "the message must name the version the stub does serve"


async def test_the_configured_version_is_the_one_that_answers(stub: StubClient) -> None:
    """Both directions: the configured version works, and moving it moves the door.

    The version is a configured, first-class value — it is echoed in every webhook's
    ``X-Shopify-API-Version`` header and in ``webPixelCreate``'s ``apiVersion.handle``. A
    stub that answered on versions it then contradicts in its own headers is disagreeing with
    itself.
    """
    configured = (await stub.config())["api_version"]
    assert configured == "2026-07"
    ok = await stub.graphql("query { orders(first: 1) { edges { cursor } } }", version=configured)
    assert ok.status_code == 200

    await stub.configure(api_version="2027-01")
    assert (await stub.config())["api_version"] == "2027-01"
    stale = await stub.graphql("query { orders(first: 1) { edges { cursor } } }", version="2026-07")
    assert stale.status_code == 404
    fresh = await stub.graphql("query { orders(first: 1) { edges { cursor } } }", version="2027-01")
    assert fresh.status_code == 200
    assert fresh.json()["data"]["orders"] is not None, (
        "the version gate must not change what a valid request answers"
    )


async def test_the_version_that_answers_is_the_version_the_stub_stamps_on_its_output(
    stub: StubClient,
) -> None:
    """The coherence the gate exists to protect, asserted end to end."""
    await stub.configure(api_version="2027-01")
    response = await stub.graphql(
        SUBSCRIBE_MUTATION,
        {"topic": "ORDERS_PAID", "sub": {"uri": "https://example.test/hook"}},
        version="2027-01",
    )
    subscription = response.json()["data"]["webhookSubscriptionCreate"]["webhookSubscription"]
    assert subscription["apiVersion"]["handle"] == "2027-01"


# ---------------------------------------------------------------------------------------
# T-129 (stub 3): the module route table is the served surface, not a summary of it
# ---------------------------------------------------------------------------------------
#
# `shopify_stub.app`'s module docstring is the only index of this service's HTTP surface —
# there is no OpenAPI page (`docs_url` and `redoc_url` are both None), so a consumer reads
# the docstring or reads every decorator. It drifted: `GET /_stub/codes` was registered at
# `@router.get("/codes")` and served from the day the route landed, and appeared in neither
# group of the table; the delivery-log commit edited that exact table and did not notice.
#
# The two tests below close the drift in both directions by comparing the prose to the
# LIVE application rather than to another copy of the prose, so a documented route that is
# not served and a served route that is not documented are each a red test.
#
# W5 adversarial, both halves of that comparison were wrong in the same way — each could be
# satisfied without the thing it claims to measure:
#
#   * the DOCUMENTED half scanned the whole docstring for any backticked ``METHOD /path``,
#     so the narrative paragraph *underneath* the table — the one that names
#     ``GET /_stub/codes`` as the route the table used to omit — counted as documentation.
#     Deleting the table row left that sentence standing and both tests stayed green: the
#     defect this section exists to prevent, reintroducible with no test noticing. The
#     inline pattern is now anchored to the ``**Operational**:`` line, which is the only
#     prose line that is allowed to declare a route; and
#   * the SERVED half read `create_app().openapi()`, which enumerates the schema rather
#     than the routing table. A route declared ``include_in_schema=False`` is served and
#     absent from that schema, so one keyword argument made a route invisible to the very
#     check that exists to notice undocumented routes. It now walks `application.routes`.
#
# `test_the_route_table_names_the_control_plane_route_that_was_missing` re-applies both
# defects to a copy of the docstring / a copy of the app and asserts each derivation still
# sees them, so neither hole can reopen quietly.

#: A row of either reStructuredText table: ``` ``/path`` ``` then the method column.
#: ``GET/PUT`` is one row with two methods, so the method cell is split on ``/``.
_TABLE_ROW = re.compile(r"^``(?P<path>/[^`]+)``\s+(?P<methods>[A-Z]+(?:/[A-Z]+)*)(?:\s|$)", re.M)

#: The one prose line allowed to declare routes: "**Operational**: ``GET /healthz``.".
#: The ``**Operational**:`` prefix is part of the pattern and must sit on the SAME line —
#: without it this matched any backticked ``METHOD /path`` anywhere in the docstring,
#: including the paragraph below the table that names ``GET /_stub/codes`` in prose.
_OPERATIONAL_LINE = re.compile(r"^\*\*Operational\*\*:(?P<routes>.*)$", re.M)

#: A ``METHOD /path`` pair inside the operational line. Applied ONLY to that line's text.
_INLINE_ROUTE = re.compile(r"``(?P<methods>[A-Z]+(?:/[A-Z]+)*) (?P<path>/[^`]+)``")

#: FastAPI mounts its own schema endpoint at ``application.openapi_url``. It is framework
#: furniture rather than part of the surface this service designed, so it is excluded from
#: the served set — by that attribute, never by ``include_in_schema``, which is the exact
#: keyword this derivation exists to be blind-proof against.


def _documented_surface(doc: str | None = None) -> set[tuple[str, str]]:
    """``(method, path)`` pairs named in ``shopify_stub.app``'s module docstring.

    ``doc`` defaults to the live docstring. It is a parameter so a test can hand the parser
    a *mutated* docstring — the table row deleted, the prose left standing — and prove the
    parser still reports the route as undocumented.
    """
    text = (app_module.__doc__ or "") if doc is None else doc
    documented: set[tuple[str, str]] = set()
    for match in _TABLE_ROW.finditer(text):
        for method in match.group("methods").split("/"):
            documented.add((method, match.group("path")))
    for line in _OPERATIONAL_LINE.finditer(text):
        for match in _INLINE_ROUTE.finditer(line.group("routes")):
            for method in match.group("methods").split("/"):
                documented.add((method, match.group("path")))
    return documented


def _schema_surface(application: Any) -> set[tuple[str, str]]:
    """``(method, path)`` pairs ``openapi()`` reports. **Not** the served surface.

    Kept only so :func:`test_the_module_route_table_is_the_served_surface` can assert the
    containment that proves the routing walk below is not under-reporting.
    """
    return {
        (method.upper(), path)
        for path, operations in application.openapi()["paths"].items()
        for method in operations
    }


def _served_surface(application: Any = None) -> set[tuple[str, str]]:
    """``(method, path)`` pairs the freshly-built application actually routes.

    Derived from the ROUTING TABLE, not from ``openapi()``: the schema omits every route
    declared ``include_in_schema=False``, and those are served all the same, so a
    schema-derived surface hides an undocumented route behind one keyword argument.

    ``fastapi.routing.iter_route_contexts`` is the framework's own flattening of
    ``app.routes`` — necessary because an ``include_router`` call no longer leaves the child
    routes at the top level, it leaves an ``_IncludedRouter`` wrapper whose paths are not
    reachable through ``route.path``. Falling back to a plain walk keeps this working if
    that helper is ever withdrawn; the containment assertion in the test is what would
    catch either walk going blind.
    """
    app_under_test = app_module.create_app() if application is None else application
    schema_path = getattr(app_under_test, "openapi_url", None)

    entries: list[tuple[str | None, Any]] = []
    flatten = getattr(fastapi_routing, "iter_route_contexts", None)
    if flatten is not None:
        entries = [
            (getattr(ctx, "path", None), getattr(ctx, "methods", None))
            for ctx in flatten(app_under_test.routes)
        ]
    else:  # pragma: no cover - only on a FastAPI without the flattening helper
        entries = [
            (getattr(route, "path", None), getattr(route, "methods", None))
            for route in app_under_test.routes
        ]

    served: set[tuple[str, str]] = set()
    for path, methods in entries:
        # FastAPI mounts its own schema endpoint at `openapi_url`; that is framework
        # furniture, not part of the surface this service designed. Excluded by that
        # attribute and never by `include_in_schema`, which is the keyword this derivation
        # exists to be blind-proof against.
        if path is None or methods is None or path == schema_path:
            continue
        for method in methods:
            # HEAD and OPTIONS are synthesised by the framework for every GET route; the
            # docstring documents the methods the service declares.
            if method in {"HEAD", "OPTIONS"}:
                continue
            served.add((method, path))
    return served


def test_the_module_route_table_is_the_served_surface() -> None:
    """Every served route is documented and every documented route is served.

    Set equality, not containment: containment in one direction lets a route be added
    without a table row (the `/_stub/codes` defect) and containment in the other lets a
    row outlive the route it names.
    """
    application = app_module.create_app()
    documented = _documented_surface()
    served = _served_surface(application)
    assert documented, "the docstring parser found no routes at all — it has stopped working"
    # The routing walk must never report FEWER routes than the schema does. A walk that
    # went blind — the `_IncludedRouter` wrapper this code had to learn about, a future
    # nesting change — would otherwise make every undocumented route vanish silently,
    # which is the same failure as reading `openapi()` in the first place.
    schema = _schema_surface(application) - {("GET", application.openapi_url or "")}
    assert schema <= served, (
        f"the routing walk is missing routes the schema knows about: {sorted(schema - served)}"
    )
    assert served - documented == set(), (
        f"served but undocumented in shopify_stub.app's route table: {sorted(served - documented)}"
    )
    assert documented - served == set(), (
        f"documented in the route table but not served: {sorted(documented - served)}"
    )


def test_the_route_table_names_the_control_plane_route_that_was_missing() -> None:
    """The specific regression, named — and both derivations mutated to prove they see it.

    ``GET /_stub/codes`` is what the drift hid. The first two assertions state the fact.
    The rest are what stop this test from being dead weight beside the set comparison
    above: they re-apply the original defect to a *copy* of each input and require the
    derivation to still report it.

    1. Delete only the table row from a copy of the docstring, leaving the narrative
       paragraph that names ``GET /_stub/codes`` in prose. That is the exact state that
       used to keep both tests green, because the docstring was scanned whole for any
       backticked ``METHOD /path``. The route must now read as undocumented.
    2. Register a route with ``include_in_schema=False`` on a copy of the app. It is
       served; ``openapi()`` cannot see it. The served set must.

    Both are copies — the live docstring and the live app are untouched.
    """
    assert ("GET", "/_stub/codes") in _served_surface(), "the route is registered and served"
    assert ("GET", "/_stub/codes") in _documented_surface(), (
        "and the control-plane table must name it"
    )

    doc = app_module.__doc__ or ""
    table_row = next(
        line for line in doc.splitlines() if line.startswith("``/_stub/codes``") and "GET" in line
    )
    without_the_row = doc.replace(table_row + "\n", "")
    assert without_the_row != doc, "the mutation must actually remove the row"
    assert "``GET /_stub/codes``" in without_the_row, (
        "the narrative paragraph naming the route in prose must survive the mutation — "
        "it is the input that used to satisfy this test on its own"
    )
    assert ("GET", "/_stub/codes") not in _documented_surface(without_the_row), (
        "deleting the table row must make the route undocumented; prose below the table "
        "is not the route table, and counting it is how the /_stub/codes drift stayed "
        "invisible to its own regression test"
    )
    assert ("GET", "/_stub/config") in _documented_surface(without_the_row), (
        "and the parser must still read the rest of the table — a mutation that breaks "
        "the parser outright would satisfy the assertion above for the wrong reason"
    )

    hidden = app_module.create_app()

    @hidden.get("/_stub/not-in-the-schema", include_in_schema=False)
    async def _hidden() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    assert ("GET", "/_stub/not-in-the-schema") not in {
        (method.upper(), path)
        for path, operations in hidden.openapi()["paths"].items()
        for method in operations
    }, "include_in_schema=False is what makes a served route invisible to openapi()"
    assert ("GET", "/_stub/not-in-the-schema") in _served_surface(hidden), (
        "the served surface must be the routing table, not the schema: a route hidden "
        "from openapi() is still served, and would otherwise never need a table row"
    )


# ---------------------------------------------------------------------------------------
# T-129 (adversarial): DEFAULT_API_VERSION's comment described the route it replaced
# ---------------------------------------------------------------------------------------
#
# `shopify_stub.state.DEFAULT_API_VERSION` carried "Callers may use any version segment in
# the URL" long after `admin_graphql` began answering 404 for every version but the
# configured one — reproduced by driving a live stub: 2026-07 -> 200, and 2025-01,
# 2099-99 and "banana" -> 404 each. That is the same defect class as the rest of T-129: a
# sentence that survived the change it described, in the one file a consumer reads to find
# out what version to send.
#
# The corrected comment names the refusal status. This test reads that number back out of
# the comment and compares it to what the route actually answers, so the prose is now held
# to the code rather than sitting beside it.

#: The refusal status, as the ``#:`` block above ``DEFAULT_API_VERSION`` states it.
_DOCUMENTED_REFUSAL = re.compile(r"refused with \*\*(?P<status>\d{3})\*\*")


def _documented_version_refusal_status() -> int:
    """The status `state.py`'s own comment promises for an unserved API version.

    The ``#:`` block is unwrapped to one line before matching, so the sentence may be
    rewrapped by a formatter without changing what this reads.
    """
    source = inspect.getsource(state_module)
    anchor = source.index("DEFAULT_API_VERSION = ")
    lines = source[:anchor].splitlines()
    block: list[str] = []
    for line in reversed(lines):
        if not line.startswith("#:"):
            break
        block.append(line[2:].strip())
    prose = " ".join(reversed(block))
    matches = _DOCUMENTED_REFUSAL.findall(prose)
    assert len(matches) == 1, (
        "DEFAULT_API_VERSION's comment must state exactly one refusal status for an "
        f"unserved version; found {matches}. It used to state none at all, and said "
        '"callers may use any version segment in the URL" instead.'
    )
    return int(matches[0])


async def test_the_documented_version_refusal_is_the_one_the_route_gives(
    stub: StubClient,
) -> None:
    """The comment's promise, checked against the live route in both directions."""
    documented = _documented_version_refusal_status()
    assert documented != 200, "a comment documenting success for an unserved version is the bug"

    served = (await stub.config())["api_version"]
    ok = await stub.graphql("query { orders(first: 1) { edges { cursor } } }", version=served)
    assert ok.status_code == 200, "the configured version must still answer"

    for other in ("2019-04", "2025-01", "9999-99", "banana"):
        assert other != served
        response = await stub.graphql(
            "query { orders(first: 1) { edges { cursor } } }", version=other
        )
        assert response.status_code == documented, (
            f"state.py documents {documented} for an unserved version; "
            f"{other!r} answered {response.status_code}"
        )
