"""The shopper answers the clarifying question and the answer is thrown away (Defect A).

Reproduced by the owner on the live app and reproduced here through the SERVED route, with
the owner's exact two utterances. Everything in this file is written from the shopper's
side of the wire: what they typed, what came back, and whether the sentence the page then
renders is TRUE.

Three distinct failures live in the one screenshot, and they are separated here because
they have three different causes and three different fixes:

1. **The answer produces nothing.** Neither extractor mints a constraint from "cherry
   wood" / "at least 48 inches wide" — the closed lexicon does not carry those words, and
   the live model's reply is discarded by ``parse_llm_reply`` because ``INTENT_CONTRACT``
   never asks for JSON.
2. **The gap is asked twice.** ``next_gap()`` recomputes from state, and nothing records
   that a gap was already put to the shopper and answered, so an honest answer that yields
   no constraint costs a second question.
3. **The page then lies.** ``unresolved`` carries ``constraints``, and
   ``IntentConfirm.tsx`` renders "We never got an answer about: constraints. We have not
   guessed." — a sentence that is false the moment a turn was absorbed against that gap.

The R19 wall is why (1) is not fixed by simply widening what may become a hard constraint.
Measured on the served exchange (``POST http://localhost:8083/auctions``, three repeats,
query "a sofa"): a bare intent shortlists 3 slots; adding ``material eq cherry-wood``,
``color eq navy`` or ``width_in gte 48`` shortlists **0**, with ``relaxed_constraints``
empty; adding ``color eq terra`` — a value a shortlisted product actually carries —
shortlists 1 and publishes the constraint as relaxed. A preference never costs a slot: the
same query with ``material``, ``width_in`` or an invented ``cherry_wood`` preference
shortlists 3 every time. So a constraint this service cannot vouch for belongs in the
scoring half, and saying so out loud is the difference between a thin intent and a lying
one.
"""

from __future__ import annotations

import importlib
import json
import re

import pytest
from fastapi.testclient import TestClient

from apps.buyer.svc.src.intent import reset_confirmations
from apps.buyer.svc.src.intent.clarifier import CANNED_QUESTIONS, INTENT_CONTRACT, clarify
from apps.buyer.svc.src.intent.extraction import (
    GAP_BUDGET,
    GAP_CONSTRAINTS,
    GAP_USE_CASE,
    parse_llm_reply,
)
from apps.buyer.svc.src.intent.routes import set_buyer_llm

CLARIFY = "/buyer/intent/clarify"

#: The owner's two utterances, byte for byte, as they were typed into the live app.
OPENER = "I want a cherry wood table under $200"
ANSWER = "It must be cherry wood, and at least 48 inches wide"


@pytest.fixture()
def client():
    """The served app, with no model — the offline floor D20 ships by default."""
    reset_confirmations()
    set_buyer_llm(None)
    main = importlib.import_module("buyer_svc.main")
    app = main.create_app()
    app.state.auction_client = object()
    with TestClient(app) as test_client:
        yield test_client
    reset_confirmations()
    set_buyer_llm(None)


class ScriptedLLM:
    """A model that replies with whatever it was handed, once per round.

    Not a stub of the parser's input — a stub of the MODEL's output, which is the seam the
    shipped fixture never covers: every recorded reply in
    ``packages/llm/fixtures/recorded/buyer_intent.json`` is hand-authored JSON, so the
    suite proves the parser reads JSON and nothing proves a model ever writes it.
    """

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.systems: list[str] = []
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, system: str = "") -> str:
        self.systems.append(system)
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


# --------------------------------------------------------------------------------------
# 1. the answer must not vanish
# --------------------------------------------------------------------------------------


def test_the_answer_to_the_constraints_question_reaches_the_intent(client) -> None:
    """The shopper says "it must be cherry wood" and the intent says so back.

    This is the whole of Defect A in one assertion. At HEAD the response carried
    ``hard_constraints: [price_usd lte 200.0]`` and ``preferences: []`` — the second
    utterance contributed nothing at all, in either half of R19's split.

    The assertion is on the whole confirmation payload rather than on one column of it,
    because R19's ``Preference`` is ``field``/``direction``/``weight`` and has nowhere to
    put a value: "cherry wood" can be a hard constraint's value or a softened reading's
    value, and both are things the shopper is shown. What it may never be is absent, which
    is what it was.
    """
    response = client.post(CLARIFY, json={"turns": [OPENER, ANSWER]})
    assert response.status_code == 200, response.text
    body = response.json()

    shown = json.dumps(body).lower()
    assert "cherry" in shown, (
        "the shopper answered 'It must be cherry wood' and nothing in the confirmation "
        f"payload carries the words back: {json.dumps(body)}"
    )

    # And it is carried WITH its field, not as loose prose in a transcript somewhere: a
    # value the page cannot attach to an attribute is not a reading, it is an echo.
    carriers = [row for row in body["softened"] if "cherry" in json.dumps(row).lower()]
    carriers += [
        row for row in body["intent"]["hard_constraints"] if "cherry" in json.dumps(row).lower()
    ]
    assert carriers, (
        "'cherry wood' appears in the payload but is attached to no attribute; "
        f"softened={body['softened']!r} hard={body['intent']['hard_constraints']!r}"
    )
    assert any(row.get("field") == "material" for row in carriers), (
        f"'cherry wood' is recorded against no `material` field: {carriers!r}"
    )


def test_the_material_in_the_opening_query_is_read_from_the_opening_query(client) -> None:
    """ "a cherry wood table" says cherry wood before any question is asked.

    The owner noticed this too: the query itself names the material and nothing was
    extracted from it, so the loop asks about must-haves that the shopper had already
    given it.
    """
    response = client.post(CLARIFY, json={"turns": [OPENER]})
    assert response.status_code == 200, response.text
    body = response.json()
    material = [row for row in body["softened"] if row.get("field") == "material"]
    material += [
        row for row in body["intent"]["hard_constraints"] if row.get("field") == "material"
    ]
    assert material, (
        f"'{OPENER}' names a material and nothing was read from it: "
        f"softened={body['softened']!r} hard={body['intent']['hard_constraints']!r}"
    )
    assert "cherry" in json.dumps(material).lower(), (
        f"a material was read from the opener but it is not the one stated: {material!r}"
    )


# --------------------------------------------------------------------------------------
# 2. a gap that has been answered is not asked again
# --------------------------------------------------------------------------------------


def test_a_gap_the_shopper_answered_is_not_put_to_them_a_second_time(client) -> None:
    """One question about must-haves, not two.

    At HEAD this returned both the first and the second entry of
    ``CANNED_QUESTIONS[GAP_CONSTRAINTS]`` — the shopper answered and was asked the same
    thing again in different words, which is the part of the screenshot that reads as the
    app not listening.
    """
    body = client.post(CLARIFY, json={"turns": [OPENER, ANSWER]}).json()
    constraint_questions = [
        question for question in body["questions"] if question in CANNED_QUESTIONS[GAP_CONSTRAINTS]
    ]
    assert len(constraint_questions) <= 1, (
        "the must-haves gap was put to the shopper twice after they answered it: "
        f"{constraint_questions!r}"
    )


# --------------------------------------------------------------------------------------
# 3. the sentence the page renders must be TRUE
# --------------------------------------------------------------------------------------


