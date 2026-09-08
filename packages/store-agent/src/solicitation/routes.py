"""``POST /v1/bid-requests`` — the door the exchange solicits this store's bid through.

``packages/contracts/openapi/store-agent.openapi.json`` has published this operation since the
contract was written, and until this file existed nothing answered it: ``main.create_app()``
mounts every ``<feature>/routes.py`` beside itself and there was no such file anywhere under
``packages/store-agent/src``, so ``app.openapi()['paths']`` was ``{}`` and the container was an
HTTP server with nothing to serve (T-309). The bidding runtime, the provenance hooks, the
learning grid and the external door were all built and tested — as a *library*, reachable only
in-process. This module is the transport and the activation gate, and nothing more: it
validates the body against the pinned `BidRequest`, hands it to the one
:class:`~store_agent.modes.AgentRunner` this process advocates with — which computes the answer
with :func:`store_agent.runtime.bid`, logs it, and submits it only when the store is active —
and renders whatever the runner let out.

**Solicitation only, and therefore unsigned.** The contract says so in its own description: this
is exchange → seller. A hosted Tier-1 agent never crosses the external trust boundary, so the
`Bid` it answers with carries no signing envelope and this route asks for none. The *inbound*
direction — an external Tier-2 seller submitting a bid — is a different door on a different
service (``POST /v1/auctions/{auction_id}/bids`` on the exchange), and the code that guards it
is :func:`store_agent.external.door.receive_bid`. Mounting `receive_bid` here would publish an
anonymous door the contract does not declare, on the wrong side of the arrow.

**Two answers, exactly as published.** 200 with the `Bid`, or 204 with no body when the agent
declines. 204 is the contract's word for a decline and it carries no body by definition, so the
reason travels in :data:`DECLINE_REASON_HEADER` — an operator watching an agent that has stopped
bidding needs to know whether it is out of stock, outside its envelope or simply unconfigured,
and a bare 204 answers none of those. The exchange needs no header to act: a store that does not
answer with a bid is represented at catalogue list price (R10).

**ACTIVATION IS READ HERE, AND IT IS THE ONLY PLACE IT CAN BE READ (R7).** This route used to
be ``answer = bid(bid_request, context)`` — no runner, no activation, no log — so the approved
envelope's ``activation`` had no effect on anything served. Measured on one real fixture,
``fixtures/envelopes/store-alpha.approved.json``, one intent, three envelopes::

    activation=active   200  unit_price=100.0  claims=5  checkout_url=…/cart/44352913:1
    activation=shadow   200  unit_price=100.0  claims=5  checkout_url=…/cart/44352913:1
    activation=killed   200  unit_price=100.0  claims=5  checkout_url=…/cart/44352913:1

A store whose merchant had not approved activation was bidding live, and a store the kill
switch had switched off still bid, still got shortlisted, and could still have a single-use
discount code minted against it. ``store_agent.modes.AgentRunner`` implements all three states
and had zero production callers.

So the answer is computed by the runner, which logs every auction in every mode and hands the
answer to a submitter only when the store is active. The submitter is
:class:`~store_agent.solicitation.advocate.ResponseChannel` — for a synchronously solicited
agent the submission channel *is* the response body — and what this route serves is what came
back out of it, never ``entry.answer``. Both hold the same bid; only one holds it because the
submission gate let it through.

**A THIRD answer, and the one an integrator meets first: 422.** This is the external door of the
product — it is how a store's agent, written by someone outside this repo, joins the network —
and driven by hand with a plausible body it refused three times before accepting anything. Every
refusal was correct and not one of them said what a valid body looks like. So the router carries
:class:`~store_agent.solicitation.refusal.EnrichedRefusalRoute`, which keeps FastAPI's per-field
detail, adds the permitted values and accepted keys DERIVED from the schema that did the
refusing, points at :data:`CANONICAL_BID_REQUEST` in this service's own ``/openapi.json``, and
drops the ``input`` echo that was quoting an unauthenticated caller's body back at them — and
that turned ``1e400`` into an HTTP 500. Nothing about WHICH bodies are refused changes; see that
module for the whole argument, including why a route class and not ``@app.exception_handler``.

**A non-submitting store answers 204, not 403**, and the caller decides that rather than
taste. ``exchange.composition.HttpBidSolicitor._refusal`` reads a 204 as this contract's
decline and reports ``store_declined:<this header's reason>``; **every other status** becomes
``store_refused:<status>``, whose documented meaning is a defect on the agent's side. A shadow
store is working exactly as its merchant approved and a killed store is doing what it was
told, so filing either under the exchange's word for a malfunction would corrupt the one
signal an operator uses to decide which service to go and fix. Neither choice fails the
auction — R10 carries a non-bidding store at catalogue list price either way.
"""

