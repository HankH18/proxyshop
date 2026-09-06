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

The third-party half of that only works because the probe is WALLED OFF from the dev venv.
``site-packages`` has to be on the probe's path — the image ``pip install``s real wheels,
and without them every module importing ``fastapi`` would fail for a reason the image does
not have — but the venv holds the whole DEV set, which is far larger than the nine wheels a
service image installs. :data:`_PIP_LAYER_WALL` therefore refuses any top-level name that
resolves inside ``site-packages`` and is not in that image's own computed pip closure.
Without it this file reported all four service images clean while every one of them ships
``proxyshop_support/fixture_loader.py``, whose line 58 is ``import pytest`` at column 0 and
whose ``pytest`` is in no image's pip layer. That was a false green on a live defect, found
by adversarial review OF THIS FILE, and it is why the probe computes a closure instead of
trusting ``PYTHONPATH``.

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

**The assumption the runtime half rests on, stated rather than hidden — and stated at the
strength the evidence actually supports, which is weaker than the first draft of this
paragraph claimed.** The materialiser uses ``shutil.copytree(..., symlinks=True)``, i.e. it
assumes Docker's ``COPY`` copies a symlink as a symlink rather than dereferencing it. It is
NOT verified against a running daemon here: D3 forbids a verify that reaches the network,
and the build pulls a base image and downloads wheels.

The in-repo support for it is ``apps/exchange/Dockerfile:56-58``, which says that copying
``packages/contracts/src/`` alone "lands a dangling link in the image and
``contracts.protocol`` dies on ``No module named 'contracts.generated'`` at import time —
observed, not theorised". A ``COPY`` that dereferenced would have materialised the
symlink's contents instead, so the inference is valid. Three honest qualifications, each
one an overstatement that adversarial review caught in the previous wording:

* that sentence is not a distinct observation record. It appears VERBATIM in all four
  service Dockerfiles (``apps/exchange:58``, ``apps/trust:57``, ``apps/buyer:62``,
  ``apps/merchant:58``), inside the block those files themselves label "BYTE-IDENTICAL". It
  is replicated boilerplate, and it is one claim, not four.
* it cannot be squared with "no image here has ever been built" — a claim this paragraph
  used to make. ``services/shopify-stub/compose.yaml``'s warning is scoped to THAT image
  only (``grep -rl "never been BUILT" *(compose)*`` returns that one file), so it never
  supported the general statement, and if the general statement were true the exchange
  comment could not have been observed. The general claim is withdrawn.
* ``symlinks=True`` is the stricter reading for the DANGLING case that motivated it, but it
  is not conservative in general: a repo symlink with an ABSOLUTE target resolves on the
  host and would dangle in the image, which this materialiser would not reproduce. Audited:
  the only symlink under any image's ``COPY`` sources is the relative
  ``packages/contracts/src/generated -> ../generated/python``, so there is no live instance.

``symlinks=False`` (what ``apps/trust/tests/test_repro_open_tickets.py`` uses) dereferences,
which would silently repair a dangling-symlink defect inside the probe and hand back a
false green — so the choice here is still the right one.

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
* That report comes back on a PRIVATE channel (:data:`_PROBE_CHANNEL`), not on stdout, and
  every name the probe was asked about must appear in it. Both halves were false greens
  found by executing this file rather than reading it. The probe imports ~513 shipped
  modules in its own process, so its stdout belongs to the code under test as much as to
  the probe: the old reader took the FIRST ``RESULT ``-prefixed line while the probe emits
  its own LAST, and the old outcome lookup read a name the probe never mentioned as a clean
  import. One ``print`` at import time in any shipped module therefore replaced the whole
  outcome dictionary AND turned every genuinely broken module green — measured on a scratch
  tree, ten broken merchant modules including the image's own ``uvicorn`` entrypoint went
  to ``0 broken`` with no error, no checker edit and no ``.dockerignore`` edit. A stray line
  now changes nothing (no stdout line is parsed), and a short report is an assertion
  failure naming the missing names rather than a clean bill of health.
