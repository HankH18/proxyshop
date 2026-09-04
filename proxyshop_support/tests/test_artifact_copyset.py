"""Artifact gates: grade the DEPLOYABLE IMAGES, not the checkout they were written in.

Every other gate in this repo — the frozen acceptance suite, ``make verify``, all twelve
frozen metrics — imports from a checkout where *every* package is present, because the
editable root distribution puts the repo root and ``.pkgroot/`` on ``sys.path`` for every
process. An image ships a SUBSET of that tree: whatever its Dockerfile's ``COPY`` set
names. A package that the checkout resolves and the image does not is invisible to all of
them, and that whole defect class has been invisible for the life of the project. Of the
five images in this repo one cannot start at all, one starts with a fifth of its shipped
modules unimportable, and one runs a feature permanently degraded — and no metric moved.

**Where this file lives, and why it is not under ``tests/``.** ``pyproject.toml``'s
``testpaths`` is ``["packages","apps","services","pixel","fixtures","e2e","docs",
"proxyshop_support"]``. A top-level ``tests/`` directory — T-301's declared scope — is NOT
collected by ``make verify``, so a gate written there would never run in the build it
exists to protect. ``proxyshop_support`` is collected, and this is orchestrator-shared
runtime support, so the gate goes here.

**Two checks, and neither is redundant.**

*Static* (:func:`unwired_first_party_imports`) reads the Dockerfile as text: every
``import X`` / ``from X import ...`` in an image's shipped tree must have ``X`` reachable in
that image. The first-party package names come from the repo's own ``.pkgroot`` symlinks,
so a package added later is picked up with no edit here. Fatal-vs-silent is decided by
COUNTING import sites at column 0 (module scope, kills the module) against indented ones
(inside a function or a ``try``, may be swallowed) — measured, never declared. It needs no
subprocess, no ``-S``, no symlink fidelity and no assumption about Docker's ``COPY``
semantics, and it is the ONLY half that can see T-300, where the import is both lazy and
wrapped in ``except ImportError``: every shipped buyer module imports cleanly and a runtime
probe reports zero failures, which is not a clean bill of health.

*Runtime* (:func:`image_import_report`) materialises the ``COPY`` set into a temp tree,
recreates the ``.pkgroot`` symlinks from the Dockerfile's own ``ln -s`` pairs, and imports
every shipped module in a subprocess. It covers the case the static half cannot: a module
that IS shipped but broken for another reason — a syntax error, an import side effect, a
missing third-party pin. It also measures the TRANSITIVE blast radius: T-298 is one bad
import line and seven dead modules.

**What NEITHER half sees, stated so nobody reads a green here as coverage it is not.** A
package reached by ``importlib.import_module("<name>")`` — a STRING, not an import
statement — is invisible to the static half, because there is no ``ast.Import`` node naming
it, and invisible to the runtime half whenever the call sits behind a lazy ``__getattr__``
(PEP 562), because the module then imports cleanly and only the first attribute lookup
fails. T-193 is exactly that shape: ``apps/trust/src/verification/__init__.py:46-48``
resolves ``claim_verification`` by string inside ``__getattr__``, an AST scan of
``apps/trust/src`` finds ZERO import statements naming it (measured), and the trust image
therefore passes both checks in this file while ``trust.verification.verify`` raises inside
the shipped artifact. That defect has its own gate — ``apps/trust/tests/
test_repro_open_tickets.py::test_the_trust_image_copy_set_can_resolve_the_claim_verifier``
— which probes the ATTRIBUTE rather than the import, and it is the right shape for it.
Closing that hole generically means resolving string arguments to ``import_module``, which
is a different check from either of these two; it is not attempted here.

**The assumption the runtime half rests on, stated rather than hidden.** The materialiser
uses ``shutil.copytree(..., symlinks=True)``, i.e. it assumes Docker's ``COPY`` copies a
symlink as a symlink rather than dereferencing it. That is not verified against a running
daemon here (D3 forbids a verify that reaches the network, and no image in this repo has
ever been built on this host — see ``services/shopify-stub/compose.yaml``'s own warning).
It is not a guess either: ``apps/exchange/Dockerfile:56-58`` records the observation that
copying ``packages/contracts/src/`` alone "lands a dangling link in the image and
``contracts.protocol`` dies on ``No module named 'contracts.generated'`` at import time —
observed, not theorised", and a ``COPY`` that dereferenced would have materialised that
symlink's contents instead. So: **preserved, on in-repo observed evidence; assumed, not
re-measured here.** The direction of the risk matters and is the safe one — ``symlinks=True``
is the STRICTER reading. ``symlinks=False`` (what ``apps/trust/tests/test_repro_open_tickets.py``
uses) dereferences, which would silently repair a dangling-symlink defect inside the probe
and hand back a false green.

**Anti-false-green machinery**, all of it established by measurement rather than taste:

* ``-S`` on every probe subprocess. ``.venv/lib/python3.12/site-packages/_proxyshop.pth``
  puts the checkout root and its ``.pkgroot`` on ``sys.path`` at interpreter startup
  regardless of ``PYTHONPATH`` and regardless of ``cwd``; ``-S`` disables ``site`` and
  therefore all ``.pth`` handling. ``-E`` and ``-I`` are both unusable — they discard
  ``PYTHONPATH`` too, which is the one thing the probe needs.
* The venv's ``site-packages`` IS passed on ``PYTHONPATH`` (last), because the image
  ``pip install``s the same pinned set and without it every module that imports ``fastapi``
  would fail for a reason the image does not have. It is inert as a leak: with ``-S`` its
  ``.pth`` files never execute.
* The probe reports its own ``sys.path`` and the ``__file__`` of everything it imported.
  Any path inside the checkout that is not ``.venv``, and any first-party module resolved
  outside the temp tree, fails the test as a MEASUREMENT error rather than passing it.
* :func:`test_the_static_copy_set_checker_detects_a_fix_and_a_regression` is the positive
  control: it synthesises a fixed Dockerfile and a broken one in memory — no file in the
  repo is touched — and proves the checker flips in both directions. A checker that cannot
  see a fix is worthless, and the trust lane's equivalent gate was found XPASSing against a
  live defect for exactly that reason.

Every defect test is ``@pytest.mark.xfail(strict=True)``: a normal run reports ``xfailed``
and ``make verify`` stays green, the ticket's gate runs ``--runxfail -k <name>`` and gets a
real red, and ``strict=True`` means whoever fixes the defect must delete the marker.

Nothing in this file touches a Dockerfile, a compose fragment or any product source. This
lane writes gates, not fixes.
"""

