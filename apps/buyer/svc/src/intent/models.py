"""The structured intent R1 produces, and the objects the loop hands back (T-071).

SPEC R1: *a shopping need becomes a confirmed structured intent after at most three
clarifying questions, and no auction exists before the buyer confirms.*

Three shapes live here.

:class:`Intent`
    DESIGN §Interfaces' ``Intent``, field for field. The R19 split is structural rather
    than conventional: a :class:`HardConstraint` **has no** ``weight`` attribute at all, so
    a ranker cannot silently score one while the buyer believes it filtered, and a
    :class:`Preference` **has no** ``op``, so a scoring term cannot silently become an
    eligibility gate. Neither class can be constructed outside its closed vocabulary.

:class:`ClarifyOutcome`
    What :func:`~buyer_svc.intent.clarify` returns: the questions that were actually asked
    (``len <= 3``), the intent that came out, and — deliberately — ``confirmed = False``.
    There is no way to build a ``ClarifyOutcome`` that claims confirmation, because
    clarification is not confirmation and the whole R1 invariant rests on that gap.

:class:`AuctionCreated`
    What :func:`~buyer_svc.intent.confirm` returns once the buyer has confirmed and the
    exchange has opened an auction. Its existence is the *only* evidence in this package
    that an auction exists at all.

Serialization
-------------
Every object here serializes through ``to_dict()`` and omits optional fields that are
``None`` rather than emitting nulls, so the payload handed to the exchange's
``POST /auctions`` validates against ``contracts.Intent`` (which is ``extra="forbid"``)
without carrying keys nobody set. ``apps/buyer/svc/tests/test_intent_models.py`` pins that
agreement against the real generated schema.

The budget-band vocabulary is **imported** from :mod:`buyer_svc.profile` rather than
restated: ``Intent.budget_band`` and ``BuyerProfile.buckets["budget_band"]`` are the same
band seen from two sides, and a vocabulary written down twice drifts.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..profile import BUDGET_BANDS, TOP_BUDGET_BAND
from .errors import (
    IntentTooLarge,
    InvalidConstraint,
    InvalidPreference,
    UnstructuredIntent,
)

__all__ = [
    "BUDGET_BAND_UNSPECIFIED",
    "BUDGET_BAND_VOCABULARY",
    "CONSTRAINT_OPS",
    "DEFAULT_CURRENCY",
    "INTENT_SCHEMA_VERSION",
    "MAX_CLARIFYING_QUESTIONS",
    "MAX_FREE_TEXT_CHARS",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_INTENT_BYTES",
    "MAX_INTENT_TERMS",
    "PREFERENCE_DIRECTIONS",
    "AuctionCreated",
    "ClarifyOutcome",
    "HardConstraint",
    "Intent",
    "Preference",
    "QuestionCapBroken",
    "check_intent_bounds",
    "coerce_intent",
    "intent_from_payload",
    "payload_weight",
]

#: R1's cap. Three, not "about three": the loop counts and stops.
MAX_CLARIFYING_QUESTIONS = 3

#: R19's closed set of eligibility-filter comparisons. Mirrors ``contracts.ConstraintOp``.
CONSTRAINT_OPS: tuple[str, ...] = ("eq", "lte", "gte", "in", "contains")

#: R19's closed set of scoring directions. Mirrors ``contracts.PreferenceDirection``.
PREFERENCE_DIRECTIONS: tuple[str, ...] = ("maximize", "minimize", "prefer")

#: The band the loop publishes when the buyer never named a budget and stopped answering.
#:
#: R1 requires a budget band on the confirmed intent, and the honest value for "we asked
#: and the buyer did not say" is not a made-up number — it is this label. Downstream it
#: means *no price ceiling was stated*: a ranker must not read it as a band and must not
#: invent one. It is deliberately outside the numeric labels so a filter written against
#: those cannot match it by accident.
BUDGET_BAND_UNSPECIFIED = "unspecified"

#: Every band label an :class:`Intent` may carry, taken from T-070's profile coarsener so
#: the two halves of the system cannot drift apart.
BUDGET_BAND_VOCABULARY: tuple[str, ...] = (
    *(label for _low, _high, label in BUDGET_BANDS),
    TOP_BUDGET_BAND,
    BUDGET_BAND_UNSPECIFIED,
)

#: The one currency this slice prices in; DESIGN pins ``Intent.currency`` as a field, not
#: as a guess, so it is stated rather than left null.
DEFAULT_CURRENCY = "USD"

#: ``Intent.schema_version``. Bumped when the field set changes, never silently.
INTENT_SCHEMA_VERSION = "1.0.0"


# --- what an anonymous caller may spend of this service (T-368) -----------------------
#
# ``POST /buyer/intent/confirm`` takes no credential, and what it accepts it KEEPS: the
# ``intent_id`` off the body becomes a key in the process-global
# :class:`~buyer_svc.intent.confirmation.ConfirmationLedger`, which has no capacity, no TTL,
# no LRU and no sweep, and whose ``release()`` frees only an UNSPENT claim. Measured before
# these ceilings existed: one anonymous request carrying a 30 000-character ``intent_id``
# answered **201** and left a permanent entry keyed by all 30 002 bytes of it; a burst grew
# linearly at ~30 127 B/request. So an anonymous caller chose how much of this service's
# storage to consume, permanently.
#
# The ceilings live HERE rather than on the route on purpose: :func:`confirm` is callable
# without FastAPI in scope, and it — not the handler — is what mints the ledger key. A bound
# that exists only on the handler is a bound with a door beside it.

#: The most characters an identifier-shaped field may run to: ``intent_id``, ``cluster_id``,
#: ``currency``, ``created_at``, ``schema_version``, ``budget_band``, ``category``, a
#: constraint's ``field`` and ``unit``, and a preference's ``field``.
#:
#: **Not a new opinion.** 128 is the ceiling the exchange already enforces on the identifiers
#: it is handed — ``exchange.auction.routes.MAX_IDENTIFIER_LENGTH`` — and the ``intent_id``
#: this service stores travels to that same ``POST /auctions``, so one request used to leave a
#: permanent entry in two processes. Restated rather than imported for the reason
#: :data:`buyer_svc.composition.MAX_ROSTER_ENTRIES` restates its 500: the buyer service does
#: not import the exchange's app package, and a cross-app import would make that true.
#: ``tests/test_intent_models.py`` pins the two numbers equal, so the copy cannot drift in
#: silence. An identifier is a name; a 20 KB one is a storage lever, not a name.
MAX_IDENTIFIER_LENGTH = 128

#: The most characters a free-text field may run to: ``query`` — the buyer's own words — and
#: ``ship_to``, plus any string a hard constraint carries as its ``value``.
#:
#: A shopping need stated once. This package already treats an utterance-sized string as 240
#: characters (:data:`~buyer_svc.intent.extraction.MAX_QUESTION_CHARS`), and the longest query
#: the shipped dialogue fixtures produce is 23. 2 KiB is two orders of magnitude above that
#: and still one order below the 30 000-character field T-368 measured this service storing.
MAX_FREE_TEXT_CHARS = 2048

#: The most elements any list on an intent may carry: ``hard_constraints``, ``preferences``,
#: and the members of an ``in``/``contains`` constraint value.
#:
#: The exchange refuses an intent carrying more than 64 hard constraints
#: (``exchange.auction.routes.MAX_HARD_CONSTRAINTS``) because its eligibility gate is
#: O(roster x constraints) and interpolates one exclusion reason per failure. An intent this
#: service would forward to that door has no reason to be built with more, and the same number
#: covers ``preferences`` because the ranker scores each of those per candidate too.
MAX_INTENT_TERMS = 64

#: The most bytes one intent document may weigh, counting every key and every string in it.
#:
#: **A third bound, and it is not implied by the other two.** 60 hard constraints (under the
#: count cap) each carrying a 2 000-character value (under the free-text cap) is a 120 KB
#: document that every per-field check accepts. So the budget is on the document TOGETHER —
#: the same shape, for the same reason, as ``exchange.auction.routes``'
#: ``MAX_HARD_CONSTRAINT_BYTES``. 32 KiB is ~500 bytes per term at the count cap, a generous
#: ``{"field": ..., "op": "eq", "value": ...}``, against a measured honest intent of 435
#: bytes (hand-written, two constraints) and 523 bytes (minted by :func:`clarify` from a
#: three-turn dialogue).
MAX_INTENT_BYTES = 32 * 1024

#: Punctuation one JSON element costs on the wire — a comma and a pair of quotes. Counted so
#: that a list of a million empty strings is not free to this service.
_ELEMENT_OVERHEAD_BYTES = 2

#: What a JSON number, boolean or null is worth to :func:`payload_weight`. Their text is
#: bounded by the parser, so they are charged a flat, deliberately generous eight bytes.
_SCALAR_WEIGHT_BYTES = 8


class QuestionCapBroken(AssertionError):
    """The loop broke its own R1 cap. An internal invariant, not a caller's mistake.

    ``AssertionError`` rather than an :class:`~buyer_svc.intent.errors.IntentError`,
    because it can only fire on a bug in *this* package: every construction path runs
    through :func:`~buyer_svc.intent.clarifier.clarify`, which counts.
    """


def _clean(text: object) -> str:
    """Whitespace-normalised text. The one place utterances become comparable."""
    return " ".join(str(text).split())


def _mapping_of(value: Any) -> Mapping[str, Any] | None:
    """Read a mapping out of a model object, or ``None`` when it is not one."""
    if isinstance(value, Mapping):
        return value
    for attr in ("to_dict", "model_dump", "dict"):
        fn = getattr(value, attr, None)
        if not callable(fn):
            continue
        try:
            candidate = fn()
        except Exception:  # noqa: BLE001 - a hostile object is simply not an intent
            continue
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _optional(value: Any) -> str | None:
    text = _clean(value) if value is not None else ""
    return text or None


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _as_text_tuple(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(_clean(value) for value in values if _clean(value))


def _as_plain(record: Any) -> Any:
    """A record's own ``to_dict`` if it has one, otherwise the record unchanged.

    ``ClarifyOutcome`` carries ``GapAnswer`` and ``SoftenedReading`` rows that live in
    ``buyer_svc.intent.extraction``, and importing them here would close the loop
    ``extraction -> models -> extraction``. Duck-typing the serialiser keeps the dependency
    pointing one way, which is why these fields are typed ``Any``.
    """
    to_dict = getattr(record, "to_dict", None)
    return to_dict() if callable(to_dict) else record


@dataclass(frozen=True)
class HardConstraint:
    """R19: an eligibility **filter** — ``field``/``op``/``value``, never weighted.

    There is no ``weight`` field on this class and there never may be. A weighted "hard"
    constraint is the failure R19 names: the buyer is told a requirement was a requirement
    while the ranker quietly trades it away against something else.
    """

    field: str
    op: str
    value: Any
    unit: str | None = None

    def __post_init__(self) -> None:
        if not _clean(self.field):
            raise InvalidConstraint(
                f"a hard constraint needs a field, got {_short_repr(self.field)}"
            )
        if self.op not in CONSTRAINT_OPS:
            raise InvalidConstraint(
                f"R19 hard constraints are eligibility filters: op must be one of "
                f"{list(CONSTRAINT_OPS)}, got {_short_repr(self.op)}. An op this vocabulary "
                f"cannot "
                f"express is dropped rather than coerced into a neighbouring one — "
                f"turning `lt` into `lte` silently widens what the buyer asked for."
            )

    @property
    def key(self) -> tuple[str, str]:
        """Identity for de-duplication: one filter per (field, op)."""
        return (self.field, self.op)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"field": self.field, "op": self.op, "value": self.value}
        if self.unit is not None:
            payload["unit"] = self.unit
        return payload


@dataclass(frozen=True)
class Preference:
    """R19: a **score** term — ``field``/``direction``/``weight``, never a gate."""

    field: str
    direction: str
    weight: float

    def __post_init__(self) -> None:
        if not _clean(self.field):
            raise InvalidPreference(f"a preference needs a field, got {_short_repr(self.field)}")
        if self.direction not in PREFERENCE_DIRECTIONS:
            raise InvalidPreference(
                f"R19 preferences are scores: direction must be one of "
                f"{list(PREFERENCE_DIRECTIONS)}, got {_short_repr(self.direction)}"
            )
        if isinstance(self.weight, bool) or not isinstance(self.weight, (int, float)):
            raise InvalidPreference(
                f"a preference carries a numeric weight, got {_short_repr(self.weight)}. "
                f"`True` is "
                f"not a weight even though Python will happily add it to one."
            )
        if not math.isfinite(self.weight):
            raise InvalidPreference(
                f"a preference weight must be a finite number, got "
                f"{_short_repr(self.weight)}. `1e400` is legal JSON and Python parses it to "
                f"`inf`; a score term weighted by infinity contributes no ordering, and NaN "
                f"contributes an undefined one."
            )
        object.__setattr__(self, "weight", float(self.weight))

    @property
    def key(self) -> str:
        """Identity for de-duplication: one score term per field."""
        return self.field

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "direction": self.direction, "weight": self.weight}


@dataclass(frozen=True)
class Intent:
    """The structured intent R1 shows the buyer for confirmation.

    ``query`` is the buyer's **own words**, never a paraphrase. That is not stylistic: the
    buyer is asked to confirm this object, and a summary they never said is a confirmation
    of something else. Everything the loop *derived* lives in the typed fields beside it,
    where it can be shown, corrected and argued with.
    """

    query: str
    budget_band: str
    intent_id: str
    cluster_id: str
    created_at: str
    hard_constraints: tuple[HardConstraint, ...] = ()
    preferences: tuple[Preference, ...] = ()
    category: str | None = None
    ship_to: str | None = None
    currency: str = DEFAULT_CURRENCY
    schema_version: str = INTENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not _clean(self.query):
            raise UnstructuredIntent("R1 requires a non-empty use case / query on the intent")
        if not _clean(self.budget_band):
            raise UnstructuredIntent("R1 requires a budget band on the intent")
        if not _clean(self.intent_id):
            raise UnstructuredIntent("an intent needs an id before it can create anything")
        object.__setattr__(self, "hard_constraints", tuple(self.hard_constraints))
        object.__setattr__(self, "preferences", tuple(self.preferences))

    def to_dict(self) -> dict[str, Any]:
        """The ``contracts.Intent``-shaped payload. Optional-and-unset keys are omitted."""
        payload: dict[str, Any] = {
            "intent_id": self.intent_id,
            "cluster_id": self.cluster_id,
            "query": self.query,
            "hard_constraints": [item.to_dict() for item in self.hard_constraints],
            "preferences": [item.to_dict() for item in self.preferences],
            "currency": self.currency,
            "budget_band": self.budget_band,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }
        if self.category is not None:
            payload["category"] = self.category
        if self.ship_to is not None:
            payload["ship_to"] = self.ship_to
        return payload

    def __getitem__(self, key: str) -> Any:
        payload = self.to_dict()
        if key in payload:
            return payload[key]
        raise KeyError(key)


@dataclass(frozen=True)
class ClarifyOutcome:
    """What the clarification loop produced, and what it had to ask to get there.

    ``confirmed`` is a field and it is always ``False`` — the constructor overwrites
    whatever it was handed. It is here so the object the UI renders answers "may this
    become an auction?" explicitly, rather than leaving every caller to infer it from the
    object's type.
    """

    intent: Intent
    questions: tuple[str, ...] = ()
    answers: tuple[str, ...] = ()
    transcript: tuple[str, ...] = ()
    #: Gaps the shopper was never asked about, or was asked and never answered. **Not**
    #: every gap still open: a gap they answered unusably belongs on ``understood``, and
    #: putting it here is what made the confirmation screen say "We never got an answer
    #: about: constraints" to a shopper who had just answered.
    unresolved: tuple[str, ...] = ()
    llm_calls: int = 0
    confirmed: bool = False
    #: One record per question actually put to the shopper: what was asked, what they said,
    #: and what this service managed to make of it. ``used`` empty is the honest form of
    #: "you answered and we could not use it".
    understood: tuple[Any, ...] = ()
    #: Must-haves kept but deliberately not enforced as eligibility filters, with the
    #: reason. See ``buyer_svc.intent.extraction.VOUCHED_FILTER_FIELDS``.
    softened: tuple[Any, ...] = ()
    #: Everything a model proposed that this service refused, with the reason. Write-only
    #: at HEAD — ``IntentDraft.dropped`` was extended and read by nobody — which is the same
    #: blindness that hid the dropped answer, one layer down.
    dropped: tuple[str, ...] = ()

    _FIELDS = (
        "intent",
        "questions",
        "answers",
        "transcript",
        "unresolved",
        "llm_calls",
        "confirmed",
        "understood",
        "softened",
        "dropped",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "questions", _as_text_tuple(self.questions))
        object.__setattr__(self, "answers", _as_text_tuple(self.answers))
        object.__setattr__(self, "transcript", _as_text_tuple(self.transcript))
        object.__setattr__(self, "unresolved", tuple(self.unresolved))
        object.__setattr__(self, "confirmed", False)
        object.__setattr__(self, "understood", tuple(self.understood))
        object.__setattr__(self, "softened", tuple(self.softened))
        object.__setattr__(self, "dropped", _as_text_tuple(self.dropped))
        if len(self.questions) > MAX_CLARIFYING_QUESTIONS:
            raise QuestionCapBroken(
                f"R1 caps the clarification loop at {MAX_CLARIFYING_QUESTIONS} questions; "
                f"this outcome carries {len(self.questions)}: {list(self.questions)!r}"
            )

    def __getitem__(self, key: str) -> Any:
        if key in self._FIELDS:
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return key in self._FIELDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict(),
            "questions": list(self.questions),
            "answers": list(self.answers),
            "transcript": list(self.transcript),
            "unresolved": list(self.unresolved),
            "llm_calls": self.llm_calls,
            "confirmed": self.confirmed,
            "understood": [_as_plain(record) for record in self.understood],
            "softened": [_as_plain(record) for record in self.softened],
            "dropped": list(self.dropped),
        }


@dataclass(frozen=True)
class AuctionCreated:
    """The receipt for the one auction one confirmation is allowed to create."""

    auction_id: str
    intent_id: str
    intent: Intent
    response: Any = None
    created_at: str = ""
    called: str = ""

    _FIELDS = ("auction_id", "intent_id", "intent", "response", "created_at", "called")

    def to_dict(self) -> dict[str, Any]:
        return {
            "auction_id": self.auction_id,
            "intent_id": self.intent_id,
            "intent": self.intent.to_dict(),
            "created_at": self.created_at,
            "called": self.called,
        }

    def __getitem__(self, key: str) -> Any:
        if key in self._FIELDS:
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return key in self._FIELDS


#: How deeply a hard constraint's ``value`` may nest before this package refuses to walk it.
#:
#: A filter value is a scalar, a string, or a list of those — ``{"field": "colour", "op":
#: "in", "value": ["navy", "black"]}``. Four levels is more than any of R19's ops can use.
#: The bound exists because that walk is recursive and a body of nothing but nested brackets
#: fits inside :data:`MAX_INTENT_BYTES` thousands of levels deep, which is how a size refusal
#: would have become a ``RecursionError`` and an unauthenticated 500.
MAX_VALUE_DEPTH = 4


def payload_weight(value: Any, budget: int = MAX_INTENT_BYTES) -> int:
    """How many bytes this document is worth, stopping as soon as it passes ``budget``.

    Deliberately **not** ``len(json.dumps(value))``: serializing a document in order to
    measure it allocates a second copy of exactly the thing that is too big. This walks the
    object with an explicit stack — iteratively, so a deeply nested body cannot reach a
    ``RecursionError`` and turn a refusal into a 500 — charging every string its UTF-8
    length, every key its own, every element :data:`_ELEMENT_OVERHEAD_BYTES` of punctuation,
    and every number, boolean and null a flat :data:`_SCALAR_WEIGHT_BYTES`.

    Past ``budget`` the return value means "over", not "how far over": the walk stops there.
    """
    total = 0
    stack: list[Any] = [value]
    while stack:
        if total > budget:
            return total
        item = stack.pop()
        if isinstance(item, str):
            if len(item) > budget:
                # Characters are never more numerous than UTF-8 bytes, so this string is
                # already over the budget, and encoding it to find out by how much would
                # allocate the whole of it a second time.
                return budget + 1
            total += len(item.encode("utf-8", "replace")) + _ELEMENT_OVERHEAD_BYTES
        elif isinstance(item, Mapping):
            total += _ELEMENT_OVERHEAD_BYTES
            for key, sub in item.items():
                total += len(str(key)) + _ELEMENT_OVERHEAD_BYTES
                stack.append(sub)
        elif isinstance(item, (list, tuple)):
            total += _ELEMENT_OVERHEAD_BYTES
            stack.extend(item)
        else:
            total += _SCALAR_WEIGHT_BYTES
    return total


def _refuse_length(name: str, actual: int, limit: int, unit: str = "characters") -> None:
    """The one refusal shape. It names the field and the ceiling, and never the value."""
    raise IntentTooLarge(
        f"intent field {name!r} is {actual} {unit}; this service stores at most {limit} "
        f"{unit} there. The value is not quoted back: a refusal that costs what accepting "
        f"cost is not a refusal."
    )


def _short_repr(value: Any, limit: int = 64) -> str:
    """A ``repr`` for an error message that a hostile value cannot make expensive."""
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}... ({len(text)} chars, elided)"


def _within_budget(payload: Any, *, what: str = "intent", budget: int = MAX_INTENT_BYTES) -> None:
    if payload_weight(payload, budget) > budget:
        raise IntentTooLarge(
            f"this {what} weighs more than {budget} bytes, which is more than this service "
            f"keeps for one shopping need. Nothing was read out of it, and none of it is "
            f"quoted back."
        )


def _bounded_text(value: Any, *, name: str, limit: int) -> str:
    """:func:`_clean`, with a ceiling."""
    text = _clean(value) if value is not None else ""
    if len(text) > limit:
        _refuse_length(name, len(text), limit)
    return text


def _bounded_optional(value: Any, *, name: str, limit: int) -> str | None:
    return _bounded_text(value, name=name, limit=limit) or None


def _bounded_raw(value: Any, *, name: str, limit: int) -> str:
    """``str(value)`` with a ceiling, for the fields a closed vocabulary judges verbatim.

    ``op`` and ``direction`` are matched against :data:`CONSTRAINT_OPS` /
    :data:`PREFERENCE_DIRECTIONS` exactly as they arrive — ``" eq "`` is not ``"eq"`` — so
    this bounds them without normalising them. Whitespace-normalising here would widen a
    closed vocabulary as a side effect of adding a length check.
    """
    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        _refuse_length(name, len(text), limit)
    return text


def _bounded_elements(value: Any, *, name: str) -> Sequence[Any]:
    items = _sequence(value)
    if len(items) > MAX_INTENT_TERMS:
        _refuse_length(name, len(items), MAX_INTENT_TERMS, unit="elements")
    return items


def _bounded_value(value: Any, *, name: str, depth: int = 0) -> Any:
    """A hard constraint's ``value``: bounded in length, in element count, and in depth."""
    if depth > MAX_VALUE_DEPTH:
        _refuse_length(name, depth, MAX_VALUE_DEPTH, unit="levels of nesting")
    if isinstance(value, str):
        if len(value) > MAX_FREE_TEXT_CHARS:
            _refuse_length(name, len(value), MAX_FREE_TEXT_CHARS)
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_INTENT_TERMS:
            _refuse_length(name, len(value), MAX_INTENT_TERMS, unit="keys")
        return {
            _bounded_text(key, name=name, limit=MAX_IDENTIFIER_LENGTH): _bounded_value(
                item, name=name, depth=depth + 1
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_INTENT_TERMS:
            _refuse_length(name, len(value), MAX_INTENT_TERMS, unit="elements")
        return [_bounded_value(item, name=name, depth=depth + 1) for item in value]
    return value


def check_intent_bounds(intent: Intent) -> None:
    """Refuse an intent this service will not store, whatever built it (T-368).

    Called twice on the confirmation path, and both are load-bearing:
    :func:`intent_from_payload` runs the ceilings against the WIRE document before it builds
    anything, and :func:`~buyer_svc.intent.confirmation.confirm` runs them against the
    resolved object before the ledger claim. The second is not a restatement of the first —
    :func:`coerce_intent` hands an :class:`Intent` back untouched, so a caller holding an
    object the wire never built would otherwise mint the permanent ledger key itself.
    """
    _within_budget(intent.to_dict())
    for name, limit in (
        ("intent_id", MAX_IDENTIFIER_LENGTH),
        ("cluster_id", MAX_IDENTIFIER_LENGTH),
        ("budget_band", MAX_IDENTIFIER_LENGTH),
        ("currency", MAX_IDENTIFIER_LENGTH),
        ("created_at", MAX_IDENTIFIER_LENGTH),
        ("schema_version", MAX_IDENTIFIER_LENGTH),
        ("category", MAX_IDENTIFIER_LENGTH),
        ("query", MAX_FREE_TEXT_CHARS),
        ("ship_to", MAX_FREE_TEXT_CHARS),
    ):
        text = getattr(intent, name, None)
        if isinstance(text, str) and len(text) > limit:
            _refuse_length(name, len(text), limit)

    for name, items in (
        ("hard_constraints", intent.hard_constraints),
        ("preferences", intent.preferences),
    ):
        if len(items) > MAX_INTENT_TERMS:
            _refuse_length(name, len(items), MAX_INTENT_TERMS, unit="elements")

    for constraint in intent.hard_constraints:
        _bounded_text(
            constraint.field, name="hard_constraints[].field", limit=MAX_IDENTIFIER_LENGTH
        )
        _bounded_raw(constraint.op, name="hard_constraints[].op", limit=MAX_IDENTIFIER_LENGTH)
        _bounded_optional(
            constraint.unit, name="hard_constraints[].unit", limit=MAX_IDENTIFIER_LENGTH
        )
        _bounded_value(constraint.value, name="hard_constraints[].value")
    for preference in intent.preferences:
        _bounded_text(preference.field, name="preferences[].field", limit=MAX_IDENTIFIER_LENGTH)


def intent_from_payload(payload: Mapping[str, Any]) -> Intent:
    """Rebuild an :class:`Intent` from its serialized form, validating as it goes.

    This is the wire path — the one an **unauthenticated** ``POST /buyer/intent/confirm``
    body takes — so the size checks come FIRST, before a single field is cleaned, copied or
    turned into an object. Work done on a document before deciding whether to keep it is
    work an anonymous caller chose for this service (T-368).
    """
    _within_budget(payload)
    constraints = [
        _constraint_from(item)
        for item in _bounded_elements(payload.get("hard_constraints"), name="hard_constraints")
    ]
    preferences = [
        _preference_from(item)
        for item in _bounded_elements(payload.get("preferences"), name="preferences")
    ]
    intent = Intent(
        query=_bounded_text(payload.get("query", ""), name="query", limit=MAX_FREE_TEXT_CHARS),
        budget_band=_bounded_text(
            payload.get("budget_band", ""), name="budget_band", limit=MAX_IDENTIFIER_LENGTH
        ),
        intent_id=_bounded_text(
            payload.get("intent_id", ""), name="intent_id", limit=MAX_IDENTIFIER_LENGTH
        ),
        cluster_id=_bounded_text(
            payload.get("cluster_id", ""), name="cluster_id", limit=MAX_IDENTIFIER_LENGTH
        ),
        created_at=_bounded_text(
            payload.get("created_at", ""), name="created_at", limit=MAX_IDENTIFIER_LENGTH
        ),
        hard_constraints=tuple(constraints),
        preferences=tuple(preferences),
        category=_bounded_optional(
            payload.get("category"), name="category", limit=MAX_IDENTIFIER_LENGTH
        ),
        ship_to=_bounded_optional(
            payload.get("ship_to"), name="ship_to", limit=MAX_FREE_TEXT_CHARS
        ),
        currency=_bounded_text(
            payload.get("currency") or DEFAULT_CURRENCY,
            name="currency",
            limit=MAX_IDENTIFIER_LENGTH,
        ),
        schema_version=_bounded_text(
            payload.get("schema_version") or INTENT_SCHEMA_VERSION,
            name="schema_version",
            limit=MAX_IDENTIFIER_LENGTH,
        ),
    )
    check_intent_bounds(intent)
    return intent


def coerce_intent(value: Any) -> Intent:
    """Read an :class:`Intent` out of whatever a caller handed us.

    Accepts an :class:`Intent`, a mapping (an intent that came back over the wire), or any
    object exposing ``to_dict``/``model_dump``/``dict``. Anything else — ``None`` most of
    all — is refused, because "confirm this" with nothing to confirm must never reach the
    exchange.
    """
    if isinstance(value, Intent):
        return value
    payload = _mapping_of(value)
    if payload is None:
        raise UnstructuredIntent(
            f"confirm() needs the structured intent the buyer confirmed; got "
            f"{type(value).__name__}. Pass the `intent` off a ClarifyOutcome, or its dict."
        )
    return intent_from_payload(payload)


def _constraint_from(item: Any) -> HardConstraint:
    if isinstance(item, HardConstraint):
        return item
    payload = _mapping_of(item)
    if payload is None:
        # `_short_repr`, not `{item!r}`: this refusal is rendered into an HTTP 4xx on an
        # unauthenticated door, and a message that quotes a 30 KB "constraint" back is the
        # same amplifier the size ceilings exist to remove.
        raise InvalidConstraint(f"a hard constraint must be an object, got {_short_repr(item)}")
    return HardConstraint(
        field=_bounded_text(
            payload.get("field", ""), name="hard_constraints[].field", limit=MAX_IDENTIFIER_LENGTH
        ),
        op=_bounded_raw(
            payload.get("op", ""), name="hard_constraints[].op", limit=MAX_IDENTIFIER_LENGTH
        ),
        value=_bounded_value(payload.get("value"), name="hard_constraints[].value"),
        unit=_bounded_optional(
            payload.get("unit"), name="hard_constraints[].unit", limit=MAX_IDENTIFIER_LENGTH
        ),
    )


def _preference_from(item: Any) -> Preference:
    if isinstance(item, Preference):
        return item
    payload = _mapping_of(item)
    if payload is None:
        raise InvalidPreference(f"a preference must be an object, got {_short_repr(item)}")
    return Preference(
        field=_bounded_text(
            payload.get("field", ""), name="preferences[].field", limit=MAX_IDENTIFIER_LENGTH
        ),
        direction=_bounded_raw(
            payload.get("direction", ""),
            name="preferences[].direction",
            limit=MAX_IDENTIFIER_LENGTH,
        ),
        # Still deliberately unjudged here: `Preference.__post_init__` is the one place that
        # decides what counts as a weight, and it rejects `True`, `None` and "1.0" with a
        # message naming the field. Narrowing the type here instead would move that
        # judgement into a second place that could disagree with it. What IS done here is
        # bounding it — the value is interpolated into that refusal, so an unbounded string
        # in `weight` was an unbounded 4xx body.
        weight=_bounded_value(  # type: ignore[arg-type]
            payload.get("weight"), name="preferences[].weight"
        ),
    )
