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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..profile import BUDGET_BANDS, TOP_BUDGET_BAND
from .errors import InvalidConstraint, InvalidPreference, UnstructuredIntent

__all__ = [
    "BUDGET_BAND_UNSPECIFIED",
    "BUDGET_BAND_VOCABULARY",
    "CONSTRAINT_OPS",
    "DEFAULT_CURRENCY",
    "INTENT_SCHEMA_VERSION",
    "MAX_CLARIFYING_QUESTIONS",
    "PREFERENCE_DIRECTIONS",
    "AuctionCreated",
    "ClarifyOutcome",
    "HardConstraint",
    "Intent",
    "Preference",
    "QuestionCapBroken",
    "coerce_intent",
    "intent_from_payload",
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
            raise InvalidConstraint(f"a hard constraint needs a field, got {self.field!r}")
        if self.op not in CONSTRAINT_OPS:
            raise InvalidConstraint(
                f"R19 hard constraints are eligibility filters: op must be one of "
                f"{list(CONSTRAINT_OPS)}, got {self.op!r}. An op this vocabulary cannot "
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
            raise InvalidPreference(f"a preference needs a field, got {self.field!r}")
        if self.direction not in PREFERENCE_DIRECTIONS:
            raise InvalidPreference(
                f"R19 preferences are scores: direction must be one of "
                f"{list(PREFERENCE_DIRECTIONS)}, got {self.direction!r}"
            )
        if isinstance(self.weight, bool) or not isinstance(self.weight, (int, float)):
            raise InvalidPreference(
                f"a preference carries a numeric weight, got {self.weight!r}. `True` is "
                f"not a weight even though Python will happily add it to one."
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
    unresolved: tuple[str, ...] = ()
    llm_calls: int = 0
    confirmed: bool = False

    _FIELDS = (
        "intent",
        "questions",
        "answers",
        "transcript",
        "unresolved",
        "llm_calls",
        "confirmed",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "questions", _as_text_tuple(self.questions))
        object.__setattr__(self, "answers", _as_text_tuple(self.answers))
        object.__setattr__(self, "transcript", _as_text_tuple(self.transcript))
        object.__setattr__(self, "unresolved", tuple(self.unresolved))
        object.__setattr__(self, "confirmed", False)
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


def intent_from_payload(payload: Mapping[str, Any]) -> Intent:
    """Rebuild an :class:`Intent` from its serialized form, validating as it goes."""
    constraints = [_constraint_from(item) for item in _sequence(payload.get("hard_constraints"))]
    preferences = [_preference_from(item) for item in _sequence(payload.get("preferences"))]
    return Intent(
        query=_clean(payload.get("query", "")),
        budget_band=_clean(payload.get("budget_band", "")),
        intent_id=_clean(payload.get("intent_id", "")),
        cluster_id=_clean(payload.get("cluster_id", "")),
        created_at=_clean(payload.get("created_at", "")),
        hard_constraints=tuple(constraints),
        preferences=tuple(preferences),
        category=_optional(payload.get("category")),
        ship_to=_optional(payload.get("ship_to")),
        currency=_clean(payload.get("currency") or DEFAULT_CURRENCY),
        schema_version=_clean(payload.get("schema_version") or INTENT_SCHEMA_VERSION),
    )


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
        raise InvalidConstraint(f"a hard constraint must be an object, got {item!r}")
    return HardConstraint(
        field=_clean(payload.get("field", "")),
        op=str(payload.get("op", "")),
        value=payload.get("value"),
        unit=_optional(payload.get("unit")),
    )


def _preference_from(item: Any) -> Preference:
    if isinstance(item, Preference):
        return item
    payload = _mapping_of(item)
    if payload is None:
        raise InvalidPreference(f"a preference must be an object, got {item!r}")
    return Preference(
        field=_clean(payload.get("field", "")),
        direction=str(payload.get("direction", "")),
        # Deliberately unchecked here: `Preference.__post_init__` is the one place that
        # decides what counts as a weight, and it rejects `True`, `None` and "1.0" with a
        # message naming the field. Narrowing the type here instead would move that
        # judgement into a second place that could disagree with it.
        weight=payload.get("weight"),  # type: ignore[arg-type]
    )
