"""The clarifier's hashed cluster reaches a real store agent as a name it pursues.

What is being measured, and why it needs three processes
--------------------------------------------------------
The defect these tests close is a namespace break with a component on each side of it:

* ``apps/buyer/svc/src/intent/clarifier.py::_cluster_id`` mints ``cl-<sha256[:16]>`` from the
  shopper's own words — no catalogue in scope, and by its own contract no exchange handle;
* ``packages/store-agent``'s ``AuctionContext.pursues`` is a set membership over the NAMED
  clusters a merchant's envelope authorises — ``cluster-espresso``.

A hash is never a member of a set of names, so every solicited store answered
``204 cluster_not_pursued`` and every shortlist was empty. Nothing between them performed the
join; ``DESIGN.md:34`` says the exchange should.

So these tests use the **real clarifier**, the **real exchange** and the **real store agent**,
each on its own loopback port, and no double anywhere on the path. That is not
thoroughness for its own sake: the two halves of the break live in two packages, and any
test that typed the intent itself — or handed the store agent a context it wrote to match —
would be measuring the join it supplied. The intent below is whatever
``POST /buyer/intent/clarify`` actually answers on this tree, hash and all; the envelope is
the ``fixtures/envelopes/`` shape a merchant's onboarding produces; and the only thing this
file states is the exchange's own deployment document, which is what an operator states.

The control
-----------
:func:`test_without_a_cluster_catalogue_the_same_run_is_declined_cluster_not_pursued` is the
same run with ``intent_clusters`` removed from the deployment document and nothing else
changed. It asserts the store agent declines ``cluster_not_pursued`` and the exchange falls
back to the list price — i.e. the red this file's green is measured against. Without it,
"the store bid" would be evidence that a store agent bids, not that the assignment did it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from exchange.accept.offer import use_registered_domains
from exchange.composition import ENV_DEPLOYMENT, ENV_DEPLOYMENT_JSON
from exchange.main import create_app as create_exchange

from proxyshop_support.asgi_server import serve

#: Generous against the auction's own bid window plus two loopback hops, and finite so a
#: wedged server fails the test rather than hanging the suite.
REQUEST_TIMEOUT_SECONDS = 30.0

#: The shopper's side of the conversation, verbatim from ``e2e/support/s1/run.json`` — the
#: same three turns the S1 demo drives. Nothing here says "espresso machine" twice to help the
#: assignment along; these are the words the scenario already used.
BUYER_TURNS: tuple[str, ...] = (
    "I want an espresso machine for the office, nothing temperamental",
    "Budget is up to about five hundred dollars",
    "It has to run on our normal wall sockets",
)

#: The one store on this roster, from ``e2e/support/s1/run.json``'s first seller.
STORE_ID = "store-northroast"
STORE_DOMAIN = "store-northroast.example.com"
PRODUCT_REF = "prod-northroast-hx"
LIST_PRICE = 389.0

#: The NAMED catalogue cluster the merchant's envelope authorises. Written on BOTH sides
#: here, as it is in the world: the envelope names it because a merchant approved it, and the
#: deployment document names it because an operator stated the exchange's vocabulary. The
#: point of these tests is that the shopper's intent reaches it without either side changing.
CATALOGUE_CLUSTER = "cluster-espresso"

#: What the operator tells this exchange its catalogue clusters are. Only the vocabulary —
#: no intent, no hash, and nothing derived from what the clarifier is about to answer.
INTENT_CLUSTERS: tuple[dict[str, Any], ...] = (
    {
        "cluster_id": CATALOGUE_CLUSTER,
        "label": "Espresso machines",
        "category": "coffee",
        "terms": ["espresso machine", "espresso"],
        "attributes": {"brew_method": "espresso"},
    },
    {
        "cluster_id": "cluster-warm-layers",
        "label": "Warm layers",
        "category": "apparel",
        "terms": ["fleece", "base layer", "insulated jacket"],
        "attributes": {"insulation": "down"},
    },
)

PROFILE: dict[str, Any] = {
    "pseudonym": "pseu-cluster-0001",
    "buckets": {
        "budget_band": "300-500",
        "category_affinity": ["coffee"],
        "frequency_tier": "occasional",
        "region": "US-W",
        "first_time": True,
    },
}


def _store_context() -> dict[str, Any]:
    """The document a hosted store agent reads out of ``STORE_AGENT_CONTEXT``.

    The same shape ``e2e/support/s1/flow.py`` and ``proxyshop_demo/s1.py`` write, because it is
    the same scenario: a per-product floor at 75% of list, a 20% depth cap, and an envelope
    that pursues exactly one NAMED cluster. ``pursue_clusters`` is the merchant's
    authorization and this file never widens it.
    """
    return {
        "store_id": STORE_ID,
        "store_domain": STORE_DOMAIN,
        "envelope": {
            "store_id": STORE_ID,
            "version": 1,
            "floors": [{"product_ref": PRODUCT_REF, "min_price": LIST_PRICE * 0.75}],
            "max_discount_pct": 20.0,
            "budget_cap": 5000.0,
            "pursue_clusters": [CATALOGUE_CLUSTER],
            "standing_commitments": [],
            "activation": "active",
        },
        "catalog": {
            PRODUCT_REF: {
                "product_ref": PRODUCT_REF,
                "list_price": LIST_PRICE,
                "brew_method": "espresso",
                "boiler_type": "heat exchange",
                "pump_pressure_bar": 9,
                "warranty_months": 24,
            }
        },
        "live_state": {PRODUCT_REF: {"in_stock": True, "units_left": 7}},
        "learned_policy": None,
        "network_priors": {CATALOGUE_CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }


def _deployment_document(agent_url: str, *, clusters: bool) -> dict[str, Any]:
    """The document a person writes to deploy this exchange.

    ``clusters=False`` is the control: byte-for-byte the same deployment with the
    ``intent_clusters`` key absent, which is the exchange as it shipped before this change.
    """
    document: dict[str, Any] = {
        "sellers": [
            {
                "store_id": STORE_ID,
                "eligibility": "eligible",
                "registered_domain": STORE_DOMAIN,
                "bid_endpoint": f"{agent_url}/v1/bid-requests",
            }
        ],
        "trust_snapshot": {
            "stores": {STORE_ID: {"store_id": STORE_ID, "blacklisted": False, "score": 0.8}}
        },
        "checkout_mode": "redirect",
    }
    if clusters:
        document["intent_clusters"] = [dict(row) for row in INTENT_CLUSTERS]
    return document


@pytest.fixture
def unwired() -> Iterator[None]:
    """Leave the process-wide registered-domain seam exactly as this file found it.

    Same fixture and same reason as ``test_composition_root.py``'s: the accept seam turns a
    module-level global, and a file that wires an app without restoring it changes what every
    later test in the process reads.
    """
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


@pytest.fixture(scope="module")
def agent_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The REAL ``packages/store-agent`` deployable, configured the way its container is.

    The context is written to a file and named in ``STORE_AGENT_CONTEXT``, exactly as the
    Dockerfile's ``uvicorn store_agent.main:app`` gets it — nothing here calls
    ``configure_solicitation``, so the app resolves its own envelope. The resolution is then
    forced and checked, because a store agent that silently resolved nothing would answer 500
    and this file would be measuring a misconfiguration rather than a decline.
    """
    from store_agent.main import create_app as create_store_agent
    from store_agent.solicitation.serving import store_context

    document = tmp_path_factory.mktemp("store-agent") / "store-context.json"
    document.write_text(json.dumps(_store_context(), indent=2), encoding="utf-8")

    previous_context = os.environ.get("STORE_AGENT_CONTEXT")
    previous_domain = os.environ.get("STORE_AGENT_STORE_DOMAIN")
    os.environ["STORE_AGENT_CONTEXT"] = str(document)
    os.environ["STORE_AGENT_STORE_DOMAIN"] = STORE_DOMAIN
    try:
        app = create_store_agent()
        resolved = store_context(app)
        assert resolved and resolved.get("store_id") == STORE_ID, (
            f"the store agent resolved {resolved!r} out of {document}; an agent advocating for "
            f"the wrong store would make every answer below a different scenario"
        )
        with serve(app) as url:
            yield url
    finally:
        for name, value in (
            ("STORE_AGENT_CONTEXT", previous_context),
            ("STORE_AGENT_STORE_DOMAIN", previous_domain),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(scope="module")
def clarified_intent() -> dict[str, Any]:
    """A REAL clarified intent, off the real buyer service, over a real socket.

    Not typed into this file. ``cluster_id`` is whatever ``clarifier._cluster_id`` hashes out
    of the confirmed query on this tree, which is the entire point: a hand-written
    ``"cl-deadbeef"`` would be this test asserting against its own idea of the break.
    """
    from buyer_svc.main import create_app as create_buyer

    with serve(create_buyer()) as url:
        with httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post("/buyer/intent/clarify", json={"turns": list(BUYER_TURNS)})
    assert response.status_code == 200, (
        f"POST /buyer/intent/clarify -> {response.status_code}: {response.text}"
    )
    intent = dict(response.json()["intent"])
    assert intent.get("cluster_id"), "the clarifier answered an intent naming no cluster at all"
    assert intent["cluster_id"] != CATALOGUE_CLUSTER, (
        f"the clarifier answered {intent['cluster_id']!r}, which IS the catalogue cluster — "
        f"the namespace break this file measures would not exist and the green below would "
        f"prove nothing"
    )
    return intent


@contextmanager
def _served_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_url: str,
    *,
    clusters: bool,
) -> Iterator[httpx.Client]:
    """A client onto the real served exchange, configured the way a deployment configures it.

    The ONLY things this does are write the deployment document, point the environment at it,
    and start ``create_app()``. ``TestClient`` is deliberately not used: in-process ASGI is
    exactly the boundary at which this class of defect hides.
    """
    document = tmp_path / f"deployment-{'clusters' if clusters else 'bare'}.json"
    document.write_text(
        json.dumps(_deployment_document(agent_url, clusters=clusters), indent=2), encoding="utf-8"
    )
    monkeypatch.setenv(ENV_DEPLOYMENT, str(document))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)
    with serve(create_exchange()) as url:
        with httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            yield client


