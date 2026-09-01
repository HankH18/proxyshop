"""Scaffold smoke test for ``apps/seller-reference``.

Orchestrator-owned (T-000) and **frozen**. Its only job is to make sure this workspace is
*collected and executed* rather than silently empty: B10 notes that `pytest <empty dir>`
exits 5 and the root verify maps that to 0, so without a real test here a broken workspace
would look identical to a green one.

The filename is deliberately not of the form a feature ticket would choose — every ticket
reserves the ``test_<topic>*.py`` prefix for its own topic, and ``scaffold`` is nobody's.
"""

from __future__ import annotations

import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_import_namespace_resolves() -> None:
    """``seller_reference`` imports, and resolves to this member's FLAT ``src/`` directory."""
    module = importlib.import_module("seller_reference")
    assert module.__file__ is not None
    resolved = Path(module.__file__).resolve().parent
    assert resolved == (REPO_ROOT / "apps/seller-reference/src").resolve()


def test_scope_directories_exist() -> None:
    """Every directory a ticket scope names is present, so `pytest <path>` cannot exit 4."""
    for relative in []:
        assert (REPO_ROOT / relative).is_dir(), relative
