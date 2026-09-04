"""R1's ordering invariant: no auction exists before the buyer confirms (T-071).

The frozen suite checks the two obvious cases — ``confirmed=False`` calls nothing and
``confirmed=True`` calls once. Everything that has ever actually broken this rule lives in
between: a truthy string arriving from a JSON body, a double-clicked confirm button, a
confirmation of an intent nobody clarified, a client that raises halfway through.
"""

from __future__ import annotations

import pytest

from apps.buyer.svc.src.intent import (
    AuctionClientUnusable,
    AuctionCreated,
    ConfirmationLedger,
    ConfirmationWithheld,
    Intent,
    IntentAlreadyConfirmed,
    UnstructuredIntent,
    clarify,
    confirm,
)


class Recorder:
    """A protocol-agnostic client. Every call lands in ``.calls``; nothing else does."""

    def __init__(self, result=None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.result = result if result is not None else {"auction_id": "auc-1"}

    def _bind(self, name):
        def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.result

        return _call

    def __call__(self, *args, **kwargs):
        return self._bind("__call__")(*args, **kwargs)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._bind(name)


class BrokenClient:
    """Accepts the call and then fails, the way a real exchange outage does."""

    def __init__(self) -> None:
        self.calls = 0

    def create_auction(self, payload):
        self.calls += 1
        raise ConnectionError("exchange unreachable")


class NoDoorClient:
    """Exposes nothing that could create an auction."""

    something_else = "nope"


@pytest.fixture()
def intent() -> Intent:
    return clarify(["I want a light roast under $20"]).intent


@pytest.fixture()
def ledger() -> ConfirmationLedger:
    """A private ledger, so these tests never share the process-wide one."""
    return ConfirmationLedger()


# --- withholding -------------------------------------------------------------------


@pytest.mark.parametrize(
    "withheld",
    [False, None, 0, "", [], "false", "no", "0", 1, 2, "true", "yes", object()],
    ids=[
        "False",
        "None",
        "zero",
        "empty-string",
        "empty-list",
        "string-false",
        "string-no",
        "string-zero",
        "int-one",
        "int-two",
        "string-true",
        "string-yes",
        "object",
    ],
)
def test_only_the_boolean_True_confirms(intent, ledger, withheld) -> None:
    """Truthiness is the hole. ``confirmed="false"`` is TRUTHY and must still refuse.

    Five of these thirteen values are truthy in Python — ``"false"``, ``"no"``, ``"0"``,
    ``1``, ``2``, ``"true"``, ``"yes"``, ``object()`` — and a plain ``if confirmed:`` would
    open an auction for every one of them, including for a buyer whose client sent the
    string ``"false"``.
    """
    client = Recorder()
    with pytest.raises(ConfirmationWithheld):
        confirm(intent, client, confirmed=withheld, ledger=ledger)
    assert client.calls == [], f"confirmed={withheld!r} reached the exchange: {client.calls!r}"


def test_withholding_does_not_even_look_the_client_up(intent, ledger) -> None:
    """A client with no usable method still refuses for the RIGHT reason when unconfirmed.

    If the confirmation check ran after the client was resolved, this would raise
    ``AuctionClientUnusable`` and the ordering guarantee would be an accident of which
    error came first.
    """
    with pytest.raises(ConfirmationWithheld):
        confirm(intent, NoDoorClient(), confirmed=False, ledger=ledger)


def test_withholding_leaves_the_intent_confirmable_afterwards(intent, ledger) -> None:
    client = Recorder()
    with pytest.raises(ConfirmationWithheld):
        confirm(intent, client, confirmed=False, ledger=ledger)
    created = confirm(intent, client, confirmed=True, ledger=ledger)
    assert len(client.calls) == 1
    assert created.auction_id == "auc-1"


# --- confirming --------------------------------------------------------------------


def test_a_confirmed_intent_creates_exactly_one_auction(intent, ledger) -> None:
    client = Recorder()
    created = confirm(intent, client, confirmed=True, ledger=ledger)
    assert isinstance(created, AuctionCreated)
    assert len(client.calls) == 1
    name, args, kwargs = client.calls[0]
    assert name == "create_auction"
    assert kwargs == {}
    assert args[0]["intent"]["query"] == intent.query
    assert args[0]["intent"]["budget_band"] == intent.budget_band


def test_the_payload_omits_halves_the_caller_did_not_supply(intent, ledger) -> None:
    client = Recorder()
    confirm(intent, client, confirmed=True, ledger=ledger)
    payload = client.calls[0][1][0]
    assert set(payload) == {"intent"}, (
        "an absent roster and an empty roster mean different things to the exchange"
    )


def test_supplied_halves_reach_the_exchange(intent, ledger) -> None:
    client = Recorder()
    confirm(
        intent,
        client,
        confirmed=True,
        ledger=ledger,
        profile={"pseudonym": "psn-1", "buckets": {}},
        roster=[{"store_id": "s1", "tier": 1, "list_price": 18.0}],
        bid_timeout_seconds=4.5,
    )
    payload = client.calls[0][1][0]
    assert payload["profile"]["pseudonym"] == "psn-1"
    assert payload["roster"][0]["store_id"] == "s1"
    assert payload["bid_timeout_seconds"] == 4.5


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"auction_id": "auc-7"}, "auc-7"),
        ({"auctionId": "auc-8"}, "auc-8"),
        ({"id": "auc-9"}, "auc-9"),
        ("auc-10", "auc-10"),
    ],
)
def test_the_auction_id_is_read_off_whatever_the_exchange_returns(
    intent, ledger, response, expected
) -> None:
    created = confirm(intent, Recorder(result=response), confirmed=True, ledger=ledger)
    assert created.auction_id == expected


