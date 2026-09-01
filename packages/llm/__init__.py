"""``packages.llm`` — the import name the frozen acceptance suite uses.

The implementation lives in the FLAT ``src/`` layout every ticket scope glob requires
(D42), and is importable as ``llm.<module>`` through the tracked ``.pkgroot/llm`` symlink
(see ``packages/llm/pyproject.toml`` for why the symlink exists). This module re-exports
that same surface under ``packages.llm``.

Both names bind to the *same* module objects — this file imports from ``llm`` rather than
re-implementing anything, so ``packages.llm.RecordedLLM is llm.RecordedLLM`` and an
``except llm.errors.UnrecordedPromptError`` written by one ticket catches what another
ticket's ``packages.llm`` double raises. Duplicating the modules under two names would
break exactly that.

``.pkgroot`` is on ``sys.path`` for every consumer: the root distribution's editable
install puts it there (``dev-mode-dirs``), and pytest adds it via the root
``pythonpath`` setting.
"""

from __future__ import annotations

import llm as _llm
from llm import *  # noqa: F403 - the public surface is defined by llm.__all__

__all__ = list(_llm.__all__)
