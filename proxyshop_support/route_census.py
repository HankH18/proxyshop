"""Every HTTP route this repo actually SERVES, enumerated statically, and who drives it.

**Why this file exists, measured rather than asserted.** This repo's dominant recurring
defect is code that is built, unit-tested, and reached by no served request. Three instances,
each found by hand and by no gate:

* ``EXCHANGE_SHOP_ROSTER=graph`` is set in no compose file, env file or runbook anywhere in
  the tree, so ``GraphShopRoster`` is never bound and the graph retrieval that is the
  product's first step is off in every deployment — while ``apps/exchange/Dockerfile:66``
  carries a comment claiming something sets it.
* the S1 driver hand-builds a ``checkout_pixel`` ledger event at ``e2e/support/s1/flow.py``
  while the real emitter sits on the served ``POST /pixel/collect`` route
  (``apps/merchant/svc/src/collector/routes.py``), never driven.
* the buyer service served fourteen routes and published no OpenAPI contract at all, while
  every other service had one. That one is closed — ``buyer.openapi.json`` landed and this
  census compares it like the rest — and it is left written down because it is the shape of
  the class: nothing was broken, so nothing was red.

And the measurement that explains all three: the frozen acceptance suite at
``.swarm-loop/acceptance/`` — this run's scored gate, "the product works" — contains **zero**
tests that construct an app or a client. Checked by AST: no ``create_app``, no ``TestClient``,
no ``ASGITransport``, no ``httpx.Client``, in any of its nine modules or its conftest. Its
tests are not vacuous — they import product functions and assert real things about them — but
not one request is ever served, so the gate is *structurally* incapable of detecting an
unreachable feature. That suite is frozen and is not touched here. This is a gate beside it.

**Why the census is a static AST walk and not five imported app factories.** Importing five
``create_app()``s in one process makes them fight over environment variables, Postgres, Redis
and Neo4j; the census would then be answering "can this box start five services" rather than
"what does this repo serve". The walk mirrors the orchestrator-frozen ``create_app()`` that
every service shares (see ``apps/buyer/svc/src/main.py``): glob ``src/*/routes.py``, mount the
module-level ``router`` each one exports.

**The walk fails loudly rather than under-reporting, and that is the whole design.** A
decorator whose path this module cannot resolve raises :class:`UnresolvablePath`; a router
call it does not understand raises :class:`UncensusedRouterCall`; a ``routes.py`` under a
globbed service with no module-level ``router`` raises :class:`RouteCensusError`. An
under-counting census would turn the reachability gate into a green light for exactly the
routes it could not see — which is the failure mode this file exists to prevent, reproduced
one level up. ``proxyshop_support/tests/test_route_reachability.py`` proves each refusal
against a synthetic routes module rather than trusting this paragraph.

Paths are therefore resolved, not merely read: several routes spell their path with a
module-level constant (``COLLECTOR_PATH`` in ``merchant_svc.install.config``, reached through
an import; ``REPORTS_PATH`` and ``CODES_PATH`` defined in their own route modules;
``WEBHOOK_PATH_PREFIX`` inside an f-string). :class:`ConstantIndex` follows the import to the
defining module using the repo's own ``.pkgroot`` symlinks, so it needs no package installed
and no ``sys.path`` games.

**This module ships.** ``COPY proxyshop_support/ /app/proxyshop_support/`` appears in seven of
this repo's Dockerfiles, so nothing at module scope may import anything an image might not
install: standard library only here, and ``.pkgroot`` is read lazily inside functions rather
than at import, because an image has no ``.pkgroot``.

Runnable::

    python -m proxyshop_support.route_census            # the table, plus the driven split
    python -m proxyshop_support.route_census --json     # the same, machine-readable
    python -m proxyshop_support.route_census --undriven # just the work queue
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import sys
import tokenize
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CLIENT_MARKERS",
    "code_text",
    "expanded_urls",
    "SERVICES",
    "ConstantIndex",
    "Route",
    "RouteCensusError",
    "Service",
    "UncensusedRouterCall",
    "UnresolvablePath",
    "census",
    "client_marker_re",
    "contract_divergence",
    "driven_split",
    "main",
    "route_files",
    "route_path_regex",
    "routes_in",
    "test_sources",
]

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: The verbs an ``APIRouter`` exposes as decorators. ``trace`` is included for completeness;
#: nothing in this repo serves one, and a route that appeared on it would be censused rather
#: than skipped, which is the only direction this module is allowed to be wrong in.
HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "options", "trace"}
)

#: Every attribute of a router that can ADD a served surface. Any of these encountered on a
#: name bound to ``APIRouter`` and not consumed by the walk raises: an unrecognised way of
#: mounting a route is indistinguishable, from the outside, from a route that does not exist.
ROUTER_SURFACE_CALLS: Final[frozenset[str]] = HTTP_METHODS | {
    "api_route",
    "add_api_route",
    "add_api_websocket_route",
    "add_route",
    "add_websocket_route",
    "host",
    "include_router",
    "mount",
    "route",
    "websocket",
    "websocket_route",
}


class RouteCensusError(RuntimeError):
    """The census could not answer honestly, so it refuses to answer at all."""


class UnresolvablePath(RouteCensusError):
    """A route's path is an expression this module cannot evaluate to a string."""


class UncensusedRouterCall(RouteCensusError):
    """A router call that adds a surface the walk did not turn into a :class:`Route`."""


@dataclass(frozen=True)
class Service:
    """One deployable that answers HTTP, and where its route modules live.

    ``globs`` mirrors the service's own ``create_app()``. For the six services that share the
    orchestrator-frozen factory that is ``src/*/routes.py``; the Shopify stub builds its app
    by hand out of two router factories in one module, so it names that module directly.

    ``mounted_router`` is the name ``create_app()`` actually mounts. Restricting the census to
    it is what keeps a router that is defined and never included from being reported as
    served. ``None`` means "every router in the file", which is right for the stub because its
    ``create_app`` includes both of the routers its module builds.
    """

    name: str
    root: str
    package: str
    globs: tuple[str, ...]
    mounted_router: str | None = "router"
    contract: str | None = None
    dev_double: bool = False