from __future__ import annotations

import logging
from typing import Any

from contracts import Bid, BidRequest, EnvelopeActivation
from fastapi import APIRouter, Request, Response

from ..runtime import Decline, DeclineReason, is_decline
from .advocate import Advocate, advocate
from .refusal import REFUSAL_SCHEMA, EnrichedRefusalRoute

__all__ = [
    "CANONICAL_BID_REQUEST",
    "DECLINE_REASON_HEADER",
    "KILLED_REASON",
    "NOT_ACTIVATED_REASON",
    "UNCONFIGURED_REASON",
    "UNDISCLOSED_REASON",
    "answer_bid_request",
    "no_submission_reason",
    "router",
]

#: **A body this door accepts**, published in the served ``/openapi.json`` and pointed at by
#: every refusal this route issues. It is the same object
#: ``packages/contracts/openapi/store-agent.openapi.json`` declares for this operation, and
#: ``test_solicitation_refusal.py`` asserts that in both directions — a checked-in duplicate the
#: build refuses to let drift, rather than a second example that can quietly stop matching.
#:
#: An example is worth publishing only if it is REAL. The contract's test suite already proved
#: this one validates against the `BidRequest` schema; what nothing proved is the property an
#: integrator actually depends on, which is that the DOOR takes it. So it is driven through the
#: served route in the tests and required not to be refused. Schema-legal and door-accepted are
#: different claims, and this file publishes the second one.
#:
#: Every optional field is filled in on purpose. A minimal example teaches the required set and
#: leaves an integrator guessing about the rest; this one names all five `ProfileBuckets` keys
#: and both `Intent` list shapes, which is exactly the vocabulary the three measured refusals
#: were missing.
CANONICAL_BID_REQUEST: dict[str, Any] = {
    "auction_id": "auc-0001",
    "intent": {
        "intent_id": "int-0001",
        "cluster_id": "cluster-serum",
        "query": "gentle vitamin C serum for sensitive skin",
        "category": "skincare",
        "hard_constraints": [{"field": "fragrance_free", "op": "eq", "value": True}],
        "preferences": [{"field": "price", "direction": "minimize", "weight": 0.6}],
        "ship_to": "US-CA",
        "currency": "USD",
        "budget_band": "40-80",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "2.0.0",
    },
    "product_ref": "sku-serum-15",
    "profile": {
        "pseudonym": "psn-0001",
        "buckets": {
            "budget_band": "40-80",
            "category_affinity": ["skincare"],
            "frequency_tier": "occasional",
            "region": "US-CA",
            "first_time": False,
        },
    },
    "respond_by": "2026-01-01T00:00:30Z",
}

router = APIRouter(tags=["solicitation"], route_class=EnrichedRefusalRoute)

#: This module's logger. R7 asks for the shadow bid to be *logged*; the structured log a
#: merchant reads is the runner's own sink (``Advocate.log``), and this is the operator-facing
#: line that says an auction was answered and in which mode.
_log = logging.getLogger(__name__)

#: Carries the `DeclineReason` on a 204, since a 204 has no body to put it in.
DECLINE_REASON_HEADER = "x-proxyshop-decline-reason"

#: The reason a 204 carries when this process was never given a store context. Not a member of
#: `DeclineReason` — that enum names conditions of a *store*, and "nobody configured this
#: deployment" is a condition of the process. Kept distinct so an operator can tell a store that
#: chose not to bid from an agent that was never wired.
UNCONFIGURED_REASON = "store_context_unconfigured"

