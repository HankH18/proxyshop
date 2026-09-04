"""Reproduction gates for T-308 — the system has no observability at all.

Every test here asserts the behaviour that SHOULD hold and therefore fails against the tree
as it stands. Each carries ``@pytest.mark.xfail(strict=True)`` while the defect is live, so:

* an ordinary run reports ``xfailed`` and the repo-wide build gate stays GREEN;
* the ticket's own gate runs
  ``export PROXYSHOP_WORKER=4 && ./.venv/bin/pytest
  proxyshop_support/tests/test_repro_observability.py -q --runxfail -k test_t308``
  and gets a real, selected ``2 failed``;
* the marker cannot outlive the bug — once the defect is closed the test XPASSes, which
  ``strict=True`` turns into a failure, forcing whoever fixed it to delete the marker.

**Nothing here is allowed to skip.** A skip is not a gate, and the compose datastore stack is
routinely down in this repo, so every assertion below is made against source text, the repo's
own on-disk layout, or a subprocess — never against a live datastore. Measured: the seven ASGI
factories this file drives (six ``main.create_app`` plus ``shopify_stub.app.create_app``) all
build, and their lifespans all run, with every datastore blackholed.

**Nothing here mutates this process's logging state.** That constraint is not stylistic.
``logging`` is process-global, and the two obvious ways to test it are both wrong here:

* ``caplog`` installs its own handler *and* forces a level. It would make the blackout
  property pass unconditionally — it configures the very thing under test.
* Calling ``basicConfig``/``setLevel`` in-process would configure logging for every test that
  runs after this one in the same worker, which is the neighbour-corrupting behaviour T-247
  exists to punish.

So the behavioural measurement happens in a **fresh interpreter** (the idiom
``apps/merchant/svc/tests/test_repro_open_tickets.py`` already uses for T-247), and this
process only ever parses source. This module never imports ``logging`` at all.

**What "observable" is taken to mean here, and why each half is necessary.** An adversarial
review broke three earlier drafts of this file, and every rule below is the scar of one of
them:

* *Not* "a config call exists". A grep for ``basicConfig`` goes green on one untested line,
  and ``basicConfig()`` with no ``level=`` leaves the root logger on WARNING.
* *Not* "a handler exists". ``root.setLevel(INFO)`` plus ``logging.NullHandler()`` puts a
  handler at level 0 in the ancestry and shows an operator precisely nothing. So the record
  must be **delivered** — the emitted sentinel must turn up in the child's real stdout/stderr
  or in a file a handler writes to.
* *Not* "some logger in the module works". An earlier draft took the first ``Logger`` in the
  module's namespace, and a decoy ``getLogger("unrelated.diagnostics")`` three lines above
  ``_log`` made it pass while the real call site stayed dark. The logger is now resolved from
  the **receiver name parsed at the call site itself**.
* *Not* "the package contains a logging call". Five files that nothing imports made an earlier
  draft green. A call site now only counts when the running service actually **loaded** the
  module holding it, and only when its receiver is really bound to ``getLogger(...)`` in that
  file — ``log = object(); log.info("x")`` is not logging.

**Neither gate hardcodes a list of services.** Both derive one from ``.pkgroot/`` — the tracked
symlinks that make the flat ``src/`` layout importable — so a service added tomorrow is picked
up and cannot silently skip the gate. Three sweeps in this repo have been caught going *quiet*
rather than red (T-229 6->0 of 8, T-281 70->0 of 79, T-241 48->0 of 66): a loop that iterates
nothing and passes. Every count this file derives is therefore floor-checked BEFORE it is
compared, and reported in the failure message.

**Known limitation, recorded so nobody "fixes" it by weakening the gate.** All five Dockerfiles
start their service through ``uvicorn``, and ``uvicorn`` runs its own ``dictConfig``. A repair
delivered *only* as a ``--log-config`` flag in the Dockerfiles would leave these gates red,
deliberately: it would give an operator nothing under ``pytest``, under ``make demo-seed``, or
under any in-process run, and this repo's own ``proxyshop_support/asgi_server.py`` boots apps
without uvicorn's CLI at all. The fix these gates ask for is in-process configuration on the
startup path, which a uvicorn deployment then inherits.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import warnings
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKGROOT = REPO_ROOT / ".pkgroot"

#: ``logging.Logger`` methods that emit a record. ``warn``/``fatal`` are the deprecated
#: aliases; ``log`` is the explicit-level form.
LOG_LEVEL_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}
)

#: Names whose *instantiation* means a package serves HTTP. A bare type annotation is
#: deliberately not enough: ``def served_routes(app: FastAPI)`` in a schema library does not
#: make that library a service, and an earlier draft classified ``packages/contracts`` as one
#: on exactly that basis.
HTTP_SURFACE_NAMES = frozenset({"APIRouter", "FastAPI"})

# --------------------------------------------------------------------------------------
# Sweep floors. These are NOT a service list — they are the armour that stops a derivation
# which has silently stopped finding anything from reporting success. Each sits below today's
# measured value so that legitimately retiring one service does not produce a red for the
# wrong reason, while a sweep collapsing toward zero is caught immediately.
# --------------------------------------------------------------------------------------
#: Measured today: 7 (buyer_svc, merchant_svc, exchange, trust, ingest, shopify_stub,
#: store_agent).
MIN_SERVICE_PACKAGES = 6
#: Measured today: 215 product .py files across those seven packages.
MIN_FILES_INSPECTED = 150
#: Measured today: 5 INFO call sites, in 5 distinct modules.
MIN_INFO_CALL_SITES = 3
#: Measured today: all 7 factories build and all 7 lifespans run.
MIN_FACTORIES_STARTED = 4


def _parse(path: pathlib.Path) -> ast.AST | None:
    """Parse one product file, or ``None`` if it will not parse.

    ``SyntaxWarning`` is muted here and only here: ``services/ingest/src/er/identity.py`` has
    ``\\s`` in a non-raw docstring, so parsing the product tree emits a warning that has
    nothing to do with this ticket and would otherwise be charged to it.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            return ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - unparseable product file
            return None


