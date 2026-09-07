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
from proxyshop_support.contract_sweep import (
    contract_probe_app as _contract_probe_app,
)
from proxyshop_support.contract_sweep import (
    normalise_route as _normalise_route,  # noqa: F401 - re-exported for T-321's sweep, see below
)
from proxyshop_support.contract_sweep import (
    operation_divergence as _operation_divergence,
)
from proxyshop_support.contract_sweep import (
    operations as _operations,  # noqa: F401 - re-exported for T-321's sweep, see below
)
from proxyshop_support.contract_sweep import (
    published_operations as _published_operations,
)
from proxyshop_support.contract_sweep import (
    served_operations as _served_operations,
)

# ``_normalise_route`` and ``_operations`` are not called by name in this file — they are
# reached through ``_published_operations``/``_served_operations`` — and the ``noqa`` above is
# what keeps them bound anyway. That is deliberate, not an oversight: T-321's sweep
# (``packages/store-agent/tests/test_repro_open_tickets.py``) counts how many distinct
# implementations stand behind each helper ROLE across the repo's gate modules, and its arming
# test requires every role to resolve through at least two of them. Dropping the two unused
# re-exports would empty two of those roles down to one module and make a "one implementation"
# verdict unfalsifiable — the sweep would be agreeing with itself about names nobody exposes.

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


# MARKER REMOVED — T-245 is fixed. The marker read, verbatim:
#
#     @pytest.mark.xfail(strict=True, reason=(
#         "T-245: catalog_mcp skips an entry whose native_product_key is empty while "
#         "signed_fetch mints prod_<hash('')> for every such entry, so two unidentifiable "
#         "products from one store collapse onto ONE graph node — a divergence the "
#         "inspect.signature acceptance check cannot see; remove this marker with the fix"))
#
# What it encodes: the two adapters that are supposed to share one mapping disagree on what
# reaches the graph, and the disagreement merges a store's whole unidentified catalog onto a
# single product node on the first real write.
#
# Would this test still be wrong if my change were reverted? YES, and it was isolated from the
# lane's OTHER mapping change rather than proved jointly: with the T-245 hunks removed and
# T-249's kept, this test returns to `xfailed` while T-249's stays fixed; with T-249's removed
# and T-245's kept, the reverse. So this test's pass is caused by the T-245 repair
# specifically — `signed_fetch._read_catalog` skipping an entry whose `native_product_key` is
# empty (the warning `catalog_mcp` already emits), and `mapping.product_id_for` refusing to
# hash the empty key at all.
#
# No assertion below is touched, including the positive control that asserts these fixture
# entries are the ones naming no identifier.
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


# MARKER REMOVED — T-249 is fixed. The marker read, verbatim:
#
#     @pytest.mark.xfail(strict=True, reason=(
#         "T-249: coerce_price returns None for a negative/NaN/inf entry price, and "
#         "signed_fetch reads None as 'not stated' and falls through to the JSON-LD offer "
#         "price — so an entry price of '-5.00' beside a JSON-LD price of '12.00' puts 12.0 in "
#         "the graph where the pre-T-023 code put -5.0; remove this marker with the fix"))
#
# What it encodes: "the store stated a hostile price" and "the store stated no price" are the
# same signal to the merge, so a store picks which of its two surfaces prices a product by
# making the first one unusable.
#
# Would this test still be wrong if my change were reverted? YES, and it was isolated from the
# lane's OTHER mapping change rather than proved jointly: with the T-249 hunk reverted to
# `price = coerce_price(item.get("price")); if price is None: price = coerce_price(
# matched.get("price"))` and T-245's kept, this test returns to `xfailed` while T-245's stays
# fixed; with T-245's removed and T-249's kept, the reverse. So this test's pass is caused by
# the T-249 repair specifically — `mapping.price_is_stated`, which makes the JSON-LD offer
# fill a GAP only, so a refused statement costs the offer instead of promoting the other
# surface's number.
#
# No assertion below is touched.
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

#: The six contract-sweep helpers used below are NOT defined here (T-321). They were written
#: out once in this file, once in ``apps/exchange/tests/test_repro_open_tickets.py`` and once in
#: ``packages/store-agent/tests/test_repro_open_tickets.py``, under two different spellings, with
#: nothing comparing the copies — and they had already drifted once, the store-agent path
#: normaliser being a regex that disagreed with this one on ``/a/{b/{c}``. One rule now lives in
#: :mod:`proxyshop_support.contract_sweep` and all three gates import it; the local names are
#: kept (see this file's import head) so every call site below reads exactly as it did.
#:
#: Nothing this file measures changed: the folded implementation is the logic that was here, and
#: ``contract_probe_app`` keeps THIS file's ``probe:<contract file name>`` title.


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
    # Both floors RAISED with the T-312 (ingest half) fix, from 6 served / 1 published, to the
    # counts measured after it. EIGHT operations became declared in that change; floors left at
    # the pre-fix numbers could not see any of them deleted again.
    assert len(served) >= 9, (
        f"ingest lost served surface since this gate was measured: {len(served)} operation(s), "
        "floor 9. Deleting routes so the sets agree is not a fix."
    )
    assert len(published) >= 9, (
        f"the ingest contract lost operations since this gate was measured: {len(published)}, "
        "floor 9. Serving a promise is a fix; deleting the promise so the sets agree is not."
    )


