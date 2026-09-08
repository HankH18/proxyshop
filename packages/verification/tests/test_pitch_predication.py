"""The guard reaches EVERY rule, and a phrase is not an assertion about THIS offer.

The defect this file grades
---------------------------
``pitch._asserts_current_state`` ran inside ``_FlagRule.read`` and nowhere else, so eight of the
ten rules in ``PITCH_RULES`` — the two duration rules, four quantity rules, the vocabulary rule
and the two day-window rules — minted a reading out of any sentence their pattern touched:
negated, historical, comparative, conditional, or plainly about a different product. And the
flag guard that did exist was a DENYLIST of sentence shapes, so honest constructions nobody had
thought of walked straight past it.

Measured before the change, each against a catalogue recording ``availability: "in_stock"``,
``boiler_type: "heat exchange"``, ``pump_pressure_bar: 15``, ``warranty_months: 24``,
``return_window_days: 30`` and ``water_tank_l: 2.9`` — **a platform that AGREES with the store
on every fact it holds**::

    "Our competitors are out of stock right now."          -> in_stock False        contradicted
    "No more out of stock disappointments."                -> in_stock False        contradicted
    "Nobody likes seeing sold out."                        -> in_stock False        contradicted
    "The refurbished listing is sold out; this is the new unit."
                                                           -> in_stock False        contradicted
    "Unlike the 9 bar competitor, ours delivers 15 bar of pressure."
                                                           -> pump_pressure_bar 9   contradicted
    "Our previous model had a 12 month warranty; this one carries a 24 month warranty."
                                                           -> warranty_months 12    contradicted
    "It is not a 3 bar machine."                           -> pump_pressure_bar 3   contradicted
    "Returns are accepted within 14 days on clearance and within 30 days on everything else."
                                                           -> return_window_days 14 contradicted
                                                              (and still does — see
                                                              ``test_a_scope_restriction_after_
                                                              the_match_is_a_known_false_
                                                              positive``, which records why)
    "If you would rather have the 1.5 L tank, the smaller reservoir model is over here."
                                                           -> water_tank_l 1.5      contradicted

Nine honest sentences, nine ``policy_penalties: -0.15`` plus a ``catalog_claim_accuracy`` hit
each, from a platform whose own record said the store was telling the truth. **Eight of the nine
are closed.** The ninth is the scope restriction, and it is not closed but recorded: the guard
that caught it searched the whole clause including the tail the SELLER writes, and an
adversarial sweep found 125 working kill switches through that tail. See
``test_a_scope_restriction_after_the_match_is_a_known_false_positive`` at the foot of this file
for the measurement and the reason a wider pattern is not the fix.

What a denylist cannot promise, stated so nobody re-derives it
--------------------------------------------------------------
No lexical rule can deliver "a store saying something true is never punished, whatever words it
chooses". The ways to say a true thing are unbounded; a pattern set is finite. What a rule set
CAN choose is the direction it errs in, and that is the change these gates hold: the tests that
decide whether a match becomes a reading are stated as ALLOWLISTS (a closed-class tail, a
governed clause), so a construction nobody anticipated yields SILENCE rather than a reading —
and silence costs the seller exactly what not writing the sentence costs.

Both columns, always
--------------------
A suite that only proves suppression is passed by deleting the rules, so every declined sentence
here is paired with the sentence one phrase away from it that must still mint, and every
declined sentence's rule is separately proved to still catch a liar.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from claim_verification.pitch import _clause_boundaries, decompose_pitch
from claim_verification.verifier import catalog_keys, verify

VERIFIER_VERSION = "test-predication"
PRODUCT = "prod-1"

#: A crawl-shaped snapshot that AGREES with the store about everything it records. Every
#: ``contradicted`` below is therefore the decomposer reading a claim the store did not make.
#: ``read_at`` twenty minutes after ``captured_at`` keeps the stock fact inside D59's freshness
#: window, so the flag readings really are graded rather than excused.
AGREEING_CATALOGUE: dict[str, Any] = {
    "snapshot_id": "snap-agreeing",
    "captured_at": "2026-09-07T12:00:00Z",
    "read_at": "2026-09-07T12:20:00Z",
    "products": [
        {
            "product_ref": PRODUCT,
            "canonical_name": "Espresso machine",
            "status": "active",
            "attributes": {
                "boiler_type": {"value": "heat exchange"},
                "pump_pressure_bar": {"value": 15.0, "unit": "bar"},
                "warranty_months": {"value": 24, "unit": "months"},
                "return_window_days": {"value": 30, "unit": "days"},
                "water_tank_l": {"value": 2.9, "unit": "L"},
                "dispatch_window": {"value": 2, "unit": "days"},
            },
            "offer": {"price": 389.0, "currency": "USD", "availability": "in_stock"},
        }
    ],
}


def _readings(text: str) -> list[tuple[str, Any]]:
    return [
        (str(claim["key"]), claim["value"])
        for claim in decompose_pitch(
            text, store_id="store-1", vocabulary=catalog_keys(AGREEING_CATALOGUE, PRODUCT)
        )
    ]


def _verdicts(text: str) -> list[tuple[str, Any, str]]:
    """``(key, claimed value, status)`` per reading, against the agreeing catalogue."""
    claims = decompose_pitch(
        text, store_id="store-1", vocabulary=catalog_keys(AGREEING_CATALOGUE, PRODUCT)
    )
    if not claims:
        return []
    result = verify(
        {
            "pitch_id": "p-1",
            "store_id": "store-1",
            "product_ref": PRODUCT,
            "text": text,
            "claims": [dict(claim, claim_ref=str(index)) for index, claim in enumerate(claims)],
        },
        AGREEING_CATALOGUE,
        VERIFIER_VERSION,
    )
    return [
        (str(row["key"]), claim["value"], str(row["status"]))
        for claim, row in zip(claims, result["claims"], strict=True)
    ]


#: ``(honest sentence, what it minted before, the twin one phrase away that must still mint)``.
#: Every left column entry graded ``contradicted`` against the agreeing catalogue above.
PUNISHED_BEFORE: tuple[tuple[str, tuple[str, Any], tuple[str, Any]], ...] = (
    (
        "Our competitors are out of stock right now.",
        ("in_stock", False),
        ("We are out of stock right now.", ("in_stock", False)),
    ),
    (
        "No more out of stock disappointments.",
        ("in_stock", False),
        ("Out of stock, sorry.", ("in_stock", False)),
    ),
    (
        "Nobody likes seeing sold out.",
        ("in_stock", False),
        ("Sold out, sorry.", ("in_stock", False)),
    ),
    (
        "The refurbished listing is sold out; this is the new unit.",
        ("in_stock", False),
        ("This unit is sold out.", ("in_stock", False)),
    ),
    (
        "It is not a 3 bar machine.",
        ("pump_pressure_bar", 3.0),
        ("It is a 3 bar machine.", ("pump_pressure_bar", 3.0)),
    ),
    (
        "If you would rather have the 1.5 L tank, the smaller reservoir model is over here.",
        ("water_tank_l", 1.5),
        ("It has a 1.5 L tank.", ("water_tank_l", 1.5)),
    ),
)


@pytest.mark.parametrize(("text", "was", "twin"), PUNISHED_BEFORE)
def test_an_honest_sentence_mints_nothing_and_its_twin_still_mints(
    text: str, was: tuple[str, Any], twin: tuple[str, tuple[str, Any]]
) -> None:
    """The left column costs the store nothing; the right column is one phrase away and does.

    Without the twin every guard here could be over-broad — a pattern that declined any
    sentence containing the phrase would satisfy the left column on its own, and would also
    make the rule useless.
    """
    assert _readings(text) == [], f"{text!r} still mints {was}"
    twin_text, expected = twin
    assert _readings(twin_text) == [expected], twin_text


@pytest.mark.parametrize(("text", "was", "_twin"), PUNISHED_BEFORE)
def test_none_of_them_costs_an_honest_store_a_contradiction(
    text: str, was: tuple[str, Any], _twin: object
) -> None:
    """The half that costs money, driven through the real comparator.

    The catalogue agrees with the store about every fact it holds, so each verdict below was
    ``contradicted`` purely because the decomposer read a claim the seller did not make.
    """
    assert _verdicts(text) == [], f"{text!r} still earns a verdict (it used to mint {was})"


#: Sentences that name another product AND this one, where the reading about this offer must
#: SURVIVE. These graded ``contradicted`` before, on the reading about the other product, and a
#: fix that silenced the whole sentence would cost the store the true claim it did make.
CONTRASTIVE_BUT_STILL_INFORMATIVE: tuple[tuple[str, tuple[str, Any], tuple[str, Any]], ...] = (
    (
        "Unlike the 9 bar competitor, ours delivers 15 bar of pressure.",
        ("pump_pressure_bar", 9.0),
        ("pump_pressure_bar", 15.0),
    ),
    (
        "Our previous model had a 12 month warranty; this one carries a 24 month warranty.",
        ("warranty_months", 12),
        ("warranty_months", 24),
    ),
    (
        "This is a heat exchanger, not a dual boiler.",
        ("boiler_type", "dual boiler"),
        ("boiler_type", "heat exchange"),
    ),
    (
        "We never used a thermoblock; this is a heat exchange machine.",
        ("boiler_type", "thermoblock"),
        ("boiler_type", "heat exchange"),
    ),
)


@pytest.mark.parametrize(("text", "dropped", "kept"), CONTRASTIVE_BUT_STILL_INFORMATIVE)
def test_the_clause_about_another_product_is_dropped_and_this_one_is_kept(
    text: str, dropped: tuple[str, Any], kept: tuple[str, Any]
) -> None:
    """Scoped to the reading it cannot make, never to the sentence that carries it.

    The last two entries used to be answered by the whole-pitch cancellation, which dropped
    BOTH readings — so an honest store that stated a true fact in the same breath as a
    contrast got silence for it. Now it gets the ``verified`` it earned.
    """
    assert _readings(text) == [kept], text
    assert dropped not in _readings(text)
    assert _verdicts(text) == [(kept[0], kept[1], "verified")], text


def test_two_readings_neither_of_which_names_this_offer_yield_neither() -> None:
    """The conflict the tie-break cannot decide, and the honest answer to it.

    "The grinder" and "the machine" are two explicit subjects and the catalogue holds one
    offer; which of them it describes is exactly what is undecidable. Two opposite readings the
    decomposer cannot attribute are not evidence for either value.
    """
    assert _readings("The grinder is sold out, but the machine is ready to ship.") == []
    assert _verdicts("The grinder is sold out, but the machine is ready to ship.") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A liar on each rule the guard newly reaches, against the same agreeing catalogue.
        ("Ours delivers 9 bar of pressure.", [("pump_pressure_bar", 9.0, "contradicted")]),
        ("It carries a 60 month warranty.", [("warranty_months", 60, "contradicted")]),
        ("Returns are accepted within 90 days.", [("return_window_days", 90, "contradicted")]),
        ("This is a dual boiler machine.", [("boiler_type", "dual boiler", "contradicted")]),
        ("It has a 1.5 L tank.", [("water_tank_l", 1.5, "contradicted")]),
        ("Sold out, sorry.", [("in_stock", False, "contradicted")]),
        # ...and the truthful form of each, which must still be verified rather than silenced.
        ("Ours delivers 15 bar of pressure.", [("pump_pressure_bar", 15.0, "verified")]),
        ("It carries a 24 month warranty.", [("warranty_months", 24, "verified")]),
        ("Returns are accepted within 30 days.", [("return_window_days", 30, "verified")]),
        ("This is a heat exchanger.", [("boiler_type", "heat exchange", "verified")]),
        ("It has a 2.9 L tank.", [("water_tank_l", 2.9, "verified")]),
        ("In stock and ready to ship today.", [("in_stock", True, "verified")]),
    ],
)
def test_every_rule_the_guard_now_reaches_still_catches_a_liar_and_still_credits_the_truth(
    text: str, expected: list[tuple[str, Any, str]]
) -> None:
    """The adversarial column. Widening a guard until nothing is graded passes every negative
    in this file and none of these."""
    assert _verdicts(text) == expected, text


def test_a_future_commitment_is_still_a_commitment_but_a_future_state_is_not() -> None:
    """Why the mood test is split by what the rule reads, rather than applied uniformly.

    A warranty, a dispatch window and a return window are promises about what the store WILL
    do, and reading "will" as a disqualifier would throw away the honest form of the claim. A
    stock flag and a specification are about how things ARE, and "we will be in stock again on
    Friday" is not a statement that the shelf is full now.
    """
    assert _readings("We will ship your order within 2 days.") == [("dispatch_window", 2)]
    assert _readings("We will honour a 24 month warranty.") == [("warranty_months", 24)]
    assert _readings("We will be in stock again on Friday.") == []
    assert _readings("It will be a 15 bar machine.") == []


def test_a_subordinator_qualifies_the_clause_it_introduces_and_not_the_one_before_it() -> None:
    """The guard used to search the whole clause for ``if``/``when``, which is right for one of
    these and wrong for the other — the same cue, once qualifying the clause that contains the
    match and once qualifying the clause after it."""
    assert _readings("If it does go out of stock we will tell you within the hour.") == []
    assert _readings("Sold out for now; only 2 left when it returns.") == [
        ("in_stock", False),
        ("units_left", 2),
    ]


def test_a_relative_clause_decides_nothing_and_does_not_disqualify_the_clause_before_it() -> None:
    """Measured as a lever: four characters bought immunity from any claim.

    ``which`` was first written as a disqualifier — the antecedent of "…, which is sold out." is
    something this module cannot identify, so the reading is not taken. Applied to the clause it
    SAT IN rather than the clause it INTRODUCES, it disqualified the main clause too, and a
    liar appending a relative tail escaped a contradiction it had earned::

        "It carries a 60 month warranty."                  -> warranty_months 60  contradicted
        "It carries a 60 month warranty which we honour."  -> nothing at all

    It is a clause boundary now, so the main clause keeps its reading and the relative clause
    still decides nothing.
    """
    assert _verdicts("It carries a 60 month warranty which we honour.") == [
        ("warranty_months", 60, "contradicted")
    ]
    assert _verdicts("This runs a 9 bar pump, which is plenty.") == [
        ("pump_pressure_bar", 9.0, "contradicted")
    ]
    # ...and the relative clause itself still decides nothing, in either direction.
    assert _readings("Every size is in stock except the display model, which is sold out.") == []
    assert _readings("The unit, which we restocked, is in stock.") == [("in_stock", True)]


def test_the_clause_scan_is_paid_once_per_sentence_and_not_once_per_match() -> None:
    """A cost bound, and it is a denial-of-service gate rather than a tidy-up.

    Every rule now asks about every one of its matches, and the clause lookup used to re-scan
    the sentence each time. Measured on a 19,000-character SINGLE sentence carrying 500
    ``, but`` pairs — a shape a bidder chooses, inside ``MAX_PITCH_CHARS`` — one
    ``decompose_pitch`` took **1.27 s** on a live bid path, against 0.31 s before the guard
    reached every rule. Memoising the boundary scan and bisecting the lookup makes it 0.02 s.

    Asserted structurally rather than only by the clock: the boundary scan must run once per
    distinct sentence however many matches ask about it. A wall-clock ceiling is kept beside it
    because a structural assertion cannot see a future O(n^2) somewhere else in the path, and
    the ceiling is loose enough (one second, against a measured twenty milliseconds) that only
    a real regression trips it.
    """
    pathological = "This is in stock, but it is sold out, " * 500
    assert len(pathological) > 18_000
    _clause_boundaries.cache_clear()
    started = time.perf_counter()
    decompose_pitch(pathological, store_id="s", vocabulary=("in_stock",))
    elapsed = time.perf_counter() - started

    info = _clause_boundaries.cache_info()
    assert info.misses == 1, (
        f"the clause boundaries of one sentence were scanned {info.misses} times; every rule "
        "asks about every match, so this is the whole cost of the guard"
    )
    assert info.hits > 100, "the memo is not being consulted, so the bound is not being held"
    assert elapsed < 1.0, f"one 19k-character sentence took {elapsed:.2f}s on the bid path"


def test_the_attributive_test_is_an_allowlist_of_what_may_follow_a_predicate() -> None:
    """The predecessor listed the NOUNS that could follow a flag phrase; nouns are an open set.

    **The nouns below are the discriminator and they are chosen on that basis, not on how
    natural they read.** Restoring the predecessor's denylist verbatim — ``orders|items|
    products|lines|skus|variants|models|sizes|options|colou?rs|units|purchases|customers|
    shoppers|deliveries|delivery|shipments|goods|stock|inventory|listings|ranges`` — and
    re-running this file was measured: every head noun IT names still declines, so an
    ``items``/``sizes``/``orders`` example proves nothing about which structure is in force.
    The head nouns here are the ones nobody thought to list, which is the entire claim an
    allowlist makes, and each mints ``in_stock`` under the denylist and nothing under the
    allowlist.

    Two sentences an earlier version of this docstring credited to this test are deliberately
    absent, because the credit was wrong: "No more out of stock disappointments." declines on
    ``no more`` in :data:`~claim_verification.pitch._NEGATED` and "Nobody likes seeing sold
    out." on ``nobody`` in :data:`~claim_verification.pitch._OTHER_REFERENT`. Both are graded,
    with the guard that actually decides them, by
    :func:`test_an_honest_sentence_mints_nothing_and_its_twin_still_mints`.
    """
    for attributive in (
        # Head nouns the predecessor's denylist named — kept so a narrowing is visible too.
        "In stock items ship the same day.",
        "Sold out sizes are restocked weekly.",
        "Back-ordered orders ship separately at no extra cost.",
        # Head nouns it did NOT name. Each of these mints in_stock under the denylist.
        "In stock notifications go out every morning.",
        "Sold out alerts arrive by email.",
        "In stock quantities are shown at checkout.",
        "Out of stock badges appear on every card.",
        "Sold out banners come down at restock.",
        "In stock guarantees apply to every purchase.",
    ):
        assert _readings(attributive) == [], attributive
    for predicative in (
        "In stock, ships the same day.",
        "It is in stock today.",
        "Sold out, restocked weekly.",
        "In stock right now.",
        "Sold out for now.",
    ):
        assert _readings(predicative), predicative


# =====================================================================================
# The residuals, named. A guard whose limits are undocumented gets rediscovered as a defect.
# =====================================================================================
def test_a_scope_restriction_after_the_match_is_a_known_false_positive() -> None:
    """A restriction that follows the claim is read as if the claim were unrestricted.

    This sentence WAS in the table above and is not any more, and the reason is measured. The
    referent guard used to search the whole clause, which caught it — and handed the seller an
    off-switch, because the tail of a sentence is the seller's to write. An adversarial sweep
    over five rules found **125** working kill switches: "This machine is in stock." earns a
    ``contradicted`` and "This machine is in stock in every colour." earns nothing, as do
    ` for anyone`, ` as a bundle`, ` on clearance`, ` in a black finish`, ` on all options`.
    The guard is scoped to the clause PREFIX now — where a subject is — so eight of the nine
    honest sentences above keep their fix and this one does not.

    It is recorded rather than quietly dropped because it is a real cost to a real store, and
    because the fix for it is not a wider pattern: it needs to know that "on clearance" scopes
    a policy while "on all options" does not, which is the catalogue's knowledge and not this
    module's.
    """
    scoped = (
        "Returns are accepted within 14 days on clearance and within 30 days on everything else."
    )
    assert _readings(scoped) == [("return_window_days", 14)]
    assert _verdicts(scoped) == [("return_window_days", 14, "contradicted")]


def test_a_referent_in_the_subject_still_declines_and_that_cuts_both_ways() -> None:
    """The prefix scoping keeps the honest protection and keeps its dual, which is the trade.

    An honest store writing "The 1 kg bag is sold out" is not convicted; a dishonest one
    writing "This 1 kg machine is in stock" is not graded. They are the same sentence shape and
    no lexical rule separates them. The escape buys the liar SILENCE, not a ``verified`` — a
    claim the exchange declines to read earns the store nothing.
    """
    assert _readings("The 1 kg bag is sold out.") == []
    assert _readings("This 1 kg machine is in stock.") == []
    assert _readings("The blue tin is sold out.") == []
    # ...and the same sentences without the discriminator are read in both directions.
    assert _readings("The bag is sold out.") == [("in_stock", False)]
    assert _readings("This machine is in stock.") == [("in_stock", True)]


def test_a_relative_clause_is_licensed_by_its_antecedent_and_not_by_its_pronoun() -> None:
    """``which`` opens a clause about an ANTECEDENT, and sometimes the antecedent is this offer.

    Declining every relative clause was measured as an evasion — "This machine, which is in
    stock, ships fast." escaped a ``contradicted`` that the same claim earns without the
    commas. So the immediately preceding clause decides: a deictic antecedent licenses the
    reading, anything else does not.
    """
    assert _verdicts("This machine, which is in stock, ships fast.") == [
        ("in_stock", True, "verified")
    ]
    assert _readings("The refurbished listing, which is sold out, is not this one.") == []
    # The antecedent is the clause immediately before, not any deixis earlier in the sentence.
    assert _readings(
        "This machine is in stock, and the display model, which is sold out, is over there."
    ) == [("in_stock", True)]


def test_until_is_not_a_statement_about_the_future() -> None:
    """``until``/``till`` were briefly read as futurity markers and let a liar escape:
    "Until further notice this machine is in stock." asserts stock NOW."""
    assert _readings("Until further notice this machine is in stock.") == [("in_stock", True)]
    assert _readings("It is in stock until Friday.") == [("in_stock", True)]


def test_a_parenthetical_aside_does_not_sever_a_cue_from_the_phrase_it_governs() -> None:
    """A comma is not a wall for a negation, and a coordinator is.

    Both left-hand sentences minted a flag against a catalogue that AGREES with the store, on
    the module at 77c83ce and on every version of this guard until the mood scope was widened::

        "It has, in the past, been sold out."     -> in_stock False   contradicted
        "We do not, as a rule, go out of stock."  -> in_stock False   contradicted

    The cue and the phrase it governs sat in different comma-delimited clauses, so the clause
    scoping never saw it. Mood cues now govern from anywhere before the match in the coordinate
    clause; a subject still belongs to its own clause, which is a different question.
    """
    for aside in ("It has, in the past, been sold out.", "We do not, as a rule, go out of stock."):
        assert _readings(aside) == [], aside
        assert _verdicts(aside) == [], aside

    # A coordinator IS a wall, which is the case the clause scoping was written for.
    assert _readings("It is in stock and we do not charge for returns.") == [("in_stock", True)]
    # ...and a negation AFTER the phrase, in its own comma clause, is contrastive: it denies
    # the other thing, and both readings of the sentence are right.
    assert _readings("This is a heat exchanger, not a dual boiler.") == [
        ("boiler_type", "heat exchange")
    ]
    assert _readings("It is in stock, not the grinder.") == [("in_stock", True)]


def test_a_time_adjunct_that_includes_now_is_not_a_statement_about_the_past() -> None:
    """``since`` and ``once`` point at the past only in company, and alone they mean the
    opposite. Reading them as past markers declined seven true present-tense claims per rule
    across an adversarial sweep — which costs an honest store the claim it made, and hands a
    liar the same escape."""
    assert _readings("This machine is in stock since day one.") == [("in_stock", True)]
    assert _readings("It has a 2.9 L tank once you order.") == [("water_tank_l", 2.9)]
    # ...and the sentences that genuinely ARE historical still decline, on the words that
    # actually make them historical.
    assert _readings("It has not been sold out since spring.") == []
    assert _readings("It sold out once in 2024.") == []
    assert _readings("Sold out twice last month; back on the shelf now.") == []


def test_an_ly_word_is_an_adverb_when_it_ends_its_clause_and_a_noun_otherwise() -> None:
    """Position does the work the morphology cannot. English nouns end in ``-ly`` too, and a
    bare ``\\w+ly`` in the predicative tail let four attributive uses through."""
    assert _readings("This machine is in stock immediately.") == [("in_stock", True)]
    assert _readings("It is in stock, shipped promptly.") == [("in_stock", True)]
    for attributive in (
        "In stock supply is limited.",
        "Out of stock supply problems are behind us.",
        "Sold out family packs return Monday.",
        "In stock assembly kits ship free.",
    ):
        assert _readings(attributive) == [], attributive


def test_a_lead_in_does_not_kill_the_claim_that_follows_it() -> None:
    """A colon heading or a scene-setting comma clause is not a parenthetical aside.

    Widening mood scope over EVERY punctuation boundary closed two real false positives and
    opened sixteen liar escapes in the commonest merchant construction there is: a lead-in.
    Measured against a catalogue saying ``out_of_stock``, each of these earned NOTHING where
    the same claim without its lead-in is ``contradicted``::

        "Don't wait: this machine is in stock."        "Nothing beats this: …"
        "Not a problem: …"                             "That was then: …"
        "We were sceptical, …"                         "No more waiting: …"

    A comma is transparent for mood only as the closing half of a bracketed pair, which is what
    an aside actually is.
    """
    for lead in (
        "Don't wait: ",
        "Not a problem: ",
        "Nothing beats this: ",
        "That was then: ",
        "No more waiting: ",
        "It could not be simpler: ",
        "We were sceptical, ",
        "You would not think it, ",
    ):
        assert _readings(f"{lead}this machine is in stock.") == [("in_stock", True)], lead

    # ...and a genuine bracketed aside still carries its cue across, which is the case the
    # widening was for.
    assert _readings("It has, in the past, been sold out.") == []
    assert _readings("We do not, as a rule, go out of stock.") == []


def test_a_relative_antecedent_must_clear_the_referent_guard_too() -> None:
    """Checking the antecedent for deixis alone let the relative path bypass the referent guard.

    ``we``/``our``/``it``/``their`` appear in nearly every merchant sentence, so a licence that
    asked for nothing else was near-vacuous. Measured against a catalogue that AGREES with the
    store, each true, each about somebody else's shelf, each ``contradicted``::

        "Our competitor's model, which is sold out, is cheaper."
        "Their older model, which is sold out, is not ours."
        "Our range, which is sold out, does not include this."
    """
    for other in (
        "Our competitor's model, which is sold out, is cheaper.",
        "Their older model, which is sold out, is not ours.",
        "Our range, which is sold out, does not include this.",
        "The display model (blue), which is sold out, is here.",
    ):
        assert _readings(other) == [], other
        assert _verdicts(other) == [], other

    # ...and a bracketed interruption between the subject and the pronoun is walked over, not
    # read as the antecedent: four characters of parenthesis must not buy silence.
    for bracketed in (
        "This machine (new), which is in stock, ships today.",
        "This machine [refurbished], which is in stock, ships today.",
    ):
        assert _readings(bracketed) == [("in_stock", True)], bracketed


def test_a_bound_quoted_before_a_number_is_not_the_number() -> None:
    """Every quantity rule reads a bare number and every comparator grades it for EQUALITY, so
    a floor or a ceiling was read as the specification. All three of these are TRUE of a 15 bar
    machine and all three were ``contradicted`` against one, on HEAD and until this guard."""
    for bounded in (
        "It delivers more than 9 bar.",
        "At least 9 bar of pressure.",
        "Over 9 bar.",
        "Pressure from 9 bar.",
    ):
        assert _readings(bounded) == [], bounded
        assert _verdicts(bounded) == [], bounded

    # ...including where the rule's own pattern swallowed the bound: ``warranty_term_trailing``
    # starts at the noun and reaches forward to the number, so a look-behind cannot see it.
    assert _readings("It carries a warranty of over 60 months.") == []
    assert _readings("Warranty: at least 24 months.") == []

    # The unbounded forms still read, so this is not the quantity rules being deleted — and a
    # preposition that is not quoting a bound is left alone.
    assert _readings("It delivers 15 bar of pressure.") == [("pump_pressure_bar", 15.0)]
    assert _readings("It carries a warranty of 24 months.") == [("warranty_months", 24)]
    assert _readings("Ships from Portland within 2 business days.") == [("dispatch_window", 2)]


# =====================================================================================
# The one-word tail: both directions of the position rule
#
# Enumerating the predicative adverbs by name left every adverb nobody enumerated as SILENCE,
# and silence is what a false claim costs nothing. Measured against a crawl saying
# `out_of_stock`, twenty-five one-word tails turned a `contradicted` into no verdict at all --
# forever, anyway, indeed, regardless, nonetheless, anytime, overseas, abroad, meanwhile,
# hereafter, forthwith, thereafter, outright, upfront, onwards, overnight and the rest. Driven
# over the served door, "This machine is in stock forever." ranked AHEAD of an identically-lying
# store that still paid, with the false sentence served verbatim in the slot's `message`.
#
# The rule is POSITION, not a longer list of names: a word with nothing after it is modifying
# nothing, so it cannot be the attributive use the allowlist exists to decline. Naming the
# twenty-five would have been worse -- several of them head noun phrases ("in stock FIRST aid
# kits", "NEXT day delivery", "EVERYDAY low prices") -- so it would have reopened the honest
# direction to close the dishonest one.
# =====================================================================================


@pytest.mark.parametrize(
    "sentence",
    [
        "This machine is in stock forever.",
        "This machine is in stock anyway.",
        "This machine is in stock regardless.",
        "This machine is in stock indeed.",
        "This machine is in stock nonetheless.",
        "This machine is in stock overnight.",
        "This machine is in stock abroad.",
        "This machine is in stock outright.",
    ],
)
def test_a_one_word_tail_leaves_the_flag_a_predicate_so_a_liar_can_be_convicted(
    sentence: str,
) -> None:
    """A stock claim with an adverb after it is still a stock claim."""
    claims = decompose_pitch(sentence)
    assert [(c["key"], c["value"]) for c in claims] == [("in_stock", True)], (
        f"{sentence!r} minted nothing, so a crawl saying out_of_stock has no claim to convict "
        f"and the sentence is served to the shopper unchallenged"
    )


@pytest.mark.parametrize(
    "sentence",
    [
        # The four that killed a bare `\w+ly`, all still declined.
        "In stock supply is limited.",
        "Out of stock supply problems are behind us.",
        "Sold out family packs return Monday.",
        "In stock assembly kits ship free.",
        # ...and the ones a longer NAME list would have broken, which is why the rule is
        # position: each has a listed-looking adverb heading a noun phrase.
        "In stock first aid kits ship free.",
        "Next day delivery on every in stock item ships free.",
        "In stock notifications go out every morning.",
    ],
)
def test_a_word_with_more_words_after_it_can_be_a_noun_head_and_is_still_declined(
    sentence: str,
) -> None:
    """The honest direction. An attributive use must cost the seller nothing."""
    assert decompose_pitch(sentence) == [], (
        f"{sentence!r} minted a stock claim out of a phrase used attributively, so an honest "
        f"seller is graded on a noun they never asserted"
    )