from __future__ import annotations

import ast
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import textwrap
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PKGROOT = REPO_ROOT / ".pkgroot"

#: The venv's ``site-packages``. Passed to every probe so the shipped modules can find the
#: third-party pins the image's own ``pip install`` layer provides. It lives INSIDE the
#: checkout, so the leak guard has to allow it explicitly — see the module docstring.
SITE_PACKAGES = Path(sysconfig.get_paths()["purelib"]).resolve()


# =====================================================================================
# Reading the repo: which packages exist, and which images ship
# =====================================================================================


@lru_cache(maxsize=1)
def first_party_packages() -> dict[str, str]:
    """``{import name: repo-relative directory that provides it}``, read off ``.pkgroot``.

    ``.pkgroot/<pkg> -> <member>/src`` is what makes the flat src layout (R1b/D42)
    importable, and it is the repo's own registry of first-party import names. Deriving the
    list from it rather than hard-coding one means a package added to the workspace later is
    graded by every check in this file with no edit here.
    """
    found: dict[str, str] = {}
    for entry in sorted(PKGROOT.iterdir()):
        if not entry.is_symlink():
            continue
        target = (PKGROOT / os.readlink(entry)).resolve()
        found[entry.name] = str(target.relative_to(REPO_ROOT))
    return found


@dataclass(frozen=True)
class ImageSpec:
    """Everything an image's Dockerfile says about what it will contain."""

    label: str
    #: ``(repo-relative source, absolute /app destination)`` for every ``COPY``.
    copies: tuple[tuple[str, str], ...]
    #: ``(link target, absolute /app link path)`` for every ``ln -s`` under ``/app``.
    links: tuple[tuple[str, str], ...]
    #: The image's ``sys.path`` roots, from ``ENV PYTHONPATH``. Defaults to ``/app``, which
    #: is ``WORKDIR`` and therefore ``sys.path[0]`` for the ``CMD`` process.
    path_roots: tuple[str, ...]
    #: The dotted module of the ``uvicorn <module>:app`` entrypoint, if the ``CMD`` names one.
    entrypoint: str | None


def parse_dockerfile_text(text: str, label: str) -> ImageSpec:
    """Parse the COPY set, the ``ln -s`` pairs, ``PYTHONPATH`` and the entrypoint.

    Line continuations are joined first, because ``ENV`` and the ``RUN`` that builds
    ``.pkgroot`` are both written across several backslash-continued lines.
    """
    joined = re.sub(r"\\\n", " ", text)

    copies: list[tuple[str, str]] = []
    for raw in re.findall(r"^COPY\s+(.+)$", joined, flags=re.MULTILINE):
        tokens = [t for t in raw.split() if not t.startswith("--")]
        if len(tokens) < 2:
            continue
        destination = tokens[-1]
        for source in tokens[:-1]:
            copies.append((source, destination))

    links = tuple(
        (target, link)
        for target, link in re.findall(r"\bln\s+-s\s+(\S+)\s+(\S+)", joined)
        if link.startswith("/app/")
    )

    match = re.search(r"\bPYTHONPATH=(\S+)", joined)
    path_roots = tuple(match.group(1).split(":")) if match else ("/app",)

    cmd = re.search(r"\"([A-Za-z_][\w.]*):app\"", joined)
    return ImageSpec(label, tuple(copies), links, path_roots, cmd.group(1) if cmd else None)


@lru_cache(maxsize=1)
def images() -> dict[str, ImageSpec]:
    """Every Dockerfile in the repo, keyed by its repo-relative path.

    Discovered, not listed: an image added later is graded automatically, and an image
    DELETED cannot silently take its gate with it.
    """
    found: dict[str, ImageSpec] = {}
    for path in sorted(REPO_ROOT.rglob("Dockerfile")):
        parts = set(path.parts)
        if ".venv" in parts or "node_modules" in parts or ".swarm-loop" in parts:
            continue
        label = str(path.relative_to(REPO_ROOT))
        found[label] = parse_dockerfile_text(path.read_text(encoding="utf-8"), label)
    return found


def image_path_for(spec: ImageSpec, repo_relative: str) -> str | None:
    """Where a repo file lands inside the image, or ``None`` if it is not copied."""
    for source, destination in spec.copies:
        src = source.rstrip("/")
        dst = destination.rstrip("/")
        if repo_relative == src:
            return dst
        if repo_relative.startswith(src + "/"):
            return dst + repo_relative[len(src) :]
    return None


def copies_directory(spec: ImageSpec, repo_relative_dir: str) -> str | None:
    """The ``COPY`` source that puts ``repo_relative_dir``'s contents in the image."""
    target = repo_relative_dir.rstrip("/") + "/"
    for source, _destination in spec.copies:
        src = source.rstrip("/") + "/"
        if target.startswith(src) or src.startswith(target):
            return source
    return None