# T-312 (ingest half) CLOSED — the `xfail(strict=True)` marker that stood here is REMOVED in the
# change that closed it. The assertion is untouched.
#
# Half of the marker's reason was stale and is recorded rather than dropped. It said "the
# published POST /refresh/{store_id} is served by nothing (measured 404)". RE-MEASURED on this
# branch: `ingest.main.create_app()` mounts three routers (er, extraction, scheduler) and the
# scheduler's `POST /refresh/{store_id}` IS served — driven here, it answers 404 with the JSON
# body `{"detail": "no ingest target registered for store 'store-1'; ..."}`, which is the
# handler refusing an unregistered store, not a routing miss. Both are "404" to a curl; they are
# not the same fact, and the marker had recorded the wrong one.
#
# So only the served-but-unpublished direction was still live, and the repair was PUBLISH: the
# `er` and `extraction` routers are a real operator surface (`docs/deploy.md:284` documents
# `curl :8085/er/config`), and `POST /extraction/stores/{store_id}` crawls an arbitrary
# storefront — precisely the reachable-but-unreviewed surface the ticket was filed about.
# Every one was DRIVEN with a real payload before it was declared, and every published response
# example is a body the running service actually returned, not a body someone imagined: the
# first draft of the `/extraction/stores/{store_id}` example was hand-written, the served report
# turned out to carry `base_url`, `observed_at`, `model_calls`, `reused`, `hash_index` and
# `upserts` as well, and it was replaced by the measured one.
#
# `GET /schedule` and `POST /schedule/tick` arrived from another lane's scheduler work WHILE
# this change was in flight — the re-measure that caught them is why they are here rather than
# left as a fresh instance of the same defect the moment this one closed. They were driven the
# same way and declared with the rest.
#
#   BEFORE (this branch, worker 2, --runxfail):
#     served 7, published 1; published but NOT served: none;
#     served but NOT published: the six er/extraction operations
#   AFTER: served 9, published 9, both directions empty.
#   CAUSATION: deleting the eight new entries from
#     packages/contracts/openapi/ingest.openapi.json returns this node to that failure —
#     re-measured against `git show HEAD:...` and it reports exactly the BEFORE line.
def test_t312_ingest_serves_exactly_the_operations_its_contract_publishes() -> None:
    """The ingest service's served surface and its published contract, in both directions.

    Measured after the fix by building the app and reading ``app.openapi()['paths']``::

        served == published ==
                   GET  /er/config                    POST /er/match
                   POST /er/resolve                   GET  /extraction/config
                   POST /extraction/policy-pages      POST /extraction/stores/{store_id}
                   POST /refresh/{store_id}           GET  /schedule
                   POST /schedule/tick

    Both directions are asserted here on purpose, and the served-but-unpublished one is the
    half this ticket turned on: eight live endpoints — entity resolution, extraction and the
    scheduler's driving surface, including ``POST /extraction/stores/{id}``, which crawls an
    arbitrary store — were served by a deployed service and declared by no contract. Nothing
    told a client they existed, and nothing told a reviewer of the contract that this service's
    real reachable surface was nine times what the document showed.

    Either direction is repairable independently and the test names both, so whichever
    regresses first the message says exactly what is wrong.
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


# =============================================================================================
# T-254 — the DESIGN Claim projection is defined and never produced
# =============================================================================================
#
# The gate is the same shape as T-236's and for the same reason: the question is whether a
# capability the design names is performed by anything the service runs, and that is a
# reachability question, not a behaviour one. `ExtractedClaim.as_claim()` returns DESIGN's
# `Claim{key, value, provenance}` — the projection T-021's objective "decompose to atomic
# Claims" names — and nothing calls it. Its sibling `as_attribute()` IS called, from
# `extraction/pipeline.py`, and reaches the graph. So ingestion writes AttributeValues and the
# Claim projection exists only as a definition.
#
# `as_attribute` is what arms this: pointed at it, the scan finds a call in an app-reachable
# module, so an empty result for `as_claim` means "nothing produces it" rather than "the
# scanner can no longer see method calls".

#: The projection under test, and the wired sibling that serves as the scanner's control.
_CLAIM_PROJECTION = "as_claim"
_ATTRIBUTE_PROJECTION = "as_attribute"


def _method_calls(path: Path, name: str) -> list[int]:
    """Line numbers in ``path`` at which ``.<name>(...)`` is CALLED.

    A ``def`` is not a ``Call``, so the definition never counts as its own caller — which is
    the entire distinction this ticket turns on. Attribute calls only: the projections are
    methods, and a bare ``as_claim(...)`` would be a different function.
    """
    with warnings.catch_warnings():
        # Compiling someone else's source re-emits its SyntaxWarnings against this test.
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(path.read_text(encoding="utf-8"))
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    )


def _app_reachable_ingest_sources() -> list[Path]:
    """Every ``services/ingest/src`` file that building the real app pulls in.

    Measured in a subprocess by :func:`_modules_imported_by_the_app` and re-checked against
    :data:`REPO_ROOT` here, because this venv's ``site-packages/_proxyshop.pth`` puts a
    checkout root on ``sys.path`` for every process that uses it — a probe that trusts the
    resolution it happens to get can measure a different tree than the one under test.
    """
    reachable: list[Path] = []
    for name, filename in sorted(_modules_imported_by_the_app().items()):
        if not filename:
            continue
        resolved = Path(filename).resolve()
        assert REPO_ROOT in resolved.parents, (
            f"{name} resolved to {resolved}, which is outside the tree under test "
            f"({REPO_ROOT}) — the probe measured the wrong checkout"
        )
        if INGEST_SRC in resolved.parents:
            reachable.append(resolved)
    return reachable


def _an_extracted_claim() -> Any:
    """One valid ``ExtractedClaim``, built the way extraction builds them.

    Imported inside the function rather than at this file's head, the way the T-312 helpers
    below do it, so no import has to be added to the frozen import block (E402).
    """
    from ingest.extraction.claims import ClaimProvenance, ExtractedClaim  # noqa: PLC0415

    return ExtractedClaim(
        key="shipping.dispatch_window_days",
        value=2,
        claim_type="shipping_window",
        confidence=0.9,
        provenance=ClaimProvenance(
            source="scraped",
            ref="snapshot://store-one.example.com/policies/shipping@sha256:0f1e2d3c",
            observed_at="2026-01-01T00:00:00Z",
        ),
    )


def test_t254_the_claim_projection_sweep_is_armed() -> None:
    """Not xfail, and not optional: the T-254 gate below is worthless without this.

    Four ways that gate could pass — or fail — while measuring nothing, all closed here:

    * the app imports no ingest source at all, so the scan iterates zero files and an empty
      result says nothing (three sweeps in this repo were found going QUIET rather than red);
    * ``ingest.extraction.claims``, the module that DEFINES the projection, is not among the
      files scanned, so "is it produced" is being asked of a service that does not have it;
    * the scanner stops seeing method calls for structural reasons — a changed ``ast``, a
      renamed method — so a wired projection and an unwired one look identical. Closed by
      pointing the scanner at ``as_attribute``, which IS called from an app-reachable module;
    * the projection is broken rather than merely unwired, which would make the gate below
      red for a reason the ticket is not about. Closed by calling it and checking its shape.

    The last check is also where DESIGN's ``Claim{key, value, provenance}`` is pinned: exactly
    those three keys and nothing else, which is what makes it a *projection* rather than a
    second serialisation of the whole record.
    """
    sources = _app_reachable_ingest_sources()
    assert sources, "the app imported no ingest source at all; the sweep would be blind"

    claims_module = INGEST_SRC / "extraction/claims.py"
    assert claims_module in sources, (
        f"{claims_module} is not reachable from the running app, so this sweep cannot say "
        f"anything about whether its projection is produced; reachable: "
        f"{[str(p.relative_to(REPO_ROOT)) for p in sources]}"
    )
    assert _method_calls(claims_module, _CLAIM_PROJECTION) == [], (
        "the projection's own module calls it, which would make the gate below pass without "
        "the projection ever leaving this file — the finding is about production, not recursion"
    )

    sibling = {
        str(path.relative_to(REPO_ROOT)): lines
        for path in sources
        if (lines := _method_calls(path, _ATTRIBUTE_PROJECTION))
    }
    assert sibling, (
        f"the scan cannot find a call to the wired sibling {_ATTRIBUTE_PROJECTION}() either, "
        f"so it is broken rather than measuring anything about {_CLAIM_PROJECTION}(); "
        f"scanned {len(sources)} app-reachable ingest module(s)"
    )

    claim = _an_extracted_claim()
    projection = claim.as_claim()
    assert projection == {
        "key": "shipping.dispatch_window_days",
        "value": 2,
        "provenance": claim.provenance,
    }, projection
    assert set(projection) == {"key", "value", "provenance"}, (
        f"DESIGN's Claim is {{key, value, provenance}} and nothing else; got {sorted(projection)}"
    )


def test_t254_the_claim_projection_is_produced_by_the_running_service() -> None:
    """An objective's wiring, not its definition. T-021 said "decompose to atomic Claims".

    ``ExtractedClaim`` carries two projections of the same reading. ``as_attribute()`` becomes
    the ``AttributeValue`` node a policy page ``STATES``, and it is wired:
    ``extraction/pipeline.py`` calls it when building the upsert batch, so every claim that
    clears C10's floor reaches the graph in that shape. ``as_claim()`` becomes DESIGN's
    ``Claim{key, value, provenance}`` — the shape that crosses a wire or a function boundary,
    the one T-021's objective names — and it is called by nothing at all.

    That is not a style observation. The two shapes carry different things: an
    ``AttributeValue`` keeps the typed value and drops the provenance into a ``SUPPORTED_BY``
    edge, while a ``Claim`` carries the provenance *inline*, which is what lets a downstream
    consumer hold one fact and its evidence together without a graph round trip. A service
    that only ever produces the first has implemented half the decomposition.

    Note what this does NOT require. It does not name a caller, a module, or a shape for the
    fix: the extraction route can build its response on the projection instead of hand-rolling
    the same three fields; the pipeline can emit Claims beside AttributeValues; a new consumer
    can produce them. Any of those passes. What is refused is only the current state, in which
    the projection is defined, exported through no public surface, called by nothing, and
    therefore free to be wrong without anything noticing.

    The module list is measured in a subprocess and every file in it is checked to be inside
    this tree, so a stale ``.pth`` cannot answer the question with another checkout's code.
    """
    sources = _app_reachable_ingest_sources()
    produced = {
        str(path.relative_to(REPO_ROOT)): lines
        for path in sources
        if (lines := _method_calls(path, _CLAIM_PROJECTION))
    }

    assert produced, (
        f"no module the ingest app imports calls {_CLAIM_PROJECTION}(), so the DESIGN Claim "
        f"projection is never produced by the running service; its wired sibling "
        f"{_ATTRIBUTE_PROJECTION}() is called from "
        f"{sorted(str(p.relative_to(REPO_ROOT)) for p in sources if _method_calls(p, _ATTRIBUTE_PROJECTION))}"
        f", and {len(sources)} app-reachable ingest module(s) were scanned"
    )


# ---------------------------------------------------------------------------------------
# T-367 — the module-scope extraction ledger grows one permanent entry per request
# ---------------------------------------------------------------------------------------
#
# WHAT THE DEFECT WAS. ``ingest.extraction.routes`` holds a module-scope
# ``ExtractionLedger`` built from two plain dicts. ``POST /extraction/policy-pages`` is
# unauthenticated, and every call that actually extracts ends in
# ``ledger.record(payload.url, digest, result)``. The caller picks the key (``url``, which
# carried ``min_length=1`` and no ceiling) and the retained value (whatever ``body``
# extracts to, ``body`` being a bare ``str``). Nothing evicted: no capacity, no TTL, no
# LRU, no sweep. ``forget()`` existed and no HTTP path called it. So entry count was
# exactly ``n`` after ``n`` distinct pages, for every ``n``, for the life of the process —
# growth on ORDINARY traffic, not merely under attack, against a 256 MiB container
# (``compose.yaml`` ``mem_limit: 256m``).
#
# The gates below are ordered: an arming test first (the write path really does record, so
# the growth probe is measuring something), then the bound itself, then the reason the
# ledger exists at all — a cache that no longer answers is not a fixed cache, it is a
# deleted one.

#: How many requests past the published capacity the growth gate drives. Enough that an
#: unbounded ledger is unmistakably unbounded and a bounded one has evicted many times.
_T367_OVERSHOOT = 64

#: The capacity the gate assumes when the module publishes none. Only ever used to size the
#: request loop: with no named constant there is no bound to honour and the gate is red on
#: that count alone.
_T367_ASSUMED_CAPACITY = 512


def _t367_reset_ledger() -> Any:
    """The module-scope ledger, emptied. Returns it."""
    import importlib

    routes = importlib.import_module("ingest.extraction.routes")
    routes.ledger.hashes.clear()
    routes.ledger.results.clear()
    return routes.ledger


def _t367_capacity() -> int | None:
    """The ledger's published entry ceiling, or ``None`` when it publishes none."""
    import importlib

    differential = importlib.import_module("ingest.extraction.differential")
    capacity = getattr(differential, "MAX_LEDGER_PAGES", None)
    return int(capacity) if isinstance(capacity, int) else None