def _product_files(base: pathlib.Path) -> list[pathlib.Path]:
    """Every shipped ``.py`` file under ``base`` — no tests, no conftest, no venv."""
    found: list[pathlib.Path] = []
    for path in base.rglob("*.py"):
        parts = set(path.parts)
        if parts & {"tests", ".venv", "node_modules", "__pycache__"}:
            continue
        if path.name == "conftest.py":
            continue
        found.append(path)
    return sorted(found)


def _receiver_name(node: ast.expr) -> str | None:
    """The name an attribute access hangs off: ``_log`` for both ``_log`` and ``self._log``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _logger_bindings(tree: ast.AST) -> set[str]:
    """Names this file really binds to a ``getLogger(...)`` result.

    Parsed rather than guessed, so a service whose logger is called ``audit`` is counted
    without this file knowing the name in advance — and so that ``log = object()`` followed by
    ``log.info("x")``, which an earlier draft counted as logging, is not.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "getLogger":
            continue
        for target in node.targets:
            resolved = _receiver_name(target)
            if resolved:
                bound.add(resolved)
    return bound


def _logging_call_sites(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Every real ``<logger>.<level>(...)`` call in one file, as ``(line, level, receiver)``.

    Parsed, never string-matched. ``apps/merchant/svc/src/install/tokens.py:46`` carries the
    text ``logger.info("%r", result)`` inside a docstring — a ``git grep`` counts it as a
    thirteenth logging file, and it is not one.
    """
    receivers = _logger_bindings(tree) | {"logging"}
    sites: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LOG_LEVEL_METHODS:
            continue
        receiver = _receiver_name(node.func.value)
        if receiver in receivers and receiver is not None:
            sites.append((node.lineno, node.func.attr, receiver))
    return sites


def _instantiates_http_surface(tree: ast.AST) -> bool:
    """True when this module CALLS ``APIRouter(...)`` or ``FastAPI(...)``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None)
            )
            if name in HTTP_SURFACE_NAMES:
                return True
    return False


def _pkgroot_members() -> list[pathlib.Path]:
    """The ``.pkgroot`` symlinks, with an explicit failure if the layout is gone.

    Without this, a missing ``.pkgroot`` raises ``FileNotFoundError`` from ``iterdir()`` and
    the gate crashes instead of reporting itself broken.
    """
    assert PKGROOT.is_dir(), (
        f"{PKGROOT} does not exist, so no service package can be derived at all — the sweep "
        "is broken, not the product"
    )
    return sorted(PKGROOT.iterdir())


