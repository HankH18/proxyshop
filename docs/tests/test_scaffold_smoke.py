"""Scaffold smoke test for ``docs/``.

Orchestrator-owned (T-000) and **frozen**. ``docs/tests`` is a pytest testpath, and the
runbook ticket's verify is ``pytest docs/tests/test_runbook.py -q`` — which exits 4, not 5,
if the directory does not exist in a fresh worktree. This test keeps the directory real and
keeps the two Makefile hand-off targets from silently disappearing.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_docs_directories_exist() -> None:
    assert (REPO_ROOT / "docs" / "demo").is_dir()
    assert (REPO_ROOT / "docs" / "tests").is_dir()


def test_makefile_keeps_the_demo_handoff_targets() -> None:
    """`demo-seed` and `e2e-live` exist from day one so no later ticket edits the Makefile."""
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "demo-seed:" in makefile
    assert "e2e-live:" in makefile
    assert "python -m fixtures.seed" in makefile
    assert "docs/demo/e2e_live.sh" in makefile