def test_t367_the_ledger_growth_probe_is_armed(policy_pages: dict[str, str]) -> None:
    """The route really does write to the module-scope ledger.

    Without this, a growth gate would pass just as happily against a handler that recorded
    nothing at all — which is the failure mode where the bound looks enforced and is in
    fact never exercised.
    """
    import importlib

    from fastapi.testclient import TestClient

    ledger = _t367_reset_ledger()
    body = policy_pages["shipping.html"]

    with TestClient(importlib.import_module("ingest.main").create_app()) as client:
        for index in range(8):
            response = client.post(
                "/extraction/policy-pages",
                json={"url": f"https://store.example.com/policies/armed-{index}", "body": body},
            )
            assert response.status_code == 200, response.text
            assert response.json()["reused"] is False

    try:
        assert len(ledger.hashes) == 8, (
            f"eight distinct pages were extracted and the ledger holds {len(ledger.hashes)} "
            f"hashes; the growth gate below cannot measure a write path that does not write"
        )
        assert len(ledger.results) == 8, (
            f"the ledger retained {len(ledger.results)} results for eight extractions; the "
            f"byte-cost of an entry is what the retention budget is about"
        )
    finally:
        _t367_reset_ledger()


def test_t367_repeated_policy_page_posts_do_not_grow_the_ledger_without_bound(
    policy_pages: dict[str, str],
) -> None:
    """N distinct pages must not become N permanent ledger entries.

    This is the whole finding, measured through the running app rather than argued about:
    every request is a legitimate one — a distinct, short, well-formed URL and a committed
    fixture page as the body — and the question is only whether the process keeps all of
    them. It must not. The ledger is a cache, and a cache without an eviction policy is a
    memory leak with a docstring.

    The bound has to be a *published* one. A ledger that happens to stay small because the
    test drove few requests is not bounded; the gate therefore reads the module's own
    named ceiling and drives past it.
    """
    import importlib

    from fastapi.testclient import TestClient

    capacity = _t367_capacity()
    driven = (capacity or _T367_ASSUMED_CAPACITY) + _T367_OVERSHOOT
    ledger = _t367_reset_ledger()
    body = policy_pages["shipping.html"]

    try:
        with TestClient(importlib.import_module("ingest.main").create_app()) as client:
            for index in range(driven):
                response = client.post(
                    "/extraction/policy-pages",
                    json={
                        "url": f"https://store.example.com/policies/page-{index}",
                        "body": body,
                    },
                )
                assert response.status_code == 200, (index, response.text)

        held = len(ledger.hashes)
        assert held < driven, (
            f"{driven} legitimate requests left {held} ledger entries — one per request, "
            f"retained for the life of the interpreter. Nothing about this needs an "
            f"attacker: it is what ordinary traffic does to a dict with no eviction policy."
        )
        assert capacity is not None, (
            "ingest.extraction.differential publishes no MAX_LEDGER_PAGES, so the ledger's "
            "ceiling is not a named, documented number that a reader or a gate can check"
        )
        assert held <= capacity, (
            f"the ledger holds {held} pages, past its own published capacity of {capacity}"
        )
        assert len(ledger.results) <= held, (
            f"{len(ledger.results)} retained results against {held} retained hashes: an "
            f"evicted page must take its result with it, or the expensive half of the "
            f"entry outlives the cheap half that indexes it"
        )
        assert held >= capacity // 2, (
            f"the ledger kept only {held} of {driven} pages against a capacity of "
            f"{capacity}; that is not eviction, that is a cache that has stopped caching"
        )
    finally:
        _t367_reset_ledger()