def _service_packages() -> dict[str, pathlib.Path]:
    """The service packages, DERIVED from ``.pkgroot`` — never a literal list.

    ``.pkgroot/<pkg>`` is a tracked symlink to a workspace member's flat ``src/`` directory
    (see ``pyproject.toml``'s ``dev-mode-dirs``), so the set of members is on disk and a
    thirteenth one appears here the moment it is added. A member counts as a *service* when
    some module of it really instantiates ``APIRouter`` or ``FastAPI``.

    That test is used rather than "has ``main.create_app``" because ``services/shopify-stub``
    mounts its app in ``app.py`` with no factory, and a ``create_app`` rule would drop a real
    service out of the sweep. The pure libraries it correctly excludes are contracts, llm,
    claim_verification, sim and seller_reference, none of which serve a request.
    """
    services: dict[str, pathlib.Path] = {}
    for link in _pkgroot_members():
        target = link.resolve()
        if not target.is_dir():
            continue
        for path in _product_files(target):
            tree = _parse(path)
            if tree is not None and _instantiates_http_surface(tree):
                services[link.name] = target
                break
    return services


def _asgi_factories() -> list[str]:
    """Dotted module paths of every top-level module that defines ``create_app``.

    The subprocess builds each one's app AND runs its lifespan before measuring, so that a fix
    which configures logging inside ``create_app`` or in a lifespan startup hook — the two
    natural places for it — is seen. Derived rather than listed, so a new service's startup
    path is exercised without editing this file.

    Every top-level module of a member is scanned rather than just ``main.py``: measured, six
    members put the factory in ``main.py`` but ``services/shopify-stub`` puts it in ``app.py``,
    and a ``main.py`` rule would silently never start it.
    """
    factories: list[str] = []
    for link in _pkgroot_members():
        target = link.resolve()
        if not target.is_dir():
            continue
        for path in sorted(target.glob("*.py")):
            tree = _parse(path)
            if tree is None:
                continue
            if any(
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == "create_app"
                for node in ast.walk(tree)
            ):
                factories.append(f"{link.name}.{path.stem}")
    return factories


def _module_name(path: pathlib.Path, services: dict[str, pathlib.Path]) -> str | None:
    """The dotted import path a product file is reachable under, or ``None``.

    Longest prefix wins, so an umbrella ``.pkgroot/buyer -> ../apps/buyer`` link added later
    cannot shadow ``buyer_svc``. ``__init__`` is stripped: ``exchange.auction.__init__`` is a
    *second, duplicate* module object as far as ``importlib`` is concerned, whose
    ``getLogger(__name__)`` returns a child of the logger the product actually uses.
    """
    best: tuple[str, str] | None = None
    for name, base in services.items():
        prefix = f"{base.relative_to(REPO_ROOT).as_posix()}/"
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(prefix) and (best is None or len(prefix) > len(best[1])):
            best = (name, prefix)
    if best is None:
        return None
    package, prefix = best
    tail = path.relative_to(REPO_ROOT).as_posix()[len(prefix) :].removesuffix(".py")
    parts = [part for part in tail.split("/") if part]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join([package, *parts])


def _survey(
    services: dict[str, pathlib.Path],
) -> tuple[dict[str, list[tuple[str, int, str, str]]], int]:
    """Every logging call site per package, plus the number of files really parsed.

    Returns ``{package: [(module, line, level, receiver), ...]}``.
    """
    per_package: dict[str, list[tuple[str, int, str, str]]] = {}
    files_parsed = 0
    for package, base in sorted(services.items()):
        sites: list[tuple[str, int, str, str]] = []
        for path in _product_files(base):
            tree = _parse(path)
            if tree is None:
                continue
            files_parsed += 1
            module = _module_name(path, services)
            if module is None:  # pragma: no cover - defensive
                continue
            sites += [
                (module, line, level, recv) for line, level, recv in _logging_call_sites(tree)
            ]
        per_package[package] = sites
    return per_package, files_parsed


