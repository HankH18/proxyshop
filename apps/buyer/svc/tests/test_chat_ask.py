"""The shopper's follow-up questions, driven through the SERVED route.

    POST /buyer/chat/ask   {"auction_id": "...", "question": "..."}   -> 200

Every assertion below is on a response this service actually returned. The exchange is a
double, because what is under test is the buyer's half — what an answer is ALLOWED to be
made of — and a double lets a slot carry the exact combination of published fields each
rule is about. The same route is driven against the real exchange over a real socket in
``test_chat_ask_live.py``.

What this file exists to pin, and each of these is a rule the feature would be unsafe
without:

1. **The browser supplies no facts.** The body has an auction id and a question in it and
   nothing else, and the material is fetched from the exchange by the handler. A page cannot
   make the platform assert something about a shop that never bid.
2. **A shop's message is quoted, never restated.** It is carried on its own field, under the
   shop's domain, and a written answer that reproduces a run of it is refused — even though
   the writer is never shown one.
3. **A shop's claim is always attributed.** A published commitment reaches the shopper with
   the shop's name and its provenance in the same sentence as the value, never as a fact the
   platform is vouching for.
4. **A question the platform cannot ground is a refusal that names what it would have
   needed** — not a fluent guess, and not silence.
5. **It degrades honestly.** With no model configured the answer is the deterministic
   assembly, byte-identically, on every run.
6. **And it does not refuse honest traffic.** Every gate here is also driven in the
   silent-on-honest direction: a question the corpus DOES hold must be answered, and the
   same words must not be reported as unheld.
7. **A comparison is the shape of a question, not a hole in the record.** Every comparative
   and superlative phrasing — "which is better", "what did each shop promise", "why is the
   first one first" — used to come back with a `not_held` that the answer underneath it then
   contradicted. Section 7 drives the phrasings that broke, and drives the refusals beside
   them, because widening what counts as answerable is how a refusal gate stops refusing.
8. **A denial is never printed above an answer that supplies it.** Section 7's fix carried
   its own version of section 7's bug, in the family half rather than the subject half, and
   the whole of section 8 is the mechanisms that keep the two halves apart — each of which
   was measured green with the mechanism deleted before it was written.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from apps.buyer.svc.src.chat import (
    ANSWER_CONTRACT,
    NOT_HELD_DETAIL,
    SOURCE_ASSEMBLED,
    SOURCE_WRITTEN,
    TOPICS,
    VOICE_PLATFORM,
    VOICE_SHOP_CLAIM,
    answer_about,
    answer_prompt,
    assemble,
    corpus_for,
    read_question,
    screen_reasons,
)
from apps.buyer.svc.src.chat.answering import _settles_a_comparison

ASK = "/buyer/chat/ask"
AUCTION = "auction-chat-1"

#: A store that joined the network: its own advocate wrote this and it rode in on the bid.
SHOP_PITCH = (
    "We have knitted these in Yorkshire since 1974 and we will take it back for any reason "
    "inside a month, no questions asked and no restocking fee."
)

#: A second store's own words. Used only by :func:`both_in_network`, and it exists because
#: ``shop_messages`` is the only field on the wire that says how many slots an answer covered
#: — with one message on the shortlist it reads ``1`` whether the answer narrowed or not.
SECOND_PITCH = (
    "Ours are made to a price and we say so: pick us if the number matters more than the "
    "wool does, and send it back within the fortnight if it does not."
)


# ------------------------------------------------------------------------------------------
# payload builders — one shortlist, shaped like the one the exchange really serves
# ------------------------------------------------------------------------------------------


def slot(
    name: str,
    store: str,
    *,
    title: str,
    price: float,
    commitments: list[dict] | None = None,
    message: str | None = None,
    fallback: bool = False,
    labels: list[str] | None = None,
) -> dict:
    """One shortlist slot, in the shape ``GET /auctions/{id}/shortlist`` was MEASURED to serve.

    Field for field from a live demo-market response: ``product.identity`` with its crawl
    ``source`` and ``observed_at``, a ``trust_summary`` carrying the six graded dimensions,
    ``provenance_labels``, ``commitments`` with per-claim provenance, and ``message``.
    """
    return {
        "slot": name,
        "bid_ref": f"{AUCTION}:{store}",
        "fit_score": 0.5,
        "trust_summary": {
            "store_id": store,
            "available": True,
            "score": 0.77,
            "confidence": 0.94,
            "low_data": False,
            "dimensions": [
                "catalog_claim_accuracy",
                "discount_honored",
                "feedback_match",
                "not_returned",
                "price_honored",
                "shipped_on_time",
            ],
        },
        "provenance_labels": labels if labels is not None else ["store-confirmed"],
        "product": {
            "product_ref": f"prod-{store}",
            "variant_ref": None,
            "identity": {
                "title": title,
                "brand": title.split()[0],
                "source": f"snap-{store}",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        },
        "price": {
            "unit_price": price,
            "total_price": price,
            "currency": "USD",
            "discount": None,
            "expires_at": "2026-09-08T19:37:45.767000Z",
        },
        "commitments": commitments,
        "message": message,
        "fallback": fallback,
        "fallback_reason": "store_declined:cluster_not_pursued" if fallback else None,
        "store_domain": store,
    }


def shortlist() -> dict:
    return {
        "auction_id": AUCTION,
        "slots": [
            slot(
                "fit",
                "woolworks.example",
                title="Merino Beanie",
                price=72.0,
                commitments=[
                    {
                        "key": "free_returns",
                        "value": "30 days",
                        "unit": None,
                        "provenance": {
                            "source": "owner_statement",
                            "ref": "envelope:woolworks:v3#free_returns",
                            "observed_at": "2026-01-01T00:00:00Z",
                            "authority_rank": 1,
                        },
                    }
                ],
                message=SHOP_PITCH,
            ),
            slot(
                "value",
                "fastfashion.example",
                title="Acrylic Beanie",
                price=19.0,
                fallback=True,
                labels=["from their website"],
            ),
        ],
    }


def both_in_network() -> dict:
    """The same two options, with the SECOND one carrying a message as well.

    A witness, not a variation: an answer carries the message of every slot it covers, so on
    this shortlist ``len(shop_messages)`` is 2 for an answer about the whole screen and 1 for
    one that narrowed to a card. On :func:`shortlist` it is 1 either way, which is why the
    ordinal gate below could not tell the difference and passed with its rule deleted.
    """
    payload = shortlist()
    payload["slots"][1]["message"] = SECOND_PITCH
    return payload


def recorded() -> dict:
    return {
        "auction_id": AUCTION,
        "recorded_at": "2026-09-08T00:00:00Z",
        "response": {
            "ranked": [
                {
                    "bid_ref": f"{AUCTION}:woolworks.example",
                    "store_id": "woolworks.example",
                    "rank_score": 0.539,
                    "components": {
                        "intent_match": 0.175,
                        "verified_claim_ratio": 0.1,
                        "trust": 0.155,
                        "price_value": 0.059,
                        "delivery_fit": 0.05,
                    },
                },
            ]
        },
    }


class StubExchange:
    """The two READS this route makes, and deliberately not the two writes.

    It has no ``create_auction`` and no ``accept_offer``, so a route that grew a call to
    either would fail here rather than in production. Asking a question may not become a
    purchase.
    """

    def __init__(self, live: dict | None = None, record: dict | None = None) -> None:
        self.live = live
        self.record = record
        self.asked: list[str] = []

    def shortlist_for(self, auction_id: str):
        self.asked.append(auction_id)
        return self.live

    def outcome_for(self, auction_id: str):
        return self.record


@pytest.fixture()
def app_with(monkeypatch):
    """A buyer app with an exchange double bound, and no model configured.

    ``app.state.exchange_client`` is set BEFORE the first request, which
    ``buyer_svc.composition.configure_buyer`` documents as the gesture that wins: it binds
    only what is unset, so a test's double is never replaced by a deployment's client.
    """

    def build(client: object):
        from apps.buyer.svc.src.main import create_app
        from apps.buyer.svc.src.pitch import writer as pitch_writer_module

        monkeypatch.setattr(pitch_writer_module, "_override", None, raising=False)
        monkeypatch.setattr(pitch_writer_module, "_live", None, raising=False)
        monkeypatch.setattr(pitch_writer_module, "_live_built", False, raising=False)
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        app = create_app()
        app.state.exchange_client = client
        return TestClient(app)

    return build


# ------------------------------------------------------------------------------------------
# 1. the browser supplies no facts
# ------------------------------------------------------------------------------------------


def test_the_body_carries_no_shortlist_and_the_handler_fetches_one(app_with):
    exchange = StubExchange(shortlist(), recorded())
    client = app_with(exchange)

    response = client.post(ASK, json={"auction_id": AUCTION, "question": "what is the price?"})

    assert response.status_code == 200, response.text
    # The material came from the exchange, for the id in the body, on this request.
    assert exchange.asked == [AUCTION]
    body = response.json()
    assert body["auction_id"] == AUCTION
    assert {ground["store_domain"] for ground in body["grounds"]} <= {
        "woolworks.example",
        "fastfashion.example",
    }


def test_a_posted_shortlist_is_not_a_field_this_route_reads(app_with):
    """A page that invents a shop cannot make the platform answer about it.

    ``AskBody`` has no such field, so the invented slot is not merely ignored — there is no
    parameter it could have arrived in, and pydantic drops it before the handler runs.
    """
    exchange = StubExchange(shortlist(), recorded())
    client = app_with(exchange)

    response = client.post(
        ASK,
        json={
            "auction_id": AUCTION,
            "question": "what is the price?",
            "shortlist": {
                "auction_id": AUCTION,
                "slots": [slot("fit", "invented.example", title="Gold Beanie", price=1.0)],
            },
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "invented.example" not in response.text
    assert all(ground["store_domain"] != "invented.example" for ground in body["grounds"])


# ------------------------------------------------------------------------------------------
# 2 & 3. the two voices
# ------------------------------------------------------------------------------------------


def test_a_shops_message_is_carried_whole_and_stays_out_of_the_answer(app_with):
    client = app_with(StubExchange(shortlist(), recorded()))

    body = client.post(
        ASK, json={"auction_id": AUCTION, "question": "what is the return policy?"}
    ).json()

    quoted = [row for row in body["shop_messages"] if row["store_domain"] == "woolworks.example"]
    assert quoted and quoted[0]["message"] == SHOP_PITCH
    # Verbatim on its own field, under the shop's name — and nowhere in the platform's own
    # sentences. "Yorkshire" and "restocking" are the shop's words and only the shop's.
    assert "Yorkshire" not in body["answer"]
    assert "restocking" not in body["answer"]


def test_a_written_answer_that_reproduces_a_shops_message_is_refused():
    """The teeth behind rule 2, exercised directly on the screen.

    The writer is never shown a shop's message — :func:`answer_prompt` builds its tail from
    the evidence, which excludes it — so a reply carrying a run of one did not get it from
    the platform. It is refused whatever it happens to say.
    """
    reading = read_question(
        "what is the return policy?", corpus_for(shortlist(), recorded=recorded())
    )

    laundered = (
        "They will take it back for any reason inside a month, no questions asked and no "
        "restocking fee, which is a strong policy."
    )

    reasons = screen_reasons(laundered, reading)
    assert any("launders a shop's message" in reason for reason in reasons), reasons


def test_a_published_commitment_reaches_the_shopper_attributed(app_with):
    client = app_with(StubExchange(shortlist(), recorded()))

    body = client.post(
        ASK, json={"auction_id": AUCTION, "question": "what is the return policy?"}
    ).json()

    claim = [
        ground
        for ground in body["grounds"]
        if ground["key"] == "free returns" and ground["store_domain"] == "woolworks.example"
    ]
    assert claim, body["grounds"]
    assert claim[0]["voice"] == VOICE_SHOP_CLAIM
    assert "woolworks.example published this" in claim[0]["attribution"]
    assert "owner statement" in claim[0]["attribution"]
    assert claim[0]["observed_at"] == "2026-01-01T00:00:00Z"
    # And the ANSWER says who claimed it, in the same breath as the value. A claim rendered
    # bare would read as something the platform had checked.
    assert "woolworks.example published this" in body["answer"]


def test_the_platforms_own_fields_are_not_dressed_in_a_shops_badge(app_with):
    client = app_with(StubExchange(shortlist(), recorded()))

    body = client.post(ASK, json={"auction_id": AUCTION, "question": "what is the price?"}).json()

    prices = [ground for ground in body["grounds"] if ground["topic"] == "price"]
    assert prices
    for ground in prices:
        assert ground["voice"] == VOICE_PLATFORM
        assert ground["attribution"] == "the exchange's published offer for this auction"


# ------------------------------------------------------------------------------------------
# 4. the refusal
# ------------------------------------------------------------------------------------------


def test_a_question_the_platform_cannot_ground_is_a_refusal_that_names_the_subject(app_with):
    client = app_with(StubExchange(shortlist(), recorded()))

    body = client.post(
        ASK,
        json={"auction_id": AUCTION, "question": "which of these is actually third-party tested?"},
    ).json()

    assert body["not_held"] == [{"subject": "third-party tested", "detail": NOT_HELD_DETAIL}]
    assert body["grounds"] == []
    assert "third-party tested" in body["answer"]
    assert "don't know" in body["answer"]
    # And it says what it would have needed, rather than only that it failed.
    assert NOT_HELD_DETAIL in body["answer"]


def test_a_refusal_names_what_the_platform_can_answer_from_instead(app_with):
    client = app_with(StubExchange(shortlist(), recorded()))

    answer = client.post(
        ASK, json={"auction_id": AUCTION, "question": "is any of this vegan?"}
    ).json()["answer"]

    assert "vegan" in answer
    assert "What I can answer from" in answer
    assert "the reliability snapshot" in answer


def test_a_family_of_facts_is_not_answered_with_a_different_member_of_it(app_with):
    """ "Is it in stock?" must not come back with the return policy.

    Both are commitments, so a topic-level match would have answered one with the other.
    ``stock`` names no published key and is not a family word, so the honest answer is that
    nobody published it — even though this slot HAS a commitment.
    """
    client = app_with(StubExchange(shortlist(), recorded()))

    body = client.post(ASK, json={"auction_id": AUCTION, "question": "is it in stock?"}).json()

    assert [row["subject"] for row in body["not_held"]] == ["stock"]
    assert "free returns" not in body["answer"]


def test_an_exchange_that_has_forgotten_the_auction_is_a_404_that_does_not_guess(app_with):
    client = app_with(StubExchange(None, None))

    response = client.post(ASK, json={"auction_id": AUCTION, "question": "what is the price?"})

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "no shortlist for this auction" in detail
    assert "does not say why" in detail


def test_a_service_with_no_exchange_wired_says_so_rather_than_answering(app_with):
    client = app_with(None)

    response = client.post(ASK, json={"auction_id": AUCTION, "question": "what is the price?"})

    assert response.status_code == 503
    assert "no exchange wired" in response.json()["detail"]


def test_a_process_that_never_recorded_the_ranking_says_so(app_with):
    """ "Why did this one come top?" with no recorded row is answered honestly.

    ``ranking_recorded: false`` is on the wire, so the page can tell "we no longer hold the
    ranking" from "the ranking was all zeroes". They are different answers.
    """
    client = app_with(StubExchange(shortlist(), None))

    body = client.post(
        ASK, json={"auction_id": AUCTION, "question": "how were these ranked?"}
    ).json()

    assert body["ranking_recorded"] is False
    assert all(ground["topic"] != "ranking" for ground in body["grounds"])


# ------------------------------------------------------------------------------------------
# 5. honest degradation
# ------------------------------------------------------------------------------------------


def test_with_no_model_configured_the_answer_is_the_deterministic_assembly(app_with):
    client = app_with(StubExchange(shortlist(), recorded()))

    first = client.post(ASK, json={"auction_id": AUCTION, "question": "what is the price?"}).json()
    second = client.post(ASK, json={"auction_id": AUCTION, "question": "what is the price?"}).json()

    assert first["answer_source"] == SOURCE_ASSEMBLED
    # Byte-identical, twice. No clock, no randomness and no socket on this path.
    assert first == second
    assert "72.00 USD" in first["answer"]


def test_the_offline_double_is_refused_rather_than_served_as_prose():
    """D20's default provider answers with a marker, and a marker is not an answer."""
    from llm.doubles import DeterministicLLM

    corpus = corpus_for(shortlist(), recorded=recorded())
    answer = answer_about("what is the price?", corpus, writer=DeterministicLLM())

    assert answer.answer_source == SOURCE_ASSEMBLED
    assert "double:" not in answer.answer


