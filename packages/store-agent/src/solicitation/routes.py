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
import time
from typing import Any

from contracts import Bid, BidRequest, EnvelopeActivation
from fastapi import APIRouter, Request, Response

from ..runtime import Decline, DeclineReason, is_decline
from .advocate import Advocate, advocate
from .copywriter import PitchAttempt, pitch_budget_seconds
from .refusal import REFUSAL_SCHEMA, EnrichedRefusalRoute

__all__ = [
    "CANONICAL_BID_REQUEST",
    "DECLINE_REASON_HEADER",
    "KILLED_REASON",
    "NOT_ACTIVATED_REASON",
    "UNCONFIGURED_REASON",
    "UNDISCLOSED_REASON",
    "answer_bid_request",
    "log_solicitation",
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

    **THE EXCHANGE'S DEADLINE IS THIS STORE'S DEADLINE, and this is where the two are joined.**
    ``bid_request.respond_by`` is a required field of the contract and it is not decoration: it
    is the instant the exchange abandons this solicitation. The store already parsed it — as a
    fallback *offer expiry* — and had never once read it as a time budget, so the copywriter ran
    against a fixed five seconds nobody had reconciled with the auction window. Measured on the
    live stack in process (one store, driven through ``AgentRunner.run`` with a real key), the
    offer took 12.7 ms median (n=20) and the pitch 3.35–5.04 s (n=5) — the pitch is 99.67% of
    that request. Measured over the wire against all four hosted containers, this endpoint ran
    1.97–4.73 s (n=24). Against a 3-second window every hosted agent missed, and a market of
    four bidding stores silently degraded to R10's list-price fallback for all four. So the
    remaining time is computed here, once, and armed on the copywriter for the duration of the
    run.

    **What that buys, stated exactly.** This path is synchronous end to end: the offer is
    computed first, in ~12.7 ms, and then the request WAITS on the copywriter for as long as
    the budget allows — a default-window auction measured 2.9–3.7 s wall for precisely that
    reason. So the offer does not run concurrently with the prose and this route has never
    claimed a watchdog. What the budget guarantees is the thing that was actually broken:
    **the prose cannot push the bid past the exchange's deadline.** The offer is finished long
    before the model is asked for anything and is never *lost* to a slow copywriter — it is
    held until the budget expires and then shipped with the deterministic fallback pitch,
    instead of shipping late to an exchange that has stopped listening.

    The clock read is HERE and nowhere deeper — ``runtime/`` may not read one at all
    (``test_the_runtime_reads_no_clock_and_no_randomness``), and this is the one place on the
    path where "what time is it" is a legitimate question rather than an ambient input.
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

    # `time.time()` and not `monotonic`, because this is compared against an ABSOLUTE instant
    # the exchange stated; a monotonic reading has no relationship to it. The reserve inside
    # `pitch_budget_seconds` is what absorbs the clock skew that comparison invites.
    budget = pitch_budget_seconds(bid_request.respond_by, now=time.time())
    pitch = hosted.pitch
    if pitch is not None:
        pitch.arm(budget)
    try:
        entry = hosted.runner.run(bid_request)
    finally:
        # In a `finally` so a raising bid path cannot leave a budget armed on a threadpool
        # thread, where the NEXT auction on that thread would inherit it.
        attempt = pitch.disarm() if pitch is not None else PitchAttempt()

    log_solicitation(hosted, entry, attempt)
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


#: What :func:`log_solicitation` says the copywriter's CALL did, per outcome.
#:
#: It used to be spelled `source=` and to answer `model` for the outcome `ok`, which was a
#: claim about the shipped prose that this route cannot make. MEASURED on the deployed demo:
#: three of four agents shipped the byte-identical deterministic template —
#:
#:     Free returns: 30 return window. Also: in stock: yes; units left: 12.
#:
#: — while each logged `outcome=ok source=model`, and the one agent that shipped genuine prose
#: logged the identical line. An operator could not tell them apart from the log, which is the
#: one job the log has.
#:
#: The cause is a real limit rather than an oversight. `ok` means the model answered INSIDE its
#: budget; `runtime.pitch.compose_pitch` then screens that answer, and a reply that arrives on
#: time and fails the content rule is discarded — `return screen(fallback_pitch(material),
#: material)` — with the template shipping instead. That screening happens in a pure function
#: whose only return value is the string, and the client that builds :class:`PitchAttempt`
#: never sees it. So the route genuinely cannot report the shipped text's provenance.
#:
#: What it CAN report is what the call did, and now that is all it says. `pitch_call=answered`
#: means the model replied in time and NOT that its words were used. Reporting the provenance
#: honestly needs `compose_pitch` to say which of its two strings it returned; that is a change
#: to a function on the bid path and is deliberately not made from here.
_PITCH_CALL = {
    "ok": "answered",
    "timed_out": "budget_missed",
    "failed": "errored",
    "skipped": "not_called",
    "not_attempted": "unarmed",
}


def log_solicitation(hosted: Advocate, entry: Any, attempt: PitchAttempt) -> None:
    """One operator line per solicitation, saying what the copywriter cost and whether it made it.

    **This line used to carry no timing at all**, which is why a defect that made every hosted
    store miss every auction was invisible from the outside: the store logged "answered a
    solicitation in mode active (submitting=True)" and served a 200 whether the pitch had taken
    40 ms or 5 s, and whether the bytes on `Bid.message` were the model's sentences or the
    deterministic fallback. An operator reading the log had no way to tell a market of four
    advocating stores from a market of four stores serving template prose after the exchange had
    already given up on them.

    So: the store, the mode, the budget it was given, what the copywriter actually spent, and
    which of the two pitches the bid carries — at WARNING whenever the merchant did not get its
    prose, INFO otherwise. WARNING is the right level because that is a *deployment* fact, not a
    bid fact: the bid is fine, and the thing a merchant pays for is not being delivered.

    **``outcome`` says WHICH, and the two point at different repairs.** ``skipped`` and
    ``timed_out`` are the clock — the auction had closed, or the model did not answer inside the
    budget — and they are what a wider window or a faster model fixes. ``failed`` is the
    copywriter itself: an auth error, a connection reset, a provider 500, none of which any
    auction window repairs. Both are WARNINGs, and until they were separate words a dead API key
    logged identically to a slow model, which is the misdiagnosis this whole line exists against.

    ``budget`` and ``elapsed`` are ``-`` rather than ``0`` when they do not exist, because "no
    budget was armed" and "the budget was zero seconds" are opposite facts and a log that spells
    them the same way is the log that hid this in the first place.
    """
    _log.log(
        logging.WARNING if attempt.degraded else logging.INFO,
        "%s: answered a solicitation in mode %s (submitting=%s); "
        "pitch budget=%s elapsed=%s outcome=%s pitch_call=%s; %d entr(y|ies) in the bid log",
        hosted.runner.store_id or "an unidentified store",
        entry.mode,
        entry.submitting,
        "-" if attempt.budget is None else f"{attempt.budget:.3f}s",
        "-" if attempt.elapsed is None else f"{attempt.elapsed:.3f}s",
        attempt.outcome,
        _PITCH_CALL.get(attempt.outcome, "unknown"),
        len(hosted.log) if hosted.log is not None else -1,
    )


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