#: The seven things in this repo that answer HTTP. ``apps/seller-reference`` is a library, not
#: a service — it has no ``routes.py`` and no ``main.py`` — and ``services/sim`` is a batch
#: harness, so neither appears. Both are asserted absent by the census's own tests, because a
#: service that grew routes and was never added here would be invisible to the gate.
SERVICES: Final[tuple[Service, ...]] = (
    Service(
        name="exchange",
        root="apps/exchange",
        package="exchange",
        globs=("src/*/routes.py",),
        contract="packages/contracts/openapi/exchange.openapi.json",
    ),
    Service(
        name="buyer",
        root="apps/buyer/svc",
        package="buyer_svc",
        globs=("src/*/routes.py",),
        contract="packages/contracts/openapi/buyer.openapi.json",
    ),
    Service(
        name="merchant",
        root="apps/merchant/svc",
        package="merchant_svc",
        globs=("src/*/routes.py",),
        contract="packages/contracts/openapi/merchant.openapi.json",
    ),
    Service(
        name="trust",
        root="apps/trust",
        package="trust",
        globs=("src/*/routes.py",),
        contract="packages/contracts/openapi/trust.openapi.json",
    ),
    Service(
        name="ingest",
        root="services/ingest",
        package="ingest",
        globs=("src/*/routes.py",),
        contract="packages/contracts/openapi/ingest.openapi.json",
    ),
    Service(
        name="store-agent",
        root="packages/store-agent",
        package="store_agent",
        globs=("src/*/routes.py",),
        contract="packages/contracts/openapi/store-agent.openapi.json",
    ),
    Service(
        name="shopify-stub",
        root="services/shopify-stub",
        package="shopify_stub",
        globs=("src/app.py",),
        mounted_router=None,
        contract=None,
        dev_double=True,
    ),
)


@dataclass(frozen=True)
class Route:
    """One served (method, path), and where the code that answers it lives.

    ``aliases`` are the module-level constant names the path was spelled with —
    ``COLLECTOR_PATH``, ``WEBHOOK_PATH_PREFIX``, ``DASHBOARD_MOUNT``. Tests in this repo call
    those constants rather than retyping the string, so a reachability check that only looked
    for the literal ``/pixel/collect`` would report a heavily-driven route as undriven.
    """

    service: str
    method: str
    path: str
    handler: str
    file: str
    line: int
    kind: str = "route"
    aliases: tuple[str, ...] = ()
    router: str = "router"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.service, self.method, self.path)

    def __str__(self) -> str:
        return f"{self.service} {self.method:7} {self.path}"

    def located(self) -> str:
        return f"{self.service} {self.method:7} {self.path}  ->  {self.file}:{self.line}"