* A private channel and a full set of verdicts are NOT sufficient, and this bullet exists
  because the previous one was read as if they were. An adversarial review measured four
  more ways a single shipped module — which runs inside the probe's own process — could
  zero the report while satisfying both: forging an origin on the dotted spelling the
  origin guard did not examine (``10 broken of 79`` to ``6``, hiding all four
  ``envelope/*`` modules), forging the whole report and then ``chmod 444``-ing the channel
  so the probe's own final write died (``0 broken`` at exit 0, unchecked ``returncode``),
  reducing every verdict to ``{"file": None}``, and — in the guard file rather than here —
  re-adding a stdout reader as a FALLBACK, which all three guard tests missed. So a report
  is now graded only if the subprocess exited 0, every asked name has a verdict, every
  verdict carries a non-empty ``error`` or ``file`` string, and every clean verdict's origin
  resolves inside the container-shaped tree WHATEVER its top-level spelling. All four cost
  nothing on a clean tree: 914 verdicts across nine images, zero null origins, zero origins
  outside the tree, nine probes exiting 0. :data:`_PROBE_CHANNEL` carries the detail, and
  ``test_artifact_copyset_probe_channel.py`` holds one guard test per hole, each one proved
  red by reverting its own check.
* :func:`test_the_static_copy_set_checker_detects_a_fix_and_a_regression` is the positive
  control: it synthesises a fixed Dockerfile and a broken one in memory — no file in the
  repo is touched — and proves the checker flips in both directions. A checker that cannot
  see a fix is worthless, and the trust lane's equivalent gate was found XPASSing against a
  live defect for exactly that reason.

A defect test lands here as ``@pytest.mark.xfail(strict=True)``: a normal run reports
``xfailed`` and ``make verify`` stays green, the ticket's gate runs ``--runxfail -k <name>``
and gets a real red, and ``strict=True`` means whoever fixes the defect must delete the
marker. There are none left in this file — T-334 was the last, and its marker went with the
repair, so every node here now grades live.

Nothing in this file touches a Dockerfile, a compose fragment or any product source. This
lane writes gates, not fixes.
"""

from __future__ import annotations

import ast
import atexit
import fnmatch
import json
import os
import posixpath
import re
import shlex
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
    #: ``(repo-relative source, absolute destination)`` for every build-context ``COPY``.
    copies: tuple[tuple[str, str], ...]
    #: ``(target, absolute link path)`` for every ``ln -s`` the build creates under WORKDIR.
    links: tuple[tuple[str, str], ...]
    #: The image's ``sys.path`` roots, from ``ENV PYTHONPATH``. Defaults to ``WORKDIR``,
    #: which is ``sys.path[0]`` for the ``CMD`` process.
    path_roots: tuple[str, ...]
    #: The dotted module of the ``uvicorn <module>:app`` entrypoint, if a ``CMD`` names one.
    entrypoint: str | None
    #: Raw requirement tokens from every ``RUN pip install`` — the image's dependency layer.
    pip_requirements: tuple[str, ...]
    #: ``COPY --from=<stage|image> …`` lines. Their bytes do NOT come from the build context,
    #: so this checker cannot model them; it refuses to guess. See
    #: :func:`test_the_repo_has_images_and_first_party_packages_to_grade`.
    stage_copies: tuple[str, ...]
    #: ``COPY ["src", "dest"]`` JSON-array lines, likewise refused rather than mis-parsed.
    json_form_copies: tuple[str, ...]
    #: The image's ``WORKDIR``, which relative ``ln -s`` link paths resolve against.
    workdir: str


def _instructions(text: str) -> list[tuple[str, str]]:
    """``(INSTRUCTION, argument text)`` for every Dockerfile instruction.

    Comments are stripped BEFORE continuations are joined, which is Docker's own order and
    the reason this is not a one-line regex. Do it the other way round — the naive
    ``re.sub(r"\\\\\\n", " ", text)`` — and a comment whose last character happens to be a
    backslash swallows the instruction beneath it, silently removing a whole ``COPY`` (and
    everything it ships) from the graded set. A gate that goes quiet is worse than one that
    goes red.

    Instruction keywords are matched case-insensitively and may be indented: ``copy`` and
    ``  COPY`` are both legal Dockerfile and both used to parse to nothing here.
    """
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    joined = re.sub(r"\\[ \t]*\n", " ", "\n".join(kept))
    found: list[tuple[str, str]] = []
    for line in joined.splitlines():
        match = re.match(r"\s*([A-Za-z][A-Za-z_]*)\s+(.*\S)\s*$", line)
        if match:
            found.append((match.group(1).upper(), match.group(2)))
    return found


def parse_dockerfile_text(text: str, label: str) -> ImageSpec:
    """Parse the COPY set, the ``ln -s`` pairs, ``PYTHONPATH``, the pip layer and the CMD.

    Every one of the widenings below replaces a narrower pattern that an adversarial review
    broke with legal Dockerfile syntax. They are listed because each one was a measured
    false green or false red, not a hypothetical:

    * ``COPY --from=<stage>`` is separated out rather than having its flag stripped and its
      source read as a repo path. Stripping ``--from`` made the materialiser copy a
      directory OUT OF THE REPO that the build context never supplies, which closed T-298
      against a Dockerfile whose bytes could not exist in the image.
    * ``COPY ["src","dest"]`` (JSON form) is recorded and refused instead of being split on
      whitespace into a nonsense pair.
    * ``ENV PYTHONPATH`` accepts the legacy space form and quotes, and the LAST assignment
      wins, because Docker's does. The old pattern took the first ``PYTHONPATH=`` anywhere
      in the file — including inside a comment.
    * ``ln`` accepts ``-sf``/``-fs``/``--symbolic`` and a link path relative to ``WORKDIR``.
      A dropped link was a double false red: the package reported unwired AND the whole
      package reported broken by the runtime half.
    * ``CMD``/``ENTRYPOINT`` accept the shell form, not just the exec form.
    """
    copies: list[tuple[str, str]] = []
    links: list[tuple[str, str]] = []
    pip_requirements: list[str] = []
    stage_copies: list[str] = []
    json_form: list[str] = []
    path_roots: tuple[str, ...] | None = None
    entrypoint: str | None = None
    workdir = "/"

    for instruction, rest in _instructions(text):
        if instruction == "WORKDIR":
            workdir = rest.strip()
        elif instruction == "COPY":
            if rest.lstrip().startswith("["):
                json_form.append(rest)
                continue
            flags = [t for t in rest.split() if t.startswith("--")]
            tokens = [t for t in rest.split() if not t.startswith("--")]
            if len(tokens) < 2:
                continue
            destination = tokens[-1]
            if any(f.startswith("--from=") for f in flags):
                stage_copies.append(rest)
                continue
            for source in tokens[:-1]:
                copies.append((source, destination))
        elif instruction == "ENV":
            for found in re.finditer(r"PYTHONPATH(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)", rest):
                value = found.group(1).strip("\"'")
                path_roots = tuple(part for part in value.split(":") if part)
        elif instruction == "RUN":
            for target, link in re.findall(
                r"\bln\s+(?:-[A-Za-z]*s[A-Za-z]*|--symbolic)\s+(\S+)\s+(\S+)", rest
            ):
                absolute = link if link.startswith("/") else posixpath.join(workdir, link)
                links.append((target, posixpath.normpath(absolute)))
            install = re.search(r"\bpip\s+install\b(.*)", rest)
            if install:
                try:
                    tokens = shlex.split(install.group(1))
                except ValueError:  # pragma: no cover - unbalanced quotes in a RUN
                    tokens = install.group(1).split()
                pip_requirements.extend(t for t in tokens if t and not t.startswith("-"))
        elif instruction in {"CMD", "ENTRYPOINT"}:
            named = re.search(r"([A-Za-z_][\w.]*):app\b", rest)
            if named:
                entrypoint = named.group(1)

    return ImageSpec(
        label=label,
        copies=tuple(copies),
        links=tuple(links),
        path_roots=path_roots or (workdir,),
        entrypoint=entrypoint,
        pip_requirements=tuple(pip_requirements),
        stage_copies=tuple(stage_copies),
        json_form_copies=tuple(json_form),
        workdir=workdir,
    )


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


def _real_copies(spec: ImageSpec) -> list[tuple[str, str]]:
    """The ``COPY`` pairs whose source actually EXISTS in the build context.

    A ``COPY`` naming a path the repo does not contain fails the real ``docker build``, so
    it can never make a package present. Trusting one is a false green with a very short
    recipe: ``COPY packages/llm/src/NOTHING_HERE /app/junk`` plus the matching ``ln -s``
    used to close T-300 while shipping nothing at all.
    """
    return [(s, d) for s, d in spec.copies if (REPO_ROOT / s.rstrip("/")).exists()]


def image_path_for(spec: ImageSpec, repo_relative: str) -> str | None:
    """Where a repo file lands inside the image, or ``None`` if it is not copied.

    The LAST matching ``COPY`` wins, because Docker applies them in order and a later one
    overwrites an earlier one. Returning the first match put files at a path the built tree
    never had; :func:`image_import_report` then found no dotted name for them and dropped
    them from the graded set without a word.

    A destination written with a trailing slash is a DIRECTORY, so a file source lands
    inside it under its own basename — ``COPY a/b.py /app/dir/`` is ``/app/dir/b.py``, not
    ``/app/dir``.
    """
    landed: str | None = None
    for source, destination in _real_copies(spec):
        src = source.rstrip("/")
        dst = destination.rstrip("/")
        if repo_relative == src:
            is_file = (REPO_ROOT / src).is_file()
            landed = (
                f"{dst}/{posixpath.basename(src)}" if is_file and destination.endswith("/") else dst
            )
        elif repo_relative.startswith(src + "/"):
            landed = dst + repo_relative[len(src) :]
    return landed


def copies_directory(spec: ImageSpec, repo_relative_dir: str) -> str | None:
    """The ``COPY`` source that puts ``repo_relative_dir``'s contents in the image."""
    target = repo_relative_dir.rstrip("/") + "/"
    for source, _destination in _real_copies(spec):
        src = source.rstrip("/") + "/"
        if target.startswith(src) or src.startswith(target):
            return source
    return None


def image_path_is_materialised(spec: ImageSpec, image_path: str) -> bool:
    """Does some ``COPY`` actually put bytes at ``image_path`` inside the image?"""
    wanted = image_path.rstrip("/") + "/"
    for _source, destination in _real_copies(spec):
        landed = destination.rstrip("/") + "/"
        if wanted.startswith(landed) or landed.startswith(wanted):
            return True
    return False


def package_wiring(spec: ImageSpec, package: str) -> tuple[bool, str]:
    """Is ``package`` importable in this image? ``(verdict, why)``, from the text alone.

    Three conditions, and the repo needs all three — see ``apps/exchange/Dockerfile:61,68``,
    where closing a missing package took a ``COPY`` *and* a matching ``ln -s``:

    1. the directory that provides the package is inside the ``COPY`` set, with a source
       that exists in the build context;
    2. something puts the package's NAME on one of the image's ``sys.path`` roots — either a
       ``COPY`` destination landing it there directly (how ``shopify_stub`` and
       ``proxyshop_support`` work) or an ``ln -s`` into ``.pkgroot`` (how everything else
       does); and
    3. if it is an ``ln -s``, the link's TARGET has to be somewhere the ``COPY`` set
       actually lands. This third condition is not pedantry. ``COPY packages/llm/src/
       /app/lib/llm/`` plus ``ln -s ../packages/llm/src /app/.pkgroot/llm`` is a perfectly
       buildable image in which ``.pkgroot/llm`` is a DANGLING link — the package is in the
       image and still not importable — and without this check the checker called it wired
       and dropped T-300's finding.
    """
    provider = first_party_packages().get(package)
    if provider is None:
        return False, f"{package!r} is not a name .pkgroot provides"
    if copies_directory(spec, provider) is None:
        return False, f"no COPY covers {provider}/"
    for _source, destination in _real_copies(spec):
        dst = destination.rstrip("/")
        for root in spec.path_roots:
            if dst == f"{root.rstrip('/')}/{package}":
                return True, f"COPY lands it at {dst}"
    for target, link in spec.links:
        for root in spec.path_roots:
            if link.rstrip("/") != f"{root.rstrip('/')}/{package}":
                continue
            pointed = (
                target
                if target.startswith("/")
                else posixpath.normpath(posixpath.join(posixpath.dirname(link), target))
            )
            if image_path_is_materialised(spec, pointed):
                return True, f"ln -s {target} {link}"
            return False, (
                f"{link} -> {pointed}, which no COPY puts in the image: the link dangles"
            )
    return False, f"{provider}/ is copied but nothing puts {package!r} on PYTHONPATH"


def path_root_claims(spec: ImageSpec) -> dict[str, str]:
    """``{package: the sys.path entry the build creates for it}`` — the image's own CLAIMS.

    A build that lands ``<pkg>`` on one of the image's ``sys.path`` roots — by an ``ln -s``
    into ``.pkgroot`` (how most images do it) or by a ``COPY`` destination (how
    ``services/shopify-stub/Dockerfile``, which has no ``ln -s`` at all, does it) — is
    asserting that ``import <pkg>`` works inside the image. That assertion stands whether or
    not any module survives to make the import, which is exactly why :func:`_unwired_for`
    needs it: see T-334, where deleting a ``COPY`` deleted the package's only importer and
    took the evidence that the ``COPY`` was needed away with it.

    Deliberately the mirror of :func:`package_wiring`'s conditions 2 and 3: this asks only
    *does the build put the NAME there*, never *does anything back it up*. Keeping the two
    apart is what lets a report be raised from a verdict that is already computed.

    ``_real_copies``, not ``spec.copies``: a ``COPY`` whose source is not in the build
    context fails ``docker build`` outright, so it claims nothing — it just breaks, and
    :func:`test_the_repo_has_images_and_first_party_packages_to_grade` is what reports that.
    """
    roots = tuple(root.rstrip("/") for root in spec.path_roots)
    claimed: dict[str, str] = {}
    for package in sorted(first_party_packages()):
        wanted = {f"{root}/{package}" for root in roots}
        for _source, destination in _real_copies(spec):
            if destination.rstrip("/") in wanted:
                claimed[package] = destination
                break
        if package in claimed:
            continue
        for _target, link in spec.links:
            if link.rstrip("/") in wanted:
                claimed[package] = link
                break
    return claimed


@lru_cache(maxsize=1)
def dockerignore_patterns() -> tuple[str, ...]:
    """The repo's ``.dockerignore`` patterns — what the build context does NOT contain."""
    path = REPO_ROOT / ".dockerignore"
    if not path.is_file():
        return ()
    return tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def is_dockerignored(repo_relative: str) -> bool:
    """Approximate Docker's exclusion of a build-context path.

    An approximation, and named as one: it applies each pattern to the full repo-relative
    path and to every path component, which covers the ``**/__pycache__``, ``node_modules``,
    ``dist`` shapes this repo's ``.dockerignore`` actually uses. It does not implement
    negation (``!pattern``) or Go's full ``filepath.Match`` semantics. The consequence of
    getting it wrong is bounded: a file wrongly kept is graded when the image would not
    contain it (a possible false red), never a file wrongly dropped from the graded set.
    """
    parts = repo_relative.split("/")
    for raw in dockerignore_patterns():
        if raw.startswith("!"):
            continue
        pattern = raw.strip("/")
        bare = pattern.removeprefix("**/")
        if fnmatch.fnmatch(repo_relative, pattern) or fnmatch.fnmatch(
            repo_relative, f"{pattern}/*"
        ):
            return True
        if any(fnmatch.fnmatch(part, bare) for part in parts):
            return True
    return False


def shipped_sources(spec: ImageSpec) -> dict[str, str]:
    """``{repo-relative .py file: its /app path}`` for every module the image ships.

    ``**/tests/**`` and ``conftest.py`` are excluded. They ARE copied — ``COPY
    proxyshop_support/`` takes this very file into four of the five images — but a test
    module's imports are not the artifact's runtime closure and grading them would make
    this file's own imports part of what it grades.

    ``.dockerignore`` is applied, so the graded set is the build context's view rather than
    the working tree's.
    """
    found: dict[str, str] = {}
    for source, _destination in _real_copies(spec):
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
            if path.name == "conftest.py" or is_dockerignored(str(relative)):
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
    """One import of a package in a shipped module, and whether it runs at import time."""

    module_path: str
    lineno: int
    col_offset: int
    statement: str
    package: str
    #: True when the import executes as the module is imported, i.e. it is FATAL if it
    #: fails. For a statement that is column 0; for a dynamic call it is "not lexically
    #: inside a function", which is the same question asked of a construct that has no
    #: meaningful column.
    module_scope: bool
    #: ``"statement"`` for ``import``/``from``, ``"dynamic"`` for ``import_module("x")``.
    kind: str = "statement"

    def __str__(self) -> str:
        where = "module-scope" if self.module_scope else f"indented col {self.col_offset}"
        tag = "" if self.kind == "statement" else f" [{self.kind}]"
        return f"{self.module_path}:{self.lineno} ({where}){tag} {self.statement}"


def _dynamic_import_target(call: ast.Call) -> str | None:
    """The constant module name in ``importlib.import_module("x")`` / ``__import__("x")``."""
    func = call.func
    name = (
        func.attr
        if isinstance(func, ast.Attribute)
        else func.id
        if isinstance(func, ast.Name)
        else None
    )
    if name not in {"import_module", "__import__"} or not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def import_sites(module_path: str, packages: frozenset[str]) -> list[ImportSite]:
    """Every import of one of ``packages`` in a shipped module.

    AST rather than grep, and the difference is the whole point: a package name inside a
    docstring, a comment or a string constant is not an import, and this repo's modules are
    heavily documented with example import lines a text scan counts as real. Relative
    imports (``from .criteria import ...``) are skipped — they cannot name a top-level
    package.

    ``importlib.import_module("x")`` and ``__import__("x")`` with a CONSTANT argument are
    counted too. Without that, T-300 could be closed by rewriting
    ``apps/buyer/svc/src/intent/routes.py:99`` as ``importlib.import_module("llm")``: the
    package would still be missing from the image, the feature would still be degraded, and
    both halves of this file would have gone quiet — the static half because there is no
    import node, the runtime half because the call is lazy. A non-constant argument (the
    ``for spelling in (...)`` loop in ``apps/trust/src/verification/__init__.py:46-48``)
    remains invisible; that limitation is stated in the module docstring.
    """
    path = REPO_ROOT / module_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[ImportSite] = []

    def record(node: ast.AST, package: str, module_scope: bool, kind: str) -> None:
        found.append(
            ImportSite(
                module_path=module_path,
                lineno=getattr(node, "lineno", 0),
                col_offset=getattr(node, "col_offset", 0),
                statement=ast.unparse(node)[:88],
                package=package,
                module_scope=module_scope,
                kind=kind,
            )
        )

    def walk(node: ast.AST, inside_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    top = alias.name.split(".")[0]
                    if top in packages:
                        record(child, top, child.col_offset == 0, "statement")
            elif isinstance(child, ast.ImportFrom) and not child.level:
                top = (child.module or "").split(".")[0]
                if top in packages:
                    record(child, top, child.col_offset == 0, "statement")
            elif isinstance(child, ast.Call):
                target = _dynamic_import_target(child)
                if target and target.split(".")[0] in packages:
                    record(child, target.split(".")[0], not inside_function, "dynamic")
            nested = inside_function or isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
            )
            walk(child, nested)

    walk(tree, False)
    return found


@cache
def unwired_first_party_imports(label: str) -> dict[str, tuple[ImportSite, ...]]:
    """``{package: sites}`` for every first-party package an image imports but cannot resolve.

    This is check (a) in full. No subprocess, no filesystem materialisation, no assumption
    about what Docker does with a symlink: the Dockerfile's text and the shipped tree's ASTs
    are the only inputs.
    """
    return _unwired_for(images()[label])


def _static_failure_report(label: str) -> str:
    """Why this image is unsound, package by package.

    An entry with NO import sites is not an empty report, it is the T-334 shape: the image
    puts the name on a ``sys.path`` root and cannot back the claim up, and the module that
    used to import it went out of the image with the very ``COPY`` that is missing. It is
    spelled out rather than printed as "0 sites and 0 sites", which read like a bug in the
    report and invited exactly the shrug that kept this class of defect alive.
    """
    return _failure_report_for(images()[label], label, unwired_first_party_imports(label))


def _failure_report_for(
    spec: ImageSpec, label: str, unwired: dict[str, tuple[ImportSite, ...]]
) -> str:
    """The report proper, over a spec and a report rather than a repo label.

    Split out so the claim-only branch below can be RENDERED by a test. Every real image is
    clean, so that branch would otherwise first run on the day it actually matters — and a
    reporting path that has never executed is how a checker crashes, or says nothing useful,
    at exactly the moment it finally has something to say.
    """
    claims = path_root_claims(spec)
    lines = [f"{label}: the image cannot resolve first-party packages it ships or claims."]
    for package, sites in unwired.items():
        _, why = package_wiring(spec, package)
        fatal = [s for s in sites if s.module_scope]
        lazy = [s for s in sites if not s.module_scope]
        if sites:
            lines.append(
                f"  {package!r}: {why} — {len(fatal)} module-scope site(s) (fatal at import) "
                f"and {len(lazy)} indented site(s) (lazy, possibly swallowed)"
            )
            lines.extend(f"      {site}" for site in sites)
        elif package in claims:
            lines.append(
                f"  {package!r}: {why} — the build puts {package!r} on this image's "
                f"sys.path at {claims[package]}, so the image CLAIMS to provide it; no "
                f"module it still ships imports it, which is what the missing COPY took "
                f"away rather than evidence that nothing needs it"
            )
        else:
            # Unreachable from _unwired_for, which only adds a site-less entry for a
            # package it found in path_root_claims. Rendered rather than raised anyway:
            # this took a KeyError under a mutation of _unwired_for, and a report that
            # dies while explaining a failure destroys the report for every OTHER package
            # in it. The report is the last thing that should have a way to go quiet.
            lines.append(
                f"  {package!r}: {why} — reported with no import site and no sys.path "
                f"claim, which this checker has no rule that produces: treat the REPORT as "
                f"suspect before acting on this line"
            )
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
    return materialise_spec(images()[label], label)


def materialise_spec(spec: ImageSpec, key: str) -> Path:
    """The materialiser proper, usable on a synthesised spec as well as a repo image."""
    root = Path(tempfile.mkdtemp(prefix="artifact-copyset-"))
    _TEMP_TREES[key] = root
    app = root / "app"
    app.mkdir()
    workdir = spec.workdir.rstrip("/") or "/app"

    def _under_workdir(image_path: str) -> Path:
        relative = posixpath.relpath(image_path, workdir)
        return app if relative == "." else app / relative

    def _ignored(directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        dropped = set()
        for name in names:
            try:
                relative = str((base / name).relative_to(REPO_ROOT))
            except ValueError:  # pragma: no cover - source outside the repo
                continue
            if is_dockerignored(relative):
                dropped.add(name)
        return dropped

    for source, destination in _real_copies(spec):
        src = REPO_ROOT / source.rstrip("/")
        dst = _under_workdir(destination.rstrip("/"))
        if src.is_file() and destination.endswith("/"):
            dst = dst / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True, ignore=_ignored)
        elif src.is_file():
            shutil.copy2(src, dst)
    for target, link in spec.links:
        spot = _under_workdir(link)
        spot.parent.mkdir(parents=True, exist_ok=True)
        if not spot.is_symlink() and not spot.exists():
            spot.symlink_to(target)
    return app


# -------------------------------------------------------------------------------------
# The image's dependency layer. Without this the probe runs against the whole dev venv.
# -------------------------------------------------------------------------------------


def _canonical_distribution(name: str) -> str:
    """PEP 503 normalisation, so ``email-validator`` and ``email_validator`` are one name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned_distributions(spec: ImageSpec) -> dict[str, frozenset[str]]:
    """``{distribution: requested extras}`` from the Dockerfile's ``pip install`` layer."""
    seeds: dict[str, set[str]] = {}
    for requirement in spec.pip_requirements:
        match = re.match(r"([A-Za-z0-9._-]+)(?:\[([^\]]*)\])?", requirement)
        if not match:
            continue
        extras = {e.strip() for e in (match.group(2) or "").split(",") if e.strip()}
        seeds.setdefault(_canonical_distribution(match.group(1)), set()).update(extras)
    return {name: frozenset(extras) for name, extras in seeds.items()}


@cache
def pip_distribution_closure(label: str) -> frozenset[str]:
    """The transitive distribution closure the image's ``pip install`` layer would resolve.

    ``fastapi`` drags in ``starlette``; ``pydantic`` drags in ``pydantic_core``,
    ``annotated_types`` and ``typing_extensions``; ``jsonschema[format]`` drags in
    ``referencing`` and the format validators. Those are legitimately importable in the
    image and must not be reported as missing, so the closure is computed rather than
    guessed at from the nine names the Dockerfile lists.

    Extras are honoured for the SEED distributions only (``uvicorn[standard]`` pulls its
    standard extra; a transitive dependency's optional extras are not installed). Any other
    marker — ``python_version``, ``sys_platform`` — is treated as satisfied, which
    over-approximates the closure. Over-approximating is the safe direction here: it can
    only fail to report a defect this repo does not have, never invent one it does.
    """
    from importlib.metadata import PackageNotFoundError, requires

    seeds = pinned_distributions(images()[label])
    seen: set[str] = set()
    queue: list[tuple[str, frozenset[str]]] = list(seeds.items())
    while queue:
        name, extras = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            requirements = requires(name) or []
        except PackageNotFoundError:
            continue
        for requirement in requirements:
            base, _, marker = requirement.partition(";")
            dependency = re.split(r"[<>=!~\[( ]", base.strip())[0].strip()
            if not dependency:
                continue
            wanted = re.findall(r"extra\s*==\s*[\"']([^\"']+)[\"']", marker)
            if wanted and not (set(wanted) & set(extras)):
                continue
            queue.append((_canonical_distribution(dependency), frozenset()))
    return frozenset(seen)


@lru_cache(maxsize=1)
def _import_names_by_distribution() -> dict[str, frozenset[str]]:
    from importlib.metadata import packages_distributions

    table: dict[str, set[str]] = {}
    for import_name, distributions in packages_distributions().items():
        for distribution in distributions:
            table.setdefault(_canonical_distribution(distribution), set()).add(import_name)
    return {name: frozenset(imports) for name, imports in table.items()}


@cache
def image_third_party_imports(label: str) -> frozenset[str]:
    """Top-level import names the image's own pip layer makes available."""
    table = _import_names_by_distribution()
    names: set[str] = set()
    for distribution in pip_distribution_closure(label):
        names |= table.get(distribution, frozenset())
    return frozenset(names)


def _dotted_names(
    app: Path, path_roots: tuple[str, ...], workdir: str = "/app"
) -> dict[str, set[str]]:
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
    home = workdir.rstrip("/") or "/app"
    for root in path_roots:
        stripped = root.rstrip("/")
        base = app if stripped == home else app / posixpath.relpath(stripped, home)
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


#: Installed into every probe. The venv's ``site-packages`` has to be on the path — the
#: image ``pip install``s real wheels and without them every module importing ``fastapi``
#: would fail for a reason the image does not have — but the venv holds the whole DEV set,
#: which is far larger than any image's nine pinned runtime wheels. Left alone, the probe
#: reports a module that does ``import pytest`` at column 0 as importable, because pytest
#: is right there in the venv and nowhere in the image. This finder is what makes the
#: difference visible: for a TOP-LEVEL name that is neither stdlib nor in the image's own
#: pip closure, it asks the ordinary finders where the module would come from, and refuses
#: it if the answer is site-packages. Modules that resolve inside the container-shaped tree
#: are untouched, so first-party code is unaffected.
_PIP_LAYER_WALL = """
import sys
_ALLOWED = set(json.loads(sys.argv[2]))
_SITE = sys.argv[3]

class _PipLayerOnly:
    def find_spec(self, fullname, path=None, target=None):
        if path is not None or fullname in sys.builtin_module_names:
            return None
        if fullname in _ALLOWED or fullname in sys.stdlib_module_names:
            return None
        for finder in list(sys.meta_path):
            if finder is self:
                continue
            try:
                spec = finder.find_spec(fullname, path, target)
            except Exception:
                continue
            if spec is None:
                continue
            origin = spec.origin or ""
            locations = list(getattr(spec, "submodule_search_locations", None) or [])
            where = origin if origin not in ("", "namespace", "built-in", "frozen") else (
                locations[0] if locations else ""
            )
            if where.startswith(_SITE):
                raise ModuleNotFoundError(
                    "No module named %r — present in the dev venv, absent from this image's "
                    "pip install layer" % fullname,
                    name=fullname,
                )
            return spec
        return None

sys.meta_path.insert(0, _PipLayerOnly())
"""

#: The probe's PRIVATE report channel, installed into every probe ahead of the pip wall.
#:
#: This used to be ``stdout``, tagged ``SYSPATH ``/``RESULT ``, and that was a live
#: false-green hazard rather than a theoretical one. ``stdout`` is SHARED with the code
#: under test: the probe imports ~513 shipped modules in-process, any one of which may
#: print at import time. The reader took the FIRST matching line, and the probe's own
#: ``RESULT`` is emitted LAST — so one module printing ``RESULT {"_": {"file": null}}``
#: replaced the entire outcome dictionary, every genuinely broken module then read clean
#: (the reader treated a name missing from the outcome as a successful import), and the
#: image reported 0 broken with no error anywhere. Measured on a scratch copy of this
#: tree: a real defect that the checker reported as ten broken merchant modules —
#: including ``merchant_svc.main``, the image's own entrypoint — became ``0 broken`` when
#: a single ``print`` line was added to one unrelated shipped module.
#:
#: Reading the LAST tagged line instead of the first would have fixed that one instance
#: and left the channel shared. This writes the report to a file the parent names, and
#: ``pop``s the variable naming it out of ``os.environ`` before a single shipped module is
#: imported. No line a module writes to stdout is read by this gate at all; stdout is kept
#: only as diagnostic text for the assertion messages (:meth:`ProbeRun.diagnosis`).
#:
#: **What that does and does not buy, stated at the strength the evidence supports — which
#: is weaker than the first version of this paragraph claimed.** It removes the ACCIDENT
#: entirely: no amount of printing, on stdout or stderr, can now alter, shorten or replace
#: the report, which is the failure this repair exists for — a debugging ``print`` left in
#: any of the 515 shipped modules. It does not, and cannot, make the measurement proof
#: against a shipped module that is deliberately trying to defeat it: that module runs in
#: the probe's own process. This paragraph used to hedge that with three "could"s. All
#: three have since been MEASURED on a scratch copy of this tree at ``c5d2a15``, so they
#: are facts, and none of them needs ``builtins.open``:
#:
#: * the channel path is not a secret. ``os.environ.pop`` hides it from the ENVIRONMENT,
#:   not from the process: the probe keeps it in ``__main__._CHANNEL``, and one line in a
#:   shipped module (``sys.modules["__main__"]._CHANNEL``) prints it. The file is also
#:   globbable — ``/private/var/folders/*/*/T/copyset-probe-*.json`` found it.
#: * the graded name list is not a secret either. It is ``sys.argv[1]``, so a forged report
#:   can be exactly set-equal to what was asked.
#: * ``_emit`` itself is reachable: rebinding ``__main__._emit`` replaces the finished
#:   report while the probe still exits 0.
#:
#: **So the claim that used to close this paragraph is withdrawn.** "Every name must come
#: back with a verdict and every verdict must grade something" is NOT the defence: an
#: adversarial review satisfied both conditions three separate ways while hiding a merchant
#: image that cannot start. What actually grades a report is four checks, split across
#: :func:`run_in_image` and :func:`image_import_report`, each of which closed one measured
#: hole:
#:
#: 1. the probe subprocess must exit 0. A module that wrote a complete forged report and
#:    then ``chmod 444``'d the channel killed the probe's own final ``_emit`` with
#:    ``PermissionError`` and still reported ``0 broken of 79``.
#: 2. every name asked for must come back with a verdict; and
#: 3. every verdict must carry a non-empty ``error`` string or a non-empty ``file`` origin.
#:    ``{"file": None}`` satisfied (2), passed a malformed check that only asked whether one
#:    of the two KEYS was present, and was then skipped by (4) on ``origin is None``.
#: 4. every clean verdict's origin must resolve INSIDE the container-shaped tree, whatever
#:    the name's top-level spelling. (4) used to run only for names whose first segment is a
#:    ``.pkgroot`` package, which left 291 of the 914 verdicts on a clean tree (``apps.*``
#:    223, ``services.*`` 52, ``fixtures.*`` 16) never examined — and those are the SECOND
#:    name of files that also have a first-party one, so fabricating an origin for them
#:    alone hid four broken ``envelope/*`` modules behind their honest ``merchant_svc.*``
#:    twins.
#:
#: What still defeats all four, said plainly so nobody reads this as a security boundary: a
#: shipped module that writes a complete, set-equal report whose origins are real paths
#: inside the tree, and then lets the probe exit 0. No in-process probe can be hardened
#: against the code it imports. What the four buy is that forging a green now takes
#: deliberate, legible work in a shipped module instead of one stray line.
#:
#: ``_emit`` is called twice on purpose. The first call, before any import, records that
#: the probe reached its first statement; the second replaces it with the finished report.
#: That keeps the outcomes distinguishable — never started (no file at all), started and
#: died mid-import (file, but no report key), finished (both) — which a single write at
#: the end would collapse into one silent "nothing to grade". A fourth state, the channel
#: existing and EMPTY, is refused explicitly in :func:`run_in_image` rather than folded
#: into "never started", because a module that does ``open(_CHANNEL, "w")`` and nothing
#: else produces it.
_PROBE_CHANNEL = """
import json as _json, os as _os

_CHANNEL = _os.environ.pop("PROXYSHOP_PROBE_OUT")

def _emit(**payload):
    with open(_CHANNEL, "w") as _fh:
        _json.dump(payload, _fh)
        _fh.flush()
        _os.fsync(_fh.fileno())
"""

_IMPORT_PROBE = (
    textwrap.dedent(
        """
        import importlib, json, sys
        """
    )
    + _PROBE_CHANNEL
    + _PIP_LAYER_WALL
    + textwrap.dedent(
        """
        _sys_path = list(sys.path)
        _emit(sys_path=_sys_path)
        outcome = {}
        for name in json.loads(sys.argv[1]):
            try:
                module = importlib.import_module(name)
            except BaseException as exc:
                outcome[name] = {"error": f"{type(exc).__name__}: {exc}"}
            else:
                outcome[name] = {"file": getattr(module, "__file__", None)}
        _emit(sys_path=_sys_path, outcome=outcome)
        """
    )
)


@dataclass(frozen=True)
class ProbeRun:
    """One probe subprocess, and the payload it wrote on its own private channel.

    ``payload is None`` means there was no channel file at all — the probe died before its
    first statement. It never means "the probe found nothing", and no caller may read it
    that way: a zero that is really an absence is the whole defect class this file exists to
    find, and :data:`_PROBE_CHANNEL` records why it was also a defect IN this file. The
    third reading of that ``None`` — a channel file that exists and is empty, which is what
    a module doing ``open(_CHANNEL, "w")`` leaves behind — is ruled out inside
    :func:`run_in_image` rather than left for a caller to notice.

    ``process.returncode`` is checked in :func:`run_in_image` before any payload is
    returned, so a ``ProbeRun`` that reaches a caller came from a subprocess that exited 0.
    It used to be read in :meth:`diagnosis` alone, i.e. in assertion PROSE and never in a
    branch, which is exactly how a forged report that outlived the process meant to write it
    got graded as ``0 broken of 79``.
    """

    label: str
    process: subprocess.CompletedProcess[str]
    payload: dict[str, Any] | None

    def diagnosis(self) -> str:
        """Assertion-message text for a missing or partial payload.

        The probe's stdout appears here and ONLY here. It is evidence about what the
        subprocess did; it is never parsed, because the modules under test can write to it.
        """
        parts = [f"exit status {self.process.returncode}"]
        err = self.process.stderr.strip()
        if err:
            parts.append(f"stderr tail:\n{err[-1500:]}")
        out = self.process.stdout.strip()
        if out:
            parts.append(
                "stdout tail (NOT parsed by this gate — shipped modules can write to it):\n"
                + out[-800:]
            )
        return "\n".join(parts)


def _describe_channel(written: str) -> str:
    """What the private channel held, for an assertion message that has to be actionable.

    Raw bytes are useless here: the report opens with the probe's ``sys_path``, so the first
    300 characters of a 677-byte forgery are all path and the interesting half — how many
    verdicts it carried, and for what — is off the end. A report that LOOKS complete is
    exactly what the measured ``chmod 444`` attack leaves behind, so saying so is the whole
    point of the message.
    """
    if not written:
        return "0 bytes"
    try:
        parsed: object = json.loads(written)
    except json.JSONDecodeError:
        return f"{len(written)} bytes that are not JSON: {written[:200]!r}"
    if not isinstance(parsed, dict):
        return f"{len(written)} bytes holding {type(parsed).__name__}, not an object"
    described = f"{len(written)} bytes, an object with keys {sorted(parsed)}"
    verdicts = parsed.get("outcome")
    if isinstance(verdicts, dict):
        described += f" and {len(verdicts)} verdicts, e.g. {sorted(verdicts)[:5]}"
    return described


def run_in_image(label: str, code: str, *argv: str) -> ProbeRun:
    """Run ``code`` against the built tree with the image's ``PYTHONPATH`` and nothing else.

    ``-S`` is load-bearing: without it ``site`` processes ``_proxyshop.pth`` and the LIVE
    CHECKOUT lands on ``sys.path`` whatever ``PYTHONPATH`` says, at which point the probe
    grades the repo rather than the artifact. Measured directly: with ``-S``,
    ``import exchange`` inside the merchant tree raises ``ModuleNotFoundError``; without it,
    the same probe resolves it to ``<checkout>/.pkgroot/exchange/__init__.py`` and the image
    reports zero broken modules. ``-E``/``-I`` cannot be used — they discard ``PYTHONPATH``,
    which is the only thing pointing at the tree under test.

    Every probe is handed the image's allowed third-party import names and the venv's
    ``site-packages`` prefix as ``argv[2]``/``argv[3]``, which is what :data:`_PIP_LAYER_WALL`
    needs.

    The report comes back over :data:`_PROBE_CHANNEL` — a file this function creates the
    NAME of, deletes before the run, and removes afterwards. Nothing on the subprocess's
    stdout is parsed, at any precedence and as no kind of fallback: a reader re-added even
    as a last resort is caught by
    ``test_artifact_copyset_probe_channel.py::test_stdout_is_not_read_even_as_a_fallback_when_the_channel_has_no_outcome``.
    See :data:`_PROBE_CHANNEL` for the measured false-green that made a shared channel
    unusable here.

    **The deletion does not mean "only the probe can bring the channel into existence",
    which is what this docstring used to say.** Every one of the shipped modules runs inside
    the probe process, so any of them can create or overwrite that file; one of them
    globbing it, writing a complete forged report and then ``chmod 444``-ing it was measured
    on a scratch copy of this tree. What the deletion buys is narrower and still worth
    having: a channel file that exists afterwards was created by SOMETHING in the probe
    process, so "no file" and "an empty file" are distinguishable, and both are refused
    here rather than handed to a caller as ``payload is None``.

    Two things are therefore checked before anything on the channel is trusted, and both
    are checked HERE rather than in the callers, because the hole they close was a caller
    that did not think to look: the subprocess's exit status, and whether the channel exists
    but is empty. ``apps/trust/tests/test_repro_open_tickets.py:492`` already asserts the
    exit status of its own probe; that precedent had simply not been followed here.
    """
    app = materialised_image(label)
    spec = images()[label]
    workdir = spec.workdir.rstrip("/") or "/app"
    roots = [
        str(app if r.rstrip("/") == workdir else app / posixpath.relpath(r.rstrip("/"), workdir))
        for r in spec.path_roots
    ]
    allowed = json.dumps(sorted(image_third_party_imports(label)))

    handle, channel = tempfile.mkstemp(prefix="copyset-probe-", suffix=".json")
    os.close(handle)
    # Deleted, not truncated: an empty file that already exists would be indistinguishable
    # from a probe that started and wrote nothing, and this file does not get to guess.
    os.unlink(channel)
    try:
        process = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-S", "-c", code, *(argv or ("",)), allowed, str(SITE_PACKAGES)],
            cwd=str(app),
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": os.pathsep.join([*roots, str(SITE_PACKAGES)]),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PROXYSHOP_WORKER": os.environ.get("PROXYSHOP_WORKER", "0"),
                "PROXYSHOP_PROBE_OUT": channel,
            },
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        existed = os.path.exists(channel)
        written = Path(channel).read_text() if existed else ""

        # NOTHING the probe left behind may be read before its exit status is. `check=False`
        # plus a `returncode` that was read in exactly one place — `ProbeRun.diagnosis()`,
        # i.e. assertion PROSE and never a branch — was a hole with a four-line recipe,
        # measured on a scratch copy of this tree at c5d2a15: a shipped module globbed this
        # channel, wrote a complete report whose origins were `os.getcwd()`-relative (cwd IS
        # the container-shaped tree, so they validate), and then `os.chmod(p, 0o444)`. The
        # probe's own final `_emit` died with `PermissionError: [Errno 13] Permission
        # denied`, the forged report survived on the channel, and the gate reported
        # `0 broken of 79` at exit 0 with the deliberately poisoned module broken throughout.
        # The sibling gate `apps/trust/tests/test_repro_open_tickets.py:492` already asserts
        # this; the precedent was simply not followed here.
        assert process.returncode == 0, (
            f"{label}: the probe subprocess exited {process.returncode}, so NOTHING it left "
            f"on the private channel may be graded — a non-zero exit means the report is at "
            f"best partial and at worst a forgery that outlived the process meant to write "
            f"it. A report that looks complete is exactly what that attack leaves behind, "
            f"so what the channel held is stated rather than assumed: "
            f"{_describe_channel(written)}\n{ProbeRun(label, process, None).diagnosis()}"
        )

        # An empty channel file is NOT "the probe never started". `os.unlink` above deletes
        # the name specifically so those two are distinguishable, and `if written:` threw
        # that signal away again: a module that does `open(_CHANNEL, "w")` and nothing else
        # left a 0-byte file and a `payload is None` that every caller reads as "never
        # reached its first statement". Both readings of that zero are named here and one is
        # ruled out by the file's own existence.
        if existed and not written:
            raise AssertionError(
                f"{label}: the probe's private channel EXISTS and is empty. This function "
                f"deletes the channel before the run, so an empty file means something in "
                f"the probe process created it and wrote nothing — not that the probe never "
                f"started, which is the other reading of a zero here and the one `payload "
                f"is None` would have handed to every caller.\n"
                f"{ProbeRun(label, process, None).diagnosis()}"
            )

        payload: dict[str, Any] | None = None
        if written:
            try:
                payload = json.loads(written)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"{label}: the probe's private channel holds {len(written)} bytes that "
                    f"are not JSON, so this run cannot be graded in EITHER direction and "
                    f"must not be read as clean: {exc}\n{written[:400]}"
                ) from exc
        if payload is not None and not isinstance(payload, dict):
            raise AssertionError(
                f"{label}: the probe's private channel holds {type(payload).__name__}, not "
                f"the object every caller here indexes: {written[:400]}"
            )
        return ProbeRun(label, process, payload)
    finally:
        Path(channel).unlink(missing_ok=True)


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