def test_a_writer_that_raises_never_costs_the_shopper_the_answer():
    class Exploding:
        def complete(self, *_args, **_kwargs):
            raise RuntimeError("provider is down")

    corpus = corpus_for(shortlist(), recorded=recorded())
    answer = answer_about("what is the price?", corpus, writer=Exploding())

    assert answer.answer_source == SOURCE_ASSEMBLED
    assert "72.00 USD" in answer.answer


def test_a_written_answer_that_survives_the_screen_is_served_as_written():
    """The screen is not a rejection of everything: honest prose passes it.

    Without this the "written" branch could be dead and every test above would still be
    green, which is how a screen that refuses everything survives a suite.
    """

    class Honest:
        def complete(self, *_args, **_kwargs):
            return (
                "The Merino Beanie at woolworks.example is 72.00 USD and the Acrylic Beanie "
                "at fastfashion.example is 19.00 USD, both held until 2026-09-08."
            )

    corpus = corpus_for(shortlist(), recorded=recorded())
    answer = answer_about("what is the price?", corpus, writer=Honest())

    assert answer.answer_source == SOURCE_WRITTEN, screen_reasons(
        Honest().complete(), read_question("what is the price?", corpus)
    )
    assert "72.00 USD" in answer.answer


def test_a_writer_may_not_reach_past_the_material_the_question_selected():
    """A price question is shown price material, so a return window is an invented number.

    Measured while writing the test above, which is worth keeping: the screen is scoped to
    what the writer was actually HANDED, not to the whole corpus. A reply that reaches for a
    published fact nobody put in its prompt is refused for the same reason as one that
    reaches for a fact nobody published at all — from where the writer sits, they are the
    same move.
    """

    class Reaching:
        def complete(self, *_args, **_kwargs):
            return (
                "The Merino Beanie at woolworks.example is 72.00 USD with a free returns "
                "window of 30 days."
            )

    corpus = corpus_for(shortlist(), recorded=recorded())
    reading = read_question("what is the price?", corpus)

    assert screen_reasons(Reaching().complete(), reading) == ("invented numbers: 30",)
    assert answer_about("what is the price?", corpus, writer=Reaching()).answer_source == (
        SOURCE_ASSEMBLED
    )


