"""Exchange exposure policy (T-034).

The bandit that decides how much of a cluster's shortlist opportunity each store draws.
It adjusts **exposure and exploration only** -- the single published rank formula in
DESIGN is not touched by anything under here.

See :mod:`exchange.policy.bandit` for the model, the fail-closed eligibility rule, and
why the exploration floor is applied before the blacklist rather than after.
"""

from __future__ import annotations

from .bandit import (
    DEFAULT_DRAWS,
    PRIOR_WEIGHT,
    BanditState,
    Posterior,
    exposure,
    initial_state,
    update,
)

__all__ = [
    "DEFAULT_DRAWS",
    "PRIOR_WEIGHT",
    "BanditState",
    "Posterior",
    "exposure",
    "initial_state",
    "update",
]
