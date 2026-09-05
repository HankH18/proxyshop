"""``POST /v1/bid-requests`` — the door the exchange solicits this store's bid through.

``packages/contracts/openapi/store-agent.openapi.json`` has published this operation since the
contract was written, and until this file existed nothing answered it: ``main.create_app()``
mounts every ``<feature>/routes.py`` beside itself and there was no such file anywhere under
``packages/store-agent/src``, so ``app.openapi()['paths']`` was ``{}`` and the container was an
HTTP server with nothing to serve (T-309). The bidding runtime, the provenance hooks, the
learning grid and the external door were all built and tested — as a *library*, reachable only
in-process. This module is the transport, and nothing more: it validates the body against the
pinned `BidRequest`, hands it to :func:`store_agent.runtime.bid` with the configured store
context, and renders the answer.

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
"""

from __future__ import annotations

from typing import Any

from contracts import Bid, BidRequest
from fastapi import APIRouter, Request, Response

from ..runtime import Decline, DeclineReason, bid, is_decline
from .serving import store_context

__all__ = ["DECLINE_REASON_HEADER", "UNCONFIGURED_REASON", "answer_bid_request", "router"]

router = APIRouter(tags=["solicitation"])

#: Carries the `DeclineReason` on a 204, since a 204 has no body to put it in.
DECLINE_REASON_HEADER = "x-proxyshop-decline-reason"

#: The reason a 204 carries when this process was never given a store context. Not a member of
#: `DeclineReason` — that enum names conditions of a *store*, and "nobody configured this
#: deployment" is a condition of the process. Kept distinct so an operator can tell a store that
#: chose not to bid from an agent that was never wired.
UNCONFIGURED_REASON = "store_context_unconfigured"


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
    },
)
async def answer_bid_request(bid_request: BidRequest, request: Request) -> Any:
    """Bid for this store, or decline and say why.

    The model is passed to :func:`~store_agent.runtime.bid` **as the model**, not as a dump: the
    runtime is shape-tolerant by design and the two spellings are asserted byte-identical in
    ``test_runtime.py``, so re-serialising here would only add a place for the two to drift.

    No blanket ``except`` around the call, and that is deliberate. :func:`~store_agent.runtime.bid`
    already converts every "this input is not the shape it claims to be" failure into a decline
    with a reason — that is its documented contract — so anything still escaping is a defect in
    this package, and answering a defect with a 204 would make a broken agent indistinguishable
    from a store that declined. A 500 is visible; a silently-declining agent is not.
    """
    context = store_context(request.app)
    if context is None:
        return Response(status_code=204, headers={DECLINE_REASON_HEADER: UNCONFIGURED_REASON})

    answer = bid(bid_request, context)
    if is_decline(answer):
        return Response(status_code=204, headers={DECLINE_REASON_HEADER: _reason_of(answer)})
    return answer


def _reason_of(answer: Decline) -> str:
    """The decline's reason as a header-safe token.

    ``DeclineReason`` is a `str` enum of ASCII identifiers, so this is a formality on every value
    the runtime can produce today — but a header value that is not latin-1 encodable raises
    inside the server rather than at the call site, which would turn a decline into a 500. The
    fallback keeps the failure mode "a decline with a vague reason", never "a crash".
    """
    reason = getattr(answer, "reason", None)
    token = str(getattr(reason, "value", reason) or DeclineReason.unusable_store_context.value)
    try:
        token.encode("latin-1")
    except UnicodeEncodeError:  # pragma: no cover - unreachable through DeclineReason
        return "undisclosed"
    return token
