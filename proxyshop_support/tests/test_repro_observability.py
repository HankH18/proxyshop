"""Reproduction gates for T-308 — the system has no observability at all.

Every test here asserts the behaviour that SHOULD hold and therefore fails against the tree
as it stands. Each carries ``@pytest.mark.xfail(strict=True)`` while the defect is live, so:

* an ordinary run reports ``xfailed`` and the repo-wide build gate stays GREEN;
* the ticket's own gate runs
  ``export PROXYSHOP_WORKER=0 && ./.venv/bin/pytest
  proxyshop_support/tests/test_repro_observability.py -q --runxfail -k test_t308``
  and gets a real, selected ``2 failed``;
* the marker cannot outlive the bug — once the defect is closed the test XPASSes, which
  ``strict=True`` turns into a failure, forcing whoever fixed it to delete the marker.

**Nothing here is allowed to skip.** A skip is not a gate, and the compose datastore stack is
routinely down in this repo, so every assertion below is made against source text, the
repo's own on-disk layout, or a subprocess — never against a live datastore. Measured: the
six ASGI factories this file drives all build without any datastore reachable.

**Nothing here mutates this process's logging state.** That constraint is not stylistic.
``logging`` is process-global, and the two obvious ways to test it are both wrong here:

* ``caplog`` installs its own handler *and* forces a level. It would make the blackout
  property pass unconditionally — it configures the very thing under test.
* Calling ``basicConfig``/``setLevel`` in-process would configure logging for every test
  that runs after this one in the same worker, which is the neighbour-corrupting behaviour
  T-247 exists to punish.

So the behavioural measurement happens in a **fresh interpreter** (the idiom
``apps/merchant/svc/tests/test_repro_open_tickets.py`` already uses for T-247), and this
process only ever parses source. Nothing here adds, removes or re-levels a handler, and
nothing here imports a product module at collection time.

**Neither gate hardcodes a list of services.** Both derive one from ``.pkgroot/`` — the
tracked symlinks that make the flat ``src/`` layout importable — so a service added
tomorrow is picked up and cannot silently skip the gate. Three sweeps in this repo have
been caught going *quiet* rather than red (T-229 6->0 of 8, T-281 70->0 of 79, T-241
48->0 of 66): a loop that iterates nothing and passes. Every count this file derives is
therefore floor-checked BEFORE it is compared, and reported in the failure message.
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

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKGROOT = REPO_ROOT / ".pkgroot"

#: ``logging.Logger`` methods that emit a record. ``warn`` and ``fatal`` are the deprecated
#: aliases; ``log`` is the explicit-level form.
LOG_LEVEL_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}
)

#: Receiver names that mean "this is a logger" even when the binding is not visible in the
#: file being parsed — ``self._log``, a logger passed in as an argument, a class attribute.
#: Names bound to a real ``getLogger(...)`` call are discovered per-file in addition to
#: these, so a service that spells its logger something else is still counted.
LOGGER_NAME_HINTS = frozenset({"log", "_log", "logger", "_logger", "LOG", "LOGGER", "logging"})

#: Names whose presence means a package serves HTTP. Matched as real AST references, never
#: as source text: a package does not become a service by mentioning FastAPI in a docstring.
HTTP_SURFACE_NAMES = frozenset({"APIRouter", "FastAPI"})

# --------------------------------------------------------------------------------------
# Sweep floors. These are NOT a service list — they are the armour that stops a derivation
# which has silently stopped finding anything from reporting success. Each sits below
# today's measured value so that legitimately retiring one service does not produce a red
# for the wrong reason, while a sweep that collapses toward zero is caught immediately.
# --------------------------------------------------------------------------------------
#: Measured today: 7 (buyer_svc, merchant_svc, exchange, trust, ingest, shopify_stub,
#: store_agent).
MIN_SERVICE_PACKAGES = 6
#: Measured today: 215 product .py files across those seven packages.
MIN_FILES_INSPECTED = 150
#: Measured today: 5 INFO call sites, in 5 distinct modules.
MIN_INFO_CALL_SITES = 3
#: Measured today: all 6 of the ``main.create_app`` factories build.
MIN_FACTORIES_STARTED = 4


def _parse(path: pathlib.Path) -> ast.AST | None:
    """Parse one product file, or ``None`` if it will not parse.

    ``SyntaxWarning`` is muted here and only here: ``services/ingest/src/er/identity.py``
    has ``\\s`` in a non-raw docstring, so parsing the product tree emits a warning that has
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


