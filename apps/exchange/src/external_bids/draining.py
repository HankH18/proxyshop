"""The verification queue's CONSUMER — R8/R18, and until this module there was none.

``verification_queue.py`` says it plainly: *"Nothing drains it yet, and that is said here
rather than discovered later."* Re-measured on this tree before this module: no ``LPOP`` or
``BRPOP`` call site anywhere in the repository, no ``[project.scripts]`` in any member's
``pyproject.toml``, no lifespan or startup hook in ``exchange.main``, and exactly one process
in the exchange image (``uvicorn exchange.main:app``). So an admitted Tier-2 bid queued
durably and NOTHING performed R8's "routed to claim extraction + verification (R18)"; at
:data:`~.verification_queue.MAX_QUEUED_WORK_ITEMS` the whole external channel starts refusing
correctly signed bids because a backlog nobody consumes eventually reaches its ceiling.

Why the drainer runs on the request path, which is the part worth arguing with
------------------------------------------------------------------------------
Two obvious alternatives are both closed, and neither by preference:

**A background worker cannot exist here.** There is no process to put it in. The exchange
deployable runs one command and it is the ASGI server; nothing in this repository declares a
console script, a lifespan task or a sidecar, so a module written as a worker would be a
module with no caller — the exact "built, tested, and reachable by nobody" shape this channel
is already an instance of.

**A new served route cannot be the answer either.**
``apps/exchange/tests/test_repro_open_tickets.py::test_t312_the_exchange_serves_exactly_the_
operations_its_contract_publishes`` requires the exchange's served surface and
``packages/contracts/openapi/exchange.openapi.json`` to agree in BOTH directions, so adding
``POST /v1/external-bids/drain`` is a change to the published contract, its ``PINNED_ROUTES``
row and that row's TypeScript twin — three files in ``packages/contracts``, and the right way
to add an operation rather than something to slip past a gate.

What is left is the operation the exchange already publishes for this channel:
``POST /v1/auctions/{auction_id}/bids``. So the door drains a bounded batch as it admits. That
is not a workaround dressed up — it is the standard shape for a queue in a request-only
process model, and it has the property that matters: a channel under load drains faster than
it fills, because one request adds one item and consumes up to :data:`DRAIN_BATCH_SIZE`.

Four rules, each of which is a way this could have been worse than the backlog
------------------------------------------------------------------------------
**It hangs off an ADMISSION, never off a request.** Draining is work. A door that drained
before judging would let an anonymous caller with a junk signature spend the exchange's CPU on
the backlog once per request, for free. Only a submission that cleared every gate in
``store_agent.external.door`` reaches this.

**A verdict with nowhere to go is not a drain, it is deletion.** Nothing is consumed unless
the exchange can actually RECORD what it decided: a ledger recorder and the approved
``claim_type -> trust dimension`` routing must both be wired.
:func:`~..ranking.serving.claim_dimensions_of` defaults to ``None`` on purpose — the table is
human-approved ground truth the exchange's image does not ship — and a deployment in that
state leaves the backlog exactly where it was.

**A store this exchange holds no snapshot for is not graded, it is deferred.** Its claims
would all come back ``ambiguous``, and consuming an admitted bid in exchange for a record
saying "we could not check" is the misconfigured-exchange version of the same deletion. Such
an item is put back — at the TAIL, so it cannot block the ones behind it — and counted.

**It can never fail the submission.** Every path out of :func:`drain_verification_queue`
returns a count. The seller was told 202 by a door that had already decided; an audit trail
that could turn that into a 500 would be worse than no audit trail.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..ranking.verification import (
    DEFAULT_VERIFIER_VERSION,
    attest_candidate_claims,
    snapshot_for,
)

__all__ = [
    "DRAIN_BATCH_SIZE",
    "DrainReport",
    "drain_verification_queue",
    "verify_work_item",
]

#: How many work items one admitted submission consumes. Strictly greater than one, and that
#: is the whole bound: a batch of one can never overtake its own producer, so a channel taking
#: submissions faster than one at a time would still grow. Eight keeps the added work on a
#: request small — a decomposition plus a comparator run per item, against a snapshot already
#: narrowed to one product — while emptying a full-ceiling backlog in a bounded number of
#: submissions rather than never.
DRAIN_BATCH_SIZE = 8

#: The method names a queue may spell "take the oldest item" with. Duck-typed for the same
#: reason ``store_agent.external.door`` duck-types ``enqueue``: the queue is a seam a
#: deployment fills, and this module must not require the exchange to import the seller
#: package's idea of one.
_POP_NAMES = ("pop", "dequeue", "lpop")


class DrainReport:
    """What one drain did. A record rather than a bare count, because "nothing happened" has
    three different causes and an operator staring at a backlog needs to know which."""

    __slots__ = ("deferred", "reason", "verified")

    def __init__(self, *, verified: int = 0, deferred: int = 0, reason: str = "") -> None:
        #: Work items consumed and announced.
        self.verified = int(verified)
        #: Work items put back because this exchange holds no snapshot for their store.
        self.deferred = int(deferred)
        #: Why nothing was attempted at all, when nothing was.
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"DrainReport(verified={self.verified}, deferred={self.deferred}, "
            f"reason={self.reason!r})"
        )


def _read(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _resolve_pop(queue: Any) -> Any:
    for name in _POP_NAMES:
        candidate = getattr(queue, name, None)
        if callable(candidate):
            return candidate
    return None


def verify_work_item(
    item: Any,
    *,
    catalog: Any,
    recorder: Any,
    dimensions: Any,
    verifier_version: Any = DEFAULT_VERIFIER_VERSION,
) -> list[dict[str, Any]]:
    """One queued external submission, decomposed, graded and announced.

    The work item is ``store_agent.external.door._work_item``'s shape: the door's own deep
    snapshot of the submission under ``submission``, plus the six identity fields at the top
    level. ``store_id`` is read from the item rather than re-read from the seller's original
    object for the reason that docstring gives — the two are views of ONE dict, the dict the
    signature was verified over — and it is an authenticated fact here in the sense that
    matters: the signing bytes bind it, the eligibility gate keyed on it, and the price wall
    was applied against the roster row for it.

    The pitch is graded exactly like the structured claims beside it, and that is the point of
    the whole change: ``Bid.message`` is the artefact a shop BUYS (D55), so it is the one that
    gets checked against the platform's own snapshot rather than the one that gets dropped.

    Returns the attested claims. The durable record is the ``claim_verified`` events
    :func:`~..ranking.verification.attest_candidate_claims` announced through ``recorder`` on
    the way through.
    """
    submission = _read(item, "submission") or {}
    store_id = str(_read(item, "store_id") or _read(submission, "store_id") or "")
    auction_id = str(_read(item, "auction_id") or _read(submission, "auction_id") or "")
    offer = _read(submission, "offer") or {}
    return attest_candidate_claims(
        _read(submission, "claims") or [],
        store_id=store_id,
        product_ref=_read(offer, "product_ref"),
        catalog=catalog,
        verifier_version=verifier_version,
        recorder=recorder,
        auction_id=auction_id,
        dimensions=dimensions,
        message=_read(submission, "message"),
    )


def _requeue(queue: Any, item: Any) -> None:
    """Put a deferred item back at the tail, swallowing a queue that refuses it.

    At the tail rather than the head so one store the exchange holds no snapshot for cannot
    block every item behind it. A queue at its ceiling raises ``VerificationQueueFull``, which
    is caught: that item is then lost, and it is the one case in this module where something
    admitted can be — a backlog at 10,000 items with an unwired catalogue. Reported by the
    ceiling itself (the door starts refusing) rather than silently, and strictly better than
    the alternative, which is dropping every deferred item unconditionally.
    """
    put = getattr(queue, "enqueue", None)
    if not callable(put):
        return
    try:
        put(item)
    except Exception:
        return


def drain_verification_queue(
    queue: Any,
    *,
    catalog: Any,
    recorder: Any,
    dimensions: Any,
    verifier_version: Any = DEFAULT_VERIFIER_VERSION,
    max_items: int = DRAIN_BATCH_SIZE,
) -> DrainReport:
    """Consume up to ``max_items`` queued submissions, verifying and announcing each.

    **Never raises.** See the module docstring: this runs after a door has already told a
    seller 202, and an audit trail that can fail an admitted bid is worse than none.
    """
    if queue is None:
        return DrainReport(reason="no_queue_bound")
    if recorder is None or not callable(getattr(recorder, "record", None)):
        return DrainReport(reason="no_ledger_recorder")
    if dimensions is None:
        # The exchange announces no verdict until a deployment hands it the approved routing
        # (`claim_dimensions_of`). Consuming work items whose verdicts cannot be announced is
        # deletion wearing a drain's name.
        return DrainReport(reason="no_claim_dimension_routing")
    pop = _resolve_pop(queue)
    if pop is None:
        return DrainReport(reason="queue_has_no_consumer_method")

    verified = 0
    deferred = 0
    for _ in range(max(0, int(max_items))):
        try:
            item = pop()
        except Exception:
            # A datastore that cannot be read is a drain that did not happen.
            return DrainReport(verified=verified, deferred=deferred, reason="queue_unreadable")
        if item is None:
            break
        submission = _read(item, "submission") or {}
        store_id = str(_read(item, "store_id") or _read(submission, "store_id") or "")
        offer = _read(submission, "offer") or {}
        if snapshot_for(catalog, store_id, _read(offer, "product_ref")) is None:
            _requeue(queue, item)
            deferred += 1
            continue
        try:
            verify_work_item(
                item,
                catalog=catalog,
                recorder=recorder,
                dimensions=dimensions,
                verifier_version=verifier_version,
            )
        except Exception:
            # Unreachable through the graded paths — `attest_candidate_claims` already
            # swallows a verifier that will not run — and handled anyway, because the item is
            # already out of the queue at this point and losing an admitted bid to an
            # unanticipated fault is exactly what the ceiling's refuse-rather-than-truncate
            # rule exists to prevent.
            _requeue(queue, item)
            return DrainReport(verified=verified, deferred=deferred + 1, reason="verifier_failed")
        verified += 1
    return DrainReport(verified=verified, deferred=deferred)