def package_wiring(spec: ImageSpec, package: str) -> tuple[bool, str]:
    """Is ``package`` importable in this image? ``(verdict, why)``, from the text alone.

    Two conditions, and the repo needs both — see ``apps/exchange/Dockerfile:61,68``, where
    closing a missing package took a ``COPY`` *and* a matching ``ln -s``:

    1. the directory that provides the package is inside the ``COPY`` set, and
    2. something puts it on one of the image's ``sys.path`` roots — either a ``COPY``
       destination that lands it there directly (how ``shopify_stub`` and
       ``proxyshop_support`` work) or an ``ln -s`` into ``.pkgroot`` (how everything else
       does).
    """
    provider = first_party_packages().get(package)
    if provider is None:
        return False, f"{package!r} is not a name .pkgroot provides"
    if copies_directory(spec, provider) is None:
        return False, f"no COPY covers {provider}/"
    for _source, destination in spec.copies:
        dst = destination.rstrip("/")
        for root in spec.path_roots:
            if dst == f"{root.rstrip('/')}/{package}":
                return True, f"COPY lands it at {dst}"
    for target, link in spec.links:
        for root in spec.path_roots:
            if link.rstrip("/") == f"{root.rstrip('/')}/{package}":
                return True, f"ln -s {target} {link}"
    return False, f"{provider}/ is copied but nothing puts {package!r} on PYTHONPATH"


def shipped_sources(spec: ImageSpec) -> dict[str, str]:
    """``{repo-relative .py file: its /app path}`` for every module the image ships.

    ``**/tests/**`` and ``conftest.py`` are excluded. They ARE copied — ``COPY
    proxyshop_support/`` takes this very file into four of the five images — but a test
    module's imports are not the artifact's runtime closure and grading them would make
    this file's own imports part of what it grades.
    """
    found: dict[str, str] = {}
    for source, _destination in spec.copies:
        base = REPO_ROOT / source.rstrip("/")
        if base.is_file() and base.suffix == ".py":
            candidates = [base]
        elif base.is_dir():
            candidates = sorted(base.rglob("*.py"))
        else:
            continue
        for path in candidates:
            relative = path.relative_to(REPO_ROOT)
            if "tests" in relative.parts or "__pycache__" in relative.parts:
                continue
            if path.name == "conftest.py":
                continue
            landed = image_path_for(spec, str(relative))
            if landed is not None:
                found[str(relative)] = landed
    return found


# =====================================================================================
# (a) the STATIC check — the COPY set against the imports of the tree it ships
# =====================================================================================


@dataclass(frozen=True)
class ImportSite:
    """One ``import``/``from ... import`` of a first-party package in a shipped module."""

    module_path: str
    lineno: int
    col_offset: int
    statement: str

    @property
    def module_scope(self) -> bool:
        """Column 0 means the statement runs when the module is imported: FATAL.

        Anything indented is inside a function, a class body or a ``try`` — it may never
        run, and it may be swallowed. That is the whole fatal-vs-silent distinction, and it
        is COUNTED here rather than declared by a ticket.
        """
        return self.col_offset == 0

    def __str__(self) -> str:
        where = "module-scope" if self.module_scope else f"indented col {self.col_offset}"
        return f"{self.module_path}:{self.lineno} ({where}) {self.statement}"


def _first_party_imports(path: Path, packages: frozenset[str]) -> list[tuple[str, ast.stmt]]:
    """``(top-level package, import node)`` for every first-party import in a file.

    AST rather than grep: a package name inside a docstring, a comment or a string constant
    is not an import, and this repo's modules are heavily documented with example import
    lines that a text scan counts as real. Relative imports (``from .criteria import ...``)
    are skipped — they cannot name a first-party top-level package.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, ast.stmt]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in packages:
                    found.append((top, node))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            top = (node.module or "").split(".")[0]
            if top in packages:
                found.append((top, node))
    return found


@cache
def unwired_first_party_imports(label: str) -> dict[str, tuple[ImportSite, ...]]:
    """``{package: sites}`` for every first-party package an image imports but cannot resolve.

    This is check (a) in full. No subprocess, no filesystem materialisation, no assumption
    about what Docker does with a symlink: the Dockerfile's text and the shipped tree's ASTs
    are the only inputs.
    """
    spec = images()[label]
    packages = frozenset(first_party_packages())
    wired = {name: package_wiring(spec, name)[0] for name in packages}
    unwired: dict[str, list[ImportSite]] = {}
    for module_path in sorted(shipped_sources(spec)):
        for package, node in _first_party_imports(REPO_ROOT / module_path, packages):
            if wired[package]:
                continue
            unwired.setdefault(package, []).append(
                ImportSite(module_path, node.lineno, node.col_offset, ast.unparse(node)[:88])
            )
    return {package: tuple(sites) for package, sites in sorted(unwired.items())}


def _static_failure_report(label: str) -> str:
    spec = images()[label]
    lines = [f"{label}: the shipped tree imports first-party packages the image does not ship."]
    for package, sites in unwired_first_party_imports(label).items():
        _, why = package_wiring(spec, package)
        fatal = [s for s in sites if s.module_scope]
        lazy = [s for s in sites if not s.module_scope]
        lines.append(
            f"  {package!r}: {why} — {len(fatal)} module-scope site(s) (fatal at import) "
            f"and {len(lazy)} indented site(s) (lazy, possibly swallowed)"
        )
        lines.extend(f"      {site}" for site in sites)
    return "\n".join(lines)


# =====================================================================================
# (b) the RUNTIME check — materialise the COPY set and import what it contains
# =====================================================================================

_TEMP_TREES: dict[str, Path] = {}


def _cleanup_temp_trees() -> None:
    for path in _TEMP_TREES.values():
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup_temp_trees)


@cache
def materialised_image(label: str) -> Path:
    """Build a container-shaped ``/app`` from the Dockerfile's own COPY and ``ln -s`` lines.

    ``symlinks=True`` is the documented assumption of this whole half — see the module
    docstring. The ``ln -s`` pairs are READ OFF the Dockerfile rather than transcribed, so a
    link added by a fix is reproduced here and the gate can see its own repair.
    """
    spec = images()[label]
    root = Path(tempfile.mkdtemp(prefix="artifact-copyset-"))
    _TEMP_TREES[label] = root
    app = root / "app"
    app.mkdir()
    for source, destination in spec.copies:
        src = REPO_ROOT / source.rstrip("/")
        dst = app / destination.removeprefix("/app/").rstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
        elif src.is_file():
            shutil.copy2(src, dst)
    for target, link in spec.links:
        spot = app / link.removeprefix("/app/")
        spot.parent.mkdir(parents=True, exist_ok=True)
        if not spot.is_symlink() and not spot.exists():
            spot.symlink_to(target)
    return app


def _dotted_names(app: Path, path_roots: tuple[str, ...]) -> dict[str, set[str]]:
    """``{resolved file: every dotted name it is reachable by}`` inside the built tree.

    Names are collected per FILE, not per name, because one file is legitimately reachable
    by several: ``packages/contracts/generated/python/protocol.py`` is
    ``contracts.generated.protocol`` through the ``.pkgroot`` symlink and
    ``packages.contracts.generated.python.protocol`` from ``/app``. Only the first of those
    imports, and a per-name verdict would report the second as a defect in all five images.
    A file counts as broken only when EVERY name it has fails.

    Symlinks are followed, which is what reaches the ``.pkgroot`` tree at all. Path segments
    that are not Python identifiers (``.pkgroot``, ``shopify-stub``, ``store-agent``) are
    pruned, because no import statement can name them.
    """
    names: dict[str, set[str]] = {}
    for root in path_roots:
        stripped = root.rstrip("/")
        base = app if stripped == "/app" else app / stripped.removeprefix("/app/")
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=True):
            dirnames[:] = sorted(d for d in dirnames if d.isidentifier())
            relative = Path(dirpath).relative_to(base)
            prefix = [] if str(relative) == "." else list(relative.parts)
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                stem = filename[:-3]
                if not stem.isidentifier():
                    continue
                parts = prefix if stem == "__init__" else [*prefix, stem]
                if not parts:
                    continue
                resolved = str(Path(dirpath, filename).resolve())
                names.setdefault(resolved, set()).add(".".join(parts))
    return names


_IMPORT_PROBE = textwrap.dedent(
    """
    import importlib, json, sys, traceback
    print("SYSPATH " + json.dumps(sys.path))
    outcome = {}
    for name in json.loads(sys.argv[1]):
        try:
            module = importlib.import_module(name)
        except BaseException as exc:
            outcome[name] = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            outcome[name] = {"file": getattr(module, "__file__", None)}
    print("RESULT " + json.dumps(outcome))
    """
)


def run_in_image(label: str, code: str, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` against the built tree with the image's ``PYTHONPATH`` and nothing else.

    ``-S`` is load-bearing: without it ``site`` processes ``_proxyshop.pth`` and the LIVE
    CHECKOUT lands on ``sys.path`` whatever ``PYTHONPATH`` says, at which point the probe
    grades the repo rather than the artifact. ``-E``/``-I`` cannot be used — they discard
    ``PYTHONPATH``, which is the only thing pointing at the tree under test.
    """
    app = materialised_image(label)
    spec = images()[label]
    roots = [
        str(app if r.rstrip("/") == "/app" else app / r.rstrip("/").removeprefix("/app/"))
        for r in spec.path_roots
    ]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-S", "-c", code, *argv],
        cwd=str(app),
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.pathsep.join([*roots, str(SITE_PACKAGES)]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PROXYSHOP_WORKER": os.environ.get("PROXYSHOP_WORKER", "0"),
        },
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _tagged(stdout: str, tag: str) -> str | None:
    prefix = f"{tag} "
    return next(
        (line[len(prefix) :] for line in stdout.splitlines() if line.startswith(prefix)), None
    )