#: The reason a 204 carries when the approved envelope has not been activated — R7's shadow
#: mode. Not a member of `DeclineReason` for the same reason :data:`UNCONFIGURED_REASON` is
#: not: that enum names conditions of a *store's offer* (no stock, outside the envelope), and
#: "this merchant has not switched the agent on yet" is a condition of the AUTHORIZATION. An
#: operator reading `cluster_not_pursued` would go looking at the envelope's clusters; the
#: store did have something to bid, and was not permitted to.
NOT_ACTIVATED_REASON = "envelope_not_activated"

#: The reason a 204 carries when the store has been switched off — R9's kill switch. Distinct
#: from :data:`NOT_ACTIVATED_REASON` because the two are opposite facts about the merchant:
#: one has not decided yet, the other has decided to stop.
KILLED_REASON = "store_killed"


@router.post(
    "/v1/bid-requests",
    operation_id="answerBidRequest",
    summary="Answer a solicitation with a Bid or a Decline.",
    response_model=Bid,
    responses={
        200: {"description": "A Bid. Every claim carries hook provenance."},
        204: {
            "description": (
                "Decline. The reason is in the "
                f"`{DECLINE_REASON_HEADER}` header; a silent or Tier-0 store is represented by a "
                "synthesized list-price fallback upstream, not by an empty bid here."
            )
        },
        422: {
            "description": (
                "The body does not validate against `BidRequest`. `detail` is FastAPI's "
                "per-field list (`loc`, `msg`, `type`). `help` adds what the refusal is "
                "otherwise missing, derived from this schema rather than restated beside it: "
                "`permitted_values` for a field whose schema is a closed set, `closed_objects` "
                "for the accepted and required keys of an `additionalProperties: false` object, "
                "and `example`, a JSON Pointer at the valid request body in this document. "
                "**No value from the request is echoed** — the field is named, never what was "
                "sent for it."
            ),
            # The SHAPE, not only the prose. Overriding this status replaces FastAPI's default
            # entry outright, so a description on its own would have published a refusal whose
            # body nothing declared — see `REFUSAL_SCHEMA`, which the tests validate real
            # refusals against after reading it back out of this served document.
            "content": {"application/json": {"schema": REFUSAL_SCHEMA}},
        },
    },
    # Publishes the canonical body in the schema this PROCESS serves, which is the document an
    # integrator can actually reach: `curl $AGENT/openapi.json`. The refusal's `help.example`
    # pointer resolves here, so "what does a valid body look like" is answerable from the door
    # itself and never requires a copy of this repository.
    openapi_extra={
        "requestBody": {"content": {"application/json": {"example": CANONICAL_BID_REQUEST}}}
    },
)
def answer_bid_request(bid_request: BidRequest, request: Request) -> Any:
    """Bid for this store, or decline and say why.

    **`def`, not `async def`, and that is the whole reason this line is commented.**
    :func:`~store_agent.runtime.bid` is synchronous and CPU-bound — it walks the catalogue,
    fingerprints every claim and runs the provenance boundary — and an `async def` endpoint runs
    on the event loop itself. The shipped container is ``uvicorn … --workers 1``, so one bid
    would hold the whole process: the exchange's next solicitation, and the compose healthcheck's
    ``/openapi.json``, would both queue behind it, and an agent that stops answering its
    healthcheck gets restarted. A plain `def` hands the call to FastAPI's threadpool instead. It
    is safe there: `bid()` builds its own `ToolHooks` per call, reads no clock and no RNG, and
    this package holds no mutable module-level state for two calls to race on.

    The model is passed to :func:`~store_agent.runtime.bid` **as the model**, not as a dump: the
    runtime is shape-tolerant by design and the two spellings are asserted byte-identical in
    ``test_runtime.py``, so re-serialising here would only add a place for the two to drift.

    No blanket ``except`` around the call, and that is deliberate. :func:`~store_agent.runtime.bid`
    already converts every "this input is not the shape it claims to be" failure into a decline
    with a reason — that is its documented contract — so anything still escaping is a defect in
    this package, and answering a defect with a 204 would make a broken agent indistinguishable
    from a store that declined. A 500 is visible; a silently-declining agent is not.
    """
    hosted = advocate(request.app)
    if hosted is None:
        return Response(status_code=204, headers={DECLINE_REASON_HEADER: UNCONFIGURED_REASON})

    channel = hosted.channel
    if channel is not None:
        # Clear this thread's slot BEFORE the run. A threadpool thread outlives the request
        # that used it, so anything left here is a previous auction's bid waiting in front of
        # the next caller — and the store may have been killed in between.
        channel.take()

    entry = hosted.runner.run(bid_request)
    _log.info(
        "%s: answered a solicitation in mode %s (submitting=%s); %d entr(y|ies) in the bid log",
        hosted.runner.store_id or "an unidentified store",
        entry.mode,
        entry.submitting,
        len(hosted.log) if hosted.log is not None else -1,
    )
    if not entry.submitting:
        return Response(
            status_code=204,
            headers={DECLINE_REASON_HEADER: no_submission_reason(hosted, entry.mode)},
        )

    # Off the CHANNEL, not off the entry. Both carry this auction's answer; only the channel
    # carries it because the runner's submission gate handed it over. Serving `entry.answer`
    # would leave a fully-formed bid one `if` away from the wire in every mode.
    answer = channel.take() if channel is not None else entry.answer
    if answer is None:  # pragma: no cover - the runner submitted, so something was delivered
        raise RuntimeError(
            "the runner reported a submitting auction and delivered no answer; a 500 is "
            "correct here because a silently-declining agent is indistinguishable from a "
            "store that chose not to bid"
        )
    if is_decline(answer):
        return Response(status_code=204, headers={DECLINE_REASON_HEADER: _reason_of(answer)})
    return answer