def _referenced_names(tree: ast.AST) -> set[str]:
    """Every identifier a module really uses — never a docstring, never a comment."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
    return found


def _receiver_name(node: ast.expr) -> str | None:
    """The name the attribute access hangs off: ``_log`` for ``self._log``, for ``_log``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _logger_bindings(tree: ast.AST) -> set[str]:
    """Names this file binds to a ``getLogger(...)`` result.

    Parsed rather than guessed, so a service whose logger is called ``audit`` is counted
    without this file knowing the name in advance.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "getLogger":
            continue
        for target in node.targets:
            resolved = _receiver_name(target)
            if resolved:
                bound.add(resolved)
    return bound


def _logging_call_sites(path: pathlib.Path, tree: ast.AST) -> list[tuple[str, int, str]]:
    """Every real ``<logger>.<level>(...)`` call in one file, as ``(path, line, level)``.

    Parsed, never string-matched. ``apps/merchant/svc/src/install/tokens.py:46`` carries
    the text ``logger.info("%r", result)`` inside a docstring — a ``git grep`` counts it as
    a thirteenth logging file, and it is not one.
    """
    receivers = LOGGER_NAME_HINTS | _logger_bindings(tree)
    sites: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LOG_LEVEL_METHODS:
            continue
        if _receiver_name(node.func.value) in receivers:
            sites.append((str(path.relative_to(REPO_ROOT)), node.lineno, node.func.attr))
    return sites


def _service_packages() -> dict[str, pathlib.Path]:
    """The service packages, DERIVED from ``.pkgroot`` — never a literal list.

    ``.pkgroot/<pkg>`` is a tracked symlink to a workspace member's flat ``src/`` directory
    (see ``pyproject.toml``'s ``dev-mode-dirs``), so the set of members is on disk and a
    thirteenth one appears here the moment it is added. A member counts as a *service* when
    some module of it really references ``APIRouter`` or ``FastAPI`` — i.e. it serves HTTP
    and therefore has a request an operator could need to follow.

    That test is used rather than "has ``main.create_app``" because
    ``services/shopify-stub`` mounts its app in ``app.py`` with no factory, and a
    ``create_app`` rule would drop a real service out of the sweep. The pure libraries it
    correctly excludes are contracts, llm, claim_verification, sim and seller_reference,
    none of which serve a request.
    """
    services: dict[str, pathlib.Path] = {}
    for link in sorted(PKGROOT.iterdir()):
        target = link.resolve()
        if not target.is_dir():
            continue
        for path in _product_files(target):
            tree = _parse(path)
            if tree is not None and _referenced_names(tree) & HTTP_SURFACE_NAMES:
                services[link.name] = target
                break
    return services


def _asgi_factories() -> list[str]:
    """Dotted module paths of every top-level module that defines ``create_app``.

    The subprocess builds each one's app before measuring, so that a fix which configures
    logging inside ``create_app`` — the natural place for it — is seen. Derived rather than
    listed, so a new service's startup path is exercised without editing this file.

    Every top-level module of a member is scanned rather than just ``main.py``: measured,
    six members put the factory in ``main.py`` but ``services/shopify-stub`` puts it in
    ``app.py`` (``shopify_stub.app.create_app``), and a ``main.py`` rule would silently
    never start it.
    """
    factories: list[str] = []
    for link in sorted(PKGROOT.iterdir()):
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


# --------------------------------------------------------------------------------------
# The blackout probe. Runs in a FRESH interpreter for three reasons, all of them load
# bearing:
#
#  * pytest installs its own root handlers and may set a level, so a reading taken inside
#    the test process measures pytest's configuration, not the product's;
#  * "a freshly-configured process" is the actual claim, and only a new process has one;
#  * a subprocess cannot leave this process's logging state mutated for its neighbours.
#
# It attaches a capture handler DIRECTLY TO THE PRODUCT'S OWN LOGGER OBJECT — the very
# ``Logger`` instance the call site at ``<module>:<line>`` calls — and emits through it.
# Nothing here sets a level: the level is the thing under test. It also inventories the
# handlers that are really installed on that logger's ancestry, because a fix that raises
# the level but installs no sink still shows an operator nothing (records would fall
# through to ``logging.lastResort``, which is itself WARNING).
# --------------------------------------------------------------------------------------
_BLACKOUT_PROBE = """
import importlib, json, logging, pathlib, sys

targets = json.loads(sys.argv[1])
factories = json.loads(sys.argv[2])


def snapshot():
    root = logging.getLogger()
    return {
        "handlers": [type(h).__name__ for h in root.handlers],
        "level": root.getEffectiveLevel(),
    }


boot = snapshot()

# Give the system every chance to configure logging: importing a service package and
# building its ASGI app is the whole of this repo's startup path.
started, factory_errors = [], []
for name in factories:
    try:
        module = importlib.import_module(name)
        factory = getattr(module, "create_app", None)
        if factory is None:
            factory_errors.append(name + ": no create_app")
            continue
        factory()
        started.append(name)
    except Exception as exc:
        factory_errors.append(name + ": " + repr(exc))

after = snapshot()

results = []
for module_name, lineno in targets:
    entry = {"module": module_name, "lineno": lineno}
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        entry["error"] = repr(exc)
        results.append(entry)
        continue
    entry["file"] = str(pathlib.Path(module.__file__ or "").resolve())
    loggers = [v for v in vars(module).values() if isinstance(v, logging.Logger)]
    if not loggers:
        entry["error"] = "module holds no logging.Logger attribute"
        results.append(entry)
        continue
    logger = loggers[0]
    entry["logger_name"] = logger.name
    entry["effective_level"] = logger.getEffectiveLevel()

    # What is really installed, walking the ancestry the way callHandlers does. Taken
    # BEFORE the probe handler exists so it can never count itself.
    inventory, current = [], logger
    while current is not None:
        for handler in current.handlers:
            inventory.append(
                {"logger": current.name, "handler": type(handler).__name__, "level": handler.level}
            )
        current = current.parent if current.propagate else None
    entry["real_handlers"] = inventory

    captured = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    probe_handler = _Capture()
    logger.addHandler(probe_handler)
    try:
        logger.info("proxyshop-t308-blackout-probe from %s", module_name)
    finally:
        logger.removeHandler(probe_handler)
    entry["emitted"] = len(captured)
    results.append(entry)

print("PROBE" + json.dumps({
    "boot": boot,
    "after": after,
    "started": started,
    "factory_errors": factory_errors,
    "results": results,
}))
"""


# ======================================================================================
# T-308 (a) — an INFO record emitted by a service logger is silently discarded
# ======================================================================================
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-308: nothing in apps/, packages/, services/, proxyshop_support/ or scripts/ ever "
        "configures logging — no basicConfig, no dictConfig, no handler, no structlog — so a "
        "freshly-started service process has root handlers=[] at effective level 30. Every "
        "INFO call site in the product is dropped before a record is even constructed: the "
        "lossy-pixel notice (collector/routes.py:64), the un-minted code "
        "(codes/routes.py:133), the envelope that stays in shadow (onboarding/routes.py:154), "
        "the webhook handed to the default sink (install/webhooks.py:404) and the recorded "
        "feedback (feedback/submission.py:493). Remove this marker with the fix"
    ),
)
def test_t308_an_info_record_from_a_service_logger_reaches_an_installed_handler() -> None:
    """An INFO call in a started service has to actually emit, to a handler that exists.

    Deliberately **not** a search for ``basicConfig``. A gate that greps for a config call
    goes green the moment somebody writes one line anywhere, which is untested configuration
    wearing a tick — and ``logging.basicConfig()`` with no ``level=`` argument leaves the
    root logger on WARNING, so that one line would change nothing an operator can see. This
    asserts the observable effect instead, and both halves of it:

    1. **the record is emitted** — captured at a handler attached to the product's own
       ``Logger`` object, so the level configuration is really exercised; and
    2. **a real handler is installed** — inventoried from the logger's ancestry, so a fix
       that lowers the level but installs no sink (records falling through to
       ``logging.lastResort``, itself WARNING) does not count as observability.

    The call sites are discovered by parsing the tree rather than being listed here, so the
    gate follows the product: a new INFO call in a new service is covered automatically.
    """
    services = _service_packages()
    info_sites: list[tuple[str, int, str]] = []
    files_inspected = 0
    for base in services.values():
        for path in _product_files(base):
            tree = _parse(path)
            if tree is None:
                continue
            files_inspected += 1
            info_sites += [site for site in _logging_call_sites(path, tree) if site[2] == "info"]

    # ---- ARM THE SWEEP. Three sweeps in this repo went quiet rather than red; a probe
    # that discovered nothing must never be able to report success.
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
        f"only {len(info_sites)} INFO call sites were found ({info_sites}), below the floor "
        f"of {MIN_INFO_CALL_SITES} — with nothing to emit, this gate would pass vacuously"
    )

    # One probe target per module that owns an INFO call site, earliest line first.
    targets: dict[str, int] = {}
    for rel, lineno, _level in sorted(info_sites):
        module = rel.removesuffix(".py").replace("/", ".")
        for name, base in services.items():
            prefix = f"{base.relative_to(REPO_ROOT).as_posix().replace('/', '.')}."
            if module.startswith(prefix):
                module = name + "." + module[len(prefix) :]
                break
        targets.setdefault(module, lineno)

    factories = _asgi_factories()

    # The subprocess must import the tree under test, and only PYTHONPATH makes that true.
    # A bare ``python -c`` gets the repo's import roots from this venv's
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
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(_BLACKOUT_PROBE),
            json.dumps(sorted(targets.items())),
            json.dumps(factories),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    marker = [line for line in completed.stdout.splitlines() if line.startswith("PROBE")]
    assert marker, (
        "the blackout probe produced no verdict line.\n"
        f"stdout:\n{completed.stdout[-4000:]}\nstderr:\n{completed.stderr[-4000:]}"
    )
    payload = json.loads(marker[-1][len("PROBE") :])

    # Control: the services really did start. Without this a probe whose imports all blew
    # up would report "nothing emitted" and read as the defect rather than as a broken
    # measurement.
    assert len(payload["started"]) >= MIN_FACTORIES_STARTED, (
        f"only {len(payload['started'])} of {len(factories)} ASGI factories built "
        f"({payload['started']}; errors: {payload['factory_errors']}), so the system was "
        "never given its chance to configure logging and this reading means nothing"
    )

    silent: list[str] = []
    for entry in payload["results"]:
        assert "error" not in entry, (
            f"the probe could not measure {entry['module']}: {entry['error']} — a broken "
            "probe, not a reading"
        )
        # The .pth guard: this venv's site-packages puts another checkout on sys.path for
        # every process that uses it, and that has already produced one false green in this
        # repo. A reading taken from a different tree is not a reading.
        resolved = pathlib.Path(entry["file"])
        assert resolved.is_relative_to(REPO_ROOT), (
            f"the probe imported {resolved}, which is outside the tree under test "
            f"({REPO_ROOT}) — a .pth leak, not a measurement"
        )
        usable = [h for h in entry["real_handlers"] if h["level"] <= 20]
        if not entry["emitted"] or not usable:
            silent.append(
                f"{entry['module']}:{entry['lineno']} (logger {entry['logger_name']!r}, "
                f"effective level {entry['effective_level']}, emitted={entry['emitted']}, "
                f"handlers={entry['real_handlers']})"
            )

    assert not silent, (
        f"{len(silent)} of {len(payload['results'])} INFO call sites emit nothing an "
        f"operator can see after {len(payload['started'])} services were started "
        f"({payload['started']}). The root logger booted with handlers="
        f"{payload['boot']['handlers']} at level {payload['boot']['level']} and was still "
        f"handlers={payload['after']['handlers']} at level {payload['after']['level']} "
        f"afterwards, so `_log.info(...)` is dropped before a record is constructed and "
        f"only WARNING+ reaches stderr via logging.lastResort. Silent sites: {silent}"
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
        "auction, the trust scores, the crawl, the bidding agent and the Shopify surface "
        "emit nothing at any level, so an operator cannot follow a request through the "
        "system at all. Remove this marker with the fix"
    ),
)
def test_t308_every_service_package_has_at_least_one_logging_call_site() -> None:
    """A service that never logs cannot be watched, whatever the root logger is set to.

    The service list is derived from ``.pkgroot`` on every run, so a service added tomorrow
    is held to this the day it lands and cannot skip the gate by not being in a literal.
    The counts the derivation produced are reported in the failure message, and floor-checked
    before they are compared, so a derivation that has stopped finding files reports itself
    broken instead of reporting the product fixed.
    """
    services = _service_packages()

    coverage: dict[str, tuple[int, int]] = {}
    total_files = 0
    for name, base in sorted(services.items()):
        files = _product_files(base)
        with_logging = 0
        parsed = 0
        for path in files:
            tree = _parse(path)
            if tree is None:
                continue
            parsed += 1
            if _logging_call_sites(path, tree):
                with_logging += 1
        coverage[name] = (with_logging, parsed)
        total_files += parsed

    # ---- ARM THE SWEEP, before any comparison.
    assert len(services) >= MIN_SERVICE_PACKAGES, (
        f"the .pkgroot service derivation found only {len(services)} packages "
        f"({sorted(services)}), below the floor of {MIN_SERVICE_PACKAGES} — the sweep is "
        "broken, not the product"
    )
    assert total_files >= MIN_FILES_INSPECTED, (
        f"only {total_files} product files were parsed across {sorted(services)}, below the "
        f"floor of {MIN_FILES_INSPECTED} — a sweep that inspects nothing passes everything"
    )

    dark = {name: counts for name, counts in coverage.items() if counts[0] == 0}
    assert not dark, (
        f"{len(dark)} of {len(services)} service packages contain no logging call of any "
        f"level, so nothing they do is observable even once the root logger is configured. "
        f"Files containing a logging call, per package: "
        f"{ {name: f'{hit}/{total}' for name, (hit, total) in coverage.items()} }. "
        f"Dark packages: {sorted(dark)} "
        f"({total_files} product files inspected across {len(services)} derived packages)"
    )
