"""The impression a trust movement came from, read as ONE outcome on ONE arm (R17).

``trust.feedback.attribution.impression_for`` joins a store's own ledger rows on ``auction_id``
and says WHICH of the store's decisions earned a trust movement:
``{schema_version, auction_id, bid_ref, slot, order_ref, shown, converted, arm}``.
``trust.feedback.engine._wire_event`` parks it in the pushed event's ``payload`` under
:data:`ATTRIBUTION_KEY`, beside the disclosure report. It arrives at ``POST /v1/trust-events``,
the agent answers ``200``, and until this module existed **nothing read it**: the runner folded
``payload.delta`` into a per-dimension aggregate and credited the arm it played by the SIGN of
that delta.

WHY THE SIGN OF A DELTA IS NOT A WIN/LOSS RECORD
-------------------------------------------------
A delta answers "did this store's reputation move, and which way". The loop's posterior asks a
different question — "did the arm I played close a sale" — and the two genuinely disagree. A
``feedback`` event carrying ``matched_pitch: false`` is a NEGATIVE ``feedback_match`` delta on
an auction the buyer converted in; by the sign it is a loss for the arm, by the outcome it is a
win that earned a complaint. :func:`~store_agent.learning.state.sample_depth` Thompson-samples a
rung from a Beta posterior over *conversions*, so ``converted`` is the coordinate it is
estimating and the sign is a proxy that happens to correlate.

**And the sign cannot express the loss at all.** Every event that produces a delta produces a
non-zero one, so under the sign alone a rung accumulates a win whenever the platform said
something nice and a loss whenever it said something unkind — never "this arm was in front of a
shopper and did not close". ``shown: true, converted: false`` is that fact, and it is the half
that makes the posterior a posterior rather than a counter.

WHAT IS REFUSED, AND WHY EACH REFUSAL IS SAFE
-----------------------------------------------
This module never raises and never guesses. Every reading returns one of the words in
:data:`READING_REASONS`, and everything except :data:`USABLE` leaves the caller free to fall
back to the delta the event already carried — so a malformed, unversioned or unauthorised
attribution costs the loop one observation and costs the posture nothing.

The refusal with money behind it is :data:`UNAUTHORIZED_DEPTH`. The rung is read off the wire,
and a rung the merchant's envelope never authorised is not an arm this agent could have played:
tallying it would teach the loop to want a depth its own :meth:`~store_agent.hooks.tools.
ToolHooks.authorize_discount` will refuse forever, and a depth past the top of the grid would
not even be refused — :func:`~store_agent.learning.grid.bucket_index` snaps to the NEAREST rung,
so 0.30 against a grid that stops at 0.20 would silently become evidence about 0.20.

Nothing here reads a clock, a network or a random source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .grid import DEFAULT_DEPTH_BUCKETS, percent_as_fraction

__all__ = [
    "ATTRIBUTION_KEY",
    "ATTRIBUTION_SOURCE",
    "AUCTION_MISMATCH",
    "DEPTH_WALL_TOLERANCE",
    "MISSING_ATTRIBUTION",
    "NOT_SHOWN",
    "NO_ARM",
    "PERCENTAGE_DISCOUNT_TYPES",
    "READING_REASONS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "UNAUTHORIZED_DEPTH",
    "UNREADABLE_DISCOUNT",
    "UNRECOGNISED_SCHEMA_VERSION",
    "USABLE",
    "ImpressionOutcome",
    "ImpressionReading",
    "read_impression",
]

#: Where the impression rides inside a pushed ``LedgerEvent.payload``. The producer's own name
#: for it is ``trust.feedback.engine.TRUST_ATTRIBUTION_KEY``; the two are spelled independently
#: because this package does not import the trust service, and
#: ``test_impression_intake.py::test_the_key_this_agent_reads_is_the_key_the_trust_service_writes``
#: asserts they agree, so the duplication is guarded rather than trusted.
ATTRIBUTION_KEY = "trust_attribution"

#: The record shapes this reader understands. ``trust.feedback.attribution
#: .ATTRIBUTION_SCHEMA_VERSION`` is 1, and its own header says "a recipient that does not
#: recognise the version is expected to ignore the record" — so an unrecognised one is dropped
#: here rather than read optimistically field by field.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

#: Stamped on an outcome row this reader produced, so a fold can be told apart from one credited
#: by the sign of a delta (:data:`~store_agent.learning.outcomes.OUTCOME_SOURCE`). Nothing in
#: :func:`~store_agent.learning.state.update` reads it; it is there for whoever audits a state.
ATTRIBUTION_SOURCE = "trust_attribution"

#: ``Discount.type`` values meaning "``value`` is a percentage depth". The same closed set
#: ``store_agent.hooks.provenance.PERCENTAGE_DISCOUNT_TYPES`` walls a bid's discount with, and
#: spelled again here because ``hooks`` imports ``learning`` and the reverse would close a cycle.
#: A fixed-amount discount is REFUSED rather than converted: an amount cannot become a depth
#: without re-deriving the list price the offer was against, which this record does not carry.
PERCENTAGE_DISCOUNT_TYPES: frozenset[str] = frozenset({"percentage", "percent", "pct"})

#: Slack when walling a wire-borne depth against the envelope's cap and the grid's top rung.
#: Both sides are binary floats that travelled through JSON, and without this a quoted 15.0%
#: could fail to clear a cap of 15.0%. Far below anything a merchant could feel — the same size,
#: and for the same reason, as ``hooks.provenance.DISCOUNT_MATCH_TOLERANCE``.
DEPTH_WALL_TOLERANCE = 1e-9

# -- the vocabulary a reading answers with ----------------------------------------------------

#: The record is present, versioned, about this auction, and names a rung this agent could have
#: played. The ONLY reason that carries an outcome.
USABLE = "usable"

#: The event carried no ``trust_attribution`` at all — an older trust build, or a delta whose
#: chain said nothing (``impression_for`` returns ``None`` for an event naming no auction).
MISSING_ATTRIBUTION = "absent"

#: ``schema_version`` is missing, is not an integer, or is one this reader does not know.
UNRECOGNISED_SCHEMA_VERSION = "unrecognised_schema_version"

#: The record describes a DIFFERENT auction than the event it rode in on. A routing fault, not
#: this auction's news; crediting it would move the wrong arm and nothing downstream could tell.
AUCTION_MISMATCH = "auction_mismatch"

#: ``arm`` is null or unreadable, so the record says nothing about the terms that were played.
#: ``impression_for`` returns this when the chain carried no ``bid_placed`` offer for the
#: auction, which is a real condition on a ledger written by an older build.
NO_ARM = "no_arm"

#: The arm states a discount this reader cannot turn into a depth: an unknown ``type``, a fixed
#: money amount, or a ``value`` that is not a usable percentage.
UNREADABLE_DISCOUNT = "unreadable_discount"

#: The depth is past the merchant's approved cap or past the top of the grid. See the module
#: docstring: this is the refusal with money behind it.
UNAUTHORIZED_DEPTH = "unauthorized_depth"

#: Neither ``shown`` nor ``converted`` is true. The store bid and was benched, or the chain does
#: not yet say — either way no shopper saw this arm, so there is nothing to grade it on. Counting
#: it as a loss would tally the exchange's ranking decision as if it were the shopper's.
NOT_SHOWN = "not_shown"

#: Every word :func:`read_impression` may answer with. A tuple, so it cannot be appended to at
#: runtime and so ``store_agent.learning`` holds no mutable module-level container.
READING_REASONS: tuple[str, ...] = (
    USABLE,
    MISSING_ATTRIBUTION,
    UNRECOGNISED_SCHEMA_VERSION,
    AUCTION_MISMATCH,
    NO_ARM,
    UNREADABLE_DISCOUNT,
    UNAUTHORIZED_DEPTH,
    NOT_SHOWN,
)


@dataclass(frozen=True, slots=True)
class ImpressionOutcome:
    """One impression, as the loop needs it: the rung played, and whether it closed."""

    #: The auction this outcome is about. Equal to the event's own ``auction_id``.
    auction_id: str
    #: ``True`` when the buyer converted on this store's offer; ``False`` when the offer was
    #: SHOWN and did not convert. There is no third value — an impression nobody saw is not an
    #: outcome, and :data:`NOT_SHOWN` is the reading it gets instead.
    won: bool
    #: The discount depth actually quoted, as a FRACTION. ``0.0`` is a real rung — an offer at
    #: list price — and is distinct from "the record states no usable depth", which is refused.
    depth: float


@dataclass(frozen=True, slots=True)
class ImpressionReading:
    """What one pushed event's ``trust_attribution`` turned out to say. Never an exception."""

    #: One of :data:`READING_REASONS`.
    reason: str
    #: The outcome, and ``None`` for every reason except :data:`USABLE`.
    outcome: ImpressionOutcome | None = None

    @property
    def usable(self) -> bool:
        return self.outcome is not None


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _depth_of(arm: Mapping[str, Any]) -> float | None:
    """The arm's discount as a FRACTION, or ``None`` when it states none usably.

    An arm with no ``discount`` — the key absent, or explicitly null — is a bid at the catalog
    list price, which is the shallowest rung of the grid and a real arm the loop learns from.
    ``trust.feedback.attribution._arm`` is explicit about the same distinction on its side.
    """
    discount = arm.get("discount")
    if discount is None:
        return 0.0
    body = _mapping(discount)
    if body is None:
        return None
    raw_type = body.get("type")
    spelling = str(raw_type).strip().lower() if raw_type is not None else ""
    if spelling not in PERCENTAGE_DISCOUNT_TYPES:
        return None
    # `Discount.value` is a PERCENT (`apps/exchange/src/checkout/discounts.py` reads
    # `{"type": "percentage", "value": 20.0}` as 0.2), so the crossing is `percent_as_fraction`
    # and never `as_fraction` — the field's unit is stated, so there is nothing to infer.
    return percent_as_fraction(body.get("value"))