def no_submission_reason(hosted: Advocate, mode: EnvelopeActivation) -> str:
    """Why this store submitted nothing, as a header token.

    The envelope is consulted for `killed` rather than only the mode, because the mode is a
    SNAPSHOT and `killed` is the one thing ``AgentRunner`` re-reads live on every auction: a
    merchant who pulls the kill switch on a store that was activated leaves the runner in mode
    `active` and `submits` False, and reporting that as "not activated yet" would tell the
    operator the opposite of what happened.
    """
    if mode is EnvelopeActivation.killed or bool(
        getattr(hosted.runner, "killed_by_envelope", False)
    ):
        return KILLED_REASON
    return NOT_ACTIVATED_REASON


#: The characters a decline reason may put in a response header. An **allowlist**, because the
#: first draft screened for latin-1 encodability — which is what raises inside the server — and
#: therefore let through exactly the two characters that make a header illegal instead:
#: ``"a\r\nX-Injected: 1"`` passed that check and would have been written verbatim. Nothing in
#: `DeclineReason` can spell it today (it is an enum of ASCII identifiers) and no injected header
#: was observed reaching a client, but a guard that exists to be defence in depth has to hold
#: against the input it was written for.
_REASON_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")

#: What a reason becomes when it cannot be rendered. Never an empty header: an absent reason and
#: an unrenderable one are different facts.
UNDISCLOSED_REASON = "undisclosed"


def _reason_of(answer: Decline) -> str:
    """The decline's reason as a header-safe token.

    ``DeclineReason`` is a `str` enum of ASCII identifiers, so this is a formality on every value
    the runtime can produce today. It is here because a header value carrying CR/LF is a response
    split and one that is not latin-1 encodable raises inside the server — both would turn a
    decline into something worse than a decline. The fallback keeps the failure mode "a decline
    with a vague reason", never "a crash" and never "a header the caller wrote".
    """
    reason = getattr(answer, "reason", None)
    token = str(getattr(reason, "value", reason) or DeclineReason.unusable_store_context.value)
    if not token or set(token) - _REASON_CHARACTERS:  # pragma: no cover - not reachable today
        return UNDISCLOSED_REASON
    return token
