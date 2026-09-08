"""A stock phrase in prose is not a stock assertion — ``pitch._FlagRule`` (D59, R18/D55).

The defect this file grades. ``pitch.PITCH_RULES`` carries two ``_FlagRule``s that mint an
``in_stock`` boolean the moment a bare substring appears anywhere in a sentence: ``in stock`` /
``available now`` / ``ready to ship`` for ``True``, ``out of stock`` / ``sold out`` /
``back-ordered`` for ``False``. Neither looked at what the sentence DID with the phrase.

That was harmless while ``in_stock`` was in no crawl-shaped snapshot's vocabulary — every such
reading resolved nowhere, came back ``unsupported`` and was rewritten to ``ambiguous`` as the
exchange's own gap, and cost nobody anything. **D59 makes the fact decidable off the offer
block**, so these readings are now graded, and a ``contradicted`` one costs the store a published
``policy_penalties: -0.15`` and a ``catalog_claim_accuracy`` trust hit.

Measured before the guard landed, every sentence in :data:`HONEST_BUT_NOT_AN_ASSERTION` minted
``in_stock: False`` and graded ``contradicted`` **against a catalogue whose offer says
``in_stock`` — a platform that AGREES with the store**. The fourth minted ``False`` and ``True``
from one sentence, so the same store was ``contradicted`` and ``verified`` on one key at once.

The governing principle is this repository's own and it is stated in both directions, because
both directions are graded here:

* an honest store must not be penalised, and a reading that is not confidently about CURRENT,
  WHOLE-PRODUCT availability is not one this platform can confidently read — so it is not minted
  at all. Silence costs the seller exactly what not writing the sentence costs.
* **a suite that only proves suppression is passed by deleting the rules.** So every negative
  below has a positive twin: the rules must still fire, and a liar must still be caught, on the
  same run.
"""

from __future__ import annotations

from typing import Any

import pytest
from claim_verification.pitch import PITCH_RULES, decompose_pitch
from claim_verification.verifier import catalog_keys, verify

VERIFIER_VERSION = "test-stock-prose"
PRODUCT = "prod-1"

#: The crawl's clock, and a read twenty minutes later — inside the freshness window D59 holds
#: the stock fact to, so the platform's reading is current and every claim below really is
#: graded rather than excused. Shapes copied from ``test_stock_fact.py``.
CRAWLED_AT = "2026-09-07T12:00:00Z"
READ_FRESH = "2026-09-07T12:20:00Z"


def _crawled(availability: str = "in_stock") -> dict[str, Any]:
    """A snapshot exactly as the recorded corpus crawl produces one.

    ``attributes`` is EMPTY — ``ingest.adapters.mapping.build_upserts`` emits no attribute op —
    and the stock fact lives only on the offer block, which is the shape that made these
    readings decidable in the first place.
    """
    return {
        "snapshot_id": f"neo4j-crawl:store-1:{PRODUCT}",
        "captured_at": CRAWLED_AT,
        "read_at": READ_FRESH,
        "products": [
            {
                "product_ref": PRODUCT,
                "canonical_name": "Merino beanie",
                "status": "active",
                "attributes": {},
                "offer": {
                    "price": 28.0,
                    "currency": "USD",
                    "availability": availability,
                    "observed_at": "2026-06-01T09:00:00Z",
                },
            }
        ],
    }


def _readings(text: str, key: str = "in_stock") -> list[Any]:
    """Every value ``text`` mints on ``key``, in text order."""
    snapshot = _crawled()
    claims = decompose_pitch(text, store_id="store-1", vocabulary=catalog_keys(snapshot, PRODUCT))
    return [claim["value"] for claim in claims if claim["key"] == key]