def test_a_written_answer_that_invents_a_number_is_refused():
    class Inventing:
        def complete(self, *_args, **_kwargs):
            return "The Merino Beanie at woolworks.example ships in 2 days for 72.00 USD."

    corpus = corpus_for(shortlist(), recorded=recorded())
    reading = read_question("what is the price?", corpus)

    assert any(
        "invented numbers: 2" in reason
        for reason in screen_reasons(Inventing().complete(), reading)
    )
    assert answer_about("what is the price?", corpus, writer=Inventing()).answer_source == (
        SOURCE_ASSEMBLED
    )


def test_a_written_answer_that_affirms_something_unheld_is_refused():
    corpus = corpus_for(shortlist(), recorded=recorded())
    reading = read_question("is any of this third-party tested?", corpus)

    affirming = (
        "Yes, the Merino Beanie at woolworks.example is third-party tested and independently "
        "checked before it ships."
    )

    reasons = screen_reasons(affirming, reading)
    assert "does not open by saying what the platform does not hold" in reasons, reasons


def test_a_written_refusal_that_never_names_the_subject_is_refused():
    corpus = corpus_for(shortlist(), recorded=recorded())
    reading = read_question("is any of this third-party tested?", corpus)

    evasive = "There is nothing further I can add about the Merino Beanie at this time."

    reasons = screen_reasons(evasive, reading)
    assert any("does not name what the platform does not hold" in reason for reason in reasons)


