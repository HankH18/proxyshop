"""Shadow mode, activation, and trust intake — the loop the advocate runs in (R7/R6/R13).

:func:`store_agent.runtime.bid` answers ONE `BidRequest`. This module decides what happens to
that answer, and it is the only place in the store agent where "did anything leave the
building" is decided:

* **shadow** — compute the whole bid, log it, submit NOTHING. The un-activated default.
* **active** — compute, log, and hand the answer to the submitter.
* **killed** — the kill switch. Compute, log, submit nothing again.

Three properties are load-bearing and each is spelled out where it is implemented:

**The mode is state on a live instance, never a constructor decision.** ``runner.mode =
"active"`` starts submission on the next auction with no restart, no reconstruction and no
re-read of the envelope; ``runner.mode = "killed"`` stops it again on the same object. A store
owner flipping activation cannot be made to wait for a deploy, and a kill switch that needs one
is not a kill switch.

**Logging happens before submitting, always.** A submitted bid that was never logged is the one
ordering R7 cannot tolerate: the shadow log is the audit trail the merchant reads to decide
whether to activate at all, and an entry missing from it is a bid nobody can account for. So
the sink is called first and a sink that fails aborts the submission.

**The rationale rides on this module's own record, never on the `Bid`.** `contracts.Bid` is
``extra="forbid"``; it carries `message` (free text an external agent may send, never a source
of claims) and has no `rationale` field, so attaching one RAISES. That is not an inconvenience
to route around — the bid is the protocol object the exchange validates, and "why this store
bid this way" is not part of it. :class:`BidLogEntry` is the wrapper: it holds the answer
verbatim, the mode that decided its fate, the trust posture behind it, and the rationale.

**What a trust event may and may not move.** An injected `TrustEventPayload` changes the
POSTURE this runner states — nothing else. It never re-prices an offer. The cold bid is the
catalog list price moved only by a depth `authorize_discount` granted; a trust signal that
quietly shaded a price would be exactly the improvisation the hook boundary exists to forbid,
and would do it in the one place the boundary cannot see, since the runner sits outside it.

No clock, no RNG, no set iteration on this path — two runs on identical inputs are
byte-identical (S4), which is why the per-dimension posture is rendered in sorted dimension
order rather than in the order events happened to arrive.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from contracts import Bid, EnvelopeActivation, TrustDimension, TrustEventPayload

from ..runtime import Decline, bid, is_decline

#: The modes in which an answer leaves the building. Named once, so "which modes submit" is a
#: fact with one definition rather than a condition repeated at each call site.
SUBMITTING_MODES = frozenset({EnvelopeActivation.active})

#: The method names an injected sink or submitter may expose instead of being callable. Tried in
#: this order, and only when the object is not callable at all.
COLLABORATOR_METHODS = ("write", "append", "log", "record", "put", "enqueue", "send", "submit")

#: Posture stances, in the order evidence overrides them. `guarded` wins over `reinforced`
#: whenever any dimension carries net-negative evidence: a store that is late is late, however
#: well it prices.
GUARDED = "guarded"
REINFORCED = "reinforced"
NEUTRAL = "neutral"


def _field(source: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off a mapping or an object, defensively.

    The store context is whatever the merchant service handed over — a dict off a queue, a
    pydantic model, a namespace. Nothing here may raise on a shape it did not expect; the bid
    path itself already converts an unreadable context into a decline, and this module must not
    beat it to the punch with a `TypeError`.
    """
    try:
        if isinstance(source, Mapping):
            return source.get(name, default)
        return getattr(source, name, default)
    except Exception:  # noqa: BLE001 - a context that cannot even be read has no fields
        return default


def _as_mode(value: Any) -> EnvelopeActivation:
    """Coerce to the envelope's OWN activation vocabulary, or refuse.

    The three modes are `contracts.EnvelopeActivation`, not strings this module invented, so the
    merchant service, the envelope and this runner cannot drift into three spellings of "on".
    Anything outside those three values is refused loudly: an activation flip is the most
    consequential call in this file and a typo'd one must never be read as some third state.
    """
    if isinstance(value, EnvelopeActivation):
        return value
    if isinstance(value, str):
        try:
            return EnvelopeActivation(value)
        except ValueError:
            pass
    known = ", ".join(member.value for member in EnvelopeActivation)
    raise ValueError(f"unknown activation {value!r}; the envelope's vocabulary is {known}")