def test_a_response_with_no_auction_id_still_returns_the_receipt(intent, ledger) -> None:
    """The auction exists once the call returned; losing the receipt would be worse."""
    created = confirm(intent, Recorder(result={"ok": True}), confirmed=True, ledger=ledger)
    assert created is not None
    assert created.auction_id == ""
    assert created.intent_id == intent.intent_id


# --- one confirmation, one auction --------------------------------------------------


def test_confirming_twice_does_not_open_two_auctions(intent, ledger) -> None:
    client = Recorder()
    confirm(intent, client, confirmed=True, ledger=ledger)
    with pytest.raises(IntentAlreadyConfirmed):
        confirm(intent, client, confirmed=True, ledger=ledger)
    assert len(client.calls) == 1, f"a double click opened {len(client.calls)} auctions"


def test_a_second_client_cannot_re_confirm_the_same_intent(intent, ledger) -> None:
    first, second = Recorder(), Recorder()
    confirm(intent, first, confirmed=True, ledger=ledger)
    with pytest.raises(IntentAlreadyConfirmed):
        confirm(intent, second, confirmed=True, ledger=ledger)
    assert second.calls == []


def test_a_failed_call_releases_the_claim_so_a_retry_still_works(intent, ledger) -> None:
    broken = BrokenClient()
    with pytest.raises(ConnectionError):
        confirm(intent, broken, confirmed=True, ledger=ledger)
    assert broken.calls == 1

    good = Recorder()
    created = confirm(intent, good, confirmed=True, ledger=ledger)
    assert created.auction_id == "auc-1"
    assert len(good.calls) == 1


def test_two_different_intents_may_both_be_confirmed(ledger) -> None:
    client = Recorder()
    for turns in (["a wool scarf"], ["a daypack for commuting"]):
        confirm(clarify(turns).intent, client, confirmed=True, ledger=ledger)
    assert len(client.calls) == 2


# --- confirming something that was never clarified ----------------------------------


@pytest.mark.parametrize(
    "not_an_intent",
    [None, "just a string", 42, [], {}, {"query": "coffee"}, object()],
    ids=["None", "string", "int", "list", "empty-dict", "no-budget-band", "object"],
)
def test_an_intent_that_was_never_clarified_creates_nothing(ledger, not_an_intent) -> None:
    client = Recorder()
    with pytest.raises(UnstructuredIntent):
        confirm(not_an_intent, client, confirmed=True, ledger=ledger)
    assert client.calls == [], f"{not_an_intent!r} reached the exchange"


def test_a_serialized_intent_round_trips_and_confirms(intent, ledger) -> None:
    client = Recorder()
    created = confirm(intent.to_dict(), client, confirmed=True, ledger=ledger)
    assert created.intent_id == intent.intent_id
    assert len(client.calls) == 1


def test_a_serialized_intent_with_an_illegal_op_is_refused(intent, ledger) -> None:
    """R19's closed op set survives the wire; ``lt`` does not become ``lte`` on the way in."""
    payload = intent.to_dict()
    payload["hard_constraints"] = [{"field": "price_usd", "op": "lt", "value": 20}]
    client = Recorder()
    with pytest.raises(Exception) as caught:
        confirm(payload, client, confirmed=True, ledger=ledger)
    assert not isinstance(caught.value, (TypeError, AttributeError, NameError))
    assert client.calls == []


# --- an unusable client -------------------------------------------------------------


def test_a_client_with_no_way_in_is_reported_not_guessed_at(intent, ledger) -> None:
    with pytest.raises(AuctionClientUnusable):
        confirm(intent, NoDoorClient(), confirmed=True, ledger=ledger)


def test_no_client_at_all_is_reported(intent, ledger) -> None:
    with pytest.raises(AuctionClientUnusable):
        confirm(intent, None, confirmed=True, ledger=ledger)


def test_an_unusable_client_does_not_burn_the_confirmation(intent, ledger) -> None:
    with pytest.raises(AuctionClientUnusable):
        confirm(intent, NoDoorClient(), confirmed=True, ledger=ledger)
    client = Recorder()
    assert confirm(intent, client, confirmed=True, ledger=ledger).auction_id == "auc-1"


def test_a_bare_callable_client_is_accepted_as_the_last_resort(intent, ledger) -> None:
    seen: list[dict] = []

    def create(payload):
        seen.append(payload)
        return {"auction_id": "auc-callable"}

    created = confirm(intent, create, confirmed=True, ledger=ledger)
    assert created.auction_id == "auc-callable"
    assert len(seen) == 1


# --- no builtin exception ever escapes a refusal -------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda i, c, l: confirm(i, c, confirmed=False, ledger=l),
        lambda i, c, l: confirm(i, c, confirmed="yes", ledger=l),
        lambda i, c, l: confirm(None, c, confirmed=True, ledger=l),
        lambda i, c, l: confirm(i, NoDoorClient(), confirmed=True, ledger=l),
    ],
    ids=["withheld", "truthy-string", "no-intent", "no-door"],
)
def test_a_refusal_is_never_a_builtin_call_error(intent, ledger, call) -> None:
    """R1's contract: a refusal may raise, but never TypeError/AttributeError/NameError.

    Those four say "the caller wrote the call wrong", and a consumer that cannot tell a
    refusal from a broken call has no way to render either one.
    """
    with pytest.raises(Exception) as caught:
        call(intent, Recorder(), ledger)
    assert not isinstance(
        caught.value, (TypeError, AttributeError, NameError, ImportError)
    ), type(caught.value).__name__
