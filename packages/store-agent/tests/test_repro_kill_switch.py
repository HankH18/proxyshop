"""R7 on the SERVED door: shadow computes and submits nothing, and the kill switch kills.

WHAT WAS MEASURED BEFORE THIS FILE EXISTED
--------------------------------------------
``packages/store-agent/src/solicitation/routes.py`` answered every solicitation with
``answer = bid(bid_request, context)``. No runner, no activation read, no log. Driven against
this repo's own ``fixtures/envelopes/store-alpha.approved.json`` — a fixture whose envelope
says ``"activation": "shadow"`` — all three states answered **identically**::

    activation=active   200  unit_price=389.0  claims=4
    activation=shadow   200  unit_price=389.0  claims=4
    activation=killed   200  unit_price=389.0  claims=4

So a merchant who had not yet approved activation was already bidding live, and a store the
kill switch had switched off still bid, still got shortlisted, and could still have a
single-use discount code minted against it. ``store_agent.modes.AgentRunner`` — the class
that implements all three states — had ZERO production callers; every call site in the
repository was a test.

WHAT THE DOOR ANSWERS IN EACH STATE, AND WHY
----------------------------------------------
``active``  -> ``200`` with the ``Bid``. Unchanged, and this file's honest-traffic control.

``shadow``  -> ``204`` with :data:`NOT_ACTIVATED_REASON` in the decline header.
``killed``  -> ``204`` with :data:`KILLED_REASON`.

**204 and not 403**, and the choice is made by reading the caller rather than by taste. The
caller is the exchange's fan-out, ``exchange.composition.HttpBidSolicitor._refusal``: a
``204`` is read as *the store agent contract's decline* and reported as
``store_declined:<the header's reason>``, while **every other status** is reported as
``store_refused:<status>`` — the bucket whose documented meaning is "a defect on that side".
A shadow store is not a broken store and a killed store is not a broken store; both answered,
and both answered no. Answering 403 would file a correct, deliberate refusal under the
exchange's word for a malfunction, and would do it on the one signal an operator uses to
decide which service to go and fix. Neither status fails the auction — R10 represents a
non-bidding store at catalogue list price either way — so the only thing the choice moves is
the sentence the operator reads, and 204 is the true one.

WHY THE PROOF IS DRIVEN OVER HTTP AND THEN THROUGH THE EXCHANGE
-----------------------------------------------------------------
The defect class here is code that works when a test calls it and is unreachable when a
request arrives; ``AgentRunner`` was that exact shape. So nothing below calls the runner to
establish an outcome — every state assertion is read off a real ``POST /v1/bid-requests``,
and the shortlist proof drives the exchange's own outbound solicitor
(``HttpBidSolicitor``), its own ``solicit_bids`` orchestration, and its own ranker.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from contracts import Bid
from exchange.auction.collect import REFUSAL_FIELD, STORE_DECLINED_REASON
from exchange.composition import HttpBidSolicitor
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.orchestration.solicitation import solicit_bids
from exchange.ranking import rank
from exchange.ranking.verification import StaticCatalogSnapshots, attest_candidates
from fastapi.testclient import TestClient
from store_agent.main import create_app
from store_agent.solicitation import (
    CONTEXT_ENV,
    DECLINE_REASON_HEADER,
    DOMAIN_ENV,
    KILLED_REASON,
    NOT_ACTIVATED_REASON,
    configure_solicitation,
    load_context_from_env,
)
from store_agent.solicitation.advocate import ResponseChannel, advocate

REPO_ROOT = Path(__file__).resolve().parents[3]
ENVELOPE_FIXTURE = REPO_ROOT / "fixtures" / "envelopes" / "store-alpha.approved.json"

#: EVERY approved envelope this repository ships, with the activation each one actually
#: states. Used verbatim by the honest-traffic corpus at the bottom of this file.
SHIPPED_ENVELOPES = {
    "store-alpha": (REPO_ROOT / "fixtures/envelopes/store-alpha.approved.json", "shadow"),
    "store-beta": (REPO_ROOT / "fixtures/envelopes/store-beta.approved.json", "active"),
}

STORE_ID = "store-alpha"
STORE_DOMAIN = "store-alpha.example.com"
CLUSTER = "cluster-warm-layers"
PRODUCT_REF = "prod-cap"
VARIANT_REF = "44352913"
AS_OF = "2026-01-01T00:00:00Z"
#: `config["now"]` for `rank()`. The offer expires in 2999, so nothing is near a boundary.
RANK_NOW = 1767225600.0

#: The three words `contracts.EnvelopeActivation` publishes, which is the whole vocabulary a
#: merchant's approved envelope may state.
STATES = ("active", "shadow", "killed")


def _fixture() -> dict[str, Any]:
    """This repo's own approved envelope for store-alpha, read and never edited."""
    return json.loads(ENVELOPE_FIXTURE.read_text(encoding="utf-8"))