def _envelope_states(context: Any) -> EnvelopeActivation:
    """The activation the approved envelope states — and `shadow` when it states none.

    Fail-closed on purpose, at BOTH of the two ways an envelope can fail to say anything:
    a missing or unreadable `activation` (no envelope at all, an envelope that is not a mapping,
    a key that is not there) and an `activation` outside the vocabulary (`"on"`, `7`, `""`).
    Neither is evidence that the owner activated the store, and defaulting anywhere but `shadow`
    would let a malformed or absent envelope produce a submitting agent — the one default nobody
    can undo after the fact.
    """
    stated = _field(_field(context, "envelope"), "activation")
    if stated is None:
        return EnvelopeActivation.shadow
    try:
        return _as_mode(stated)
    except ValueError:
        return EnvelopeActivation.shadow


def _delivery(collaborator: Any) -> Any | None:
    """The one bound call that would deliver to ``collaborator``, or `None` if there is none.

    Callables deliver by being called; otherwise the first of :data:`COLLABORATOR_METHODS` the
    object exposes is used. Both shapes are accepted because a sink is somebody else's object —
    a queue with `put`, a logger with `record`, a plain function — and the runner has no business
    dictating the spelling.

    Resolving the delivery WITHOUT performing it is what lets an unusable collaborator be
    refused at the activation flip instead of at the first auction. `submitter is None` is not a
    sufficient test: `0`, `""` and a bare `object()` are all not-None and all blow up with a
    `TypeError` at the first bid, which is exactly the "activated into a void" outcome the check
    exists to prevent.
    """
    if collaborator is None:
        return None
    if callable(collaborator):
        return collaborator
    for name in COLLABORATOR_METHODS:
        method = getattr(collaborator, name, None)
        if callable(method):
            return method
    return None


def _emit(collaborator: Any, payload: Any, *, role: str) -> None:
    """Hand ``payload`` to an injected sink or submitter, exactly once.

    Exactly one of the delivery paths runs, so nothing is ever delivered twice — an object that
    is both callable AND carries a `.submit` receives one call, not two.
    """
    deliver = _delivery(collaborator)
    if deliver is None:
        raise TypeError(
            f"a {role} must be callable or expose one of {COLLABORATOR_METHODS}; "
            f"got {type(collaborator).__name__}"
        )
    deliver(payload)


def _isolated(answer: Any) -> Any:
    """A private copy of an answer, so a collaborator cannot reach back into the audit trail.

    `contracts.Bid` is a mutable pydantic model, and :class:`BidLogEntry` being frozen freezes
    only the REFERENCE to it. Handing the submitter the very object the log holds means a
    submitter that normalizes a price in place silently rewrites an audit row that was already
    written — measured: `entry.answer.offer.unit_price` became `1.0` while `entry.rationale`
    still read `offer prod-cap at 100.00 USD`. A log that a later reader can edit is not a log,
    so the submitter gets its own copy and the entry keeps the original.
    """
    copy_deep = getattr(answer, "model_copy", None)
    if callable(copy_deep):
        return copy_deep(deep=True)
    return copy.deepcopy(answer)


def _amount(value: Any) -> str:
    """Two fixed decimals, so a rationale is stable text rather than a float repr."""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _signed(value: float) -> str:
    """A signed magnitude that never renders non-zero evidence as zero.

    ``f"{-1e-9:+.2f}"`` is ``-0.00``, which sits in the audit log next to the word `guarded` and
    contradicts it. Anything that would round away to nothing is written in exponent form
    instead, so the number and the stance beside it always agree.
    """
    rounded = f"{value:+.2f}"
    if value != 0.0 and float(rounded) == 0.0:
        return f"{value:+.2e}"
    return rounded


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@dataclass(frozen=True, slots=True)
class TrustSignal:
    """One trust dimension, folded over every event this runner has ingested for it."""

    #: The dimension the evidence landed on (`contracts.TrustDimension`).
    dim: TrustDimension
    #: The sum of the ingested deltas. Negative is worse; the sign is the whole signal.
    net_delta: float
    #: How many events produced it, so one loud event and ten quiet ones are distinguishable.
    observations: int

    def describe(self) -> str:
        return f"{self.dim} {_signed(self.net_delta)} over {_plural(self.observations, 'signal')}"