# --------------------------------------------------------------------------------------
# The blackout probe. Runs in a FRESH interpreter for three reasons, all load bearing:
#
#  * pytest installs its own root handlers and may set a level, so a reading taken inside the
#    test process measures pytest's configuration, not the product's;
#  * "a freshly-started process" is the actual claim, and only a new process has one;
#  * a subprocess cannot leave this process's logging state mutated for its neighbours.
#
# It builds every service app AND drives the ASGI lifespan startup, then attaches a capture
# handler to the logger NAMED AT THE CALL SITE and emits a unique sentinel through it. Nothing
# here sets a level: the level is the thing under test.
# --------------------------------------------------------------------------------------
_BLACKOUT_PROBE = """
import asyncio, importlib, json, logging, pathlib, sys

targets = json.loads(sys.argv[1])
factories = json.loads(sys.argv[2])
packages = json.loads(sys.argv[3])


def snapshot():
    root = logging.getLogger()
    return {
        "handlers": [type(h).__name__ for h in root.handlers],
        "level": root.getEffectiveLevel(),
    }


def run_lifespan(app):
    # Drive the real ASGI lifespan protocol far enough to see startup complete. Shutdown is
    # deliberately NOT sent: a shutdown hook could tear down the very configuration we are
    # about to measure. The task is cancelled instead.
    async def drive():
        queue = asyncio.Queue()
        seen = []

        async def receive():
            return await queue.get()

        async def send(message):
            seen.append(message.get("type"))

        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
        task = asyncio.ensure_future(app(scope, receive, send))
        await queue.put({"type": "lifespan.startup"})
        for _ in range(1000):
            if task.done() or any(t and t.startswith("lifespan.startup") for t in seen):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        return seen

    return asyncio.run(asyncio.wait_for(drive(), timeout=15))


boot = snapshot()

# Give the system every chance to configure logging: importing a service package, building its
# ASGI app and running its lifespan is the whole of this repo's in-process startup path.
started, factory_errors, factory_files, lifespans = [], [], [], []
for name in factories:
    try:
        module = importlib.import_module(name)
        factory = getattr(module, "create_app", None)
        if factory is None:
            factory_errors.append(name + ": no create_app")
            continue
        app = factory()
        started.append(name)
        factory_files.append(str(pathlib.Path(module.__file__ or "").resolve()))
    except Exception as exc:
        factory_errors.append(name + ": " + repr(exc))
        continue
    try:
        lifespans.append({"factory": name, "messages": run_lifespan(app)})
    except Exception as exc:
        lifespans.append({"factory": name, "error": repr(exc)})

after = snapshot()

# Which modules the running services really loaded. A logging call in a file nothing imports
# is not observability, and an earlier draft of this gate was made green by five such files.
loaded = sorted(n for n in list(sys.modules) if n.split(".")[0] in packages)

results = []
for module_name, lineno, receiver, sentinel in targets:
    entry = {"module": module_name, "lineno": lineno, "receiver": receiver}
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        entry["error"] = repr(exc)
        results.append(entry)
        continue
    entry["file"] = str(pathlib.Path(module.__file__ or "").resolve())

    # Resolve the logger NAMED AT THE CALL SITE, not merely some logger in the module. A decoy
    # `getLogger("unrelated.diagnostics")` defined above `_log` fooled an earlier draft.
    logger = getattr(module, receiver, None)
    if not isinstance(logger, logging.Logger):
        by_name = logging.Logger.manager.loggerDict.get(module_name)
        logger = by_name if isinstance(by_name, logging.Logger) else None
    if logger is None:
        entry["error"] = (
            "the logger used at this call site is not reachable: module attribute "
            + repr(receiver)
            + " is not a logging.Logger and no logger named "
            + repr(module_name)
            + " is registered"
        )
        results.append(entry)
        continue
    entry["logger_name"] = logger.name
    entry["effective_level"] = logger.getEffectiveLevel()

    # What is really installed, walking the ancestry the way callHandlers does. Taken BEFORE
    # the probe handler exists so it can never count itself.
    inventory, handler_objects, current = [], [], logger
    while current is not None:
        for handler in current.handlers:
            handler_objects.append(handler)
            inventory.append({
                "logger": current.name,
                "handler": type(handler).__name__,
                "level": handler.level,
                "destination": (
                    getattr(handler, "baseFilename", None)
                    or getattr(getattr(handler, "stream", None), "name", None)
                    or ("socket" if getattr(handler, "sock", None) is not None else None)
                ),
            })
        current = current.parent if current.propagate else None
    entry["real_handlers"] = inventory

    captured = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    probe_handler = _Capture()
    logger.addHandler(probe_handler)
    try:
        logger.info("%s", sentinel)
    except Exception as exc:
        entry["emit_error"] = repr(exc)
    finally:
        logger.removeHandler(probe_handler)
    entry["emitted"] = len(captured)

    # Did the record actually go ANYWHERE an operator could read? The sentinel is never echoed
    # into this JSON: the parent looks for it in the child's real stdout/stderr, and here we
    # look for it in any file a handler writes to. A handler that exists but delivers nothing
    # — logging.NullHandler being the obvious one — fails both, which is the whole point.
    for handler in handler_objects:
        try:
            handler.flush()
        except Exception:
            pass
    wrote_to_file = False
    for handler in handler_objects:
        base = getattr(handler, "baseFilename", None)
        if not base:
            continue
        try:
            if sentinel in pathlib.Path(base).read_text(encoding="utf-8", errors="replace"):
                wrote_to_file = True
        except Exception:
            pass
    entry["wrote_to_file"] = wrote_to_file
    results.append(entry)

print("PROBE" + json.dumps({
    "boot": boot,
    "after": after,
    "started": started,
    "factory_errors": factory_errors,
    "factory_files": factory_files,
    "lifespans": lifespans,
    "loaded": loaded,
    "results": results,
}))
"""