def test_an_honest_refusal_in_the_writers_own_words_passes_the_screen():
    """The silent-on-honest direction of the refusal gate.

    "Testing" rather than "tested", and the phrase not repeated verbatim — a real model's
    wording, and the reply that a stricter equality check refused. It must pass.
    """
    corpus = corpus_for(shortlist(), recorded=recorded())
    reading = read_question("is any of this third-party tested?", corpus)

    honest = (
        "ProxyShop does not hold anything about third-party testing for these options, so I "
        "cannot say whether either of them has been checked that way."
    )

    assert screen_reasons(honest, reading) == ()


# ------------------------------------------------------------------------------------------
# 6. the silent-on-honest direction, for the rest of the gates
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("what is the return policy?", "free returns"),
        ("how much are these?", "72.00 USD"),
        ("how reliable is woolworks.example?", "77%"),
        ("what are the provenance labels?", "store-confirmed"),
        ("did they actually bid or did you scrape them?", "bid in this auction"),
        ("why was this one ranked first?", "rank score"),
        ("tell me about the Merino one", "Merino Beanie"),
    ],
)
def test_a_question_the_corpus_holds_is_answered_and_not_reported_as_unheld(
    app_with, question, expected
):
    client = app_with(StubExchange(shortlist(), recorded()))

    body = client.post(ASK, json={"auction_id": AUCTION, "question": question}).json()

    assert body["grounds"], f"{question!r} produced no grounds: {body['answer']}"
    assert expected in body["answer"], body["answer"]
    assert body["not_held"] == [], f"{question!r} wrongly refused {body['not_held']}"


@pytest.mark.parametrize(
    "question",
    [
        # The measured original, and the spelling that first broke it.
        "is any of this third-party tested?",
        "is any of this third party tested?",
        # The same two with no widener in them, so the ordinal loop is actually REACHED.
        "is this third-party tested?",
        "is this third party tested?",
        # And an ordinal a two-slot shortlist can actually resolve. "third" is index 2 and
        # falls off the end of a shortlist of two, so it could never have narrowed here
        # however broken the rule was; "first party" is the same compound at index 0.
        "is this first party data?",
        "was this first party audited?",
    ],
)
def test_an_ordinal_inside_a_compound_does_not_narrow_the_question_to_one_card(app_with, question):
    """ "third-party" is not the ordinal "third", and neither is "third party".

    Measured while building this: the spaced spelling narrowed the question this whole
    feature was asked for down to the third card, so a shortlist of two answered about one
    shop and the refusal covered one shop instead of the shortlist.

    This test used to pass with the rule deleted, and it is worth saying exactly why, because
    both halves are traps a test of a narrowing rule falls into. Its only question said "is
    ANY of this", and ``any`` is a widener, so ``_selected_slots`` returned every slot before
    the ordinal loop ran at all; and its ordinal was ``third`` against a shortlist of two,
    which resolves out of range and narrows to nothing whatever the guard says. It is
    parametrized over both fixes, and driven against a shortlist where both shops are in the
    network so that ``shop_messages`` counts the slots the answer covered.
    """
    client = app_with(StubExchange(both_in_network(), recorded()))

    body = client.post(ASK, json={"auction_id": AUCTION, "question": question}).json()

    assert body["not_held"], f"{question!r} was answered rather than refused"
    # Both slots are covered, so the refusal is about the shortlist and not one card.
    assert len(body["shop_messages"]) == 2, (
        f"{question!r} narrowed to {len(body['shop_messages'])} card(s): {body['answer']}"
    )
    assert "either" in body["answer"] or "What I can answer from" in body["answer"]