@dataclass(frozen=True, slots=True)
class TrustPosture:
    """What this runner has been told about its own store's delivery, per dimension.

    Deliberately NOT a trust score. Scoring lives in the trust engine, is computed from the
    ledger, and is the exchange's business; this is the store advocate's own read of the
    feedback pushed at it, used for one purpose — to state a posture in the rationale beside the
    bid it just computed.
    """

    signals: tuple[TrustSignal, ...] = ()

    @property
    def stance(self) -> str:
        if any(signal.net_delta < 0 for signal in self.signals):
            return GUARDED
        if any(signal.net_delta > 0 for signal in self.signals):
            return REINFORCED
        return NEUTRAL

    @property
    def weak_dimensions(self) -> tuple[TrustDimension, ...]:
        """The dimensions carrying net-negative evidence, in the posture's sorted order."""
        return tuple(signal.dim for signal in self.signals if signal.net_delta < 0)

    def describe(self) -> str:
        """The posture clause of a rationale. Stable text for a stable set of signals."""
        if not self.signals:
            return f"trust posture: {NEUTRAL} (no trust signal ingested)"
        detail = ", ".join(signal.describe() for signal in self.signals)
        clause = f"trust posture: {self.stance} ({detail})"
        weak = self.weak_dimensions
        if weak:
            clause += " — not leading on " + ", ".join(str(dim) for dim in weak)
        return clause


@dataclass(frozen=True, slots=True)
class BidLogEntry:
    """One auction's worth of audit trail: the answer, the posture, and the reason for both.

    This is the object the sink receives, and it is where the `rationale` lives. It cannot live
    on the `Bid` — `contracts.Bid` is ``extra="forbid"`` and has no such field — and it should
    not: the bid is the protocol object the exchange validates, and the advocate's reasoning is
    not part of that contract. Frozen, because a log entry a reader can rewrite is not a log.
    """

    #: The auction answered, read back off the answer rather than off the request, so the entry
    #: cannot claim to be about an auction the answer is not about.
    auction_id: str
    #: The store this runner advocates for.
    store_id: str
    #: The mode in force when the answer was computed.
    mode: EnvelopeActivation
    #: Whether the mode means this answer goes to the exchange. Recorded at the moment of the
    #: DECISION, before the submission is attempted — the log is deliberately written first, so
    #: this is the runner's intent, and a submitter that then throws is the submitter's own
    #: failure to report rather than a retroactive edit of the audit trail.
    submitting: bool
    #: Why the answer looks the way it does, including the trust posture behind it.
    rationale: str
    #: The posture, structured, so a reader does not have to parse the rationale back.
    trust_posture: tuple[TrustSignal, ...] = ()
    #: The answer itself, verbatim — the `Bid` that would have been submitted, or the `Decline`.
    answer: Bid | Decline | None = None


def _offer_clauses(answer: Bid) -> tuple[str, ...]:
    offer = answer.offer
    currency = f" {offer.currency}" if offer.currency else ""
    clauses = [f"offer {offer.product_ref} at {_amount(offer.unit_price)}{currency}"]
    discount = offer.discount
    if discount is None:
        clauses.append("no authorized discount: the catalog list price stands")
    else:
        ref = _field(discount.provenance, "ref") or "an uncited grant"
        clauses.append(f"discount {_amount(discount.value)} ({discount.type}) citing {ref}")
    keys = ", ".join(str(commitment.key) for commitment in offer.commitments)
    clauses.append(f"standing commitments: {keys or 'none'}")
    return tuple(clauses)


def _decline_clauses(answer: Decline) -> tuple[str, ...]:
    clauses = [f"no bid: {answer.reason}"]
    if answer.detail:
        clauses.append(f"detail: {answer.detail}")
    return tuple(clauses)


def rationale_for(answer: Bid | Decline, posture: TrustPosture) -> str:
    """The human-readable reason an answer looks the way it does.

    Assembled from the answer and the posture and from nothing else — no clock, no counter, no
    identity of the runner that produced it — so the same answer under the same posture always
    renders the same string. That is what makes "an injected trust event changed the rationale"
    a signal rather than noise: the ONLY thing that can move this text is the evidence.
    """
    clauses = _decline_clauses(answer) if is_decline(answer) else _offer_clauses(answer)
    return "; ".join((*clauses, posture.describe()))


