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
import json
import os
import pkgutil
import subprocess
import sys
import sysconfig
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


def _child_env() -> dict[str, str]:
    """The environment a spawned child needs in order to reach BOTH ledger spellings.

    T-122. This used to be ``{**os.environ, "PYTHONPATH": str(REPO_ROOT), ...}``, which is
    wrong twice over in the same expression:

    * ``apps.trust.src.ledger`` is reachable from the repo root, but ``trust.ledger`` lives
      under ``.pkgroot`` (a directory of symlinks into each package's ``src``), so a child
      given only the repo root cannot import the very spelling these tests are about; and
    * assigning to ``PYTHONPATH`` *replaces* whatever the parent had rather than extending
      it, so a caller who had already put ``.pkgroot`` on the path lost it here.

    It looked fine only because a developer checkout has ``.venv/.../_proxyshop.pth``, which
    puts both directories on ``sys.path`` regardless of ``PYTHONPATH`` — so the defect was
    invisible until a run where that ``.pth`` did not apply, and then it took ``make verify``
    down with four ``ModuleNotFoundError: No module named 'trust'``. Both directories are
    named explicitly, and anything inherited is appended rather than discarded.
    """
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(REPO_ROOT), str(REPO_ROOT / ".pkgroot"), os.environ.get("PYTHONPATH", "")]
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _run(statements: str) -> subprocess.CompletedProcess[str]:
    """Run ``statements`` in a fresh interpreter that can reach both import spellings.

    A fresh process is the only way to control *which spelling is imported first*: inside
    this pytest session both are long since loaded, so an in-process test can only observe
    the order this session happened to use.
    """
    return subprocess.run(
        [sys.executable, "-c", statements],
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_child_is_handed_pkgroot_and_not_only_the_repo_root() -> None:
    """T-122. The guard for :func:`_child_env`, written so it cannot pass by accident.

    Two halves, and the second is the one that matters. Asserting on the dictionary alone
    would be a test of the literal that was typed one line above it; running the child with
    ``-S`` disables ``site``, so ``_proxyshop.pth`` — the thing that hid the original defect
    on every developer machine — contributes nothing, and the only reason the child can
    import ``trust`` is that this function put ``.pkgroot`` in its environment.
    ``purelib`` is handed back so third-party dependencies still resolve; without it this
    would prove only that ``-S`` hides pydantic.
    """
    entries = _child_env()["PYTHONPATH"].split(os.pathsep)
    assert str(REPO_ROOT) in entries, "the child cannot reach apps.trust.src.ledger"
    assert str(REPO_ROOT / ".pkgroot") in entries, "the child cannot reach trust.ledger"

    env = {
        **_child_env(),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join(
            [str(REPO_ROOT), str(REPO_ROOT / ".pkgroot"), sysconfig.get_paths()["purelib"]]
        ),
    }
    proc = subprocess.run(
        [sys.executable, "-S", "-c", "import trust.ledger as m; print(m.__file__)"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert Path(proc.stdout.strip()).resolve() == (
        REPO_ROOT / "apps" / "trust" / "src" / "ledger" / "__init__.py"
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


# =======================================================================================
# T-126 -- the three properties T-119 left ungraded
# =======================================================================================
# Every test above imports the two spellings one after the other, which is the only order
# a single-threaded test can produce. The binding was written to hold in any order, and it
# did -- but "any order" and "no order at all" are different claims, and only the first was
# ever driven. The three tests below drive the other one, plus the two halves of the
# machinery that no assertion in this file could previously distinguish from absent.


#: A child that imports the two spellings from two threads released at the same instant.
#:
#: Both threads reach ``import_module`` before either can finish, so the eager
#: ``from .canonical import (...)`` at the top of ``__init__.py`` runs TWICE before the
#: ``_bind_submodules()`` at the bottom runs once. Measured with that as the only sequencing
#: (i.e. before T-126), 5 runs out of 5 in this tree: ``canonical``, ``chain`` and ``replay``
#: each came out as two module objects, and one run in five died inside the lazy loader with
#: ``ValueError: module object for 'trust.ledger.migrations' substituted in sys.modules
#: during a lazy load``.
_RACE_PROGRAM = """
import importlib
import pkgutil
import sys
import threading

SPELLINGS = ("trust.ledger", "apps.trust.src.ledger")
released = threading.Barrier(len(SPELLINGS))
failures = []


def first_import(name):
    released.wait()
    try:
        importlib.import_module(name)
    except BaseException as exc:
        failures.append(name + ": " + repr(exc))


threads = [
    threading.Thread(target=first_import, args=(name,), name=name) for name in SPELLINGS
]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=60)

alive = [thread.name for thread in threads if thread.is_alive()]
assert not alive, "the racing first-imports never returned: " + repr(alive)
assert not failures, failures

trust_spelling = sys.modules["trust.ledger"]
apps_spelling = sys.modules["apps.trust.src.ledger"]

names = sorted(info.name for info in pkgutil.iter_modules(trust_spelling.__path__))
assert names, "pkgutil found no submodules under the ledger package"

split = [
    name
    for name in names
    if sys.modules.get("trust.ledger." + name)
    is not sys.modules.get("apps.trust.src.ledger." + name)
]
assert split == [], (
    "a concurrent first import produced TWO module objects for " + repr(split) + ": every "
    "class, exception and module-level constant in those files exists twice, so an "
    "`except` written against one spelling does not catch what the other raises."
)

module_type = type(sys)
for name in names:
    module = sys.modules["trust.ledger." + name]
    for package in (trust_spelling, apps_spelling):
        bound = getattr(package, name, None)
        if isinstance(bound, module_type):
            assert bound is module, (package.__name__, name)

assert trust_spelling.__all__ == apps_spelling.__all__
differing = [
    name
    for name in trust_spelling.__all__
    if getattr(trust_spelling, name) is not getattr(apps_spelling, name)
]
assert differing == [], "exported names differ after a concurrent first import: " + repr(
    differing
)

for producer, catcher in ((trust_spelling, apps_spelling), (apps_spelling, trust_spelling)):
    try:
        producer.canonical_json(float("nan"))
    except catcher.CanonicalisationError:
        pass
    else:
        raise AssertionError(
            "canonical_json(nan) did not raise the other spelling's CanonicalisationError"
        )

print("raced")
"""


@pytest.mark.parametrize("attempt", range(5))
def test_the_binding_holds_when_both_spellings_are_first_imported_concurrently(
    attempt: int,
) -> None:
    """T-126 acceptance 1: the binding raced rather than merely re-ordered.

    ``test_the_binding_does_not_depend_on_which_spelling_is_imported_first`` above proves
    the binding survives either ORDER. It cannot prove it survives NO order: in that test
    one execution is always complete before the other begins, which is precisely the
    assumption the bottom-of-file binding rests on. Here the two first-imports are released
    from a barrier into two threads.

    Repeated rather than run once because a race that fails intermittently is a race that
    passes intermittently, and one green run of a thread test is not evidence. (The defect
    this closes was in fact deterministic in this tree -- 5 of 5 -- but that is a property
    of one interpreter on one machine, not something to encode.)
    """
    proc = _run(_RACE_PROGRAM)
    assert proc.returncode == 0, f"attempt {attempt}:\n{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "raced"


@pytest.mark.parametrize(
    ("opening", "other"),
    [
        ("trust.ledger", "apps.trust.src.ledger"),
        ("apps.trust.src.ledger", "trust.ledger"),
    ],
)
def test_a_plain_submodule_import_leaves_the_attribute_bound_too(opening: str, other: str) -> None:
    """T-126 acceptance 2: the ``setattr`` half of ``_publish``, driven.

    ``_publish`` does two things per spelling, and its docstring says both matter. Only one
    of them was graded. Every other test in this file reaches submodules through
    ``importlib.import_module`` or through ``sys.modules``, and both of those are satisfied
    by the ``sys.modules.setdefault`` half alone -- so deleting the ``setattr`` half left
    all 19 tests here green.

    What it breaks is the statement a consumer actually writes. ``import a.b.c`` is a no-op
    when ``a.b.c`` is already in ``sys.modules``, and the import machinery binds ``c`` on
    ``a.b`` only when it *loads* the module; a pre-seeded ``sys.modules`` entry skips that.
    So ``import apps.trust.src.ledger.store`` succeeds and the very next line --
    ``apps.trust.src.ledger.store.append_event`` -- raises
    ``AttributeError: module 'apps.trust.src.ledger' has no attribute 'store'``. Measured
    verbatim in this tree with the ``setattr`` half removed, in both directions.

    ``store`` is the sharp case on purpose: it is published as a LAZY module object, so
    nothing has executed ``store.py`` at the point the attribute has to be there.
    """
    proc = _run(
        f"import {opening}\n"
        f"import sys\n"
        f"import {other}.store\n"
        f"append_event = {other}.store.append_event\n"
        f"assert callable(append_event), append_event\n"
        f"assert {other}.store is sys.modules['{other}.store']\n"
        f"assert {other}.store is sys.modules['{opening}.store']\n"
        f"print('attribute-bound')\n"
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "attribute-bound"


@pytest.mark.parametrize(
    ("root_on_path", "opening", "other"),
    [
        (REPO_ROOT, "apps.trust.src.ledger", "trust.ledger"),
        (REPO_ROOT / ".pkgroot", "trust.ledger", "apps.trust.src.ledger"),
    ],
    ids=["repo-root only", ".pkgroot only"],
)
def test_the_spelling_fallback_lends_sys_path_a_root_and_takes_it_back(
    root_on_path: Path, opening: str, other: str, tmp_path: Path
) -> None:
    """T-126 acceptance 3: the fallback's side effect on the interpreter, bounded.

    ``_bind_submodules`` puts the other spelling's root on ``sys.path`` when that spelling
    is not importable from the path the interpreter already has. That is load-bearing -- a
    consumer holding only ``.pkgroot`` never creates the ``apps.`` name otherwise, and a
    *later* ``import apps.trust.src.ledger`` would then execute the package a second time
    and rebuild the duplicates this whole file is about.

    It used to be permanent, and that is the part this grades. ``import trust.ledger`` is a
    statement about one package; leaving ``<checkout>`` on ``sys.path`` afterwards silently
    makes the whole repository importable for the rest of the process, and the reverse
    direction makes every member package under ``.pkgroot`` importable. Nothing declared it
    and nothing tested it, so nothing would have noticed code growing to depend on it.

    The child gets exactly ONE of the two roots and runs under ``-S``, so ``_proxyshop.pth``
    -- which puts both on the path in any developer checkout and would make this vacuous --
    contributes nothing. Both halves are asserted: the binding still holds (so the fallback
    really did fire and really was needed), and ``sys.path`` is byte-for-byte what it was.
    """
    missing_root = REPO_ROOT if root_on_path != REPO_ROOT else REPO_ROOT / ".pkgroot"
    env = {
        **_child_env(),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join([str(root_on_path), sysconfig.get_paths()["purelib"]]),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            f"import sys\n"
            f"assert {str(missing_root)!r} not in sys.path, 'the child was handed both roots'\n"
            f"before = list(sys.path)\n"
            f"import {opening}\n"
            f"import {other}\n"
            f"assert sys.modules['{opening}.canonical'] is sys.modules['{other}.canonical'], (\n"
            f"    'the fallback did not bind: this test would be vacuous'\n"
            f")\n"
            f"added = [entry for entry in sys.path if entry not in before]\n"
            f"assert added == [], 'the fallback left entries on sys.path: ' + repr(added)\n"
            f"assert sys.path == before, (before, sys.path)\n"
            f"print('scoped')\n",
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "scoped"


# =======================================================================================
# Wave-5 close-out -- the claim two threads on two names could not have caught
# =======================================================================================
# `test_the_binding_holds_when_both_spellings_are_first_imported_concurrently` releases
# exactly TWO threads, on the two PACKAGE names, and `apps/trust/src/ledger/__init__.py`
# read that as licence to state, as a fact about the design,
#
#     "there is no cycle for `_ModuleLock` to detect".
#
# It is false, and no number of repeats of two threads over two package names can reach the
# input that shows it -- which is the whole shape of the defect: a guard that cannot detect
# a recurrence of the thing it was written about. Both halves are driven below.
#
# Measured in this tree, 40 runs of each shape:
#
#   8 threads, the two package names four times each
#       0 hangs, 0 import errors, 0 split submodules, 0 disagreeing `__all__`
#
#   14 threads, the two package names PLUS every submodule under both
#       0 hangs, but an import failure in 40/40 (225 `_DeadlockError`, 13 `ImportError`,
#       4 `KeyError`), and SPLIT SUBMODULES in 14/40
#
# The cycle is CPython's rather than this package's, and it predates T-126: `import pkg.sub`
# acquires the CHILD module lock and imports the parent while still holding it, and the
# parent's own eager `from .canonical import ...` then waits on that child. Raced with ONE
# spelling and no `apps.` name mentioned at all -- `trust.ledger` against four of its own
# submodules -- it is 10 runs out of 10 with the T-126 hook present and 10 out of 10 with
# the hook disabled. Nothing in this package creates it and nothing in this package can
# close it without giving up the eager re-exports, so the two tests below split the claim
# into the part that is guaranteed and the part that is a documented boundary.


def _wide_race_program(targets: tuple[str, ...]) -> str:
    """:data:`_RACE_PROGRAM` over an arbitrary thread roster.

    The program's own assertions are reused verbatim -- only the roster line changes -- so
    the widened race grades exactly the same invariants as the two-thread one and cannot
    drift away from it.
    """
    roster = 'SPELLINGS = ("trust.ledger", "apps.trust.src.ledger")'
    assert roster in _RACE_PROGRAM, "the race program's roster line moved; this test is blind"
    return _RACE_PROGRAM.replace(roster, f"SPELLINGS = {targets!r}", 1)


@pytest.mark.parametrize("per_spelling", [2, 3, 4], ids=lambda n: f"{2 * n}-threads")
@pytest.mark.parametrize("attempt", range(3))
def test_the_binding_holds_when_more_than_two_threads_race_the_two_spellings(
    per_spelling: int, attempt: int
) -> None:
    """The sequencing hook's guarantee, past the pair of threads it was written against.

    Two threads is the minimum that races at all, and a minimum is not a property. Here the
    same barrier releases four, six and eight first-imports over the two package names, and
    the assertions are :data:`_RACE_PROGRAM`'s own: one module object per submodule under
    both spellings, the package attributes bound to it, ``__all__`` identical, and each
    spelling's ``CanonicalisationError`` catching what the other raises.

    This is the half that IS guaranteed, and it is graded rather than asserted: measured
    clean 40/40 at eight threads, and with the T-126 defect re-applied -- the body of
    ``_sequence_behind_the_primary_spelling`` replaced by a bare ``return``, so the
    secondary no longer waits for the primary -- all 9 of these turn red (alongside all 5
    of the two-thread test's).
    """
    proc = _run(_wide_race_program(tuple(SPELLINGS) * per_spelling))
    assert proc.returncode == 0, f"attempt {attempt}:\n{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "raced"


#: The roster the two-thread test cannot reach: both package names, plus every submodule
#: under both spellings. Discovered from :data:`SUBMODULES` rather than listed, so a module
#: added to the directory later is raced too.
SUBMODULE_RACE_TARGETS: tuple[str, ...] = (
    *SPELLINGS,
    *(f"{spelling}.{name}" for name in SUBMODULES for spelling in SPELLINGS),
)

_SUBMODULE_RACE_PROGRAM = """
import importlib
import json
import sys
import threading

TARGETS = json.loads({targets})
released = threading.Barrier(len(TARGETS))
raised = []


def first_import(name):
    released.wait()
    try:
        importlib.import_module(name)
    except BaseException as exc:
        raised.append(type(exc).__name__)


threads = [threading.Thread(target=first_import, args=(t,), name=t) for t in TARGETS]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=45)

alive = sorted(thread.name for thread in threads if thread.is_alive())
both_importable = "trust.ledger" in sys.modules and "apps.trust.src.ledger" in sys.modules
all_equal = both_importable and (
    sys.modules["trust.ledger"].__all__ == sys.modules["apps.trust.src.ledger"].__all__
)
print(json.dumps({{
    "alive": alive,
    "raised": sorted(set(raised)),
    "both_importable": both_importable,
    "all_equal": all_equal,
}}))
"""


@pytest.mark.parametrize("attempt", range(3))
def test_racing_a_submodule_name_is_a_detected_cycle_and_never_a_hang(attempt: int) -> None:
    """The boundary, stated as the property that actually holds there.

    Racing ``import trust.ledger`` against ``import trust.ledger.canonical`` is a genuine
    import cycle -- ``import pkg.sub`` holds the child's module lock while it imports the
    parent, and the parent's eager ``from .canonical import ...`` waits on that child --
    and this file used to claim no such cycle existed. It does.

    ONE thing is asserted, because one thing is what measurement supports: **no thread
    hangs.** ``_ModuleLock`` sees the cycle and breaks it, so the interpreter fails loudly
    instead of wedging -- 0 hangs in 40 runs of this roster, plus every run of every other
    probe written against it.

    Everything else about this race is recorded rather than asserted, and the reason is
    worth keeping: the first draft of this test DID assert the other two invariants the
    same 40 runs showed clean -- that both package names end up in ``sys.modules`` and that
    their ``__all__`` agree -- and one ``scripts/verify.sh check`` later, under a full
    parallel suite, ``both_importable`` came back False and the gate went red. 40 clean runs
    is not an invariant; it is 40 clean runs. What is actually true here:

    * split submodules in 14 runs of 40 -- ``sys.modules['trust.ledger.X']`` and
      ``sys.modules['apps.trust.src.ledger.X']`` holding different objects, because
      ``_lock_unlock_module`` swallows ``_DeadlockError`` and hands back a partially
      initialised module;
    * an import failure in 40 of 40 (225 ``_DeadlockError``, 13 ``ImportError``, 4
      ``KeyError``);
    * and, at least once under load, one spelling missing from ``sys.modules`` entirely.

    So this is a documented BOUNDARY, not a guarantee: do not first-import this package by
    dotted submodule name from several threads at once. Asserting a clean binding here
    would be asserting something measured false.

    Honesty about the one assertion's reach: the mutation it is aimed at -- replacing the
    hook's ``importlib.import_module`` wait, which the import machinery can see into, with
    a spin on a flag it cannot -- was driven, and this test did NOT distinguish it on this
    roster (6 runs, 0 hangs, ``_DeadlockError`` every time, same as shipped). Treat it as a
    measured floor, not as a guard proven to catch a specific edit. The test with teeth for
    the sequencing hook is the one above: delete the hook's wait and all 9 of the 4-, 6- and
    8-thread package-name races go red.
    """
    proc = _run(
        _SUBMODULE_RACE_PROGRAM.format(targets=repr(json.dumps(list(SUBMODULE_RACE_TARGETS))))
    )
    assert proc.returncode == 0, f"attempt {attempt}:\n{proc.stdout}\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert result["alive"] == [], (
        f"first-importing the ledger by submodule name from {len(SUBMODULE_RACE_TARGETS)} "
        f"threads HUNG in {result['alive']}. The cycle is supposed to be detected and "
        f"broken by `_ModuleLock`; a wait the import machinery cannot see into (a plain "
        f"threading.Lock or Event around the sequencing hook) turns it into a real deadlock."
    )
