"""``POST /buyer/chat/ask`` driven against the REAL exchange, on a real socket.

The companion to ``test_chat_ask.py``, which drives the same route against an exchange
double. That file pins what an answer is allowed to be made OF; this one pins that the
material reaches the answer at all — because a shortlist assembled by hand proves nothing
about a field that has to survive a store's bid, the exchange's ranking, a JSON round trip
and this service's own reading of it.

The shape is ``test_auctions_shortlist.py``'s, and its harness is imported rather than
copied: the exchange is ``exchange.main:app`` under a real ``uvicorn`` on 127.0.0.1, the
buyer is ``buyer_svc.main:create_app()`` configured by the two environment variables an
operator sets, and nothing on ``app.state`` is wired by hand. A field asserted below has
travelled from a store's bid to the sentence a shopper reads.

No model is configured here, so every answer is the deterministic assembly. That is the
point: the offline default is the configuration most deployments run, and the answer has to
be correct and byte-stable in it. The live writer is exercised separately, against the demo
market, and its output is quoted in this branch's report.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.buyer.svc.src.chat import NOT_HELD_DETAIL, SOURCE_ASSEMBLED, VOICE_SHOP_CLAIM
from apps.buyer.svc.src.intent import reset_confirmations

# The loopback harness, imported rather than re-copied. These are the same three objects
# `test_auctions_shortlist.py` uses to stand the real exchange up; duplicating them would
# mean two definitions of "a wired exchange" that could drift apart.
from .test_auctions_shortlist import (  # noqa: TID252 - a sibling test module's harness
    INTENT,
    ROSTER,
    Bidders,
    _LoopbackExchange,
    _wired_exchange,
)

ASK = "/buyer/chat/ask"
CONFIRM = "/buyer/intent/confirm"


@pytest.fixture
def exchange():
    server = _LoopbackExchange(_wired_exchange(Bidders()))
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def buyer_client(exchange, monkeypatch):
    """A buyer service configured the way an operator configures one: two variables."""
    from apps.buyer.svc.src.intent.routes import set_buyer_llm
    from apps.buyer.svc.src.pitch import writer as pitch_writer_module

    reset_confirmations()
    set_buyer_llm(None)
    # No model: the assembled floor is what this file measures. Reset the memoised seam too,
    # or a live client built by an earlier test in the same process would answer here.
    monkeypatch.setattr(pitch_writer_module, "_override", None, raising=False)
    monkeypatch.setattr(pitch_writer_module, "_live", None, raising=False)
    monkeypatch.setattr(pitch_writer_module, "_live_built", False, raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("BUYER_DEPLOYMENT", raising=False)
    monkeypatch.delenv("BUYER_DEPLOYMENT_JSON", raising=False)
    monkeypatch.delenv("BUYER_ROSTER", raising=False)
    monkeypatch.setenv("EXCHANGE_URL", exchange.url)
    monkeypatch.setenv("BUYER_ROSTER_JSON", json.dumps(ROSTER))

    app = importlib.import_module("buyer_svc.main").create_app()
    with TestClient(app) as client:
        yield client
    reset_confirmations()


def open_an_auction(client: TestClient) -> str:
    created = client.post(CONFIRM, json={"intent": dict(INTENT), "confirmed": True})
    assert created.status_code == 201, created.text
    return str(created.json()["auction_id"])


def ask(client: TestClient, auction_id: str, question: str) -> dict[str, Any]:
    response = client.post(ASK, json={"auction_id": auction_id, "question": question})
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_a_price_a_shopper_is_answered_with_travelled_from_a_real_bid(buyer_client):
    """The number in the sentence is the one the store bid, not the one the roster listed.

    Every store bids 10% under its listing, so a price that reached the answer out of the
    roster instead of out of the bid would read 80.00 rather than 72.00.
    """
    auction_id = open_an_auction(buyer_client)

    body = ask(buyer_client, auction_id, "how much are these?")

    assert body["answer_source"] == SOURCE_ASSEMBLED
    assert "72.00 USD" in body["answer"], body["answer"]
    assert "80.00" not in body["answer"]
    assert body["not_held"] == []


def test_a_commitment_reaches_the_answer_attributed_to_the_store_that_published_it(buyer_client):
    """R2's COMMITMENTS, through the whole stack, with its provenance still attached.

    ``free_returns: 30 days`` is a claim in ``fixtures/envelopes/store-alpha.approved.json``
    that a merchant review actually signed off. It rides in on the bid, the exchange
    publishes it with its ``owner_statement`` provenance, and the answer says who claimed it.
    """
    auction_id = open_an_auction(buyer_client)

    body = ask(buyer_client, auction_id, "what is the return policy?")

    claims = [ground for ground in body["grounds"] if ground["voice"] == VOICE_SHOP_CLAIM]
    assert claims, body["grounds"]
    assert any(claim["key"] == "free returns" for claim in claims)
    for claim in claims:
        assert claim["store_domain"] in claim["attribution"]
        assert "owner statement" in claim["attribution"]
    assert "published this" in body["answer"]


def test_the_trust_snapshot_the_exchange_served_is_what_the_answer_quotes(buyer_client):
    """R15: the served snapshot is final, and this route never recomputes it.

    ``demo-woolworks`` is wired at 0.81 with confidence 0.7, so the answer must read 81% and
    70% — numbers that exist nowhere in this service.
    """
    auction_id = open_an_auction(buyer_client)

    body = ask(buyer_client, auction_id, "how reliable are these shops?")

    assert "81%" in body["answer"], body["answer"]
    assert "70%" in body["answer"], body["answer"]


def test_the_published_ranking_components_are_answerable_and_are_not_recomputed(buyer_client):
    """ "Why is this one first?" is answered from the exchange's own published row."""
    auction_id = open_an_auction(buyer_client)

    body = ask(buyer_client, auction_id, "how were these ranked?")

    assert body["ranking_recorded"] is True
    ranking = [ground for ground in body["grounds"] if ground["topic"] == "ranking"]
    assert ranking, body["grounds"]
    for ground in ranking:
        assert ground["attribution"] == (
            "the exchange's published ranking, recorded when this auction ran"
        )