def test_t367_the_ledger_still_answers_for_the_page_it_saw_most_recently(
    policy_pages: dict[str, str],
) -> None:
    """Bounding the ledger must not delete the guarantee it exists for.

    ``POST /extraction/policy-pages`` promises that replaying an unchanged page performs no
    extraction — ``reused: true``, ``model_calls: 0`` — and that promise is about two
    separate HTTP requests, which is the entire reason the ledger is module state. So the
    fix has to evict the LEAST recently used entry and keep the freshest one, not empty the
    structure on a timer or refuse to record.
    """
    import importlib

    from fastapi.testclient import TestClient

    capacity = _t367_capacity() or _T367_ASSUMED_CAPACITY
    ledger = _t367_reset_ledger()
    body = policy_pages["shipping.html"]
    url = "https://store.example.com/policies/kept"

    try:
        with TestClient(importlib.import_module("ingest.main").create_app()) as client:
            # Fill the ledger to well past capacity, then extract the page under test last.
            for index in range(capacity + _T367_OVERSHOOT):
                filler = client.post(
                    "/extraction/policy-pages",
                    json={"url": f"https://store.example.com/policies/f-{index}", "body": body},
                )
                assert filler.status_code == 200, (index, filler.text)

            first = client.post("/extraction/policy-pages", json={"url": url, "body": body})
            assert first.status_code == 200, first.text
            digest = first.json()["content_hash"]
            assert first.json()["claims"], "the page under test extracted nothing to cache"

            replay = client.post(
                "/extraction/policy-pages",
                json={"url": url, "body": body, "known_hash": digest},
            )

        assert replay.status_code == 200, replay.text
        again = replay.json()
        assert again["reused"] is True, "the differential guarantee did not survive the bound"
        assert again["model_calls"] == 0, again
        assert again["claims"], (
            "the replay was answered with an empty result: the hash survived eviction but "
            "the cached claims did not, so the cache hit is cheap and useless"
        )
        assert ledger.cached(url, digest) is not None, (
            "the most recently used page was evicted while older ones were kept; that is "
            "not an LRU, and under steady traffic it evicts exactly what is about to be hit"
        )
    finally:
        _t367_reset_ledger()