def _leaked_paths(entries: list[str]) -> list[str]:
    """Checkout paths on the probe's ``sys.path``. ``.venv`` is allowed; nothing else is."""
    leaked = []
    for entry in entries:
        if not entry:
            continue
        resolved = Path(entry).resolve()
        inside = resolved == REPO_ROOT or REPO_ROOT in resolved.parents
        if inside and SITE_PACKAGES not in {resolved, *resolved.parents}:
            leaked.append(entry)
    return leaked


@dataclass(frozen=True)
class ImportReport:
    """What actually happened when the shipped tree was imported module by module."""

    label: str
    module_count: int
    #: ``{repo-relative shipped file: the error every one of its names raised}``
    broken: dict[str, str]

    def summary(self) -> str:
        head = (
            f"{self.label}: {len(self.broken)} of {self.module_count} shipped modules cannot be "
            f"imported from the artifact's own COPY set"
        )
        return "\n".join(
            [head, *(f"    {name}: {why}" for name, why in sorted(self.broken.items()))]
        )


@cache
def image_import_report(label: str) -> ImportReport:
    """Check (b): import every shipped module inside the container-shaped tree."""
    spec = images()[label]
    app = materialised_image(label)
    by_file = _dotted_names(app, spec.path_roots)

    wanted: dict[str, set[str]] = {}
    for repo_relative, landed in shipped_sources(spec).items():
        built = app / landed.removeprefix("/app/")
        names = by_file.get(str(built.resolve()))
        if names:
            wanted[repo_relative] = names

    every_name = sorted({name for names in wanted.values() for name in names})
    result = run_in_image(label, _IMPORT_PROBE, json.dumps(every_name))

    reported_path = _tagged(result.stdout, "SYSPATH")
    assert reported_path is not None, (
        f"{label}: the container-shaped probe never reached its first statement, so this "
        f"gate measured nothing at all:\n{result.stderr.strip()[-1500:]}"
    )
    leaked = _leaked_paths(json.loads(reported_path))
    assert leaked == [], (
        f"{label}: the probe can see the live checkout on sys.path ({leaked}), so it can "
        f"import packages the image does not ship and this measurement is worthless. "
        f"{SITE_PACKAGES}/_proxyshop.pth puts {REPO_ROOT} and {PKGROOT} on sys.path at "
        f"interpreter startup regardless of PYTHONPATH; `-S` is what keeps them off."
    )

    raw = _tagged(result.stdout, "RESULT")
    assert raw is not None, (
        f"{label}: the probe did not finish; it graded nothing:\n{result.stderr.strip()[-1500:]}"
    )
    outcome: dict[str, dict[str, Any]] = json.loads(raw)

    packages = frozenset(first_party_packages()) | {"packages", "proxyshop_support"}
    for name, entry in sorted(outcome.items()):
        origin = entry.get("file")
        if origin is None or name.split(".")[0] not in packages:
            continue
        if app.resolve() not in Path(origin).resolve().parents:
            raise AssertionError(
                f"{label}: the probe imported {name} from {origin}, which is OUTSIDE the "
                f"container-shaped tree at {app.resolve()} — it graded the live checkout"
            )

    broken: dict[str, str] = {}
    for repo_relative, names in sorted(wanted.items()):
        errors = {n: outcome[n]["error"] for n in sorted(names) if "error" in outcome.get(n, {})}
        if len(errors) == len(names):
            broken[repo_relative] = "; ".join(sorted(set(errors.values())))
    return ImportReport(label, len(wanted), broken)


