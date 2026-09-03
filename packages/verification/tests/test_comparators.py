"""Adversarial unit tests for the deterministic comparator surface (T-062 gate).

These tests encode the properties R18 asks of a claim comparator, and each one is written so
that the *plausible wrong implementation* fails it:

* canonicalisation must make "0.5 kg" and ``{"value": 500, "unit": "g"}`` the same fact, so a
  formatting difference cannot be published as a contradiction;
* it must NOT make "2 business days" and "2 metres" the same fact, which is what a comparator
  that compared bare numbers after discarding an unrecognised unit would do;
* an unparseable claim is ``ambiguous``, never ``contradicted`` — "I could not tell" and "the
  seller was wrong" are different accusations;
* the numeric tolerance is the *published* per-field one, so every boundary here is derived
  from :data:`FIELD_TOLERANCES` and never from a literal a later edit could drift away from;
* only ``verified`` satisfies a hard constraint, including when the status is a string this
  package has never heard of.

Nothing here touches I/O, a clock, or a model: the comparators are pure functions of two
values, which is exactly what makes the C10 text-blindness claim in ``test_verifier.py``
enforceable rather than aspirational.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from claim_verification import (
    DECIDED_STATUSES,
    FIELD_TOLERANCES,
    UNDECIDED_STATUSES,
    UNIT_FAMILIES,
    VERIFICATION_STATUSES,
    ComparisonOutcome,
    InvalidVerificationStatus,
    compare,
    is_decided,
    normalize_boolean,
    normalize_text,
    parse_quantity,
    require_status,
    satisfies_hard_constraint,
    to_base_unit,
    tolerance_for,
    unit_family,
)
from claim_verification.normalize import BOOLEAN_FALSE, BOOLEAN_TRUE

# ---------------------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (500, (500.0, None)),
        (0.5, (0.5, None)),
        (-3, (-3.0, None)),
        ("500", (500.0, None)),
        ("500 g", (500.0, "g")),
        ("0.5 kg", (0.5, "kg")),
        ("  389.00  ", (389.0, None)),
        ("389.00USD", (389.0, "USD")),
        (".5 kg", (0.5, "kg")),
        ("1.5e2", (150.0, None)),
        ("2E-3 kg", (0.002, "kg")),
        ("+12 cm", (12.0, "cm")),
        ("-3 kg", (-3.0, "kg")),
        # A number followed by something that is NOT a unit still yields the trailing text,
        # so the caller can tell "no unit" from "a unit I do not know".
        ("2 business days", (2.0, "business days")),
        ("20%", (20.0, "%")),
        # No number anywhere: the honest answer is "not a quantity", which the caller turns
        # into `ambiguous` rather than into a string comparison of numbers.
        ("your mains voltage", None),
        ("", None),
        ("   ", None),
        ("kg", None),
        (None, None),
    ],
)
def test_parse_quantity_reads_the_shapes_a_pitch_actually_produces(
    value: Any, expected: tuple[float, str | None] | None
) -> None:
    """R18: every quantity spelling a pitch writes parses to ``(number, unit-or-None)``.

    Ints, floats, bare numeric strings, padded strings, scientific notation, a leading sign,
    a number with an unrecognised unit, and text with no number at all. The last group is
    ``None`` — a value the comparator must NOT guess at.
    """
    assert parse_quantity(value) == expected


@pytest.mark.parametrize("value", [True, False])
def test_parse_quantity_refuses_to_read_a_boolean_as_a_number(value: bool) -> None:
    """A boolean is not a quantity, even though ``bool`` is an ``int`` in Python.

    Without the explicit guard, ``True`` parses as ``1.0`` and a claim of "yes" against a
    catalog quantity of ``1`` would verify numerically — a coincidence of Python's type
    hierarchy published as evidence about a seller.
    """
    assert parse_quantity(value) is None


@pytest.mark.parametrize(
    ("number", "unit", "expected_value", "expected_family"),
    [
        (1, "kg", 1000.0, "mass"),
        (1, "lb", 453.59237, "mass"),
        (2.5, "cm", 0.025, "length"),
        (1, "psi", 0.0689475729, "pressure"),
        (2, "hours", 7200.0, "time"),
        (1, "g", 1.0, "mass"),
        # case and surrounding whitespace are not part of a unit's identity
        (1, "  KG  ", 1000.0, "mass"),
        (2, "Hours", 7200.0, "time"),
        (3, "CM", 0.03, "length"),
    ],
)
def test_to_base_unit_converts_within_a_family_regardless_of_spelling(
    number: float, unit: str, expected_value: float, expected_family: str
) -> None:
    """R18: a family's units are one scale, so grams and kilograms compare as one number.

    The conversion is also case- and whitespace-insensitive: a catalog that writes "KG" and a
    pitch that writes "kg" are recording the same unit, and treating them as two would
    manufacture a contradiction out of capitalisation.
    """
    value, family = to_base_unit(number, unit)
    assert family == expected_family
    assert value == pytest.approx(expected_value)
    assert unit_family(unit) is not None


@pytest.mark.parametrize("unit", ["business days", "USD", "widgets", "", "   ", None])
def test_an_unrecognised_unit_is_returned_unconverted_with_no_family(unit: Any) -> None:
    """An unknown unit converts nothing and claims no family, rather than defaulting to one.

    Silently assigning a family would let ``{"value": 389.0, "unit": "USD"}`` be compared as
    if dollars were metres. The ``None`` family is what lets :func:`compare` decide, case by
    case, whether the unknown label blocks the comparison.
    """
    assert unit_family(unit) is None
    assert to_base_unit(7, unit) == (7.0, None)


def test_normalize_boolean_accepts_every_published_spelling() -> None:
    """Every member of BOOLEAN_TRUE/BOOLEAN_FALSE parses, in any casing or padding.

    The two frozensets are the published vocabulary; a spelling listed there but not actually
    parsed would be a promise the module does not keep, and a comparator would answer
    ``ambiguous`` for a claim it advertises that it understands.
    """
    assert BOOLEAN_TRUE.isdisjoint(BOOLEAN_FALSE)
    for spelling in BOOLEAN_TRUE:
        assert normalize_boolean(spelling) is True, spelling
        assert normalize_boolean(f"  {spelling.upper()} ") is True, spelling
    for spelling in BOOLEAN_FALSE:
        assert normalize_boolean(spelling) is False, spelling
        assert normalize_boolean(f"  {spelling.upper()} ") is False, spelling


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        (1.0, True),
        (0.0, False),
        # Anything outside the published vocabulary is undecided, NOT false.
        ("maybe", None),
        ("", None),
        ("   ", None),
        ("yes please", None),
        ("2", None),
        (2, None),
        (-1, None),
        (None, None),
        ([], None),
        ({"value": True}, None),
    ],
)
def test_normalize_boolean_is_none_for_everything_outside_the_vocabulary(
    value: Any, expected: bool | None
) -> None:
    """Real bools and 0/1 parse; everything else is ``None`` — "could not be decided".

    ``None`` is what the caller turns into ``ambiguous``. A parser that returned ``False`` for
    an unrecognised value would turn every unparseable boolean claim into a contradiction and
    accuse a seller on the strength of a spelling it does not know.
    """
    assert normalize_boolean(value) is expected


# ---------------------------------------------------------------------------------------
# numeric comparison and the published tolerance
# ---------------------------------------------------------------------------------------


def test_a_numeric_claim_exactly_at_the_published_tolerance_verifies() -> None:
    """The boundary is inclusive, and it is derived from FIELD_TOLERANCES, not a literal.

    ``allowance = tolerance * max(|claimed|, |catalog|)``, so a claim that differs from the
    catalog by exactly the published fraction of the larger magnitude is still within
    tolerance. Writing the boundary as a number here would let a later edit to the published
    tolerance drift silently away from the test that is supposed to police it.
    """
    tolerance = FIELD_TOLERANCES["weight"]
    catalog = 100.0
    at_boundary = catalog - tolerance * catalog

    outcome = compare(at_boundary, {"value": catalog, "unit": "g"}, key="weight")
    assert outcome.status == "verified"
    assert str(tolerance) in outcome.reason

    # The allowance scales with the LARGER magnitude, so the same absolute gap on the high
    # side is comfortably inside it.
    assert (
        compare(catalog + tolerance * catalog, {"value": catalog, "unit": "g"}, key="weight").status
        == "verified"
    )


def test_a_numeric_claim_just_outside_the_published_tolerance_contradicts() -> None:
    """A hair past the published boundary is a contradiction, so the tolerance is a real edge.

    Paired with the boundary test above this pins the comparator to the published number from
    both sides: a comparator that quietly widened its tolerance would pass one of these two
    tests and fail the other.
    """
    tolerance = FIELD_TOLERANCES["weight"]
    catalog = 100.0
    just_outside = catalog - tolerance * catalog * 1.001

    outcome = compare(just_outside, {"value": catalog, "unit": "g"}, key="weight")
    assert outcome.status == "contradicted"
    assert str(tolerance) in outcome.reason


def test_unit_conversion_decides_which_side_of_the_boundary_a_claim_falls() -> None:
    """R18's headline case: "0.5 kg" against ``{"value": 500, "unit": "g"}`` is VERIFIED.

    And the conversion is not a rubber stamp — "0.6 kg" against the same 500 g is 20% out,
    far past the published ``net_weight`` tolerance, and contradicts. A comparator that
    compared the raw numbers would call the true claim (0.5 vs 500) contradicted and the
    false one (0.6 vs 500) contradicted too, i.e. it would be right for the wrong reason.
    """
    assert compare("0.5 kg", {"value": 500, "unit": "g"}, key="net_weight").status == "verified"
    assert compare("500 g", {"value": 0.5, "unit": "kg"}, key="net_weight").status == "verified"
    assert compare("0.6 kg", {"value": 500, "unit": "g"}, key="net_weight").status == "contradicted"


def test_a_mass_claim_against_a_length_attribute_contradicts() -> None:
    """Two recognised units from different families disagree about the fact, not the format.

    "500 g" against ``{"value": 500, "unit": "m"}`` is not a near-miss to be forgiven by a
    tolerance: the numbers are identical and the claim is still false. The reason names both
    families so the verdict can be argued with.
    """
    outcome = compare("500 g", {"value": 500, "unit": "m"}, key="length")
    assert outcome.status == "contradicted"
    assert "mass" in outcome.reason and "length" in outcome.reason


def test_a_bare_number_is_read_in_the_catalog_unit() -> None:
    """ "500" against ``{"value": 500, "unit": "g"}`` means 500 grams, and verifies.

    The corollary is the part a naive implementation gets wrong: because the bare number is
    read in the catalog's unit, "500" against ``{"value": 0.5, "unit": "kg"}`` is a claim of
    500 kg and must CONTRADICT. Converting the catalog to its base unit and then comparing
    the bare number against that would verify it — 500 g against 500 g — and silently agree
    with a claim a thousand times too large.
    """
    assert compare("500", {"value": 500, "unit": "g"}, key="net_weight").status == "verified"
    assert compare("500", {"value": 0.5, "unit": "kg"}, key="net_weight").status == "contradicted"


def test_an_unrecognised_unit_never_compares_as_a_bare_number() -> None:
    """ "2 business days" must not verify against "2 metres" — the numbers are not the fact.

    A recognised unit facing an unrecognised one falls through to the string comparator, so
    the verdict rests on the whole text rather than on a number stripped of a unit nobody
    modelled. Two identical unrecognised units still compare numerically, which is what makes
    "2 business days" vs "2 business days" verify.
    """
    assert (
        compare("2 business days", {"value": 2, "unit": "days"}, key="dispatch_window").status
        == "contradicted"
    )
    assert (
        compare("2 business days", {"value": 2, "unit": "m"}, key="length").status == "contradicted"
    )
    assert (
        compare("2 business days", {"value": "2 business days"}, key="dispatch_window").status
        == "verified"
    )
    assert (
        compare("3 business days", {"value": "2 business days"}, key="dispatch_window").status
        == "contradicted"
    )


def test_a_currency_label_does_not_block_a_numeric_price_comparison() -> None:
    """The approved golden set's price claim: "389.00" vs ``{"value": 389.0, "unit": "USD"}``.

    "USD" is a currency label, not a physical unit, and it sits on only one side of the
    comparison. Treating an unrecognised label as a blocking unit would push this through the
    string comparator — "389.00" != "389.0" — and publish the golden set's one true price
    claim as a contradiction. The comparison still has teeth: a price outside the tight money
    tolerance contradicts.
    """
    assert compare("389.00", {"value": 389.0, "unit": "USD"}, key="unit_price").status == "verified"
    assert (
        compare("380.00", {"value": 389.0, "unit": "USD"}, key="unit_price").status
        == "contradicted"
    )


# ---------------------------------------------------------------------------------------
# the comparator families
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claimed", "catalog", "expected"),
    [
        ("yes", True, "verified"),
        ("true", True, "verified"),
        (True, True, "verified"),
        ("no", False, "verified"),
        (False, False, "verified"),
        ("no", True, "contradicted"),
        ("yes", False, "contradicted"),
        (True, False, "contradicted"),
        # unparseable is UNDECIDED, never an accusation
        ("maybe", True, "ambiguous"),
        ("dishwasher friendly", False, "ambiguous"),
        ("the best you will ever own", True, "ambiguous"),
    ],
)
def test_boolean_claims_agree_disagree_or_stay_ambiguous(
    claimed: Any, catalog: bool, expected: str
) -> None:
    """A boolean attribute: agreement verifies, disagreement contradicts, noise is ambiguous.

    The last rows are the ones that matter. A comparator that folded "unparseable" into
    "does not equal the catalog value" would report a contradiction — positive evidence that
    the seller lied — every time a claim was phrased in words it had not been taught. The
    ambiguous rows check themselves against the published vocabulary rather than against a
    hard-coded idea of it, so growing BOOLEAN_TRUE/BOOLEAN_FALSE cannot leave this test
    asserting that a *recognised* spelling is undecidable.
    """
    if expected == "ambiguous":
        assert normalize_text(claimed) not in (BOOLEAN_TRUE | BOOLEAN_FALSE)
    assert compare(claimed, {"value": catalog}, key="dishwasher_safe").status == expected


def test_containment_verifies_a_present_member_and_contradicts_an_absent_one() -> None:
    """``op: "contains"``: an enumerated catalog list is a CLOSED assertion.

    A catalog that lists three ingredients is asserting those three, so "contains retinol" is
    false rather than unknown. Answering ``unsupported`` for the absent member would make
    every ingredient lie unfalsifiable, which is the exact R18 failure mode.
    """
    attribute = {"value": ["arabica beans", "robusta beans"]}
    assert (
        compare("arabica beans", attribute, key="ingredients", op="contains").status == "verified"
    )
    assert compare("retinol", attribute, key="ingredients", op="contains").status == "contradicted"


@pytest.mark.parametrize("attribute", [None, {"value": None}, [], {"value": []}, {"value": ()}])
def test_containment_against_nothing_is_unsupported_not_contradicted(attribute: Any) -> None:
    """An absent or empty attribute says nothing, so membership in it is UNSUPPORTED.

    The distinction from the test above is the whole four-status vocabulary in miniature:
    absence from a list the catalog actually enumerates is evidence, while absence of the
    list itself is silence. Collapsing the two would let an empty catalog contradict every
    claim ever made against it.
    """
    assert compare("anything", attribute, key="ingredients", op="contains").status == "unsupported"


def test_containment_normalises_members_before_deciding() -> None:
    """Membership runs the same canonicalisation equality does: "0.5 kg" matches "500 g".

    Otherwise containment would be the one comparator family where formatting decides the
    verdict, and a correctly-stocked size would read as a missing one.
    """
    attribute = {"value": ["250 g", "500 g"]}
    assert compare("0.5 kg", attribute, key="net_weight", op="contains").status == "verified"
    assert compare(" 250 G ", attribute, key="net_weight", op="contains").status == "verified"
    assert compare("1 kg", attribute, key="net_weight", op="contains").status == "contradicted"


def test_multi_valued_equality_verifies_a_stocked_value() -> None:
    """Equality against an attribute that admits several values: naming one of them verifies.

    A region-dependent SKU stocked at both 120 V and 230 V is a single attribute with two
    admitted values, and a claim that names either of them is true. Casing and spacing are
    normalised here exactly as they are for a single-valued attribute, so "230 v" is the same
    claim as "230 V".
    """
    attribute = {"value": ["120 V", "230 V"], "note": "region-dependent SKU"}
    assert compare("120 V", attribute, key="voltage").status == "verified"
    assert compare(" 230 v ", attribute, key="voltage").status == "verified"


def test_a_claim_that_names_no_value_of_the_attributes_kind_is_ambiguous() -> None:
    """Vagueness against a multi-valued attribute is undecidable, NOT a contradiction.

    A SKU stocked at both 120 V and 230 V does not make "works on your mains voltage" false —
    it makes it unanswerable, because the claim names no voltage at all. ``ambiguous`` is the
    honest verdict; a comparator that reported ``contradicted`` here would publish negative
    evidence against a seller for being unspecific, and the approved golden set pins this
    exact case (``gp-001-c-voltage``).
    """
    attribute = {"value": ["120 V", "230 V"], "note": "region-dependent SKU"}
    outcome = compare("your mains voltage", attribute, key="voltage")
    assert outcome.status == "ambiguous"
    assert outcome.observed == attribute["value"]


@pytest.mark.parametrize(
    ("claimed", "catalog", "expected"),
    [
        ("cotton", "cotton", "verified"),
        ("Cotton", "cotton", "verified"),
        ("  COTTON  ", "cotton", "verified"),
        ("heat  exchange", "heat exchange", "verified"),
        ("linen", "cotton", "contradicted"),
        ("cotton blend", "cotton", "contradicted"),
    ],
)
def test_string_equality_ignores_case_and_whitespace_but_not_content(
    claimed: str, catalog: str, expected: str
) -> None:
    """Casefolded, whitespace-collapsed equality — and nothing looser than that.

    "Cotton" and " cotton " are the same fact; "cotton" and "linen" are not, and neither are
    "cotton" and "cotton blend". A comparator that normalised by substring or prefix would
    verify the last row, which is how "100% single-origin arabica" gets to pass against a
    catalog that says "arabica beans".
    """
    assert compare(claimed, {"value": catalog}, key="material").status == expected


@pytest.mark.parametrize("claimed", [None, "", "   ", "\t\n"])
def test_a_claim_with_no_value_is_ambiguous(claimed: Any) -> None:
    """A claim that asserts nothing cannot be contradicted by a catalog that says something.

    This is the malformed-claim path: the pitch produced a claim row with an empty value, and
    the only honest verdict is that there was nothing to check. Falling through to the string
    comparator would compare "" against the catalog value and contradict.
    """
    outcome = compare(claimed, {"value": "cotton"}, key="material")
    assert outcome.status == "ambiguous"
    assert outcome.observed == "cotton"


# ---------------------------------------------------------------------------------------
# the published tolerance table
# ---------------------------------------------------------------------------------------


def test_tolerance_for_falls_back_to_the_published_default() -> None:
    """An unlisted field gets ``default``; a listed one gets its own, case-insensitively.

    The lookup normalises the key, so a catalog that writes "Price" and a table that writes
    "price" agree. A key nobody published must not raise and must not be 0 — it gets the
    default, which is what makes the table extendable without editing a comparator.
    """
    assert tolerance_for("a_field_nobody_published") == FIELD_TOLERANCES["default"]
    assert tolerance_for(None) == FIELD_TOLERANCES["default"]
    assert tolerance_for("price") == FIELD_TOLERANCES["price"]
    assert tolerance_for("  PRICE  ") == FIELD_TOLERANCES["price"]


def test_every_published_tolerance_is_a_usable_relative_fraction() -> None:
    """Every entry is in (0, 1), there is a ``default``, and none of them is a licence.

    A tolerance of 0 makes float round-tripping a contradiction and a tolerance of 1 makes
    every number verified, so both endpoints are excluded. The ceilings are the part with
    teeth, and they are here because every *boundary* test in this file derives its numbers
    from this table — which is right (a literal boundary drifts away from the published
    value) but means a silently widened tolerance would move those tests with it and be
    caught by nothing. So the table itself is policed: a quarter is the most any published
    field may forgive, the default stays inside 5%, and money stays inside 1% and strictly
    tighter than the default. A 2% "tolerance" on a price is a rounding error big enough to
    hide a real overcharge.
    """
    assert "default" in FIELD_TOLERANCES
    for field, tolerance in FIELD_TOLERANCES.items():
        assert isinstance(tolerance, float), field
        assert 0.0 < tolerance <= 0.25, field
    assert FIELD_TOLERANCES["default"] <= 0.05
    for money in ("price", "unit_price", "total_price", "list_price"):
        assert FIELD_TOLERANCES[money] <= 0.01, money
        assert FIELD_TOLERANCES[money] < FIELD_TOLERANCES["default"], money


def test_the_unit_table_is_internally_consistent() -> None:
    """Every family has exactly one base unit (factor 1.0) and only positive factors.

    A family with no factor-1.0 spelling has no base to convert to and a family with two of
    them is ambiguous about which; a zero or negative factor would turn a conversion into a
    sign error rather than a loud failure. ``unit_family`` must also round-trip every
    published spelling, or the table advertises units it cannot resolve.
    """
    for family, spellings in UNIT_FAMILIES.items():
        bases = [unit for unit, factor in spellings.items() if factor == 1.0]
        assert bases, family
        assert all(factor > 0 for factor in spellings.values()), family
        for unit in spellings:
            assert unit_family(unit) == (family, spellings[unit]), (family, unit)


# ---------------------------------------------------------------------------------------
# total-function guarantee
# ---------------------------------------------------------------------------------------

_HOSTILE_CLAIMS: tuple[Any, ...] = (
    None,
    "",
    "   ",
    0,
    -1,
    True,
    float("nan"),
    float("inf"),
    "x" * 5000,
    ["a", "b"],
    (),
    {"value": {"nested": [1, 2]}},
    {},
    b"500 g",
    "500 g",
    "0.5",
)

_HOSTILE_ATTRIBUTES: tuple[Any, ...] = (
    None,
    {},
    {"value": None},
    {"value": {}},
    {"value": []},
    {"value": [None, {"value": 1}]},
    {"value": "cotton", "unit": None},
    {"value": 500, "unit": "g"},
    {"value": {"type": "percentage", "value": 20.0}},
    [],
    "cotton",
    0,
    False,
    b"bytes",
)


def test_compare_is_total_and_never_raises() -> None:
    """Every (claim, attribute, op) combination returns one of the four statuses.

    :func:`compare` is called once per claim inside a loop over a whole pitch, and the whole
    point of the four-status vocabulary is that a value the comparator cannot handle is a
    VERDICT (``ambiguous``/``unsupported``) rather than an exception. If it raised, one
    malformed claim from one seller would take down the verification of every other claim in
    the pitch — the failure mode the verifier's docstring promises cannot happen.
    """
    checked = 0
    for claimed in _HOSTILE_CLAIMS:
        for attribute in _HOSTILE_ATTRIBUTES:
            for op in (None, "contains", "eq", "", "HAS", 5, ["contains"]):
                outcome = compare(claimed, attribute, key="net_weight", op=op)
                assert isinstance(outcome, ComparisonOutcome)
                assert outcome.status in VERIFICATION_STATUSES, (claimed, attribute, op)
                assert isinstance(outcome.reason, str) and outcome.reason
                checked += 1
    assert checked == len(_HOSTILE_CLAIMS) * len(_HOSTILE_ATTRIBUTES) * 7


def test_compare_is_a_pure_function_of_its_two_values() -> None:
    """The same inputs give an equal outcome, and neither argument is mutated.

    Purity is what makes a stored verdict re-checkable: a comparator that cached, mutated its
    attribute argument, or consulted anything outside its parameters would make the second
    verification of an unchanged pitch differ from the first.
    """
    attribute = {"value": ["arabica beans", "robusta beans"]}
    snapshot_of_attribute = {"value": ["arabica beans", "robusta beans"]}

    first = compare("arabica beans", attribute, key="ingredients", op="contains")
    second = compare("arabica beans", attribute, key="ingredients", op="contains")

    assert first == second
    assert first is not second
    assert attribute == snapshot_of_attribute


def test_comparison_outcome_equality_is_by_value_not_identity() -> None:
    """``ComparisonOutcome`` compares by (status, observed, reason) and is hashable.

    Verdicts get compared and de-duplicated downstream. Identity equality would make two
    runs of the same comparison unequal — quietly breaking every idempotency check built on
    top of it — and comparing against a non-outcome must yield ``False``, not an exception.
    """
    outcome = ComparisonOutcome("verified", 500, "the normalised values are equal")
    assert outcome == ComparisonOutcome("verified", 500, "the normalised values are equal")
    assert outcome != ComparisonOutcome("contradicted", 500, "the normalised values are equal")
    assert outcome != "verified"
    assert (
        len({outcome, ComparisonOutcome("verified", 500, "the normalised values are equal")}) == 1
    )


# ---------------------------------------------------------------------------------------
# the status vocabulary
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("verified", True),
        ("contradicted", False),
        ("unsupported", False),
        ("ambiguous", False),
        # An unrecognised status is not a reason to admit a claim; if anything it is a
        # stronger reason to refuse it.
        ("probably", False),
        ("VERIFIED", False),
        ("", False),
        (None, False),
        (True, False),
    ],
)
def test_only_verified_satisfies_a_hard_constraint(status: Any, expected: bool) -> None:
    """R18/R19: a promise the buyer refused to go without is satisfied by ``verified`` alone.

    ``unsupported`` or ``ambiguous`` reading as true is the named failure: it turns "we could
    not check" into "we checked and it is fine", and every unfalsifiable pitch becomes a
    compliant one. Note the deliberate asymmetry — this is NOT the negation of
    ``contradicted``: three of the four statuses fail a hard constraint.
    """
    assert satisfies_hard_constraint(status) is expected


def test_the_four_statuses_partition_into_decided_and_undecided() -> None:
    """DECIDED and UNDECIDED are a partition of the four, and ``is_decided`` follows it.

    The trust engine weights the two groups differently (D53/R19: undecided outcomes move
    coverage and confidence but never a dimension mean), so a status that fell into both
    groups, or neither, would be double-counted or silently dropped.
    """
    assert len(VERIFICATION_STATUSES) == 4
    assert len(set(VERIFICATION_STATUSES)) == 4
    assert DECIDED_STATUSES | UNDECIDED_STATUSES == set(VERIFICATION_STATUSES)
    assert DECIDED_STATUSES.isdisjoint(UNDECIDED_STATUSES)
    for status in VERIFICATION_STATUSES:
        assert is_decided(status) is (status in DECIDED_STATUSES)
    assert is_decided("probably") is False


def test_require_status_closes_the_vocabulary() -> None:
    """The four are accepted verbatim; anything else raises ``InvalidVerificationStatus``.

    R18 keeps the vocabulary closed on purpose: a fifth status would reach the trust engine
    as an observation type nobody weighted. The error is a ``ValueError`` subclass so a
    caller that already handles bad input keeps working, and it names the four in its message
    so the failure is self-explanatory.
    """
    for status in VERIFICATION_STATUSES:
        assert require_status(status) == status
    assert issubclass(InvalidVerificationStatus, ValueError)
    for rejected in ("maybe", "VERIFIED", "", None, 0):
        with pytest.raises(InvalidVerificationStatus) as raised:
            require_status(rejected)
        assert "verified" in str(raised.value)


def test_a_non_finite_claimed_number_is_undecidable_and_never_verified() -> None:
    """R18/R19: infinity and NaN are `ambiguous` — never `verified`, and never an exception.

    This case used to be an artefact rather than a decision, and the artefact was dangerous.
    The tolerance allowance is *relative* to the larger magnitude, so an infinite claim earned
    an infinite allowance and ``inf <= inf`` made ``float("inf")`` compare **verified** against
    every numeric attribute in the catalog. ``json.loads`` accepts ``Infinity`` by default, so a
    crafted pitch reached it.

    The comparator now refuses a non-finite claim before any arithmetic. `ambiguous` and not
    `contradicted`, because the two are different statements: a claim of infinity grams is not
    a false assertion about the goods, it is not an assertion the catalog can address at all —
    and R19 keeps undecidable claims off the dimension mean rather than scoring the seller for
    them. What matters most is the negative: it is not `verified`, so it can never satisfy a
    hard constraint.
    """
    assert math.isnan(float("nan"))
    for value in (float("nan"), float("inf"), float("-inf")):
        outcome = compare(value, {"value": 500, "unit": "g"}, key="weight")
        assert outcome.status == "ambiguous", (
            f"a non-finite claimed value {value!r} was decided as {outcome.status!r}; "
            "an unanswerable claim is ambiguous, and above all it is never verified"
        )
        assert satisfies_hard_constraint(outcome.status) is False

    # ...and a merely very large FINITE number still gets a real comparison, so the guard
    # above cannot be satisfied by refusing every big number.
    assert compare(1e308, {"value": 500, "unit": "g"}, key="weight").status == "contradicted"
    assert compare(500.0, {"value": 500, "unit": "g"}, key="weight").status == "verified"