def _open_an_auction(client: httpx.Client, intent: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/auctions",
        json={
            "intent": intent,
            "profile": PROFILE,
            "roster": [
                {
                    "store_id": STORE_ID,
                    "tier": 1,
                    "product_ref": PRODUCT_REF,
                    "list_price": LIST_PRICE,
                    "max_discount_pct": 20.0,
                }
            ],
        },
    )
    assert response.status_code == 201, f"POST /auctions -> {response.status_code}: {response.text}"
    return response.json()


# =====================================================================================
# The acceptance criterion
# =====================================================================================
def test_a_real_store_agent_bids_on_the_clarifiers_own_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unwired: None,
    agent_url: str,
    clarified_intent: dict[str, Any],
) -> None:
    """The clarifier's hashed intent, through the real exchange, reaches a bidding store.

    Three assertions, because they fail for three different reasons and a test that made only
    one of them would call a list-price fallback a success:

    1. the store was solicited and answered with a real bid (``fallback`` is ``False``);
    2. the auction record carries the NAMED cluster, so the exchange's own audit trail and the
       store's authorization check are in one namespace;
    3. nothing was excluded FOR ITS CLUSTER.

    **What this deliberately does not assert, and why.** The shortlist this run produces is
    empty, and that is a different gap with a different owner. ``ranking.serving.catalog_of``
    defaults to ``NoCatalogSnapshots``, which "verifies no claim, so no hard constraint is
    satisfied and a hard-constrained auction shortlists nobody (ESC-020)" — and the real
    clarified intent carries the hard constraint ``brew_method eq espresso``, so the collected
    bid is excluded ``hard_constraint_unsatisfied``. There is no ``EXCHANGE_DEPLOYMENT`` key
    for ``ranking_catalog``, so an operator cannot wire one; this file will not manufacture a
    catalog snapshot to turn that red green, because a test that supplied the missing
    collaborator would be reporting a shortlist nobody deployed. The exclusion reasons are
    asserted instead, and the assertion is that none of them is about the cluster.
    """
    with _served_exchange(tmp_path, monkeypatch, agent_url, clusters=True) as client:
        body = _open_an_auction(client, clarified_intent)
        auction_id = body["auction_id"]
        record = client.get(f"/auctions/{auction_id}")
        assert record.status_code == 200, f"GET /auctions/{auction_id} -> {record.status_code}"

    assert body["denied"] == [], f"the R12 gate denied the rostered store: {body['denied']}"
    assert body["solicited"] == [STORE_ID], (
        f"the store was never asked: solicited={body['solicited']!r}"
    )
    entries = body["entries"]
    assert entries and entries[0]["fallback"] is False, (
        f"the store did not answer with a usable bid, so the exchange manufactured its "
        f"list-price fallback — which is what a `cluster_not_pursued` decline looks like from "
        f"the exchange's side: {entries}"
    )
    assert record.json()["cluster_id"] == CATALOGUE_CLUSTER, (
        f"the auction was recorded against {record.json()['cluster_id']!r}; the assignment did "
        f"not reach the state machine, so the ledger and the store agent disagree"
    )
    excluded = {row["bid_ref"]: row["exclusion_reasons"] for row in body["excluded"]}
    cluster_shaped = {
        bid_ref: reasons
        for bid_ref, reasons in excluded.items()
        if any("cluster" in reason for reason in reasons)
    }
    assert not cluster_shaped, (
        f"a bid was collected and then excluded for its cluster, which means the assignment "
        f"reached the store agent and not the ranker: {cluster_shaped}"
    )