# =====================================================================================
# The positive control. NOT xfail — it must pass today, or nothing below means anything.
# =====================================================================================


def test_the_static_copy_set_checker_detects_a_fix_and_a_regression() -> None:
    """The checker flips in BOTH directions on Dockerfiles it has never seen.

    A checker that reports a defect it cannot actually detect is worthless, and this repo
    has already shipped one: ``apps/trust/tests/test_repro_open_tickets.py``'s image gate
    hard-coded the ``.pkgroot`` link pair and therefore measured FAILS against the *fixed*
    Dockerfile — it could not see its own repair.

    Two witnesses, both synthesised in memory and neither touching the repo:

    * **fix** — ``apps/merchant/Dockerfile`` plus the two lines that are already in
      ``apps/exchange/Dockerfile:61,68`` (``COPY apps/exchange/src/`` and the matching
      ``ln -s``). ``exchange`` must disappear from the merchant image's unwired set, and
      ``apps/merchant/svc/src/codes/offer.py`` — T-298's crash site — must stop being
      reported at all.

      It does NOT become empty, and that is a finding rather than a limitation of the
      control: ``COPY apps/exchange/src/`` also ships ``exchange.retrieval``, whose seven
      module-scope ``from ingest…`` imports (T-299) the merchant image does not satisfy
      either. Applying T-298's stated precedent verbatim moves the merchant image from one
      broken package to another, and the checker says so. Whoever closes T-298 needs to
      know that before they write the fix; asserting an empty set here would have hidden it.
    * **regression** — ``apps/trust/Dockerfile``, which is clean under this check, with its
      ``contracts`` ``ln -s`` deleted. The package is still COPY'd; nothing puts it on
      ``PYTHONPATH``. The checker must catch that, because a fix in this repo needs the
      ``COPY`` *and* the link and a checker that only looks at ``COPY`` would pass it.
    """
    merchant = (REPO_ROOT / "apps" / "merchant" / "Dockerfile").read_text(encoding="utf-8")
    before = parse_dockerfile_text(merchant, "merchant/before")
    assert "exchange" in _unwired_for(before), (
        f"the merchant image is expected to be FAILING on `exchange` today, got "
        f"{sorted(_unwired_for(before))}"
    )

    fixed_text = merchant.replace(
        "COPY apps/merchant/svc/src/      /app/apps/merchant/svc/src/",
        "COPY apps/merchant/svc/src/      /app/apps/merchant/svc/src/\n"
        "COPY apps/exchange/src/          /app/apps/exchange/src/",
    ).replace(
        " && ln -s ../apps/merchant/svc/src /app/.pkgroot/merchant_svc",
        " && ln -s ../apps/merchant/svc/src /app/.pkgroot/merchant_svc \\\n"
        " && ln -s ../apps/exchange/src /app/.pkgroot/exchange",
    )
    assert fixed_text != merchant, "the positive control patched nothing — its anchors moved"
    after = _unwired_for(parse_dockerfile_text(fixed_text, "merchant/after"))
    assert "exchange" not in after, (
        "the static checker CANNOT SEE the in-repo fix precedent. Adding "
        "`COPY apps/exchange/src/` and `ln -s ../apps/exchange/src /app/.pkgroot/exchange` "
        "to the merchant Dockerfile is exactly what apps/exchange/Dockerfile:61,68 does, and "
        f"the checker still reports `exchange` unwired: {after['exchange']}"
    )
    still_reported = {site.module_path for sites in after.values() for site in sites}
    assert "apps/merchant/svc/src/codes/offer.py" not in still_reported, (
        "T-298's crash site is still reported after T-298's own fix precedent is applied — "
        f"the checker cannot see the repair: {sorted(still_reported)}"
    )
    assert set(after) == {"ingest"}, (
        "the fixed merchant image is expected to report exactly one remaining package — "
        "`ingest`, dragged in with exchange.retrieval — and reported "
        f"{sorted(after)} instead. If this changed, the T-298/T-299 coupling changed with it."
    )

    trust = (REPO_ROOT / "apps" / "trust" / "Dockerfile").read_text(encoding="utf-8")
    clean = parse_dockerfile_text(trust, "trust/clean")
    assert _unwired_for(clean) == {}, (
        f"apps/trust/Dockerfile is expected to be clean under this check: {_unwired_for(clean)}"
    )
    broken_text = trust.replace(
        " && ln -s ../packages/contracts/src /app/.pkgroot/contracts \\\n", ""
    )
    assert broken_text != trust, "the regression control patched nothing — its anchor moved"
    broken = parse_dockerfile_text(broken_text, "trust/broken")
    assert "contracts" in _unwired_for(broken), (
        "the static checker is blind to a package that is COPY'd but never put on "
        "PYTHONPATH — the half of a fix that apps/trust/tests/test_repro_open_tickets.py "
        "records as necessary. It reported: " + repr(_unwired_for(broken))
    )


def _unwired_for(spec: ImageSpec) -> dict[str, tuple[ImportSite, ...]]:
    """:func:`unwired_first_party_imports` for a spec that is not one of the repo's images."""
    packages = frozenset(first_party_packages())
    wired = {name: package_wiring(spec, name)[0] for name in packages}
    unwired: dict[str, list[ImportSite]] = {}
    for module_path in sorted(shipped_sources(spec)):
        for package, node in _first_party_imports(REPO_ROOT / module_path, packages):
            if not wired[package]:
                unwired.setdefault(package, []).append(
                    ImportSite(module_path, node.lineno, node.col_offset, ast.unparse(node)[:88])
                )
    return {package: tuple(sites) for package, sites in sorted(unwired.items())}


