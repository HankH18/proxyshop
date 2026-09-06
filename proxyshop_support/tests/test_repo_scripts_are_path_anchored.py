"""T-252's class, measured: a test that grades a FROZEN script must name it by PATH.

The recorded T-252 site — ``test_fixture_loader.py:114``, the ``subprocess.run`` that grades
a scratch tree — is REFUTED, and the refutation is recorded in the lane report rather than
here because a test cannot usefully assert that a defect is absent. In short: that child
gets the scratch dir at ``sys.path[0]`` twice over (``python -m`` puts cwd there, and
pytest's prepend import mode re-inserts rootdir there even under ``PYTHONSAFEPATH=1``),
the fixture loader it drives uses ``spec_from_file_location`` under a namespaced name, and
the one module name that DOES collide with the checkout — ``conftest`` — still resolves to
the scratch copy. Measured with a decoy checkout ahead of the repo root on ``PYTHONPATH``.

What was live is the same class 74 lines further down: two tests grading the frozen
``scripts/check_verify_contracts.py`` obtained it with ``sys.path.insert(0, scripts)`` plus
a bare ``import check_verify_contracts``. That idiom does not name a file, it names a
string, and three things beat the inserted path — a pre-bound ``sys.modules`` entry, a
``sys.meta_path`` finder (which runs before ``sys.path`` is consulted at all), and a decoy
at another position 0. Measured: the tests graded the decoy and reported its answer.

This file pins the repair behaviourally. It never reads source text: it hijacks the bare
name three ways in-process and asserts on the ``__file__`` the loader actually resolved and
on the answer the loaded module actually gives.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import sys
import types
from collections.abc import Sequence
from importlib.machinery import ModuleSpec
from pathlib import Path

import pytest

from proxyshop_support.tests import _repo_scripts
from proxyshop_support.tests._repo_scripts import (
    MODULE_PREFIX,
    REPO_ROOT,
    SCRIPTS,
    load_repo_script,
)

DECOY_ANSWER = ["THIS-ANSWER-CAME-FROM-THE-DECOY"]
TARGET = "check_verify_contracts"


class _DecoyFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """A meta_path finder AND loader that claims ``TARGET``.

    This is the layer that makes the difference between a real repair and a cosmetic one.
    A ``sys.modules`` purge plus ``sys.path.insert(0, ...)`` defeats the other two hijacks
    but NOT this one: ``sys.meta_path`` is consulted before ``sys.path`` is looked at at
    all, so the only thing that survives it is naming the file.

    Its signatures match ``MetaPathFinderProtocol`` exactly. The first draft typed them
    ``object`` and mypy — not any test — caught that it therefore was not a finder at all.
    """

    def __init__(self, module: types.ModuleType) -> None:
        self.module = module

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: types.ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname != TARGET:
            return None
        return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec: ModuleSpec) -> types.ModuleType:
        return self.module

    def exec_module(self, module: types.ModuleType) -> None:
        return None


@pytest.fixture
def hijacked(tmp_path: Path):
    """Bind ``TARGET`` to a decoy three independent ways, and undo it all afterwards.

    **What "undo" means here, stated so nobody reads more into it.** The teardown restores
    ``sys.meta_path`` and ``sys.path`` by destructive slice assignment from the snapshot
    taken at setup (``sys.meta_path[:] = saved_meta``), so it does not remove the decoy — it
    REPLACES the whole list with the pre-test one. A finder or path entry that some other
    code legitimately added *during* the test would be discarded with it, silently.

    That is correct for what this file does — nothing here adds one; the only ``sys.path``
    traffic is ``test_t252_the_hijack_is_armed`` pushing and popping ``SCRIPTS`` inside its
    own ``try/finally`` — and the alternative (search
    for the decoy and remove just that) has its own failure mode: it leaks the hijack if the
    object identity moves. It is written down rather than repaired because the repair is a
    behaviour change nothing currently needs.
    """
    decoy = types.ModuleType(TARGET)
    decoy.__file__ = str(tmp_path / "decoy.py")
    decoy._fixture_names = lambda _path: list(DECOY_ANSWER)  # type: ignore[attr-defined]

    (tmp_path / f"{TARGET}.py").write_text(
        f"def _fixture_names(path):\n    return {DECOY_ANSWER!r}\n", encoding="utf-8"
    )

    saved_modules = sys.modules.get(TARGET)
    saved_path = list(sys.path)
    saved_meta = list(sys.meta_path)
    sys.modules[TARGET] = decoy
    sys.path.insert(0, str(tmp_path))
    sys.meta_path.insert(0, _DecoyFinder(decoy))
    try:
        yield tmp_path
    finally:
        sys.meta_path[:] = saved_meta
        sys.path[:] = saved_path
        if saved_modules is None:
            sys.modules.pop(TARGET, None)
        else:
            sys.modules[TARGET] = saved_modules


def test_t252_the_hijack_is_armed(hijacked: Path) -> None:
    """The control: prove the hijack BITES, or the next test passes for the wrong reason.

    This is the whole difference between "the loader is path-anchored" and "nothing was
    trying to take the name". It deliberately performs the HEAD-shaped idiom — insert the
    scripts directory at ``sys.path[0]``, then a bare import — and asserts it loses.
    """
    sys.path.insert(0, str(SCRIPTS))
    try:
        import check_verify_contracts as bare_name  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    assert bare_name._fixture_names(Path("anything.py")) == DECOY_ANSWER, (
        "the decoy did not win the bare-name import, so this file's hijack has stopped "
        "working and the assertions below prove nothing"
    )

    # …and again with the sys.modules entry gone, so the META_PATH layer is what wins.
    # Without this the fixture's third hijack would be untested decoration, and a repair
    # that merely purged sys.modules before re-importing would look sufficient when it is
    # not: meta_path is consulted before sys.path is read.
    del sys.modules[TARGET]
    sys.path.insert(0, str(SCRIPTS))
    try:
        import check_verify_contracts as still_decoy  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    assert still_decoy._fixture_names(Path("anything.py")) == DECOY_ANSWER, (
        "a sys.modules purge plus `sys.path.insert(0, scripts)` reached the real script, "
        "so the meta_path hijack is not biting and this file over-claims what it proves"
    )

    assert (SCRIPTS / f"{TARGET}.py").is_file(), (
        f"{SCRIPTS / f'{TARGET}.py'} does not exist, so there is no real script to grade"
    )


def test_t252_the_loader_resolves_the_frozen_script_through_every_hijack(
    hijacked: Path,
) -> None:
    """Same three hijacks, path-anchored load: the real frozen file, every time."""
    gate = load_repo_script(TARGET)
    assert Path(gate.__file__ or "") == SCRIPTS / f"{TARGET}.py", (
        f"load_repo_script resolved {gate.__file__!r}, not the frozen {SCRIPTS / f'{TARGET}.py'}"
    )
    assert gate._fixture_names(SCRIPTS / f"{TARGET}.py") != DECOY_ANSWER, (
        "the loaded module answered with the decoy's answer, so the __file__ above is not "
        "the module actually being called"
    )
    assert sys.modules.get(TARGET) is not gate, (
        "the loader bound itself to the BARE name, so it now competes for exactly the "
        "identifier the whole point was to stop depending on"
    )


def test_t252_a_missing_script_is_a_finding_not_a_fallback() -> None:
    """No silent fallback to a bare import: that is how the defect gets back in."""
    with pytest.raises(FileNotFoundError):
        load_repo_script("no_such_frozen_script_exists")


# =====================================================================================
# Containment: the name is interpolated into a path and the result is EXECUTED
# =====================================================================================


def test_the_traversal_escape_is_armed() -> None:
    """The control. Without it the test below could pass because there is nothing to reach.

    ``load_repo_script`` builds ``SCRIPTS / f"{name}.py"`` and hands the result to
    ``spec_from_file_location`` + ``exec_module``. ``Path`` does not normalise ``..``, and
    ``Path.is_file()`` asks the OS, which does — so ``scripts/../conftest.py`` is a real,
    readable file and the pre-check loader loaded and RAN it. This asserts that the target
    of that escape genuinely exists, so the refusal graded below is refusing something.
    """
    escape = SCRIPTS / ".." / "conftest.py"
    assert escape.is_file(), (
        f"{escape} is not a file, so the traversal below reaches nothing and the test "
        f"after it would pass vacuously"
    )
    assert escape.resolve() == (REPO_ROOT / "conftest.py").resolve(), (
        f"{escape} does not resolve to the root conftest ({REPO_ROOT / 'conftest.py'}), so "
        f"this control no longer describes the escape it is arming"
    )
    assert escape.resolve().parent != SCRIPTS.resolve(), (
        "the escape target lands inside scripts/ after all; there is nothing to contain"
    )


@pytest.mark.parametrize(
    "name",
    [
        "../conftest",  # the measured escape: loads AND EXECUTES the root conftest
        "../../conftest",
        "subdir/thing",
        "./check_verify_contracts",
        "",
        ".",
        "..",
    ],
)
def test_a_name_that_is_not_a_bare_stem_is_refused_before_anything_is_executed(
    name: str,
) -> None:
    """A traversing name is arbitrary code execution, not a bad lookup.

    The refusal is a ``ValueError`` and not a ``FileNotFoundError`` on purpose: the file may
    very well exist. What is wrong is that it is not the file this helper promises, and the
    caller must see the difference between "your frozen script is missing" (a finding, per
    the test above) and "you asked for something outside the frozen directory".

    Also asserted: nothing was bound. A refusal that had already run ``exec_module`` would
    leave the module in ``sys.modules`` under its namespaced name, and the raise would be
    decoration over a side effect that already happened.
    """
    before = {key for key in sys.modules if key.startswith(MODULE_PREFIX)}
    with pytest.raises(ValueError) as caught:
        load_repo_script(name)
    assert repr(name) in str(caught.value), (
        f"the refusal message does not name the input {name!r}, so a caller cannot tell "
        f"which argument was rejected: {caught.value}"
    )
    after = {key for key in sys.modules if key.startswith(MODULE_PREFIX)}
    assert after == before, (
        f"refusing {name!r} still registered {sorted(after - before)} in sys.modules, so "
        f"the module was executed before the check ran"
    )


def test_a_bare_stem_that_resolves_out_of_the_directory_is_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SECOND containment condition, which the first one hides.

    ``load_repo_script`` refuses on two independent grounds — the stem must be one path
    component, and the file must resolve directly inside ``SCRIPTS``. Every name in the
    parametrised test above is caught by the first, so without this the second would be
    prose: measured with ``--cov-branch``, its ``raise`` was the one unexecuted statement
    in the module and no caller anywhere in the repo could reach it.

    A symlink is the only way a BARE stem escapes, and planting one in the real frozen
    ``scripts/`` from a test is a worse idea than the check is worth. So ``SCRIPTS`` is
    redirected at a scratch directory instead: same code path, same two ``resolve()``
    calls, no mutation of the checkout.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.py").write_text("SMUGGLED = True\n", encoding="utf-8")

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "decoy.py").symlink_to(outside / "elsewhere.py")
    monkeypatch.setattr(_repo_scripts, "SCRIPTS", scripts)

    # Armed: the stem IS a single component and the file IS readable, so the first
    # condition passes and `is_file()` would have said yes. Only containment stops it.
    assert "decoy" == Path("decoy").name
    assert (scripts / "decoy.py").is_file()

    with pytest.raises(ValueError) as caught:
        _repo_scripts.load_repo_script("decoy")
    assert "resolves to" in str(caught.value), (
        f"the refusal came from the component check, not the containment check, so the "
        f"second condition is still ungraded: {caught.value}"
    )
    assert f"{MODULE_PREFIX}.decoy" not in sys.modules, (
        "the smuggled module was executed before the containment check ran"
    )

    # And the same redirect still LOADS a real file that stays inside the directory —
    # otherwise this test would pass against a check that refused everything.
    (scripts / "honest.py").write_text("HONEST = True\n", encoding="utf-8")
    loaded = _repo_scripts.load_repo_script("honest")
    assert loaded.HONEST is True
    del sys.modules[f"{MODULE_PREFIX}.honest"]


def test_the_legitimate_stem_still_loads_after_the_containment_check() -> None:
    """The containment check must not cost the thing the helper exists for.

    Paired with the parametrised refusal above: a check that refused everything would
    satisfy that test and break the loader, and this is what tells the two apart.
    """
    gate = load_repo_script(TARGET)
    assert Path(gate.__file__ or "") == SCRIPTS / f"{TARGET}.py", (
        f"load_repo_script({TARGET!r}) resolved {gate.__file__!r} rather than the frozen "
        f"{SCRIPTS / f'{TARGET}.py'}"
    )
