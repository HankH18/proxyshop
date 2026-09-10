"""Intent → retrieval predicates, and the R19 hard-constraint decision (T-031, acceptance 1).

Two jobs, and keeping them separate is the whole design:

* **Pushdown** turns the hard constraints a graph query *can* express into
  :class:`ingest.graph.AttributeFilter` objects, so Neo4j narrows the candidate set before
  it crosses the wire. This is an **optimisation**.
* **Decision** re-evaluates every hard constraint locally, against whatever the source
  actually returned. This is the **rule**.

They are separate because ``AttributeFilter`` cannot express two of the five pinned
``ConstraintOp`` values: it has no disjunction (``in``) and no substring predicate
(``contains``). A pushdown that *approximated* those would drop satisfying rows before
anyone could see it; a pushdown that ignored them would let violating rows through if the
local decision were the same code path that produced the filter. So ``in`` and ``contains``
push down **nothing** and are decided here, and every other op is decided here too even
though the graph already applied it. The cost is one predicate evaluation over an
already-narrow set; the benefit is that "retrieval returns hard-criteria-satisfying
candidates only" is a property of *this module*, not a property of whichever source is
plugged in — which is why the tests can drive a deliberately lenient source and still
assert it.

Fail closed, three ways (R19: "Neither ``unsupported`` nor ``ambiguous`` ever satisfies a
hard constraint"):

* an attribute the candidate does not carry → **not satisfied**;
* an attribute carried in a different unit than the constraint named → **not satisfied**;
* an op or value the module cannot decide → :class:`UndecidableCriterion` at construction,
  never a silent pass at evaluation time.

Canonicalisation goes through :func:`ingest.graph.model.slug` and
:func:`~ingest.graph.model.canonical_text` — the *same* functions
``AttributeFilter.as_parameter`` and ``AttributeValue.as_properties`` use. If the local
decision folded keys or values differently from the graph, a constraint could pass one and
fail the other, and which answer you got would depend on how many rows the index returned.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from contracts.ranking import preference_term_conflict
from ingest.graph import AttributeFilter
from ingest.graph.model import canonical_text, slug

__all__ = [
    "BUDGET_FIELDS",
    "BUDGET_OPS",
    "CONSTRAINT_OPS",
    "DEFAULT_CANDIDATE_LIMIT",
    "MAX_CANDIDATE_LIMIT",
    "NEUTRAL_ALIGNMENT",
    "PREFERENCE_DIRECTIONS",
    "CriterionVerdict",
    "HardCriterion",
    "MalformedIntent",
    "RefusedPreference",
    "RetrievalQuery",
    "SoftPreference",
    "UndecidableCriterion",
    "build_query",
    "is_budget_bound",
]

#: The pinned `ConstraintOp` vocabulary (`packages/contracts`). No regex: an eligibility
#: filter has to be decidable against an attribute value, not against a pattern language.
CONSTRAINT_OPS: tuple[str, ...] = ("eq", "lte", "gte", "in", "contains")

#: The constraint fields that state the buyer's budget in MONEY rather than a claim about a
#: product, folded through the same :func:`slug` that :attr:`HardCriterion.canonical_field`
#: folds with — so ``price_usd``, ``Price USD`` and ``price-usd`` are one field here and not
#: three.
#:
#: ``budget_band`` is deliberately absent, and it is the obvious wrong answer. The band is a
#: LOSSY PROJECTION minted *from* the parsed number
#: (``apps/buyer/svc/src/intent/extraction.py`` turns a ceiling into one of a handful of
#: buckets), so binding a money rule to it would judge a bid against a bucket the shopper never
#: said — and against a bound the buyer service, not the buyer, chose.
#:
#: **This lives here, in retrieval, and is re-exported by ``ranking.filters``** — which is
#: where it used to be defined and where :func:`~exchange.ranking.filters.budget_reasons` still
#: decides it. It moved down because THREE decisions have to agree about which fields are money
#: and only one of them is in ranking: this module's :meth:`HardCriterion.pushdown` must not
#: push a money bound into the catalogue query, this module's
#: :meth:`RetrievalQuery.exclusion_reasons` must not re-decide it against product attributes,
#: and ``ranking.filters.hard_constraint_reasons`` must keep it out of
#: ``verified_hard_fit_count``. ``ranking`` imports ``retrieval`` and not the other way round,
#: so a definition up there would have forced a second list of price spellings down here — and
#: a bound exempted in one list and not the other is precisely the defect this vocabulary was
#: extended to close.
BUDGET_FIELDS: frozenset[str] = frozenset({slug("price_usd")})

#: The ops that state a money BOUND. ``eq``, ``in`` and ``contains`` on a price field are not
#: budgets — "exactly $40" is a description of a product, not a ceiling — and they keep
#: whatever meaning they already had, decided against claims like any other constraint.
BUDGET_OPS: tuple[str, ...] = ("lte", "gte")

#: The pinned `PreferenceDirection` vocabulary.
PREFERENCE_DIRECTIONS: tuple[str, ...] = ("maximize", "minimize", "prefer")

#: What a preference contributes when nothing about it can be measured — no preferences at
#: all, zero total weight, or a numeric preference whose observed range is degenerate.
#: 0.5, never 0.0: "nothing to discriminate on" is not "worst possible", and the same
#: reasoning DESIGN applies to ``delivery_fit`` applies here.
NEUTRAL_ALIGNMENT = 0.5

#: How many candidates a retrieval returns when the caller does not say.
DEFAULT_CANDIDATE_LIMIT = 20

#: The largest limit a caller may ask for. A ceiling, not a preference: the limit is
#: multiplied by ``LOCAL_FILTER_OVERSAMPLE`` and again by ``candidate_products``' own
#: ``oversample``, so an unchecked ``limit=10**9`` becomes a ~4e10-row Cypher ``LIMIT`` —
#: a caller-controlled denial of service against the shared Neo4j, from one integer.
MAX_CANDIDATE_LIMIT = 500


class MalformedIntent(ValueError):
    """The intent cannot be turned into a retrieval query at all."""


class UndecidableCriterion(MalformedIntent):
    """A hard constraint this module cannot decide — refused, never silently satisfied."""


def _fields(obj: Any) -> Mapping[str, Any]:
    """Read a protocol object as a mapping, whether it arrived as one or as a model."""
    if isinstance(obj, Mapping):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dict(dump())
    raise MalformedIntent(
        f"expected a mapping or a pydantic protocol model, got {type(obj).__name__}"
    )


def _enum_value(raw: Any) -> str:
    """`ConstraintOp.eq` and `"eq"` must reach this module as the same string."""
    return str(getattr(raw, "value", raw))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class CriterionVerdict:
    """One constraint's answer for one candidate. ``reason`` is empty when satisfied."""

    satisfied: bool
    reason: str = ""