# =====================================================================================
# Resolving a path expression to a string
# =====================================================================================
class ConstantIndex:
    """Module-level string constants, following ``from X import Y`` to the defining module.

    Import resolution goes through the repo's ``.pkgroot`` symlinks — ``merchant_svc`` ->
    ``apps/merchant/svc/src`` — which is the same mapping ``pyproject.toml`` puts on
    ``pythonpath``. Nothing is imported: the defining module is parsed, not executed, so a
    constant can be read out of a module whose imports need a database.
    """

    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = (repo_root or REPO_ROOT).resolve()
        self._parsed: dict[Path, tuple[dict[str, ast.expr], dict[str, tuple[str, int, str]]]] = {}
        self._roots: dict[str, Path] | None = None

    # -- package map -------------------------------------------------------------------
    @property
    def package_roots(self) -> dict[str, Path]:
        """``{"merchant_svc": <repo>/apps/merchant/svc/src, ...}`` from ``.pkgroot``."""
        if self._roots is None:
            pkgroot = self.repo_root / ".pkgroot"
            if not pkgroot.is_dir():
                raise RouteCensusError(
                    f"no {pkgroot} — the census resolves imported path constants through the "
                    "repo's own package symlinks and cannot run outside a checkout"
                )
            self._roots = {
                child.name: child.resolve() for child in sorted(pkgroot.iterdir()) if child.is_dir()
            }
        return self._roots

    def module_file(self, dotted: str) -> Path | None:
        """The file defining ``merchant_svc.install.config``, or ``None`` if it is not ours."""
        head, *rest = dotted.split(".")
        base = self.package_roots.get(head)
        if base is None:
            return None
        candidate = base.joinpath(*rest) if rest else base
        if candidate.with_suffix(".py").is_file():
            return candidate.with_suffix(".py")
        init = candidate / "__init__.py"
        return init if init.is_file() else None

    # -- one module's own bindings -----------------------------------------------------
    def _bindings(self, file: Path) -> tuple[dict[str, ast.expr], dict[str, tuple[str, int, str]]]:
        cached = self._parsed.get(file)
        if cached is not None:
            return cached
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        assigned: dict[str, ast.expr] = {}
        imported: dict[str, tuple[str, int, str]] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned[target.id] = node.value
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.value is not None:
                    assigned[node.target.id] = node.value
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imported[alias.asname or alias.name] = (
                        node.module or "",
                        node.level,
                        alias.name,
                    )
        self._parsed[file] = (assigned, imported)
        return assigned, imported

    def value(self, file: Path, name: str, _seen: frozenset[tuple[Path, str]] = frozenset()) -> str:
        """The string ``name`` is bound to at module scope in ``file``.

        Raises :class:`UnresolvablePath` when it is not bound to something this module can
        evaluate — deliberately, rather than returning ``None`` and letting a caller decide
        to skip the route.
        """
        if (file, name) in _seen:
            raise UnresolvablePath(f"{file}: `{name}` resolves in a cycle")
        assigned, imported = self._bindings(file)
        if name in assigned:
            return self.literal(assigned[name], file, _seen=_seen | {(file, name)})
        if name in imported:
            module, level, original = imported[name]
            target = self._relative_module(file, module, level) if level else module
            defining = self.module_file(target) if target else None
            if defining is None:
                raise UnresolvablePath(
                    f"{file}: `{name}` is imported from `{target or module}`, which is not a "
                    "first-party module the census can read"
                )
            return self.value(defining, original, _seen=_seen | {(file, name)})
        raise UnresolvablePath(f"{file}: `{name}` is not bound at module scope")

    def _relative_module(self, file: Path, module: str, level: int) -> str:
        """``from .config import X`` inside ``a/b/routes.py`` -> ``a.b.config``, dotted."""
        here = file.parent if file.name != "__init__.py" else file.parent.parent
        for _ in range(level - 1):
            here = here.parent
        for package, root in self.package_roots.items():
            try:
                inside = here.relative_to(root)
            except ValueError:
                continue
            prefix = ".".join((package, *inside.parts))
            return f"{prefix}.{module}" if module else prefix
        raise UnresolvablePath(f"{file}: a relative import from outside every package root")

    # -- expression evaluation ----------------------------------------------------------
    def literal(
        self,
        node: ast.expr,
        file: Path,
        *,
        names: set[str] | None = None,
        _seen: frozenset[tuple[Path, str]] = frozenset(),
    ) -> str:
        """Evaluate a path expression to a string, or raise :class:`UnresolvablePath`.

        Handles the four shapes this repo actually uses — a literal, a module constant, an
        imported module constant, and an f-string interpolating one — plus ``+`` concatenation
        because it costs nothing. Everything else raises, including a value that is a real
        string but computed at runtime (``f"/x/{make_id()}"``), because a census that guessed
        there would be reporting a path no request can hit.

        ``names`` collects every identifier the evaluation went through, which is what
        :attr:`Route.aliases` is built from.
        """
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return node.value
            raise UnresolvablePath(
                f"{file}:{node.lineno}: a route path that is a {type(node.value).__name__}"
            )
        if isinstance(node, ast.Name):
            if names is not None:
                names.add(node.id)
            return self.value(file, node.id, _seen=_seen)
        if isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            if dotted is None:
                raise UnresolvablePath(
                    f"{file}:{node.lineno}: a route path read off a computed attribute"
                )
            head, _, attribute = dotted.rpartition(".")
            defining = self.module_file(head)
            if defining is None:
                raise UnresolvablePath(
                    f"{file}:{node.lineno}: a route path read off `{dotted}`, which the census "
                    "cannot trace to a first-party module"
                )
            if names is not None:
                names.add(attribute)
            return self.value(defining, attribute, _seen=_seen)
        if isinstance(node, ast.JoinedStr):
            out: list[str] = []
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    out.append(part.value)
                elif isinstance(part, ast.FormattedValue):
                    if part.conversion not in (-1, ord("s")) or part.format_spec is not None:
                        raise UnresolvablePath(
                            f"{file}:{node.lineno}: an f-string route path with a conversion or "
                            "format spec the census will not evaluate"
                        )
                    out.append(self.literal(part.value, file, names=names, _seen=_seen))
                else:  # pragma: no cover - CPython emits only the two node types above
                    raise UnresolvablePath(f"{file}:{node.lineno}: an unreadable f-string part")
            return "".join(out)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.literal(node.left, file, names=names, _seen=_seen) + self.literal(
                node.right, file, names=names, _seen=_seen
            )
        raise UnresolvablePath(
            f"{file}:{getattr(node, 'lineno', '?')}: a route path spelled as "
            f"{type(node).__name__}, which the census cannot evaluate statically. Spell it as a "
            "module-level string constant so the route can be counted."
        )


def _dotted(node: ast.expr) -> str | None:
    """``a.b.C`` -> ``"a.b.C"``; ``None`` when any link is not a plain name."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


# =====================================================================================
# The walk
# =====================================================================================
@dataclass
class _Walk:
    """One route module's walk state. Separate from :class:`ConstantIndex` on purpose."""

    service: Service
    file: Path
    module: str
    index: ConstantIndex
    routes: list[Route] = field(default_factory=list)
    router_prefixes: dict[str, str] = field(default_factory=dict)
    all_router_names: set[str] = field(default_factory=set)
    consumed: set[int] = field(default_factory=set)