def own_module_failures(label: str) -> dict[str, str]:
    """The broken shipped modules that belong to the image's OWN package.

    Every service image also ships ``packages/contracts`` and ``proxyshop_support``. A
    defect in one of those is a defect in ALL FOUR images at once and belongs to the
    whole-image T-301 gates, not to a per-service ticket: a T-299 gate that goes red because
    a shared helper imports ``pytest`` is red for somebody else's reason, and a gate that
    fails before reaching the thing it exists to grade is the specific failure
    ``apps/trust/tests/test_repro_open_tickets.py`` records at its ``SHIM`` assertion.

    "Own package" is derived from the ``CMD`` entrypoint's top-level name, not written down.
    """
    spec = images()[label]
    report = image_import_report(label)
    provider = first_party_packages().get((spec.entrypoint or "").split(".")[0])
    if provider is None:
        return dict(report.broken)
    prefix = provider.rstrip("/") + "/"
    return {path: why for path, why in report.broken.items() if path.startswith(prefix)}


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


def verdict_error(entry: object) -> str | None:
    """The failure a probe verdict records, or ``None`` if it does not record one.

    One place decides what an ``error`` verdict IS, because three separate places used to
    decide it differently and the disagreements were exploitable. ``"error" in entry`` was
    the broken-module loop's rule, ``{"error", "file"} & set(entry)`` was the malformed
    check's, and ``entry.get("file") is None`` was the origin guard's — so ``{"error":
    None}`` counted as a failure whose message was ``None``, and ``{"file": None}`` counted
    as a clean import that the origin guard then declined to examine.

    A verdict records a failure only when ``error`` is a NON-EMPTY string. Measured on a
    clean tree across all nine images: 914 verdicts, zero of them with a null, empty or
    non-string ``error``, so this costs nothing here and refuses a forgery.
    """
    if not isinstance(entry, dict):
        return None
    error = entry.get("error")
    return error if isinstance(error, str) and error else None


