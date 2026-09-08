"""``POST /internal/outcomes`` must resolve a missing cluster, and must still refuse without one.

    PROXYSHOP_WORKER=13 .venv/bin/python -m pytest \
        apps/exchange/tests/test_outcome_cluster_is_resolved_from_the_auction.py -q

**The defect this file grades, measured over the served stack before the repair.**
``GET /events/verify`` on the running trust service reported the exchange half of the learning
loop as ``delivered: 0, lost: 536`` — every outcome the trust service had ever computed, for the
life of the deployment, refused. The refusal was correct and deliberate::

    the delta names no cluster, and exposure is decided within a cluster — the exchange cannot
    route this outcome to a posterior, and pooling it into a shared bucket would move clusters
    it never happened in

and it was unconditional, because **nothing upstream of this door has ever held a cluster to put
in ``pseudonymous_context.cluster_id``**. The trust service computes a delta from a ledger event;
a ledger event carries ``auction_id``, ``store_id`` and ``order_ref`` and no cluster. So the
guarantee "a payload with no cluster is a 400" held for 100% of production traffic, and
:func:`~exchange.policy.bandit.update` learned from none of it.

**Why the producer could not have fixed it.** The only field spelled ``cluster_id`` anywhere
upstream is ``buyer_svc.intent.clarifier._cluster_id`` — a SHA-256 content hash over the
shopper's query, budget band and constraints, rendered ``cl-<16 hex>``. That is a different fact
wearing the same name: it groups equivalent *intents*, while a posterior is keyed by a *catalogue*
cluster the exchange's deployment document names. ``retrieval.clusters.assign_cluster`` says so by
construction — it keeps a stated cluster only when the catalogue already knows it — and driving
the served route proves it end to end: an auction opened with ``cluster_id: cl-a1e22cd5cf224f6c``
came back from ``GET /auctions/{id}`` as ``cluster-liver-support``. A cluster sealed into the
append-only ledger by the buyer would be a name this exchange has already decided not to believe,
and :meth:`~exchange.policy.routes.InMemoryBanditPosteriors.record` would not reject it — it MINTS
a ``(cluster, store)`` pair from whatever name it is handed, under an LRU capped at
``DEFAULT_BANDIT_CLUSTERS``. One phantom cluster per distinct query, evicting the real one.

**What the repair is.** ``exchange.policy.routes._cluster_of_the_auction`` reads the cluster off
this exchange's own :class:`AuctionRecord`, reached through ``event.auction_id``. That value is
not merely *available* here, it is *definitionally the right one*: ``ranking.serving`` asks for
exposure with ``cluster_id=str(read(intent, "cluster_id", ""))`` — the intent's cluster after
``assign_cluster`` has applied its own — and ``auction.routes`` writes that same post-assignment
value into ``machine.create(..., cluster_id=...)``. So the record carries the exact key the read
half will later use, and folding under it makes the two halves agree by construction. It is the
same thing the sibling producer already does: ``accept/routes.py`` takes "the auction's
``cluster_id`` off the record it already loaded".

**The half this file exists to protect.** The refusal must survive for the case it was written
for. An outcome naming no auction, naming one this exchange no longer holds (records expire and
the book is bounded), or naming one that ran in no cluster is still a 400 under
``outcome_carries_no_cluster`` — and must move NO posterior, because that is the pooling the
refusal exists to prevent. Both directions are asserted below.

Every assertion drives the real routes through ``TestClient`` and reads the posterior book back
off ``app.state``. Nothing here reads the source text of the module it grades.
"""

from __future__ import annotations

from typing import Any

import pytest
from exchange.main import create_app
from exchange.policy.routes import REASON_NO_CLUSTER
from fastapi.testclient import TestClient

#: The cluster the auction under test runs in. With no deployment document bound, the catalogue
#: is empty, ``assign_cluster`` returns ``SOURCE_UNASSIGNED`` and the intent keeps what it
#: arrived with — so this is the value ``AuctionRecord.cluster_id`` ends up holding, and it is
#: also exactly what ``ranking.serving`` would pass to ``exposure``. That identity is the
#: property under test, not an artefact of the fixture.
CLUSTER = "cluster-under-test"

STORE = "store-under-test.example"