def _is_api_router(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "APIRouter"
    return isinstance(func, ast.Attribute) and func.attr == "APIRouter"


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _bind_routers(walk: _Walk, body: Sequence[ast.stmt], routers: dict[str, str]) -> None:
    """Record ``<name> = APIRouter(prefix=...)`` bindings visible in ``body``."""
    for statement in body:
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            value, targets = statement.value, list(statement.targets)
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            value, targets = statement.value, [statement.target]
        if value is None or not _is_api_router(value):
            continue
        assert isinstance(value, ast.Call)
        prefix_node = _keyword(value, "prefix")
        prefix = "" if prefix_node is None else walk.index.literal(prefix_node, walk.file)
        for target in targets:
            if isinstance(target, ast.Name):
                routers[target.id] = prefix
                walk.all_router_names.add(target.id)


def _router_of(node: ast.expr, routers: dict[str, str]) -> tuple[str, str] | None:
    """``router.post`` -> ``("router", "post")`` when ``router`` is a bound router here."""
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return None
    if node.value.id not in routers:
        return None
    return node.value.id, node.attr


def _methods_of(call: ast.Call, walk: _Walk, keyword: str) -> list[str]:
    node = _keyword(call, keyword)
    if node is None:
        raise UnresolvablePath(
            f"{walk.file}:{call.lineno}: a router call with no `{keyword}=`, so the census "
            "cannot say which verbs it serves"
        )
    if not isinstance(node, ast.List | ast.Tuple | ast.Set):
        raise UnresolvablePath(
            f"{walk.file}:{call.lineno}: `{keyword}=` is not a literal sequence of verbs"
        )
    out: list[str] = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            raise UnresolvablePath(f"{walk.file}:{call.lineno}: a non-literal HTTP verb")
        out.append(element.value.upper())
    return out


def _path_argument(call: ast.Call, walk: _Walk, position: int = 0) -> tuple[str, set[str]]:
    node = _keyword(call, "path")
    if node is None:
        if len(call.args) <= position:
            raise UnresolvablePath(
                f"{walk.file}:{call.lineno}: a router call with no path argument at all"
            )
        node = call.args[position]
    names: set[str] = set()
    return walk.index.literal(node, walk.file, names=names), names


def _join(prefix: str, path: str) -> str:
    """FastAPI's own rule: the prefix is a literal prefix and an empty path means the prefix."""
    joined = f"{prefix}{path}"
    return joined or "/"


def _record(
    walk: _Walk,
    *,
    methods: Iterable[str],
    prefix: str,
    path: str,
    aliases: Iterable[str],
    handler: str,
    line: int,
    router: str,
    kind: str = "route",
) -> None:
    full = _join(prefix, path)
    for method in methods:
        walk.routes.append(
            Route(
                service=walk.service.name,
                method=method,
                path=full,
                handler=handler,
                file=str(walk.file.relative_to(walk.index.repo_root)),
                line=line,
                kind=kind,
                aliases=tuple(sorted(aliases)),
                router=router,
            )
        )


def _handle_decorator(
    walk: _Walk, decorator: ast.expr, routers: dict[str, str], qualname: str
) -> None:
    if isinstance(decorator, ast.Attribute):
        found = _router_of(decorator, routers)
        if found is not None and found[1] in ROUTER_SURFACE_CALLS:
            raise UncensusedRouterCall(
                f"{walk.file}:{decorator.lineno}: `@{found[0]}.{found[1]}` used without a call, "
                "which the census does not understand"
            )
        return
    if not isinstance(decorator, ast.Call):
        return
    found = _router_of(decorator.func, routers)
    if found is None:
        return
    name, attribute = found
    if attribute not in ROUTER_SURFACE_CALLS:
        return
    assert isinstance(decorator.func, ast.Attribute)
    walk.consumed.add(id(decorator.func))
    prefix = routers[name]
    if attribute in HTTP_METHODS:
        path, aliases = _path_argument(decorator, walk)
        _record(
            walk,
            methods=[attribute.upper()],
            prefix=prefix,
            path=path,
            aliases=aliases,
            handler=qualname,
            line=decorator.lineno,
            router=name,
        )
        return
    if attribute in {"api_route", "route"}:
        path, aliases = _path_argument(decorator, walk)
        _record(
            walk,
            methods=_methods_of(decorator, walk, "methods"),
            prefix=prefix,
            path=path,
            aliases=aliases,
            handler=qualname,
            line=decorator.lineno,
            router=name,
        )
        return
    if attribute in {"websocket", "websocket_route"}:
        path, aliases = _path_argument(decorator, walk)
        _record(
            walk,
            methods=["WEBSOCKET"],
            prefix=prefix,
            path=path,
            aliases=aliases,
            handler=qualname,
            line=decorator.lineno,
            router=name,
            kind="websocket",
        )
        return
    raise UncensusedRouterCall(
        f"{walk.file}:{decorator.lineno}: `@{name}.{attribute}(...)` adds a served surface the "
        "census does not know how to count"
    )


def _handle_call(walk: _Walk, call: ast.Call, routers: dict[str, str], scope: str) -> None:
    found = _router_of(call.func, routers)
    if found is None:
        return
    name, attribute = found
    if attribute not in ROUTER_SURFACE_CALLS:
        return
    assert isinstance(call.func, ast.Attribute)
    walk.consumed.add(id(call.func))
    prefix = routers[name]
    if attribute == "mount":
        path, aliases = _path_argument(call, walk)
        mounted = call.args[1] if len(call.args) > 1 else _keyword(call, "app")
        described = ast.unparse(mounted) if mounted is not None else "<unknown>"
        _record(
            walk,
            methods=["MOUNT"],
            prefix=prefix,
            path=path,
            aliases=aliases,
            handler=f"{scope} -> {described}",
            line=call.lineno,
            router=name,
            kind="mount",
        )
        return
    if attribute in {"add_api_route", "add_route"}:
        path, aliases = _path_argument(call, walk)
        endpoint = call.args[1] if len(call.args) > 1 else _keyword(call, "endpoint")
        described = ast.unparse(endpoint) if endpoint is not None else "<unknown>"
        _record(
            walk,
            methods=_methods_of(call, walk, "methods"),
            prefix=prefix,
            path=path,
            aliases=aliases,
            handler=f"{scope} -> {described}",
            line=call.lineno,
            router=name,
        )
        return
    if attribute in {"add_websocket_route", "add_api_websocket_route"}:
        path, aliases = _path_argument(call, walk)
        _record(
            walk,
            methods=["WEBSOCKET"],
            prefix=prefix,
            path=path,
            aliases=aliases,
            handler=scope,
            line=call.lineno,
            router=name,
            kind="websocket",
        )
        return
    raise UncensusedRouterCall(
        f"{walk.file}:{call.lineno}: `{name}.{attribute}(...)` adds a served surface the census "
        "does not know how to count. `include_router` in particular would hide every route of "
        "the router it folds in, so it is refused rather than skipped."
    )


def _walk_body(walk: _Walk, body: Sequence[ast.stmt], routers: dict[str, str], scope: str) -> None:
    _bind_routers(walk, body, routers)
    for statement in body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            qualname = f"{scope}.{statement.name}"
            for decorator in statement.decorator_list:
                _handle_decorator(walk, decorator, routers, qualname)
            _walk_body(walk, statement.body, dict(routers), qualname)
            continue
        if isinstance(statement, ast.ClassDef):
            _walk_body(walk, statement.body, dict(routers), f"{scope}.{statement.name}")
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            _handle_call(walk, statement.value, routers, scope)
            continue
        nested = [
            block
            for attribute in ("body", "orelse", "finalbody")
            for block in [getattr(statement, attribute, None)]
            if isinstance(block, list)
        ]
        for block in nested:
            _walk_body(walk, block, routers, scope)
        for handler in getattr(statement, "handlers", []) or []:
            _walk_body(walk, handler.body, routers, scope)


def _assert_nothing_missed(walk: _Walk, tree: ast.AST) -> None:
    """Every router-surface attribute in the file must have become a :class:`Route`.

    This is the loud-failure net. The walk above understands the shapes this repo uses today;
    a route added tomorrow through a shape it does not understand would otherwise be silently
    absent, and a census that silently under-reports is worse than no census at all — it turns
    the reachability gate green for exactly the routes it cannot see.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in ROUTER_SURFACE_CALLS:
            continue
        if not isinstance(node.value, ast.Name) or node.value.id not in walk.all_router_names:
            continue
        if id(node) in walk.consumed:
            continue
        raise UncensusedRouterCall(
            f"{walk.file}:{node.lineno}: `{node.value.id}.{node.attr}` reaches a router's "
            "route table in a shape the census did not count. Every served route must be "
            "visible to the reachability gate; extend proxyshop_support/route_census.py "
            "rather than leaving this one uncounted."
        )


def routes_in(service: Service, file: Path, index: ConstantIndex) -> list[Route]:
    """Every route ``file`` contributes to ``service``'s served surface."""
    source = file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(file))
    module = _module_name(service, file, index)
    walk = _Walk(service=service, file=file, module=module, index=index)
    _walk_body(walk, tree.body, {}, module)
    _assert_nothing_missed(walk, tree)
    if service.mounted_router is not None:
        if service.mounted_router not in walk.all_router_names:
            raise RouteCensusError(
                f"{file}: exports no module-level `{service.mounted_router}`. The frozen "
                f"create_app() for {service.name} globs src/*/routes.py and mounts that name; "
                "a module without it serves nothing and its presence here is a half-landed "
                "ticket, not a route module."
            )
        walk.routes = [route for route in walk.routes if route.router == service.mounted_router]
    return sorted(walk.routes, key=lambda route: (route.path, route.method))


