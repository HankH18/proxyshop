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
POSTURE this runner states, and — through the learned state it holds — the arm it will PLAY
next. It never re-prices *this* offer. The cold bid is the catalog list price moved only by a
depth `authorize_discount` granted; a trust signal that quietly shaded a price would be exactly
the improvisation the hook boundary exists to forbid, and would do it in the one place the
boundary cannot see, since the runner sits outside it. What the loop learns still goes through
that wall on the next auction: :meth:`AgentRunner._select_arm` renders it as `learned_policy`
and hook 5 re-asks the envelope about the depth before it can reach a bid.

**Which of its own decisions earned a movement.** A pushed event may carry the impression the
trust service attributed the movement to, under `trust_attribution` inside the event's payload.
That record names the terms actually quoted and whether the buyer converted, which is the
Bernoulli outcome the loop's posterior is over; the signed `delta` is a statement about the
store's REPUTATION and only correlates with it. So where the record is present and usable it
supersedes the delta's sign for crediting the arm — and where it is absent, unversioned or
unauthorised, the sign is still what credits, exactly as it always did. See
:mod:`store_agent.learning.attribution` and :meth:`AgentRunner._credit`.

No clock, no RNG, no set iteration on this path — two runs on identical inputs are
byte-identical (S4), which is why the per-dimension posture is rendered in sorted dimension
order rather than in the order events happened to arrive.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from contracts import Bid, EnvelopeActivation, TrustDimension, TrustEventPayload

from ..learning import (
    ATTRIBUTION_SOURCE,
    DEFAULT_DEPTH_BUCKETS,
    MISSING_ATTRIBUTION,
    OUTCOME_SOURCE,
    Arm,
    StoreLearningState,
    available_commitments,
    cold_arm,
    outcome_row,
    percent_as_fraction,
    policy_for_auction,
    read_impression,
    sample_arm,
    update,
    verdict,
)
from ..runtime import Decline, bid

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

#: How many auctions' arms one runner remembers so a later trust verdict can be credited to the
#: arm that earned it. A ring with FIFO eviction, because this is a long-lived object on a served
#: process and an unbounded map behind a request path is a leak. A verdict naming an auction
#: older than this window is ingested into the posture and credited to nothing — which is the
#: honest outcome, since the runner genuinely no longer knows what it played there.
MAX_REMEMBERED_ARMS = 512

# -- what one ingested event did to the learned policy, in one word ---------------------------
#
# The vocabulary :class:`ArmCredit` answers with. It rides back out of `POST /v1/trust-events`
# so that "the agent read the attribution" and "the agent quietly ignored it" are distinguishable
# from OUTSIDE this process — which is the whole complaint the attribution itself was built to
# answer, one layer down.

#: This runner keeps no learned state, so there is no policy for an outcome to move. Every
#: library caller that predates R17's loop is here, and none of them is broken.
CREDIT_NO_STATE = "no_learning_state"

#: The event names an auction this runner did not answer, or answered longer ago than
#: :data:`MAX_REMEMBERED_ARMS`. The posture still moves; there is simply no arm to credit.
CREDIT_UNREMEMBERED = "unremembered_auction"

#: Credited from the impression the trust service attributed the movement to: the rung actually
#: quoted, and `converted` as the outcome. This is the reading that makes a LOSS learnable.
CREDIT_IMPRESSION = "impression"

#: This auction's impression has already been folded. One impression is ONE Bernoulli trial, and
#: a converted auction goes on to produce several trust events — an `order_paid`, an
#: `order_fulfilled`, a `feedback` — each re-reporting the same `converted: true`. Booking each
#: would turn one sale into four wins, which is the "posterior that only ever rises" this reader
#: exists to avoid.
CREDIT_ALREADY = "already_credited"

#: Credited by the SIGN of the delta, which is what happened before an attribution rode along and
#: what still happens for every event that carries none.
CREDIT_DELTA_SIGN = "delta_sign"