class AgentRunner:
    """The hosted advocate's loop for one store: answer, log, and — only if active — submit.

    Constructed around the store context it advocates for::

        runner = AgentRunner(context, sink=shadow_log, submitter=exchange_client)
        runner.run(bid_request)          # shadow by default: logged, not submitted
        runner.mode = "active"           # the same object now submits
        runner.mode = "killed"           # and now it does not, again

    `sink` is REQUIRED. Shadow mode *is* the log — a runner with nowhere to write has no shadow
    to observe, and accepting one would make the un-activated default silently useless.
    `submitter` is optional, because a store that has never been activated has no submission path
    yet; activating without a USABLE one is refused at the flip rather than discovered at the
    first auction, so a store cannot be switched on into a void and look live while bidding
    nowhere.

    **What is snapshotted and what is live.** `mode` is a snapshot taken at construction (or at
    the last explicit flip) — it has to be, because an explicit activation must survive an
    envelope that still reads `shadow`. Everything else about the context is live: `bid()`
    re-reads `catalog`, `live_state` and the envelope's floors and caps on every `run()`, so a
    merchant edit lands on the next auction. The single exception, `killed`, is re-read too; see
    :attr:`killed_by_envelope` for why that asymmetry points the only safe way.
    """

    __slots__ = ("_context", "_deltas", "_mode", "_sink", "_store_id", "_submitter")

    def __init__(
        self,
        context: Any,
        *,
        sink: Any,
        submitter: Any = None,
        mode: Any = None,
    ) -> None:
        if sink is None:
            raise ValueError(
                "a store runner needs a sink: shadow mode is the audit log, and a runner with "
                "nowhere to log has nothing to show a merchant deciding whether to activate"
            )
        self._context = context
        self._store_id = str(_field(context, "store_id", "") or "")
        self._sink = sink
        self._submitter = submitter
        self._deltas: dict[TrustDimension, list[float]] = {}
        # Through the setter, so construction and a later flip enforce the same rules.
        self.mode = _envelope_states(context) if mode is None else mode

    # -- activation ---------------------------------------------------------

    @property
    def mode(self) -> EnvelopeActivation:
        """The activation in force. Assignable at any time; see the class docstring."""
        return self._mode

    @mode.setter
    def mode(self, value: Any) -> None:
        mode = _as_mode(value)
        if mode in SUBMITTING_MODES and _delivery(self._submitter) is None:
            raise ValueError(
                f"cannot flip to {mode} with no usable submitter: an activated store whose "
                f"submitter is {self._submitter!r} looks live and bids nowhere"
            )
        self._mode = mode

    @property
    def killed_by_envelope(self) -> bool:
        """Whether the store context's envelope currently reads `killed`.

        The mode is a SNAPSHOT — an explicit ``runner.mode = "active"`` must survive an envelope
        that still says `shadow`, which is what "activation without a restart" means and what the
        frozen goal asserts. So the envelope is not re-read to decide activation.

        `killed` is the one exception, and it is re-read on every auction. The asymmetry is
        deliberate and it only ever goes one way: this can suppress a submission and can never
        cause one. A merchant who pulls the kill switch by writing it into the envelope must not
        have to also reach a live object, and the alternative — a kill switch that quietly does
        not kill because the process holds a stale snapshot — is the one failure direction that
        cannot be walked back.
        """
        return _envelope_states(self._context) is EnvelopeActivation.killed

    @property
    def submits(self) -> bool:
        """Whether an answer computed right now would be sent to the exchange."""
        return self._mode in SUBMITTING_MODES and not self.killed_by_envelope

    @property
    def store_id(self) -> str:
        return self._store_id

    # -- trust intake -------------------------------------------------------

    @property
    def trust_posture(self) -> TrustPosture:
        """Every ingested event, folded per dimension, in sorted dimension order.

        Sorted rather than insertion-ordered so two runners fed the same events in different
        orders state the same posture — the log must not depend on the order a queue happened
        to deliver feedback in.
        """
        return TrustPosture(
            tuple(
                TrustSignal(dim=dim, net_delta=sum(deltas), observations=len(deltas))
                for dim, deltas in sorted(self._deltas.items(), key=lambda item: item[0].value)
            )
        )

    def _accept(self, event: Any) -> TrustEventPayload:
        """Validate one pushed event against every rule, WITHOUT recording it.

        Three refusals, all loud:

        * an event that is not a readable `TrustEventPayload` never reaches the posture.
          `pydantic.ValidationError` is a `ValueError`, so it arrives at a caller as one and is
          deliberately not re-wrapped: the field-level detail is the useful part.
        * an event naming a DIFFERENT store is a routing bug, not this store's news. Sealed
          state is per store; absorbing a neighbour's feedback would quietly corrupt the posture
          of both, and nothing downstream could ever detect it. The comparison is UNCONDITIONAL:
          guarding it with ``if self._store_id`` turned the seal OFF for a runner whose context
          carried no readable `store_id` — a typo'd `storeId` key was enough — and such a runner
          then absorbed every store's feedback. With no store of its own to compare against, a
          runner refuses every event instead of accepting every event.
        * a delta that is not a finite number is not evidence. `TrustEventPayload` admits `nan`
          and `inf`, and one `nan` poisons a dimension permanently: the sum stays `nan` forever,
          and because ``nan < 0`` and ``nan > 0`` are both False the stance silently reads
          `neutral`. Measured: one `nan` followed by twenty −5.0 events still reported `neutral`.
        """
        payload = TrustEventPayload.model_validate(event)
        if payload.store_id != self._store_id:
            raise ValueError(
                f"trust event names store {payload.store_id!r}; this runner advocates for "
                f"{self._store_id!r}, and one store's feedback is never another's evidence"
            )
        if not math.isfinite(payload.delta):
            raise ValueError(
                f"trust event {payload.event.event_id!r} carries a non-finite delta "
                f"({payload.delta!r}); it would blind the {payload.dim} dimension permanently"
            )
        return payload

    def ingest_trust_event(self, event: Any) -> TrustEventPayload:
        """Take one pushed `TrustEventPayload` — a mapping or the model — into the posture.

        Returns the validated payload, so a caller that handed over a mapping can see what was
        actually taken in. See :meth:`_accept` for what is refused.
        """
        payload = self._accept(event)
        self._deltas.setdefault(payload.dim, []).append(float(payload.delta))
        return payload

    def ingest_trust_events(self, events: Iterable[Any]) -> tuple[TrustEventPayload, ...]:
        """:meth:`ingest_trust_event` over a batch — all of it, or none of it.

        Every event is validated BEFORE any is recorded. Folding over
        :meth:`ingest_trust_event` instead would half-apply a batch whose third event is
        misrouted: the first two stay absorbed, the caller sees a raise, and retrying the
        corrected batch double-counts them. Measured before this was fixed — a four-event batch
        left `price_honored -2.00` behind, then `-4.00` after the retry.
        """
        accepted = tuple(self._accept(event) for event in events)
        for payload in accepted:
            self._deltas.setdefault(payload.dim, []).append(float(payload.delta))
        return accepted

    # -- the loop ------------------------------------------------------------

    def run(self, request: Any) -> BidLogEntry:
        """Answer one `BidRequest`, log the answer, and submit it if and only if active.

        The order is the point. The bid is computed IN EVERY MODE — shadow is not "do less
        work", it is "do all the work and post nothing", because a shadow log that skipped the
        expensive part would tell a merchant nothing about what activation would actually do.
        Then the entry is logged. Only then, and only when the mode says so, is the answer
        handed to the submitter: a sink that throws aborts the run with nothing submitted,
        which is the failure direction that leaves no unlogged bid behind.

        The submitter receives its OWN copy of the answer — equal field for field, never the
        same object. See :func:`_isolated`: the entry is frozen, but that freezes the reference
        and not the mutable `Bid` behind it, and a submitter that adjusts a price in place would
        otherwise rewrite an audit row that had already been written.
        """
        answer = bid(request, self._context)
        posture = self.trust_posture
        entry = BidLogEntry(
            auction_id=str(_field(answer, "auction_id", "") or ""),
            store_id=str(_field(answer, "store_id", "") or self._store_id),
            mode=self._mode,
            submitting=self.submits,
            rationale=rationale_for(answer, posture),
            trust_posture=posture.signals,
            answer=answer,
        )
        _emit(self._sink, entry, role="sink")
        if entry.submitting:
            _emit(self._submitter, _isolated(answer), role="submitter")
        return entry


__all__ = [
    "COLLABORATOR_METHODS",
    "GUARDED",
    "NEUTRAL",
    "REINFORCED",
    "SUBMITTING_MODES",
    "AgentRunner",
    "BidLogEntry",
    "TrustPosture",
    "TrustSignal",
    "rationale_for",
]