def _module_name(service: Service, file: Path, index: ConstantIndex) -> str:
    root = index.repo_root / service.root / "src"
    inside = file.relative_to(root)
    parts = [*inside.parts[:-1], inside.stem]
    return ".".join((service.package, *parts))


def route_files(service: Service, repo_root: Path | None = None) -> list[Path]:
    """The route modules ``service``'s ``create_app()`` would discover, in its own order."""
    base = (repo_root or REPO_ROOT).resolve() / service.root
    found: list[Path] = []
    for pattern in service.globs:
        found.extend(sorted(base.glob(pattern)))
    return found


def census(services: Sequence[Service] = SERVICES, repo_root: Path | None = None) -> list[Route]:
    """Every route every service in ``services`` serves. Raises rather than under-reporting."""
    index = ConstantIndex(repo_root)
    out: list[Route] = []
    for service in services:
        files = route_files(service, index.repo_root)
        if not files:
            raise RouteCensusError(
                f"{service.name}: no route module matched {list(service.globs)} under "
                f"{service.root}. A service that serves nothing is a finding, not a silence."
            )
        for file in files:
            out.extend(routes_in(service, file, index))
    return out


# =====================================================================================
# Who drives what
# =====================================================================================
_IGNORED_TOKENS: Final[frozenset[int]] = frozenset({tokenize.NL, tokenize.COMMENT})


def _significant(tokens: Sequence[tokenize.TokenInfo], index: int, step: int) -> int | None:
    cursor = index + step
    while 0 <= cursor < len(tokens):
        if tokens[cursor].type not in _IGNORED_TOKENS:
            return tokens[cursor].type
        cursor += step
    return None


#: Decorator attributes that mean "the file this appears in SERVES that path".
#:
#: A test module that stands up a double — ``@app.get("/reports/losses")`` in
#: ``apps/merchant/svc/tests/_fixtures_dashboard.py:180``, ``@app.get("/snapshot")`` at :199,
#: ``@app.post("/events")`` in two more — is the OPPOSITE of a driver: it answers that path
#: rather than calling it. Left in, those four modules credited three real routes they never
#: request. The decorator's arguments are therefore blanked along with the comments.
_SERVER_DECORATORS: Final[frozenset[str]] = HTTP_METHODS | {
    "api_route",
    "add_api_route",
    "route",
    "websocket",
    "websocket_route",
}


def _blank(grid: list[list[str]], start: tuple[int, int], end: tuple[int, int]) -> None:
    (start_row, start_col), (end_row, end_col) = start, end
    for row in range(start_row, end_row + 1):
        line = grid[row - 1]
        first = start_col if row == start_row else 0
        last = end_col if row == end_row else len(line)
        for column in range(first, min(last, len(line))):
            if line[column] not in "\r\n":
                line[column] = " "


