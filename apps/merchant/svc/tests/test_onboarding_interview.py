"""T-053 — reading a plain-language onboarding interview.

What the green here is evidence **of**: that the answer readers in
:mod:`merchant_svc.onboarding.interview` turn sentences into numbers, selections and
refusals *deterministically*, and that every way an interview can be incomplete is a refusal
rather than a silently empty envelope field. A wall that failed to parse is a wall the store
believes it has and does not, which is worse than no wall at all.

What it is **not** evidence of: that a merchant's real answers are phrased like these. The
readers here are a closed grammar, not natural-language understanding; an answer outside it
raises, which is the point.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from merchant_svc.onboarding.interview import (
    AnswerNotUnderstood,
    TranscriptRejected,
    declines,
    read_money,
    read_percentage,
    read_selection,
    read_shop_host,
    read_transcript,
    words_to_number,
)


# --------------------------------------------------------------------------------------
# Numbers a merchant actually says
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("twenty", 20.0),
        ("ninety-five", 95.0),
        ("ninety five", 95.0),
        ("one hundred", 100.0),
        ("one hundred and fifty", 150.0),
        ("two thousand", 2000.0),
        ("fifteen", 15.0),
        ("zero", 0.0),
    ],
)
def test_number_words_read_as_numbers(phrase: str, expected: float) -> None:
    assert words_to_number(phrase) == expected


@pytest.mark.parametrize(
    "phrase", ["", "the wool", "and", "forty-ish", "twenty twenty twenty-plus"]
)
def test_a_phrase_that_is_not_a_number_reads_as_none(phrase: str) -> None:
    """``None``, never ``0``. A failed parse that returns zero is a floor of nothing."""
    assert words_to_number(phrase) is None


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("20%", 20.0),
        ("up to 20 percent", 20.0),
        ("Twenty percent off, and that's the ceiling", 20.0),
        ("no more than 12.5%", 12.5),
        ("about 5 per cent", 5.0),
    ],
)
def test_a_discount_ceiling_is_read_out_of_a_sentence(answer: str, expected: float) -> None:
    assert read_percentage(answer) == expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Let's cap it at $500 a month.", 500.0),
        ("500 dollars", 500.0),
        ("Ninety-five dollars. Not a cent under.", 95.0),
        ("Nothing under forty dollars — below that the shipping eats the order.", 40.0),
        ("$1,200 a month", 1200.0),
        ("$49.99", 49.99),
    ],
)
def test_a_price_is_read_out_of_a_sentence(answer: str, expected: float) -> None:
    assert read_money(answer) == expected


def test_an_answer_naming_two_different_amounts_is_refused_not_guessed() -> None:
    """Choosing between "$40" and "$95" is choosing the merchant's floor for them."""
    with pytest.raises(AnswerNotUnderstood, match="2 different money values"):
        read_money("somewhere between $40 and $95, your call")


def test_an_answer_naming_the_same_amount_twice_is_not_ambiguous() -> None:
    assert read_money("forty dollars. $40, final.") == 40.0


def test_an_answer_with_no_number_reads_as_none() -> None:
    assert read_money("no, nothing special there") is None
    assert read_percentage("whatever you think is fair") is None


# --------------------------------------------------------------------------------------
# Refusals versus values
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "answer",
    [
        "No.",
        "No — we don't price match.",
        "No, nothing special there — the store-wide number is fine.",
        "none",
        "Not really, no.",
        "nope",
    ],
)
def test_a_refusal_is_recognised_as_one(answer: str) -> None:
    assert declines(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "No questions asked returns for 30 days",
        "Nothing under forty dollars — below that the shipping eats the order.",
        "Free returns for 30 days.",
        "Ships within 2 business days.",
    ],
)
def test_an_answer_that_merely_starts_with_no_is_not_a_refusal(answer: str) -> None:
    """ "No questions asked returns" is a promise. Reading it as a refusal would drop it."""
    assert declines(answer) is False


# --------------------------------------------------------------------------------------
# Choosing from what was on screen
# --------------------------------------------------------------------------------------
_OPTIONS = [
    {"cluster_id": "cluster-warm-layers", "label": "warm layers"},
    {"cluster_id": "cluster-trail-running", "label": "trail running"},
    {"cluster_id": "cluster-camp-cooking", "label": "camp cooking"},
]


def test_a_selection_keeps_the_order_the_options_were_offered_in() -> None:
    assert read_selection("Trail running and warm layers, please.", _OPTIONS) == [
        "cluster-warm-layers",
        "cluster-trail-running",
    ]


@pytest.mark.parametrize(
    "answer",
    [
        "Warm layers for sure, and trail running. Camp cooking isn't really us.",
        "Warm layers and trail running, but not camp cooking.",
        "Warm layers and trail running. No camp cooking.",
        "Warm layers, trail running. Skip camp cooking.",
    ],
)
def test_a_cluster_the_merchant_declined_is_not_pursued(answer: str) -> None:
    """R6 in miniature: pursuing a cluster the merchant said no to spends their budget."""
    assert read_selection(answer, _OPTIONS) == [
        "cluster-warm-layers",
        "cluster-trail-running",
    ]


