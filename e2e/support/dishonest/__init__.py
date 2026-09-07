"""T-084 — the S2 episode harness: does the platform CATCH the scripted dishonest store?

SPEC S2, verbatim:

    The scripted dishonest store (behaviors defined in the human-approved fixture manifest)
    falls below the blacklist threshold within the simulation episode budget and disappears
    from shortlists. **Checkable against the manifest, not the trust engine's own config.**

That last sentence is the design of this package, so it is stated once here and obeyed
everywhere below.

The producer side of S2 already existed: :mod:`sim.dishonest` scripts the adversary out of
``fixtures/manifest.json`` and ``services/sim/tests/test_dishonest.py`` grades it element for
element. Nothing checked the other half — that the platform then *catches* it. This package
is that half, and ``e2e/test_dishonest.py`` is where it is asserted.

Where each number comes from, and why the split is not cosmetic
---------------------------------------------------------------
=================================  ==================================================
the pass/fail numbers              ``fixtures/manifest.json`` — ``blacklist_threshold``,
(what "caught" means)              ``episode_budget``, the roster, which store is the
                                   adversary. Read by :mod:`.criterion`.
the score itself                   ``trust.scoring`` / ``trust.snapshot``, driven by
(the thing under test)             :func:`sim.runner.run_simulation`. Read by
                                   :mod:`.episode`.
the buyer-facing consequence       the served ``POST /auctions`` route on
                                   ``exchange.main:create_app()``. Driven by
                                   :mod:`.served`.
=================================  ==================================================

A test that asked ``trust.scoring`` what its own threshold is and then checked the engine met
it would prove nothing: the grader and the graded would be the same document. So
:func:`.criterion.s2_criterion` reads the human-approved numbers, refuses to default any of
them, and every verdict in ``e2e/test_dishonest.py`` is stated against those. The engine's own
``BLACKLIST_THRESHOLD`` is asserted *equal* to the manifest's — a cross-check with the
authority pointing from the approved document to the code, never the other way.

Offline (C9)
------------
No network egress, no clock, no database, no LLM. The simulation is a pure function of
``(manifest, seed)``; the served half runs three real ASGI apps on loopback ports through
``proxyshop_support.asgi_server.serve`` (D40: port 0, reported back).

This module *drives*; ``e2e/test_dishonest.py`` *asserts* — the same split
``e2e/support/s1/flow.py`` uses, for the same reason: the assertions should read as the
acceptance criteria they are.
"""

from __future__ import annotations

__all__ = [
    "UNWIRED_HOP",
    "DishonestEpisodeRun",
    "EpisodeFacts",
    "ManifestCriterionError",
    "S2Criterion",
    "ServedShortlistError",
    "denied_store_ids",
    "episodes_below",
    "first_episode_below",
    "open_shortlist_over",
    "ranked_store_ids",
    "registry_from_sealed_delistings",
    "run_dishonest_episode",
    "s2_criterion",
    "sealed_delistings",
    "shortlist_store_ids",
    "solicited_store_ids",
]

from .criterion import ManifestCriterionError, S2Criterion, s2_criterion
from .episode import (
    DishonestEpisodeRun,
    EpisodeFacts,
    episodes_below,
    first_episode_below,
    run_dishonest_episode,
    sealed_delistings,
)
from .served import (
    UNWIRED_HOP,
    ServedShortlistError,
    denied_store_ids,
    open_shortlist_over,
    ranked_store_ids,
    registry_from_sealed_delistings,
    shortlist_store_ids,
    solicited_store_ids,
)