def verdict_origin(entry: object) -> str | None:
    """The ``__file__`` a probe verdict claims the module was imported from.

    ``None`` when the verdict does not claim one — which, paired with
    :func:`verdict_error`, is what makes a verdict MALFORMED rather than clean.
    ``{"file": None}`` is the exact shape of the historical stdout poison
    (``print('RESULT {"_": {"file": null}}')``) and it used to pass every check in
    :func:`image_import_report`. Measured on a clean tree across all nine images: 914
    verdicts, zero with a null or empty ``file``, so requiring a non-empty string costs
    nothing.
    """
    if not isinstance(entry, dict):
        return None
    origin = entry.get("file")
    return origin if isinstance(origin, str) and origin else None


@cache
def image_import_report(label: str) -> ImportReport:
    """Check (b): import every shipped module inside the container-shaped tree."""
    spec = images()[label]
    app = materialised_image(label)
    by_file = _dotted_names(app, spec.path_roots, spec.workdir)

    shipped = shipped_sources(spec)
    workdir = spec.workdir.rstrip("/") or "/app"
    wanted: dict[str, set[str]] = {}
    unreachable: list[str] = []
    for repo_relative, landed in shipped.items():
        built = app / posixpath.relpath(landed, workdir)
        names = by_file.get(str(built.resolve()))
        if names:
            wanted[repo_relative] = names
        else:
            unreachable.append(f"{repo_relative} -> {landed}")

    # A shipped module with no dotted name is not "fine", it is UNGRADED. Every one of the
    # earlier silent-drop bugs in this file — first-COPY-wins, a mis-parsed destination, a
    # file landed where the path roots cannot see it — showed up here as a module quietly
    # leaving the denominator. Making it loud is the only way that stays true.
    assert unreachable == [], (
        f"{label}: {len(unreachable)} of {len(shipped)} shipped modules have no importable "
        f"dotted name in the built tree, so they were graded by NOTHING. That is a defect in "
        f"this checker (a mis-parsed COPY destination or path root), not in the image:\n"
        + "\n".join(f"    {line}" for line in unreachable[:20])
    )

    every_name = sorted({name for names in wanted.values() for name in names})
    run = run_in_image(label, _IMPORT_PROBE, json.dumps(every_name))

    assert run.payload is not None, (
        f"{label}: the container-shaped probe never reached its first statement, so this "
        f"gate measured nothing at all:\n{run.diagnosis()}"
    )
    assert "sys_path" in run.payload, (
        f"{label}: the probe wrote a report with no sys.path in it, so the leak guard below "
        f"has nothing to check and this measurement cannot be trusted:\n{run.diagnosis()}"
    )
    leaked = _leaked_paths(run.payload["sys_path"])
    assert leaked == [], (
        f"{label}: the probe can see the live checkout on sys.path ({leaked}), so it can "
        f"import packages the image does not ship and this measurement is worthless. "
        f"{SITE_PACKAGES}/_proxyshop.pth puts {REPO_ROOT} and {PKGROOT} on sys.path at "
        f"interpreter startup regardless of PYTHONPATH; `-S` is what keeps them off."
    )

    assert "outcome" in run.payload, (
        f"{label}: the probe started but did not finish; it graded nothing:\n{run.diagnosis()}"
    )
    outcome: dict[str, dict[str, Any]] = run.payload["outcome"]

    # EVERY name handed to the probe must come back with a verdict. This used to be
    # `outcome.get(n, {})` at the bottom of this function, which read a name the probe never
    # mentioned as a CLEAN import — so any accident that shortened the outcome dictionary
    # (the stray-stdout hijack :data:`_PROBE_CHANNEL` records, a probe killed part-way, a
    # future edit that filters the loop) turned every broken module in the image green at
    # once. "Absent" is now the loudest thing this report can say, not the quietest.
    reported, asked = set(outcome), set(every_name)
    assert reported == asked, (
        f"{label}: the probe was asked to import {len(asked)} module names and returned a "
        f"verdict on {len(reported)}. A name with no verdict is UNGRADED, not clean, so this "
        f"report is refused rather than read.\n"
        f"    no verdict returned ({len(asked - reported)}): {sorted(asked - reported)[:20]}\n"
        f"    verdicts nobody asked for ({len(reported - asked)}): {sorted(reported - asked)[:20]}\n"
        f"{run.diagnosis()}"
    )
    # A verdict has to GRADE something: a non-empty error string, or a non-empty origin.
    # `{"error", "file"} & set(entry)` only asked whether one of the KEYS was present, so
    # `{"file": None}` — byte-for-byte the historical stdout poison — passed here and was
    # then skipped by the origin guard below on `origin is None`. Measured on a scratch
    # copy of this tree at c5d2a15: a shipped module that rebinds `__main__._emit` and
    # writes every verdict as `{"file": None}` took the merchant image from 10 broken of 79
    # to `0 broken of 79` at exit 0, with the deliberately poisoned module still broken.
    malformed = sorted(
        name
        for name, entry in outcome.items()
        if verdict_error(entry) is None and verdict_origin(entry) is None
    )
    assert malformed == [], (
        f"{label}: {len(malformed)} of {len(outcome)} verdicts grade nothing — a verdict "
        f"must carry either a non-empty 'error' string or a non-empty 'file' origin, and "
        f"these carry neither, so the loop below would read every one of them as a "
        f"successful import. `{{'file': None}}` is the exact shape of the stdout poison "
        f"this channel was rebuilt to defeat and it used to pass this check: "
        f"{malformed[:20]}\n{run.diagnosis()}"
    )

    # EVERY clean verdict's origin must resolve inside the container-shaped tree, whatever
    # the name's top-level spelling. This guard used to read
    #
    #     if origin is None or name.split(".")[0] not in packages: continue
    #
    # with `packages` the `.pkgroot` names plus `packages`/`proxyshop_support` — so it only
    # ever inspected FIRST-PARTY top-levels. Measured on a clean tree: that skipped 291 of
    # the 914 verdicts (`apps.*` 223, `services.*` 52, `fixtures.*` 16), and those are the
    # SECOND dotted name of files that also have a first-party one. A file counts as broken
    # only when every one of its names fails, so fabricating an origin for the unexamined
    # spelling alone is enough to hide it behind its own honest twin: four lines appended to
    # `apps/merchant/svc/src/http_limits.py` that rebind `__main__.outcome` to
    # `{n: {"file": "/app/fake.py"} for n in json.loads(sys.argv[1])}` took the merchant
    # image from 10 broken of 79 to 6 at exit 0, hiding all four `envelope/*` modules
    # including the deliberately poisoned one. `apps` is not a `.pkgroot` name, so
    # `apps.merchant.svc.src.envelope.digest` was never examined while
    # `merchant_svc.envelope.digest` was.
    #
    # There is no legitimate exception to widen this back for: every name handed to the
    # probe was derived from a `.py` file INSIDE the built tree by `_dotted_names`, so an
    # origin anywhere else is a measurement error by construction. Measured on a clean tree:
    # 914 verdicts, zero with an origin outside the tree.
    outside = [
        f"{name} <- {verdict_origin(entry)}"
        for name, entry in sorted(outcome.items())
        if verdict_error(entry) is None
        and app.resolve() not in Path(verdict_origin(entry) or "").resolve().parents
    ]
    assert outside == [], (
        f"{label}: {len(outside)} of {len(outcome)} verdicts name an origin OUTSIDE the "
        f"container-shaped tree at {app.resolve()}, so they grade something that is not the "
        f"artifact — the live checkout, or nothing at all. Every name handed to the probe "
        f"came from a file inside that tree, so there is no honest way to be here:\n"
        + "\n".join(f"    {line}" for line in outside[:20])
        + f"\n{run.diagnosis()}"
    )

    broken: dict[str, str] = {}
    for repo_relative, names in sorted(wanted.items()):
        # Direct indexing, never `.get(n, {})`: the set-equality assertion above has already
        # proved every name is present, and a KeyError here would be a loud bug in this file
        # rather than a silent clean bill of health for the image. `verdict_error` rather
        # than `"error" in ...` so this loop and the two assertions above cannot disagree
        # about what a failure is — `{"error": None}` used to land here as a failure whose
        # joined message was `None`.
        errors = {
            n: error for n in sorted(names) if (error := verdict_error(outcome[n])) is not None
        }
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

    **This control must not depend on any ticket still being open.** The first version of it
    read ``apps/merchant/Dockerfile`` and asserted the T-298 defect was present — so closing
    T-298 would have turned a green, non-xfail test RED, and the lane that fixed the bug
    would have been handed a broken build by the gate that was supposed to grade the fix.
    Everything below is therefore DERIVED from whatever the tree currently contains, and
    passes both while the defects are open and after every one of them is closed.

    The witness is a round trip on a package that is wired TODAY, whichever that is:

    1. pick any image and a first-party package it currently resolves and imports;
    2. delete that package's ``ln -s`` — the ``COPY`` stays, so the package is still in the
       image and simply not on ``PYTHONPATH``. The checker must now report it. This is the
       half of a repair that ``apps/trust/tests/test_repro_open_tickets.py`` records as
       necessary, and a checker that only looked at ``COPY`` lines would miss it;
    3. put the link back and the report must return to exactly what it was.

    Plus a materialisation witness for the ``COPY``-exists rule: a ``COPY`` naming a path
    the build context does not contain must never make a package count as shipped — that
    recipe (``COPY packages/llm/src/NOTHING_HERE`` plus a matching ``ln -s``) closed T-300
    while shipping nothing at all.
    """
    packages = frozenset(first_party_packages())

    # 1. a package some image resolves today AND imports — derived, never named here.
    witness: tuple[str, str, str] | None = None
    for label in sorted(images()):
        spec = images()[label]
        imported = {
            site.package
            for module_path in sorted(shipped_sources(spec))
            for site in import_sites(module_path, packages)
        }
        for package in sorted(imported):
            wired, _why = package_wiring(spec, package)
            links = [ln for ln in spec.links if ln[1].endswith(f"/{package}")]
            if wired and links:
                witness = (label, package, links[0][1])
                break
        if witness:
            break
    assert witness is not None, (
        "no image in this repo resolves a first-party package through an `ln -s` that its "
        "own shipped tree imports, so this control has nothing to flip. That is either a "
        "repo-wide change or a broken parser — either way the gates below prove nothing."
    )
    label, package, link_path = witness
    text = (REPO_ROOT / label).read_text(encoding="utf-8")
    baseline = _unwired_for(parse_dockerfile_text(text, f"{label}/baseline"))
    assert package not in baseline, (
        f"{label} was measured as resolving {package!r} and the static check disagrees — "
        f"the two halves of this checker do not agree with each other: {sorted(baseline)}"
    )

    # 2. remove the symlink that puts it on PYTHONPATH. Still COPY'd; no longer importable.
    without_link = "\n".join(
        line
        for line in text.splitlines()
        if not re.search(rf"\bln\s+-s\S*\s+\S+\s+{re.escape(link_path)}\b", line)
    )
    assert without_link != text, f"the control could not remove the `ln -s ... {link_path}` line"
    regressed = _unwired_for(parse_dockerfile_text(without_link, f"{label}/regressed"))
    assert package in regressed, (
        f"the static checker is BLIND to a package that is COPY'd but has nothing putting it "
        f"on PYTHONPATH. Removing `ln -s ... {link_path}` from {label} left {package!r} "
        f"unreported, so it cannot tell a real repair from half of one. Reported: "
        f"{sorted(regressed)}"
    )

    # 3. put it back: the checker must see the repair, not just the break.
    repaired = _unwired_for(parse_dockerfile_text(text, f"{label}/repaired"))
    assert repaired == baseline and package not in repaired, (
        f"the static checker CANNOT SEE its own repair: restoring `ln -s ... {link_path}` "
        f"to {label} did not return the report to its baseline. This is the exact defect "
        f"that left the trust lane's image gate XPASSing against a live defect."
    )

    # 4. a COPY whose source does not exist ships nothing, whatever the symlink says.
    fabricated = (
        text.replace("ln -s ", "ln -s ", 1)
        + "\nCOPY packages/does-not-exist/src/ /app/packages/does-not-exist/src/\n"
    )
    invented = parse_dockerfile_text(fabricated, f"{label}/fabricated")
    assert copies_directory(invented, "packages/does-not-exist/src") is None, (
        "a COPY naming a path the build context does not contain was accepted as shipping "
        "it. That is a buildable-looking Dockerfile that ships nothing, and it used to be "
        "enough to close T-300."
    )


def _unwired_for(spec: ImageSpec) -> dict[str, tuple[ImportSite, ...]]:
    """The static check for ANY spec, including ones synthesised by the positive control.

    Two independent reasons to report a package, and the second one is T-334's repair:

    1. **A shipped module imports it** and ``wired`` says the image cannot resolve it. Every
       such import site is listed, so :func:`_static_failure_report` can separate the fatal
       module-scope ones from the lazy indented ones.
    2. **The image CLAIMS it** — :func:`path_root_claims` — and ``wired`` says the same
       thing. No import site is needed, and none is invented; the entry carries an empty
       site tuple, which the return contract has always allowed.

    Rule 1 alone is circular, which is what made this checker go quiet exactly when it
    mattered: a package's only importer is usually inside the very ``COPY`` that provides
    it, so deleting the ``COPY`` deletes the evidence that it was needed and the checker
    grades a smaller image and finds it consistent. The verdict was never missing — the
    ``wired`` line above computes it for EVERY first-party package, and rule 1 threw away
    the ones no survivor happened to import. Rule 2 consumes them.

    It reports a CLAIM, not an absence: a package this image never puts on a ``sys.path``
    root is not this image's business and stays unreported however absent it is. That is
    the difference between a checker and a list of everything the repo contains.
    """
    packages = frozenset(first_party_packages())
    wired = {name: package_wiring(spec, name)[0] for name in packages}
    unwired: dict[str, list[ImportSite]] = {}
    for module_path in sorted(shipped_sources(spec)):
        for site in import_sites(module_path, packages):
            if not wired[site.package]:
                unwired.setdefault(site.package, []).append(site)
    for package in path_root_claims(spec):
        if not wired[package]:
            unwired.setdefault(package, [])
    return {package: tuple(sites) for package, sites in sorted(unwired.items())}


def test_the_repo_has_images_and_first_party_packages_to_grade() -> None:
    """A checker that finds nothing to check passes every gate below for the wrong reason.

    Everything here is a way this file could go quiet rather than red. The two Dockerfile
    constructs it deliberately does NOT model are rejected loudly instead of guessed at:

    * ``COPY --from=<stage>`` takes its bytes from another build stage or image, not from
      the build context. Stripping the flag and reading the source as a repo path made the
      materialiser copy a directory straight out of the checkout that the image would never
      contain — which closed T-298 against an artifact whose exchange code cannot exist.
    * ``COPY ["src","dest"]`` (JSON form) splits into nonsense under whitespace parsing.

    Neither appears in this repo today. The day one does, this test fails and says so,
    which is the only acceptable behaviour for a checker that cannot model it.
    """
    assert len(images()) >= 5, f"expected the five service images, found {sorted(images())}"
    packages = first_party_packages()
    assert len(packages) >= 10, f".pkgroot yielded too few packages to be the real one: {packages}"

    from importlib.metadata import PackageNotFoundError, version

    for label, spec in sorted(images().items()):
        assert spec.copies, f"{label}: no COPY instructions parsed — the parser is broken"
        assert shipped_sources(spec), f"{label}: the COPY set contains no python module"
        assert spec.stage_copies == (), (
            f"{label} uses `COPY --from=`, which this checker does not model: the bytes come "
            f"from another stage or image, not the build context, so neither the COPY-set "
            f"analysis nor the materialiser can say what is in the artifact. Teach it the "
            f"construct before relying on any verdict about this image: {spec.stage_copies}"
        )
        assert spec.json_form_copies == (), (
            f"{label} uses the JSON-array COPY form, which this checker does not model: "
            f"{spec.json_form_copies}"
        )
        missing = [s for s, _ in spec.copies if not (REPO_ROOT / s.rstrip("/")).exists()]
        assert missing == [], (
            f"{label} COPYs paths that do not exist in the build context, so `docker build` "
            f"would fail and every verdict about this image is fiction: {missing}"
        )
        assert spec.path_roots, f"{label}: no sys.path roots parsed"
        assert spec.pip_requirements, (
            f"{label}: no `pip install` requirements parsed, so the runtime probe would "
            f"grade this image against the WHOLE dev venv and silently pass any module "
            f"importing a package the image never installs"
        )
        unknown = []
        for distribution in sorted(pinned_distributions(spec)):
            try:
                version(distribution)
            except PackageNotFoundError:
                unknown.append(distribution)
        assert unknown == [], (
            f"{label} pins distributions this venv does not have installed, so the "
            f"dependency closure computed for it is silently TRUNCATED and the runtime probe "
            f"would report false failures: {unknown}"
        )


# =====================================================================================
# T-301 — the gate the whole artifact-blindness class was invisible to
# =====================================================================================


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

    Scoped to ``apps/exchange/src`` by :func:`own_module_failures`. This image also ships
    the shared ``proxyshop_support`` tree, whose ``fixture_loader`` imports ``pytest`` at
    module scope while no image's pip layer installs it — a real defect in all four service
    images, carried by the T-301 gates, that this ticket must not be red for.
    """
    failures = own_module_failures("apps/exchange/Dockerfile")
    assert failures == {}, image_import_report("apps/exchange/Dockerfile").summary()