def test_the_response_can_tell_never_answered_apart_from_answered_unusably(client) -> None:
    """The page cannot render a true sentence out of a payload that does not know.

    ``unresolved`` keeps its meaning — gaps the INTENT is still missing — because a shopper
    who answers the budget question with "no idea honestly" has answered and the budget is
    still unresolved. Both are true and they are different facts. So the distinction the
    page needs is not a narrower ``unresolved``; it is a second field that says, per gap,
    whether anybody answered and what came of it.

    Without this, "We never got an answer about: constraints. We have not guessed." is the
    only sentence available, and it renders over an answer the shopper actually gave.
    """
    body = client.post(CLARIFY, json={"turns": [OPENER, ANSWER]}).json()
    answered = {row["gap"] for row in body["understood"]}

    assert GAP_CONSTRAINTS in answered, (
        "the shopper answered the must-haves question and no record of that answer reached "
        f"the page: understood={body['understood']!r}"
    )
    record = next(row for row in body["understood"] if row["gap"] == GAP_CONSTRAINTS)
    assert record["answer"] == ANSWER, (
        f"the record does not carry the shopper's own words: {record!r}"
    )
    assert record["question"], f"the record does not say what was asked: {record!r}"

    # The constraints gap is legitimately STILL OPEN here — a softened reading narrows
    # nothing, so the search really is unfiltered — and that is the case the page has to
    # get right. Both facts have to be reachable from this payload: the gap is open, AND
    # the shopper answered it and here is what we did with what they said.
    assert GAP_CONSTRAINTS in body["unresolved"], (
        "sanity: a softened reading is not a filter, so the constraints gap should still be "
        f"open; unresolved={body['unresolved']!r}"
    )
    assert record["used"], (
        "the answer produced a softened material reading and the record claims nothing came "
        f"of it: {record!r}"
    )


def test_an_answer_that_could_not_be_used_is_reported_as_used_or_not_at_all(client) -> None:
    """Not answering and answering-unusably are different facts and need different words.

    The honest report has room for both, so this asserts the response carries a place to
    say "you told us, and here is what we could not do with it" — and that it names the
    gap the shopper actually answered. Without this field the only way to be truthful about
    an unusable answer is to say nothing, which loses the shopper's words entirely.
    """
    body = client.post(CLARIFY, json={"turns": [OPENER, ANSWER]}).json()
    assert "understood" in body, (
        "the /clarify response has no way to distinguish 'never answered' from 'answered "
        f"and we could not use all of it'; keys={sorted(body)!r}"
    )
    understood = body["understood"]
    assert isinstance(understood, list), f"understood should be a list, got {understood!r}"
    answered = [row for row in understood if row.get("gap") == GAP_CONSTRAINTS]
    assert answered, f"nothing in `understood` records the answered gap: {understood!r}"
    assert ANSWER in json.dumps(answered), (
        f"`understood` does not carry the shopper's own words back: {answered!r}"
    )


def test_a_gap_the_shopper_never_answered_is_still_reported_as_unanswered(client) -> None:
    """The control, and the one that stops this fix from becoming a whitewash.

    A shopper who walks away mid-loop genuinely never answered, and the page must still say
    so. Making ``unresolved`` truthful must not make it empty.
    """
    body = client.post(CLARIFY, json={"turns": ["I need a gift for someone"]}).json()
    assert body["unresolved"], (
        "a shopper who answered no question at all should leave gaps unresolved; "
        f"got {body['unresolved']!r} for a single vague opener"
    )


# --------------------------------------------------------------------------------------
# 4. R19: what the answer may become, given a corpus that cannot decide it
# --------------------------------------------------------------------------------------


def test_an_unvouched_field_becomes_a_preference_and_never_an_eligibility_filter(
    client,
) -> None:
    """A hard constraint is a filter, and a filter nothing can satisfy is an empty page.

    Measured on the served exchange (see this module's docstring): ``material eq
    cherry-wood`` shortlists 0 of 3 slots and is not even relaxed. ``width_in gte 48``
    likewise. A preference on either field shortlists 3 of 3. So whatever this service
    learns about "cherry wood" from a model, it must not turn into an eligibility filter on
    a field the shopper's own literal words did not already establish.
    """
    body = client.post(CLARIFY, json={"turns": [OPENER, ANSWER]}).json()
    hard_fields = {constraint["field"] for constraint in body["intent"]["hard_constraints"]}
    assert hard_fields <= {"price_usd"}, (
        "a hard constraint on a field this service cannot vouch for empties the shortlist "
        f"(measured: 0 of 3 slots); the intent carries {sorted(hard_fields)!r}"
    )


def test_a_model_proposed_filter_on_a_new_field_is_softened_rather_than_applied() -> None:
    """The model is allowed to contribute. It is not allowed to empty the page.

    Driven with a scripted model reply in exactly the shape the reworded contract asks for,
    so this fails for the right reason: not "the model said nothing", but "the model said
    something and this service applied it as a filter".
    """
    reply = json.dumps(
        {
            "hard_constraints": [
                {"field": "material", "op": "eq", "value": "cherry wood"},
                {"field": "width_in", "op": "gte", "value": 48},
            ],
            "preferences": [],
            "clarifying_question": None,
        }
    )
    outcome = clarify([OPENER, ANSWER], ScriptedLLM(reply, reply, reply))
    hard_fields = {constraint.field for constraint in outcome.intent.hard_constraints}
    soft_fields = {preference.field for preference in outcome.intent.preferences}
    assert hard_fields <= {"price_usd"}, (
        f"the model's proposal became an eligibility filter: {sorted(hard_fields)!r}"
    )
    assert {"material", "width_in"} <= soft_fields, (
        "the model read the shopper correctly and the reading was dropped instead of "
        f"scored: preferences={sorted(soft_fields)!r}"
    )


# --------------------------------------------------------------------------------------
# 5. the model seam: the contract must ask for what the parser can read
# --------------------------------------------------------------------------------------


def test_the_contract_asks_the_model_for_the_shape_the_parser_requires() -> None:
    """``parse_llm_reply`` needs a JSON object; the contract never said so.

    Measured against the live Anthropic model on this stack: with the HEAD contract it
    replied with a Markdown table reading ``| material | = | cherry wood |`` and ``| width
    | >= | 48 in |`` — correct on both counts — and ``parse_llm_reply`` returned an empty
    proposal, because ``_load_json_object`` finds no ``{`` and the bare-question fallback
    needs a single line ending in ``?``.

    This asserts on the contract's TEXT rather than on a model's behaviour, because a test
    that calls the real model is not a test the offline suite can run. What the real model
    does with the reworded contract is verified separately, by hand, against the live key.
    """
    lowered = INTENT_CONTRACT.lower()
    assert "json" in lowered, (
        "INTENT_CONTRACT does not tell the model to reply with JSON, and `parse_llm_reply` "
        f"reads nothing else:\n{INTENT_CONTRACT}"
    )
    for key in ("hard_constraints", "preferences", "clarifying_question"):
        assert key in INTENT_CONTRACT, (
            f"INTENT_CONTRACT never names the key {key!r} that `parse_llm_reply` looks for:"
            f"\n{INTENT_CONTRACT}"
        )


def test_a_markdown_table_reply_is_reported_rather_than_silently_discarded() -> None:
    """The exact reply the live model gave, and it must not vanish without trace.

    Captured verbatim from one ``AnthropicLLM.complete()`` call with the HEAD contract.
    Whatever the reworded contract achieves, a model that answers in prose one day must
    leave evidence rather than an empty proposal nobody can see.
    """
    markdown = (
        "Here is my reading of the request:\n\n"
        "| field | op | value |\n"
        "|---|---|---|\n"
        "| material | = | cherry wood |\n"
        "| price | < | $200 |\n"
        "| width | >= | 48 in |\n"
    )
    proposal = parse_llm_reply(markdown)
    assert proposal.empty, "sanity: the parser still cannot read a Markdown table"
    assert proposal.dropped, (
        "a whole model reply was discarded and `dropped` records nothing, so no test, no "
        "log and no response field can ever notice it happening"
    )