def test_the_repo_has_images_and_first_party_packages_to_grade() -> None:
    """A checker that finds nothing to check passes every gate below for the wrong reason."""
    assert len(images()) >= 5, f"expected the five service images, found {sorted(images())}"
    packages = first_party_packages()
    assert len(packages) >= 10, f".pkgroot yielded too few packages to be the real one: {packages}"
    for label, spec in images().items():
        assert spec.copies, f"{label}: no COPY instructions parsed — the parser is broken"
        assert shipped_sources(spec), f"{label}: the COPY set contains no python module"


# =====================================================================================
# T-301 — the gate the whole artifact-blindness class was invisible to
# =====================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-301: three of the five images import a first-party package their Dockerfile does "
        "not ship — merchant/exchange (T-298/T-299, module scope, fatal) and buyer (T-300, "
        "lazy and swallowed); remove this marker when every COPY set covers its own imports"
    ),
)
def test_t301_every_image_copy_set_covers_every_first_party_import_it_ships() -> None:
    """Check (a) over every image at once: the COPY set must satisfy the shipped tree.

    This is the gate that did not exist. ``make verify``, the frozen acceptance suite and
    all twelve frozen metrics run against a checkout in which every package resolves, so a
    package missing from an image's ``COPY`` set moves none of them.

    The report distinguishes fatal from silent by COUNTING column-0 import sites against
    indented ones — no ticket tells it which is which. A module-scope site kills the module
    that holds it and every module that imports it; an indented one is lazy and may be
    swallowed, which is worse to find and no less real.
    """
    failures = [
        _static_failure_report(label)
        for label in sorted(images())
        if unwired_first_party_imports(label)
    ]
    assert failures == [], "\n\n".join(failures)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-301: the merchant and exchange images' COPY sets cannot import 7 and 11 of their "
        "own shipped modules respectively; remove this marker when every shipped module "
        "imports from the artifact that ships it"
    ),
)
def test_t301_every_shipped_module_imports_inside_the_container_shaped_tree() -> None:
    """Check (b) over every image at once: what ships must import from what ships.

    Not redundant with (a), in either direction. This half catches a module that IS in the
    COPY set but broken for a reason no COPY-set analysis can name — a syntax error, an
    import-time side effect, a third-party pin the Dockerfile's ``pip install`` layer
    forgot. It also measures the transitive blast radius that (a) reports as a single line:
    one bad import in ``merchant_svc/codes/offer.py`` takes seven modules with it.

    And (a) catches what this half cannot — see T-300, where this probe reports a perfectly
    clean buyer image while a shipped feature is permanently degraded.
    """
    reports = [image_import_report(label) for label in sorted(images())]
    failures = [report.summary() for report in reports if report.broken]
    assert failures == [], "\n\n".join(failures)


# =====================================================================================
# T-298 — the merchant container cannot start
# =====================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-298: apps/merchant/Dockerfile does not COPY the exchange package while "
        "apps/merchant/svc/src/codes/offer.py:36 imports exchange.checkout.discounts at "
        "module scope, so uvicorn merchant_svc.main:app raises ModuleNotFoundError and the "
        "container cannot start; remove this marker with the fix"
    ),
)
def test_t298_the_merchant_image_can_import_its_own_entrypoint() -> None:
    """The merchant image's ``CMD`` module must import from the merchant image.

    ``apps/merchant/Dockerfile:80`` is ``CMD ["uvicorn", "merchant_svc.main:app", ...]`` and
    ``apps/merchant/svc/src/codes/offer.py:36`` is ``from exchange.checkout.discounts import
    UnusableDiscount, shopify_discount_percentage`` at column 0. ``exchange`` is not in this
    image's ``COPY`` set, so the import raises before uvicorn has an app: **the container
    cannot start.** Not degraded, not slow — dead on the first boot.

    The entrypoint is read out of the Dockerfile's own ``CMD`` rather than written here, so
    an entrypoint that moves cannot leave this test grading a module nobody runs.

    Both halves are asserted because they fail for different reasons and a repair could
    plausibly address one and not the other. The in-repo fix precedent is
    ``apps/exchange/Dockerfile:61,68`` verbatim — a ``COPY apps/exchange/src/`` plus
    ``ln -s ../apps/exchange/src /app/.pkgroot/exchange``.
    """
    label = "apps/merchant/Dockerfile"
    spec = images()[label]
    assert spec.entrypoint == "merchant_svc.main", (
        f"expected the merchant CMD to name merchant_svc.main:app, parsed {spec.entrypoint!r}"
    )

    wired, why = package_wiring(spec, "exchange")
    report = image_import_report(label)
    entrypoint_file = "apps/merchant/svc/src/main.py"
    assert wired, (
        f"{label} cannot resolve `exchange` ({why}), which "
        f"apps/merchant/svc/src/codes/offer.py imports at module scope. "
        f"{len(report.broken)} of {report.module_count} shipped modules are unimportable:\n"
        f"{report.summary()}"
    )
    assert entrypoint_file not in report.broken, (
        f"{label}'s entrypoint module cannot be imported from the artifact that ships it: "
        f"{report.broken.get(entrypoint_file)}"
    )


