"""T-119: the ledger is ONE module object under both of its import spellings.

``.pkgroot/trust`` is a tracked symlink to ``apps/trust/src``, so ``apps/trust/src/ledger``
is reachable under two dotted names and Python executes its files twice unless something
stops it:

* ``trust.ledger`` -- how every member package reaches the ledger;
* ``apps.trust.src.ledger`` -- how the frozen acceptance suite and this directory's own
  tests reach it.

Measured before T-119 landed, in one interpreter::

    trust.ledger.canonical is apps.trust.src.ledger.canonical                     -> False
    trust.ledger.canonical.CanonicalisationError
        is apps.trust.src.ledger.canonical.CanonicalisationError                  -> False

Two class objects with one name is the failure that does not announce itself: a
``try: ... except CanonicalisationError`` written against one spelling does not catch what
the other raises, so a *refusal* the ledger issued on purpose surfaces as an unhandled
crash in the caller. D16 makes this package the single source of hashing truth for the
whole repo, which is exactly the position in which two copies is worst.

``packages/llm`` solved the same problem with ``_bind_submodules`` (T-014, and
``packages/llm/tests/test_llm_package_surface.py`` is the sibling of this file). The tests
here pin the ported version, plus the two properties the port had to preserve that llm's
version does not have to: the ``replay`` name is a function rather than the submodule, and
importing the package still pulls in neither psycopg nor redis.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest
import trust.ledger as trust_spelling

import apps.trust.src.ledger as apps_spelling

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The two names this package answers to, longest-lived first.
SPELLINGS = ("trust.ledger", "apps.trust.src.ledger")

#: Discovered rather than listed. A hard-coded tuple here would agree with the hard-coded
#: tuple in the package and neither would notice a module added to the directory later.
SUBMODULES = tuple(sorted(info.name for info in pkgutil.iter_modules(trust_spelling.__path__)))


def _run(statements: str) -> subprocess.CompletedProcess[str]:
    """Run ``statements`` in a fresh interpreter with only the repo root added to the path.

    A fresh process is the only way to control *which spelling is imported first*: inside
    this pytest session both are long since loaded, so an in-process test can only observe
    the order this session happened to use.
    """
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-c", statements],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_two_spellings_are_the_same_file() -> None:
    """The premise. If these ever diverge, every assertion below is about nothing."""
    assert SUBMODULES, "pkgutil found no modules under the ledger package"
    assert Path(trust_spelling.__file__ or "").resolve() == (
        REPO_ROOT / "apps" / "trust" / "src" / "ledger" / "__init__.py"
    )
    assert Path(apps_spelling.__file__ or "").resolve() == (
        Path(trust_spelling.__file__ or "").resolve()
    )


@pytest.mark.parametrize("name", SUBMODULES)
def test_every_submodule_is_one_object_under_both_spellings(name: str) -> None:
    """T-119 acceptance 1, for every module in the package rather than for a chosen few.

    ``importlib.import_module`` rather than attribute access on purpose: the attribute
    could be bound correctly while a plain ``import apps.trust.src.ledger.store`` still
    executed the file a second time, and it is the plain import that a consumer writes.
    """
    modules = {spelling: importlib.import_module(f"{spelling}.{name}") for spelling in SPELLINGS}
    first, second = (modules[spelling] for spelling in SPELLINGS)
    assert first is second, (
        f"trust.ledger.{name} and apps.trust.src.ledger.{name} are different module "
        f"objects ({first!r} vs {second!r}): every class, exception and module-level "
        f"constant in {name}.py exists twice, and an `except` written against one spelling "
        f"does not catch what the other raises."
    )
    assert sys.modules[f"trust.ledger.{name}"] is sys.modules[f"apps.trust.src.ledger.{name}"]


def test_the_canonicalisation_error_is_one_class_under_both_spellings() -> None:
    """T-119 acceptance 2, first half: the class object itself."""
    assert trust_spelling.CanonicalisationError is apps_spelling.CanonicalisationError
    assert (
        importlib.import_module("trust.ledger.canonical").CanonicalisationError
        is importlib.import_module("apps.trust.src.ledger.canonical").CanonicalisationError
    )


def test_a_cross_spelling_except_actually_catches() -> None:
    """T-119 acceptance 2, second half -- the consequence, driven rather than asserted.

    Identity of two class objects is the mechanism; *this* is what it buys, and it is what
    silently failed before: the ledger refuses a NaN, the caller catches the refusal it was
    told to catch, and the refusal goes past it anyway.
    """
    raised = 0
    for producer, catcher in ((trust_spelling, apps_spelling), (apps_spelling, trust_spelling)):
        try:
            producer.canonical_json(float("nan"))
        except catcher.CanonicalisationError:
            raised += 1
        else:  # pragma: no cover - only reachable if the canonicaliser stopped refusing
            raise AssertionError("canonical_json(nan) did not raise at all")
    assert raised == 2


def test_every_exported_name_is_the_same_object_under_both_spellings() -> None:
    """``__all__`` is the promise this package makes; both spellings must keep the same one.

    This walks the lazy names too (``append_event``, ``apply_migrations``, ...), so the
    PEP-562 ``__getattr__`` path is bound as well as the eagerly imported one.
    """
    assert trust_spelling.__all__ == apps_spelling.__all__
    differing = [
        name
        for name in trust_spelling.__all__
        if getattr(trust_spelling, name) is not getattr(apps_spelling, name)
    ]
    assert differing == [], differing


def test_replay_is_still_the_function_and_not_the_submodule() -> None:
    """The one name that is both a submodule and an export, under both spellings.

    Binding submodules as attributes of the package would have silently replaced the
    ``replay`` FUNCTION with ``replay.py`` -- ``from apps.trust.src.ledger import replay``
    is in the frozen acceptance suite, and it means the function.
    """
    for package in (trust_spelling, apps_spelling):
        assert callable(package.replay), package.__name__
        assert package.replay is not sys.modules[f"{package.__name__}.replay"]
    assert trust_spelling.replay is apps_spelling.replay
    assert trust_spelling.observations_from_events is apps_spelling.observations_from_events


@pytest.mark.parametrize(
    "opening_import",
    [
        "trust.ledger",
        "apps.trust.src.ledger",
        "trust.ledger.store",
        "apps.trust.src.ledger.store",
        "trust.ledger.canonical",
        "apps.trust.src.ledger.canonical",
    ],
)
def test_the_binding_does_not_depend_on_which_spelling_is_imported_first(
    opening_import: str,
) -> None:
    """Whichever name a consumer reaches for first, the other resolves to what it created.

    ``store`` is deliberately in the matrix: it is one of the three modules the package
    loads lazily, so it is the one a naive port would leave unbound -- the eager loop
    ``packages/llm`` uses cannot import it without dragging psycopg into every consumer of
    the canonicaliser, and the ``__getattr__`` path that replaces it is only reached by
    consumers who go *through* the package rather than importing the submodule directly.
    """
    submodules = ", ".join(repr(name) for name in SUBMODULES)
    proc = _run(
        f"import importlib\n"
        f"importlib.import_module({opening_import!r})\n"
        f"for mod in ({submodules},):\n"
        f"    x = importlib.import_module('trust.ledger.' + mod)\n"
        f"    y = importlib.import_module('apps.trust.src.ledger.' + mod)\n"
        f"    assert x is y, (mod, x, y)\n"
        f"print('bound')\n"
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "bound"


def test_importing_the_ledger_still_pulls_in_no_database_driver() -> None:
    """The invariant the binding had to preserve, not a new one.

    ``.errors`` imports psycopg **and** redis, ``.store`` and ``.migrations`` import
    psycopg, and the package's ``__getattr__`` exists so that T-060's in-memory event store
    can import the canonicaliser, the sealer and the verifier with neither installed. The
    obvious way to bind every spelling -- the eager ``importlib.import_module`` loop
    ``packages/llm`` uses -- would have quietly ended that. A fresh interpreter is the only
    place this is observable; by the time this session runs, psycopg is long loaded.
    """
    proc = _run(
        "import sys\n"
        "import trust.ledger\n"
        "import apps.trust.src.ledger\n"
        "loaded = sorted(m for m in ('psycopg', 'redis') if m in sys.modules)\n"
        "assert loaded == [], loaded\n"
        "assert 'trust.ledger.store' in sys.modules, 'the lazy submodule was never bound'\n"
        "assert sys.modules['trust.ledger.store'] "
        "    is sys.modules['apps.trust.src.ledger.store']\n"
        "trust.ledger.append_event\n"
        "assert 'psycopg' in sys.modules, 'touching append_event must load the driver'\n"
        "print('lazy')\n"
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "lazy"