def _graded(text: str, availability: str = "in_stock") -> list[tuple[Any, str]]:
    """``(claimed value, status)`` for every ``in_stock`` verdict ``text`` earns.

    Driven through the real comparator against a catalogue that AGREES with the store, because
    "an honest store is not penalised" is a statement about verdicts, not about readings.
    """
    snapshot = _crawled(availability)
    claims = decompose_pitch(text, store_id="store-1", vocabulary=catalog_keys(snapshot, PRODUCT))
    result = verify(
        {
            "pitch_id": "pitch-1",
            "store_id": "store-1",
            "product_ref": PRODUCT,
            "text": text,
            "claims": [{k: v for k, v in claim.items() if k != "provenance"} for claim in claims],
        },
        snapshot,
        VERIFIER_VERSION,
    )
    by_ref = {str(claim.get("claim_ref") or claim["key"]): claim for claim in claims}
    return [
        (by_ref[str(row.get("claim_ref") or row["key"])]["value"], row["status"])
        for row in result["claims"]
        if row["key"] == "in_stock"
    ]


#: The measured table. Each entry is ``(sentence, why it is not an assertion)``. Every one of
#: these minted ``in_stock: False`` and graded ``contradicted`` before the guard.
HONEST_BUT_NOT_AN_ASSERTION: tuple[tuple[str, str], ...] = (
    (
        "We never let this one go out of stock, and returns are free for 30 days.",
        "negated: the clause asserts the opposite of the phrase it contains",
    ),
    (
        "It has not been sold out since spring.",
        "negated and time-shifted: a statement about the spring, and a denial of it",
    ),
    (
        "Sold out twice last month; back on the shelf now.",
        "time-shifted: two past episodes, and the sentence says so",
    ),
    (
        "The 1 kg bag is sold out, but the 250 g is ready to ship today.",
        "per-package, both ways: one sentence, two listings, two opposite readings",
    ),
    (
        "If it does go out of stock we will tell you within the hour.",
        "conditional: a promise about a state that is not the case",
    ),
    (
        "Back-ordered orders ship separately at no extra cost.",
        "attributive: the phrase modifies 'orders', a shipping policy rather than this shelf",
    ),
)

#: The positive twins. A fix that deleted the two rules would pass every negative above.
GENUINE_ASSERTIONS: tuple[tuple[str, bool], ...] = (
    ("In stock and ready to ship today.", True),
    ("Sold out, sorry.", False),
    ("It is in stock today and only 7 left at this price.", True),
    ("Sold out for now; only 2 left when it returns.", False),
    ("A heat-exchanger boiler, in stock.", True),
    ("Available now.", True),
    ("This one is back-ordered.", False),
    ("Dual boiler, 15 bar pump, 2.9 L tank, 230 V, three-year warranty, in stock.", True),
)


# =====================================================================================
# The six sentences: no reading, and therefore no verdict to be penalised for
# =====================================================================================
@pytest.mark.parametrize(("text", "why"), HONEST_BUT_NOT_AN_ASSERTION)
def test_a_stock_phrase_that_is_not_an_assertion_mints_no_stock_reading(
    text: str, why: str
) -> None:
    """RED before the guard: every one of these minted ``in_stock: False``."""
    assert _readings(text) == [], f"{text!r} still mints a stock reading ({why})"


@pytest.mark.parametrize(("text", "why"), HONEST_BUT_NOT_AN_ASSERTION)
def test_none_of_them_costs_an_honest_store_a_contradiction(text: str, why: str) -> None:
    """The half that actually costs money, graded through the real comparator.

    The catalogue AGREES with the store — its offer says ``in_stock`` — so every verdict here
    was ``contradicted`` purely because the decomposer read a sentence the seller did not write.
    Each one was worth ``policy_penalties: -0.15`` and a ``catalog_claim_accuracy`` hit.
    """
    assert _graded(text) == [], f"{text!r} still earns a stock verdict ({why})"