def _server_decorator_spans(
    tokens: Sequence[tokenize.TokenInfo],
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """The argument list of every ``@x.get(...)``-shaped decorator, as blankable spans."""
    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for position, token in enumerate(tokens):
        if token.type != tokenize.OP or token.string != "@":
            continue
        cursor = position + 1
        last_name: str | None = None
        while cursor < len(tokens) and tokens[cursor].type in (tokenize.NAME, tokenize.OP):
            current = tokens[cursor]
            if current.type == tokenize.NAME:
                last_name = current.string
            elif current.string != ".":
                break
            cursor += 1
        if last_name not in _SERVER_DECORATORS:
            continue
        if cursor >= len(tokens) or tokens[cursor].string != "(":
            continue
        depth = 0
        for scan in range(cursor, len(tokens)):
            if tokens[scan].type != tokenize.OP:
                continue
            if tokens[scan].string in "([{":
                depth += 1
            elif tokens[scan].string in ")]}":
                depth -= 1
                if depth == 0:
                    spans.append((tokens[cursor].start, tokens[scan].end))
                    break
    return spans


def code_text(source: str) -> str:
    """``source`` with comments, bare-string statements and route decorators blanked out.

    **This is not a nicety; it is the difference between a real answer and a wrong one.**
    Measured on this tree: ``POST /claims/verifications`` is named in exactly four test files
    and in all four the mention is a comment or a docstring — an ASCII service diagram in
    ``apps/exchange/tests/test_repro_open_tickets.py``, a note in
    ``apps/trust/tests/test_repro_open_tickets.py``, and a paragraph in ``e2e/test_s1_flow.py``
    describing a route that suite does not call. A raw-text scan reports that route as driven;
    it is not driven by anything. Prose about a route is the single easiest way to fool this
    heuristic, and it is the one distortion cheap enough to remove outright.

    Route decorators go the same way, for the same reason turned inside out: a test module that
    writes ``@app.get("/reports/losses")`` is standing up a DOUBLE of that route, not calling
    it. Counting it as a driver credits the route to the one file guaranteed never to request
    it. See :data:`_SERVER_DECORATORS`.

    Blanking rather than deleting keeps every line number and column intact, so a match's
    position still points at real code. A file that will not tokenise is returned unchanged —
    a syntax error in a test file is somebody else's gate, and failing here would turn this
    module into a second, worse linter.
    """
    grid = [list(line) for line in source.splitlines(keepends=True)]
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (IndentationError, SyntaxError, tokenize.TokenError, ValueError):
        return source
    for position, token in enumerate(tokens):
        drop = token.type == tokenize.COMMENT
        if token.type == tokenize.STRING and not drop:
            before = _significant(tokens, position, -1)
            after = _significant(tokens, position, 1)
            drop = after == tokenize.NEWLINE and before in (
                None,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENCODING,
            )
        if drop:
            _blank(grid, token.start, token.end)
    for start, end in _server_decorator_spans(tokens):
        _blank(grid, start, end)
    return "".join("".join(line) for line in grid)


#: Text that means "this file builds something a request can be sent to".
#:
#: Deliberately a fixed, readable list rather than a clever inference. Measured against the
#: repo: ``TestClient`` appears in 67 test modules, ``create_app`` in 95, ``AsyncClient`` in
#: 13 and ``ASGITransport`` in 2; ``serve`` is ``proxyshop_support.asgi_server.serve``, which
#: runs a real app on a loopback socket and is how the e2e suite and several service tests
#: drive a service over a real connection.
#:
#: **Every one is matched on a word boundary, which is not pedantry.** Spelled as the plain
#: substring ``serve(``, this list matched the local helpers ``_serve(`` and ``_observe(`` —
#: and so admitted ``packages/contracts/tests/test_repro_open_tickets.py``, a file that
#: imports ``ast``, ``json``, ``pathlib`` and ``pytest`` and constructs no client at all, as a
#: driver of five routes.
CLIENT_MARKERS: Final[tuple[str, ...]] = (
    "TestClient",
    "ASGITransport",
    "AsyncClient",
    "httpx.Client",
    "create_app",
    "asgi_server",
    "serve(",
    "service_launch",
    "launch_service",
)

_CLIENT_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(rf"(?<![A-Za-z0-9_]){re.escape(marker)}" for marker in CLIENT_MARKERS)
)


def client_marker_re() -> re.Pattern[str]:
    """The compiled :data:`CLIENT_MARKERS` alternation, exposed so it can be tested."""
    return _CLIENT_RE


#: Where test code lives. Every directory literally named ``tests``, plus the two suites that
#: are not shaped that way: ``e2e/`` (whose ``test_*.py`` modules delegate the actual driving
#: to ``e2e/support/**``, so the support modules must be scanned too) and the frozen
#: acceptance suite, which is included precisely so that its contribution is measured rather
#: than assumed — it contributes nothing, and that is the finding.
_EXTRA_TEST_ROOTS: Final[tuple[str, ...]] = ("e2e", ".swarm-loop/acceptance")

#: Test-driving code that lives outside every test directory, named one file at a time and
#: with the reason on the record, because a wildcard here would quietly widen what counts as
#: evidence.
#:
#: ``shopify_stub.testing`` is a typed client for the stub's own control surface. Its module
#: docstring says why it sits in ``src/``: ``services/shopify-stub`` has a hyphen and cannot be
#: a Python package, so under pytest's ``importlib`` import mode a test module cannot import a
#: sibling helper by name. Eight test modules across four services import ``StubClient`` from
#: it, and every ``/_stub`` call they make goes through it — so leaving it out reports seven of
#: the stub's fourteen routes as undriven when they are driven on every stub test run.
_EXTRA_TEST_SOURCES: Final[tuple[str, ...]] = ("services/shopify-stub/src/testing.py",)