# ------------------------------------------------------------------------------------------
# 7. the phantom refusal — a comparison is the SHAPE of a question, not a gap in the record
# ------------------------------------------------------------------------------------------
#
# The parametrisation above is every phrasing this feature was built against, and every one
# of them is a plain "what is X". Driven on the live stack, EVERY comparative and superlative
# phrasing produced a `not_held` the answer then went on to contradict: "show me every shop's
# return policy" returned all four return windows underneath a bold "We don't know about
# “every”", and "what did each shop promise?" made the platform state, in its own voice, that
# it holds no record of what each shop promised — in the same sentence that listed them.
#
# That is the worst thing this surface can do. It exists to show that the platform's voice
# can be trusted about what it does and does not hold, and a denial its own next clause
# refutes costs more than either the denial or the answer would have alone.


@pytest.mark.parametrize(
    "question",
    [
        # The nine measured on the served route, plus the two the verdict called worst.
        "show me every shop's return policy",
        "what did each shop promise?",
        "which one is better?",
        "what is the difference between these?",
        "compare these for me",
        "which has the lowest price?",
        "which shop has the best reputation?",
        "which store is most trustworthy?",
        "how does each one compare on price?",
        "what is the highest rated shop?",
        "which one is cheapest?",
        "which of these did you scrape?",
    ],
)
def test_a_comparison_is_not_reported_as_something_the_platform_does_not_hold(app_with, question):
    """A widener is not a subject, so it may not come back as one the platform lacks."""
    client = app_with(StubExchange(shortlist(), recorded()))

    body = client.post(ASK, json={"auction_id": AUCTION, "question": question}).json()

    assert body["not_held"] == [], (
        f"{question!r} denied holding {[row['subject'] for row in body['not_held']]} "
        f"and then answered: {body['answer']}"
    )
    assert body["grounds"], f"{question!r} produced no grounds at all: {body['answer']}"
    assert "I don't know about" not in body["answer"], body["answer"]


@pytest.mark.parametrize(
    "question",
    [
        # The single most natural question to ask a ranked list, and the one that made the
        # platform answer "the order here isn't a ranking I can explain" while holding the
        # rank score and all four of its components.
        "why is the first one first?",
        "why did the first one win?",
        "why is the first one the winner?",
    ],
)
def test_a_question_about_the_order_is_answered_from_the_ranking(app_with, question):
    """The correctness of this may not turn on which synonym the shopper reached for.

    "Why did the first one come **top**?" already worked, because ``top`` happens to be in
    ``FAMILY_WORDS``. Every other spelling of the same question fell through to the slot
    introducing itself with its title and its price — an answer to something nobody asked,
    with a sentence in it denying the platform could explain the order.
    """
    client = app_with(StubExchange(shortlist(), recorded()))

    body = client.post(ASK, json={"auction_id": AUCTION, "question": question}).json()

    assert body["not_held"] == [], body["not_held"]
    assert {row["topic"] for row in body["grounds"]} == {"ranking"}, body["answer"]
    assert "rank score" in body["answer"], body["answer"]


@pytest.mark.parametrize(
    ("question", "subject"),
    [
        ("is it in stock?", "stock"),
        ("are these vegan?", "vegan"),
        ("is any of this organic?", "organic"),
        ("what is the shipping weight?", "shipping weight"),
        ("do any of them have a warranty?", "warranty"),
        ("which one ships fastest?", "ships fastest"),
    ],
)
def test_a_subject_nobody_published_is_still_refused_by_name(app_with, question, subject):
    """The other direction of the same rule, and the reason it is not just "stop refusing".

    Widening what counts as answerable is how a refusal gate stops refusing anything. Every
    one of these names something no shop on this shortlist published and the platform's crawl
    did not record, and every one of them has to keep coming back by name.
    """
    client = app_with(StubExchange(shortlist(), recorded()))

    body = client.post(ASK, json={"auction_id": AUCTION, "question": question}).json()

    assert [row["subject"] for row in body["not_held"]] == [subject], body["not_held"]
    assert body["not_held"][0]["detail"] == NOT_HELD_DETAIL
    assert body["answer"].startswith("I don't know about"), body["answer"]


def test_a_family_this_shortlist_holds_nothing_in_is_refused_rather_than_answered_with_another(
    app_with,
):
    """An auction whose recorded ranking has aged out of this service says so.

    ``Corpus.ranking_recorded`` documents this case as one an answer "says so rather than
    guessing" about. It did not: with no ranked rows the slot fell through to introducing
    itself, and "how did you rank these?" came back as a product title and a price, with
    nothing anywhere admitting the question had gone unanswered. A confident answer to a
    question nobody asked is the same failure as a denial over an answer — the platform's
    voice saying something that is not so.
    """
    client = app_with(StubExchange(shortlist(), None))

    body = client.post(
        ASK, json={"auction_id": AUCTION, "question": "how did you rank these?"}
    ).json()

    assert body["ranking_recorded"] is False
    assert body["grounds"] == [], body["grounds"]
    assert "don’t hold" in body["answer"] or "don't hold" in body["answer"], body["answer"]
    assert "ranking" in body["answer"], body["answer"]
    # Not answered with the price instead. The count below is NOT a test of `_catalogue`'s
    # `excluding=`: no slot in this fixture has a ranking row, so the family is absent from
    # `Corpus.topics_held` and the catalogue could not have offered it back however that
    # argument behaved — measured, the assertion stays green with `excluding=` ignored. The
    # gate for that is `test_the_catalogue_does_not_offer_back_the_family_the_same_answer_
    # just_denied`, which narrows to a card with no row on a shortlist that has one.
    assert "72.00" not in body["answer"] and "19.00" not in body["answer"], body["answer"]
    assert body["answer"].count("the ranking and what went into it") == 1, body["answer"]


