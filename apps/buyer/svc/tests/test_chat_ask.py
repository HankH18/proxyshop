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
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.buyer.svc.src.chat import (
    NOT_HELD_DETAIL,
    SOURCE_ASSEMBLED,
    SOURCE_WRITTEN,
    VOICE_PLATFORM,
    VOICE_SHOP_CLAIM,
    answer_about,
    corpus_for,
    read_question,
    screen_reasons,
)

ASK = "/buyer/chat/ask"
AUCTION = "auction-chat-1"

#: A store that joined the network: its own advocate wrote this and it rode in on the bid.
SHOP_PITCH = (
    "We have knitted these in Yorkshire since 1974 and we will take it back for any reason "
    "inside a month, no questions asked and no restocking fee."
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


def test_a_hyphenated_ordinal_does_not_narrow_the_question_to_one_card(app_with):
    """ "third-party" is not the ordinal "third", and neither is "third party".

    Measured while building this: the spaced spelling narrowed the question this whole
    feature was asked for down to the third card, so a shortlist of two answered about one
    shop and the refusal covered one shop instead of the shortlist.
    """
    client = app_with(StubExchange(shortlist(), recorded()))

    for spelling in ("third-party tested", "third party tested"):
        body = client.post(
            ASK, json={"auction_id": AUCTION, "question": f"is any of this {spelling}?"}
        ).json()
        assert body["not_held"], spelling
        # Both slots are covered, so the refusal is about the shortlist and not one card.
        assert len(body["shop_messages"]) == 1  # only one slot HAS a message
        assert "either" in body["answer"] or "What I can answer from" in body["answer"]


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