def test_t367_an_oversized_policy_page_is_refused_cleanly_and_not_echoed_back() -> None:
    """The caller chooses the key AND the retained value, so both need a ceiling.

    An entry cap alone bounds the ledger's *length*, not its *size*: 30,000 characters of
    URL and a megabytes-long body were measured at 61,479 bytes retained per request. The
    refusal has to be a clean 4xx — never a 5xx, never an unbounded allocation — and it
    must not hand the offending input back in the error body, which would turn a rejected
    request into an amplifier.
    """
    import importlib

    from fastapi.testclient import TestClient

    ledger = _t367_reset_ledger()
    marker = "PolicyPageOverflowMarker"
    long_url = "https://store.example.com/policies/" + ("a" * 30_000)
    long_body = f"<p>Orders ship within 2 business days. {marker} {'z' * 8_000_000}</p>"

    try:
        with TestClient(importlib.import_module("ingest.main").create_app()) as client:
            for label, payload in (
                ("url", {"url": long_url, "body": "<p>Orders ship within 2 business days.</p>"}),
                ("body", {"url": "https://store.example.com/policies/big", "body": long_body}),
            ):
                response = client.post("/extraction/policy-pages", json=payload)
                assert 400 <= response.status_code < 500, (
                    f"an oversized {label} was answered {response.status_code}; an "
                    f"unauthenticated surface must refuse it, and refuse it as a 4xx"
                )
                text = response.text
                assert len(text) < 4096, (
                    f"the refusal of an oversized {label} was {len(text)} bytes long — it is "
                    f"echoing the offending input back to whoever sent it"
                )
                assert marker not in text and "a" * 200 not in text, (
                    f"the refusal of an oversized {label} quotes the input back"
                )

        assert not ledger.hashes, (
            f"a refused request was recorded anyway: {len(ledger.hashes)} ledger entries "
            f"after two 4xx responses"
        )
    finally:
        _t367_reset_ledger()


def test_t367_the_anonymous_config_endpoint_publishes_no_ledger_occupancy(
    policy_pages: dict[str, str],
) -> None:
    """``GET /extraction/config`` is unauthenticated and used to publish ``pages_known``.

    That counter is a free progress oracle: it tells an anonymous caller exactly how much
    of the ledger their own traffic has filled, request by request, which is the readout an
    attacker needs to tune a memory-exhaustion attempt and which no legitimate caller of a
    *policy* endpoint needs at all. The endpoint's job is to publish the extraction policy —
    the confidence floor, the extractor version, the claim vocabulary — not the service's
    internal occupancy.
    """
    import importlib

    from fastapi.testclient import TestClient

    _t367_reset_ledger()
    body = policy_pages["shipping.html"]

    try:
        with TestClient(importlib.import_module("ingest.main").create_app()) as client:
            before = client.get("/extraction/config")
            assert before.status_code == 200, before.text
            for index in range(4):
                client.post(
                    "/extraction/policy-pages",
                    json={"url": f"https://store.example.com/policies/o-{index}", "body": body},
                )
            after = client.get("/extraction/config")
            assert after.status_code == 200, after.text

        assert "pages_known" not in after.json(), (
            "GET /extraction/config still publishes pages_known to anonymous callers"
        )
        assert after.json() == before.json(), (
            f"the anonymous config response moved when four pages were extracted: "
            f"{before.json()} -> {after.json()}; whatever moved is an occupancy oracle"
        )
        assert {"confidence_floor", "extractor_version", "claim_types"} <= set(after.json()), (
            "the published extraction policy lost fields it is actually for"
        )
    finally:
        _t367_reset_ledger()


