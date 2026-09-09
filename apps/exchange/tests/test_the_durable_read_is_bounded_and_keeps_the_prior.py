"""The durable posterior book's READ, driven through the doors — the two defects that kept it off.

``6d60241`` shipped :class:`~exchange.policy.durable.RedisBanditPosteriors` **defaulted off**
rather than reverting it, because an adversarial review found two defects on the served path and
closed neither. Both live in one function — ``RedisBanditPosteriors._read`` — and both come from
the same line: the read materialises the CROSS PRODUCT of every cluster it holds by every store it
holds, filling the pairs nobody recorded with ``Posterior(1.0, 1.0)``.

**Defect 1 — the cap bounds pairs, the read bounds nothing.** :data:`DEFAULT_MAX_PAIRS` evicts at
512 *recorded pairs*. 512 pairs that share no cluster and no store is not 512 cells, it is
512 x 512. A pair is created by a caller NAMING one on an unauthenticated door, so the shape of
the book is the attacker's to choose, and the 30-day sliding expiry means the amplifier they
build outlives the restart that used to reclaim it.

**Defect 2 — the filler erases the prior the trust snapshot seeds.** ``Posterior(1.0, 1.0)`` is
what :func:`~exchange.policy.bandit.initial_state` seeds a pair with **only where the snapshot
says nothing about the store** — score 0.5, confidence 0.0. Where it says something, the filler
is not the prior, it is the *absence* of one, and
:func:`~exchange.policy.exploration.exposure_shares` carries it over the real prior it just
built. ``b2981da`` is what made this bite: before it the demo's static trust key carried no
``confidence``, so ``bandit._prior`` multiplied its weight by 0.0 and *every* trust prior was
already ``Posterior(1.0, 1.0)`` — the filler was invisible because it was correct. Live snapshot
rows carry ``confidence`` 0.88-0.94, so it is correct no longer.

The two defects share a root and therefore a fix, which is why they are asserted in one file: a
read that holds ONLY the pairs it recorded is both bounded by them and silent about the rest.

**Everything here is measured through a door**, never on the class. The bound is measured on
``POST /auctions/{id}/accept`` — the unauthenticated door ``compose.yaml`` names — and the prior
on the ``exploration`` block ``POST /auctions`` publishes. A unit test on
:class:`RedisBanditPosteriors` would be evidence about the class and not about the exchange.

**Three of the four tests below fail against the old read; the second one does not, and that is
deliberate.** ``test_the_unauthenticated_outcomes_door_is_what_mints_the_pairs`` passes at
``cf455c2`` too, because it pins no defect — it is the REACHABILITY leg, and without it the worst
case the first test fills by hand is a fixture's invention rather than a shape a caller can
build. Verified by restoring the old ``_read`` in memory and re-running: tests 1, 3 and 4 fail,
test 2 passes, 20 runs out of 20 in both directions with no seed dependence.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from exchange.accept import use_registered_domains
from exchange.auction.routes import configure_auctions
from exchange.checkout import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.policy.durable import (
    BANDIT_BOOK_REDIS,
    DEFAULT_MAX_PAIRS,
    ENV_BANDIT_POSTERIORS,
    PAIRS_KEY,
    POSTERIORS_KEY,
    RedisBanditPosteriors,
)
from exchange.ranking.serving import bandit_posteriors_of, configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from fastapi.testclient import TestClient

from proxyshop_support.redis_client import WorkerRedis

from .test_durable_posteriors import (  # the helpers that keep the composition root the binder
    LEARNED_CLUSTER,
    NEWCOMERS,
    acceptable,
    newcomer_app,
)
from .test_exploration_slice import INTENT, TRUST, _bid, _domain, _outcome, _post, _snapshot


@pytest.fixture
def durable_env(monkeypatch: pytest.MonkeyPatch, redis_client: WorkerRedis) -> WorkerRedis:
    """The environment that selects the durable book, with this worker's Redis flushed.

    The twin of ``test_durable_posteriors.durable_env``, restated rather than imported: a pytest
    fixture imported by name is a redefinition at its use site, and the point of both is that
    nothing here wires the posterior book — ``EXCHANGE_BANDIT_POSTERIORS`` does, through the
    composition root, which is the half being measured.
    """
    monkeypatch.setenv(ENV_BANDIT_POSTERIORS, BANDIT_BOOK_REDIS)
    monkeypatch.delenv("EXCHANGE_DEPLOYMENT", raising=False)
    monkeypatch.delenv("EXCHANGE_DEPLOYMENT_JSON", raising=False)
    monkeypatch.setenv("EXCHANGE_AUCTION_STORE", "memory")
    return redis_client


@pytest.fixture
def unwired() -> Any:
    """Leave the process-wide registered-domain registry exactly as this test found it.

    ``configure_accept(registered_domains=...)`` turns a PROCESS-wide seam as well as this app's.
    """
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


#: A store the auction's roster never names. Recording a pair for it is how these tests put a
#: CLUSTER into the book without also putting a posterior on top of any store the auction ranks.
ABSENT_STORE = "store-elsewhere"


@pytest.fixture
def cells_read() -> Any:
    """Every cell count :meth:`RedisBanditPosteriors._read` materialised, in call order.

    The real method still runs; this only measures what it built. Patched onto the CLASS rather
    than onto an instance because the instance under test is the one the composition root
    constructs, which no test here is allowed to hold.
    """
    seen: list[int] = []
    original = RedisBanditPosteriors._read

    def counting(self: Any, client: Any) -> Any:
        state = original(self, client)
        if state is not None:
            seen.append(sum(len(row) for row in state.posteriors.values()))
        return state

    RedisBanditPosteriors._read = counting  # type: ignore[method-assign]
    try:
        yield seen
    finally:
        RedisBanditPosteriors._read = original  # type: ignore[method-assign]


def fill_distinct_pairs(client: WorkerRedis, pairs: int) -> int:
    """``pairs`` recorded pairs, each naming a cluster and a store no other pair names.

    The worst case, and the one a caller gets to choose: ``DEFAULT_MAX_PAIRS`` pairs sharing no
    cluster and no store is the maximum cross product the cap admits. Written through the book's
    OWN ``_write`` — the same call ``record`` makes — so the encoding, the eviction and the
    expiry are the shipped ones; ``record`` itself is not used because it would also pay the
    quadratic read this test exists to measure, once per pair.
    """
    book = RedisBanditPosteriors(client=client)
    for index in range(pairs):
        book._write(client, f"store-{index:04d}", f"cluster-{index:04d}", index % 2 == 0, None)
    recorded = int(client.zcard(PAIRS_KEY) or 0)
    assert recorded == pairs, recorded
    return recorded


# =====================================================================================
# 1. The bound: what the read costs is what was recorded
# =====================================================================================
@pytest.mark.docker
def test_the_served_accept_materialises_no_more_cells_than_the_book_recorded(
    durable_env: WorkerRedis, unwired: None, cells_read: list[int]
) -> None:
    """The cap bounds recorded PAIRS, so the served read may not cost the cross product.

    512 pairs sharing no cluster and no store — the shape the eviction cap admits and an
    anonymous caller chooses — and then one accept through the unauthenticated door. The read
    behind that accept may materialise at most one cell per pair the book actually holds.
    """
    recorded = fill_distinct_pairs(durable_env, DEFAULT_MAX_PAIRS)

    app, stores = newcomer_app()
    acceptable(app, stores)
    body = _post(app, stores, intent={**INTENT, "cluster_id": LEARNED_CLUSTER})
    chosen = body["shortlist"]["slots"][0]["bid_ref"]

    cells_read.clear()
    started = time.perf_counter()
    accepted = TestClient(app).post(
        f"/auctions/{body['auction_id']}/accept", json={"bid_ref": chosen}
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert accepted.status_code == 200, accepted.text
    assert isinstance(bandit_posteriors_of(app), RedisBanditPosteriors), (
        "the composition root did not bind the durable book, so this measured the in-memory one"
    )
    assert cells_read, "the served accept never reached the durable read; nothing was measured"

    assert max(cells_read) <= recorded, (
        f"the served POST /auctions/{{id}}/accept materialised {max(cells_read)} posterior cells "
        f"from {recorded} recorded pairs ({elapsed_ms:.1f} ms for the accept). The cap bounds "
        f"PAIRS; a read that bounds the CROSS PRODUCT of the dimensions instead is an amplifier "
        f"an anonymous caller sizes, on a door that needs no credential, surviving restarts for "
        f"the 30-day expiry."
    )


@pytest.mark.docker
def test_the_unauthenticated_outcomes_door_is_what_mints_the_pairs(
    durable_env: WorkerRedis,
) -> None:
    """Reachability for the bound above: the pairs are created by a caller naming them.

    Without this the worst case is a fixture's invention. ``POST /internal/outcomes`` needs no
    credential and mints one pair per ``(cluster, store)`` it is handed, so the SHAPE of the
    book — how its pairs spread across clusters and stores — is chosen by whoever calls it.
    """
    app, _stores = newcomer_app()
    client = TestClient(app)
    minted = 24
    for index in range(minted):
        posted = client.post(
            "/internal/outcomes",
            json=_outcome(f"attacker-store-{index}", f"attacker-cluster-{index}", delta=1.0),
        )
        assert posted.status_code == 204, posted.text

    assert isinstance(bandit_posteriors_of(app), RedisBanditPosteriors), bandit_posteriors_of(app)
    assert int(durable_env.zcard(PAIRS_KEY) or 0) == minted, durable_env.zcard(PAIRS_KEY)
    assert int(durable_env.hlen(POSTERIORS_KEY) or 0) == 2 * minted, durable_env.hlen(
        POSTERIORS_KEY
    )


@pytest.mark.docker
def test_the_read_holds_only_the_pairs_that_were_recorded(durable_env: WorkerRedis) -> None:
    """The mechanism behind both defects, stated once as a shape.

    The datastore is already sparse — one field pair per recorded ``(cluster, store)``. Density
    is invented at read time and nowhere else, so this is the assertion the two served ones
    above and below both reduce to.
    """
    book = RedisBanditPosteriors(client=durable_env)
    book._write(durable_env, "store-1", "cluster-1", True, None)
    book._write(durable_env, "store-2", "cluster-2", True, None)

    state = book.state()
    assert state is not None
    cells = {(cluster, store) for cluster, row in state.posteriors.items() for store in row}
    assert cells == {("cluster-1", "store-1"), ("cluster-2", "store-2")}, (
        f"the read holds {sorted(cells)} for two recorded pairs. Every extra cell is a pair "
        f"nobody recorded, carrying a posterior nobody wrote."
    )


# =====================================================================================
# 2. The prior: an absent pair is absent, not flat
# =====================================================================================
def _app_with_confidence(confidences: dict[str, float], low_data: tuple[str, ...]) -> Any:
    """``test_exploration_slice._app``, but the trust rows state their own ``confidence``.

    That column is the whole of defect 2. ``bandit._prior`` weights the trust score by
    ``PRIOR_WEIGHT * confidence``, so a row with a confidence is a row whose prior is NOT
    ``Posterior(1.0, 1.0)`` — and only then is flattening it to ``Posterior(1.0, 1.0)``
    observable. ``_app`` states a flat ``0.5`` for every store; these tests need two low-data
    stores whose priors differ, which is what makes the promoted store a readable answer.
    """
    stores = {**TRUST, **dict.fromkeys(low_data, 0.10)}

    def solicit(store: Any) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        if store_id not in stores:
            return None
        return {"store_id": store_id, "received_at": time.time(), "bid": _bid(store_id)}

    app = create_app()
    configure_auctions(
        app,
        solicitor=solicit,
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in stores}),
    )
    configure_ranking(
        app,
        trust_snapshot={
            store: {
                "blacklisted": False,
                "score": score,
                "confidence": confidences.get(store, 0.5),
                "low_data": store in low_data,
            }
            for store, score in stores.items()
        },
        registered_domains=StaticRegisteredDomains({s: _domain(s) for s in stores}),
        catalog=StaticCatalogSnapshots({s: _snapshot(s) for s in stores}),
    )
    assert getattr(app.state, "bandit_posteriors", None) is None
    return app, stores


#: ``store-x`` and ``store-y`` are identical in every RANKED feature — same trust score, so the
#: shortlist puts both on the bench — and differ only in how confident trust is. That makes their
#: seeded priors differ (``Posterior(1.1, 1.9)`` against ``Posterior(1.4, 4.6)``) while nothing
#: the ranker reads does, so which of the two takes the exploration slot is a statement about the
#: PRIOR and about nothing else. Measured over 500 auction ids: ``store-x`` takes it 500/500 with
#: the priors intact, and ``store-y`` takes it 500/500 once ``store-y``'s is flattened to
#: ``Posterior(1.0, 1.0)`` — the smallest sampled-share ratio across those runs is 2.9x and 2.1x
#: respectively, so the answer does not depend on which auction id the exchange minted.
CONFIDENCES = {"store-x": 0.25, "store-y": 1.0}


def _explored_in_cluster_one(app: Any, stores: dict[str, float]) -> str:
    body = _post(app, stores)  # INTENT's own cluster-1, which no outcome below is recorded in
    assert body["exploration"] is not None, body
    return str(body["exploration"]["store_id"])


@pytest.mark.docker
def test_an_outcome_in_another_cluster_does_not_flatten_this_ones_trust_seeded_prior(
    durable_env: WorkerRedis,
) -> None:
    """``bandit.py``'s own contract — "an outcome in ``cluster-1`` cannot move ``cluster-2``'s
    exposure" — read back off the served shortlist, against the durable book.

    Two runs that differ by ONE recorded outcome, and that outcome is in a cluster this auction
    is not held in, for a store this auction does not rank against it. Nothing about
    ``cluster-1`` is recorded in either run, so ``cluster-1`` must answer identically in both.

    ``test_exploration_slice.py`` already asserts this shape against the in-memory book and
    passes, because that book fills an unrecorded pair with the store's own **trust-seeded**
    prior (it re-seeds through ``initial_state`` on every write). Its assertion is also
    deliberately weak — "the answer is one of the two newcomers" — which both books satisfy
    while one of them is wrong. This is the strong form, and the durable book is the subject.
    """
    control_app, stores = _app_with_confidence(CONFIDENCES, NEWCOMERS)
    control_client = TestClient(control_app)
    # Puts `cluster-1` into the book WITHOUT putting a posterior on any store this auction
    # ranks. Both runs record it, so it is not what separates them.
    assert (
        control_client.post(
            "/internal/outcomes", json=_outcome(ABSENT_STORE, "cluster-1", delta=1.0)
        ).status_code
        == 204
    )
    control = _explored_in_cluster_one(control_app, stores)

    durable_env.delete(POSTERIORS_KEY, PAIRS_KEY)

    other_app, stores = _app_with_confidence(CONFIDENCES, NEWCOMERS)
    other_client = TestClient(other_app)
    assert (
        other_client.post(
            "/internal/outcomes", json=_outcome(ABSENT_STORE, "cluster-1", delta=1.0)
        ).status_code
        == 204
    )
    # The ONE difference: `store-y` converts once, in a cluster this auction is not held in.
    assert (
        other_client.post(
            "/internal/outcomes", json=_outcome("store-y", LEARNED_CLUSTER, delta=1.0)
        ).status_code
        == 204
    )
    after_other_cluster = _explored_in_cluster_one(other_app, stores)

    assert isinstance(bandit_posteriors_of(other_app), RedisBanditPosteriors)
    assert after_other_cluster == control, (
        f"cluster-1 promoted {control!r} before an outcome was recorded for store-y in "
        f"{LEARNED_CLUSTER!r}, and {after_other_cluster!r} after. Nothing about cluster-1 was "
        f"recorded either time: the cross-product read minted a (cluster-1, store-y) cell "
        f"nobody wrote and filled it with Posterior(1.0, 1.0), which erased the prior trust "
        f"seeded for store-y and moved one cluster's exposure with another cluster's outcome."
    )