def read_impression(
    event_payload: Any,
    *,
    auction_id: str,
    max_depth: float | None = None,
    depth_buckets: Sequence[float] = DEFAULT_DEPTH_BUCKETS,
) -> ImpressionReading:
    """Read one pushed event's impression attribution. Total: it answers, it never raises.

    Args:
        event_payload: the pushed ``LedgerEvent.payload`` — an open mapping owned by whoever
            produced the event, so anything at all may be in it.
        auction_id: the auction the EVENT names. The record must agree; see
            :data:`AUCTION_MISMATCH`.
        max_depth: the deepest discount the merchant's envelope authorises, as a FRACTION, or
            ``None`` when this agent cannot read one. ``None`` is not "anything goes" — the
            grid's own top rung still walls it — it only means the envelope stated no opinion
            this reader could recover.
        depth_buckets: the rungs the state tallies. Only the widest one is read, as a ceiling.

    Returns:
        An :class:`ImpressionReading`. ``reason == USABLE`` is the only one carrying an outcome.
    """
    payload = _mapping(event_payload)
    record = _mapping(payload.get(ATTRIBUTION_KEY)) if payload is not None else None
    if record is None:
        return ImpressionReading(MISSING_ATTRIBUTION)

    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return ImpressionReading(UNRECOGNISED_SCHEMA_VERSION)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        return ImpressionReading(UNRECOGNISED_SCHEMA_VERSION)

    if str(record.get("auction_id") or "") != str(auction_id or ""):
        return ImpressionReading(AUCTION_MISMATCH)

    arm = _mapping(record.get("arm"))
    if arm is None:
        return ImpressionReading(NO_ARM)

    depth = _depth_of(arm)
    if depth is None:
        return ImpressionReading(UNREADABLE_DISCOUNT)

    ceiling = max(float(bucket) for bucket in depth_buckets) if depth_buckets else 0.0
    if max_depth is not None:
        ceiling = min(ceiling, float(max_depth))
    if depth > ceiling + DEPTH_WALL_TOLERANCE:
        return ImpressionReading(UNAUTHORIZED_DEPTH)

    # `is True` rather than truthiness, on both. These are declared booleans; a string, a number
    # or a null means the producer did not say, and reading "false" as a conversion is the
    # failure direction that mints wins out of nothing.
    if record.get("converted") is True:
        return ImpressionReading(USABLE, ImpressionOutcome(str(auction_id), True, depth))
    if record.get("shown") is True:
        return ImpressionReading(USABLE, ImpressionOutcome(str(auction_id), False, depth))
    return ImpressionReading(NOT_SHOWN)