def _run_probe(
    targets: list[list[object]], factories: list[str], packages: list[str]
) -> tuple[dict[str, Any], str]:
    """Run the blackout probe in a fresh interpreter and return ``(payload, child_output)``."""
    # The subprocess must import the tree under test, and only PYTHONPATH makes that true. A
    # bare ``python -c`` gets the repo's import roots from this venv's
    # ``site-packages/_proxyshop.pth`` — which names whichever checkout provisioned the venv,
    # not necessarily this one — because ``-c`` puts only the cwd on ``sys.path``, never
    # ``cwd/.pkgroot``. PYTHONPATH is placed ahead of every ``.pth`` addition, so this
    # reproduces pytest's own ``pythonpath = [".", ".pkgroot"]`` for the child. Measured:
    # without it, a copy of this repo run against a shared venv imports the OTHER checkout's
    # modules, and the .pth guard below is what caught it.
    roots = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / ".pkgroot")])
    inherited = os.environ.get("PYTHONPATH")
    env = dict(
        os.environ,
        PROXYSHOP_WORKER=os.environ.get("PROXYSHOP_WORKER", "0"),
        PYTHONPATH=f"{roots}{os.pathsep}{inherited}" if inherited else roots,
    )
    argv = [
        sys.executable,
        "-c",
        textwrap.dedent(_BLACKOUT_PROBE),
        json.dumps(targets),
        json.dumps(factories),
        json.dumps(packages),
    ]
    try:
        completed = subprocess.run(
            argv, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - only on a hung startup path
        # TimeoutExpired carries output as bytes-or-None, so decode defensively rather than
        # losing the entire diagnostic — an earlier draft reported nothing but its own source.
        def _text(stream: object) -> str:
            if isinstance(stream, bytes):
                return stream.decode("utf-8", "replace")
            return stream if isinstance(stream, str) else "<none>"

        raise AssertionError(
            "the blackout probe did not finish within 180s — a service startup path hung "
            f"rather than a reading being taken.\nstdout:\n{_text(exc.stdout)[-4000:]}\n"
            f"stderr:\n{_text(exc.stderr)[-4000:]}"
        ) from exc

    child_output = completed.stdout + completed.stderr
    marker = [line for line in completed.stdout.splitlines() if line.startswith("PROBE")]
    assert marker, (
        "the blackout probe produced no verdict line.\n"
        f"stdout:\n{completed.stdout[-4000:]}\nstderr:\n{completed.stderr[-4000:]}"
    )
    return json.loads(marker[-1][len("PROBE") :]), child_output


# ======================================================================================
# T-308 (a) — an INFO record emitted by a service logger is silently discarded
# ======================================================================================
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-308: nothing in apps/, packages/, services/, proxyshop_support/ or scripts/ ever "
        "configures application logging — no basicConfig, no dictConfig, no root handler, no "
        "structlog (which is a declared dependency with zero importers) — so a freshly-started "
        "service process has root handlers=[] at effective level 30. Every INFO call site in "
        "the product is dropped before a record is even constructed: the lossy-pixel notice "
        "(collector/routes.py:64), the un-minted code (codes/routes.py:133), the envelope that "
        "stays in shadow (onboarding/routes.py:154), the webhook handed to the default sink "
        "(install/webhooks.py:404) and the recorded feedback (feedback/submission.py:493); "
        "remove this marker with the fix"
    ),
)
def test_t308_an_info_record_from_a_service_logger_reaches_an_installed_handler() -> None:
    """An INFO call in a started service has to emit, and the record has to go somewhere.

    Two independent things must both hold, and neither implies the other:

    1. **the record is constructed** — captured at a handler attached to the product's own
       ``Logger`` object, so the level configuration is really exercised; and
    2. **the record is delivered** — the sentinel turns up in the child's real stdout/stderr,
       or in a file one of its handlers writes to.

    The call sites, the logger names and the startup path are all discovered by parsing the
    tree rather than being listed here, so the gate follows the product.
    """
    services = _service_packages()
    per_package, files_inspected = _survey(services)
    info_sites = [
        (module, line, receiver)
        for sites in per_package.values()
        for module, line, level, receiver in sites
        if level == "info"
    ]

    # ---- ARM THE SWEEP. Three sweeps in this repo went quiet rather than red; a probe that
    # discovered nothing must never be able to report success.
    assert len(services) >= MIN_SERVICE_PACKAGES, (
        f"the .pkgroot service derivation found only {len(services)} packages "
        f"({sorted(services)}), below the floor of {MIN_SERVICE_PACKAGES} — the sweep is "
        "broken, not the product"
    )
    assert files_inspected >= MIN_FILES_INSPECTED, (
        f"only {files_inspected} product files were parsed across {sorted(services)}, below "
        f"the floor of {MIN_FILES_INSPECTED} — the sweep is broken, not the product"
    )
    assert len(info_sites) >= MIN_INFO_CALL_SITES, (
        f"only {len(info_sites)} INFO call sites were found ({info_sites}), below the floor of "
        f"{MIN_INFO_CALL_SITES} — with nothing to emit, this gate would pass vacuously"
    )

    # One probe target per (module, receiver) that owns an INFO call site, earliest line first.
    # Each carries a unique sentinel which the child emits but NEVER echoes back in its JSON,
    # so finding it in the child's output is evidence the record was written out rather than
    # evidence the child said so.
    chosen: dict[tuple[str, str], int] = {}
    for module, line, receiver in sorted(info_sites):
        chosen.setdefault((module, receiver), line)
    sentinels = {key: f"t308-sentinel-{index}" for index, key in enumerate(sorted(chosen))}
    targets: list[list[object]] = [
        [module, chosen[(module, receiver)], receiver, sentinels[(module, receiver)]]
        for module, receiver in sorted(chosen)
    ]

    factories = _asgi_factories()
    payload, child_output = _run_probe(targets, factories, sorted(services))

    # The .pth guard, applied to the STARTUP path first. This venv's site-packages puts a
    # checkout on sys.path for every process that uses it, and that has already produced one
    # false green in this repo. It is checked before the started-count control below, because a
    # factory imported out of a foreign checkout would otherwise be counted as "started" and
    # satisfy the very control meant to prove THIS tree got its chance.
    for factory_file in payload["factory_files"]:
        assert pathlib.Path(str(factory_file)).is_relative_to(REPO_ROOT), (
            f"the probe started a service from {factory_file}, which is outside the tree under "
            f"test ({REPO_ROOT}) — a .pth leak, not a measurement"
        )

    started = list(payload["started"])
    assert len(started) >= MIN_FACTORIES_STARTED, (
        f"only {len(started)} of {len(factories)} ASGI factories built ({started}; errors: "
        f"{payload['factory_errors']}), so the system was never given its chance to configure "
        "logging and this reading means nothing"
    )

    silent: list[str] = []
    for entry in payload["results"]:
        assert "error" not in entry, (
            f"the probe could not measure {entry['module']}: {entry['error']} — a broken probe, "
            "not a reading"
        )
        resolved = pathlib.Path(entry["file"])
        assert resolved.is_relative_to(REPO_ROOT), (
            f"the probe imported {resolved}, which is outside the tree under test ({REPO_ROOT})"
            " — a .pth leak, not a measurement"
        )
        sentinel = sentinels[(entry["module"], entry["receiver"])]
        delivered = bool(entry["wrote_to_file"]) or sentinel in child_output
        if not entry["emitted"] or not delivered:
            silent.append(
                f"{entry['module']}:{entry['lineno']} (logger {entry['logger_name']!r} via "
                f"{entry['receiver']!r}, effective level {entry['effective_level']}, "
                f"emitted={entry['emitted']}, delivered={delivered}, "
                f"handlers={entry['real_handlers']})"
            )

    assert not silent, (
        f"{len(silent)} of {len(payload['results'])} INFO call sites emit nothing an operator "
        f"can see after {len(started)} services were started and their lifespans run "
        f"({started}). The root logger booted with handlers={payload['boot']['handlers']} at "
        f"level {payload['boot']['level']} and was still handlers={payload['after']['handlers']} "
        f"at level {payload['after']['level']} afterwards. Each site below either never "
        f"constructed the record at all (emitted=0 — the effective level forbids INFO, so only "
        f"WARNING+ reaches stderr via logging.lastResort), or constructed one that reached no "
        f"stream, file or socket (delivered=False — a handler that exists and writes nowhere). "
        f"Silent sites: {silent}"
    )


