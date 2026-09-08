"""``POST /v1/trust-events`` — the door the trust service pushes this store's deltas through.

R13's receiving end. ``store_agent.modes.AgentRunner.ingest_trust_event`` implements the whole
intake — the contract validation, the "this event names another store" seal, the non-finite
delta refusal — and until this file existed every one of its call sites in the repository was
a test. ``main.create_app()`` mounts every ``<feature>/routes.py`` beside itself, so this
module is the entire transport; it validates nothing the runner validates and decides nothing
the runner decides.

WHAT COMES BACK, AND WHY IT IS THE POSTURE **AND THE ARM**
------------------------------------------------------------
The response carries the runner's posture *after* the event: the stance, and every dimension
with its net delta and the number of signals behind it. A 200 with an empty body would make an
ingested event indistinguishable from a discarded one to the only party who can see this door
— the trust service — and R13's whole complaint is about work that happens and cannot be
observed. It is also what a merchant's operator reads to confirm a push landed.

It carries a second thing now, under ``learning``, and it is the same argument one layer down.
A pushed event may ride with ``trust_attribution`` — the impression
``trust.feedback.attribution.impression_for`` joined this movement to — and that record exists
to tell the store WHICH of its own decisions earned the movement. The agent answered ``200`` to
attributions it never read, and a posture-only body could not have told anyone: "credited the
15% rung a conversion", "ignored: that rung is past this merchant's cap" and "no attribution
rode along" were one indistinguishable success. ``learning`` is
:class:`~store_agent.modes.ArmCredit` rendered — the reason, what the record itself turned out
to say, the cluster and rung credited, and the state's own observation count afterwards.

It publishes nothing new about the store. Every field is either this store's own decision echoed
back to the store that made it, or a word from a closed vocabulary this package defines; no
counterparty and no rival appears, because a runner holds no other store's anything.

THREE REFUSALS, AND THE STATUS EACH GETS
------------------------------------------
``422`` — the body is not a ``TrustEventPayload``. FastAPI answers this before the runner is
reached, from the same pinned model ``ingest_trust_event`` validates with, so the door cannot
admit a shape the runner would then refuse.

``409`` — the event names a DIFFERENT store. Not a malformed request and not this agent's
news: sealed state is per store, and absorbing a neighbour's feedback would corrupt the
posture of both with nothing downstream able to detect it. A conflict is what it is — the
caller's routing and this process's identity disagree — and answering ``422`` would invite the
trust service to "fix" the body.

``503`` — this process was never given a store context. Fail closed, and the same posture the
bid door takes: an agent with no approved envelope has no store to be told about, and
inventing one would be inventing the merchant's approval. ``503`` rather than ``204`` because
the sender must be able to tell "not configured yet" from "accepted"; on the trust side any
``>= 400`` lands in the undelivered ring with its reason, which is exactly where an operator
should find this.

**No blanket ``except``.** ``ingest_trust_event`` converts every "this input is not what it
claims to be" failure into a ``ValueError``; anything else escaping is a defect in this
package, and answering a defect with a tidy 4xx would make a broken agent indistinguishable
from one refusing bad input.
"""

from __future__ import annotations

from typing import Any

from contracts import TrustEventPayload
from fastapi import APIRouter, HTTPException, Request

from ..solicitation.refusal import EnrichedRefusalRoute
from .runner import agent_runner

__all__ = ["MISROUTED_STATUS", "UNCONFIGURED_STATUS", "ingest_trust_event", "router"]