def test_the_shoppers_own_superlative_is_answerable_only_scoped_to_this_shortlist():
    """``UNSUPPORTABLE_WORDS`` was written for a shop's pitch and refused the platform's answer.

    Measured on the live service: ``a written follow-up answer was refused (unsupportable:
    lowest); serving the assembled floor``, for a question whose literal subject was the
    lowest price and whose four prices the platform published itself. Scoped to this
    shortlist the comparison is a fact about the record; unscoped it is a claim about every
    shop there is, and that half of the rule keeps its teeth.
    """
    reading = read_question(
        "which has the lowest price?", corpus_for(shortlist(), recorded=recorded())
    )

    scoped = (
        "The lowest price of these two is 19.00 USD for the Acrylic Beanie at "
        "fastfashion.example; the Merino Beanie at woolworks.example is 72.00 USD."
    )
    assert screen_reasons(scoped, reading) == ()

    unscoped = "The Acrylic Beanie at fastfashion.example has the lowest price, 19.00 USD."
    assert any("unsupportable: lowest" in reason for reason in screen_reasons(unscoped, reading))


@pytest.mark.parametrize(
    ("question", "reply", "word"),
    [
        # Volunteered rather than asked for: the platform does not reach for a superlative
        # on its own, however carefully it scopes it.
        (
            "what is the return policy?",
            "Of these two, woolworks.example published the best returns policy: a 30 day "
            "window, as its own owner statement.",
            "best",
        ),
        # Asked for, scoped, and still not a comparison any published number settles. A
        # promise about the future is not the same kind of word as "cheapest".
        (
            "is the return window guaranteed?",
            "Of these two, the 30 day window is guaranteed by woolworks.example.",
            "guaranteed",
        ),
    ],
)
def test_an_unsupportable_word_the_record_cannot_settle_is_still_refused(question, reply, word):
    reading = read_question(question, corpus_for(shortlist(), recorded=recorded()))

    reasons = screen_reasons(reply, reading)

    assert any(f"unsupportable: {word}" in reason for reason in reasons), reasons


# ------------------------------------------------------------------------------------------
# 8. the family half of the same rule — a denial above an answer that supplies it
# ------------------------------------------------------------------------------------------
#
# Section 7 fixed the SUBJECT half ("I don't know about “every”" over four return windows)
# and re-created the same defect in the FAMILY half. Mapping best/better/worse/worst to the
# ranking made "which shop has the best reputation?" name a family this shortlist can hold
# nothing in, so the served answer opened "I don't hold the ranking and what went into it for
# the options you asked about" and then recited both reliability snapshots.
#
# Everything below is driven with `record=None`, because that is the state the defect needs
# and it is not exotic: `composition.HttpExchangeClient` keeps its recorded auctions in an
# in-process ring of 64, so a container restart or 64 newer auctions is enough, and
# `Corpus.ranking_recorded` documents the state by name.
#
# Every test in this section was measured GREEN with the mechanism it is about deleted, on
# the suite as it stood before them.


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("which shop has the best reputation?", "reliability: 77%"),
        ("who has the best rating?", "reliability: 77%"),
        ("what is the best return policy?", "free returns: 30 days"),
        ("which one is worse on price?", "price: 72.00 USD"),
    ],
)
def test_a_comparative_asks_about_the_family_beside_it_and_never_about_the_ranking(
    app_with, question, expected
):
    """ "Best" is not a family. It is an operator over the family the question named.

    Each of these names a family the platform holds — trust, commitment, price — and the
    word "best" or "worse" says which way to sort it. Read as a ranking word it made the
    platform deny, first thing and in bold, holding something the shopper had not asked for
    and the answer beneath it did not supply.
    """
    client = app_with(StubExchange(shortlist(), None))

    body = client.post(ASK, json={"auction_id": AUCTION, "question": question}).json()

    assert body["ranking_recorded"] is False
    assert "the ranking and what went into it" not in body["answer"], body["answer"]
    assert expected in body["answer"], body["answer"]
    assert body["not_held"] == [], body["not_held"]


@pytest.mark.parametrize("question", ["which one is better?", "which is the worst?"])
def test_a_bare_comparative_still_asks_the_ranking_and_still_says_it_is_not_held(
    app_with, question
):
    """The other direction, and the reason the four words were not simply deleted.

    With no other family named there is nothing else a comparative can be about, and the
    exchange's published order is the honest thing to answer it from. An auction whose
    recorded rows are gone has to say so rather than introduce the slots instead.
    """
    client = app_with(StubExchange(shortlist(), None))

    body = client.post(ASK, json={"auction_id": AUCTION, "question": question}).json()

    assert body["grounds"] == [], body["grounds"]
    assert "the ranking and what went into it" in body["answer"], body["answer"]
    assert "72.00" not in body["answer"], body["answer"]


def test_the_catalogue_does_not_offer_back_the_family_the_same_answer_just_denied(app_with):
    """A refusal that ends "…and here is the ranking I can answer from" has un-said itself.

    This narrows to the SECOND card, which has no recorded ranking row, on a shortlist whose
    first card does — so the corpus holds the ranking family while the options the shopper
    asked about hold nothing of it. That gap is the only state in which `_catalogue`'s
    `excluding=` can bite, and the earlier test of it had no ranking evidence anywhere, so
    the catalogue could never have offered the family back and the assertion was vacuous.
    """
    corpus = corpus_for(shortlist(), recorded=recorded())
    assert "ranking" in corpus.topics_held, corpus.topics_held

    client = app_with(StubExchange(shortlist(), recorded()))
    body = client.post(
        ASK,
        json={"auction_id": AUCTION, "question": "why is the second one ranked where it is?"},
    ).json()

    assert body["grounds"] == [], body["grounds"]
    assert "What I can answer from" in body["answer"], body["answer"]
    assert body["answer"].count("the ranking and what went into it") == 1, body["answer"]