def test_the_sentence_that_asserted_both_ways_at_once_yields_neither() -> None:
    """One sentence, one key, ``False`` AND ``True`` — the store contradicted and verified.

    Measured before the guard::

        'The 1 kg bag is sold out, but the 250 g is ready to ship today.'
            -> [(False, 'contradicted'), (True, 'verified')]
    """
    text = "The 1 kg bag is sold out, but the 250 g is ready to ship today."
    assert decompose_pitch(text, store_id="store-1", vocabulary=("in_stock",)) == []
    assert _graded(text) == []


def test_the_guard_declines_the_flag_without_silencing_the_rest_of_the_sentence() -> None:
    """Scoped to the reading it cannot make, not to the sentence that carries it.

    "returns are free for 30 days" is an honest, checkable commitment in the same sentence as
    an unreadable stock phrase, and it survives. A guard that dropped the whole sentence would
    cost the seller claims it did make.
    """
    text = "We never let this one go out of stock, and returns are free for 30 days."
    claims = decompose_pitch(text, store_id="store-1", vocabulary=("return_window_days",))
    assert [(claim["key"], claim["value"]) for claim in claims] == [("return_window_days", 30)]


# =====================================================================================
# The positive direction: the rules still fire, and a liar is still caught
# =====================================================================================
@pytest.mark.parametrize(("text", "expected"), GENUINE_ASSERTIONS)
def test_a_genuine_stock_assertion_still_mints_its_flag(text: str, expected: bool) -> None:
    """Deleting the two rules would pass every negative above. It does not pass this."""
    assert _readings(text) == [expected], text


@pytest.mark.parametrize(("text", "expected"), GENUINE_ASSERTIONS)
def test_a_genuine_stock_assertion_is_still_graded_in_both_directions(
    text: str, expected: bool
) -> None:
    """The honest store earns ``verified``; the same sentence over a crawl that says otherwise
    still earns ``contradicted``. Suppression that swallowed the liar would be the other half
    of this defect."""
    agreeing = "in_stock" if expected else "out_of_stock"
    disagreeing = "out_of_stock" if expected else "in_stock"
    assert _graded(text, agreeing) == [(expected, "verified")], text
    assert _graded(text, disagreeing) == [(expected, "contradicted")], text


def test_both_flag_rules_are_still_in_the_table_and_still_reach_a_trust_dimension() -> None:
    """A fix that removed the rules, or their published ``claim_type``, is not this fix."""
    flags = {rule.name: rule for rule in PITCH_RULES if rule.name in ("in_stock", "out_of_stock")}
    assert set(flags) == {"in_stock", "out_of_stock"}
    for rule in flags.values():
        # ``specifications`` is the term D18's approved table routes to
        # ``catalog_claim_accuracy``; an unpublished one makes the verdict unannounceable.
        assert rule.claim_type == "specifications"


# =====================================================================================
# Each guard, isolated, against its own minimal twin
# =====================================================================================
@pytest.mark.parametrize(
    ("suppressed", "twin", "expected"),
    [
        # negation
        ("We are not in stock.", "We are in stock.", True),
        ("This never goes out of stock.", "This is out of stock.", False),
        ("It is no longer sold out.", "It is sold out.", False),
        ("We ship without back-ordered delays.", "It is back-ordered.", False),
        # hypothetical / conditional / future
        ("If it goes out of stock we refund you.", "It went out of stock.", False),
        ("Unless it is sold out, it ships Monday.", "It is in stock.", True),
        ("We will be in stock again on Friday.", "We are in stock again.", True),
        # past tense and time shifts
        ("It was sold out last week.", "It is sold out.", False),
        ("It has been out of stock before.", "It is out of stock.", False),
        ("We used to be sold out constantly.", "We are sold out.", False),
        ("It sold out once in 2024.", "It sold out.", False),
        # attributive use: a policy about a class of things, not this shelf
        ("In stock items ship the same day.", "In stock, ships the same day.", True),
        ("Sold out sizes are restocked weekly.", "Sold out, restocked weekly.", False),
        # per-package scope
        ("The 500 g bag is sold out.", "The bag is sold out.", False),
        ("Our 250 ml bottles are in stock.", "Our bottles are in stock.", True),
    ],
)
def test_each_guard_declines_its_own_case_and_leaves_the_plain_assertion_alone(
    suppressed: str, twin: str, expected: bool
) -> None:
    """Every guard is paired with the sentence one word away from it that must still mint.

    Without the twin, each guard could be over-broad — a pattern that suppressed every sentence
    containing the phrase would satisfy the left column on its own.
    """
    assert _readings(suppressed) == [], suppressed
    assert _readings(twin) == [expected], twin