# =====================================================================================
# T-300 — the buyer image runs a feature permanently degraded, and imports clean
# =====================================================================================


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

    assert own_module_failures(label) == {}, (
        "the premise of this test has changed: the buyer image's OWN package now has modules "
        "that fail to import outright, so it is no longer the lazy-and-swallowed case — "
        f"re-read T-300:\n{image_import_report(label).summary()}"
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


_RECORDINGS_PROBE = (
    textwrap.dedent(
        """
        import json, sys
        """
    )
    + _PROBE_CHANNEL
    + _PIP_LAYER_WALL
    + textwrap.dedent(
        """
        _sys_path = list(sys.path)
        _emit(sys_path=_sys_path)
        import shopify_stub.recordings as recordings
        _emit(sys_path=_sys_path, result={
            "module": recordings.__file__,
            "dir": str(recordings.RECORDINGS_DIR),
            "exists": recordings.RECORDINGS_DIR.is_dir(),
        })
        """
    )
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
    run = run_in_image(label, _RECORDINGS_PROBE)

    assert run.payload is not None, (
        f"the probe never started, so this gate measured nothing:\n{run.diagnosis()}"
    )
    assert "sys_path" in run.payload, (
        f"the probe wrote a report with no sys.path in it, so the leak guard below has "
        f"nothing to check:\n{run.diagnosis()}"
    )
    leaked = _leaked_paths(run.payload["sys_path"])
    assert leaked == [], (
        f"the probe can see the live checkout on sys.path, so it would find the repo's "
        f"fixtures rather than the image's: {leaked}"
    )

    assert "result" in run.payload, (
        f"shopify_stub.recordings could not be imported from the container-shaped tree at "
        f"all, so this gate is red BEFORE the lookup it exists to grade:\n{run.diagnosis()}"
    )
    payload = run.payload["result"]
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
            build = (definition or {}).get("build")
            # Every fragment in this stack is a FIRST-PARTY component: its whole purpose is
            # to run this repo's own code, so it has to be built from a Dockerfile in this
            # repo. Accepting a bare `image:` would let T-313 be closed by pointing a
            # service at `python:3.12-slim`, which deploys a container that runs none of the
            # component's code — measured as passing before this branch existed. A `build:`
            # written as a plain string (the short form) is equally legal compose and used
            # to skip the check entirely, because only the mapping form was inspected.
            if build is None:
                undeployable.append(
                    f"{fragment}: service {name!r} declares no `build:`, so nothing in this "
                    f"repo is compiled into the artifact it would deploy"
                )
                continue
            if isinstance(build, str):
                context, declared = build, f"{build.rstrip('/')}/Dockerfile"
            elif isinstance(build, dict):
                context = str(build.get("context", "."))
                declared = str(build.get("dockerfile") or "Dockerfile")
            else:  # pragma: no cover - compose would reject this itself
                undeployable.append(f"{fragment}: service {name!r} has an unreadable `build:`")
                continue
            resolved = (component / context / declared).resolve()
            if not resolved.is_file():
                resolved = (REPO_ROOT / declared).resolve()
            if not resolved.is_file():
                undeployable.append(
                    f"{fragment}: service {name!r} builds from {declared!r}, which is not a "
                    f"file in this repo"
                )

    assert undeployable == [], (
        "components are included in the deployable stack that cannot be deployed:\n"
        + "\n".join(f"    {line}" for line in undeployable)
    )


#: The phrases the stub's own compose fragment uses to record that its image is unbuilt.
_UNBUILT_MARKERS = ("NOT VERIFIED", "never been BUILT", "expect to debug the build")


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


# =====================================================================================
# T-334 — the static checker's blind spot is CIRCULAR: removing a COPY removes the
#         evidence that it is missing
# =====================================================================================
#
# :func:`_unwired_for` computes ``wired[name] = package_wiring(spec, name)[0]`` for EVERY
# first-party package (one line), and then reports a package only if some module the image
# still ships was found importing it. So the verdict for a package nothing imports any more
# is computed and thrown away.
#
# That is not a hypothetical gap, it is a closed loop. ``claim_verification``'s only
# module-scope importer inside the trust image's shipped tree is
# ``packages/verification/__init__.py:47`` — a file the ``COPY`` under test is what ships.
# Delete the COPY and the importer goes with it, so the checker has nothing to hang the
# report on and says the image is fine.
#
# Measured on the real ``apps/trust/Dockerfile``, sabotaged by dropping both
# ``COPY packages/verification/...`` lines and KEEPING
# ``ln -s ../packages/verification/src /app/.pkgroot/claim_verification``:
#
#     _unwired_for(sabotaged)                              -> {}          SILENT
#     package_wiring(sabotaged, "claim_verification")      -> (False, "no COPY covers packages/verification/src/")
#     shipped modules importing claim_verification         -> none
#     image_import_report("apps/trust/Dockerfile")         -> 0 broken, sabotaged AND at baseline
#
# Three refinements to the ticket record, all measured — and every number below was
# re-measured in the worktree this comment ships in, because two of them were wrong before:
#
# * The record says the fix is "one rule over the dangling-symlink branch at :428". Its
#   LINE NUMBER was right for the revision it was written against — at ``a31098b``, :428 is
#   the ``return False, f"{link} -> {pointed} … the link dangles"`` inside
#   :func:`package_wiring`'s link loop. (It has since drifted onto docstring prose in this
#   same function, and an earlier correction here resolved the number against HEAD and
#   therefore gave the wrong reason. Line numbers rot; that is the lesson, not the finding.)
#   What the record gets wrong is the CONCLUSION: that branch is not what produces this
#   verdict. Traced with ``sys.settrace`` over
#   ``package_wiring(sabotaged, "claim_verification")``, the only statements that execute
#   are the ``first_party_packages().get(package)`` lookup, its ``None`` guard, the
#   ``copies_directory(spec, provider) is None`` test and its
#   ``return False, f"no COPY covers {provider}/"`` (:435/436/438/439 at this revision).
#   The ``for target, link in spec.links`` loop runs ZERO times. The rule belongs over
#   :func:`_unwired_for`'s use of ``wired``, not over that branch.
# * The record says "the real-image control DID catch this sabotage". The container-shaped
#   runtime control in this file does NOT, and it has no pre-existing failures to hide
#   behind either: ``image_import_report`` reports ZERO broken modules for the trust image
#   on the sabotaged tree AND zero at baseline — all nine images are 0 broken at baseline —
#   so the sabotage adds nothing to it. The reason is that
#   ``apps/trust/src/verification/__init__.py`` resolves the package through a PEP 562
#   ``__getattr__`` that no static or import-time probe triggers.
# * An earlier draft of THIS COMMENT said exactly one test in the repo catches it, and
#   named only the attribute probe. That is the draft's error, not the record's: the record
#   credits two catchers ("the real-image control DID catch this sabotage, and the T-193
#   probe did"), and its SECOND one is real — "the T-193 probe" is the static gate named
#   below, which does catch it. Only the record's first attribution is wrong, and the
#   bullet above is where that is settled. There are TWO catchers, in two lanes, catching
#   by two different mechanisms. Measured by sabotaging the Dockerfile on disk and running
#   every test file in the repo that reads a Dockerfile at all — ten of them, which is all
#   of them, since nothing in the suite builds an image; these are the ones that turn red:
#
#       apps/trust/tests/test_repro_open_tickets.py::
#           test_the_trust_image_copy_set_can_resolve_the_claim_verifier
#       — materialises the COPY set into a temp tree, recreates the ``ln -s`` pairs from
#         the Dockerfile, and asks a subprocess for the ATTRIBUTE
#         ``trust.verification.verify``; fails with the seam's own ModuleNotFoundError.
#
#       packages/verification/tests/test_repro_open_tickets.py::
#           test_the_trust_image_ships_the_verifier_its_own_seam_reaches_for
#       — purely STATIC: asserts some ``COPY`` source starts with ``packages/verification``
#         and that ``claim_verification`` appears in the Dockerfile text. Live (not xfail)
#         and green at baseline, red under the sabotage. It never builds anything, so it
#         survives exactly the false-green mechanism the runtime half is exposed to.
#
#   The armed control below also turns red, because its "every real image is clean today"
#   clause is precisely this shape — but that control ships WITH this ticket, so it is not
#   evidence about what the repo covered before it.
#
# So the blind spot is guarded by two tests, in two lanes, for ONE package — while
# :func:`_t334_witnesses` derives 16 witnesses (7 distinct packages) with the same shape,
# 11 of which (5 distinct packages) survive :func:`_t334_sabotaged` into
# :func:`_t334_cases`. Either count spans 8 of the 9 images.
#
# The property below is stated over the CLAIM the image makes rather than over what
# survives in it: an image whose build puts a package's name on one of its own sys.path
# roots is claiming to provide that package, and a claim that ``package_wiring`` already
# says is false must be reported whether or not a surviving module still imports it.


def _t334_covers(source: str, repo_relative: str) -> bool:
    """Whether a ``COPY`` source path contains ``repo_relative``."""
    src = source.rstrip("/")
    return repo_relative == src or repo_relative.startswith(f"{src}/")


def _t334_drop_copies(text: str, sources: frozenset[str]) -> str:
    """``text`` with every ``COPY`` line naming one of ``sources`` removed.

    Only ``COPY`` lines, and only ones whose source token matches exactly — a ``RUN`` that
    mentions the same path, or the ``ln -s`` this sabotage must PRESERVE, is left alone.
    The removal is never trusted on line count; every caller re-parses the result and
    asserts on the parsed spec instead.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("COPY ") and any(
            re.search(rf"(?:^|\s){re.escape(source)}(?:\s|$)", stripped) for source in sources
        ):
            continue
        kept.append(line)
    return "\n".join(kept)


def _t334_witnesses() -> list[dict[str, Any]]:
    """Every (image, package) pair with the circular shape, DERIVED — never named here.

    A witness needs all four of:

    1. the image resolves the package today (``package_wiring`` -> True), so the sabotage
       is a real regression rather than a pre-existing hole;
    2. the name is put on one of the image's own ``sys.path`` roots by an ``ln -s`` — this
       is the CLAIM the property is about, and it is what survives the sabotage;
    3. some module the image ships imports the package — the evidence the checker currently
       depends on;
    4. there are ``COPY`` lines covering both the provider directory and every one of those
       importers, so removing them removes the provider AND the evidence together.

    Deriving it is what keeps the fix honest: a repair that special-cased ``trust`` or
    ``claim_verification`` would still be red on the other seven images. Nothing in this
    function reads a report, so the witness set does not move when the defect is fixed.
    """
    providers = first_party_packages()
    names = frozenset(providers)
    found: list[dict[str, Any]] = []
    for label, spec in sorted(images().items()):
        importers: dict[str, set[str]] = {}
        for module_path in sorted(shipped_sources(spec)):
            for site in import_sites(module_path, names):
                importers.setdefault(site.package, set()).add(site.module_path)
        for package in sorted(importers):
            if not package_wiring(spec, package)[0]:
                continue
            claims = [
                link
                for _target, link in spec.links
                if any(
                    link.rstrip("/") == f"{root.rstrip('/')}/{package}" for root in spec.path_roots
                )
            ]
            if not claims:
                continue
            provider = providers[package]
            doomed = frozenset(
                source
                for source, _dest in _real_copies(spec)
                if _t334_covers(source, provider)
                or any(_t334_covers(source, module) for module in importers[package])
            )
            if not doomed:
                continue
            found.append(
                {
                    "label": label,
                    "package": package,
                    "provider": provider,
                    "doomed": doomed,
                    "claims": tuple(claims),
                    "importers": tuple(sorted(importers[package])),
                }
            )
    return found


def _t334_claimed(spec: ImageSpec) -> dict[str, str]:
    """``{package: the sys.path entry the build creates for it}`` — the image's CLAIMS.

    A build that runs ``ln -s <target> <root>/<pkg>``, where ``<root>`` is one of the
    image's own ``sys.path`` roots, is asserting that ``import <pkg>`` will work inside the
    image. That assertion is what this gate holds the checker to, and it is deliberately
    independent of whether any module survives to make the import — which is the circular
    question that made the checker quiet in the first place.
    """
    claimed: dict[str, str] = {}
    for package in sorted(first_party_packages()):
        for _target, link in spec.links:
            if any(link.rstrip("/") == f"{root.rstrip('/')}/{package}" for root in spec.path_roots):
                claimed[package] = link
                break
    return claimed


def _t334_blind_spots(spec: ImageSpec) -> dict[str, str]:
    """``{package: why}`` for every package this spec CLAIMS, fails to wire, and no longer
    imports — i.e. every package the current checker is structurally unable to report.

    Computed from :func:`package_wiring`, :func:`_t334_claimed` and :func:`import_sites`
    only, never from a report, so it reads the same before and after the fix this gate
    asks for. A selection step that moved with the fix would leave the gate with nothing to
    grade the moment it went green.
    """
    names = frozenset(first_party_packages())
    blind: dict[str, str] = {}
    for package, _link in _t334_claimed(spec).items():
        wired, why = package_wiring(spec, package)
        if wired:
            continue
        if any(
            site.package == package
            for module_path in sorted(shipped_sources(spec))
            for site in import_sites(module_path, names)
        ):
            continue  # a surviving importer: the checker sees this one the ordinary way
        blind[package] = why
    return blind


def _t334_sabotaged(witness: dict[str, Any]) -> ImageSpec | None:
    """The witness's image with the COPYs removed, or ``None`` if the sabotage is not usable.

    Rejected: a sabotage that did not actually unwire the witness package; one that left a
    module still importing it (then the checker sees it the ordinary way and there is no
    blind spot to grade); one that removed the ``ln -s`` CLAIM along with the COPY, leaving
    nothing to hold the image to; and one that emptied the image outright.

    Collateral damage is deliberately NOT rejected. A ``COPY apps/buyer/svc/src/`` that has
    to go because it holds the only importer also unwires ``buyer_svc``, and an earlier
    draft threw those witnesses away — which cut the derivable set from eleven to two and,
    worse, narrowed the property to one package per image. The property below is stated
    over :func:`_t334_blind_spots` instead, so a collaterally unwired package is simply one
    more claim the report has to name rather than a reason to discard the case.
    """
    label = witness["label"]
    package = witness["package"]
    text = (REPO_ROOT / label).read_text(encoding="utf-8")
    sabotaged = parse_dockerfile_text(
        _t334_drop_copies(text, witness["doomed"]), f"{label}/t334-sabotaged"
    )
    if not shipped_sources(sabotaged):
        return None
    if not all(claim in {link for _target, link in sabotaged.links} for claim in witness["claims"]):
        return None
    if package not in _t334_blind_spots(sabotaged):
        return None
    return sabotaged


@lru_cache(maxsize=1)
def _t334_cases() -> tuple[tuple[dict[str, Any], ImageSpec], ...]:
    """The witnesses whose sabotage is usable, paired with the sabotaged spec."""
    cases = []
    for witness in _t334_witnesses():
        sabotaged = _t334_sabotaged(witness)
        if sabotaged is not None:
            cases.append((witness, sabotaged))
    return tuple(cases)


def test_t334_the_circular_blind_spot_witnesses_are_armed() -> None:
    """Not xfail, and not decoration: the gate below is worthless without every clause here.

    Under ``xfail(strict=True)`` ANY exception in the graded body reads as ``xfailed``,
    i.e. green — so a derivation that had stopped producing witnesses would be
    indistinguishable from the defect being fixed. Everything this control asserts is
    computed from :func:`package_wiring`, :func:`shipped_sources` and :func:`import_sites`
    and NEVER from a report, so it passes identically before and after the fix.
    """
    cases = _t334_cases()
    assert len(cases) >= 8, (
        f"only {len(cases)} circular witnesses could be derived from this repo. The "
        f"derivation has stopped working (or the images changed shape), and the gate below "
        f"would report on almost nothing: {[(w['label'], w['package']) for w, _s in cases]}"
    )
    assert len({witness["label"] for witness, _spec in cases}) >= 5, (
        "the witnesses come from too few images, so a fix that special-cased one image "
        f"could satisfy the gate: {sorted({w['label'] for w, _s in cases})}"
    )
    assert len({witness["package"] for witness, _spec in cases}) >= 3, (
        "every witness is the same package, so a fix keyed on that name would satisfy the "
        f"gate: {sorted({w['package'] for w, _s in cases})}"
    )

    # Every real image is clean today. Without this, the gate could be "satisfied" by a
    # checker that reports every package on every image, which reports nothing at all.
    for label, spec in sorted(images().items()):
        assert not _t334_blind_spots(spec), (
            f"{label} already claims a package it does not wire, at HEAD, with no importer "
            f"left to notice: {_t334_blind_spots(spec)}. The gate below cannot tell that "
            f"from the sabotage it makes."
        )

    for witness, sabotaged in cases:
        label, package = witness["label"], witness["package"]
        where = f"{label} / {package}"

        # The baseline really is clean — otherwise the sabotage proves nothing.
        text = (REPO_ROOT / label).read_text(encoding="utf-8")
        baseline = parse_dockerfile_text(text, f"{label}/t334-control")
        wired, why = package_wiring(baseline, package)
        assert wired, f"{where}: the image does not resolve {package!r} even at HEAD ({why})"
        assert package not in _unwired_for(baseline), (
            f"{where}: the checker already reports {package!r} on the UNSABOTAGED image, "
            f"so the two halves of it disagree and nothing below is interpretable"
        )

        # The sabotage removed COPYs and nothing else: the CLAIM survived it.
        for claim in witness["claims"]:
            assert claim in {link for _target, link in sabotaged.links}, (
                f"{where}: the sabotage removed the `ln -s ... {claim}` as well as the "
                f"COPY, so the image no longer claims to provide {package!r} and there is "
                f"nothing left to grade"
            )
        assert shipped_sources(sabotaged), (
            f"{where}: the sabotage emptied the image; it is over-broad, not surgical"
        )

        # THE ANSWER IS ALREADY COMPUTED. This is the ticket's central claim, asserted.
        sabotaged_wired, sabotaged_why = package_wiring(sabotaged, package)
        assert not sabotaged_wired, f"{where}: the sabotage did not unwire {package!r}"
        assert sabotaged_why, f"{where}: package_wiring gave no reason to report"

        # …and the evidence the checker currently needs is gone with the COPY. This is the
        # circularity itself, and it is what makes the gate below non-trivial.
        names = frozenset(first_party_packages())
        survivors = [
            str(site)
            for module_path in sorted(shipped_sources(sabotaged))
            for site in import_sites(module_path, names)
            if site.package == package
        ]
        assert not survivors, (
            f"{where}: a shipped module still imports {package!r} after the sabotage, so "
            f"the checker can see it the ordinary way and this is not a blind-spot "
            f"witness: {survivors}"
        )


def test_t334_an_image_that_claims_a_package_is_graded_when_the_copy_takes_its_importer() -> None:
    """Every derived witness, not one: a special case for ``trust`` must not satisfy this.

    The property, stated over the image's CLAIM rather than over its survivors: if the
    build puts ``<pkg>`` on one of the image's own ``sys.path`` roots, the image is
    claiming to provide it, and a claim that :func:`package_wiring` already answers ``False``
    must appear in the report. Whether a surviving module still imports it is exactly the
    circular question that made the checker quiet.

    CLOSED. :func:`_unwired_for` now consumes the verdict it was already computing: after
    the import-site loop, any package :func:`path_root_claims` finds on a ``spec.path_roots``
    entry whose ``wired[name]`` is False gets ``unwired.setdefault(name, [])`` — the empty
    site tuple the return contract always allowed. The strict ``xfail`` that named this
    defect was removed with the repair, which is why this node grades rather than xfails.

    Measured after the repair rather than simulated: all 9 real images still report ``{}``,
    the positive control's ``repaired == baseline`` round trip still holds, and every one of
    the 11 derived sabotages is now reported. The rule is over the CLAIM, so it stays quiet
    about a package the image never puts on a ``sys.path`` root — see
    :func:`test_t334_a_package_the_image_never_claims_stays_unreported`, which is the
    negative control that keeps this from degenerating into "report everything absent".
    """
    cases = _t334_cases()
    assert cases, "no witnesses — the armed control above says why this is a failure"
    silent = []
    for witness, sabotaged in cases:
        missed = sorted(set(_t334_blind_spots(sabotaged)) - set(_unwired_for(sabotaged)))
        if missed:
            reasons = _t334_blind_spots(sabotaged)
            silent.append(
                f"  {witness['label']}, after dropping {sorted(witness['doomed'])}: the "
                f"image still claims "
                + ", ".join(f"{name!r} ({reasons[name]})" for name in missed)
                + f" — but the only shipped importer(s) of {witness['package']!r} "
                f"{list(witness['importers'])} went with the COPY, so the report said nothing"
            )
    assert not silent, (
        "the static copy-set checker is SILENT about a package the image still claims to "
        "provide, for "
        + f"{len(silent)} of {len(cases)} derived witnesses, and in every one of them it "
        "had ALREADY computed the failing verdict and thrown it away:\n"
        + "\n".join(silent)
        + "\n\nRemoving a COPY removes the importer that was the evidence the COPY was "
        "needed, so the checker grades a smaller image and finds it consistent. Repair: at "
        "the end of `_unwired_for`, also report any package whose name the build puts on a "
        "`spec.path_roots` entry and whose `wired[...]` entry — already computed on the "
        "line above the loop — is False."
    )


def _t334_withdraw_claims(text: str, claims: tuple[str, ...]) -> str:
    """``text`` with each ``ln -s`` link path in ``claims`` renamed out of the package namespace.

    Renamed, not deleted. These links live inside a backslash-continued ``RUN`` chain, and
    deleting the LAST line of one leaves the line above it ending in a ``\\`` that swallows
    the instruction beneath it — the exact failure :func:`_instructions` documents. A control
    that silently removes a whole ``COPY`` on the way past is not a control, it is a second
    defect wearing one's clothes. Renaming keeps the chain intact and withdraws exactly one
    thing: the image no longer puts that package's NAME on a ``sys.path`` root.
    """
    for claim in claims:
        text = re.sub(rf"{re.escape(claim)}(?![\w.-])", f"{claim}__withdrawn", text)
    return text


def test_t334_a_package_the_image_never_claims_stays_unreported() -> None:
    """The negative control for the rule above: it reports a CLAIM, not an absence.

    The cheap way to pass the gate above is to report every first-party package an image
    cannot resolve. That checker would be red on all eleven sabotages and would also be
    useless — nine images times twelve packages of noise, in which a real finding is a
    rounding error. So the rule has to be shown to STAY QUIET on the same sabotage with one
    thing changed: the ``ln -s`` that put the name on the path is withdrawn, so the image
    stops claiming the package. Nothing else moves — the ``COPY`` is still gone, the package
    is still unresolvable, no shipped module still imports it.

    Both halves are asserted on the SAME spec, which is what makes this a control rather
    than a second opinion: ``package_wiring`` still says False (so the report is silent by
    the rule, not because the defect evaporated) and the package is absent from the report.

    The last clause states the invariant in general: the report never names a package that
    the image neither claims nor ships an importer for. That is the property a
    report-everything checker fails and this one must not.
    """
    cases = _t334_cases()
    assert cases, "no witnesses — the armed control above says why this is a failure"
    names = frozenset(first_party_packages())
    checked: list[str] = []
    for witness, sabotaged in cases:
        label, package = witness["label"], witness["package"]
        where = f"{label} / {package}"

        text = (REPO_ROOT / label).read_text(encoding="utf-8")
        unclaimed = parse_dockerfile_text(
            _t334_withdraw_claims(_t334_drop_copies(text, witness["doomed"]), witness["claims"]),
            f"{label}/t334-unclaimed",
        )
        assert shipped_sources(unclaimed), (
            f"{where}: withdrawing the claim emptied the image, so the parse was damaged "
            f"rather than the claim withdrawn"
        )
        assert package not in path_root_claims(unclaimed), (
            f"{where}: the withdrawal did not remove the claim — {package!r} is still on a "
            f"sys.path root at {path_root_claims(unclaimed)[package]}, so this control is "
            f"not testing what it says it is"
        )

        # Nothing else moved: still unresolvable, still no surviving importer.
        wired, why = package_wiring(unclaimed, package)
        assert not wired, (
            f"{where}: withdrawing the claim made {package!r} resolvable ({why}), so the "
            f"silence below would prove nothing"
        )
        survivors = [
            str(site)
            for module_path in sorted(shipped_sources(unclaimed))
            for site in import_sites(module_path, names)
            if site.package == package
        ]
        assert not survivors, f"{where}: a shipped module still imports {package!r}: {survivors}"

        assert package not in _unwired_for(unclaimed), (
            f"{where}: the checker reports {package!r} on an image that does NOT claim it. "
            f"It is unresolvable ({why}) and no shipped module imports it — this image "
            f"simply has nothing to do with it, and a checker that names it here names "
            f"every absent package on every image, which is the same as naming none: "
            f"{sorted(_unwired_for(unclaimed))}"
        )

        # The invariant, on the reporting spec: every name in the report is there because
        # the image claims it or because something it ships imports it.
        imported = {
            site.package
            for module_path in sorted(shipped_sources(sabotaged))
            for site in import_sites(module_path, names)
        }
        grounded = set(path_root_claims(sabotaged)) | imported
        assert set(_unwired_for(sabotaged)) <= grounded, (
            f"{where}: the report names a package this image neither claims nor imports: "
            f"{sorted(set(_unwired_for(sabotaged)) - grounded)}"
        )
        checked.append(where)

    assert len(checked) >= 8, (
        f"only {len(checked)} withdrawals were usable, so the negative control grades "
        f"almost nothing: {checked}"
    )
    assert len({c.split(" / ")[0] for c in checked}) >= 5, (
        f"the negative control covers too few images to rule out an image-specific fix: "
        f"{sorted({c.split(' / ')[0] for c in checked})}"
    )


def test_t334_the_report_says_why_a_claim_only_failure_is_a_failure() -> None:
    """The claim-only entry has to READ as a finding, not as an empty row.

    :func:`_unwired_for` now reports a package with no import sites at all, which is a shape
    the report never had to render before. Every real image is clean, so without this the
    branch would first execute on the day a real image breaks — and a reporting path whose
    first run is its production run is how a checker crashes, or prints "0 sites and 0
    sites", exactly when it finally has something to say.

    Asserted on content, not on formatting: the package, the ``sys.path`` entry that carries
    the claim, and ``package_wiring``'s own reason all have to appear, because those three
    are what tell a reader which COPY to restore.
    """
    cases = _t334_cases()
    assert cases, "no witnesses — the armed control above says why this is a failure"
    rendered = 0
    for witness, sabotaged in cases:
        label, package = witness["label"], witness["package"]
        report = _unwired_for(sabotaged)
        if report.get(package) != ():
            continue  # this witness reports the package with sites; not the branch under test
        text = _failure_report_for(sabotaged, label, report)
        _, why = package_wiring(sabotaged, package)
        claim = path_root_claims(sabotaged)[package]
        for needed in (label, repr(package), claim, why):
            assert needed in text, (
                f"{label} / {package}: the claim-only report omits {needed!r}, so it names a "
                f"failure without saying what to restore:\n{text}"
            )
        assert "0 module-scope site(s)" not in text, (
            f"{label} / {package}: the claim-only entry rendered as an empty site count, "
            f"which reads as a bug in the report rather than a finding:\n{text}"
        )
        rendered += 1
    assert rendered >= 8, (
        f"only {rendered} of {len(cases)} witnesses exercised the claim-only report branch, "
        f"so it is still substantially unrendered"
    )