def test_what_the_model_lost_is_visible_on_the_outcome() -> None:
    """``IntentDraft.dropped`` is write-only at HEAD: extended, and read by nobody.

    ``ClarifyOutcome`` has no such field and ``ClarifyResponse`` returns four keys, so a
    constraint refused for an unexpressible op is invisible to the shopper, to the
    response, and to every test. That is the same blindness that hid Defect A, one layer
    down, and it gets louder the moment the model starts contributing.
    """
    reply = json.dumps(
        {
            "hard_constraints": [{"field": "price_usd", "op": "lt", "value": 20}],
            "preferences": [],
            "clarifying_question": None,
        }
    )
    outcome = clarify([OPENER], ScriptedLLM(reply, reply, reply))
    assert hasattr(outcome, "dropped"), (
        "ClarifyOutcome carries no `dropped`, so a refused model constraint is unobservable "
        f"everywhere; fields={sorted(outcome.to_dict())!r}"
    )
    assert outcome.dropped, (
        "the model proposed `price_usd lt 20`, R19 cannot express `lt`, and the refusal was "
        "recorded nowhere a caller can reach"
    )


# --------------------------------------------------------------------------------------
# 6. a measurement answered to the budget question is not a budget
# --------------------------------------------------------------------------------------


def test_a_width_in_inches_is_not_read_as_a_price(client) -> None:
    """ "at least 48 inches wide" answered to "what's your budget?" gave `price_usd lte 48`.

    Found while auctioning the fixed intent, and present at HEAD too: when the opener
    already names the product, the FIRST gap is budget, so the owner's second utterance is
    absorbed with ``budget_answer=True`` and the bare-number rule reads the 48. The shopper
    stated a width and was handed a $48 budget.

    The bare-number rule itself is right and stays — a lone "40" answering the budget
    question really is dollars. It simply had no way to see that this 48 already carried a
    unit. ``_SIZE_SPAN_RE`` established both the problem and the shape of the fix for "size
    8 to 10"; this is the same thing one unit out.
    """
    body = client.post(
        CLARIFY,
        json={"turns": ["a modular table", "It must be cherry wood, and at least 48 inches wide"]},
    ).json()
    prices = [
        constraint
        for constraint in body["intent"]["hard_constraints"]
        if constraint["field"] == "price_usd"
    ]
    assert prices == [], (
        "a width in inches was read as a budget the shopper never stated: "
        f"{prices!r} from 'at least 48 inches wide'"
    )


@pytest.mark.parametrize(
    "answer",
    [
        "at least 48 inches wide",
        "about 30 cm tall",
        "under 5 lbs",
        "no heavier than 2 kg",
        "at least 8 hours of battery",
    ],
)
def test_a_measurement_is_never_a_budget(answer: str) -> None:
    """The unit table, driven one entry at a time so a gap in it fails by name."""
    outcome = clarify(["a modular table", answer])
    prices = [
        constraint
        for constraint in outcome.intent.hard_constraints
        if constraint.field == "price_usd"
    ]
    assert prices == [], f"{answer!r} was read as money: {prices!r}"


def test_a_bare_number_answering_the_budget_question_is_still_money() -> None:
    """The control. Blanking measurements must not blank budgets.

    ``test_a_bare_number_is_money_only_while_answering_the_budget_question`` pins this from
    the other side; it is repeated here because it is the exact thing the fix above could
    plausibly have broken.
    """
    outcome = clarify(["a modular table", "40"])
    prices = {
        (constraint.op, constraint.value)
        for constraint in outcome.intent.hard_constraints
        if constraint.field == "price_usd"
    }
    assert prices == {("lte", 40.0)}, f"a bare budget answer stopped being money: {prices!r}"
    assert outcome.intent.budget_band == "0-50", outcome.intent.budget_band


# --------------------------------------------------------------------------------------
# 7. the vouching is on the whole term, not on the field and not on the pair
# --------------------------------------------------------------------------------------


def test_a_model_op_the_lexicon_cannot_mint_is_not_vouched_by_a_known_value() -> None:
    """`material contains "merino wool"` is not `material eq merino-wool`.

    Caught against the LIVE model on the demo's own headline utterance, with the vouching
    checking field and value but not op: Sonnet answered
    ``{"field": "material", "op": "contains", "value": "merino wool"}``, the lexicon knows
    the pair `material`/`merino-wool`, so it was vouched and applied — a SECOND eligibility
    filter beside the lexicon's own `material eq merino-wool`, on a `contains` comparison
    this lexicon can never mint and this service cannot check. `HardConstraint.key` is
    `(field, op)`, so the two did not collide and nothing complained.
    """
    reply = json.dumps(
        {
            "hard_constraints": [{"field": "material", "op": "contains", "value": "merino wool"}],
            "preferences": [],
            "clarifying_question": None,
        }
    )
    outcome = clarify(
        ["I want a warm merino wool beanie for winter, under $100"],
        ScriptedLLM(reply, reply, reply),
    )
    ops = {
        (constraint.field, constraint.op)
        for constraint in outcome.intent.hard_constraints
        if constraint.field == "material"
    }
    assert ops == {("material", "eq")}, (
        "a model op the lexicon cannot mint became an eligibility filter because the VALUE "
        f"happened to be one the lexicon knows: {ops!r}"
    )
    assert any(reading.op == "contains" for reading in outcome.softened), (
        f"the refused op was not kept as a softened reading either: {outcome.softened!r}"
    )


def test_the_lexicons_own_terms_are_still_vouched() -> None:
    """The control: narrowing the vouching must not stop the lexicon vouching for itself.

    Every term the table can mint has to pass, or a shopper's own literal words would be
    softened into preferences and R19's filtering half would quietly empty out.
    """
    from apps.buyer.svc.src.intent.extraction import _CONSTRAINTS, vouched_as_filter
    from apps.buyer.svc.src.intent.models import HardConstraint

    for phrase, (field, op, value) in _CONSTRAINTS.items():
        assert vouched_as_filter(HardConstraint(field=field, op=op, value=value)), (
            f"the lexicon's own term for {phrase!r} is not vouched: {field} {op} {value!r}"
        )

    # `price_usd` and `size` were asserted VOUCHED here, through an `_OPEN_VALUE_TERMS`
    # exemption that passed field+op alone. That was wrong in both halves. `vouched_as_filter`
    # is asked only about a MODEL's proposal — the buyer's own ceiling is minted by
    # `read_budget` and added with `authoritative=True`, and never reaches this function — and
    # a model-named price bound is the one thing this corpus cannot survive: measured on the
    # served exchange, query "a sofa", `price_usd lte 5000` (above every price in the corpus)
    # returns 0 slots / 0 shops / 0 products considered, exactly as `price_usd lte 200` and
    # `size eq queen` do, because the graph holds no `price_usd` reading for any product and
    # the pushdown filter therefore matches nothing. "Its value is a number so there is
    # nothing to check" is a reason to trust a model less on a field, not more.
    for op in ("lte", "gte"):
        assert not vouched_as_filter(HardConstraint(field="price_usd", op=op, value=20.0)), (
            "a model-proposed price bound is vouched as an eligibility filter, which empties "
            "the shortlist on this corpus whatever the number is"
        )
    assert not vouched_as_filter(HardConstraint(field="size", op="eq", value=10.0)), (
        "a model-proposed size is vouched as an eligibility filter"
    )