def test_the_refusal_survives_the_whole_stack(buyer_client):
    """The question this feature was asked for, end to end, against the real exchange."""
    auction_id = open_an_auction(buyer_client)

    body = ask(buyer_client, auction_id, "which of these is actually third-party tested?")

    assert body["grounds"] == []
    assert body["not_held"] == [{"subject": "third-party tested", "detail": NOT_HELD_DETAIL}]
    assert "third-party tested" in body["answer"]
    # It says what it would have needed, and it does not guess from the labels it DOES hold.
    assert NOT_HELD_DETAIL in body["answer"]
    assert "store-confirmed" not in body["answer"]


@pytest.mark.parametrize(
    "question",
    [
        "show me every shop's return policy",
        "what did each shop promise?",
        "which one is better?",
        "which has the lowest price?",
        "which shop has the best reputation?",
        "how does each one compare on price?",
        "why is the first one first?",
    ],
)
def test_a_comparison_survives_the_whole_stack_without_a_phantom_refusal(buyer_client, question):
    """The other half of ``test_the_refusal_survives_the_whole_stack``, and it did not.

    Measured on the live stack before this: every one of these came back carrying a
    ``not_held`` the answer underneath then contradicted — "show me every shop's return
    policy" returned all four return windows under a bold "We don't know about “every”", and
    "what did each shop promise?" made the platform say it holds no record of what each shop
    promised in the same sentence that listed them.

    Driven here rather than only against the double because the material has to survive a
    store's bid, the exchange's ranking and this service's reading of it: a comparison that
    resolves against a hand-built corpus proves nothing about one resolved against a real
    shortlist, where the shop domains and crawled titles are different words.
    """
    auction_id = open_an_auction(buyer_client)

    body = ask(buyer_client, auction_id, question)

    assert body["not_held"] == [], (
        f"{question!r} denied holding {[row['subject'] for row in body['not_held']]} "
        f"and then answered: {body['answer']}"
    )
    assert body["grounds"], f"{question!r} produced no grounds: {body['answer']}"
    assert "I don't know about" not in body["answer"], body["answer"]


def test_the_order_of_a_real_shortlist_is_explained_from_its_own_published_ranking(buyer_client):
    """ "Why is the first one first?" — the most natural question to ask a ranked list.

    It answered "the order here isn't a ranking I can explain" while the same auction, asked
    "how did you rank these?", returned twelve ranking rows. The deictic ``first`` selected
    card one and the predicate ``first`` was stripped as an ordinal, so nothing survived to
    name the ranking family and the slot introduced itself with its title and its price
    instead. The platform denied, in its own voice, holding the ranking it had published.
    """
    auction_id = open_an_auction(buyer_client)

    body = ask(buyer_client, auction_id, "why is the first one first?")

    assert body["ranking_recorded"] is True
    assert body["not_held"] == []
    assert {ground["topic"] for ground in body["grounds"]} == {"ranking"}, body["answer"]
    assert "rank score" in body["answer"], body["answer"]
    # One card's ranking, not the whole shortlist's: the shopper pointed at a card.
    assert len({ground["bid_ref"] for ground in body["grounds"]}) == 1, body["grounds"]


def test_two_identical_questions_answer_byte_identically(buyer_client):
    """Rule 1: no clock, no environment read, no randomness and no socket on this path."""
    auction_id = open_an_auction(buyer_client)

    first = ask(buyer_client, auction_id, "what is the return policy?")
    second = ask(buyer_client, auction_id, "what is the return policy?")

    assert first == second


def test_an_auction_this_exchange_never_ran_is_a_404(buyer_client):
    response = buyer_client.post(
        ASK, json={"auction_id": "auction-nobody-opened", "question": "how much is it?"}
    )

    assert response.status_code == 404
    assert "no shortlist for this auction" in response.json()["detail"]
