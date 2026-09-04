"""The clarifier's model seam, exercised against the project's real LLM package (T-071).

A loop that is only ever driven by a hand-rolled double proves that the double works. The
tests here drive it with ``packages.llm``'s own clients instead:

* :class:`llm.RecordedLLM` built from ``fixtures/recorded/buyer_intent.json`` — the
  reviewed recording T-014 authored for exactly this seam (D21). It replays only on an
  exact ``(system, prompt)`` match, so if this module's system contract or prompt shape
  ever drifts from the fixture, this test goes red instead of the loop quietly falling
  back to its canned wording with a green suite.
* :class:`llm.DeterministicLLM`, which is what ``build_llm("buyer")`` returns under D20's
  default provider — i.e. what the service actually runs with, offline, with no key.

Both are offline (D3). Nothing here opens a socket.
"""

from __future__ import annotations

import pytest

from apps.buyer.svc.src.intent import (
    INTENT_CONTRACT,
    clarify,
    parse_llm_reply,
)

RECORDED_ESPRESSO = "something good for espresso, cheap"
RECORDED_LIGHT_ROAST = "I want a light roast under $20, and I'd rather it be recently roasted."


@pytest.fixture()
def llm_pkg():
    return pytest.importorskip("packages.llm")


def test_the_system_contract_is_byte_for_byte_the_recorded_one(llm_pkg) -> None:
    """The claim in ``clarifier.INTENT_CONTRACT``'s comment, checked rather than asserted.

    ``RecordedLLM`` keys on the ``(system, prompt)`` PAIR. A reworded contract here would
    stop every recorded reply matching and cost nothing visible - the loop would fall back
    to its own wording and every test would stay green.
    """
    assert INTENT_CONTRACT == llm_pkg.load_system_contract("buyer_intent")


def test_a_recorded_reply_reaches_the_buyer_as_the_clarifying_question(llm_pkg) -> None:
    double = llm_pkg.RecordedLLM.from_fixture("buyer_intent", role="buyer")
    outcome = clarify([RECORDED_ESPRESSO], double)
    assert outcome.questions == ("What is the most you would want to spend per 12 oz bag?",)
    assert len(double.calls) == 1


def test_the_recorded_intent_reply_lands_in_the_structured_intent(llm_pkg) -> None:
    double = llm_pkg.RecordedLLM.from_fixture("buyer_intent", role="buyer")
    outcome = clarify([RECORDED_LIGHT_ROAST], double)
    fields = {constraint.field for constraint in outcome.intent.hard_constraints}
    assert "roast_level" in fields
    assert outcome.questions == (), "the recorded reply asks nothing for this utterance"


def test_the_recorded_lt_op_is_dropped_rather_than_widened_into_lte(llm_pkg) -> None:
    """The recording proposes ``price_usd lt 20``. R19 has no ``lt``.

    Coercing it to ``lte`` would admit an item priced at exactly $20.00 that the model
    said to exclude. The buyer's own "under $20" is what supplies the ceiling instead.
    """
    reply = llm_pkg.RecordedLLM.from_fixture("buyer_intent").complete(
        f"BUYER\n{RECORDED_LIGHT_ROAST}", system=INTENT_CONTRACT
    )
    proposal = parse_llm_reply(reply)
    assert all(constraint.op != "lt" for constraint in proposal.constraints)
    assert {constraint.field for constraint in proposal.constraints} == {"roast_level"}
    assert any("lt" in note for note in proposal.dropped), proposal.dropped


def test_the_recorded_min_direction_is_normalised_not_dropped(llm_pkg) -> None:
    """``min`` is a SPELLING of ``minimize``; normalising it changes nothing."""
    reply = llm_pkg.RecordedLLM.from_fixture("buyer_intent").complete(
        f"BUYER\n{RECORDED_ESPRESSO}", system=INTENT_CONTRACT
    )
    proposal = parse_llm_reply(reply)
    assert [(p.field, p.direction) for p in proposal.preferences] == [("price_usd", "minimize")]


def test_an_unrecorded_prompt_degrades_the_loop_instead_of_breaking_it(llm_pkg) -> None:
    """``RecordedLLM`` raises on anything unrecorded, which the loop must survive."""
    double = llm_pkg.RecordedLLM.from_fixture("buyer_intent", role="buyer")
    outcome = clarify(["hmm", "not sure", "dunno", "whatever"], double)
    assert len(outcome.questions) == 3
    assert all(question.strip() for question in outcome.questions)
    assert outcome.intent is not None


def test_the_default_offline_provider_drives_the_loop(llm_pkg) -> None:
    """``build_llm("buyer")`` with no env set is D20's double - what the service runs."""
    client = llm_pkg.build_llm("buyer")
    outcome = clarify(["hmm", "not sure"], client)
    assert outcome.llm_calls >= 1
    assert all("double:" not in question for question in outcome.questions), outcome.questions
    assert outcome.intent is not None


def test_the_buyer_role_is_a_real_role_in_the_llm_config(llm_pkg) -> None:
    """The route module spells the role rather than importing it; keep the two agreeing."""
    from apps.buyer.svc.src.intent.routes import LLM_ROLE

    assert LLM_ROLE in llm_pkg.KNOWN_ROLES
    assert LLM_ROLE == llm_pkg.ROLE_BUYER


# --- the parser on its own, on replies nobody would record --------------------------


@pytest.mark.parametrize(
    "reply",
    ["", "   ", "not json at all", "double:buyer:0a1b2c3d", '{"constraints": [', "[]", None, 42],
    ids=["empty", "blank", "prose", "double-marker", "truncated", "array", "none", "int"],
)
def test_an_unusable_reply_yields_an_empty_proposal_and_never_raises(reply) -> None:
    proposal = parse_llm_reply(reply)
    assert proposal.question is None
    assert proposal.constraints == () and proposal.preferences == ()


def test_a_bare_line_ending_in_a_question_mark_is_read_as_the_question() -> None:
    proposal = parse_llm_reply("  What size do you take?  ")
    assert proposal.question == "What size do you take?"


def test_a_bare_line_without_a_question_mark_is_not(reply="Sounds good, one moment") -> None:
    assert parse_llm_reply(reply).question is None


def test_a_fenced_json_reply_is_unwrapped() -> None:
    fenced = '```json\n{"clarifying_question": "Which colour?"}\n```'
    assert parse_llm_reply(fenced).question == "Which colour?"


def test_a_wildly_long_question_is_truncated_rather_than_shown_whole() -> None:
    proposal = parse_llm_reply('{"clarifying_question": "%s?"}' % ("a" * 5000))
    assert proposal.question is not None
    assert len(proposal.question) <= 240


def test_a_proposed_constraint_that_is_not_an_object_is_dropped_with_a_reason() -> None:
    proposal = parse_llm_reply('{"constraints": ["price under twenty"]}')
    assert proposal.constraints == ()
    assert proposal.dropped