#: `route_class`, for the same three reasons the sibling door carries it — and the reasons are
#: measured on THIS door, not inherited.
#:
#: `EnrichedRefusalRoute` was written for `/v1/bid-requests` and is schema-agnostic: it reads the
#: model off `APIRoute.body_field` and walks its own JSON schema, so it learns this door's shape
#: rather than the other one's. The lane that built it measured all three of its defects here and
#: was scoped out of fixing them:
#:
#: * `1e400` in a numeric field -> **500**, ValueError out of starlette's response encoder;
#: * a 2000-deep body -> **500**, RecursionError inside FastAPI's own exception handler;
#: * a 3 KB hostile body -> an 18.9 KB refusal that echoes the caller's values back.
#:
#: All three are the stock 422's `input` echo, which this route class drops. This door is
#: unauthenticated and takes third-party JSON, exactly like its sibling, so leaving it stock
#: meant the amplification and the two crash shapes stayed reachable by anyone.
router = APIRouter(tags=["trust-intake"], route_class=EnrichedRefusalRoute)

#: The event names a store this process does not advocate for.
MISROUTED_STATUS = 409

#: This process was never given a store context. See the module docstring.
UNCONFIGURED_STATUS = 503


@router.post(
    "/v1/trust-events",
    operation_id="ingestTrustEvent",
    summary="Take one pushed trust delta into this store's posture.",
    response_model=None,
    responses={
        200: {
            "description": (
                "Ingested. The body is this agent's posture after the event, and what the "
                "event did to its learned policy."
            )
        },
        409: {"description": "The event names a store this agent does not advocate for."},
        503: {"description": "This process has no store context, so it has no posture."},
    },
)
def ingest_trust_event(event: TrustEventPayload, request: Request) -> dict[str, Any]:
    """Fold one pushed `TrustEventPayload` into this store's posture and report the result.

    **`def`, not `async def`.** The intake is synchronous and the shipped container is
    ``uvicorn … --workers 1``: an ``async def`` endpoint runs on the event loop itself, so a
    push would sit in front of the exchange's next solicitation and the compose healthcheck.
    A plain `def` hands the call to FastAPI's threadpool, which is where the bid door already
    runs for the same reason.

    The model is passed to the runner **as the model**. ``ingest_trust_event`` validates with
    ``contracts.TrustEventPayload`` and pydantic revalidates an instance of the same class
    without re-parsing, so dumping it back to a dict here would only add a place for the two
    spellings to drift.
    """
    runner = agent_runner(request.app)
    if runner is None:
        raise HTTPException(
            UNCONFIGURED_STATUS,
            {
                "error": "store_context_unconfigured",
                "message": (
                    "this store-agent process was never given a store context, so it "
                    "advocates for no store and has no posture to move. Set "
                    "STORE_AGENT_CONTEXT, or wire one with "
                    "store_agent.trust_intake.configure_trust_intake"
                ),
            },
        )

    try:
        # `ingest`, not `ingest_trust_event`: the same intake, with the learned-policy half of
        # the answer attached. See the module docstring for why the posture alone was not enough.
        report = runner.ingest(event)
    except ValueError as exc:
        # The runner has already decided to refuse; this only chooses the status. The
        # comparison below reads the two values the runner itself compared rather than
        # re-implementing the seal, so there is no second opinion about what is misrouted --
        # only about which number describes the refusal.
        misrouted = str(event.store_id) != str(runner.store_id)
        raise HTTPException(
            MISROUTED_STATUS if misrouted else 422,
            {
                "error": "misrouted_trust_event" if misrouted else "unusable_trust_event",
                "message": str(exc),
            },
        ) from exc

    accepted = report.payload
    credit = report.credit
    posture = runner.trust_posture
    return {
        "store_id": str(accepted.store_id),
        "dim": str(accepted.dim),
        "delta": float(accepted.delta),
        "event_id": str(accepted.event.event_id),
        "posture": {
            "stance": posture.stance,
            "signals": [
                {
                    "dim": str(signal.dim),
                    "net_delta": float(signal.net_delta),
                    "observations": int(signal.observations),
                }
                for signal in posture.signals
            ],
        },
        "learning": {
            "reason": credit.reason,
            "attribution": credit.attribution,
            "credited": credit.credited,
            "auction_id": credit.auction_id,
            "cluster_id": credit.cluster_id,
            "discount_depth": credit.discount_depth,
            "won": credit.won,
            "source": credit.source or None,
            "observations": credit.observations,
        },
    }