def test_a_cluster_named_only_in_a_negated_clause_is_never_selected() -> None:
    assert read_selection("Definitely not camp cooking.", _OPTIONS) == []


def test_an_option_with_no_id_or_label_is_refused() -> None:
    with pytest.raises(AnswerNotUnderstood):
        read_selection("warm layers", [{"label": "warm layers"}])
    with pytest.raises(AnswerNotUnderstood):
        read_selection("warm layers", [{"cluster_id": "c1"}])


# --------------------------------------------------------------------------------------
# Naming the shop
# --------------------------------------------------------------------------------------
def test_the_shop_is_read_out_of_the_sentence_that_names_it() -> None:
    assert (
        read_shop_host(
            "We're Northwind Outfitters — the shop is Northwind-Outfitters.myshopify.com."
        )
        == "northwind-outfitters.myshopify.com"
    )


def test_an_answer_naming_two_shops_is_refused() -> None:
    with pytest.raises(AnswerNotUnderstood, match="more than one shop"):
        read_shop_host("either alpha.myshopify.com or beta.myshopify.com")


def test_an_answer_naming_no_myshopify_host_reads_as_none() -> None:
    assert read_shop_host("Northwind Outfitters, over on the high street") is None


# --------------------------------------------------------------------------------------
# The transcript as a whole
# --------------------------------------------------------------------------------------
def test_the_fixture_transcript_reads_cleanly(onboarding_transcript: Any) -> None:
    interview = read_transcript(onboarding_transcript)
    assert interview.completed_at == "2026-01-04T17:35:00Z"
    assert interview.interview_id == "interview-northwind-001"
    assert {answer.question for answer in interview.answers} == {
        "store",
        "max_discount_pct",
        "budget_cap",
        "price_floor",
        "pursue_clusters",
        "standing_commitment",
        "activation",
    }


def test_a_transcript_with_no_completion_instant_is_refused(onboarding_transcript: Any) -> None:
    """Never the wall clock: the same interview must yield the same provenance forever."""
    maimed = copy.deepcopy(onboarding_transcript)
    del maimed["completed_at"]
    with pytest.raises(TranscriptRejected, match="never from the clock"):
        read_transcript(maimed)


def test_a_bare_list_of_turns_takes_its_instant_from_the_last_stamped_turn() -> None:
    interview = read_transcript(
        [
            {"role": "interviewer", "question": "store", "text": "which shop?"},
            {
                "role": "merchant",
                "text": "alpha.myshopify.com",
                "at": "2026-02-02T09:00:00+00:00",
            },
        ]
    )
    assert interview.completed_at == "2026-02-02T09:00:00Z"


def test_an_unanswered_question_is_refused(onboarding_transcript: Any) -> None:
    maimed = copy.deepcopy(onboarding_transcript)
    # Drop the merchant's reply to the discount ceiling.
    maimed["turns"] = [
        turn
        for turn in maimed["turns"]
        if turn.get("text")
        != "Twenty percent off, and that's the ceiling — I don't want anything past it."
    ]
    with pytest.raises(TranscriptRejected, match="without an answer"):
        read_transcript(maimed)


def test_a_question_the_script_does_not_have_is_refused(onboarding_transcript: Any) -> None:
    maimed = copy.deepcopy(onboarding_transcript)
    maimed["turns"].insert(
        0, {"role": "interviewer", "question": "risk_appetite", "text": "how bold are you?"}
    )
    maimed["turns"].insert(1, {"role": "merchant", "text": "very"})
    with pytest.raises(TranscriptRejected, match="not in the onboarding script"):
        read_transcript(maimed)


def test_an_answer_with_no_question_in_front_of_it_is_refused() -> None:
    with pytest.raises(TranscriptRejected, match="no question in front of it"):
        read_transcript(
            {
                "completed_at": "2026-01-01T00:00:00+00:00",
                "turns": [{"role": "merchant", "text": "twenty percent"}],
            }
        )


def test_a_turn_spoken_by_nobody_is_refused() -> None:
    with pytest.raises(TranscriptRejected, match="cannot say who is talking"):
        read_transcript(
            {
                "completed_at": "2026-01-01T00:00:00+00:00",
                "turns": [{"role": "narrator", "text": "and then they agreed"}],
            }
        )


def test_asking_a_single_answer_question_twice_is_refused(onboarding_transcript: Any) -> None:
    """Two discount ceilings is a transcript nobody can resolve; picking one picks for them."""
    maimed = copy.deepcopy(onboarding_transcript)
    maimed["turns"] += [
        {"role": "interviewer", "question": "max_discount_pct", "text": "and again?"},
        {"role": "merchant", "text": "make it 40 percent"},
    ]
    interview = read_transcript(maimed)
    with pytest.raises(TranscriptRejected, match="2 times"):
        interview.one("max_discount_pct")


def test_a_transcript_that_is_not_a_list_of_turns_is_refused() -> None:
    with pytest.raises(TranscriptRejected):
        read_transcript("the merchant said twenty percent")
    with pytest.raises(TranscriptRejected):
        read_transcript({"completed_at": "2026-01-01T00:00:00+00:00", "turns": "some words"})
