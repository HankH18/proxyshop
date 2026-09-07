"""Scaffold smoke test for ``packages/verification``.

Orchestrator-owned (T-000) and **frozen**. Its only job is to make sure this workspace is
*collected and executed* rather than silently empty: B10 notes that `pytest <empty dir>`
exits 5 and the root verify maps that to 0, so without a real test here a broken workspace
would look identical to a green one.

The filename is deliberately not of the form a feature ticket would choose — every ticket
reserves the ``test_<topic>*.py`` prefix for its own topic, and ``scaffold`` is nobody's.

Other members carry a ``test_scope_directories_exist`` beside this one, asserting that every
directory their ticket scopes name is present. This member had one whose list was ``[]``, so
its body never ran and it passed against any tree at all. It was removed rather than kept as a
green tick with nothing behind it. No ticket scoped to ``packages/verification`` names a
directory outside ``src/`` and ``tests/``; if one starts to, add the test back with that
directory in the list — see ``packages/llm/tests/test_scaffold_smoke.py`` for the live shape.
"""

from __future__ import annotations

import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_import_namespace_resolves() -> None:
    """``claim_verification`` imports, and resolves to this member's FLAT ``src/`` directory."""
    module = importlib.import_module("claim_verification")
    assert module.__file__ is not None
    resolved = Path(module.__file__).resolve().parent
    assert resolved == (REPO_ROOT / "packages/verification/src").resolve()