# ------------------------------------------------------------------------------------------
# the screen's half of rule 3, per family — satisfiable by the platform's own words, and
# still biting on a reply that skates over the denial it was told to deliver
# ------------------------------------------------------------------------------------------

#: A question that names each family, and nothing else. Chosen so that `missing_everywhere`
#: is empty on every one of them, which leaves the family half of the screen alone under test.
FAMILY_QUESTION: dict[str, str] = {
    "identity": "what is the product title here?",
    "price": "what do these cost?",
    "commitment": "what did each shop promise?",
    "trust": "how reliable are these?",
    "provenance": "what are the provenance labels?",
    "ranking": "how were these ranked?",
    "sourcing": "are any of these sponsored?",
}


def market_holding_nothing_in(topic: str) -> tuple[dict, dict | None]:
    """The same two options with one whole family taken away. ``(shortlist, recorded)``.

    Each of these is a market this service really serves: a store that fell back publishes
    no commitment, a shop with no trust history has no snapshot, and an auction older than
    the recorded ring has no ranking rows.
    """
    payload = copy.deepcopy(shortlist())
    record: dict | None = recorded()
    for row in payload["slots"]:
        if topic == "identity":
            row["product"], row["store_domain"] = None, ""
        elif topic == "price":
            row["price"] = None
        elif topic == "commitment":
            row["commitments"] = None
        elif topic == "trust":
            row["trust_summary"] = None
        elif topic == "provenance":
            row["provenance_labels"] = []
        elif topic == "sourcing":
            row["fallback"] = None
    if topic == "ranking":
        record = None
    return payload, record


@pytest.mark.parametrize("topic", TOPICS)
def test_the_platforms_own_floor_is_never_refused_by_the_platforms_own_screen(topic):
    """The false-positive direction, family by family — the invariant its sibling names.

    ``test_pitch_honest_traffic.py::test_the_platforms_own_copy_is_never_refused_by_the_
    platforms_own_screen`` states it: an assembled answer its own screen would reject is a
    screen that will reject the model's honest prose too, and nothing would say so.

    It did reject it. The check asked whether the reply used the topic's INTERNAL name —
    ``commitment``, ``trust``, ``identity``, ``sourcing`` — and the sentence the prompt hands
    the writer is built from ``TOPIC_BLURB``, which carries the topic's own word for only
    three of the seven. On an all-fallback market the floor "I don't hold what each shop
    promised, and how well checked it is…" was refused by "does not name the family the
    platform does not hold: commitment". With ``LLM_PROVIDER=anthropic`` that made the
    written path dead for four families and every such answer dropped silently to the floor.
    """
    payload, record = market_holding_nothing_in(topic)
    reading = read_question(FAMILY_QUESTION[topic], corpus_for(payload, recorded=record))

    assert reading.families_unheld == (topic,), reading.families_unheld
    assert reading.missing_everywhere == (), reading.missing_everywhere
    floor = assemble(reading)

    assert floor.strip(), f"{topic}: assembled to nothing"
    assert screen_reasons(floor, reading) == (), f"{topic}: refused its own floor"


@pytest.mark.parametrize("topic", TOPICS)
def test_a_written_answer_that_skates_over_the_unheld_family_is_still_refused(topic):
    """The positive control for the test above, so "satisfiable" is not "switched off".

    A reply that denies in general and never says WHAT is not held is the failure rule 3
    exists for, and it costs the screen for every family.
    """
    payload, record = market_holding_nothing_in(topic)
    reading = read_question(FAMILY_QUESTION[topic], corpus_for(payload, recorded=record))

    evasive = "I have nothing further to add about either of the two options here."

    assert screen_reasons(evasive, reading) == (
        f"does not name the family the platform does not hold: {topic}",
    )


def test_the_writer_is_handed_the_denial_the_screen_then_holds_it_to():
    """The prompt line and the screen check are the same sentence, or the writer cannot win.

    Both halves in one measurement: the request really carries the NOT HELD instruction, and
    a reply that obeys it in the writer's own words is SERVED rather than discarded. Delete
    the instruction and the writer is judged on a rule it was never told; make the check
    unsatisfiable and obeying it is not enough.
    """

    class Capturing:
        def __init__(self) -> None:
            self.prompt = ""

        def complete(self, prompt, *_args, **_kwargs):
            self.prompt = prompt.text
            return (
                "I don't hold the ranking and what went into it for these two options, so I "
                "cannot say why either came where it did."
            )

    corpus = corpus_for(shortlist(), recorded=None)
    reading = read_question("how were these ranked?", corpus)
    writer = Capturing()
    answer = answer_about("how were these ranked?", corpus, writer=writer)

    assert "NOT HELD" in writer.prompt, writer.prompt
    assert "I don't hold the ranking and what went into it" in writer.prompt, writer.prompt
    assert answer.answer_source == SOURCE_WRITTEN, screen_reasons(
        Capturing().complete(answer_prompt(reading)), reading
    )


# ------------------------------------------------------------------------------------------
# the superlative exception — the scope binds to the WORD, not to the sentence
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "reply", "word"),
    [
        # One leading "Of these," used to license every superlative after it, including one
        # explicitly scoped to the world. All five screened clean and would have been served
        # in the platform's own voice.
        (
            "which one is cheapest?",
            "Of these, the Acrylic Beanie is the cheapest wool hat there is, and the "
            "cheapest you will ever see.",
            "cheapest",
        ),
        (
            "which of these is best on trust?",
            "Of these, the Merino Beanie is the best beanie you can buy, no contest.",
            "best",
        ),
        (
            "which has the lowest price?",
            "Of the two, the Acrylic Beanie has the lowest price in the world.",
            "lowest",
        ),
        (
            "which of these is best on trust?",
            "Among these, the Merino Beanie shop is the best shop there is; no wool seller "
            "comes close.",
            "best",
        ),
        (
            "which one is cheapest?",
            "Between them, the Acrylic Beanie is the cheapest hat on earth.",
            "cheapest",
        ),
    ],
)
def test_a_scope_at_the_head_of_a_sentence_does_not_license_a_boast_at_its_end(
    question, reply, word
):
    reading = read_question(question, corpus_for(shortlist(), recorded=recorded()))

    reasons = screen_reasons(reply, reading)

    assert any(f"unsupportable: {word}" in reason for reason in reasons), reasons


