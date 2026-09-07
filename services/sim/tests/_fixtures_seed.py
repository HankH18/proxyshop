"""Fixtures for the stored seed corpus and the live-switch seam.

Every name here is prefixed ``seedfx_`` for the reason ``_fixtures_population.py`` states:
this directory's ``conftest.py`` hoists every ``_fixtures_*.py`` into one namespace, and a
name two files both define is poisoned for whichever test asks for it.

The artifact fixture is session-scoped and is written from the population run
``_fixtures_population.py`` already pays for, rather than driving a second one: standing up
two ASGI servers and running ~16 purchases twice would double the suite's cost to store bytes
that are a pure function of what the first run produced.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(scope="session")
def seedfx_artifact(
    pop_served: tuple[Any, Any],
    pop_manifest: dict[str, Any],
    pop_roster: list[dict[str, Any]],
    tmp_path_factory: pytest.TempPathFactory,
) -> Any:
    """One population run, stored to a scratch directory and loaded back digest-verified."""
    from seed.store import load, write

    run, services = pop_served
    root = tmp_path_factory.mktemp("seed-artifact")
    write(
        root,
        run=run,
        events=list(services.event_store.read()),
        roster=pop_roster,
        manifest=pop_manifest,
        producer="python -m seed run (suite)",
        episodes=len(run.postures),
    )
    return load(root)


@pytest.fixture
def seedfx_stack(pop_roster: list[dict[str, Any]]) -> Iterator[Any]:
    """A bare buyer + trust stack on loopback, with no population driven through it.

    For the seam tests, which need the two served doors and deliberately nothing else: a run
    already in progress would leave the chain and the once-per-order book carrying answers
    those tests did not write.
    """
    from seed.local_stack import local_stack

    with local_stack(pop_roster) as services:
        yield services


@pytest.fixture(scope="session")
def seedfx_repo_root() -> Path:
    """The checkout root, from the package under test rather than from this file's depth."""
    from seed.store import repo_root

    return repo_root()