# --------------------------------------------------------------------------------------
# 8. nothing this fix adds may empty a shortlist that worked
# --------------------------------------------------------------------------------------


def test_no_category_is_invented_for_a_word_the_corpus_does_not_use(client) -> None:
    """`intent.category` is not inert — the exchange's retrieval filters on it.

    Measured against the served exchange with the query "a modular table", varying only
    this field: absent -> 25 products considered and 3 slots; ``"furniture"`` -> 0 and 0;
    ``"coffee"`` -> 1 and 0. So teaching the lexicon that "table" means furniture, which is
    the obvious way to make "a cherry wood table" name a product, would have taken a working
    three-slot query to an empty page.

    This pins the restraint rather than the mechanism: the loop must find the opener
    specific WITHOUT minting a category the corpus has never heard of.
    """
    body = client.post(CLARIFY, json={"turns": ["a cherry wood table"]}).json()
    assert body["intent"].get("category") in (None, ""), (
        "a category was invented for a word the corpus's taxonomy does not carry, which "
        f"empties retrieval: {body['intent'].get('category')!r}"
    )


def test_a_cherry_wood_table_names_a_product_without_naming_a_category(client) -> None:
    """The other half: the opener must still be specific, or the loop asks what they want.

    Before the lexicon knew "cherry wood", the opener carried three loose content words and
    was specific. Teaching it the phrase consumed two of them and dropped it to one, so the
    first question became "What are you shopping for?" — asked of a shopper who had just
    said. A softened phrase keeps contributing its words, so specificity is unchanged.
    """
    body = client.post(CLARIFY, json={"turns": ["a cherry wood table"]}).json()
    first = body["questions"][0] if body["questions"] else ""
    assert first not in CANNED_QUESTIONS[GAP_USE_CASE], (
        f"'a cherry wood table' was not recognised as naming a product: asked {first!r}"
    )


# --------------------------------------------------------------------------------------
# 9. the three regressions this repair introduced, each driven from the shopper's side
# --------------------------------------------------------------------------------------


#: Every string this module has to read the same way, both directions, in one table.
#:
#: The left column is what a shopper types; the right is the money the reader must find in
#: it, as ``(ceiling, floor, band)``, with ``None`` throughout meaning "this utterance says
#: nothing about money". Both directions are here on purpose: a unit table that blanks too
#: little re-reads a width as a price, and one that blanks too much eats a stated budget,
#: and only a table with both kinds of row in it can fail for the right reason.
MONEY_CASES: tuple[tuple[str, tuple[float | None, float | None, str | None]], ...] = (
    # -- the regression: the English preposition "in" is not the unit "inches" ----------
    ("a coffee table under $400 in oak", (400.0, None, "250-500")),
    ("a rug under $300 in wool", (300.0, None, "250-500")),
    ("a lamp under $80 in brass", (80.0, None, "50-100")),
    ("something under 400 in oak", (400.0, None, "250-500")),
    # ``in`` followed by a NUMBER rather than a noun. The first repair read these as inches
    # too — a digit is not a letter — and blanked the amount, leaving `_CEILING_RE` to grab
    # the number on the OTHER side of the word: "under 400 in 2 weeks" became a $2 ceiling,
    # which is worse than losing the budget because it ships a filter nobody stated.
    ("a coffee table under 400 in 2 weeks", (400.0, None, "250-500")),
    ("a linen duvet cover under 400 in 3 days", (400.0, None, "250-500")),
    ("budget of 500 in 2 payments", (500.0, None, "500-1000")),
    ("400 in 2 weeks", (400.0, None, "250-500")),
    # The budget stated AFTER the measurement, which `_CEILING_RE.search` reaches second.
    ("a dresser, no more than 48 in wide, under $400", (400.0, None, "250-500")),
    ("under $400, no more than 48 in wide", (400.0, None, "250-500")),
    # -- the reading the unit table exists for, which must survive ----------------------
    ("at least 48 inches wide", (None, None, None)),
    ("48 in wide", (None, None, None)),
    ("48in wide", (None, None, None)),
    ("48 in", (None, None, None)),
    # A bare "in" whose following word is a noun rather than a dimension. The first repair
    # required the unit reading to be earned by a word from a closed list, and every one of
    # these falls outside any plausible list — each came back as a $48/$60 budget.
    ("at least 48 in of clearance", (None, None, None)),
    ("48 in from the wall", (None, None, None)),
    ("60 in tv stand", (None, None, None)),
    ("48 in or wider", (None, None, None)),
    ("a 48 in oak table", (None, None, None)),
    # A measurement standing behind a MONEY CUE. The second repair decided a bare "in" by
    # asking whether a money pattern already claimed the number, which is backwards for
    # every ceiling, hedge and range cue in the module: each of these was read as a budget,
    # and on the served route "no more than 48 in wide" answered after "a bookshelf under
    # $400" replaced the shopper's own $400 with $48.
    ("no more than 48 in wide", (None, None, None)),
    ("at most 60 in tall", (None, None, None)),
    ("up to 72 in wide", (None, None, None)),
    ("maximum 48 in wide", (None, None, None)),
    ("max 48 in wide", (None, None, None)),
    ("cheaper than 48 in wide", (None, None, None)),
    ("less than 36 in deep", (None, None, None)),
    ("within 30 in of the wall", (None, None, None)),
    ("no more than 48in wide", (None, None, None)),
    ("about 48 in wide", (None, None, None)),
    ("around 30 in tall", (None, None, None)),
    ("roughly 60 in wide", (None, None, None)),
    ("up to about 48 in wide", (None, None, None)),
    ("between 40 and 60 in wide", (None, None, None)),
    ("at least 48 in wide", (None, None, None)),
    # A dimension word standing in front of ANOTHER word is a colour, finish or weave —
    # every one of these lost the stated budget when the word list had no terminal test.
    ("a rug under 300 in deep blue", (300.0, None, "250-500")),
    ("a lamp under 400 in high gloss", (400.0, None, "250-500")),
    ("a rug under 400 in thick wool", (400.0, None, "250-500")),
    ("a table under 500 in long grain oak", (500.0, None, "500-1000")),
    ("curtains under 200 in wide stripe", (200.0, None, "100-250")),
    ("a bowl under 90 in square profile", (90.0, None, "50-100")),
    ("a mirror under 150 in tall format", (150.0, None, "100-250")),
    ("a sofa under 900 in deep green velvet", (900.0, None, "500-1000")),
    ("under 400 in by friday", (400.0, None, "250-500")),
    ("under 400 in x oak", (400.0, None, "250-500")),
    ("under 400 in, oak", (400.0, None, "250-500")),
    # …and a measurement whose dimension word is NOT in any list, behind a ceiling cue. The
    # money test then claimed the number and a width became a budget: measured on the served
    # route, "no more than 48 in overall" answered after "a bookshelf under $400" replaced
    # the shopper's $400 with $48. `in` cannot be the preposition in front of any of these.
    ("no more than 48 in overall", (None, None, None)),
    ("at most 60 in on the diagonal", (None, None, None)),
    ("up to 72 in front to back", (None, None, None)),
    ("no more than 36 in from the floor", (None, None, None)),
    ("at most 30 in per side", (None, None, None)),
    ("under 48 in when folded", (None, None, None)),
    ("maximum 60 in unassembled", (None, None, None)),
    ("no more than 48 in and 24 in deep", (None, None, None)),
    ("a poster 24 in by 36 in under $50", (50.0, None, "50-100")),
    ("5 ft 10 in tall", (None, None, None)),
    # The preposition `in` takes plenty of words that look like measurement grammar. A first
    # draft of `_AFTER_INCHES_ONLY` listed them as "impossible after `in`" and ate the budget
    # in every one of these; `total` sat in the dimension list and did the same.
    ("a sofa under 400 in total", (400.0, None, "250-500")),
    ("a rug under 300 in about two weeks", (300.0, None, "250-500")),
    ("under 400 in around a month", (400.0, None, "250-500")),
    ("a lamp under 250 in under an hour", (250.0, None, "250-500")),
    ("a desk under 500 in over a year", (500.0, None, "500-1000")),
    ("a shelf under 200 in between the studs", (200.0, None, "100-250")),
    ("a table under 600 in front of the window", (600.0, None, "500-1000")),
    ("under 400 in by 2 weeks", (400.0, None, "250-500")),
    ("a chair under 150 in out of stock colours", (150.0, None, "100-250")),
    ("a bed under 900 in up to three finishes", (900.0, None, "500-1000")),
    ("a rug under 300 in through the arch", (300.0, None, "250-500")),
    ("a shelf under 700 in back of the door", (700.0, None, "500-1000")),
    # …and the measurements those words must not take with them.
    ("about 48 in in total", (None, None, None)),
    ("up to 72 in front to back", (None, None, None)),
    ("48 in x 24 in", (None, None, None)),
    ("about 30 cm tall", (None, None, None)),
    ("under 5 lbs", (None, None, None)),
    ("500 m", (None, None, None)),
    ("at least 8 hours of battery", (None, None, None)),
    # -- money that must stay money ----------------------------------------------------
    ("$5", (5.0, None, "0-50")),
    ("a hundred bucks", (100.0, None, "100-250")),
    ("no more than 250 dollars", (250.0, None, "250-500")),
    ("40", (40.0, None, "0-50")),
    # The currency guard, which nothing exercised: a unit letter standing after an amount
    # is not a unit. Without `_PRICED_NUMBER_RE` the whole span is blanked and $5 vanishes.
    ("$5 m", (5.0, None, "0-50")),
    ("$300 l", (300.0, None, "250-500")),
    # Thousands separators. `_DIGIT_AMOUNT` stopped at the comma and every caller took the
    # leading group as the whole amount, so "a sofa under $1,200" shipped `price_usd lte
    # 1.0` — a filter one twelve-hundredth of what the shopper said, on the served route.
    # Four figures is where a furniture budget lives, and a comma is how people write it.
    ("a sofa under $1,200", (1200.0, None, "1000+")),
    ("no more than 2,500 dollars", (2500.0, None, "1000+")),
    ("under $1,200 in oak", (1200.0, None, "1000+")),
    ("1,200 mm wide", (None, None, None)),
)