@pytest.mark.parametrize(
    ("question", "reply"),
    [
        # The silent-on-honest direction: the scope sits beside the word, so these are
        # statements about the two prices the platform published and they must be served.
        (
            "which one is cheapest?",
            "Of these two, the cheapest is the Acrylic Beanie at 19.00 USD.",
        ),
        (
            "which one is cheapest?",
            "The Acrylic Beanie at fastfashion.example is the cheapest of these two, at 19.00 USD.",
        ),
        (
            "which has the lowest price?",
            "Between them, the lowest price is 19.00 USD.",
        ),
        (
            "which one is cheapest?",
            "The cheapest on this shortlist is the Acrylic Beanie at 19.00 USD.",
        ),
    ],
)
def test_a_superlative_with_its_scope_beside_it_is_still_served(question, reply):
    reading = read_question(question, corpus_for(shortlist(), recorded=recorded()))

    assert screen_reasons(reply, reading) == ()


@pytest.mark.parametrize(
    ("example", "allowed"),
    [
        ("The cheapest of these is the one at that shop", True),
        ("of these two, the cheapest is that one", True),
        ("It is the cheapest", False),
        ("Of these, the Acrylic Beanie is the cheapest hat there is", False),
    ],
)
def test_rule_five_illustrates_what_the_screen_actually_does(example, allowed):
    """The contract's four worked examples, run through the check they describe.

    The writer is judged on `_settles_a_comparison` and instructed by rule 5, and an example
    in the instruction that the check disagrees with is worse than no example: it teaches the
    writer to produce prose that is then discarded. The first draft of this rule illustrated
    the refusal with "of these, it is the cheapest hat there is" — measured, that one PASSES,
    because the scope is three words from the word. The example below is the measured one.
    """
    reading = read_question("which one is cheapest?", corpus_for(shortlist(), recorded=recorded()))

    settled = _settles_a_comparison("cheapest", example, reading)

    assert settled is allowed, example
    assert example in " ".join(ANSWER_CONTRACT.split()), example


# ------------------------------------------------------------------------------------------
# a trailing ordinal is not always a question about the order
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "subject"),
    [
        ("which one ships first?", "ships"),
        ("which of these arrives first?", "arrives"),
        ("which one ships last?", "ships"),
    ],
)
def test_an_ordinal_that_is_a_predicate_of_a_verb_is_not_a_question_about_the_order(
    app_with, question, subject
):
    """ "Which one ships first?" is about shipping, and the platform holds no shipping order.

    Measured on the served route before this: it came back as a refusal about ``ships`` with
    the entire rank formula printed beside it — a denial of the question the shopper asked,
    next to a confident answer to one they did not. The trailing ordinal names the exchange's
    order only where nothing content-bearing sits on either side of it.
    """
    client = app_with(StubExchange(shortlist(), recorded()))

    body = client.post(ASK, json={"auction_id": AUCTION, "question": question}).json()

    assert [row["subject"] for row in body["not_held"]] == [subject], body["not_held"]
    assert body["grounds"] == [], body["grounds"]
    assert "rank score" not in body["answer"], body["answer"]


def test_the_contract_does_not_forbid_the_number_the_screen_admits():
    """Rule 5 grew a sentence that contradicted rule 2 three lines above it.

    ``Reading.numbers`` deliberately admits the shopper's own numbers — echoing a budget
    they typed asserts nothing about a shop — and rule 2 says so. "Write no number that is
    not below", appended to rule 5, told the writer the opposite of the rule it is printed
    under, and was stricter than the screen it was describing.
    """
    reading = read_question(
        "what costs less than 40?", corpus_for(shortlist(), recorded=recorded())
    )

    echoing = (
        "You asked about 40 dollars: the Acrylic Beanie at fastfashion.example is 19.00 USD "
        "and the Merino Beanie at woolworks.example is 72.00 USD."
    )
    assert screen_reasons(echoing, reading) == ()

    # Line-wrapped rather than raw, so re-adding the sentence with a different fold does not
    # slip past the check.
    contract = " ".join(ANSWER_CONTRACT.split())
    assert "or in the shopper's own question" in contract
    assert "Write no number that is not below" not in contract


# ------------------------------------------------------------------------------------------
# the door itself
# ------------------------------------------------------------------------------------------


def test_an_oversized_question_is_refused_at_the_wire_rather_than_truncated(app_with):
    client = app_with(StubExchange(shortlist(), recorded()))

    response = client.post(ASK, json={"auction_id": AUCTION, "question": "a" * 5000})

    assert response.status_code == 422
    # The refusal does not quote the body back: echoing a hostile document costs this
    # service exactly what accepting it would have.
    assert "aaaa" not in response.text


def test_a_deeply_nested_body_is_a_4xx_and_never_a_500(app_with):
    client = app_with(StubExchange(shortlist(), recorded()))

    hostile = '{"auction_id":' + "[" * 2000 + "]" * 2000 + ',"question":"hi"}'
    response = client.post(
        ASK, content=hostile.encode(), headers={"content-type": "application/json"}
    )

    assert response.status_code in (413, 422), response.status_code
    assert response.status_code != 500


def test_this_route_holds_no_door_that_creates_or_accepts_anything(app_with):
    """Asking a question may not become a purchase, and it is structural rather than promised.

    The stub has neither ``create_auction`` nor ``accept_offer``. A route that grew a call to
    either would raise ``AttributeError`` and answer 500 here.
    """
    exchange = StubExchange(shortlist(), recorded())
    assert not hasattr(exchange, "create_auction")
    assert not hasattr(exchange, "accept_offer")

    client = app_with(exchange)
    assert (
        client.post(ASK, json={"auction_id": AUCTION, "question": "what is the price?"}).status_code
        == 200
    )