@dataclass(frozen=True)
class HardCriterion:
    """One `HardConstraint` — an eligibility FILTER (R19), never a weighted term.

    Attributes:
        field: the attribute key, in whatever spelling the intent used.
        op: one of :data:`CONSTRAINT_OPS`.
        value: the comparison operand. A list/tuple for ``in``, a string for ``contains``, a
            number for ``lte``/``gte``, and a bool, number or string for ``eq``.
        unit: the unit the constraint is stated in. When given, only attribute readings in
            that unit are eligible to satisfy it.
    """

    field: str
    op: str
    value: Any = None
    unit: str | None = None

    def __post_init__(self) -> None:
        if not str(self.field).strip():
            raise UndecidableCriterion("a hard constraint needs a non-empty field")
        object.__setattr__(self, "op", _enum_value(self.op))
        if self.op not in CONSTRAINT_OPS:
            raise UndecidableCriterion(
                f"{self.op!r} is not one of the pinned ConstraintOp values "
                f"{CONSTRAINT_OPS}; a constraint this module cannot decide is refused here "
                f"rather than silently counted as satisfied (R19)"
            )
        if self.op in ("lte", "gte") and not _is_number(self.value):
            raise UndecidableCriterion(
                f"{self.op!r} on {self.field!r} needs a numeric bound, got {self.value!r}"
            )
        if self.op == "in":
            if isinstance(self.value, (str, bytes)) or not isinstance(self.value, Sequence):
                raise UndecidableCriterion(
                    f"'in' on {self.field!r} needs a sequence of options, got {self.value!r}; "
                    f"a bare string is one option written as five characters, not an option set"
                )
            if len(self.value) == 0:
                raise UndecidableCriterion(
                    f"'in' on {self.field!r} was given an empty option set, which nothing can "
                    f"satisfy; drop the constraint or name the options"
                )
            object.__setattr__(self, "value", tuple(self.value))
        if self.op == "contains" and not isinstance(self.value, str):
            raise UndecidableCriterion(
                f"'contains' on {self.field!r} needs a string needle, got {self.value!r}"
            )

    @classmethod
    def from_mapping(cls, raw: Any) -> HardCriterion:
        fields = _fields(raw)
        try:
            return cls(
                field=str(fields["field"]),
                op=_enum_value(fields["op"]),
                value=fields.get("value"),
                unit=None if fields.get("unit") is None else str(fields["unit"]),
            )
        except KeyError as exc:
            raise MalformedIntent(
                f"hard constraint is missing {exc.args[0]!r}: {fields!r}"
            ) from exc

    @property
    def canonical_field(self) -> str:
        """The folded key — the same fold ``AttributeFilter.as_parameter`` applies."""
        return slug(self.field)

    def is_evidenced_by(self, attributes: Sequence[Mapping[str, Any]]) -> bool:
        """Do these readings say ANYTHING about this constraint's attribute?

        Not "is it satisfied" — :meth:`decide` answers that, and answers ``False`` for both
        "the reading contradicts you" and "there is no reading". Those two are the same
        verdict about one candidate and opposite facts about a *set* of them: a constraint
        some candidate carries a reading for is a filter doing its job, while one no
        candidate carries any reading for cannot narrow a shortlist and can only empty it.
        Telling them apart needs this predicate, and it folds the key through :func:`slug`
        exactly as :meth:`decide` does so the two cannot disagree about which key is which.
        """
        return any(
            slug(str(attribute.get("key", ""))) == self.canonical_field for attribute in attributes
        )

    def pushdown(self) -> AttributeFilter | None:
        """The graph-side filter for this constraint, or ``None`` when Cypher cannot say it.

        **The invariant, and the only one that matters here: a pushdown may never exclude a
        candidate the local decision would admit.** Pushdown is an optimisation; if it is
        narrower than the rule, the graph path silently returns fewer satisfying candidates
        than the rule promises — and no offline test can see it, because the double does not
        apply the filter at all. Every ``None`` below is that invariant being kept.

        Returns:
            An :class:`~ingest.graph.AttributeFilter` for ``lte``/``gte`` and for ``eq`` on a
            string or a bool; ``None`` for:

            * ``in`` and ``contains`` — ``AttributeFilter`` has no disjunction and no
              substring predicate, and a filter that merely *narrowed towards* them would
              discard rows the local decision would have admitted;
            * ``eq`` on a **number** — measured: the Cypher predicate is
              ``a.value_number = f.equals_number``, exact float equality, while
              :meth:`_equals` uses :func:`math.isclose`. A reading stored as
              ``0.30000000000000004`` against a constraint of ``0.3`` is dropped by Neo4j and
              admitted here, so pushing it down would make the graph strictly narrower than
              the rule. ``lte``/``gte`` are safe because both sides compare the same two
              floats with the same operator.
            * a **money bound** (:func:`is_budget_bound`) — the buyer's ceiling or floor. Not
              "Cypher expresses it badly" but "Cypher is being asked the wrong node": a price
              is not an attribute of a thing, it is a property of an ``Offer``, because it
              belongs to an offer at a moment with a currency and a timestamp. Measured on the
              nineteen-store demo graph, ``MATCH (o:Offer) RETURN keys(o)`` is exactly
              ``['offer_id', 'price', 'observed_at', 'currency', 'availability']``, while
              ``MATCH (a:AttributeValue) WHERE toLower(a.canonical_key) CONTAINS 'price' OR
              ... 'cost'`` returns **0** out of 147 distinct attribute keys. So an
              ``AttributeFilter('price_usd', max_number=...)`` joins ``HAS_ATTRIBUTE``, finds
              no reading on any product, and excludes **every** candidate the local decision
              would admit — the invariant above, violated maximally. Driven before the repair,
              ``POST /auctions`` with ``price_usd lte 2000`` on ``"a sofa"`` returned
              ``products_considered: 0`` against a corpus holding two sofas under $2,000, and
              ``lte 5000`` — above every price in the graph — returned 0 as well.

        The declined ops become :attr:`RetrievalQuery.local_only_criteria`, which is what
        makes :data:`~exchange.retrieval.sources.LOCAL_FILTER_OVERSAMPLE` fetch headroom for
        them.

        **Declining the money bound here is only half of it**, and the other half is next door
        in :meth:`RetrievalQuery.exclusion_reasons`. Every other ``None`` above is a constraint
        the local decision still answers; a money bound is one it *cannot* answer, for the same
        reason Cypher cannot. Measured on the live graph with only this edit applied, ``"a
        sofa"`` under $2,000 went from ``considered 0, eligible 0`` to ``considered 125,
        eligible 0`` — the identical empty shortlist with a different sentence on it.
        """
        if is_budget_bound(self):
            return None
        if self.op == "eq":
            if isinstance(self.value, bool):
                return AttributeFilter(self.field, value_bool=self.value, unit=self.unit)
            if _is_number(self.value):
                return None
            return AttributeFilter(self.field, value_string=str(self.value), unit=self.unit)
        if self.op == "lte":
            return AttributeFilter(self.field, max_number=float(self.value), unit=self.unit)
        if self.op == "gte":
            return AttributeFilter(self.field, min_number=float(self.value), unit=self.unit)
        return None

    def decide(self, attributes: Sequence[Mapping[str, Any]]) -> CriterionVerdict:
        """Decide this constraint against one candidate's attribute readings.

        Args:
            attributes: the candidate's attributes as
                :attr:`ingest.graph.Candidate.attributes` projects them —
                ``{key, value_string, value_number, value_bool, unit}`` with the **raw** key,
                which is why the key is folded here rather than compared literally.

        Returns:
            A :class:`CriterionVerdict`. Unsatisfied verdicts always carry a reason naming
            the field, because the reason is what a buyer-facing exclusion label is built
            from.
        """
        named = [
            attribute
            for attribute in attributes
            if slug(str(attribute.get("key", ""))) == self.canonical_field
        ]
        if not named:
            return CriterionVerdict(
                False,
                f"{self.field!r}: the candidate carries no such attribute, so the constraint "
                f"is undecidable and does not count as satisfied (R19)",
            )
        if self.unit is not None:
            wanted_unit = canonical_text(self.unit)
            in_unit = [
                attribute
                for attribute in named
                if attribute.get("unit") is not None
                and canonical_text(str(attribute["unit"])) == wanted_unit
            ]
            if not in_unit:
                seen = sorted({str(a.get("unit")) for a in named})
                return CriterionVerdict(
                    False,
                    f"{self.field!r}: no reading in unit {self.unit!r} (saw {seen}); a "
                    f"constraint stated in one unit is not decidable against another",
                )
            named = in_unit

        if any(self._matches(attribute) for attribute in named):
            return CriterionVerdict(True)
        return CriterionVerdict(
            False,
            f"{self.field!r} {self.op} {self.value!r} is not satisfied by "
            f"{[self._reading(a) for a in named]}",
        )

    # -- internals ---------------------------------------------------------------------

    def _matches(self, attribute: Mapping[str, Any]) -> bool:
        if self.op == "eq":
            return self._equals(attribute, self.value)
        if self.op in ("lte", "gte"):
            number = attribute.get("value_number")
            if number is None:
                return False
            bound = float(self.value)
            return float(number) <= bound if self.op == "lte" else float(number) >= bound
        if self.op == "in":
            return any(self._equals(attribute, option) for option in self.value)
        if self.op == "contains":
            text = attribute.get("value_string")
            if text is None:
                return False
            return canonical_text(str(self.value)) in canonical_text(str(text))
        return False  # unreachable: __post_init__ refuses every other op

    @staticmethod
    def _equals(attribute: Mapping[str, Any], expected: Any) -> bool:
        # bool BEFORE number: `isinstance(True, int)` is True, so a bool tested as a number
        # would compare against `value_number` and silently never match.
        if isinstance(expected, bool):
            return attribute.get("value_bool") is expected
        if _is_number(expected):
            number = attribute.get("value_number")
            return number is not None and math.isclose(
                float(number), float(expected), rel_tol=1e-9, abs_tol=1e-9
            )
        text = attribute.get("value_string")
        return text is not None and canonical_text(str(text)) == canonical_text(str(expected))

    @staticmethod
    def _reading(attribute: Mapping[str, Any]) -> Any:
        for key in ("value_string", "value_number", "value_bool"):
            if attribute.get(key) is not None:
                return attribute[key]
        return None