@pytest.mark.parametrize(("utterance", "expected"), MONEY_CASES, ids=[c[0] for c in MONEY_CASES])
def test_a_unit_of_measure_never_eats_a_stated_budget(utterance, expected) -> None:
    """``_MEASURE_SPAN_RE`` carried a bare ``in\\b``, so "under $400 in oak" lost the $400.

    The span is blanked before ANY money pattern runs, so the ``$400`` was gone before
    ``_CEILING_RE`` ever looked at the text::

        'a coffee table under $400 in oak'  ->  'a coffee table under $       oak'
        'a rug under $300 in wool'          ->  'a rug under $       wool'

    Which recreates the owner's own screenshot with the fix written for it: the loop asks
    for a budget the shopper already stated. ``in`` is the only unit in that table that is
    also an ordinary English word, and it has to be told apart from the preposition by
    something in the text — ``48in``, ``48 in``, ``48 in wide`` are the shapes people write.
    """
    from apps.buyer.svc.src.intent.extraction import read_budget

    reading, _ = read_budget(utterance, allow_bare_number=True)
    assert (reading.ceiling, reading.floor, reading.band) == expected, (
        f"{utterance!r} was read as {(reading.ceiling, reading.floor, reading.band)!r}, "
        f"expected {expected!r}"
    )


def test_a_budget_stated_before_the_word_in_is_not_asked_for_again(client) -> None:
    """The served consequence, and the owner's exact complaint recreated.

    Driven through ``POST /buyer/intent/clarify``: a shopper states a $400 ceiling in a
    sentence that happens to contain the word "in", and the loop asks them for a budget.
    """
    body = client.post(
        CLARIFY, json={"turns": ["a coffee table under $400 in oak", "I already told you"]}
    ).json()
    ceilings = [
        constraint
        for constraint in body["intent"]["hard_constraints"]
        if constraint["field"] == "price_usd" and constraint["op"] == "lte"
    ]
    assert ceilings == [{"field": "price_usd", "op": "lte", "value": 400.0}], (
        "the shopper's stated $400 ceiling never reached the intent: "
        f"{body['intent']['hard_constraints']!r}"
    )
    assert body["intent"]["budget_band"] != "unspecified", body["intent"]["budget_band"]
    assert not any(question in CANNED_QUESTIONS[GAP_BUDGET] for question in body["questions"]), (
        f"the loop asked for a budget the shopper had already stated: {body['questions']!r}"
    )
    assert GAP_BUDGET not in body["unresolved"], body["unresolved"]


def test_a_model_invented_price_bound_is_not_applied_as_an_eligibility_filter() -> None:
    """``_OPEN_VALUE_TERMS`` vouched for ``price_usd``/``size`` on field+op ALONE.

    So any value a model named became an eligibility filter — and price is the one field
    this corpus cannot decide at all. Measured on the served exchange
    (``POST http://localhost:8083/auctions``, nineteen-store corpus), query "a sofa"::

        hard constraints                slots  shops  products_considered
        (none)                              3      3                   25
        price_usd lte 5000                  0      0                    0
        price_usd lte 200                   0      0                    0
        size      eq  queen                 0      0                    0

    5000 is above every price in the corpus, so this is not a ceiling doing its job — the
    graph holds no ``price_usd`` reading for any product, the pushdown filter matches
    nothing, and retrieval returns an empty roster. A model-proposed bound therefore costs
    the whole page, which is precisely what softening exists to prevent.
    """
    reply = json.dumps(
        {
            "hard_constraints": [{"field": "price_usd", "op": "lte", "value": 5000}],
            "preferences": [],
            "clarifying_question": None,
        }
    )
    outcome = clarify(["a sofa for the living room"], ScriptedLLM(reply, reply, reply))
    prices = [
        constraint
        for constraint in outcome.intent.hard_constraints
        if constraint.field == "price_usd"
    ]
    assert prices == [], (
        "a price bound the shopper never stated became an eligibility filter, which empties "
        f"the shortlist: {prices!r}"
    )
    assert any(reading.field == "price_usd" for reading in outcome.softened), (
        f"the model's bound was not kept as a softened reading either: {outcome.softened!r}"
    )