def _t367_fat_result(evidence_chars: int) -> Any:
    """One ``ExtractionResult`` whose retained evidence is ``evidence_chars`` long."""
    from ingest.extraction.claims import ClaimProvenance, ExtractedClaim, ExtractionResult

    provenance = ClaimProvenance(
        source="scraped",
        ref="snapshot://store.example.com/policies/x@sha256:" + "0" * 64,
        observed_at="2026-01-01T00:00:00Z",
    )
    claim = ExtractedClaim(
        key="shipping.dispatch_window_days",
        value=2,
        claim_type="dispatch_window",
        confidence=0.9,
        provenance=provenance,
        evidence="s" * evidence_chars,
    )
    return ExtractionResult(claims=(claim,), content_hash="sha256:" + "0" * 64)


def test_t367_the_ledger_bounds_what_it_retains_and_not_only_how_many() -> None:
    """An entry cap bounds the ledger's length. Its *size* is a separate axis.

    One ledger entry retains an ``ExtractionResult``, and a result holds the evidence
    sentence behind every claim — text the caller supplied. So ``MAX_LEDGER_PAGES`` on its
    own permits 512 entries of arbitrary size, which is the same unbounded allocation with a
    smaller ``len()``. The measured hostile figure in the T-367 reproduction was 61,479 bytes
    retained per request against a 256 MiB container; the gate below drives entries far
    fatter than that and asserts the ledger's own retention budget terminates the arithmetic.

    The eviction must still be LRU, not "stop accepting": a ledger that refuses to record
    once it is full stops being a cache the moment it fills, and the newest page — the one a
    replay is about to ask for — is exactly the one it would be refusing.
    """
    import importlib

    differential = importlib.import_module("ingest.extraction.differential")
    budget = getattr(differential, "MAX_LEDGER_BYTES", None)
    assert isinstance(budget, int) and budget > 0, (
        "ingest.extraction.differential publishes no MAX_LEDGER_BYTES, so nothing bounds "
        "how much extracted text 512 ledger entries may hold between them"
    )

    ledger = differential.ExtractionLedger()
    fat = budget // 8
    for index in range(40):
        ledger.record(
            f"https://store.example.com/policies/fat-{index}", "sha256:aaa", _t367_fat_result(fat)
        )

    retained = ledger.retained_bytes()
    assert retained <= budget, (
        f"the ledger is charging itself {retained} bytes against a published budget of "
        f"{budget}; 40 entries of {fat} characters each was enough to blow through it"
    )
    assert len(ledger.hashes) < 40, (
        "no page was evicted, so the byte budget is published and not enforced"
    )
    assert ledger.hashes, "the byte budget evicted everything; that is not a cache"

    newest = "https://store.example.com/policies/fat-39"
    assert ledger.cached(newest, "sha256:aaa") is not None, (
        "the most recently recorded page was the one evicted; eviction must take the "
        "LEAST recently used, or a replay never hits"
    )

    # A single result too large to fit the whole budget is not retained at all — but the page
    # is still recognised as unchanged, which is the cheap half of the guarantee.
    huge = differential.ExtractionLedger()
    huge.record(
        "https://store.example.com/policies/huge", "sha256:bbb", _t367_fat_result(budget * 2)
    )
    assert huge.known_hash("https://store.example.com/policies/huge") == "sha256:bbb"
    assert huge.retained_bytes() <= budget, (
        f"one oversized result put {huge.retained_bytes()} bytes into a {budget}-byte budget"
    )


# ---------------------------------------------------------------------------------------
# T-367 (defeat) — the bound named two fields; a third caller-chosen string was retained
# ---------------------------------------------------------------------------------------
#
# WHAT DEFEATED THE FIRST FIX. T-367 named ``url`` and ``body``, and the fix bounded exactly
# those two — by listing their names in a tuple inside ``_refuse_oversized``. But
# ``ExtractRequest`` carried a third caller-chosen string, ``observed_at``, with no ceiling
# at all, and that string is not merely accepted: ``normalise_provenance`` copies it onto
# ``ClaimProvenance.observed_at``, which is held by the ``ExtractionResult`` AND by every
# claim inside it — and the ledger retains that result between requests. Measured against
# the running app before the fix, one request carrying a 30,024-character ``observed_at``:
#
#     retained result.provenance.observed_at len = 30024
#     claims retaining it: 2, each observed_at len = 30024   -> 90,072 bytes per entry
#     ledger.retained_bytes() = 981   <- the byte budget charged none of it
#     response bytes: 121,877 from a 74-byte body, with the payload echoed back
#
# and at 256 entries (half the published 512-page capacity) that is 23,058,432 bytes
# retained inside a 256 MiB container, against a ledger that believed it held 251,940.
#
# The lesson the gates below encode is NOT "cap observed_at". It is that a hand-written
# tuple of field names is not a bound on a model: it is a bound on the names somebody
# remembered, and the next field added is unbounded by default. So the first gate is the
# structural one — every string field on a request model must declare its own ceiling, or
# the model refuses to be defined — and the field-by-field gates that follow are its
# consequences rather than its substance.


def _t367b_reset_ledger() -> Any:
    """The module-scope ledger, emptied. Returns it."""
    return _t367_reset_ledger()