# =====================================================================================
# T-299 — the exchange image starts, and is crippled
# =====================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-299: apps/exchange/Dockerfile does not COPY the ingest package, so 11 of the "
        "image's shipped modules — exchange.ranking and all of exchange.retrieval — raise "
        "ModuleNotFoundError while exchange.main imports fine and every entry-point check "
        "passes; remove this marker when the shipped tree's imports resolve"
    ),
)
def test_t299_every_module_the_exchange_image_ships_can_be_imported_from_it() -> None:
    """The exchange image ships modules it cannot import, and its entrypoint hides it.

    ``exchange.main`` imports cleanly, so a smoke test, a healthcheck on ``/openapi.json``
    and any entry-point probe pass this image forever. Underneath, ``exchange.ranking``,
    ``ranking.filters``/``reasons``/``scoring``/``shortlist`` and every module of
    ``exchange.retrieval`` raise ``ModuleNotFoundError: No module named 'ingest'``. Among
    the casualties is ``retrieval/fit.py``, which holds the repo's ONLY emitter of the
    ``bid_placed`` ledger event (``FIT_LEDGER_KIND`` at line 61, emitted at lines 311-316) —
    a kind whose count is load-bearing in two frozen metrics.

    **This test deliberately does not say what the repair is.** Whether ``ingest`` is meant
    to live in the exchange image is an open design question: a deliberate service split
    with an HTTP boundary would make the correct repair *removing* the imports, not adding
    the ``COPY``. Both repairs turn this green, because what is asserted is the fact — the
    shipped tree's imports do not resolve in the shipped tree — and not a preferred fix.
    """
    report = image_import_report("apps/exchange/Dockerfile")
    assert report.broken == {}, report.summary()


# =====================================================================================
# T-300 — the buyer image runs a feature permanently degraded, and imports clean
# =====================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-300: packages/llm is absent from the buyer image's COPY set while "
        "apps/buyer/svc/src/intent/routes.py:99 imports it inside a function under "
        "try/except ImportError, so the clarification loop runs without a model forever and "
        "logs a warning instead of failing; remove this marker with the fix"
    ),
)
def test_t300_the_buyer_image_ships_the_llm_package_its_intent_router_imports() -> None:
    """The case that proves the static check is not redundant with the runtime one.

    ``apps/buyer/svc/src/intent/routes.py:99`` is ``from llm import build_llm``, INDENTED
    inside ``_resolve_llm`` and wrapped in ``except ImportError`` that logs
    ``"packages/llm is not importable; clarifying without a model"`` and returns ``None``.
    Lazy AND swallowed. Consequences, in order:

    * every module the buyer image ships imports cleanly, so check (b) reports zero
      failures for this image — which is NOT a clean bill of health;
    * no container crashes, no healthcheck fails, no metric moves;
    * the deployed clarification loop silently degrades to its own hard-coded wording and
      its own extraction, for the life of the image, and the only evidence is one WARNING
      line in a log nobody is grading.

    Only COPY-set analysis can see this. That is the entire argument for building check (a)
    alongside a runtime probe rather than instead of one, and it is asserted here rather
    than argued: the runtime report for this image is checked to be EMPTY, so a future
    reader cannot mistake a green probe for a working feature.
    """
    label = "apps/buyer/Dockerfile"
    spec = images()[label]
    unwired = unwired_first_party_imports(label)

    assert image_import_report(label).broken == {}, (
        "the premise of this test has changed: the buyer image now has modules that fail to "
        "import outright, so it is no longer the lazy-and-swallowed case — re-read T-300"
    )

    sites = unwired.get("llm", ())
    indented = [site for site in sites if not site.module_scope]
    assert "llm" not in unwired, (
        f"{label} cannot resolve `llm` ({package_wiring(spec, 'llm')[1]}), and every one of "
        f"the {len(sites)} import site(s) is indented ({len(indented)} of {len(sites)}), i.e. "
        f"lazy and swallowed rather than fatal — so the container starts, the runtime import "
        f"probe reports 0 broken modules for this image, and the clarification loop runs "
        f"without a model forever:\n" + "\n".join(f"      {site}" for site in sites)
    )


# =====================================================================================
# T-304 — the same class, reached by a relative path rather than a missing package
# =====================================================================================


_RECORDINGS_PROBE = textwrap.dedent(
    """
    import json, sys
    print("SYSPATH " + json.dumps(sys.path))
    import shopify_stub.recordings as recordings
    print("RESULT " + json.dumps({
        "module": recordings.__file__,
        "dir": str(recordings.RECORDINGS_DIR),
        "exists": recordings.RECORDINGS_DIR.is_dir(),
    }))
    """
)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-304: services/shopify-stub/src/recordings.py:46 resolves RECORDINGS_DIR as "
        "parent.parent/'fixtures'/'recorded', which is /app/fixtures/recorded in the "
        "flattened container while services/shopify-stub/Dockerfile:26 lands the fixtures at "
        "/app/services/shopify-stub/fixtures; remove this marker with the fix"
    ),
)
def test_t304_the_shopify_stub_image_resolves_its_recordings_directory() -> None:
    """A COPY-set gate would NOT catch this, which is why it is gated separately.

    Same class — the checkout resolves it and the artifact does not — reached by a different
    mechanism. Nothing is missing from the ``COPY`` set: ``services/shopify-stub/Dockerfile``
    copies the fixtures. It copies them to ``/app/services/shopify-stub/fixtures/`` while
    line 25 flattens ``src/`` to ``/app/shopify_stub/``, so
    ``Path(__file__).resolve().parent.parent`` — ``packages/…/services/shopify-stub`` in the
    repo — is bare ``/app`` in the image and the lookup lands on ``/app/fixtures/recorded``.

    Latent today: nothing under ``src/`` imports ``recordings``, so the module is dead weight
    in the image rather than a crash. It is still the artifact being wrong, and the first
    consumer to import it inherits an empty recordings set with no error.

    Measured by running the module inside the built tree rather than by re-deriving the
    arithmetic here, so a repair by ANY route — moving the ``COPY``, changing the flatten,
    rewriting the path expression — turns this green.
    """
    label = "services/shopify-stub/Dockerfile"
    result = run_in_image(label, _RECORDINGS_PROBE)

    reported_path = _tagged(result.stdout, "SYSPATH")
    assert reported_path is not None, (
        f"the probe never started, so this gate measured nothing:\n{result.stderr.strip()[-1200:]}"
    )
    assert _leaked_paths(json.loads(reported_path)) == [], (
        f"the probe can see the live checkout on sys.path, so it would find the repo's "
        f"fixtures rather than the image's: {_leaked_paths(json.loads(reported_path))}"
    )

    raw = _tagged(result.stdout, "RESULT")
    assert raw is not None, (
        f"shopify_stub.recordings could not be imported from the container-shaped tree at "
        f"all, so this gate is red BEFORE the lookup it exists to grade:\n"
        f"{result.stderr.strip()[-1200:]}"
    )
    payload = json.loads(raw)
    app = materialised_image(label).resolve()
    assert app in Path(payload["module"]).resolve().parents, (
        f"the probe imported recordings from {payload['module']}, outside the tree at {app}"
    )
    assert payload["exists"], (
        f"the shipped shopify-stub image resolves RECORDINGS_DIR to {payload['dir']}, which "
        f"does not exist in the image. services/shopify-stub/Dockerfile:25 flattens src/ to "
        f"/app/shopify_stub/ while line 26 lands the fixtures at "
        f"/app/services/shopify-stub/fixtures/, so recordings.py:46's "
        f"`Path(__file__).resolve().parent.parent / 'fixtures'` points at /app/fixtures."
    )