def test_the_shoppers_own_price_ceiling_is_still_a_hard_filter(client) -> None:
    """The control for the rule above, and the thing it must not break.

    R19 gives the shopper's stated ceiling the filtering half, and
    ``exchange.ranking.filters.budget_reasons`` decides it against the offer's own price.
    Narrowing what a MODEL may propose must leave what the BUYER said exactly where it was.
    """
    body = client.post(CLARIFY, json={"turns": ["a linen duvet cover under $200"]}).json()
    ceilings = [
        constraint
        for constraint in body["intent"]["hard_constraints"]
        if constraint["field"] == "price_usd" and constraint["op"] == "lte"
    ]
    assert ceilings == [{"field": "price_usd", "op": "lte", "value": 200.0}], (
        f"the buyer's own ceiling stopped being a hard constraint: "
        f"{body['intent']['hard_constraints']!r}"
    )
    assert not any(reading["field"] == "price_usd" for reading in body["softened"]), (
        f"the buyer's own ceiling was softened as though a model had invented it: "
        f"{body['softened']!r}"
    )


def test_an_answer_is_filed_against_what_it_produced_not_against_what_was_asked(
    client,
) -> None:
    """An ``understood`` row keyed by the ASKED gap asserts an answer it never contained.

    Measured on the served route with the four turns below::

        {"gap": "budget",
         "question": "What is the most you would want to spend?",
         "answer": "It must be cherry wood, and at least 48 inches wide",
         "used": ["material is cherry-wood"],
         "understood": true}

    which the page renders as "You did answer about budget" over a sentence about material,
    and which also tells ``next_gap`` that the budget has been addressed, so it is never
    asked again. The answer addressed the CONSTRAINTS gap. Filing it under the question it
    happened to follow is the same class of untruth this whole record was added to end.
    """
    body = client.post(
        CLARIFY,
        json={
            "turns": [
                "I want a cherry wood table",
                "It must be cherry wood, and at least 48 inches wide",
                "no, that is everything",
                "nothing else",
            ]
        },
    ).json()
    rows = body["understood"]
    material_rows = [row for row in rows if "cherry" in row["answer"]]
    assert material_rows, f"the material answer is not on the record at all: {rows!r}"
    for row in material_rows:
        assert GAP_CONSTRAINTS in row["addressed"], (
            f"an answer that named a material is not filed against the must-haves gap: {row!r}"
        )
        # The whole regression in one assertion. The sentence names no money.
        assert GAP_BUDGET not in row["addressed"], (
            "an answer about material is filed against the gap it was asked under rather "
            f"than the one it actually spoke to: {row!r}"
        )
    for row in rows:
        assert set(row["addressed"]) <= {GAP_USE_CASE, GAP_BUDGET, GAP_CONSTRAINTS}, row
        if not row["addressed"]:
            assert row["used"] == [], (
                f"a row produced terms but addressed no gap, which cannot be true: {row!r}"
            )


def test_every_shipped_dialogue_still_asks_the_number_of_questions_it_declares() -> None:
    """The question SEQUENCE was ungraded, and this repair moves it.

    ``fixtures/dialogues/*.json`` each declare ``_questions_expected``, and the field is
    read in exactly two places — ``test_intent_models.test_the_shipped_dialogues_are_well
    _formed`` checks that some fixture declares at least one, and
    ``test_one_shipped_dialogue_reaches_the_three_question_ceiling`` checks that the largest
    declared number is three. Both read the FIXTURE FILE. Neither runs the loop, and
    ``.swarm-loop/acceptance/test_e7_buyer.py`` grades the resulting intent and the "at most
    three" cap but never the count or the order. So no test anywhere would have noticed the
    loop asking a different gap, or the same gap a different number of times.

    That matters here because :meth:`IntentDraft.gaps_the_shopper_addressed` is what decides
    when a gap may be put to the shopper twice, and this change re-keys it from the gap that
    was asked to the gap the answer spoke to. ``maximally_vague_gift`` is the dialogue that
    depends on the answer: it hedges once, is asked the SAME gap again, and answers it on
    the second attempt — and because ``IntentDraft.absorb`` only lets a turn join ``query``
    when it answers the use-case gap, an ordering that moved on to the budget question
    instead would silently drop "a wool scarf, black" from the golden's ``query``.
    """
    import pathlib

    directory = pathlib.Path(__file__).resolve().parents[4] / "fixtures" / "dialogues"
    files = sorted(directory.glob("*.json"))
    assert files, f"R1's golden dialogues are missing from {directory}"

    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        outcome = clarify(data["turns"], ScriptedLLM(*data.get("llm_script", [])))
        assert len(outcome.questions) == data["_questions_expected"], (
            f"{path.name}: declares {data['_questions_expected']} questions and the loop "
            f"asked {len(outcome.questions)}: {list(outcome.questions)!r}"
        )
        assert outcome.intent.query == data["expected_intent"]["query"], (
            f"{path.name}: the buyer's own words changed — a turn joined or left `query`, "
            f"which is decided by WHICH gap it was absorbed against.\n"
            f"  golden: {data['expected_intent']['query']!r}\n"
            f"  actual: {outcome.intent.query!r}"
        )


# --------------------------------------------------------------------------------------
# 10. what an adversarial pass found still untrue after the first repair
# --------------------------------------------------------------------------------------


def test_a_model_echo_of_the_shoppers_own_ceiling_is_not_shown_as_a_second_claim() -> None:
    """The skip-guard in ``absorb_proposal``, which nothing exercised.

    Removing it left the whole buyer suite green while the page showed ``Budget: at most
    $200`` and, under "Noted, but not used to rule anything out", "price at most 5000
    dollars" — the model's number presented beside the shopper's, with nothing saying which
    was whose.
    """
    reply = json.dumps(
        {
            "hard_constraints": [{"field": "price_usd", "op": "lte", "value": 5000}],
            "preferences": [],
            "clarifying_question": None,
        }
    )
    outcome = clarify(["a linen duvet cover under $200"], ScriptedLLM(reply, reply, reply))
    prices = [
        constraint
        for constraint in outcome.intent.hard_constraints
        if constraint.field == "price_usd"
    ]
    assert [(item.op, item.value) for item in prices] == [("lte", 200.0)], (
        f"the shopper's own ceiling is not the only price constraint: {prices!r}"
    )
    assert not any(reading.field == "price_usd" for reading in outcome.softened), (
        "a model's echo of the shopper's own budget is shown as a second, softer claim "
        f"about the same number: {outcome.softened!r}"
    )

    # And the skip is RECORDED rather than silent. Both guards used to drop a proposal with
    # `dropped` empty, which is the same blindness that hid the discarded model reply one
    # layer down — and it compounds: a misread ceiling becomes authoritative and then
    # shields the model's correct one from ever being written down.
    assert any("theirs stands" in note for note in outcome.dropped), (
        f"a model proposal was skipped and nothing records that it was: {outcome.dropped!r}"
    )


def test_a_model_constraint_whose_value_is_a_list_does_not_take_the_route_down() -> None:
    """``op: "in"`` carries a LIST, and a list is not hashable.

    ``vouched_as_filter`` tests ``(field, op, value) in VOUCHED_FILTER_TERMS``, so a model
    reply of ``{"field": "material", "op": "in", "value": ["oak", "walnut"]}`` raised
    ``TypeError`` straight out of the handler: measured, ``POST /buyer/intent/clarify``
    answered **HTTP 500**. ``in`` is a published ``CONSTRAINT_OPS`` member, ``intent.ts``
    has a branch that renders one, and ``test_intent_models`` already builds a reply
    carrying it — a list is the intended shape, not a malformed one.
    """
    reply = json.dumps(
        {
            "hard_constraints": [{"field": "material", "op": "in", "value": ["oak", "walnut"]}],
            "preferences": [],
            "clarifying_question": None,
        }
    )
    outcome = clarify(["a dining table"], ScriptedLLM(reply, reply, reply))
    assert not any(constraint.op == "in" for constraint in outcome.intent.hard_constraints), (
        "a value this lexicon cannot have minted was vouched as an eligibility filter: "
        f"{outcome.intent.hard_constraints!r}"
    )
    assert any(reading.op == "in" for reading in outcome.softened), (
        f"the model's list-valued reading was lost entirely: {outcome.softened!r}"
    )


