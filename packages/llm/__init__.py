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

import sys

import llm as _llm
from llm import *  # noqa: F403 - the public surface is exactly llm.__all__

__all__ = list(_llm.__all__)

# --------------------------------------------------------------------------------------
# One class per name, whatever import path a consumer guesses.
#
# The flat layout leaves `packages/llm/src/<module>.py` reachable by two names: as
# `llm.<module>` through the `.pkgroot` symlink, and — because this package has an
# `__init__.py` and `src/` has one too — as `packages.llm.src.<module>`. Left alone,
# Python executes those files a SECOND time under the second name, and a consumer that
# writes `from packages.llm.src.doubles import RecordedLLM` holds a different class object
# from the one this module exports: `except UnrecordedPromptError` imported from one path
# then silently fails to catch what the other path raises. Measured, not hypothetical.
#
# Binding the alternative names to the canonical modules turns that second import into a
# cache hit, so every spelling below resolves to one module object:
#
#     from packages.llm import RecordedLLM
#     from packages.llm.doubles import RecordedLLM
#     from packages.llm.src.doubles import RecordedLLM
#     from llm.doubles import RecordedLLM
# --------------------------------------------------------------------------------------
_SUBMODULES = ("client", "config", "doubles", "errors", "prompting", "recordings")
sys.modules.setdefault(f"{__name__}.src", _llm)
for _name in _SUBMODULES:
    _module = sys.modules[f"llm.{_name}"]
    sys.modules.setdefault(f"{__name__}.{_name}", _module)
    sys.modules.setdefault(f"{__name__}.src.{_name}", _module)
del _name, _module
