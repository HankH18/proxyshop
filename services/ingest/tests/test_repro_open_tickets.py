"""Reproduction gates for the open `services/ingest` findings.

Every test here asserts the behaviour that SHOULD hold and therefore fails against the tree as
it stands. Each carries ``xfail(strict=True)`` so an ordinary run reports ``xfailed`` and the
repo-wide build gate stays green, while the ticket's own gate
(``pytest <file> -q --runxfail -k <name>``) reports a real failure with the test SELECTED. When
the defect is repaired the test XPASSes, which ``strict=True`` turns into a failure — so the
marker cannot outlive the bug.

The storefront fixtures (``storefront``, ``storefront_factory``) come from
``_fixtures_storefront.py``, which this directory's conftest auto-loads; under pytest's
``importlib`` import mode a test module cannot import a sibling helper by name, so everything
reaches these tests through the yielded ``StorefrontStub``.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest
from ingest.adapters import (
    CatalogRequest,
    FetchPolicy,
    FetchRefused,
    SafeHTTPClient,
    SignedFetchAdapter,
    native_product_key,
)

from proxyshop_support.asgi_server import serve

REPO_ROOT = Path(__file__).resolve().parents[3]
INGEST_SRC = REPO_ROOT / "services/ingest/src"

#: Loopback is not publicly routable, so the default SSRF posture refuses the in-process
#: storefront. Same constant the T-020 suite uses next door, for the same reason.
LOOPBACK = FetchPolicy(allowed_ports=None, extra_allowed_networks=("127.0.0.0/8",))

#: Every concrete implementation of the ``CatalogAdapter`` protocol in this service.
_ADAPTER_CLASSES = frozenset({"SignedFetchAdapter", "CatalogMCPAdapter"})

#: Functions that construct an adapter only to hand it to ``isinstance`` against the protocol.
#: They prove the class SHAPE matches and run none of its behaviour, so they are not wiring.
_TYPE_CHECK_HELPERS = frozenset({"_satisfies_catalog_adapter"})


def _client() -> SafeHTTPClient:
    return SafeHTTPClient(policy=LOOPBACK)


class _TwoFacedStore:
    """A storefront whose two surfaces price the same variant differently.

    Raw ASGI, like ``_fixtures_storefront.StorefrontStub`` next door and for the same reason:
    every property here is a wire-level one. It exists separately because that stub *derives*
    each product page's JSON-LD from the entry it serves in ``products.json``, so it can never
    make the two disagree — which is the whole of what T-249 is about.
    """

    HANDLE = "trail-runner-42"
    SKU = "TR-42-9"

    def __init__(self, *, entry_price: str, page_price: str) -> None:
        self.entry_price = entry_price
        self.page_price = page_price

    def _entry(self) -> dict[str, Any]:
        return {
            "id": 8123456,
            "title": "Trail Runner 42",
            "handle": self.HANDLE,
            "vendor": "Cascade",
            "variants": [
                {
                    "id": 44352913,
                    "title": "US 9",
                    "sku": self.SKU,
                    "price": self.entry_price,
                    "available": True,
                }
            ],
        }

    def _page(self) -> bytes:
        ld = {
            "@context": "https://schema.org/",
            "@type": "Product",
            "name": "Trail Runner 42",
            "brand": {"@type": "Brand", "name": "Cascade"},
            "offers": [
                {
                    "@type": "Offer",
                    "sku": self.SKU,
                    "price": self.page_price,
                    "priceCurrency": "USD",
                    "availability": "https://schema.org/InStock",
                }
            ],
        }
        return (
            '<!doctype html><html><head><script type="application/ld+json">'
            f"{json.dumps(ld)}</script></head><body></body></html>"
        ).encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        while True:
            message = await receive()
            if not message.get("more_body"):
                break

        path = scope["path"]
        if path == "/robots.txt":
            body, media = b"User-agent: *\nAllow: /\n", b"text/plain"
        elif path == "/products.json":
            body, media = json.dumps({"products": [self._entry()]}).encode(), b"application/json"
        elif path == f"/products/{self.HANDLE}":
            body, media = self._page(), b"text/html"
        else:
            body, media = b"not found", b"text/plain"

        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-type", media)]}
        )
        await send({"type": "http.response.body", "body": body})


def _modules_imported_by_the_app() -> dict[str, str]:
    """Every ``ingest.*`` module that building the real app pulls in, name -> file.

    Measured in a SUBPROCESS on purpose. In-process, ``sys.modules`` already holds whatever
    the rest of this directory's tests imported, and a reachability question answered against
    a polluted module table answers itself in the affirmative every time.

    The subprocess is given an explicit ``PYTHONPATH`` and its answers are checked against
    :data:`REPO_ROOT` by the caller, because this venv's ``site-packages/_proxyshop.pth``
    puts a checkout root and its ``.pkgroot`` on ``sys.path`` for every process that uses it —
    a probe that trusts the resolution it happens to get can measure a different tree than the
    one under test.
    """
    code = (
        "import json, sys\n"
        "import ingest.main\n"
        "ingest.main.create_app()\n"
        "print(json.dumps({name: getattr(module, '__file__', '') or ''\n"
        "                  for name, module in sys.modules.items()\n"
        "                  if name == 'ingest' or name.startswith('ingest.')}))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / ".pkgroot")])
    env["PROXYSHOP_WORKER"] = env.get("PROXYSHOP_WORKER", "0")
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, f"the ingest app would not build:\n{completed.stderr}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _adapter_constructions(
    path: Path, *, ignoring: frozenset[str] = _TYPE_CHECK_HELPERS
) -> list[str]:
    """Names of catalog adapters CONSTRUCTED in ``path``, ignoring the type-check helpers."""
    with warnings.catch_warnings():
        # Compiling someone else's source re-emits its SyntaxWarnings against this test.
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(path.read_text())
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in ignoring:
            skip.update(id(child) for child in ast.walk(node))

    built: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in skip:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in _ADAPTER_CLASSES:
            built.append(name)
    return built


# =============================================================================================
# T-236 — the ingestion pipeline does not exist as a running thing
# =============================================================================================


# MARKER REMOVED — T-236 is fixed. The marker read, verbatim:
#
#     @pytest.mark.xfail(strict=True, reason=(
#         "T-236: no production code constructs a CatalogAdapter — the only non-test "
#         "constructions of SignedFetchAdapter and CatalogMCPAdapter are the "
#         "_satisfies_catalog_adapter type-check helpers, so nothing the ingest app can run "
#         "reads a catalog; remove this marker with the fix"))
#
# What it encoded: no module `ingest.main.create_app()` imports builds a catalog adapter, so
# the ingestion pipeline exists as a library and as no running process. Its own reason text
# says to remove it with the fix, and `strict=True` means leaving it would turn the repair
# into a red build.
#
# Would this test still be wrong if my change were reverted? YES, and it was measured rather
# than assumed: with `services/ingest/src/scheduler/{catalog,routes}.py` moved out of the
# tree the test returns to `xfailed`, and with them restored it passes. The cause is
# `ingest.scheduler.routes`, a module `create_app`'s `*/routes.py` glob mounts, importing
# `ingest.scheduler.catalog`, which constructs `SignedFetchAdapter` and `CatalogMCPAdapter`
# in `build_catalog_adapter` and runs one end to end through `fetch_catalog` ->
# `to_upserts` -> `apply_upserts` behind `POST /refresh/{store_id}`.
#
# No assertion below is touched.
def test_the_running_ingest_app_can_reach_a_catalog_adapter_that_is_actually_built() -> None:
    """A capability the acceptance metric counts as met, that no running process performs.

    ``ingest.main.create_app`` globs ``<feature>/routes.py`` and mounts what it finds — today
    ``ingest.er.routes`` and ``ingest.extraction.routes``, six endpoints. Nothing those modules
    reach ever constructs a catalog adapter. Repo-wide, the only non-test constructions of
    either adapter are ``catalog_mcp._satisfies_catalog_adapter`` and
    ``signed_fetch._satisfies_catalog_adapter``, which build one purely to run ``isinstance``
    against the protocol and then throw it away, and ``services/ingest/src/scheduler/`` is a
    single empty ``__init__.py``.

    That matters beyond unfinished wiring because the frozen acceptance suite counts the
    capability as delivered: ``test_both_catalog_adapters_satisfy_one_interface`` compares
    ``inspect.signature`` parameter lists and never calls an adapter, so E2 is green over a
    pipeline that nothing runs.

    The assertion names the property, not the design: some module the app actually imports
    must build a catalog adapter. A scheduler, a route, a worker entrypoint — any of them
    passes. The module list is measured in a subprocess and every file in it is checked to be
    inside this tree first, so a stale ``.pth`` cannot answer the question with another
    checkout's code.
    """
    modules = _modules_imported_by_the_app()

    reachable: list[Path] = []
    for name, filename in sorted(modules.items()):
        if not filename:
            continue
        resolved = Path(filename).resolve()
        assert REPO_ROOT in resolved.parents, (
            f"{name} resolved to {resolved}, which is outside the tree under test "
            f"({REPO_ROOT}) — the probe measured the wrong checkout"
        )
        if INGEST_SRC in resolved.parents:
            reachable.append(resolved)

    assert reachable, "the app imported no ingest source at all; the probe is wrong"

    # Positive control for the scanner itself: with nothing ignored it DOES find the
    # construction inside `_satisfies_catalog_adapter`, so an empty result below means "no
    # adapter is built outside the type check" rather than "the scanner finds nothing".
    assert _adapter_constructions(INGEST_SRC / "adapters/catalog_mcp.py", ignoring=frozenset()) == [
        "CatalogMCPAdapter"
    ], "the AST scan cannot see a construction it is pointed straight at"

    built = {path: _adapter_constructions(path) for path in reachable}
    wired = {str(path.relative_to(REPO_ROOT)): names for path, names in built.items() if names}

    assert wired, (
        "no module the ingest app imports constructs a catalog adapter, so no running "
        f"process reads a catalog; app-reachable ingest modules were "
        f"{[str(p.relative_to(REPO_ROOT)) for p in reachable]}"
    )


# =============================================================================================
# T-238 — a hostile `Location` header reaches a bare `urlsplit`
# =============================================================================================


# MARKER REMOVED — T-238 is fixed. The marker read, verbatim:
#
#     @pytest.mark.xfail(strict=True, reason=(
#         "T-238: transport.py follows a redirect by calling urljoin on the server's Location "
#         "header, and urljoin parses with urlsplit, so a Location of 'http://[' raises "
#         "ValueError: Invalid IPv6 URL out of the fetch instead of the FetchRefused the guard "
#         "exists to produce; remove this marker with the fix"))
#
# What it encoded: the one URL in a crawl the hostile party writes reaches an unguarded parse,
# so a four-character Location header turns the guard's refusal into a traceback out of
# SafeHTTPClient.fetch — and out of SignedFetchAdapter.fetch_catalog with it, since that
# catches only (FetchRefused, TransportError).
#
# Would this test still be wrong if my change were reverted? YES, and it was measured: with
# `git show HEAD:` copies of adapters/transport.py and adapters/netguard.py back in place the
# test returns to `xfailed`, and with the fix restored it passes. The cause is the try/except
# around `urljoin(current, location)` in SafeHTTPClient.fetch, which now answers
# `FetchRefused(location, "unparseable-url:<exc>")` — the same reason vocabulary
# `fetch_verdict` already answers for an entry URL that will not parse.
#
# No assertion below is touched, including the positive control that a parseable Location is
# still followed.
def test_a_malformed_redirect_location_is_refused_rather_than_raised(storefront: Any) -> None:
    """The redirect target is the one URL in a crawl that the hostile party writes.

    Every other URL the fetcher handles has been through ``fetch_verdict``, which wraps its
    ``urlsplit`` in a try/except and answers ``unparseable-url:…``. The ``Location`` header does
    not: ``transport.py`` computes the next hop as ``urljoin(current, location)``, and
    ``urljoin`` parses with the same ``urlsplit`` — unguarded. ``urlsplit('http://[')`` raises
    ``ValueError: Invalid IPv6 URL``, so a store that answers one 302 with four characters
    turns a refusal into a traceback.

    Measured through this exact call: ``ValueError: Invalid IPv6 URL`` at the ``urljoin``,
    escaping ``SafeHTTPClient.fetch``. It escapes further than that — ``SignedFetchAdapter``
    catches only ``(FetchRefused, TransportError)`` around its fetches, so the same header
    breaks ``fetch_catalog``, whose docstring says it "never raises for an ordinary crawl
    outcome".

    The control below is the same route with a target that parses, which the guard follows
    normally — so the failure above is about the parse and not about redirects being blocked.
    Any repair passes: refuse the hop, or answer ``unparseable-url`` the way the entry check
    already does. What may not happen is a ``ValueError`` leaving the client.
    """
    base_url, stub = storefront

    stub.redirect_target = f"{base_url}/robots.txt"
    control = _client().fetch(f"{base_url}/redirect/custom")
    assert control.status == 200, "positive control: a parseable redirect must still be followed"

    stub.redirect_target = "http://["
    with pytest.raises(Exception) as raised:  # noqa: B017 - the TYPE is what is under test
        _client().fetch(f"{base_url}/redirect/custom")

    assert isinstance(raised.value, FetchRefused), (
        "a malformed Location header produced "
        f"{type(raised.value).__name__}({raised.value!r}) instead of a FetchRefused"
    )


# =============================================================================================
# T-245 — the shared mapping has never reached a graph, and the structural green hid this
# =============================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-245: catalog_mcp skips an entry whose native_product_key is empty while "
        "signed_fetch mints prod_<hash('')> for every such entry, so two unidentifiable "
        "products from one store collapse onto ONE graph node — a divergence the "
        "inspect.signature acceptance check cannot see; remove this marker with the fix"
    ),
)
def test_catalog_entries_with_no_identifier_do_not_collapse_onto_one_product_node(
    storefront_factory: Any,
) -> None:
    """Two products, one node — the write the structural interface check cannot detect.

    T-023's premise is that both catalog adapters map a snapshot through **one** shared
    mapping, so the MCP adapter matching the signed fetcher is structural rather than a second
    implementation. The acceptance test for that compares ``inspect.signature`` parameter lists
    and never calls either adapter, and the one test that would have replayed the ops against
    Neo4j is skipped whenever the compose stack is down. So the adapters have never been
    compared on what they actually *do*.

    They do not agree. Both read a product's identity through ``native_product_key``, which
    tries ``id``, ``product_id`` then ``handle`` and returns ``""`` when the entry names none
    of them. ``catalog_mcp.py`` checks for that and skips the entry with a warning. The signed
    fetcher does not check: it calls ``product_id_for(store_id, entry)`` regardless, which
    hashes the empty key, so **every** unidentifiable entry from a store gets the same
    ``prod_…`` id. Two different products then merge into one graph node — a store's whole
    unidentified catalog collapsing into a single product, silently, on the first real write.

    Either repair passes: skip the entries the way the MCP adapter does, or derive a distinct
    identity for them. What is refused is only the collision.
    """
    anonymous = [
        {"title": "Trail Runner 42", "variants": [{"sku": "A-1", "price": "129.95"}]},
        {"title": "Merino Hiking Sock", "variants": [{"sku": "B-1", "price": "18.00"}]},
    ]
    assert [native_product_key(entry) for entry in anonymous] == ["", ""], (
        "positive control: these entries must be the ones that name no identifier"
    )

    base_url, _ = storefront_factory(products=anonymous)
    snapshot = SignedFetchAdapter().fetch_catalog(
        CatalogRequest(
            store_id="store-1",
            base_url=base_url,
            policy=LOOPBACK,
            fetch_product_pages=False,
        )
    )

    ids = [product.product_id for product in snapshot.products]
    assert len(ids) == len(set(ids)), (
        f"two catalog entries with no identifier landed on one product node: {ids} "
        f"(warnings: {list(snapshot.warnings)})"
    )


# =============================================================================================
# T-249 — a refused entry price falls through to the other surface's price
# =============================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-249: coerce_price returns None for a negative/NaN/inf entry price, and "
        "signed_fetch reads None as 'not stated' and falls through to the JSON-LD offer "
        "price — so an entry price of '-5.00' beside a JSON-LD price of '12.00' puts 12.0 in "
        "the graph where the pre-T-023 code put -5.0; remove this marker with the fix"
    ),
)
def test_a_refused_entry_price_does_not_promote_the_other_surfaces_price() -> None:
    """``None`` means two different things to the price merge, and a store picks which.

    ``coerce_price`` refuses a negative, NaN or infinite price by returning ``None``, which is
    right. Its consumer then reads that ``None`` as *"products.json did not state a price"* and
    reaches for the JSON-LD offer instead::

        price = coerce_price(item.get("price"))
        if price is None:
            price = coerce_price(matched.get("price"))

    So "the store stated a hostile price" and "the store stated no price" are the same signal,
    and a store that writes ``-5.00`` in ``products.json`` while its page's JSON-LD says
    ``12.00`` gets 12.00 into the graph. The module's own contract is the opposite — the
    machine endpoint wins on anything it states, and JSON-LD only fills gaps — so a store can
    choose which of its two surfaces prices the product by making the first one unusable.
    Measured: ``price=12.0``, and an ``Offer`` op is emitted for it.

    The store is served for real rather than calling the merge directly: this needs the two
    surfaces to DISAGREE, which the shared ``StorefrontStub`` cannot express — it generates the
    product page's JSON-LD from the same entry it serves in ``products.json``, so both always
    carry the same number. :class:`_TwoFacedStore` serves the two numbers the ticket names and
    is crawled through the ordinary public entry point.

    Either repair passes: refuse the product, or leave the price unset. What may not happen is
    a refused price silently becoming a different number.
    """
    with serve(_TwoFacedStore(entry_price="-5.00", page_price="12.00")) as base_url:
        snapshot = SignedFetchAdapter().fetch_catalog(
            CatalogRequest(
                store_id="store-1",
                base_url=base_url,
                policy=LOOPBACK,
                fetch_product_pages=True,
            )
        )

    assert snapshot.products, f"the crawl read nothing: {list(snapshot.warnings)}"
    price = snapshot.products[0].variants[0].price

    assert price != 12.0, (
        "a hostile entry price of '-5.00' was read as 'no price stated' and the JSON-LD's "
        f"12.00 took its place: variant price is {price!r}"
    )


# =============================================================================================
# T-312 (ingest half) — the published refresh door is unserved, and every door ingest DOES
# serve is unpublished
# =============================================================================================
#
# The gate is a general property rather than a probe over one route name: build the app, read
# ``app.openapi()['paths']``, read ``packages/contracts/openapi/ingest.openapi.json``, and
# require the two operation sets to agree **in both directions**. A frozen list of names would
# grade only the missing half; here the divergence runs both ways and the larger half is the
# one a name list cannot see at all.

INGEST_OPENAPI = REPO_ROOT / "packages/contracts/openapi/ingest.openapi.json"

#: The methods an OpenAPI path item may carry. Everything else under a path item
#: (``parameters``, ``summary``, ``$ref``, ``servers``) is not an operation, and counting it as
#: one would inflate the very non-zero check that arms this sweep.
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _normalise_route(path: str) -> str:
    """``/refresh/{store_id}`` -> ``/refresh/{}``.

    The comparison is about the *wire shape* of a route, not about what the service names its
    path parameter: an app serving ``/refresh/{sid}`` genuinely answers the contract's
    ``/refresh/{store_id}``, and failing it for the spelling would make this gate red for a
    reason the ticket is not about. Failure messages still print the raw spellings, so a real
    naming divergence stays visible without being fatal.

    ``str.partition`` rather than a regex so no import has to be added to this file's frozen
    head (E402). Its behaviour on malformed input is stated exactly, because an earlier draft
    of this docstring claimed something else and was wrong: a ``{`` with no ``}`` anywhere
    after it is passed through unchanged, but ``/a/{b/{c}`` collapses ``b/{c`` into a single
    ``{}`` — the first ``{`` pairs with the only ``}``. Nothing in the five contracts is shaped
    like that today, and the injectivity check in the arming test is what keeps a future one
    from collapsing two distinct paths onto one string unnoticed.
    """
    out: list[str] = []
    rest = path
    while "{" in rest:
        head, _, tail = rest.partition("{")
        _param, closed, rest = tail.partition("}")
        if not closed:
            return "".join(out) + head + "{" + tail
        out.append(head + "{}")
    return "".join(out) + rest


def _operations(paths: dict[str, Any], *, raw: bool = False) -> set[tuple[str, str]]:
    """``{(METHOD, path)}`` from an OpenAPI ``paths`` object, normalised unless ``raw``."""
    return {
        (method.upper(), path if raw else _normalise_route(path))
        for path, item in paths.items()
        for method in item
        if method.lower() in HTTP_METHODS
    }


def _published_operations(contract: Path, *, raw: bool = False) -> set[tuple[str, str]]:
    """What an OpenAPI document on disk declares."""
    return _operations(json.loads(contract.read_text(encoding="utf-8")).get("paths", {}), raw=raw)


def _served_operations(app: Any) -> set[tuple[str, str]]:
    """What a built FastAPI application actually answers."""
    return _operations(app.openapi().get("paths", {}))


def _operation_divergence(served: set[tuple[str, str]], published: set[tuple[str, str]]) -> str:
    """A message naming BOTH differences, and the counts each side actually iterated."""
    unserved = sorted(f"{method} {path}" for method, path in published - served)
    unpublished = sorted(f"{method} {path}" for method, path in served - published)
    return (
        f"served {len(served)} operation(s), contract publishes {len(published)}; "
        f"published but NOT served: {unserved or 'none'}; "
        f"served but NOT published: {unpublished or 'none'}"
    )


def _contract_probe_app(contract: Path) -> Any:
    """A synthetic app serving exactly what ``contract`` publishes — the sweep's arming device.

    Three sweeps in this repo were found going QUIET rather than red (T-229 6->0 of 8, T-281
    70->0 of 79, T-241 48->0 of 66): a loop that iterates zero cases and passes. A
    served-vs-published comparison carries the same hazard in a nastier form, because
    ``set() == set()`` is a *pass*. Pointing the extractor at an app whose served set is known
    — built from the very paths under test — is what makes an empty ``served`` mean "this
    service serves nothing" rather than "this probe can no longer see routes".
    """
    from fastapi import FastAPI  # noqa: PLC0415 - kept out of this file's frozen import head

    def _probe() -> dict[str, Any]:  # pragma: no cover - mounted, never called
        return {}

    app = FastAPI(title=f"probe:{contract.name}")
    for method, path in sorted(_published_operations(contract, raw=True)):
        app.add_api_route(path, _probe, methods=[method])
    return app


def test_the_ingest_served_versus_published_sweep_is_armed() -> None:
    """Not xfail, and not optional: the T-312 gate below is worthless without this.

    Three ways the comparison could pass while measuring nothing, all closed here:

    * the contract stops parsing to operations, so ``published - served`` is empty;
    * the extractor stops seeing routes for structural reasons (a changed FastAPI, a swallowed
      exception inside ``app.openapi()``), so ``served`` is empty for every app and an unserved
      contract is indistinguishable from a served one;
    * the two sides normalise paths differently — the one way a set comparison can be wrong
      without either side being empty.

    The last two are closed by comparing the contract against a synthetic app built from that
    contract's own raw paths, and by requiring the real ingest app to serve something.
    """
    from ingest.main import create_app  # noqa: PLC0415

    assert INGEST_OPENAPI.is_file(), f"{INGEST_OPENAPI} does not exist"
    raw = _published_operations(INGEST_OPENAPI, raw=True)
    published = _published_operations(INGEST_OPENAPI)
    assert published, f"{INGEST_OPENAPI} declares no operations; the sweep would be blind"

    # Normalisation must be INJECTIVE, or the comparison silently shrinks. Two distinct
    # published paths that normalise to one string — `/refresh/{store_id}` beside
    # `/refresh/{slug}` — collapse identically on BOTH sides, so the probe check below still
    # passes while a service serving only one of them satisfies `served == published` with the
    # other door 404ing. Nothing here is shaped like that today; this keeps it that way.
    assert len(published) == len(raw), (
        f"normalising path parameters collapsed {len(raw)} published operations onto "
        f"{len(published)} — two distinct contract paths differ only in the NAME of a path "
        f"parameter, so the comparison can no longer tell them apart. Raw: "
        f"{sorted(f'{m} {p}' for m, p in raw)}"
    )

    probe = _served_operations(_contract_probe_app(INGEST_OPENAPI))
    assert probe == published, (
        "the extractor and the contract reader disagree on an app built from the contract "
        f"itself — {_operation_divergence(probe, published)}"
    )

    served = _served_operations(create_app())
    assert served, (
        "the ingest app served no operation at all, so the extractor cannot distinguish an "
        "unserved contract from a served one and the gate below would pass by measuring nothing"
    )
    # FLOORS, not equalities, and the asymmetry is the point. Either side GROWING is ordinary
    # progress and must not turn this file red for a lane that owns neither the contract nor
    # the gate below. Either side SHRINKING is the cheapest way to fake the fix: ``served ==
    # published`` is satisfiable from either end, and this file's own positive control
    # repaired the gate by editing the contract alone with no source change. So deleting the
    # published promise, or deleting the routes, to make the two sets agree trips here. The
    # strict xfail marker catches that fake once; this is the tripwire that survives its
    # removal.
    # Two separate comparisons, deliberately NOT `(a, b) >= (6, 1)`: tuple comparison is
    # LEXICOGRAPHIC, so that spelling accepts (7, 0) — a contract emptied to zero, waved
    # through because one more route got served. The bug the floor exists to catch would have
    # walked straight past its own guard.
    assert len(served) >= 6, (
        f"ingest lost served surface since this gate was measured: {len(served)} operation(s), "
        "floor 6. Deleting routes so the sets agree is not a fix."
    )
    assert len(published) >= 1, (
        f"the ingest contract lost operations since this gate was measured: {len(published)}, "
        "floor 1. Serving a promise is a fix; deleting the promise so the sets agree is not."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-312 (ingest half): the published POST /refresh/{store_id} is served by nothing "
        "(measured 404) while the six operations ingest.main.create_app() does mount — the "
        "er and extraction routers — appear in no contract at all, so the divergence between "
        "the served surface and the published one runs in BOTH directions; remove this marker "
        "with the fix"
    ),
)
def test_t312_ingest_serves_exactly_the_operations_its_contract_publishes() -> None:
    """One published door with no server, and six servers with no published door.

    Measured at HEAD by building the app and reading ``app.openapi()['paths']``::

        served     GET  /er/config                    POST /er/match
                   POST /er/resolve                   GET  /extraction/config
                   POST /extraction/policy-pages      POST /extraction/stores/{store_id}
        published  POST /refresh/{store_id}           -> 404

    The two sets are disjoint. ``POST /refresh/{store_id}`` is the whole published surface of
    this service — the door that re-crawls a store's catalog — and it is answered by nothing,
    which is the same class of defect as the store agent serving no path at all: the pipeline
    behind it is built and tested as a library and no request can start it.

    The other direction is the larger half and is deliberately asserted here too. Six live
    endpoints — entity resolution and extraction, including ``POST /extraction/stores/{id}``,
    which crawls an arbitrary store — are served by a deployed service and declared by no
    contract. Nothing tells a client they exist, and nothing tells a reviewer of the contract
    that this service's real attack surface is six times what the document shows. Gating only
    the missing half would let a fix mount ``/refresh`` and leave that untouched.

    Either direction is repairable independently and the test names both, so whichever is
    fixed first the message says exactly what is left.
    """
    from ingest.main import create_app  # noqa: PLC0415

    app = create_app()
    served = _served_operations(app)
    published = _published_operations(INGEST_OPENAPI)

    assert published, "the ingest contract declares nothing; the sweep is unarmed"
    assert served == published, (
        "the ingest service's served surface diverges from its published contract — "
        f"{_operation_divergence(served, published)}; mounted routers: "
        f"{getattr(app.state, 'mounted_routers', 'unknown')}"
    )