#: Nothing to credit: no usable attribution, and a delta of exactly zero moved no dimension, so
#: it is silence about the arm rather than evidence against it.
CREDIT_NONE = "no_evidence"


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
class ArmCredit:
    """What ONE ingested trust event did to this store's learned policy.

    Returned rather than logged, because the whole defect class this closes is work that happens
    and cannot be observed: the trust service delivered an attribution for weeks and got a `200`
    back that said only what the POSTURE had become. A caller — the served door, a test, an
    operator with curl — can now tell "credited the 15% rung a win" from "ignored: that rung is
    past the merchant's cap" without reading this process's logs or its private state.
    """

    #: One of the ``CREDIT_*`` words above: what happened to the ARM.
    reason: str
    #: One of :data:`store_agent.learning.attribution.READING_REASONS`: what the pushed event's
    #: `trust_attribution` record turned out to say. `absent` is the ordinary case for an event
    #: from a trust build that predates the join.
    attribution: str = MISSING_ATTRIBUTION
    #: The auction the event named, verbatim.
    auction_id: str = ""
    #: The cluster the credited arm was played in — read off the arm this runner remembers, which
    #: is the only place it exists. It is NOT on the attribution: `cluster_id` lives on
    #: `auction_opened`, whose `store_id` is null, so it is not in the store's own ledger history
    #: and the trust service cannot supply it. See :meth:`AgentRunner.arm_for`.
    cluster_id: str | None = None
    #: The rung credited, as a FRACTION, or `None` when nothing was credited.
    discount_depth: float | None = None
    #: The Bernoulli outcome folded, or `None` when nothing was folded.
    won: bool | None = None
    #: Which observable produced the row — :data:`~store_agent.learning.OUTCOME_SOURCE` or
    #: :data:`~store_agent.learning.ATTRIBUTION_SOURCE` — and ``""`` when no row was produced.
    source: str = ""
    #: How many of this store's own outcomes the state has folded, AFTER this event. The served
    #: number that proves a push moved the policy rather than only the posture.
    observations: int = 0

    @property
    def credited(self) -> bool:
        return self.source != ""


