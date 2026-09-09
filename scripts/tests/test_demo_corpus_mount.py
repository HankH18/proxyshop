"""The gate on WHICH CORPUS ``make demo-corpus`` actually loads.

Why this file exists
--------------------
Two gates on the demo corpus already existed and neither could see this:

* ``scripts/tests/test_seed_demo_trust.py`` requires every host in the corpus to carry a trust
  posture — corpus against trust seed;
* ``scripts/tests/test_build_demo_deployment.py::
  test_the_committed_documents_are_what_the_corpus_implies`` requires the committed
  ``deploy/demo/*`` to be exactly what ``scripts/build_demo_deployment.py`` derives from
  ``build_demo_deployment.CORPUS`` — corpus against documents.

Both take the corpus as given. **Nothing asserted that the corpus the compose mount hands the
loader is that same corpus**, and that is exactly what drifted. ``f9dcc1a`` committed the
curated nineteen-store ``fixtures/real-catalogs-demo/``, regenerated ``deploy/demo/*`` from it
and extended the trust seed to nineteen sellers — and left ``services/ingest/compose.yaml``
mounting ``../../fixtures/real-catalogs``, the ten supplement storefronts. Every gate above was
green. An operator running the documented ``make demo-corpus`` got the OLD corpus in the graph
and the NEW documents in the containers, which is worse than either alone: the deployment
document names products the graph does not hold.

What this asserts, in one sentence: **the corpus the loader's compose mount fills is the corpus
``deploy/demo/`` was generated from, the loader is pointed at the directory that mount fills,
and both name stores the deployment document rosters.**

How it is checked, and why it is not a grep
-------------------------------------------
:func:`corpus_loader_service` finds the service by its COMMAND — the one whose ``command`` runs
``ingest.scheduler.load_corpus`` — across every fragment ``docker-compose.yml`` includes, so
moving the loader to another fragment or renaming the service does not silently un-gate it.
:func:`resolve_compose_defaults` resolves ``${VAR:-default}`` the way compose does for a shell
with nothing exported, which is the state a fresh clone runs ``make demo-corpus`` in.

:func:`test_a_mount_naming_a_different_corpus_fails_this_gate` runs the same comparison over a
fragment that mounts the OLD corpus and requires it to FAIL. A gate that has never been shown to
go red is not a gate.

Collection: ``scripts`` is in ``[tool.pytest.ini_options] testpaths``. Nothing here opens a
socket, starts a container or needs a database — it reads the compose fragments, the corpus
manifest and the committed documents off the tree.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_COMPOSE = REPO_ROOT / "docker-compose.yml"
DEPLOY = REPO_ROOT / "deploy" / "demo"
RUNBOOK = REPO_ROOT / "docs" / "demo" / "shopper-demo.md"

#: The module path the loader's ``command`` runs. The service is found by THIS rather than by
#: the name ``corpus-loader``, so a rename is not a way to leave the mount ungated.
LOADER_MODULE = "ingest.scheduler.load_corpus"

#: ``adapters/recorded.CORPUS_ENV`` — the override the loader publishes for where the corpus is.
#: Restated here rather than imported because importing ``ingest.adapters.recorded`` pulls the
#: whole adapter package in for one string, and :func:`test_the_corpus_env_name_is_the_one_the
#: _loader_reads` asserts the two still agree.
CORPUS_ENV = "PROXYSHOP_RECORDED_CATALOGS"

#: ``${NAME}`` or ``${NAME:-default}``. Compose also accepts ``${NAME-default}`` and
#: ``${NAME:?err}``; neither appears in this repository, and :func:`resolve_compose_defaults`
#: refuses anything it does not recognise rather than passing it through as a literal.
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^{}]*))?\}")


class UnresolvableMount(AssertionError):
    """A compose value this gate cannot read the way a fresh clone would."""


def resolve_compose_defaults(value: str) -> str:
    """``value`` as compose renders it with nothing exported.

    ``${PROXYSHOP_DEMO_CORPUS:-real-catalogs-demo}`` becomes ``real-catalogs-demo``. A
    ``${VAR}`` with NO default raises: on a fresh clone compose renders it empty, which turns
    ``../../fixtures/`` into a mount of the whole fixtures tree, and a gate that quietly read
    it as a literal would call that agreement.
    """

    def _one(match: re.Match[str]) -> str:
        if match.group(2) is None:
            raise UnresolvableMount(
                f"{match.group(0)} has no `:-default`, so a clone with nothing exported "
                f"renders it EMPTY. The demo's corpus must have a working default"
            )
        return match.group(3)

    resolved = _INTERPOLATION.sub(_one, value)
    if "${" in resolved or "$(" in resolved:
        raise UnresolvableMount(f"{value!r} carries interpolation this gate cannot resolve")
    return resolved


def _fragments() -> list[Path]:
    """Every compose file the root includes, plus the root itself."""
    root = yaml.safe_load(ROOT_COMPOSE.read_text(encoding="utf-8")) or {}
    paths = [ROOT_COMPOSE]
    for entry in root.get("include") or []:
        # Compose's include accepts a bare string or a mapping with `path`.
        relative = entry if isinstance(entry, str) else (entry or {}).get("path")
        if isinstance(relative, list):
            paths.extend(REPO_ROOT / item for item in relative)
        elif relative:
            paths.append(REPO_ROOT / str(relative))
    return paths


@functools.cache
def corpus_loader_service() -> tuple[Path, str, dict[str, Any]]:
    """``(fragment, service name, definition)`` for the service that loads the corpus.

    Found by its ``command``, not its name. Exactly one service in the repository may run
    :data:`LOADER_MODULE`; two would be two answers to "what does ``make demo-corpus`` load".
    """
    found: list[tuple[Path, str, dict[str, Any]]] = []
    for path in _fragments():
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, definition in (parsed.get("services") or {}).items():
            if not isinstance(definition, dict):
                continue
            command = definition.get("command") or []
            words = command if isinstance(command, list) else str(command).split()
            if any(LOADER_MODULE == str(word) for word in words):
                found.append((path, str(name), definition))
    assert found, (
        f"no compose service runs `{LOADER_MODULE}`, so nothing in this repository loads the "
        f"recorded corpus into Neo4j and `make demo-corpus` cannot work"
    )
    assert len(found) == 1, (
        f"{len(found)} compose services run `{LOADER_MODULE}`: "
        f"{[(str(p.relative_to(REPO_ROOT)), n) for p, n, _ in found]}. "
        f"There must be exactly one answer to what `make demo-corpus` loads"
    )
    return found[0]


@functools.cache
def mounted_corpus() -> tuple[Path, str]:
    """``(host directory, container directory)`` the loader's corpus mount joins.

    The loader mounts exactly one corpus. A service that mounted two would be a service whose
    corpus depends on which one ``default_corpus_dir()`` happened to find.
    """
    fragment, service, definition = corpus_loader_service()
    corpora: list[tuple[Path, str]] = []
    for raw in definition.get("volumes") or []:
        # A long-form volume is a mapping; the short form is `source:target[:mode]`. Resolved
        # BEFORE splitting, because `${VAR:-default}` contains a colon of its own.
        if isinstance(raw, dict):
            source, target = str(raw.get("source", "")), str(raw.get("target", ""))
        else:
            parts = resolve_compose_defaults(str(raw)).split(":")
            assert len(parts) >= 2, f"{service}: cannot read the volume {raw!r}"
            source, target = parts[0], parts[1]
        source, target = resolve_compose_defaults(source), resolve_compose_defaults(target)
        host = (fragment.parent / source).resolve()
        try:
            relative = host.relative_to(REPO_ROOT / "fixtures")
        except ValueError:
            continue
        if len(relative.parts) == 1:
            corpora.append((host, target))
    assert len(corpora) == 1, (
        f"{service} in {fragment.relative_to(REPO_ROOT)} mounts {len(corpora)} directories "
        f"under fixtures/ ({[str(h.name) for h, _ in corpora]}); it must mount exactly the one "
        f"corpus it loads"
    )
    return corpora[0]


@functools.cache
def generator() -> Any:
    """``scripts/build_demo_deployment.py`` — the module that GENERATED ``deploy/demo/*``."""
    if str(REPO_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "build_demo_deployment", REPO_ROOT / "scripts" / "build_demo_deployment.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("build_demo_deployment", module)
    spec.loader.exec_module(module)
    return module


def _collected_hosts(corpus: Path) -> set[str]:
    """The hosts a corpus manifest says it actually collected.

    ``skipped is None`` is ``build_demo_deployment.corpus_hosts``'s own rule for "this row is a
    storefront the corpus carries", restated because that function reads the generator's
    ``CORPUS`` — which is precisely the constant this file is comparing the mount against, and
    so cannot be the source of both sides.
    """
    manifest = json.loads((corpus / "collection.json").read_text(encoding="utf-8"))
    return {str(row["host"]) for row in manifest.get("stores") or [] if row.get("skipped") is None}


# =====================================================================================
# 1. The mount is the corpus the documents came from
# =====================================================================================
def test_the_mount_loads_the_corpus_the_deployment_documents_were_generated_from() -> None:
    """THE GATE THIS FILE EXISTS FOR.

    ``deploy/demo/*`` is derived from ``build_demo_deployment.CORPUS`` and committed, and its
    product ids are ``sha256(store_id \\x1f native_key)`` — so a graph loaded from a different
    corpus holds different ids and the exchange's own catalogue window points at products
    nothing can resolve. The two facts must name one directory.
    """
    host, _ = mounted_corpus()
    expected = generator().CORPUS.resolve()
    _, service, _ = corpus_loader_service()
    assert host == expected, (
        f"`{service}` mounts fixtures/{host.name} and deploy/demo/* was generated from "
        f"fixtures/{expected.name}. `make demo-corpus` would load a graph the committed "
        f"deployment documents do not describe. Fix the mount in the fragment, or regenerate "
        f"the documents with scripts/build_demo_deployment.py against the corpus you meant"
    )


def test_the_loader_reads_the_directory_the_mount_fills() -> None:
    """The mount puts the corpus somewhere; the loader has to look THERE.

    ``adapters.recorded.default_corpus_dir()`` falls back to
    ``parents[4] / "fixtures/real-catalogs"`` off its own ``__file__``, which inside this image
    is ``/app/fixtures/real-catalogs``. That fallback is right for exactly one corpus and
    silently wrong for the other two, so the service must STATE the directory — and state the
    one it mounted.
    """
    _, service, definition = corpus_loader_service()
    _, target = mounted_corpus()
    environment = definition.get("environment") or {}
    assert isinstance(environment, dict), (
        f"{service}: this gate reads the mapping form of `environment`"
    )
    assert CORPUS_ENV in environment, (
        f"`{service}` mounts the corpus at {target} and states no {CORPUS_ENV}, so the loader "
        f"falls back to the repo-layout default and dies on a FileNotFoundError naming a "
        f"directory the mount did not fill"
    )
    stated = resolve_compose_defaults(str(environment[CORPUS_ENV]))
    assert stated.rstrip("/") == target.rstrip("/"), (
        f"`{service}` mounts the corpus at {target} and points the loader at {stated}. "
        f"The loader reads {CORPUS_ENV}, so it would read an empty or absent directory"
    )


def test_the_corpus_env_name_is_the_one_the_loader_reads() -> None:
    """:data:`CORPUS_ENV` is a copy of ``adapters.recorded.CORPUS_ENV``; copies drift."""
    source = (REPO_ROOT / "services" / "ingest" / "src" / "adapters" / "recorded.py").read_text(
        encoding="utf-8"
    )
    assert f'CORPUS_ENV = "{CORPUS_ENV}"' in source, (
        f"ingest.adapters.recorded no longer publishes {CORPUS_ENV} as its corpus override; "
        f"this gate and services/ingest/compose.yaml are both naming a variable nothing reads"
    )


def test_the_mounted_corpus_is_a_corpus_on_disk() -> None:
    """A mount source Docker cannot find is created EMPTY rather than refused."""
    host, _ = mounted_corpus()
    assert host.is_dir(), (
        f"{host} does not exist. Docker creates a missing bind source as an empty directory, "
        f"so this is not a compose failure — it is a loader that reads zero products and a "
        f"graph the exchange finds nobody in"
    )
    assert (host / "collection.json").is_file(), f"{host} has no collection.json"
    assert (host / "stores").is_dir(), f"{host} has no stores/ directory"


# =====================================================================================
# 2. The mount and the roster name the same stores
# =====================================================================================
def test_every_mounted_store_is_rostered_and_every_rostered_store_is_mounted() -> None:
    """Adding a store to one side and not the other is the drift this catches directly.

    A store in the corpus and not the document is a store the graph can return and the exchange
    will not shortlist (its ``sellers`` row is what makes it eligible). A store in the document
    and not the corpus is a rostered seller whose products the graph does not hold, which is a
    seller that can never be collected.
    """
    host, _ = mounted_corpus()
    corpus_hosts = _collected_hosts(host)
    deployment = json.loads((DEPLOY / "exchange-deployment.json").read_text(encoding="utf-8"))
    rostered = {str(row["store_id"]) for row in deployment.get("sellers") or []}
    assert corpus_hosts == rostered, (
        f"the corpus `make demo-corpus` mounts (fixtures/{host.name}) and "
        f"deploy/demo/exchange-deployment.json do not name the same stores.\n"
        f"  in the corpus, not rostered: {sorted(corpus_hosts - rostered) or 'none'}\n"
        f"  rostered, not in the corpus: {sorted(rostered - corpus_hosts) or 'none'}"
    )


# =====================================================================================
# 3. The documented path names the corpus the documented path loads
# =====================================================================================
def test_the_runbook_names_the_corpus_the_demo_actually_loads() -> None:
    """The defect was found by an operator following the runbook, so the runbook is gated.

    Narrow on purpose: the page may name the other corpora for contrast, and this only requires
    that it names the one ``make demo-corpus`` puts in the graph.
    """
    host, _ = mounted_corpus()
    text = RUNBOOK.read_text(encoding="utf-8")
    assert f"fixtures/{host.name}" in text, (
        f"{RUNBOOK.relative_to(REPO_ROOT)} never names fixtures/{host.name}, which is what "
        f"`make demo-corpus` loads. The runbook is the path this defect was found on"
    )


# =====================================================================================
# 4. The gate goes red — shown, not asserted
# =====================================================================================
#: ``services/ingest/compose.yaml``'s corpus-loader as ``f9dcc1a`` left it: the nineteen-store
#: documents were committed and this mount still named the incumbent ten. Every gate in the
#: repository was green over exactly this.
_DRIFTED_FRAGMENT = """
services:
  corpus-loader:
    volumes:
      - ../../fixtures/real-catalogs:/app/fixtures/real-catalogs:ro
    environment:
      PROXYSHOP_RECORDED_CATALOGS: "/app/fixtures/real-catalogs"
    command: ["python", "-m", "ingest.scheduler.load_corpus", "--all", "--no-lock"]
"""


def _corpus_of(fragment_text: str, fragment_dir: Path) -> Path:
    """The host corpus directory a fragment's loader mounts — :func:`mounted_corpus` without
    the cache or the repository, so the drifted shape above can be run through it."""
    parsed = yaml.safe_load(fragment_text) or {}
    definition = (parsed.get("services") or {})["corpus-loader"]
    for raw in definition.get("volumes") or []:
        parts = resolve_compose_defaults(str(raw)).split(":")
        return (fragment_dir / parts[0]).resolve()
    raise AssertionError("the fragment mounts nothing")


def test_a_mount_naming_a_different_corpus_fails_this_gate() -> None:
    """The drifted mount, run through the same comparison, must NOT agree."""
    drifted = _corpus_of(_DRIFTED_FRAGMENT, REPO_ROOT / "services" / "ingest")
    assert drifted == (REPO_ROOT / "fixtures" / "real-catalogs").resolve(), (
        "the fixture no longer models the f9dcc1a mount"
    )
    assert drifted != generator().CORPUS.resolve(), (
        "the mount this gate was written for now agrees with the generator's corpus, so "
        "nothing here has been shown to go red"
    )


def test_the_live_fragment_and_the_drifted_one_differ() -> None:
    """And the tree's own fragment is not the drifted one — which is the fix, restated as a
    measurement rather than trusted from a comment."""
    host, _ = mounted_corpus()
    assert host != _corpus_of(_DRIFTED_FRAGMENT, REPO_ROOT / "services" / "ingest")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("../../fixtures/${X:-a}:/app/fixtures/${X:-a}:ro", "../../fixtures/a:/app/fixtures/a:ro"),
        ("${A:-}", ""),
        ("plain", "plain"),
    ],
)
def test_interpolation_resolves_the_way_a_fresh_clone_renders_it(value: str, expected: str) -> None:
    assert resolve_compose_defaults(value) == expected


def test_a_variable_with_no_default_is_refused() -> None:
    """``${VAR}`` renders EMPTY on a clone, which mounts the whole fixtures tree."""
    with pytest.raises(UnresolvableMount):
        resolve_compose_defaults("../../fixtures/${PROXYSHOP_DEMO_CORPUS}")
