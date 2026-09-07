"""Fixtures for the R14 returning-shopper population.

Every name here is prefixed ``pop_``: this directory's ``conftest.py`` hoists every
``_fixtures_*.py`` into one namespace and a name two files both define is poisoned for whichever
test asks for it, so the prefix is what keeps these from colliding with ``_fixtures_sim.py``'s.

The served run is session-scoped because it stands up two real ASGI servers and drives ~24
purchases through them. Anything that needs the run to differ — a different noise rate, a
different seed — builds its own with :func:`pop_run_factory`, which is function-scoped and pays
for a fresh stack each time.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

#: Small enough that a whole suite of served runs stays quick, large enough that the two stores
#: being compared each accumulate more than a handful of answers. The default run is
#: 4 episodes x 4 shoppers = 16 purchases, four per store.
POP_EPISODES = 4
POP_SHOPPERS = 4


@pytest.fixture(scope="session")
def pop_manifest() -> dict[str, Any]:
    """The human-approved manifest, digest chain verified on the way in."""
    from fixtures.manifest import load_manifest

    return load_manifest()


@pytest.fixture(scope="session")
def pop_roster(pop_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The approved store roster, priced from the seeded catalog."""
    from sim.runner import build_roster

    from fixtures.generator import generate

    seed = int(pop_manifest["seed"])
    return build_roster(generate(str(pop_manifest["seed_category"]), seed), pop_manifest)


@pytest.fixture
def pop_run_factory(pop_manifest: dict[str, Any], pop_roster: list[dict[str, Any]]) -> Any:
    """Build a population run against a fresh private stack. ``factory(**overrides)``.

    Yields ``(run, stack)`` so a test can read back the chain the routes really wrote rather
    than trusting the run's own account of what it sent.
    """
    import contextlib

    import httpx
    from seed.local_stack import local_stack
    from seed.population import run_population
    from seed.shoppers import ShopperPolicy

    stack = contextlib.ExitStack()

    def factory(**overrides: Any) -> tuple[Any, Any]:
        services = stack.enter_context(local_stack(pop_roster))
        client = stack.enter_context(httpx.Client(timeout=30.0))
        policy = overrides.pop("policy", None) or ShopperPolicy(
            response_rate=overrides.pop("response_rate", ShopperPolicy().response_rate),
            noise_rate=overrides.pop("noise_rate", ShopperPolicy().noise_rate),
        )
        run = run_population(
            pop_manifest,
            overrides.pop("seed", int(pop_manifest["seed"])),
            buyer_url=services.buyer_url,
            trust_url=services.trust_url,
            client=client,
            policy=policy,
            episodes=overrides.pop("episodes", POP_EPISODES),
            shoppers_per_episode=overrides.pop("shoppers_per_episode", POP_SHOPPERS),
            **overrides,
        )
        return run, services

    with stack:
        yield factory


@pytest.fixture(scope="session")
def pop_served(pop_manifest: dict[str, Any], pop_roster: list[dict[str, Any]]) -> Iterator[Any]:
    """One default population run, and the stack it ran against. Session-scoped."""
    import contextlib

    import httpx
    from seed.local_stack import local_stack
    from seed.population import run_population

    with contextlib.ExitStack() as stack:
        services = stack.enter_context(local_stack(pop_roster))
        client = stack.enter_context(httpx.Client(timeout=30.0))
        run = run_population(
            pop_manifest,
            int(pop_manifest["seed"]),
            buyer_url=services.buyer_url,
            trust_url=services.trust_url,
            client=client,
            episodes=POP_EPISODES,
            shoppers_per_episode=POP_SHOPPERS,
        )
        yield run, services
