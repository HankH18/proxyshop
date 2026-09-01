"""Scaffold smoke test for ``fixtures/``.

Orchestrator-owned (T-000) and **frozen**. ``fixtures`` is the one workspace member with no
``src/`` directory: its thirteen data directories are addressed directly by ticket scope
globs (``fixtures/pages/**``, ``fixtures/golden/**``, ...), and the Makefile's
``demo-seed`` target runs ``python -m fixtures.seed``, i.e. it is imported as a top-level
package from the repo root. This test pins both of those facts.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every directory under fixtures/ that a ticket scope glob names, plus the data
#: directories the seed and golden-set tickets consume.
FIXTURE_DIRS = [
    "manifest",
    "approval",
    "seed",
    "catalog",
    "personas",
    "golden",
    "tests",
    "pages",
    "er",
    "mcp",
    "envelopes",
    "interviews",
    "dialogues",
]


def test_fixtures_is_importable_as_a_top_level_package() -> None:
    """``python -m fixtures.seed`` needs `fixtures` resolvable from the repo root."""
    import fixtures

    assert Path(next(iter(fixtures.__path__))).resolve() == (REPO_ROOT / "fixtures").resolve()


def test_every_fixture_directory_exists() -> None:
    """A missing directory makes a worker's `pytest fixtures/<x>` exit 4, not 5."""
    for name in FIXTURE_DIRS:
        assert (REPO_ROOT / "fixtures" / name).is_dir(), name


def test_fixtures_has_no_src_directory() -> None:
    """Guards the layout decision: moving code under `fixtures/src/` would break every
    `fixtures/<x>/**` scope glob and `python -m fixtures.seed` alike."""
    assert not (REPO_ROOT / "fixtures" / "src").exists()
