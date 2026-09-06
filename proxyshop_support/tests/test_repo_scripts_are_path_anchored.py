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
from pathlib import Path

import pytest

from proxyshop_support.tests._repo_scripts import SCRIPTS, load_repo_script

DECOY_ANSWER = ["THIS-ANSWER-CAME-FROM-THE-DECOY"]
TARGET = "check_verify_contracts"


class _DecoyFinder(importlib.abc.MetaPathFinder):
    """A meta_path finder that claims ``TARGET``. It runs BEFORE ``sys.path`` is consulted."""

    def __init__(self, module: types.ModuleType) -> None:
        self.module = module

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> object:
        if fullname != TARGET:
            return None
        spec = importlib.util.spec_from_loader(fullname, loader=None)
        assert spec is not None
        return spec

    def create_module(self, spec: object) -> types.ModuleType:  # pragma: no cover
        return self.module

    def exec_module(self, module: types.ModuleType) -> None:  # pragma: no cover
        return None


@pytest.fixture
def hijacked(tmp_path: Path):
    """Bind ``TARGET`` to a decoy three independent ways, and undo it all afterwards."""
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
