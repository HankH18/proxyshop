"""``packages.llm`` — the import name the frozen acceptance suite uses.

The implementation lives in the FLAT ``src/`` layout every ticket scope glob requires
(D42), and is importable as ``llm.<module>`` through the tracked ``.pkgroot/llm`` symlink
(see ``packages/llm/pyproject.toml`` for why the symlink exists). This module re-exports
that same surface under ``packages.llm``.

Both names bind to the *same* module objects — this file imports from ``llm`` rather than
re-implementing anything, so ``packages.llm.RecordedLLM is llm.RecordedLLM`` and an
``except llm.errors.UnrecordedPromptError`` written by one ticket catches what another
ticket's ``packages.llm`` double raises.

``.pkgroot`` is on ``sys.path`` for every consumer: the root distribution's editable
install puts it there (``dev-mode-dirs``), and pytest adds it via the root ``pythonpath``
setting.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

try:
    import llm as _llm
    from llm import *  # noqa: F403 - the public surface is exactly llm.__all__
except ImportError as _exc:  # pragma: no cover - only reachable with a broken sys.path
    raise ImportError(
        "packages.llm re-exports the `llm` namespace, which is the tracked symlink "
        "`.pkgroot/llm` -> packages/llm/src (D42's flat layout). `llm` did not import, "
        "which almost always means the repo's `.pkgroot` directory is not on sys.path. "
        "The editable install (`dev-mode-dirs`) and the root pytest `pythonpath` both "
        f"put it there, so run inside the project venv. Original error: {_exc}"
    ) from _exc

__all__ = list(_llm.__all__)


def _bind_submodules() -> None:
    """Make every spelling of a submodule resolve to ONE module object.

    The flat layout leaves ``packages/llm/src/<module>.py`` reachable by two names: as
    ``llm.<module>`` through the ``.pkgroot`` symlink, and — because this package has an
    ``__init__.py`` and ``src/`` has one too — as ``packages.llm.src.<module>``. Left
    alone, Python executes those files a SECOND time under the second name, and a consumer
    that writes ``from packages.llm.src.doubles import RecordedLLM`` holds a different
    class object from the one this module exports: ``except UnrecordedPromptError``
    imported from one path then silently fails to catch what the other path raises.
    Measured, not hypothetical.

    Binding the alternative names turns that second import into a cache hit. Both halves
    matter and both are asserted in ``tests/test_llm_package_surface.py``:

    * ``sys.modules`` — so ``from packages.llm.doubles import X`` finds the canonical one;
    * the **attribute** on this module — so plain ``import packages.llm.doubles`` followed
      by ``packages.llm.doubles.X`` works, which is what the real import machinery does
      and what a ``sys.modules`` entry alone does NOT give you.

    The names are discovered with :func:`pkgutil.iter_modules` rather than hard-coded, so
    a module added to ``src/`` later cannot silently miss the treatment.
    """
    root = sys.modules[__name__]
    sys.modules.setdefault(f"{__name__}.src", _llm)
    root.src = _llm  # type: ignore[attr-defined]
    for info in pkgutil.iter_modules(_llm.__path__):
        module = importlib.import_module(f"llm.{info.name}")
        sys.modules.setdefault(f"{__name__}.{info.name}", module)
        sys.modules.setdefault(f"{__name__}.src.{info.name}", module)
        setattr(root, info.name, module)


_bind_submodules()