# ======================================================================================
# T-308 (b) — five of the seven service packages contain no logging call at all
# ======================================================================================
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-308: logging exists in exactly two of the seven HTTP-serving packages. Measured "
        "files containing a real logging call: buyer_svc 7/34, merchant_svc 5/37, and then "
        "exchange 0/42, trust 0/33, ingest 0/37, store_agent 0/20, shopify_stub 0/12 — the "
        "auction, the trust scores, the crawl, the bidding agent and the Shopify surface emit "
        "nothing at any level, so an operator cannot follow a request through the system at "
        "all; remove this marker with the fix"
    ),
)
def test_t308_every_service_package_has_at_least_one_logging_call_site() -> None:
    """A service that never logs cannot be watched, whatever the root logger is set to.

    The service list is derived from ``.pkgroot`` on every run, so a service added tomorrow is
    held to this the day it lands and cannot skip the gate by not being in a literal.

    A call site only counts when the running service actually **loaded** the module holding it.
    That is not pedantry: an earlier draft of this gate was made green by adding five files
    that nothing imports, one per dark package, leaving all 21 real call sites still emitting
    nothing. Loadedness is measured in the same child that starts the services.
    """
    services = _service_packages()
    per_package, files_inspected = _survey(services)

    # ---- ARM THE SWEEP, before any comparison.
    assert len(services) >= MIN_SERVICE_PACKAGES, (
        f"the .pkgroot service derivation found only {len(services)} packages "
        f"({sorted(services)}), below the floor of {MIN_SERVICE_PACKAGES} — the sweep is "
        "broken, not the product"
    )
    assert files_inspected >= MIN_FILES_INSPECTED, (
        f"only {files_inspected} product files were parsed across {sorted(services)}, below the "
        f"floor of {MIN_FILES_INSPECTED} — a sweep that inspects nothing passes everything"
    )

    payload, _ = _run_probe([], _asgi_factories(), sorted(services))
    loaded = set(payload["loaded"])

    started = list(payload["started"])
    assert len(started) >= MIN_FACTORIES_STARTED, (
        f"only {len(started)} of {len(_asgi_factories())} ASGI factories built ({started}; "
        f"errors: {payload['factory_errors']}), so no module was loaded and 'is this call site "
        "reachable' cannot be answered"
    )

    static: dict[str, int] = {}
    live: dict[str, int] = {}
    for package, sites in per_package.items():
        static[package] = len({module for module, _l, _lv, _r in sites})
        live[package] = len({module for module, _l, _lv, _r in sites if module in loaded})

    dark = sorted(package for package, count in live.items() if count == 0)
    assert not dark, (
        f"{len(dark)} of {len(services)} service packages emit no log line of any level from "
        f"any module the running service actually loads, so nothing they do is observable even "
        f"once the root logger is configured. Modules with a logging call, per package "
        f"(loaded/total-with-a-call-site): "
        f"{ {p: f'{live[p]}/{static[p]}' for p in sorted(per_package)} }. "
        f"Dark packages: {dark} ({files_inspected} product files inspected across "
        f"{len(services)} derived packages; {len(loaded)} modules loaded by {len(started)} "
        f"started services)"
    )