def test_a_negated_phrase_is_declined_rather_than_inverted() -> None:
    """ "not in stock" is not read as ``in_stock: False``, and that is deliberate.

    Inverting a negation is guessing at scope — "we are not in stock on the 1 kg" negates the
    package, not the product — and a guessed boolean is compared for equality and costs 0.15
    when it is wrong. Declining costs what silence costs.
    """
    assert _readings("We are not in stock.") == []
    assert _readings("It is not sold out.") == []


def test_two_opposite_readings_cancel_even_when_no_other_guard_applies() -> None:
    """The conflict rule alone, with nothing else to suppress the sentence.

    Neither clause is negated, conditional, past or package-scoped: both are plain present-tense
    assertions, and they contradict each other. One sentence cannot assert a boolean and its
    negation, so it asserts neither.
    """
    both = "The blue tin is sold out, but the red tin is ready to ship today."
    assert _readings(both) == []
    assert _graded(both) == []

    # And splitting the identical statement into two sentences must not make it gradeable —
    # punctuation is not evidence.
    assert _readings("The blue tin is sold out. The red tin is ready to ship today.") == []


def test_the_conflict_rule_does_not_reach_past_the_key_it_cancels() -> None:
    """Cancelling ``in_stock`` leaves every other key the pitch asserted standing."""
    text = (
        "The blue tin is sold out, but the red tin is ready to ship today. "
        "It comes with a two-year warranty."
    )
    claims = decompose_pitch(text, store_id="store-1", vocabulary=("in_stock", "warranty_months"))
    assert [(claim["key"], claim["value"]) for claim in claims] == [("warranty_months", 24)]


def test_the_same_cancellation_covers_the_one_other_equality_graded_family() -> None:
    """``boiler_type`` had the identical "convicted and vindicated at once" shape. Measured::

        "Unlike a dual boiler machine, this one is a heat exchanger."
            -> boiler_type "dual boiler"   -> contradicted   (-0.15)
            -> boiler_type "heat exchange" -> verified

    Same filter, same reason: a closed-vocabulary term read two ways in one pitch is one fact
    asserted twice and incompatibly, and it can only be right once. NUMBERS stay out of the
    filter on purpose — see :func:`pitch._drop_self_contradicting_readings` — so the pump,
    tank, voltage, warranty, dispatch and return rules are unchanged here.
    """
    contrastive = "Unlike a dual boiler machine, this one is a heat exchanger."
    assert decompose_pitch(contrastive, store_id="store-1", vocabulary=("boiler_type",)) == []

    # ...and one boiler type stated once is still read, so this is not the rule being deleted.
    plain = "This one is a heat exchanger."
    assert [
        (claim["key"], claim["value"])
        for claim in decompose_pitch(plain, store_id="store-1", vocabulary=("boiler_type",))
    ] == [("boiler_type", "heat exchange")]

    # Two NUMBERS on one key are still both read: two models is as likely a reading as a lie,
    # and cancelling them is a separate decision this filter deliberately does not take.
    two_pumps = "This runs a 9 bar pump. The older model ran a 15 bar pump."
    assert sorted(
        claim["value"]
        for claim in decompose_pitch(
            two_pumps, store_id="store-1", vocabulary=("pump_pressure_bar",)
        )
    ) == [9.0, 15.0]