def context(activation: str) -> dict[str, Any]:
    """ONE real store fixture, in the three states, in the shape the merchant service hands over.

    The activation is overlaid on the CONTEXT, never on the file: ``store-alpha.approved.json``
    is an approved artifact and states ``shadow``, which is exactly what makes it the honest
    source for the un-activated case.
    """
    fixture = _fixture()
    catalog = {
        ref: ({**row, "variant_ref": VARIANT_REF} if ref == PRODUCT_REF else row)
        for ref, row in fixture["catalog"].items()
    }
    return {
        "store_id": STORE_ID,
        "store_domain": STORE_DOMAIN,
        "envelope": {**fixture["envelope"], "activation": activation},
        "catalog": catalog,
        "live_state": {
            PRODUCT_REF: {"in_stock": True, "units_left": 7},
            "prod-floor": {"in_stock": True, "units_left": 3},
        },
        "learned_policy": None,
        "network_priors": {CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }


def request_body(auction_id: str = "auc-r7", **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "auction_id": auction_id,
        "intent": intent(),
        "profile": {
            "pseudonym": "psn-r7",
            "buckets": {
                "budget_band": "50-150",
                "category_affinity": ["outerwear"],
                "frequency_tier": "occasional",
                "region": "US-CA",
                "first_time": False,
            },
        },
        "respond_by": "2999-01-01T00:00:00Z",
    }
    body.update(over)
    return body


def intent() -> dict[str, Any]:
    return {
        "intent_id": "int-r7",
        "cluster_id": CLUSTER,
        "query": "a warm mid-layer for cold commutes",
        "category": "outerwear",
        "hard_constraints": [{"field": "material", "op": "eq", "value": "merino wool"}],
        "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
        "ship_to": "US-CA",
        "currency": "USD",
        "budget_band": "50-150",
        "created_at": AS_OF,
        "schema_version": "1.0.0",
    }


def client_for(ctx: dict[str, Any] | None) -> tuple[TestClient, Any]:
    """A served agent and the app behind it, so state can be read back after the response."""
    app = create_app()
    configure_solicitation(app, context=ctx)
    return TestClient(app), app


# =============================================================================================
# 1. The gate: the three states are distinguishable on the served door.
# =============================================================================================


def test_the_three_activation_states_are_distinguishable_on_the_served_door() -> None:
    """One fixture, three envelopes, three real POSTs — and three different answers.

    Written as ONE test on purpose. The defect was that the three were identical, and a
    property about three things differing is not gradeable one parametrised case at a time:
    each case in isolation is satisfied by a door that ignores activation entirely.
    """
    answers: dict[str, tuple[int, str | None, Any]] = {}
    for state in STATES:
        client, _app = client_for(context(state))
        response = client.post("/v1/bid-requests", json=request_body())
        answers[state] = (
            response.status_code,
            response.headers.get(DECLINE_REASON_HEADER),
            response.json() if response.content else None,
        )

    active_status, active_reason, active_body = answers["active"]
    assert active_status == 200, answers["active"]
    assert active_reason is None
    assert active_body is not None and active_body["store_id"] == STORE_ID
    assert Bid.model_validate(active_body).offer.unit_price > 0

    assert answers["shadow"] == (204, NOT_ACTIVATED_REASON, None), (
        f"an un-activated store must submit nothing (R7); it answered {answers['shadow']!r}"
    )
    assert answers["killed"] == (204, KILLED_REASON, None), (
        f"a killed store must submit nothing; it answered {answers['killed']!r}"
    )

    served_bodies = [body for _status, _reason, body in answers.values() if body is not None]
    assert len(served_bodies) == 1, (
        f"exactly one of {STATES} may put a bid on the wire; {len(served_bodies)} did"
    )


# =============================================================================================
# 2. R7's other half: shadow COMPUTES and LOGS. "Submits nothing" is not "does nothing".
# =============================================================================================


@pytest.mark.parametrize("state", STATES)
def test_every_state_computes_and_logs_the_bid_it_would_have_made(state: str) -> None:
    """R7: "it computes bids for live intents and logs them with reasoning".

    The shadow log is the artifact a merchant reads to decide whether to activate at all, so a
    door that skipped the work in shadow would leave them nothing to decide on. Read off the
    process AFTER the response, never off the response body: a door that computed an entry,
    answered with it and dropped it would satisfy every assertion on the body.
    """
    client, app = client_for(context(state))
    response = client.post("/v1/bid-requests", json=request_body())
    assert response.status_code in (200, 204)

    record = advocate(app)
    assert record is not None
    entries = list(record.log.entries)
    assert len(entries) == 1, f"{state}: the auction left no log entry"

    entry = entries[0]
    assert entry.auction_id == "auc-r7"
    assert entry.store_id == STORE_ID
    assert str(entry.mode).endswith(state)
    assert entry.submitting is (state == "active")
    assert entry.rationale, f"{state}: a log entry with no reasoning is not R7's shadow log"
    assert f"offer {PRODUCT_REF} at" in entry.rationale, entry.rationale
    assert entry.answer is not None and entry.answer.offer.unit_price > 0, (
        f"{state}: the bid must be COMPUTED in every mode; shadow is not 'do less work'"
    )


def test_a_shadow_stores_computed_bid_is_the_same_bid_an_active_store_serves() -> None:
    """The log is worth reading only if it says what activation would actually do."""
    shadow_client, shadow_app = client_for(context("shadow"))
    assert shadow_client.post("/v1/bid-requests", json=request_body()).status_code == 204
    logged = list(advocate(shadow_app).log.entries)[0].answer

    active_client, _ = client_for(context("active"))
    served = active_client.post("/v1/bid-requests", json=request_body())
    assert served.status_code == 200, served.text

    assert logged.model_dump(mode="json") == served.json()


# =============================================================================================
# 3. The kill switch is a switch: it takes effect on a running process, with no restart.
# =============================================================================================


def test_the_kill_switch_stops_a_live_bidder_without_a_restart() -> None:
    """The merchant writes `killed` into the envelope and the NEXT solicitation stops.

    A kill switch that needs a redeploy is not a kill switch. The context object is the one
    the process is already advocating from, which is what a merchant service editing the
    store's envelope looks like from in here.
    """
    ctx = context("active")
    client, app = client_for(ctx)

    before = client.post("/v1/bid-requests", json=request_body(auction_id="auc-before"))
    assert before.status_code == 200, before.text

    ctx["envelope"]["activation"] = "killed"

    after = client.post("/v1/bid-requests", json=request_body(auction_id="auc-after"))
    assert after.status_code == 204, after.text
    assert after.headers[DECLINE_REASON_HEADER] == KILLED_REASON
    assert after.content == b""

    # And it still did the work and wrote it down: a killed store is observable, not silent.
    entries = list(advocate(app).log.entries)
    assert [e.submitting for e in entries] == [True, False]
    assert [e.auction_id for e in entries] == ["auc-before", "auc-after"]


def test_activation_starts_serving_bids_on_the_live_object_without_a_restart() -> None:
    """R7/R6's other direction: the merchant approves, and the NEXT solicitation is a real bid.

    Flipped on the object the process is already advocating with — no new app, no new client,
    no re-read of the environment. A store owner switching their agent on cannot be made to
    wait for a deploy, and the runner's whole design note says the mode is state on a live
    instance rather than a constructor decision. Until this door went through the runner there
    was no way to observe either half of that from outside the process.
    """
    client, app = client_for(context("shadow"))

    before = client.post("/v1/bid-requests", json=request_body(auction_id="auc-pre"))
    assert before.status_code == 204
    assert before.headers[DECLINE_REASON_HEADER] == NOT_ACTIVATED_REASON

    advocate(app).runner.mode = "active"

    after = client.post("/v1/bid-requests", json=request_body(auction_id="auc-post"))
    assert after.status_code == 200, after.text
    assert Bid.model_validate(after.json()).store_id == STORE_ID


def test_the_response_channel_keeps_two_threads_bids_apart() -> None:
    """The submitter is process-level and the route is a plain `def`, so it runs in a threadpool.

    A shared slot would let one auction's bid be served to another caller, and the other caller
    may be a store that has since been killed. Graded directly rather than inferred from the
    door, because two simultaneous solicitations are exactly what a test client does not do.
    """
    channel = ResponseChannel()
    seen: dict[str, Any] = {}
    started = threading.Barrier(2)

    def run(name: str) -> None:
        started.wait(timeout=5.0)
        channel.submit(f"bid-for-{name}")
        time.sleep(0.01)
        seen[name] = channel.take()

    threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert seen == {"a": "bid-for-a", "b": "bid-for-b"}
    # And a slot is emptied by taking it, so a reused threadpool thread starts clean.
    channel.submit("stale")
    assert channel.take() == "stale"
    assert channel.take() is None


def test_a_process_started_killed_never_serves_a_first_bid() -> None:
    """The kill switch is not only a transition. A store that starts killed starts silent."""
    client, _app = client_for(context("killed"))
    for auction in ("auc-1", "auc-2", "auc-3"):
        response = client.post("/v1/bid-requests", json=request_body(auction_id=auction))
        assert response.status_code == 204, response.text
        assert response.headers[DECLINE_REASON_HEADER] == KILLED_REASON


# =============================================================================================
# 4. The consequence: a killed store cannot reach a shortlist slot, so nothing can mint a
#    single-use code against it. Driven through the exchange's OWN outbound solicitor, its own
#    solicitation orchestration and its own ranker — with the active store as positive control.
# =============================================================================================


def roster_row() -> dict[str, Any]:
    fixture = _fixture()
    return {
        "store_id": STORE_ID,
        "tier": 1,
        "product_ref": PRODUCT_REF,
        "list_price": float(fixture["catalog"][PRODUCT_REF]["list_price"]),
        "max_discount_pct": float(fixture["envelope"]["max_discount_pct"]),
    }


def catalog_snapshot(ctx: dict[str, Any]) -> dict[str, Any]:
    """The PLATFORM's copy of the catalogue, built from the same rows the agent read."""
    listing = ctx["catalog"][PRODUCT_REF]
    attributes = {
        key: {"value": value}
        for key, value in listing.items()
        if key not in ("product_ref", "variant_ref")
    }
    attributes.update({k: {"value": v} for k, v in ctx["live_state"][PRODUCT_REF].items()})
    return {
        "snapshot_id": f"snap-{STORE_ID}",
        "captured_at": AS_OF,
        "store_id": STORE_ID,
        "products": [
            {
                "product_ref": PRODUCT_REF,
                "canonical_name": PRODUCT_REF,
                "evidence_ref": f"snap-{STORE_ID}#{PRODUCT_REF}",
                "observed_at": AS_OF,
                "attributes": attributes,
                "offer": {
                    "unit_price": listing["list_price"],
                    "currency": "USD",
                    "availability": "in_stock",
                },
            }
        ],
    }


def auction_through_the_exchange(state: str) -> dict[str, Any]:
    """One auction: the real exchange solicits this real agent over HTTP, then ranks it.

    ``HttpBidSolicitor`` is the exchange's shipped outbound client, handed the store agent's
    own ASGI transport instead of a socket. Nothing about how it reads a 200 or a 204 is
    re-implemented here.
    """
    ctx = context(state)
    client, _app = client_for(ctx)
    deadline = time.time() + 30.0
    solicitor = HttpBidSolicitor(
        {STORE_ID: "http://testserver/v1/bid-requests"}, client=client
    ).for_auction(
        auction_id="auc-r7",
        intent=intent(),
        profile=request_body()["profile"],
        # The exchange renders this into the published `respond_by`; omitted, it writes the
        # empty string and every store answers 422 — which is the exchange's own defect and
        # would make this rig grade the wrong refusal.
        respond_by=deadline,
    )

    result = solicit_bids(
        roster=[roster_row()],
        solicitor=solicitor,
        eligibility=StaticSellerEligibility({STORE_ID: ELIGIBLE}),
        now=deadline,
    )
    assert result.solicited == [STORE_ID], result.denied
    entry = result.entries[0]

    candidate = {
        "bid_id": f"auc-r7-{STORE_ID}",
        "store_id": STORE_ID,
        "store_domain": STORE_DOMAIN,
        "tier": 1,
        "network_fee": 0.0,
        "fee_rate": 0.0,
        "envelope_max_discount_pct": 0.0,
        "envelope_budget_cap": 0.0,
        "offer": entry.offer,
        "claims": list(entry.claims),
    }
    attested = attest_candidates(
        [candidate],
        catalog=StaticCatalogSnapshots({STORE_ID: catalog_snapshot(ctx)}),
        product_refs={STORE_ID: PRODUCT_REF},
    )
    ranked = rank(
        attested,
        intent(),
        {STORE_ID: {"store_id": STORE_ID, "score": 0.8, "blacklisted": False}},
        {"now": RANK_NOW, "auction_id": "auc-r7"},
    )
    return {"entry": entry, "ranked": ranked}


def test_an_active_store_reaches_a_shortlist_slot() -> None:
    """The positive control. Without it, "killed reaches no slot" is satisfied by a broken rig."""
    outcome = auction_through_the_exchange("active")
    entry = outcome["entry"]
    assert entry.fallback is False, f"the active store did not bid: {entry.fallback_reason!r}"
    assert entry.offer.get("checkout_url"), entry.offer

    ranked = outcome["ranked"]
    reasons = ranked["candidates"][0]["exclusion_reasons"]
    assert len(ranked["ranked"]) == 1, f"the active store's own bid was not rankable: {reasons}"
    assert len(ranked["shortlist"]["slots"]) == 1
    assert ranked["shortlist"]["slots"][0]["bid_ref"] == f"auc-r7-{STORE_ID}"


@pytest.mark.parametrize(
    ("state", "expected_reason"),
    [("shadow", NOT_ACTIVATED_REASON), ("killed", KILLED_REASON)],
)
def test_a_store_that_submits_nothing_reaches_no_shortlist_slot(
    state: str, expected_reason: str
) -> None:
    """The property R7 is FOR: no bid on the wire, no slot, and so no code to mint against it.

    The exchange still represents the store — R10 says a non-bidding store is carried at its
    catalogue list price — and that representation is the exchange's own synthesis, carrying
    neither ``checkout_url`` nor ``expires_at``. It is therefore excluded before it can be
    ranked, which is why "still represented" and "cannot win" are both true at once.
    """
    outcome = auction_through_the_exchange(state)
    entry = outcome["entry"]

    assert entry.fallback is True, f"{state}: the store put a bid on the wire"
    assert entry.fallback_reason == f"{STORE_DECLINED_REASON}:{expected_reason}", (
        f"{state}: the exchange read this refusal as {entry.fallback_reason!r}; a deliberate "
        f"non-bid must be reported as the store's decline, not as a malfunction"
    )
    assert not entry.offer.get("checkout_url")

    ranked = outcome["ranked"]
    assert ranked["ranked"] == [], f"{state}: a non-bidding store was ranked"
    assert ranked["shortlist"]["slots"] == [], f"{state}: a non-bidding store reached a slot"
    reasons = ranked["candidates"][0]["exclusion_reasons"]
    assert any(reason.startswith("off_domain_checkout") for reason in reasons), reasons


def test_the_exchange_reads_a_shadow_refusal_as_a_decline_and_not_as_a_malfunction() -> None:
    """Why 204 and not 403, checked against the reader rather than asserted in a comment.

    ``HttpBidSolicitor._refusal`` buckets ``204`` as ``store_declined:<reason>`` and every
    other status as ``store_refused:<status>``, whose documented meaning is a defect on the
    agent's side. A shadow store is working exactly as its merchant approved.
    """
    reason = auction_through_the_exchange("shadow")["entry"].fallback_reason or ""
    assert reason.startswith(f"{STORE_DECLINED_REASON}:")
    assert "store_refused" not in reason
    assert REFUSAL_FIELD  # the field this reason travels in exists on the entry, not on a bid


# =============================================================================================
# 5. HONEST TRAFFIC. A refusal that closes its hostile case and starts refusing legitimate
#    input has shipped in this repository before, and neither the gates nor an adversarial
#    verifier caught it. So the new refusal is driven across every approved envelope this repo
#    ships, loaded exactly as the container loads them, in both halves:
#
#      * as SHIPPED — the answer must follow the activation the merchant's own artifact states
#      * activated  — the same store, the same intent, must still bid
#
#    The second half is the one that matters. It is the control that says the 204 above comes
#    from the activation and not from something the gate broke on the way through.
# =============================================================================================


def shipped_context(store_id: str, *, activation: str | None = None) -> dict[str, Any]:
    """One shipped approved envelope, loaded the way ``uvicorn store_agent.main:app`` loads it.

    Through :func:`load_context_from_env`, so the store id recovered from the envelope and the
    domain overlaid beside the file are the container's own code paths rather than this file's
    idea of them. ``live_state`` is added because the fixtures are envelopes and catalogues,
    not stock feeds, and an agent with no stock signal declines for a reason that has nothing
    to do with activation.
    """
    path, _stated = SHIPPED_ENVELOPES[store_id]
    loaded = load_context_from_env({CONTEXT_ENV: str(path), DOMAIN_ENV: f"{store_id}.example.com"})
    assert loaded is not None and loaded["store_id"] == store_id
    loaded["live_state"] = {ref: {"in_stock": True, "units_left": 5} for ref in loaded["catalog"]}
    if activation is not None:
        loaded["envelope"] = {**loaded["envelope"], "activation": activation}
    return loaded


def shipped_intent(ctx: dict[str, Any]) -> dict[str, Any]:
    """An intent this particular store can really answer — its own cluster, its own material.

    Drawn from the store's own approved envelope and catalogue rather than hard-coded, so the
    corpus grades the activation gate and never a mismatched fixture: store-alpha stocks merino
    wool and store-beta stocks recycled fleece, and an intent naming the wrong one would come
    back ``no_matching_product`` from both halves of every case below.
    """
    listing = next(iter(ctx["catalog"].values()))
    return {
        **intent(),
        "cluster_id": str(ctx["envelope"]["pursue_clusters"][0]),
        "hard_constraints": [{"field": "material", "op": "eq", "value": listing["material"]}],
    }


@pytest.mark.parametrize("store_id", sorted(SHIPPED_ENVELOPES))
def test_a_shipped_envelope_is_answered_according_to_the_activation_it_states(
    store_id: str,
) -> None:
    """Both approved artifacts, unedited, through the real door — each one's own word obeyed."""
    _path, stated = SHIPPED_ENVELOPES[store_id]
    ctx = shipped_context(store_id)
    assert ctx["envelope"]["activation"] == stated, "the shipped fixture changed under this test"

    client, _app = client_for(ctx)
    response = client.post("/v1/bid-requests", json=request_body(intent=shipped_intent(ctx)))

    if stated == "active":
        assert response.status_code == 200, response.text
        assert response.json()["store_id"] == store_id
    else:
        assert response.status_code == 204, response.text
        assert response.headers[DECLINE_REASON_HEADER] == NOT_ACTIVATED_REASON


@pytest.mark.parametrize("store_id", sorted(SHIPPED_ENVELOPES))
def test_every_shipped_store_still_bids_once_its_merchant_activates_it(store_id: str) -> None:
    """The honest-traffic control: activation is the ONLY thing standing between these stores
    and a bid, and the new refusal takes nothing else away.

    Both fixtures, both catalogues, both cluster vocabularies — including store-beta, whose
    envelope carries a store-wide floor and no per-product floor for one of its two products,
    and store-alpha, whose intent constraint is satisfied by exactly one row.
    """
    ctx = shipped_context(store_id, activation="active")
    client, _app = client_for(ctx)
    response = client.post("/v1/bid-requests", json=request_body(intent=shipped_intent(ctx)))

    assert response.status_code == 200, (
        f"{store_id} was activated and still did not bid: "
        f"{response.headers.get(DECLINE_REASON_HEADER)!r}"
    )
    served = Bid.model_validate(response.json())
    assert served.store_id == store_id
    assert served.offer.unit_price > 0
    assert served.offer.checkout_url, "an activated store must still serve a completable offer"


@pytest.mark.parametrize("store_id", sorted(SHIPPED_ENVELOPES))
def test_an_activated_shipped_store_still_declines_for_its_own_reasons(store_id: str) -> None:
    """And the gate did not swallow the decline vocabulary that was already there.

    An activated store asked about a cluster its envelope does not pursue must still answer
    ``cluster_not_pursued`` — not the activation reason. A gate that reported every non-bid as
    "not activated" would be exactly as uninformative as the 200 it replaced.
    """
    ctx = shipped_context(store_id, activation="active")
    client, _app = client_for(ctx)
    response = client.post(
        "/v1/bid-requests",
        json=request_body(intent={**shipped_intent(ctx), "cluster_id": "cluster-espresso"}),
    )

    assert response.status_code == 204, response.text
    assert response.headers[DECLINE_REASON_HEADER] == "cluster_not_pursued"