def test_t367b_a_request_model_cannot_leave_a_caller_string_unbounded() -> None:
    """The fix for "a third field was unbounded" is not "bound the third field".

    ``BoundedTextRequest`` derives the checked list from ``model_fields``, and refuses at
    class-definition time to define a subclass that leaves a string field without a ceiling.
    That is what makes the *next* field safe: it is covered because it exists, not because
    somebody remembered to add its name to a tuple. A model that only bounds the fields it
    happens to name would pass every other gate in this file and still be one new field away
    from the defect this ticket is about.
    """
    import importlib

    from pydantic import Field

    routes = importlib.import_module("ingest.extraction.routes")
    base = getattr(routes, "BoundedTextRequest", None)
    assert base is not None, (
        "ingest.extraction.routes publishes no BoundedTextRequest: the request models' "
        "ceilings are still a hand-written list, which is exactly what let observed_at "
        "through"
    )

    with pytest.raises(TypeError) as unbounded:
        type(
            "UnboundedString",
            (base,),
            {"__annotations__": {"note": str}, "note": ""},
        )
    assert "note" in str(unbounded.value), str(unbounded.value)

    with pytest.raises(TypeError) as unbounded_list:
        type(
            "UnboundedList",
            (base,),
            {
                "__annotations__": {"tags": list[str]},
                "tags": Field(default_factory=list, json_schema_extra={"max_chars": 16}),
            },
        )
    assert "tags" in str(unbounded_list.value), (
        "a list of bounded strings is still unbounded if the list itself has no ceiling"
    )


def test_t367b_every_caller_string_on_every_request_model_is_bounded() -> None:
    """Enumerate the models' string fields and assert each one publishes a ceiling.

    Written as an enumeration rather than as five named assertions on purpose: a test that
    lists field names fails the same way the fix did. This one reads the model.
    """
    import importlib

    routes = importlib.import_module("ingest.extraction.routes")

    for model in (routes.ExtractRequest, routes.StoreCrawlRequest):
        text_fields = {
            name
            for name, info in model.model_fields.items()
            if routes._carries_text(info.annotation)
        }
        bounded = set(model.TEXT_CEILINGS)
        assert text_fields == bounded, (
            f"{model.__name__} accepts caller text on {sorted(text_fields - bounded)} with "
            f"no declared ceiling"
        )
        assert text_fields, f"{model.__name__} was read as having no string fields at all"
        for name, (max_chars, _) in model.TEXT_CEILINGS.items():
            assert 0 < max_chars <= routes.MAX_PAGE_BODY_CHARS, (
                f"{model.__name__}.{name} declares a ceiling of {max_chars}"
            )


@pytest.mark.parametrize(
    ("field_name", "payload"),
    [
        (
            "url",
            {
                "url": "https://s.example.com/" + "a" * 30_000,
                "body": "<p>Orders ship within 2 business days.</p>",
            },
        ),
        (
            "kind",
            {
                "url": "https://s.example.com/p",
                "body": "<p>Orders ship within 2 business days.</p>",
                "kind": "k" * 30_000,
            },
        ),
        (
            "known_hash",
            {
                "url": "https://s.example.com/p",
                "body": "<p>Orders ship within 2 business days.</p>",
                "known_hash": "h" * 30_000,
            },
        ),
        (
            "observed_at",
            {
                "url": "https://s.example.com/p",
                "body": "<p>Orders ship within 2 business days.</p>",
                "observed_at": "9" * 30_000,
            },
        ),
    ],
)
def test_t367b_an_oversized_caller_string_is_refused_and_retained_nowhere(
    field_name: str, payload: dict[str, str]
) -> None:
    """Every caller string, driven past its ceiling: clean 4xx, no echo, nothing retained.

    ``observed_at`` is the one that defeated the first fix; the others are here because the
    reason it was missed applies to all of them equally.
    """
    import importlib

    from fastapi.testclient import TestClient

    ledger = _t367b_reset_ledger()
    try:
        with TestClient(importlib.import_module("ingest.main").create_app()) as client:
            response = client.post("/extraction/policy-pages", json=payload)

        assert 400 <= response.status_code < 500, (
            f"an oversized {field_name} was answered {response.status_code}; an "
            f"unauthenticated surface must refuse it, and refuse it as a 4xx"
        )
        assert len(response.text) < 4096, (
            f"the refusal of an oversized {field_name} was {len(response.text)} bytes: it is "
            f"echoing the offending input back to whoever sent it"
        )
        assert "a" * 200 not in response.text and "9" * 200 not in response.text, (
            f"the refusal of an oversized {field_name} quotes the input back"
        )
        assert not ledger.hashes and not ledger.results, (
            f"a refused request left {len(ledger.hashes)} ledger entries"
        )
    finally:
        _t367b_reset_ledger()


def test_t367b_a_huge_observed_at_is_not_retained_on_the_result_or_on_any_claim() -> None:
    """The defeat, measured where it actually lived: in retained state, not in the response.

    A 4xx is not the assertion — the assertion is that no ledger entry, no result provenance
    and no claim provenance holds the caller's string. Before the fix the same request was
    answered 200 and left 90,072 bytes of it in the ledger, in three places per entry.
    """
    import importlib

    from fastapi.testclient import TestClient

    marker = "ObservedAtOverflowMarker"
    stamp = marker + "9" * 30_000
    ledger = _t367b_reset_ledger()
    try:
        with TestClient(importlib.import_module("ingest.main").create_app()) as client:
            response = client.post(
                "/extraction/policy-pages",
                json={
                    "url": "https://store.example.com/policies/observed",
                    "body": "<p>Orders ship within 2 business days.</p>",
                    "observed_at": stamp,
                },
            )

        assert 400 <= response.status_code < 500, response.status_code
        assert marker not in response.text, "the refusal echoed the stamp back"

        held: list[str] = []
        for result in ledger.results.values():
            held.append(getattr(result.provenance, "observed_at", ""))
            held.extend(claim.provenance.observed_at for claim in result.all_claims)
        assert not any(marker in text for text in held), (
            f"the caller's stamp survived into retained state in {sum(marker in t for t in held)} "
            f"place(s); this is the retention the ticket exists to close, reached through a "
            f"field the first fix never named"
        )
        assert not ledger.hashes, f"{len(ledger.hashes)} ledger entries after a 4xx"
    finally:
        _t367b_reset_ledger()