#: A gate may not be its own evidence, and this one nearly was.
#:
#: ``proxyshop_support/tests/test_route_reachability.py`` names thirteen routes in real code —
#: the allowlist keys, the constant-resolution assertions — and it also names ``"create_app"``
#: inside a tuple of markers, so it looked to this scan exactly like a test module that builds
#: an app and calls thirteen paths. Measured: with it in the corpus it became the single
#: largest "driver" in the repo, and the one genuinely undriven route was credited to the very
#: file that documents it as undriven. It issues no request; it is excluded by name.
_EXCLUDED_TEST_SOURCES: Final[tuple[str, ...]] = (
    "proxyshop_support/tests/test_route_reachability.py",
)

_SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)


@lru_cache(maxsize=8)
def test_sources(repo_root: Path | None = None) -> tuple[Path, ...]:
    """Every ``.py`` file that is test code, anywhere in the repo."""
    root = (repo_root or REPO_ROOT).resolve()
    found: set[Path] = set()
    for directory, subdirectories, filenames in _walk_tree(root):
        subdirectories[:] = [name for name in subdirectories if name not in _SKIP_DIRS]
        if directory.name == "tests":
            found.update(directory.rglob("*.py"))
            continue
        for filename in filenames:
            if filename.startswith("test_") and filename.endswith(".py"):
                found.add(directory / filename)
            elif filename == "conftest.py":
                found.add(directory / filename)
    for extra in _EXTRA_TEST_ROOTS:
        base = root / extra
        if base.is_dir():
            found.update(
                path
                for path in base.rglob("*.py")
                if not any(part in _SKIP_DIRS for part in path.parts)
            )
    for named in _EXTRA_TEST_SOURCES:
        candidate = root / named
        if not candidate.is_file():
            raise RouteCensusError(
                f"{named} is listed as test-driving code outside the test directories and is "
                "not there any more. Delete the entry rather than letting the evidence scan "
                "silently narrow."
            )
        found.add(candidate)
    found.difference_update(root / named for named in _EXCLUDED_TEST_SOURCES)
    return tuple(sorted(found))


