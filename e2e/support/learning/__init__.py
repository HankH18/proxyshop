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

Where the R16 loop is still open, said plainly
----------------------------------------------
The exchange learns from conversion outcomes through a served, published door, and the posterior
that door writes really does reorder the cluster's exposure — that is what
:mod:`~e2e.support.learning.exchange_policy` measures, end to end, through HTTP. What does **not**
exist anywhere in the tree is the consumer on the other side: ``exchange.policy.bandit.exposure``
has no production call site, so ``GET /auctions/{auction_id}/shortlist`` is today ranked by
``exchange.ranking`` alone and never reads the posterior book. ``exchange/policy/routes.py`` says
so about itself, and it is measurable — ``grep -rn "exposure" apps/exchange/src`` names only the
policy package.

So this harness reads the ordering the policy produces (``{store_id: share}`` for a cluster, which
is the share of that cluster's shortlist opportunity each store draws) and calls it what it is: the
exposure ranking, not the served shortlist. Claiming a served shortlist reorder here would be
claiming a wire that is not in the tree. When somebody lands that wire, the assertions in
``test_learning.py`` are the ones that already say what the ordering must do.

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