@dataclass(frozen=True, slots=True)
class IntakeReport:
    """One ingested event: what was taken in, and what it did to the learned policy."""

    payload: TrustEventPayload
    credit: ArmCredit


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
    #: The arm this store's own policy played (R17: pitch variant x commitment set x depth), or
    #: `None` when this runner keeps no learning state. It is on the LOG and not only inside the
    #: bid because a merchant reading a shadow run needs to see which policy produced which
    #: answer, and because its `cluster_id` is the half of the loop's join key the trust door
    #: cannot supply — see :meth:`AgentRunner.arm_for`.
    arm: Arm | None = None


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

    The branch is spelled `isinstance(answer, Decline)` and NOT `is_decline(answer)`, which is
    the same test and reads better. `is_decline` is a `TypeGuard`, and a `TypeGuard` narrows
    only the branch where it is True (PEP 647); the `else` arm kept the full `Bid | Decline`
    and `_offer_clauses` wants a `Bid`, so the dispatch was correct at runtime and unprovable
    to the type checker. `isinstance` narrows BOTH arms natively — the `Decline` one to
    `Decline` and, because the union is closed at two members, the other one to `Bid`. Please
    do not "tidy" this back into the helper: the helper cannot express the negative half until
    `TypeIs` (PEP 742, Python 3.13) is available to this project.
    """
    clauses = _decline_clauses(answer) if isinstance(answer, Decline) else _offer_clauses(answer)
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

    __slots__ = (
        "_context",
        "_credited",
        "_deltas",
        "_learning",
        "_llm",
        "_mode",
        "_played",
        "_sink",
        "_store_id",
        "_submitter",
    )

    def __init__(
        self,
        context: Any,
        *,
        sink: Any,
        submitter: Any = None,
        mode: Any = None,
        llm: Any = None,
        learning: StoreLearningState | None = None,
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
        # The copywriter that writes this store's pitch onto `Bid.message` (D55). Held rather
        # than resolved per auction because building one may read a fixture off disk, and passed
        # straight through to `bid()` because provider selection is the composition root's
        # business and not the runtime's — see `store_agent.solicitation.copywriter`. `None` is
        # the ordinary case (no model configured) and yields the deterministic fallback pitch,
        # so every existing caller of this constructor keeps behaving exactly as it did.
        self._llm = llm
        self._deltas: dict[TrustDimension, list[float]] = {}
        # R17's loop. `None` — the default, and what every library caller that predates the loop
        # gets — means this runner selects no arm, overlays no policy, and behaves exactly as it
        # did: the bid is answered under whatever `learned_policy` the store context already
        # carried, static file or nothing. A state makes this runner the store's own advocate in
        # the R17 sense, and `store_agent.solicitation.advocate` gives every SERVED process one.
        self._learning = learning
        self._played: dict[str, Arm] = {}
        # Which auctions' impressions have already been folded, and what they said. A second ring
        # rather than a flag on the arm, because `Arm` is frozen and shared with the log entry the
        # sink already holds; see `CREDIT_ALREADY` for what it prevents. Bounded exactly as
        # `_played` is — an auction that has fallen out of `_played` can never be credited again
        # anyway, so this ring can only ever be the same size or smaller in practice.
        self._credited: dict[str, bool] = {}
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

    # -- R17: the store's own policy ----------------------------------------

    @property
    def learning(self) -> StoreLearningState | None:
        """This store's learned state, or `None` when this runner keeps none.

        Exposed read-only. It is replaced wholesale on every fold — `update` returns a new frozen
        state and cannot do otherwise — so a caller holding this value holds a snapshot that no
        later outcome can rewrite underneath it.
        """
        return self._learning

    def arm_for(self, auction_id: str) -> Arm | None:
        """The arm this runner played in `auction_id`, if it still remembers that auction.

        The loop's join key, and the ONE coordinate that exists nowhere else. The exchange never
        tells a store which arm it saw. The trust service now reconstructs part of one —
        `trust.feedback.attribution.impression_for` reads the terms back off the store's own
        `bid_placed` row, which is where the rung a verdict credits comes from — but it cannot
        reach the CLUSTER: `cluster_id` lives on `auction_opened`, whose `store_id` is null, so
        it is not in the store's own history and recovering it would mean reading rows that are
        not this store's. That half is here, taken off the `BidRequest` this runner answered, and
        it is why an auction that has fallen out of this ring can be ingested into the posture
        and credited to nothing.
        """
        return self._played.get(str(auction_id))

    def _select_arm(self, request: Any) -> tuple[Arm | None, Any]:
        """The arm to play, and the context to answer under. ``(None, context)`` with no loop.

        Two decisions, and both fail closed:

        * **An arm is chosen and remembered from the very first auction**, even a cold one, so
          the first verdict has something to credit. A loop whose cold arm was anonymous would
          never accumulate the first observation and would therefore never start.
        * **The context is only OVERLAID once this store has a record in this cluster.** A store
          that has run no auctions has learned nothing, so it is answered under exactly the
          policy its context already stated — which makes a cold bid byte-identical to what this
          agent served before the loop existed, and makes the shift a before/after rather than a
          claim.
        """
        state = self._learning
        context = self._context
        if state is None or not isinstance(context, Mapping):
            return None, context
        intent = _field(request, "intent")
        cluster_id = str(_field(intent, "cluster_id") or "")
        auction_id = str(_field(request, "auction_id") or "")
        if not cluster_id or not auction_id:
            return None, context
        # The seed is the AUCTION, so the draw is a pure function of (state, cluster, auction):
        # two identical runs play identical arms, and the same auction re-solicited plays the
        # same arm. There is no per-instance generator and no clock anywhere on this path.
        arm = sample_arm(
            state,
            cluster_id,
            auction_id,
            commitments=available_commitments(context.get("envelope")),
        )
        if state.cluster(cluster_id) is None:
            return cold_arm(cluster_id), context
        return arm, {
            **context,
            "learned_policy": policy_for_auction(
                state, cluster_id, arm, base=context.get("learned_policy")
            ),
        }

    def _remember(self, auction_id: str, arm: Arm) -> None:
        """Record the arm played, evicting the oldest when the ring is full. FIFO, so it is
        deterministic: a dict preserves insertion order, and the oldest key is the first one."""
        if not auction_id:
            return
        self._played[auction_id] = arm
        while len(self._played) > MAX_REMEMBERED_ARMS:
            self._played.pop(next(iter(self._played)))

    @property
    def authorized_depth(self) -> float | None:
        """The deepest discount this store's approved envelope authorises, as a FRACTION.

        `None` when the context states none this runner can read, which is NOT "anything goes":
        :func:`store_agent.learning.attribution.read_impression` still walls a wire-borne rung
        against the top of the depth grid, so an unreadable envelope costs the loop the tighter
        of the two walls and never all of them.

        Read LIVE off the context on every event, like every other envelope field except the
        activation snapshot — a merchant who narrows the cap must not have to restart the process
        for the loop to stop learning rungs it may no longer play.
        """
        return percent_as_fraction(_field(_field(self._context, "envelope"), "max_discount_pct"))

    def _credit(self, payload: TrustEventPayload) -> ArmCredit:
        """Fold ONE trust event into the arm that earned it, and say what that did.

        **Which outcome is folded.** If the event carries a usable `trust_attribution` (see
        :mod:`store_agent.learning.attribution`) the outcome is the impression's: `converted` is
        the win, `shown and not converted` is the LOSS, and the rung is the discount actually
        quoted rather than the one this runner sampled — a counter-proposal or a walled ask means
        the terms that earned the outcome are not the terms first pitched. Otherwise the outcome
        is the SIGN of the delta, which is exactly what this method did before and what it still
        does for every event that carries no attribution.

        **The cluster comes from the arm, and it has to.** `cluster_id` is not on the attribution
        and deliberately so: it lives on `auction_opened`, whose `store_id` is null, so it is not
        in the store's own ledger history and the trust service could not supply it without
        reading another store's rows. This runner already holds it — :meth:`_select_arm` reads it
        off the `BidRequest` it answered and :meth:`_remember` files the arm under that auction —
        so the join is local, free, and the reason an unremembered auction credits nothing.

        Five ways this legitimately folds nothing, and none of them is a failure: no learning
        state, an auction this runner did not answer or no longer remembers, an impression already
        folded, a delta of exactly zero with no usable attribution, and an attribution the reader
        refused. Every one of them is named in the returned :class:`ArmCredit`.

        What it never does is invent an outcome. The store agent still cannot observe award — see
        :mod:`store_agent.learning.outcomes` — so the only rows folded here are outcomes the
        platform actually pushed.
        """
        state = self._learning
        auction_id = str(_field(payload.event, "auction_id", "") or "")
        reading = read_impression(
            _field(payload.event, "payload"),
            auction_id=auction_id,
            max_depth=self.authorized_depth,
            depth_buckets=DEFAULT_DEPTH_BUCKETS if state is None else state.depth_buckets,
        )
        if state is None:
            return ArmCredit(CREDIT_NO_STATE, reading.reason, auction_id)

        seen = state.observations
        arm = self._played.get(auction_id)
        if arm is None:
            return ArmCredit(CREDIT_UNREMEMBERED, reading.reason, auction_id, observations=seen)

        outcome = reading.outcome
        if outcome is not None:
            if auction_id in self._credited:
                return ArmCredit(
                    CREDIT_ALREADY,
                    reading.reason,
                    auction_id,
                    cluster_id=arm.cluster_id,
                    observations=seen,
                )
            reason, source, won, depth = (
                CREDIT_IMPRESSION,
                ATTRIBUTION_SOURCE,
                outcome.won,
                outcome.depth,
            )
            self._credited[auction_id] = outcome.won
            while len(self._credited) > MAX_REMEMBERED_ARMS:
                self._credited.pop(next(iter(self._credited)))
        else:
            signed = verdict(payload.delta)
            if signed is None:
                return ArmCredit(
                    CREDIT_NONE,
                    reading.reason,
                    auction_id,
                    cluster_id=arm.cluster_id,
                    observations=seen,
                )
            reason, source, won, depth = CREDIT_DELTA_SIGN, OUTCOME_SOURCE, signed, arm.depth

        # The arm is copied rather than mutated: it is frozen, and the log entry the sink already
        # holds refers to the very same object — an arm edited here would rewrite an audit row
        # that was written auctions ago.
        played = arm if depth == arm.depth else replace(arm, depth=depth)
        self._learning = update(
            state, [outcome_row(played, store_id=self._store_id, won=won, source=source)]
        )
        return ArmCredit(
            reason,
            reading.reason,
            auction_id,
            cluster_id=arm.cluster_id,
            discount_depth=float(depth),
            won=bool(won),
            source=source,
            observations=self._learning.observations,
        )

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

    def _record(self, payload: TrustEventPayload) -> ArmCredit:
        """Absorb one ALREADY-VALIDATED payload: the posture first, then the policy.

        The one place either happens, so the single-event door and the batch door cannot drift
        into two opinions of what "ingested" means.
        """
        self._deltas.setdefault(payload.dim, []).append(float(payload.delta))
        return self._credit(payload)

    def ingest(self, event: Any) -> IntakeReport:
        """Take one pushed event in, and report BOTH of the things it moved.

        The posture is what this door has always answered with. The second half —
        :class:`ArmCredit` — is what the store's own policy did about it, and it is returned
        rather than kept private because "the attribution arrived and was ignored" and "the
        attribution arrived and taught the 15% rung" are indistinguishable from outside a process
        that only reports a posture. See :meth:`_accept` for what is refused outright.
        """
        payload = self._accept(event)
        return IntakeReport(payload=payload, credit=self._record(payload))

    def ingest_trust_event(self, event: Any) -> TrustEventPayload:
        """Take one pushed `TrustEventPayload` — a mapping or the model — into the posture.

        Returns the validated payload, so a caller that handed over a mapping can see what was
        actually taken in. :meth:`ingest` is the same call with the learned-policy half of the
        answer attached; this spelling is kept because it is what every existing caller uses and
        because "what did the door take in" is a complete question on its own.
        """
        return self.ingest(event).payload

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
            self._record(payload)
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
        arm, context = self._select_arm(request)
        answer = bid(request, context, llm=self._llm)
        auction_id = str(_field(answer, "auction_id", "") or "")
        if arm is not None:
            # Remembered off the ANSWER's auction id, not the request's: the entry is already
            # read back off the answer so that it cannot claim to be about an auction the answer
            # is not about, and an arm filed under a different id than the log entry would be an
            # arm no verdict could ever find.
            self._remember(auction_id, arm)
        posture = self.trust_posture
        entry = BidLogEntry(
            auction_id=auction_id,
            store_id=str(_field(answer, "store_id", "") or self._store_id),
            mode=self._mode,
            submitting=self.submits,
            rationale=rationale_for(answer, posture),
            trust_posture=posture.signals,
            answer=answer,
            arm=arm,
        )
        _emit(self._sink, entry, role="sink")
        if entry.submitting:
            _emit(self._submitter, _isolated(answer), role="submitter")
        return entry


__all__ = [
    "COLLABORATOR_METHODS",
    "CREDIT_ALREADY",
    "CREDIT_DELTA_SIGN",
    "CREDIT_IMPRESSION",
    "CREDIT_NONE",
    "CREDIT_NO_STATE",
    "CREDIT_UNREMEMBERED",
    "GUARDED",
    "MAX_REMEMBERED_ARMS",
    "NEUTRAL",
    "REINFORCED",
    "SUBMITTING_MODES",
    "AgentRunner",
    "ArmCredit",
    "BidLogEntry",
    "IntakeReport",
    "TrustPosture",
    "TrustSignal",
    "rationale_for",
]