def _walk_tree(root: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
    for directory, subdirectories, filenames in os.walk(root):
        yield Path(directory), subdirectories, filenames


_SEGMENT: Final[str] = r"[^/\s\"'`<>]+"
_GREEDY_SEGMENT: Final[str] = r"[^\s\"'`<>]+"
_PARAM_RE: Final[re.Pattern[str]] = re.compile(r"\{([^{}]*)\}")


def route_path_regex(path: str) -> re.Pattern[str]:
    """A regex matching a concrete spelling of ``path`` inside source text.

    ``/stores/{store_id}/dashboard`` has to match ``f"/stores/{store_id}/dashboard"`` and
    ``"/stores/store-1/dashboard"`` alike, so each ``{...}`` becomes one path segment. A
    ``:path`` converter is allowed to swallow slashes, because that is what it does on the
    wire. The lookaround stops ``/events`` from matching inside ``/events/head`` and
    ``/dashboard`` from matching inside ``/stores/x/dashboard``.

    **``}`` belongs in the trailing lookahead, and leaving it out was a live defect.** With the
    class spelled ``[A-Za-z0-9_./-]``, the segment wildcard could backtrack one character and
    stop *inside* an f-string placeholder: ``/auctions/{auction_id}`` matched the text
    ``/auctions/{auction_id`` in ``client.post(f"/auctions/{auction_id}/accept")``, because the
    next character was ``}`` and nothing forbade it. Measured consequence: ``GET
    /auctions/{auction_id}`` was credited to eighteen files, only four of which ever issue that
    GET — the other fourteen only POST an accept or GET a shortlist under the same prefix.
    """
    out: list[str] = []
    cursor = 0
    for match in _PARAM_RE.finditer(path):
        out.append(re.escape(path[cursor : match.start()]))
        out.append(_GREEDY_SEGMENT if match.group(1).endswith(":path") else _SEGMENT)
        cursor = match.end()
    out.append(re.escape(path[cursor:]))
    return re.compile(r"(?<![A-Za-z0-9_/.-])" + "".join(out) + r"(?![A-Za-z0-9_./{}-])")


def _alias_regex(alias: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(alias)}\b")


def _render_string(node: ast.expr, constants: dict[str, str]) -> str | None:
    """A test file's URL expression, rendered with its own module constants folded in.

    ``{}`` stands in for anything that is only known at run time, which is exactly the shape
    :func:`_path_regex` expects a path parameter to have.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else "{}"
    if isinstance(node, ast.Name):
        return constants.get(node.id, "{}")
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                parts.append(_render_string(part.value, constants) or "{}")
            else:  # pragma: no cover - CPython emits only the two node types above
                parts.append("{}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _render_string(node.left, constants)
        right = _render_string(node.right, constants)
        return None if left is None or right is None else left + right
    return "{}"


def expanded_urls(source: str) -> str:
    """Every ``f"..."`` / ``"a" + b`` in ``source``, with the file's own constants folded in.

    **Why this exists, measured.** Tests in this repo do not retype a path per call; they bind
    it once and interpolate — ``AUCTION_VIEW = "/buyer/auctions"`` in
    ``apps/buyer/svc/tests/test_auctions_shortlist.py``, then
    ``client.get(f"{AUCTION_VIEW}/{auction_id}")``. A scan of the raw text finds
    ``/buyer/auctions`` and never ``/buyer/auctions/{auction_id}``, so the most heavily driven
    route in that suite reads as undriven. Without this expansion the undriven list is wrong in
    the direction that matters least for safety and most for usefulness: it manufactures work
    that is already done, and a work queue nobody trusts is a work queue nobody reads.

    Only module-level ``NAME = "literal"`` bindings are folded; anything else becomes ``{}``.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    constants: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value.value
    rendered: list[str] = []
    for candidate in ast.walk(tree):
        if isinstance(candidate, ast.JoinedStr | ast.BinOp):
            text = _render_string(candidate, constants)
            if text and "/" in text:
                rendered.append(text)
    return "\n".join(rendered)


@dataclass(frozen=True)
class _Source:
    """One test file, in the two forms the evidence scan reads it in."""

    path: Path
    code: str
    searchable: str


@lru_cache(maxsize=2048)
def _source(path: Path, _stamp: int) -> _Source:
    """One test file's two searchable forms, keyed by its modification time.

    Roughly three hundred files are tokenised twice per :func:`driven_split`, and the gate
    calls it more than once. ``_stamp`` is the file's ``st_mtime_ns``, so an edited file is
    re-read and an unchanged one is not — the cache can never serve a stale answer.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    code = code_text(raw)
    return _Source(path=path, code=code, searchable=f"{code}\n{expanded_urls(raw)}")


def _drivers_for(route: Route, sources: Sequence[_Source], siblings: frozenset[str]) -> list[Path]:
    """Test files that construct a client AND name ``route``'s path in code."""
    pattern = route_path_regex(route.path)
    aliases = [_alias_regex(alias) for alias in route.aliases]
    out: list[Path] = []
    for source in sources:
        if not _CLIENT_RE.search(source.code):
            continue
        named = any(
            match.group(0) not in siblings for match in pattern.finditer(source.searchable)
        ) or any(alias.search(source.code) for alias in aliases)
        if named:
            out.append(source.path)
    return out


def driven_split(
    routes: Sequence[Route], repo_root: Path | None = None
) -> tuple[dict[tuple[str, str, str], list[str]], list[Route]]:
    """``({route key: [driving files]}, [undriven routes])``.

    The evidence is STATIC and its limits are stated at the strength they deserve in the module
    docstring of ``proxyshop_support/tests/test_route_reachability.py``. In one direction it is
    airtight: a route whose path no test file names anywhere in its CODE is certainly not
    driven by one.
    """
    root = (repo_root or REPO_ROOT).resolve()
    sources = [_source(path, path.stat().st_mtime_ns) for path in test_sources(root)]
    by_service: dict[str, set[str]] = {}
    for route in routes:
        if "{" not in route.path:
            by_service.setdefault(route.service, set()).add(route.path)
    driven: dict[tuple[str, str, str], list[str]] = {}
    undriven: list[Route] = []
    for route in routes:
        siblings = frozenset(by_service.get(route.service, set()) - {route.path})
        files = _drivers_for(route, sources, siblings)
        if files:
            driven[route.key] = [str(path.relative_to(root)) for path in files]
        else:
            undriven.append(route)
    return driven, undriven


# =====================================================================================
# Cross-check: what we serve against what we publish
# =====================================================================================
def contract_divergence(
    routes: Sequence[Route], services: Sequence[Service] = SERVICES, repo_root: Path | None = None
) -> dict[str, str]:
    """``{service: message}`` for every service whose census and contract disagree.

    Reuses ``proxyshop_support.contract_sweep`` rather than reimplementing the path
    normaliser, for the reason that module exists: three copies of that rule drifted once
    already, and nothing compared them.
    """
    from proxyshop_support.contract_sweep import (  # noqa: PLC0415 - see the module docstring
        normalise_route,
        operation_divergence,
        published_operations,
    )

    root = (repo_root or REPO_ROOT).resolve()
    out: dict[str, str] = {}
    for service in services:
        if service.contract is None:
            continue
        served = {
            (route.method, normalise_route(route.path))
            for route in routes
            if route.service == service.name and route.kind == "route"
        }
        published = published_operations(root / service.contract)
        if served != published:
            out[service.name] = operation_divergence(served, published)
    return out


# =====================================================================================
# CLI
# =====================================================================================
def _render(routes: Sequence[Route], driven: dict[tuple[str, str, str], list[str]]) -> str:
    lines: list[str] = []
    width = max((len(route.path) for route in routes), default=0)
    for service in sorted({route.service for route in routes}):
        block = [route for route in routes if route.service == service]
        covered = sum(1 for route in block if route.key in driven)
        lines.append("")
        lines.append(f"{service}  —  {len(block)} route(s), {covered} driven")
        lines.append("-" * 78)
        for route in block:
            mark = "OK " if route.key in driven else "!! "
            lines.append(
                f"  {mark}{route.method:9} {route.path:<{width}}  {route.file}:{route.line}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the census, the driven/undriven split, and any contract divergence."""
    parser = argparse.ArgumentParser(
        prog="python -m proxyshop_support.route_census",
        description="Enumerate every served route and say which ones a test actually drives.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--undriven", action="store_true", help="just the undriven work queue")
    parser.add_argument(
        "--include-dev-doubles",
        action="store_true",
        help="include services/shopify-stub, which is a dev double rather than a product surface",
    )
    args = parser.parse_args(argv)

    services = tuple(
        service for service in SERVICES if args.include_dev_doubles or not service.dev_double
    )
    routes = census(services)
    driven, undriven = driven_split(routes)
    divergence = contract_divergence(routes, services)

    if args.json:
        payload: dict[str, Any] = {
            "routes": [
                {
                    **route.__dict__,
                    "driven_by": driven.get(route.key, []),
                }
                for route in routes
            ],
            "counts": {
                service.name: sum(1 for route in routes if route.service == service.name)
                for service in services
            },
            "undriven": [list(route.key) for route in undriven],
            "contract_divergence": divergence,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.undriven:
        for route in undriven:
            print(route.located())
        return 0

    print(f"{len(routes)} served route(s) across {len(services)} service(s)")
    print(_render(routes, driven))
    print()
    print(f"driven by at least one test file: {len(routes) - len(undriven)}/{len(routes)}")
    if undriven:
        print("undriven:")
        for route in undriven:
            print(f"  {route.located()}")
    print()
    if divergence:
        print("census disagrees with the published contract:")
        for service, message in sorted(divergence.items()):
            print(f"  {service}: {message}")
    else:
        print("every service with a published contract agrees with it")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess by the gate
    sys.exit(main())
