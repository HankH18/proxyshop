"""One module object per file, across both dotted spellings of this tree (T-169).

``apps/exchange/src`` is importable as ``exchange.<mod>`` (through the tracked
``.pkgroot/exchange`` symlink, which is how :mod:`exchange.main` builds the served app) and
as ``apps.exchange.src.<mod>`` (the repo-root path the frozen acceptance suite and this
package's own wiring docstring use). Python will happily execute every file TWICE, once per
spelling, and the duplication is invisible until something in the package holds identity or
state — which this one does:

* ``offer._platform_domains`` — the process-wide registered-domain source
  ``use_registered_domains`` writes — exists once per spelling, so an integrator who wires
  ``apps.exchange.src.accept`` (what ``accept/__init__.py`` documents) leaves
  ``exchange.accept`` (what the served app imports) unwired, and every accept the service
  performs falls back to ``bid["store_domain"]`` — the bidding store's own claim;
* ``offer._UNSET`` is an ``object()`` sentinel, and two of them are never identical, so a
  ``registered_domains`` forwarded across the spellings compares unequal to the sentinel it
  was meant to match and reaches the port *as the platform lookup*.

Measured on this worktree before ``_spellings.bind_package`` was added to this package:
``apps.exchange.src.accept is exchange.accept`` was ``False`` while the two files' inodes
were equal.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import exchange.accept as pkgroot_spelling
import pytest

import apps.exchange.src.accept as repo_root_spelling
from apps.exchange.src.accept._spellings import SPELLINGS, bind_spellings

SUBMODULES = ("_spellings", "gate", "offer", "routes")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: Every order in which the two spellings can arrive. Each runs in a FRESH interpreter,
#: because import order is process state and an in-process test can only ever observe the
#: one order this session happened to take.
IMPORT_ORDERINGS = (
    "import exchange.accept; import apps.exchange.src.accept as p",
    "import apps.exchange.src.accept; import exchange.accept as p",
    "import exchange.accept.routes; import apps.exchange.src.accept.routes as p",
    "import apps.exchange.src.accept.routes; import exchange.accept.routes as p",
    "import exchange.main; import apps.exchange.src.accept as p",
    "from apps.exchange.src.orchestration import accept_offer; import exchange.accept as p",
)


def test_both_spellings_are_the_same_package_object() -> None:
    assert repo_root_spelling is pkgroot_spelling


@pytest.mark.parametrize("name", SUBMODULES)
def test_both_spellings_are_the_same_submodule_object(name: str) -> None:
    assert getattr(repo_root_spelling, name) is getattr(pkgroot_spelling, name)


def test_the_sentinel_that_separates_unset_from_none_is_one_object() -> None:
    """Two ``object()`` sentinels are never identical, and the second one is not a sentinel.

    ``accept(registered_domains=_UNSET)`` means "use the wired default"; anything else means
    "use this". A second sentinel compares unequal to the first, so it stops meaning "unset"
    and starts being forwarded to the port as the platform's lookup — which is a lookup that
    exposes neither ``domain_for`` nor ``__call__``, so every accept through it is refused.
    """
    assert repo_root_spelling.offer._UNSET is pkgroot_spelling.offer._UNSET


def test_a_registry_wired_through_one_spelling_is_visible_through_the_other() -> None:
    """T-169's second half, in the direction the package's own docstring sends an integrator."""
    from exchange.checkout import StaticRegisteredDomains  # noqa: PLC0415

    platform = StaticRegisteredDomains({"store-a": "store-a.example.com"})
    previous = repo_root_spelling.use_registered_domains(platform)
    try:
        assert pkgroot_spelling.platform_registered_domains() is platform
        # ...and back the other way, so this cannot pass on a one-directional alias.
        other = StaticRegisteredDomains({"store-b": "store-b.example.com"})
        pkgroot_spelling.use_registered_domains(other)
        assert repo_root_spelling.platform_registered_domains() is other
    finally:
        repo_root_spelling.use_registered_domains(previous)


def test_both_spellings_resolve_inside_this_worktree() -> None:
    """The venv's ``_proxyshop.pth`` puts a hard-coded checkout on ``sys.path`` for every
    process using it, and has already produced one false green in this repo."""
    for label, module in (
        ("apps.exchange.src.accept", repo_root_spelling),
        ("exchange.accept", pkgroot_spelling),
    ):
        resolved = pathlib.Path(str(module.__file__)).resolve()
        assert REPO_ROOT in resolved.parents, f"{label} resolved to {resolved}, outside {REPO_ROOT}"


@pytest.mark.parametrize("program", IMPORT_ORDERINGS)
def test_identity_holds_whichever_spelling_is_imported_first(program: str) -> None:
    """In-process this session took ONE order; these take the other five, freshly."""
    script = (
        f"{program}\n"
        "import apps.exchange.src.accept as a, exchange.accept as b\n"
        "assert a is b, 'the two spellings are different module objects'\n"
        "assert a.accept is b.accept\n"
        "assert a.offer._UNSET is b.offer._UNSET\n"
        "from exchange.checkout import StaticRegisteredDomains\n"
        "a.use_registered_domains(StaticRegisteredDomains({'s': 's.example.com'}))\n"
        "assert b.platform_registered_domains() is not None, 'the wiring did not cross'\n"
        "print('ok')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("ok")


def test_binding_touches_only_this_trees_two_spellings() -> None:
    """`bind_spellings` is not a general aliasing facility, and this is what says so."""
    import json as unrelated  # noqa: PLC0415

    assert bind_spellings(unrelated) == ()
    assert SPELLINGS == ("exchange", "apps.exchange.src")