# =====================================================================================
# T-313 / T-314 — the components with no artifact at all, and the one never built
# =====================================================================================


@lru_cache(maxsize=1)
def compose_fragments() -> tuple[str, ...]:
    """The compose fragments the root stack includes, read off ``docker-compose.yml``."""
    root = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    included = root.get("include") or []
    return tuple(str(entry) for entry in included)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-313: services/ingest, services/sim, packages/store-agent and "
        "apps/seller-reference are included in the root compose stack but their fragments "
        "are still the T-000 `services: {}` stubs and none of the four has a Dockerfile, so "
        "they cannot be deployed at all; remove this marker with the fix"
    ),
)
def test_t313_every_component_the_stack_includes_is_actually_deployable() -> None:
    """Four components are in the deployable stack on paper and have no artifact.

    ``docker-compose.yml``'s ``include:`` list names nine fragments. Four of them —
    ``services/ingest``, ``services/sim``, ``packages/store-agent``,
    ``apps/seller-reference`` — are literally ``services: {}``, and none of the four
    directories contains a Dockerfile. There is nothing to build and nothing to run.

    This is a PACKAGING gap, not missing code: ``ingest.main:app`` boots and serves its
    routes under uvicorn in the checkout, and ``docs/deploy.md:22-24`` records the same four
    as "still the T-000 ``services: {}`` stubs". A component that runs in a checkout and
    cannot be deployed is exactly the artifact blindness this file exists to grade, one
    level up from a missing ``COPY``.

    The component list is derived from the root file's own ``include:`` so a fragment added
    later is graded, and a fragment that grows a real service definition stops being
    reported without an edit here.
    """
    undeployable: list[str] = []
    for fragment in compose_fragments():
        path = REPO_ROOT / fragment
        component = path.parent
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        services = parsed.get("services") or {}
        if not services:
            has_dockerfile = (component / "Dockerfile").is_file()
            undeployable.append(
                f"{fragment}: declares no service (still the T-000 `services: {{}}` stub); "
                f"{component.relative_to(REPO_ROOT)}/Dockerfile "
                f"{'exists' if has_dockerfile else 'DOES NOT EXIST'}"
            )
            continue
        for name, definition in services.items():
            build = (definition or {}).get("build") or {}
            declared = build.get("dockerfile") if isinstance(build, dict) else None
            if declared and not (REPO_ROOT / declared).is_file():
                undeployable.append(f"{fragment}: service {name} names a missing {declared}")

    assert undeployable == [], (
        "components are included in the deployable stack that cannot be deployed:\n"
        + "\n".join(f"    {line}" for line in undeployable)
    )


#: The phrases the stub's own compose fragment uses to record that its image is unbuilt.
_UNBUILT_MARKERS = ("NOT VERIFIED", "never been BUILT", "expect to debug the build")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-314: services/shopify-stub/compose.yaml carries its own warning that the image "
        "has never been built on this host, while docs/demo/starting-slice.md:64 sends an "
        "operator into it as step 1 of the demo; remove this marker once the image has been "
        "built and the warning removed"
    ),
)
def test_t314_the_shopify_stub_image_is_not_declared_unbuilt_by_its_own_compose_fragment() -> None:
    """The demo's step 1 sends an operator into an image the repo says was never built.

    ``services/shopify-stub/compose.yaml`` carries, in its own header: "⚠️ NOT VERIFIED
    OFFLINE. ``docker compose config`` parses this fragment (that much was checked), but the
    image has never been BUILT here … Whoever first runs ``docker compose --profile e2e up
    shopify-stub`` should expect to debug the build, not the service."

    ``docs/demo/starting-slice.md:64`` is that exact command, under the heading "## 1. Bring
    up the stack". The stub itself is not the problem — it runs perfectly under uvicorn,
    answers ``GET /healthz`` with 200, serves eleven control routes and is fully offline —
    so the gap is entirely between the working service and the artifact nobody has built.

    **What this gate can and cannot prove.** It is textual: it asserts the warning is gone.
    Deleting the comment without building the image would satisfy its letter and would be a
    lie, and no offline test can distinguish the two — D3 forbids a verify that reaches the
    network, and the build pulls a base image and downloads wheels. The honest repair is to
    build the image once, record what happened, and then remove the warning. The COPY set of
    this same image is graded for real by the two T-301 checks above; this test grades the
    one thing they cannot reach.
    """
    fragment = REPO_ROOT / "services" / "shopify-stub" / "compose.yaml"
    text = fragment.read_text(encoding="utf-8")
    present = [marker for marker in _UNBUILT_MARKERS if marker in text]
    runbook = REPO_ROOT / "docs" / "demo" / "starting-slice.md"
    routed = "docker compose --profile e2e up -d shopify-stub" in runbook.read_text(
        encoding="utf-8"
    )
    assert present == [], (
        f"services/shopify-stub/compose.yaml still declares its own image unbuilt "
        f"{present!r}, and docs/demo/starting-slice.md "
        f"{'DOES' if routed else 'does not'} route an operator to "
        f"`docker compose --profile e2e up -d shopify-stub` as step 1 of the demo"
    )
