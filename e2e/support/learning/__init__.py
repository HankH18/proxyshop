"""The two learning loops S4 grades, each driven through the door it is served behind (T-083).

SPEC S4, verbatim::

    In simulation, shifting a store's conversion outcomes reorders shortlists for the
    affected intent cluster (R16), and a store agent's discount depth distribution shifts
    in the direction of its own win/loss record (R17). Checkable by simulation assertions
    with fixed seeds.

Two properties, two owners, two served doors — and this package drives both. ``e2e/test_learning.py``
asserts; nothing here contains an assertion about the product, so the criteria are readable in one
file as the criteria they are.

======  ==========================================  ==========================================
R16     ``POST /internal/outcomes`` on the exchange  :mod:`e2e.support.learning.exchange_policy`
R17     ``POST /v1/bid-requests`` on a store agent   :mod:`e2e.support.learning.store_policy`
======  ==========================================  ==========================================

Both apps are the real ones — ``exchange.main.create_app`` and ``store_agent.main.create_app``,
mounted routers and all — reached over ASGI through ``TestClient``. No socket leaves the process,
no LLM is called, no clock is read and no database is touched, so the whole of S4 is checkable
offline (C9) as one ordinary pytest run.

What this harness measures, and what it deliberately does not
--------------------------------------------------------------
The exchange learns from conversion outcomes through a served, published door, and the posterior
that door writes really does reorder the cluster's exposure — that is what
:mod:`~e2e.support.learning.exchange_policy` measures, end to end, through HTTP.

This section used to go on to say that the consumer on the other side did not exist anywhere in
the tree — that ``exchange.policy.bandit.exposure`` had no production call site and
``GET /auctions/{auction_id}/shortlist`` was ranked by ``exchange.ranking`` alone. That gap is
closed: ``exchange.policy.exploration.exposure_shares`` reads ``bandit.exposure``, and
``exchange.ranking.serving`` imports it (``apps/exchange/src/ranking/serving.py:58``) and applies
R12's exploration slice to the served shortlist.

What this harness still measures is the ordering the policy produces (``{store_id: share}`` for a
cluster, which is the share of that cluster's shortlist opportunity each store draws), and it
calls it what it is: the exposure ranking, read at the policy, not a served shortlist read back
through ``GET /auctions/{auction_id}/shortlist``. That is a property of this harness rather than
of the tree — the slice the served path applies is bounded (one slot of four, only among the
already-eligible, only for a ``low_data`` store), so a served-shortlist assertion is a different
and narrower measurement than the one ``test_learning.py`` makes here.

The R17 half has no such gap: the depth a store learns from its own outcomes travels all the way
into the ``unit_price`` and ``offer.discount`` of the bid the agent really serves.
"""

from __future__ import annotations

from .exchange_policy import (
    AFFECTED_CLUSTER,
    CONTROL_CLUSTER,
    EXCHANGE_STORES,
    EXPOSURE_SEED,
    ExposureShift,
    exposure_ranking,
    run_exposure_shift,
)
from .store_policy import (
    DEPTH_SEED_WINDOW,
    LEARNING_CLUSTER,
    RIVAL_STORE,
    SUBJECT_STORE,
    DepthShift,
    run_depth_shift,
    served_discount_pct,
)

__all__ = [
    "AFFECTED_CLUSTER",
    "CONTROL_CLUSTER",
    "DEPTH_SEED_WINDOW",
    "EXCHANGE_STORES",
    "EXPOSURE_SEED",
    "LEARNING_CLUSTER",
    "RIVAL_STORE",
    "SUBJECT_STORE",
    "DepthShift",
    "ExposureShift",
    "exposure_ranking",
    "run_depth_shift",
    "run_exposure_shift",
    "served_discount_pct",
]