def test_the_served_route_survives_a_list_valued_model_constraint(client) -> None:
    """The same thing through the door, because a 500 is what the shopper actually met."""
    set_buyer_llm(
        ScriptedLLM(
            *[
                json.dumps(
                    {
                        "hard_constraints": [
                            {"field": "material", "op": "in", "value": ["oak", "walnut"]}
                        ],
                        "preferences": [],
                        "clarifying_question": None,
                    }
                )
            ]
            * 4
        )
    )
    response = client.post(CLARIFY, json={"turns": ["a dining table"]})
    assert response.status_code == 200, response.text


def test_a_model_may_not_add_the_other_half_of_a_range_the_shopper_never_asked_for() -> None:
    """``HardConstraint.key`` is ``(field, op)``, so an echo test alone leaks the floor.

    With the shopper's ``price_usd lte 200`` on file, a model proposing ``price_usd gte
    100`` does not collide with it, and the page rendered "price at least 100 dollars"
    beside the shopper's own ceiling — a bound they never uttered, with a ``maximize price``
    preference behind it on a shopper who had just stated a ceiling.
    """
    reply = json.dumps(
        {
            "hard_constraints": [{"field": "price_usd", "op": "gte", "value": 100}],
            "preferences": [],
            "clarifying_question": None,
        }
    )
    outcome = clarify(["a linen duvet cover under $200"], ScriptedLLM(reply, reply, reply))
    assert not any(reading.field == "price_usd" for reading in outcome.softened), (
        f"a model invented the other end of the shopper's budget: {outcome.softened!r}"
    )
    assert not any(
        preference.field == "price_usd" and preference.direction == "maximize"
        for preference in outcome.intent.preferences
    ), (
        "a shopper who stated a ceiling is being scored on wanting the price HIGHER: "
        f"{outcome.intent.preferences!r}"
    )


def test_a_model_op_the_lexicon_cannot_mint_is_still_kept_on_a_field_the_buyer_named() -> None:
    """The control for the two guards above: they refuse a CONTRADICTION, not a refusal.

    ``material contains "merino wool"`` beside the shopper's own ``material eq merino-wool``
    is the model being told no, and the shopper is owed the record of it. Only a numeric
    bound on a field the buyer already bounded is dropped.
    """
    reply = json.dumps(
        {
            "hard_constraints": [{"field": "material", "op": "contains", "value": "merino wool"}],
            "preferences": [],
            "clarifying_question": None,
        }
    )
    outcome = clarify(
        ["I want a warm merino wool beanie for winter, under $100"],
        ScriptedLLM(reply, reply, reply),
    )
    assert any(reading.op == "contains" for reading in outcome.softened), (
        f"a refused model op was dropped instead of recorded: {outcome.softened!r}"
    )


def test_a_reading_this_network_cannot_score_says_so_rather_than_claiming_it_ranks() -> None:
    """Softening a price bound produced a preference the exchange throws away.

    ``exchange.retrieval.criteria.build_query`` calls
    ``contracts.ranking.preference_term_conflict`` and REFUSES every ``price_usd``,
    ``delivery_*`` and ``trust`` preference before anything is scored. Measured: a softened
    ``price_usd lte 300`` put ``price_usd minimize 0.3`` on the intent and produced
    ``preferences kept: []`` in the query built from it — while the confirmation screen told
    the shopper "we use them to rank rather than to exclude". Neither filtering nor ranking,
    and said out loud to be ranking.
    """
    from contracts.ranking import preference_term_conflict

    reply = json.dumps(
        {
            "hard_constraints": [{"field": "price_usd", "op": "lte", "value": 300}],
            "preferences": [],
            "clarifying_question": None,
        }
    )
    outcome = clarify(["a sofa for the living room"], ScriptedLLM(reply, reply, reply))
    price = [reading for reading in outcome.softened if reading.field == "price_usd"]
    assert price, f"the model's bound was not kept at all: {outcome.softened!r}"
    assert price[0].scored is False, (
        f"a reading on a field the exchange refuses to score is marked as scoring: {price[0]!r}"
    )
    assert not any(preference.field == "price_usd" for preference in outcome.intent.preferences), (
        "a preference the exchange discards before scoring was still minted: "
        f"{outcome.intent.preferences!r}"
    )
    # And the rule is read from the contract rather than restated here, so a change to the
    # published terms cannot leave this service claiming something it no longer does.
    assert preference_term_conflict("price_usd") is not None
    assert preference_term_conflict("material") is None


def test_an_answer_whose_only_subject_is_closed_is_still_filed_against_the_question_asked(
    client,
) -> None:
    """Keying a row ONLY on what it produced orphaned rows off the page entirely.

    Measured on the served route with the three turns below: both ``understood`` rows are
    asked under ``budget`` and address ``use_case``, which is already closed — so neither
    was rendered anywhere and the page said "We never got an answer about: budget" to a
    shopper who had answered that question twice. A row belongs to the subjects it spoke to,
    and failing that to the question it followed; it is never nowhere.
    """
    body = client.post(
        CLARIFY,
        json={
            "turns": [
                "I want a cherry wood table",
                "It must be cherry wood, and at least 48 inches wide",
                "nothing else",
            ]
        },
    ).json()
    for gap in body["unresolved"]:
        rows = [row for row in body["understood"] if gap in row["addressed"] or row["gap"] == gap]
        assert rows or not any(row["gap"] == gap for row in body["understood"]), (
            f"the shopper was asked about {gap!r} and every record of their answer is "
            f"unreachable from it: {body['understood']!r}"
        )
    asked = {row["gap"] for row in body["understood"]}
    reachable = {gap for row in body["understood"] for gap in (row["addressed"] or [row["gap"]])}
    assert asked <= reachable | {gap for row in body["understood"] for gap in [row["gap"]]}, (
        asked,
        reachable,
    )


@pytest.mark.parametrize(
    "hedge",
    [
        "no idea honestly",
        "not sure",
        "whatever is reasonable",
        "i really cannot say",
        "depends honestly",
        "nothing else",
    ],
)
def test_two_hedges_that_mean_the_same_thing_are_recorded_the_same_way(hedge: str) -> None:
    """``_gaps_addressed`` credited the use-case gap from ``Extraction.specific`` alone.

    ``specific`` is a token COUNT — "two content words that are neither filler nor a hedge"
    — so "no idea honestly" addressed nothing while "i really cannot say" addressed
    ``use_case``, and the second was orphaned off the page while the first rendered
    correctly. Two hedges that mean the same thing produced opposite sentences. An utterance
    that yielded no term at all addressed nothing, whatever its word count.
    """
    from apps.buyer.svc.src.intent.extraction import _gaps_addressed, extract

    reading = extract(hedge, budget_answer=True)
    assert _gaps_addressed(reading) == (), (
        f"{hedge!r} yielded no term and was still filed against a gap: {_gaps_addressed(reading)!r}"
    )