def is_budget_bound(criterion: HardCriterion) -> bool:
    """Is this criterion the buyer's stated budget, rather than a claim about a product?

    Keys on the field AND the op, because ``price_usd eq 40`` is a description of a product and
    ``price_usd lte 40`` is a wallet. Only the second is money this exchange has to decide
    against an offer; the first keeps its ordinary attribute meaning.

    Three callers, and they are the same rule stated three times rather than three rules:
    :meth:`HardCriterion.pushdown` declines to ask the catalogue,
    :meth:`RetrievalQuery.exclusion_reasons` declines to decide it locally, and
    ``ranking.filters.hard_constraint_reasons`` declines to count it towards
    ``verified_hard_fit_count`` (D13's first published tie-break, which a bidder must not be
    able to set). What is left is ``ranking.filters.budget_reasons``, which decides it once,
    against the offer's own price — the only place the number exists.
    """
    return criterion.canonical_field in BUDGET_FIELDS and criterion.op in BUDGET_OPS


@dataclass(frozen=True)
class SoftPreference:
    """One `Preference` — a SCORE term (R19), and one that only ever touches fit.

    DESIGN is explicit that ``Intent.preferences[].weight`` "feeds ``intent_match`` only; it
    never touches the published weights" of the rank formula. This module is where
    ``intent_match`` is produced, so this is where preference weights are allowed to exist —
    and the reason ``apps/exchange/src/ranking`` never sees them.
    """

    field: str
    direction: str
    weight: float

    def __post_init__(self) -> None:
        if not str(self.field).strip():
            raise MalformedIntent("a preference needs a non-empty field")
        object.__setattr__(self, "direction", _enum_value(self.direction))
        if self.direction not in PREFERENCE_DIRECTIONS:
            raise MalformedIntent(
                f"{self.direction!r} is not one of the pinned PreferenceDirection values "
                f"{PREFERENCE_DIRECTIONS}"
            )
        if not _is_number(self.weight) or not math.isfinite(float(self.weight)):
            raise MalformedIntent(
                f"preference {self.field!r} needs a finite numeric weight, got {self.weight!r}"
            )
        if float(self.weight) < 0.0:
            raise MalformedIntent(
                f"preference {self.field!r} has weight {self.weight}; a negative weight is a "
                f"constraint wearing a preference's clothes. Use direction='minimize'."
            )
        object.__setattr__(self, "weight", float(self.weight))

    @classmethod
    def from_mapping(cls, raw: Any) -> SoftPreference:
        fields = _fields(raw)
        try:
            return cls(
                field=str(fields["field"]),
                direction=_enum_value(fields["direction"]),
                weight=fields["weight"],
            )
        except KeyError as exc:
            raise MalformedIntent(f"preference is missing {exc.args[0]!r}: {fields!r}") from exc

    @property
    def canonical_field(self) -> str:
        return slug(self.field)

    @property
    def numeric(self) -> bool:
        """Whether this preference scores by magnitude (and so needs set normalisation)."""
        return self.direction in ("maximize", "minimize")

    def readings(self, attributes: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [
            attribute
            for attribute in attributes
            if slug(str(attribute.get("key", ""))) == self.canonical_field
        ]

    def magnitude(self, attributes: Sequence[Mapping[str, Any]]) -> float | None:
        """The numeric reading this preference scores on, or ``None`` when absent.

        A multi-valued attribute is reduced with ``max`` for ``maximize`` and ``min`` for
        ``minimize`` — the candidate is judged on its best reading in the declared direction,
        which is the only reduction that cannot be gamed by adding a second, worse value.
        """
        numbers = [
            float(attribute["value_number"])
            for attribute in self.readings(attributes)
            if attribute.get("value_number") is not None
        ]
        if not numbers:
            return None
        return max(numbers) if self.direction == "maximize" else min(numbers)

    def present(self, attributes: Sequence[Mapping[str, Any]]) -> bool:
        """Whether the candidate carries this attribute at all — what ``prefer`` scores."""
        return bool(self.readings(attributes))


@dataclass(frozen=True)
class RefusedPreference:
    """A preference this module will not score, and the published term that already does.

    ``contracts.ranking.PREFERENCE_FIELD_TERMS`` publishes the rule and this is where the
    exchange obeys it: **``intent_match`` must refuse every field in that map.** A preference
    on price is already scored by ``price_value``; one on delivery by ``delivery_fit``; one on
    trust by ``trust``. Letting any of them back in here would score one quantity twice under
    two published names and silently repartition the published weights, which contradicts
    DESIGN's "this is the only rank formula in the system".

    The hazard is measured rather than feared, and it is the reason this dataclass exists
    instead of a silent ``continue``. S1's intent carries exactly ONE preference —
    ``{price, minimize, 1.0}`` — and :func:`~exchange.retrieval.service._preference_alignments`
    min-max normalises across the eligible set, so an ``intent_match`` wired naively over that
    intent **is** normalised inverse price. Price's share of the published weight would go from
    ``w_v = 0.15`` to ``w_v + w_m = 0.50``, and the market SPEC's core tenet rules out — an
    allocation dominated by discount — would be the market this exchange runs.

    Attributes:
        field: the preference field as the buyer spelled it, kept verbatim so an operator
            reading a report recognises their own wording.
        term: the published feature that scores it instead.
    """

    field: str
    term: str

    @property
    def reason(self) -> str:
        """One sentence naming what took this preference, for a report or a response."""
        return (
            f"preference {self.field!r} is not scored by intent_match: the published term "
            f"{self.term!r} already scores it, and scoring it twice would repartition the "
            f"published weights"
        )


@dataclass(frozen=True)
class RetrievalQuery:
    """Everything a candidate source needs, plus the rule the result is judged against."""

    intent_id: str
    query_text: str
    criteria: tuple[HardCriterion, ...]
    preferences: tuple[SoftPreference, ...]
    category: str | None
    limit: int
    #: Preferences dropped by :func:`build_query` because a published term already scores
    #: them. Reported rather than merely absent: a store operator asking why its price
    #: preference moved nothing is owed the name of the term that took it.
    refused_preferences: tuple[RefusedPreference, ...] = ()

    @property
    def attribute_filters(self) -> tuple[AttributeFilter, ...]:
        """The subset of the criteria a graph query can apply for us."""
        return tuple(
            pushed for pushed in (c.pushdown() for c in self.criteria) if pushed is not None
        )

    @property
    def local_only_criteria(self) -> tuple[HardCriterion, ...]:
        """The criteria no source can pre-filter — the reason retrieval oversamples.

        A money bound is in here and is the one member :meth:`exclusion_reasons` does not
        decide either; it is decided further down the pipe, by
        :func:`~exchange.ranking.filters.budget_reasons` against the offer's own price. The
        membership is still right, because what this property is FOR is telling
        :data:`~exchange.retrieval.sources.LOCAL_FILTER_OVERSAMPLE` that rows will be discarded
        after the fetch: asking for ``limit`` rows when something later throws some away
        returns fewer satisfying candidates than were asked for, and it makes no difference to
        that arithmetic whether the thrower-away is this module or the budget wall. Measured on
        the live graph, the oversample takes a ceiling query's fetch from 25 products to 125,
        of which 60-91% carry an offer at or under the ceiling.
        """
        return tuple(c for c in self.criteria if c.pushdown() is None)

    def exclusion_reasons(self, attributes: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        """Every reason this candidate fails. Empty means it satisfies all hard criteria.

        All criteria are evaluated, not just the first failing one: an exclusion label that
        named one of three violated constraints would be a partial explanation presented as a
        complete one.

        **A money bound is not evaluated here at all**, and that is the same rule
        ``ranking.filters.hard_constraint_reasons`` already applies one layer up, stated once
        more rather than an exception to it. These ``attributes`` are the CATALOGUE's readings
        about a product; the buyer's ceiling is about an OFFER, and no reading a catalogue can
        carry answers it — there is no attribute for "what this will cost you today". Feeding
        it to :meth:`HardCriterion.decide` therefore does not decide it, it refuses everybody:
        ``decide`` correctly answers R19's "the candidate carries no such attribute" for every
        product in the corpus, and an exclusion filter that excludes the whole corpus is not a
        filter.

        Skipping it here is NOT dropping the bound. It is decided exactly once, by
        :func:`~exchange.ranking.filters.budget_reasons`, against the price the offer itself
        names — which is the only place the number exists and the only place it can be compared.
        ``ranking.filters.unanswerable_criteria`` skips it for the mirror-image reason:
        relaxing a ceiling would re-admit every bid the budget wall just excluded.

        Measured, and this is why the skip is not cosmetic: with the pushdown declined
        (:meth:`HardCriterion.pushdown`) and this loop left alone, ``"a sofa"`` under $2,000
        against the live nineteen-store graph answered ``considered 125, eligible 0`` — all 125
        refused for carrying no ``price_usd`` attribute. The shortlist was as empty as before
        the pushdown was touched. Both edits, or neither is worth making.
        """
        reasons: list[str] = []
        for criterion in self.criteria:
            if is_budget_bound(criterion):
                continue
            verdict = criterion.decide(attributes)
            if not verdict.satisfied:
                reasons.append(verdict.reason)
        return tuple(reasons)


def build_query(intent: Any, *, limit: int | None = None) -> RetrievalQuery:
    """Translate an `Intent` into a :class:`RetrievalQuery`.

    Args:
        intent: an `Intent` mapping or pydantic model (DESIGN §Interfaces).
        limit: how many candidates to return; defaults to :data:`DEFAULT_CANDIDATE_LIMIT`.

    Returns:
        The query, with every constraint and preference validated. Validation happens **here**
        rather than at scoring time so a malformed intent fails once, loudly, before any
        candidate has been judged against it.

        **A preference a published term already scores is REFUSED, not carried.** It lands on
        :attr:`RetrievalQuery.refused_preferences` and never reaches ``preferences``, so it
        cannot move the fit score and therefore cannot move ``intent_match``. The rule and the
        map are ``contracts.ranking``'s (``PREFERENCE_FIELD_TERMS`` /
        :func:`~contracts.ranking.preference_term_conflict`), whose own docstring says "a
        producer of ``intent_match`` calls this and drops every preference it answers for" —
        this is the producer, and this is the drop. It is enforced HERE rather than at the one
        call site that needs it because every consumer of retrieval goes through this
        function, and a repair that lives at one call site is one a second caller does not get.
        See :class:`RefusedPreference` for the measured hazard.

    Raises:
        MalformedIntent: the intent carries neither query text nor any structured predicate
            (the same rule :func:`ingest.graph.candidate_products` enforces — matching
            products by free-text name alone is forbidden by DESIGN, so there is no third
            option), or ``limit`` is not positive.
        UndecidableCriterion: a hard constraint names an op or a value this module cannot
            decide.
    """
    fields = _fields(intent)
    resolved_limit = DEFAULT_CANDIDATE_LIMIT if limit is None else int(limit)
    if resolved_limit <= 0:
        raise MalformedIntent(f"limit must be positive, got {resolved_limit}")
    if resolved_limit > MAX_CANDIDATE_LIMIT:
        raise MalformedIntent(
            f"limit {resolved_limit} exceeds MAX_CANDIDATE_LIMIT ({MAX_CANDIDATE_LIMIT}); the "
            f"limit is multiplied by the local-filter oversample and again by the index "
            f"oversample, so an unbounded one is a caller-controlled scan of the whole graph"
        )

    criteria = tuple(
        HardCriterion.from_mapping(raw) for raw in (fields.get("hard_constraints") or ())
    )
    # Validate EVERY preference first, then drop the ones a published term already scores.
    # In that order on purpose: a malformed `{price, sideways}` is still a malformed intent,
    # and silently discarding it because its field is refused anyway would make this door
    # more permissive for exactly the fields the rule exists to keep out.
    stated = tuple(SoftPreference.from_mapping(raw) for raw in (fields.get("preferences") or ()))
    refused = tuple(
        RefusedPreference(preference.field, conflict)
        for preference in stated
        if (conflict := preference_term_conflict(preference.field)) is not None
    )
    refused_fields = {row.field for row in refused}
    preferences = tuple(row for row in stated if row.field not in refused_fields)
    raw_category = fields.get("category")
    category = None if raw_category is None else str(raw_category)
    query_text = str(fields.get("query") or "").strip()

    if not query_text and not criteria and category is None:
        raise MalformedIntent(
            "an intent needs query text to embed or at least one structured predicate "
            "(hard_constraints, category). Retrieval by free-text product name alone is "
            "forbidden by DESIGN, so there is no third option."
        )

    return RetrievalQuery(
        intent_id=str(fields.get("intent_id") or ""),
        query_text=query_text,
        criteria=criteria,
        preferences=preferences,
        category=category,
        limit=resolved_limit,
        refused_preferences=refused,
    )