def _auction(client: TestClient, *, cluster_id: str | None = CLUSTER) -> str:
    """Open a real auction through the served door and return its id.

    Driven rather than injected: an ``AuctionRecord`` built by hand and pushed into the store
    would prove that this module can read a dataclass, which is not the claim. The claim is that
    the record ``POST /auctions`` actually writes carries the cluster this door resolves.
    """
    intent: dict[str, Any] = {
        "intent_id": "int-0001",
        "query": "milk thistle liver support",
        "budget_band": "mid",
        "hard_constraints": [],
        "preferences": [],
    }
    if cluster_id is not None:
        intent["cluster_id"] = cluster_id
    response = client.post(
        "/auctions",
        json={
            "intent": intent,
            "roster": [{"store_id": STORE, "list_price": 19.99}],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["auction_id"])


def _outcome(
    *,
    auction_id: str | None,
    cluster_id: str | None,
    store_id: str = STORE,
    delta: float = 1.0,
) -> dict[str, Any]:
    """A ``TrustEventPayload`` shaped exactly as the trust service sends one.

    ``extra="forbid"`` on the contract model means an undeclared key is a 400 for a reason that
    has nothing to do with this file. ``delta`` is non-zero because a zero delta is folded into
    nothing and would never reach the posterior book.
    """
    return {
        "store_id": store_id,
        "event": {
            "event_id": "ev-0001",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "feedback",
            "auction_id": auction_id,
            "store_id": store_id,
            "payload": {},
        },
        "dim": "price_honored",
        "delta": delta,
        "pseudonymous_context": {"cluster_id": cluster_id, "pseudonym": "psn-0001"},
    }


def _posteriors(client: TestClient) -> dict[str, dict[str, Any]]:
    """The ``{cluster: {store: Posterior}}`` this app's book holds, or ``{}`` if it has none.

    ``{}`` is the honest answer for a run whose every request was refused: ``_posteriors``
    creates the book on first use, so a door that refuses before reaching it leaves
    ``app.state`` with no ``bandit_posteriors`` at all.
    """
    book = getattr(client.app.state, "bandit_posteriors", None)
    if book is None:
        return {}
    state = book.state()
    return {} if state is None else {k: dict(v) for k, v in state.posteriors.items()}


# =====================================================================================
# Arming — a red-before check that the fixture reaches the code under test at all
# =====================================================================================
def test_the_auction_record_carries_the_cluster_this_door_must_resolve() -> None:
    """Everything below is vacuous if ``POST /auctions`` does not record a cluster.

    This is the fixture's own guard rail, and it is also the statement of WHY the exchange is
    the right party to answer: the fact exists here, on this exchange's own record, before any
    outcome arrives.
    """
    with TestClient(create_app()) as client:
        auction_id = _auction(client)
        record = client.get(f"/auctions/{auction_id}")
        assert record.status_code == 200, record.text
        assert record.json()["cluster_id"] == CLUSTER


# =====================================================================================
# The repair — an outcome with no cluster is ROUTED, not refused
# =====================================================================================
def test_an_outcome_with_no_cluster_is_folded_into_the_clusters_its_auction_ran_in() -> None:
    """The whole defect, in one assertion: 204 and a posterior under the auction's cluster.

    Before the repair this body answered 400 ``outcome_carries_no_cluster`` and the book was
    never built — which is what ``delivered: 0, lost: 536`` was made of.
    """
    with TestClient(create_app()) as client:
        auction_id = _auction(client)

        response = client.post(
            "/internal/outcomes", json=_outcome(auction_id=auction_id, cluster_id=None)
        )
        assert response.status_code == 204, response.text

        posteriors = _posteriors(client)
        assert CLUSTER in posteriors, (
            f"the outcome was accepted but no posterior exists for {CLUSTER!r}; "
            f"the book holds {sorted(posteriors)}"
        )
        assert STORE in posteriors[CLUSTER]


def test_the_resolved_cluster_is_the_one_the_read_half_would_ask_exposure_for() -> None:
    """The write half must key on the same string the ranking path reads back.

    ``ranking.serving`` passes ``intent["cluster_id"]`` (post-assignment) to ``exposure``, and
    ``auction.routes`` writes that same value onto the record. So the posterior this door
    creates must be keyed by exactly what ``GET /auctions/{id}`` reports — not by a normalised,
    prefixed or otherwise "helpful" variant, which would be a posterior no shortlist consults.
    """
    with TestClient(create_app()) as client:
        auction_id = _auction(client)
        recorded = str(client.get(f"/auctions/{auction_id}").json()["cluster_id"])

        client.post("/internal/outcomes", json=_outcome(auction_id=auction_id, cluster_id=None))

        assert list(_posteriors(client)) == [recorded]


def test_a_stated_cluster_still_wins_over_the_auctions_own() -> None:
    """Resolution is a FALLBACK. A caller that named a cluster is not overruled by a lookup.

    The same precedence ``assign_cluster`` applies to a stated cluster, for the same reason: a
    producer that knows the answer is not second-guessed by an inference.
    """
    with TestClient(create_app()) as client:
        auction_id = _auction(client)
        response = client.post(
            "/internal/outcomes",
            json=_outcome(auction_id=auction_id, cluster_id="cluster-stated-explicitly"),
        )
        assert response.status_code == 204, response.text
        assert list(_posteriors(client)) == ["cluster-stated-explicitly"]


# =====================================================================================
# The refusal that must SURVIVE — three ways the cluster genuinely cannot be established
# =====================================================================================
@pytest.mark.parametrize(
    ("case", "auction_id", "expected_clause"),
    [
        ("the event names no auction", None, "names no auction"),
        ("the auction is gone", "auction-evicted-or-never-existed", "no longer held"),
    ],
)
def test_an_outcome_whose_cluster_cannot_be_established_is_still_refused(
    case: str, auction_id: str | None, expected_clause: str
) -> None:
    """400, under the original reason code, naming WHICH way the lookup came back empty.

    This is the direction the refusal was written for and the direction a "fix" is most likely
    to break: an outcome arriving after its auction has expired must not be pooled into a
    shared bucket, because that would move clusters it never happened in.
    """
    with TestClient(create_app()) as client:
        response = client.post(
            "/internal/outcomes", json=_outcome(auction_id=auction_id, cluster_id=None)
        )
        assert response.status_code == 400, response.text
        detail = str(response.json()["detail"])
        assert detail.startswith(REASON_NO_CLUSTER), detail
        assert expected_clause in detail, detail


def test_an_auction_that_ran_in_no_cluster_cannot_lend_one() -> None:
    """An auction whose own ``cluster_id`` is empty resolves nothing, and is refused.

    Symmetric with the read half, which does nothing for such an auction either:
    ``exploration.exposure_shares`` returns ``{}`` on an empty cluster. There is no posterior
    this outcome could move even in principle, so inventing one would be pure fabrication.
    """
    with TestClient(create_app()) as client:
        auction_id = _auction(client, cluster_id=None)
        response = client.post(
            "/internal/outcomes", json=_outcome(auction_id=auction_id, cluster_id=None)
        )
        assert response.status_code == 400, response.text
        assert "ran in no cluster" in str(response.json()["detail"])


def test_a_refused_outcome_moves_no_posterior_at_all() -> None:
    """The anti-pooling property, asserted on the book rather than on the status code.

    A 400 that had already folded the outcome would be the exact defect the refusal exists to
    prevent, merely reported honestly. The book must be untouched — and an existing posterior
    for a real cluster must not have drifted either.
    """
    with TestClient(create_app()) as client:
        auction_id = _auction(client)
        client.post("/internal/outcomes", json=_outcome(auction_id=auction_id, cluster_id=None))
        before = {
            c: {s: (p.alpha, p.beta) for s, p in row.items()}
            for c, row in _posteriors(client).items()
        }
        assert before, "the arming outcome did not land, so this test measures nothing"

        for unroutable in (None, "auction-evicted-or-never-existed"):
            refused = client.post(
                "/internal/outcomes", json=_outcome(auction_id=unroutable, cluster_id=None)
            )
            assert refused.status_code == 400, refused.text

        after = {
            c: {s: (p.alpha, p.beta) for s, p in row.items()}
            for c, row in _posteriors(client).items()
        }
        assert after == before, (
            "a refused outcome changed the posterior book; the refusal is supposed to leave "
            f"every cluster exactly as it found it. before={before} after={after}"
        )