def test_nothing_the_page_prints_back_is_a_machine_term(client) -> None:
    """``understood[].used`` is rendered verbatim inside "We kept …".

    Measured before this: the turns "I want a cherry wood table" / "oak, under $300" put
    ``We kept price usd lte 300.0, material is oak`` on the confirmation screen — the raw
    field, the raw op and a float, in a sentence addressed to a shopper.
    """
    dialogues = [
        ["I want a cherry wood table", "oak, under $300"],
        # Every one of these produced a machine term that the two assertions below missed
        # when they were the whole test: "the lowest possible price usd" and "the lowest
        # possible weight grams" (the preference branch never went through
        # `_field_in_words`), "waterproof is True" (a Python literal), "size is 10.0" (a
        # float for a shoe size).
        ["a daypack for commuting", "as cheap as possible"],
        ["a daypack for commuting", "something lightweight and waterproof"],
        ["running shoes for the trail", "size 10"],
        ["a wool scarf", "at least 48 inches wide"],
    ]
    printed: list[str] = []
    for turns in dialogues:
        body = client.post(CLARIFY, json={"turns": turns}).json()
        printed += [term for row in body["understood"] for term in row["used"]]
    assert printed, "no dialogue kept anything at all, so this test measures nothing"

    for term in printed:
        assert "_" not in term, f"a raw field name reached the shopper: {term!r}"
        # `in` is deliberately absent from this list: it is an ordinary English word and
        # "the lowest possible weight in grams" is the correct rendering, not a raw op.
        # `_OP_IN_WORDS` spells the op `in` as "is one of", which this catches by shape.
        for op in (" lte ", " gte ", " eq ", " contains "):
            assert op not in f" {term} ", f"a raw op reached the shopper: {term!r}"
        assert "[" not in term and "]" not in term, (
            f"a Python container reached the shopper: {term!r}"
        )
        # A field whose name ends in a UNIT reads as two words unless the tail is read back
        # off: the measured forms were "the lowest possible price usd", "the lowest possible
        # weight grams" and "width in at least 48". "weight IN grams" is the correct
        # rendering and is deliberately not caught here.
        for machine in ("price usd", "weight grams", "delivery days", "width in at", "usd at"):
            assert machine not in term, (
                f"a unit suffix is being read as part of the field name: {term!r}"
            )
        assert not term.endswith(" usd"), f"a bare unit suffix reached the shopper: {term!r}"
        assert "True" not in term and "False" not in term, (
            f"a Python literal reached the shopper: {term!r}"
        )
        assert not re.search(r"\b\d+\.0\b", term), (
            f"a float reached the shopper where a whole number was meant: {term!r}"
        )


def test_a_model_may_not_restate_a_number_the_shopper_settled_however_it_is_spelled() -> None:
    """``in`` and ``contains`` walked past a guard that tested the OP.

    The op separates a refusal from a contradiction on a field of VOCABULARY — a model
    proposing ``material contains "merino wool"`` beside the shopper's own ``material eq
    merino-wool`` is being told no, and they are owed the record. It separates nothing on a
    field of NUMBERS: a range and a bound are competing claims about one number however they
    are spelled. Measured, each rendered a second price or size beside the shopper's own::

        "a linen duvet cover under $200" + price_usd in [100, 200]  -> "price is one of 100, 200"
        "a linen duvet cover under $200" + price_usd contains "200" -> "price includes 200"
        "running shoes size 10"          + size in [10, 11]         -> "size is one of 10, 11"
    """
    cases = [
        ("a linen duvet cover under $200", {"field": "price_usd", "op": "in", "value": [100, 200]}),
        (
            "a linen duvet cover under $200",
            {"field": "price_usd", "op": "contains", "value": "200"},
        ),
        ("running shoes size 10", {"field": "size", "op": "in", "value": [10, 11]}),
        # The `eq` widening, which nothing exercised while the guard read only bounds.
        ("a linen duvet cover under $200", {"field": "price_usd", "op": "eq", "value": 200}),
    ]
    for opener, proposed in cases:
        reply = json.dumps(
            {"hard_constraints": [proposed], "preferences": [], "clarifying_question": None}
        )
        outcome = clarify([opener], ScriptedLLM(reply, reply, reply))
        field = proposed["field"]
        assert not any(reading.field == field for reading in outcome.softened), (
            f"{opener!r} + model {proposed!r} put a second claim about {field} on the page: "
            f"{outcome.softened!r}"
        )
        assert not any(preference.field == field for preference in outcome.intent.preferences), (
            f"…and scored it too: {outcome.intent.preferences!r}"
        )
        assert any("theirs stands" in note for note in outcome.dropped), (
            f"…and nothing records the refusal: {outcome.dropped!r}"
        )


def test_a_refusal_is_recorded_once_however_many_rounds_the_model_speaks_in() -> None:
    """``dropped`` is read by a human and was not de-duplicated.

    ``_add_softened`` next door has always de-duplicated. The clarify loop consults the
    model once per round, so a deterministic model saying the same thing each round wrote
    the identical sentence two and three times over.
    """
    reply = json.dumps(
        {
            "hard_constraints": [{"field": "price_usd", "op": "gte", "value": 100}],
            "preferences": [],
            "clarifying_question": None,
        }
    )
    outcome = clarify(
        ["a linen duvet cover under $200", "no thanks", "nothing else"],
        ScriptedLLM(reply, reply, reply, reply),
    )
    assert len(outcome.dropped) == len(set(outcome.dropped)), (
        f"the same refusal is recorded more than once: {outcome.dropped!r}"
    )


def test_a_shopper_who_named_one_end_of_a_range_is_not_told_they_settled_it() -> None:
    """The refusal note is itself a claim, and it was overclaiming.

    A shopper who said "over $100" stated a floor. Skipping the model's ceiling is right —
    it is a bound they never uttered — but recording it as "a subject the shopper had
    already settled" is the same class of overclaim the confirmation screen was repaired
    for, one layer down.
    """
    reply = json.dumps(
        {
            "hard_constraints": [{"field": "price_usd", "op": "lte", "value": 500}],
            "preferences": [],
            "clarifying_question": None,
        }
    )
    outcome = clarify(["a rug over $100"], ScriptedLLM(reply, reply, reply))
    assert outcome.dropped, "the model's ceiling was skipped with no record at all"
    for note in outcome.dropped:
        assert "settled" not in note, (
            f"the shopper stated one END of a range and is told they settled it: {note!r}"
        )


def test_a_turn_nobody_asked_about_is_not_recorded_as_an_answer(client) -> None:
    """``clarifier.clarify`` absorbs leftover turns with ``gap=GAP_USE_CASE``.

    Not because anybody asked about the use case — because that is the gap whose answers
    join ``query``. Every one of those turns produced an ``understood`` row carrying an
    EMPTY ``question``, and the confirmation screen renders a row as "You did answer when we
    asked about what you are shopping for". Measured on the served route with the three
    turns below, which cost exactly one question::

        {"gap": "use_case", "question": "", "answer": "nothing else", "used": []}

    A record of a question is a claim that it was asked.
    """
    body = client.post(
        CLARIFY,
        json={"turns": ["I want a cherry wood table", "oak, under $300", "nothing else"]},
    ).json()
    assert len(body["questions"]) == 1, body["questions"]
    for row in body["understood"]:
        assert row["question"], (
            "a turn nobody asked about is recorded as an answer to a question: "
            f"{row!r}; the loop asked {body['questions']!r}"
        )
    assert len(body["understood"]) <= len(body["questions"]), (
        f"more answers on the record than questions asked: {body['understood']!r} for "
        f"{body['questions']!r}"
    )