def test_without_a_cluster_catalogue_the_same_run_is_declined_cluster_not_pursued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unwired: None,
    agent_url: str,
    clarified_intent: dict[str, Any],
) -> None:
    """THE CONTROL. Remove the mapping and the identical run goes back to declining.

    Everything is the same object as the test above — the same served store agent, the same
    clarified intent, the same roster — except that the deployment document names no
    ``intent_clusters``. If this ever passes with a bid in it, the test above has stopped
    measuring the assignment and is measuring something that would have happened anyway.
    """
    with _served_exchange(tmp_path, monkeypatch, agent_url, clusters=False) as client:
        body = _open_an_auction(client, clarified_intent)
        record = client.get(f"/auctions/{body['auction_id']}").json()

    assert body["solicited"] == [STORE_ID], "the control must still ask the same store"
    assert record["cluster_id"] == clarified_intent["cluster_id"], (
        "with no catalogue the exchange must pass the clarifier's own id through untouched"
    )
    assert body["entries"] and body["entries"][0]["fallback"] is True, (
        f"the store bid without a cluster catalogue, so the test above proves nothing about "
        f"the assignment: {body['entries']}"
    )


def test_the_store_agent_states_cluster_not_pursued_for_the_hash_and_bids_for_the_name(
    agent_url: str,
    clarified_intent: dict[str, Any],
) -> None:
    """The break and its repair, at the store agent's own published door.

    The exchange is not in this one on purpose. It asks the real agent the same
    ``POST /v1/bid-requests`` twice — once with the clarifier's hashed cluster and once with
    the cluster the exchange now assigns — so the difference between 204 and 200 is pinned to
    the one field, with no auction, no ranking and no filter in between to explain it away.
    """
    from exchange.retrieval import assign_cluster
    from exchange.retrieval.clusters import StaticIntentClusterCatalogue

    request_body = {
        "auction_id": "auction-cluster-probe",
        "intent": clarified_intent,
        "profile": PROFILE,
        "respond_by": "2999-01-01T00:00:00Z",
    }
    with httpx.Client(base_url=agent_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        before = client.post("/v1/bid-requests", json=request_body)

        assignment = assign_cluster(
            clarified_intent, StaticIntentClusterCatalogue.from_rows(list(INTENT_CLUSTERS))
        )
        after = client.post(
            "/v1/bid-requests",
            json={**request_body, "intent": assignment.applied_to(clarified_intent)},
        )

    assert before.status_code == 204, (
        f"the agent answered {before.status_code} to the clarifier's own cluster id "
        f"{clarified_intent['cluster_id']!r}; the defect this file exists for is not present"
    )
    assert before.headers.get("x-proxyshop-decline-reason") == "cluster_not_pursued", (
        f"declined for {before.headers.get('x-proxyshop-decline-reason')!r}, not the cluster"
    )

    assert assignment.cluster_id == CATALOGUE_CLUSTER, (
        f"assigned {assignment.cluster_id!r} on evidence {assignment.evidence!r}"
    )
    assert after.status_code == 200, (
        f"the agent answered {after.status_code} to the ASSIGNED cluster "
        f"{assignment.cluster_id!r}: {after.text[:400]}"
    )
    bid = after.json()
    assert bid["store_id"] == STORE_ID
    assert float(bid["offer"]["unit_price"]) <= LIST_PRICE, (
        f"the agent bid above its own list price: {bid['offer']}"
    )
