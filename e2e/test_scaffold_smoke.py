"""Scaffold smoke test for ``e2e/``.

Orchestrator-owned (T-000) and **frozen**. The e2e lane has no import namespace of its own;
what it needs from the scaffold is that its support directories exist (so the three e2e
tickets can drop scenario data into a directory they own) and that the shared fixtures
reach it — ``e2e/conftest.py`` is a per-directory conftest, and the root fixtures must
still resolve from here.
"""

from __future__ import annotations

from pathlib import Path

from proxyshop_support.clock import EPOCH

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_support_directories_exist() -> None:
    for name in ("s1", "learning", "dishonest"):
        assert (REPO_ROOT / "e2e" / "support" / name).is_dir(), name


def test_root_fixtures_reach_this_directory(manual_clock, llm_double, hash_embedding) -> None:
    """The nine shared fixtures are visible here, not only under `<member>/tests/`."""
    assert manual_clock.now() == EPOCH
    assert llm_double.complete("ping", role="buyer").startswith("double:buyer:")
    assert len(hash_embedding("ping")) == 1024
