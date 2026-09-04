"""R1's clarification loop, attacked where a counting invariant actually breaks (T-071).

The frozen acceptance suite drives this loop with five well-behaved scripted dialogues. A
question cap and a "no auction before confirmation" rule are a COUNTING and an ORDERING
invariant, and that class of rule does not fail on well-behaved input — it fails on the
buyer who never answers, who hedges four times, who pastes a hundred lines, who hands the
loop a model that raises on every call. Those are the tests here.
"""

from __future__ import annotations

import inspect

import pytest

from apps.buyer.svc.src.intent import (
    MAX_CLARIFYING_QUESTIONS,
    ClarifyOutcome,
    EmptyDialogue,
    QuestionCapBroken,
    clarify,
)


class ScriptedLLM:
    """A protocol-agnostic double: any method name returns the next scripted reply."""

    def __init__(self, script: list[str] | None = None) -> None:
        self.script = list(script or [])
        self.cursor = 0
        self.calls: list[tuple[tuple, dict]] = []

    def _reply(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.cursor < len(self.script):
            reply = self.script[self.cursor]
            self.cursor += 1
            return reply
        return self.script[-1] if self.script else ""

    def __call__(self, *args, **kwargs):
        return self._reply(*args, **kwargs)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._reply


class AttributeSpy:
    """Records every attribute the loop reaches for, and answers all of them."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        self.touched.append(name)
        return lambda *args, **kwargs: ""


class ExplodingLLM:
    """Every call raises. A model that is down must cost phrasing, not the loop."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt, **kwargs):
        self.calls += 1
        raise RuntimeError("upstream 503")


# --- the cap -----------------------------------------------------------------------


def test_a_buyer_who_hedges_forever_is_asked_exactly_three_questions() -> None:
    outcome = clarify(["hmm", "not sure", "dunno", "whatever", "no idea", "you pick"])
    assert len(outcome.questions) == MAX_CLARIFYING_QUESTIONS
    assert outcome.intent is not None


def test_the_caller_cannot_raise_the_cap() -> None:
    """R1's three is a ceiling, not a default. ``max_questions=99`` must not lift it."""
    outcome = clarify(["hmm", "not sure", "dunno", "whatever", "nope"], max_questions=99)
    assert len(outcome.questions) <= MAX_CLARIFYING_QUESTIONS


def test_the_caller_may_lower_the_cap() -> None:
    outcome = clarify(["hmm", "not sure", "dunno", "whatever"], max_questions=1)
    assert len(outcome.questions) == 1


def test_a_zero_question_cap_still_produces_an_intent() -> None:
    outcome = clarify(["hmm"], max_questions=0)
    assert outcome.questions == ()
    assert outcome.intent.query == "hmm"
    assert outcome.intent.budget_band == "unspecified"


def test_the_outcome_object_refuses_to_carry_a_fourth_question() -> None:
    """The cap is re-checked where the object is built, not only where it is counted."""
    good = clarify(["hmm", "not sure", "dunno", "whatever"])
    with pytest.raises(QuestionCapBroken):
        ClarifyOutcome(intent=good.intent, questions=("a?", "b?", "c?", "d?"))


def test_a_hundred_turns_neither_spins_nor_asks_more_than_three() -> None:
    outcome = clarify(["hmm", *["nope"] * 100])
    assert len(outcome.questions) <= MAX_CLARIFYING_QUESTIONS
    assert outcome.intent is not None


def test_every_question_is_non_empty_and_never_repeated() -> None:
    outcome = clarify(["hmm", "not sure", "dunno", "whatever"])
    assert all(question.strip() for question in outcome.questions)
    assert len(set(outcome.questions)) == len(outcome.questions)


# --- the buyer who walks away ------------------------------------------------------


def test_a_buyer_who_never_answers_still_gets_an_intent() -> None:
    outcome = clarify(["trail running shoes, size 10, waterproof"])
    assert len(outcome.questions) == 1
    assert outcome.answers == ()
    assert outcome.intent.budget_band == "unspecified"
    assert "budget" in outcome.unresolved


def test_an_unanswered_gap_is_named_rather_than_invented() -> None:
    outcome = clarify(["a wool scarf"])
    assert outcome.unresolved, "a gap the buyer never closed must be reported"
    assert outcome.intent.budget_band == "unspecified"


# --- degenerate input --------------------------------------------------------------


def test_an_empty_dialogue_is_refused() -> None:
    with pytest.raises(EmptyDialogue):
        clarify([])


def test_a_dialogue_of_whitespace_is_refused() -> None:
    with pytest.raises(EmptyDialogue):
        clarify(["", "   ", "\n\t"])


def test_a_bare_string_is_one_utterance_and_not_a_pile_of_characters() -> None:
    """``clarify("I want coffee")`` must not iterate the string into 13 turns."""
    outcome = clarify("I want a light roast under $20")
    assert outcome.transcript == ("I want a light roast under $20",)
    assert outcome.intent.query == "I want a light roast under $20"


def test_blank_turns_between_real_ones_are_dropped_not_counted_as_answers() -> None:
    outcome = clarify(["hmm", "   ", "a wool scarf"])
    assert "" not in outcome.transcript
    assert all(turn.strip() for turn in outcome.transcript)


# --- the model seam ----------------------------------------------------------------


def test_a_model_that_raises_on_every_call_does_not_take_the_loop_down() -> None:
    llm = ExplodingLLM()
    outcome = clarify(["hmm", "not sure", "dunno", "whatever"], llm)
    assert llm.calls >= 1
    assert len(outcome.questions) == MAX_CLARIFYING_QUESTIONS
    assert all(question.strip() for question in outcome.questions)


def test_a_model_that_returns_blank_never_produces_a_blank_question() -> None:
    outcome = clarify(["hmm", "not sure"], ScriptedLLM(["", "   ", "\n"]))
    assert all(question.strip() for question in outcome.questions)


def test_the_offline_doubles_marker_is_never_shown_to_the_buyer_as_a_question() -> None:
    """``double:buyer:0a1b...`` is a marker, not a question. It must not reach a human."""
    outcome = clarify(["hmm", "not sure"], ScriptedLLM(["double:buyer:0a1b2c3d4e5f6071"]))
    assert all("double:" not in question for question in outcome.questions)


def test_a_model_proposing_a_question_that_was_already_asked_gets_our_wording() -> None:
    repeated = '{"clarifying_question": "What are you shopping for?"}'
    outcome = clarify(["hmm", "not sure", "dunno", "whatever"], ScriptedLLM([repeated]))
    assert len(set(outcome.questions)) == len(outcome.questions)


def test_the_model_is_consulted_exactly_once_per_round() -> None:
    """One call per round, never a retry. A second call would eat the next scripted reply."""
    llm = ScriptedLLM(['{"clarifying_question": null}'])
    outcome = clarify(["hmm", "not sure", "dunno", "whatever"], llm)
    assert outcome.llm_calls == len(llm.calls)
    assert outcome.llm_calls == len(outcome.questions) + 1


def test_the_loop_touches_only_the_completion_method_on_the_client() -> None:
    """It must not grope an injected client for anything else - least of all an auction."""
    spy = AttributeSpy()
    clarify(["hmm", "not sure"], spy)
    assert set(spy.touched) == {"complete"}, spy.touched


# --- what clarification is NOT -----------------------------------------------------


def test_clarify_cannot_be_handed_an_auction_client_at_all() -> None:
    """R1's ordering invariant, checked at the signature rather than in a branch."""
    parameters = set(inspect.signature(clarify).parameters)
    forbidden = {"auction", "auction_client", "exchange", "exchange_client", "client"}
    assert not parameters & forbidden, parameters


def test_an_outcome_is_never_confirmed_however_it_is_constructed() -> None:
    outcome = clarify(["a wool scarf"])
    assert outcome.confirmed is False
    forced = ClarifyOutcome(intent=outcome.intent, confirmed=True)  # type: ignore[call-arg]
    assert forced.confirmed is False, "clarifying is not confirming, ever"


def test_the_outcome_reads_the_same_by_key_and_by_attribute() -> None:
    outcome = clarify(["a wool scarf"])
    assert outcome["questions"] == outcome.questions
    assert outcome["intent"] is outcome.intent
    with pytest.raises(KeyError):
        outcome["auction_id"]


# --- the intent itself -------------------------------------------------------------


def test_the_query_is_the_buyers_own_words_and_never_a_paraphrase() -> None:
    opener = "something good for espresso, cheap"
    outcome = clarify([opener, "no more than $25 a bag"])
    assert opener in outcome.intent.query
    assert "no more than" not in outcome.intent.query, (
        "an answer to the budget question is not part of what the buyer is shopping for"
    )


def test_a_negated_ceiling_is_not_also_read_as_a_floor() -> None:
    """"no more than $25" once produced `price_usd lte 25` AND `price_usd gte 25`."""
    outcome = clarify(["a wool scarf", "no more than $25"])
    prices = {(c.op, c.value) for c in outcome.intent.hard_constraints if c.field == "price_usd"}
    assert prices == {("lte", 25.0)}, prices


def test_a_price_range_becomes_both_bounds() -> None:
    outcome = clarify(["a daypack for commuting", "between $60 and $120"])
    prices = {(c.op, c.value) for c in outcome.intent.hard_constraints if c.field == "price_usd"}
    assert prices == {("gte", 60.0), ("lte", 120.0)}
    assert outcome.intent.budget_band == "100-250"


def test_a_bare_number_is_money_only_while_answering_the_budget_question() -> None:
    answered = clarify(["a wool scarf", "40"])
    assert answered.intent.budget_band == "0-50"

    opener = clarify(["trail running shoes, size 10, waterproof"])
    sizes = {c.value for c in opener.intent.hard_constraints if c.field == "size"}
    assert sizes == {10.0}
    assert opener.intent.budget_band == "unspecified", (
        "a size must never be read as a price ceiling"
    )


def test_the_same_dialogue_produces_the_same_cluster_id_and_a_fresh_intent_id() -> None:
    turns = ["I want a light roast under $20"]
    first, second = clarify(turns), clarify(turns)
    assert first.intent.cluster_id == second.intent.cluster_id
    assert first.intent.intent_id != second.intent.intent_id