def test_t367b_an_observed_at_that_fits_the_cap_but_is_not_a_date_is_refused() -> None:
    """A cap alone is the wrong bound for a timestamp.

    ``observed_at`` becomes ``Source.observed_at`` in the graph and is what every freshness
    and ordering decision downstream reads. ``"not-a-timestamp"`` is fifteen characters — it
    passes any length ceiling — and then makes those comparisons meaningless for the life of
    the record. So the length check is the cheap half (it runs first, so no parser is ever
    pointed at a hostile string) and the parse is the half that decides whether the value is
    a time at all.
    """
    import importlib

    from fastapi.testclient import TestClient

    ledger = _t367b_reset_ledger()
    url = "https://store.example.com/policies/stamped"
    try:
        with TestClient(importlib.import_module("ingest.main").create_app()) as client:
            for junk in ("not-a-timestamp", "2026-13-45T99:99:99Z", "0", "N/A"):
                response = client.post(
                    "/extraction/policy-pages",
                    json={
                        "url": url,
                        "body": "<p>Orders ship within 2 business days.</p>",
                        "observed_at": junk,
                    },
                )
                assert 400 <= response.status_code < 500, (
                    f"{junk!r} was accepted as an observation timestamp "
                    f"({response.status_code}); it is short enough for any length cap and it "
                    f"is not a date"
                )
                assert not ledger.hashes, f"{junk!r} was refused and recorded anyway"

            # And a real one still works, in both shapes this codebase writes.
            for good in ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00"):
                ok = client.post(
                    "/extraction/policy-pages",
                    json={
                        "url": url,
                        "body": "<p>Orders ship within 2 business days.</p>",
                        "observed_at": good,
                    },
                )
                assert ok.status_code == 200, (good, ok.text)
                assert ok.json()["claims"], good
                assert ledger.results[url].provenance.observed_at == good
                _t367b_reset_ledger()
    finally:
        _t367b_reset_ledger()


def test_t367b_a_claim_provenance_refuses_to_hold_a_stamp_that_is_not_a_timestamp() -> None:
    """The invariant lives with the record, not only with the route that feeds it.

    ``ClaimProvenance`` already refused a blank ``observed_at`` because an unprovenanced
    claim is indistinguishable from a provenanced one. An unbounded or unparseable stamp is
    the same failure with a longer string: the record is retained, copied onto every claim,
    and written into the graph. Enforcing it here is what makes the bound hold for the
    scheduler and the crawl path too, not only for the one handler that was patched.
    """
    from ingest.extraction.claims import MAX_OBSERVED_AT_CHARS, ClaimProvenance, is_timestamp

    ref = "snapshot://store.example.com/policies/x@sha256:" + "0" * 64

    for junk in ("not-a-timestamp", "9" * 30_000, "9" * (MAX_OBSERVED_AT_CHARS + 1)):
        with pytest.raises(ValueError, match="observed_at") as refused:
            ClaimProvenance(source="scraped", ref=ref, observed_at=junk)
        assert junk[:200] not in str(refused.value), (
            "the ValueError quotes the offending stamp; an exception string is a place "
            "caller input gets echoed into a log by default"
        )

    for good in ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00", "2026-01-01"):
        assert is_timestamp(good), good
        assert ClaimProvenance(source="scraped", ref=ref, observed_at=good).observed_at == good


def _t367b_result_with_provenance_url(url: str) -> Any:
    """One result whose single claim's provenance carries ``url``, and nothing else varying."""
    from ingest.extraction.claims import ClaimProvenance, ExtractedClaim, ExtractionResult

    provenance = ClaimProvenance(
        source="scraped",
        ref="snapshot://store.example.com/policies/x@sha256:" + "0" * 64,
        observed_at="2026-01-01T00:00:00Z",
        url=url,
        content_hash="sha256:" + "0" * 64,
    )
    claim = ExtractedClaim(
        key="shipping.dispatch_window_days",
        value=2,
        claim_type="dispatch_window",
        confidence=0.9,
        provenance=provenance,
        evidence="ships in two days",
    )
    return ExtractionResult(claims=(claim,), content_hash="sha256:" + "0" * 64)


def test_t367b_the_ledger_charges_itself_for_the_provenance_it_retains() -> None:
    """The byte budget has to see every retained string, or it is not a budget.

    Measured before the fix: an entry holding 90,072 bytes of caller-supplied ``observed_at``
    was charged 981 bytes, because the cost function listed six field names on a claim and
    charged only ``ref`` on the result — a claim's whole ``provenance`` was in none of the
    lists. The gate is a *delta*, not a floor: two results identical except for 4,000
    characters living on a claim's provenance must differ in charge by at least those
    characters. A cost function blind to provenance answers zero here.
    """
    import importlib

    differential = importlib.import_module("ingest.extraction.differential")

    padding = 4_000
    lean = _t367b_result_with_provenance_url("https://store.example.com/p")
    fat = _t367b_result_with_provenance_url("https://store.example.com/p" + "a" * padding)

    delta = differential._result_cost(fat) - differential._result_cost(lean)
    assert delta >= padding, (
        f"{padding} extra characters retained on a claim's provenance moved the ledger's "
        f"charge by {delta} bytes; the budget cannot bound text it does not count"
    )

    # And the same text, charged through the ledger the endpoint actually writes to.
    ledger = differential.ExtractionLedger()
    ledger.record("https://store.example.com/p", "sha256:aaa", lean)
    lean_bytes = ledger.retained_bytes()
    ledger.record("https://store.example.com/p", "sha256:aaa", fat)
    assert ledger.retained_bytes() - lean_bytes >= padding, (
        "the ledger's own retained_bytes() did not move when a retained provenance grew"
    )
