"""Scaffold smoke test for ``e2e/``.

Orchestrator-owned (T-000) and **frozen**. The e2e lane has no import namespace of its own;
what it needs from the scaffold is that its support directories exist (so the three e2e
tickets can drop scenario data into a directory they own) and that the shared fixtures
reach it — ``e2e/conftest.py`` is a per-directory conftest, and the root fixtures must
still resolve from here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_sibling_fixture_files_are_auto_discovered(scaffold_fixture_probe) -> None:
    """The `_fixtures_*.py` mechanism ~40 tickets depend on actually resolves.

    `scaffold_fixture_probe` is defined in `e2e/_fixtures_scaffold.py`, not in any conftest.
    If this test errors with "fixture not found", the loader in
    `proxyshop_support.fixture_loader` has stopped working and every ticket that adds
    fixtures its own way will fail confusingly.
    """
    assert scaffold_fixture_probe == "loaded-from-_fixtures_scaffold"


def test_the_socket_guard_blocks_everything_off_loopback() -> None:
    """D19/D3: verification is offline. Loopback stays usable; the rest of the world does not.

    If this ever starts passing a real connection, some ticket's "unit" test is quietly
    reaching the network and the whole offline guarantee is gone.
    """
    import socket

    from pytest_socket import SocketConnectBlockedError

    with pytest.raises(SocketConnectBlockedError):
        socket.create_connection(("10.255.255.1", 80), timeout=0.5)
