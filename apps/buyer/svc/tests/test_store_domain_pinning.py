"""The store domain reaches the buyer, and the redirect guard it anchors actually fires.

Gap B, driven through the served routes rather than the functions behind them.

**What was wrong.** ``ShortlistSlot`` declared no ``store_domain``, so the exchange had
nowhere to publish the one value the buyer's redirect guard can pin a checkout permalink's
host against, and the buyer defaulted the field to ``""``. Measured on a devstack run: every
rendered slot carried ``store_domain: ""``, the page's host cross-check was skipped because
the value was falsy, and the accept sent ``expected_domain: null``. The guarantee
``accept()`` documents — *the exchange's permalink host must equal the store's registered
domain, exactly* — could not fire on any deployment, and nothing said so.

**What is asserted here.** Three things, each through a real request:

1. a slot that carries a domain renders with it, and a slot that carries none renders
   ``null`` — **never** ``""``, because ``""`` is the spelling that made the skip invisible;
2. an accept whose slot names a domain REFUSES an off-domain permalink (502) and reports the
   domain it pinned on the accepts it allows;
3. an accept with nothing to pin still completes, says so in ``pinned_to_domain: null``, and
   logs a WARNING — the deliberate choice not to refuse, so that a deployment whose exchange
   has no registry keeps working while its operator is told.

The exchange half — populating the field from the platform's registry — landed in
``exchange.ranking.serving._with_offer_fields`` and is graded by
``packages/contracts/tests/test_repro_open_tickets.py::test_the_published_shortlist_slot_carries_the_stores_registered_domain``
and its unregistered-store twin. Measured on the SERVED door once both halves were in:
``GET /auctions/{auction_id}/shortlist`` answers 200 with
``{"slot": "fit", "bid_ref": "…:store-a", "store_domain": "store-a.example.com"}``, and the
``POST /auctions`` body is byte-identical to it. The guarantee is reachable by a real request
for the first time.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.buyer.svc.src.accept import reset_accepted

RENDER = "/buyer/shortlist/render"
ACCEPT = "/buyer/shortlist/accept"

STORE_DOMAIN = "store-one.example.com"
ON_DOMAIN_PERMALINK = f"https://{STORE_DOMAIN}/cart/44352913:1?discount=PS-ABC123"
OFF_DOMAIN_PERMALINK = "https://attacker.example/cart/44352913:1?discount=PS-ABC123"


def _slot(**overrides: Any) -> dict[str, Any]:
    """One shortlist slot as the exchange serializes it, with a decoy ``checkout_url``.

    The decoy is the point of R3: a slot's own URL is authority for nothing, so it must never
    become the expected domain — a spoofed slot would then certify its own spoofed permalink.
    """
    slot = {
        "slot": "fit",
        "bid_ref": "bid-domain-1",
        "auction_id": "auc-domain-1",
        "fit_score": 0.91,
        "trust_summary": {"score": 0.72, "confidence": 0.4},
        "provenance_labels": ["store_confirmed"],
        "store_domain": STORE_DOMAIN,
        "checkout_url": "https://attacker.example/cart/1:1?discount=PS-ABC123",
    }
    slot.update(overrides)
    return slot


class RecordingExchange:
    def __init__(self, permalink: str = ON_DOMAIN_PERMALINK) -> None:
        self.calls: list[dict[str, Any]] = []
        self.permalink = permalink

    def accept_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        return {"permalink_url": self.permalink}


def _client(exchange: RecordingExchange) -> Any:
    reset_accepted()
    main = importlib.import_module("buyer_svc.main")
    app = main.create_app()
    app.state.exchange_client = exchange
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_ledger() -> Any:
    reset_accepted()
    yield
    reset_accepted()


# --- 1. the render route publishes the domain, and publishes ABSENCE as null ------------------


def test_the_rendered_slot_carries_the_store_domain_the_exchange_published() -> None:
    with _client(RecordingExchange()) as client:
        body = client.post(
            RENDER,
            json={"shortlist": {"auction_id": "auc-domain-1", "slots": [_slot()]}},
        )

    assert body.status_code == 200, body.text
    rendered = body.json()["slots"][0]
    assert rendered["store_domain"] == STORE_DOMAIN, (
        "the exchange published a registered domain for this store and the buyer dropped it; "
        "the page has nothing to pin the checkout host against"
    )


@pytest.mark.parametrize(
    "absent",
    [
        pytest.param({}, id="key-absent"),
        pytest.param({"store_domain": None}, id="explicit-null"),
        pytest.param({"store_domain": ""}, id="legacy-empty-string"),
        pytest.param({"store_domain": "   "}, id="blank"),
    ],
)
def test_a_slot_with_no_domain_renders_null_and_never_the_empty_string(
    absent: dict[str, Any],
) -> None:
    """All three spellings of absence collapse to one, and it is ``null``.

    ``""`` is the one answer that must not survive. It is a string, so the only thing
    separating it from a real host is a truthiness check every caller has to remember — and
    the caller that forgot silently got "no host constraint" rather than a refusal. ``null``
    cannot be mistaken for a domain by a template, a comparison, or a type checker.
    """
    slot = _slot()
    slot.pop("store_domain")
    slot.update(absent)

    with _client(RecordingExchange()) as client:
        body = client.post(
            RENDER, json={"shortlist": {"auction_id": "auc-domain-1", "slots": [slot]}}
        )

    assert body.status_code == 200, body.text
    rendered = body.json()["slots"][0]
    assert rendered["store_domain"] is None, (
        f"a slot with no registered domain rendered {rendered['store_domain']!r}; only null "
        "is a spelling of absence a consumer cannot read as a host"
    )


def test_the_rendered_slot_never_carries_the_slots_own_checkout_url() -> None:
    """The decoy control. If the domain were derived from the URL, R3 would be broken here."""
    with _client(RecordingExchange()) as client:
        body = client.post(
            RENDER,
            json={
                "shortlist": {
                    "auction_id": "auc-domain-1",
                    "slots": [_slot(store_domain=None)],
                }
            },
        )

    assert "attacker.example" not in body.text


# --- 2. the guarantee fires --------------------------------------------------------------


def test_an_off_domain_permalink_is_refused_when_the_slot_names_a_domain() -> None:
    """The whole point of publishing the field: a permalink that leaves the store is a 502.

    Before ``store_domain`` was on the contract this could not happen through the served
    route at all — the slot carried no domain, so the comparison ran against ``""`` and every
    host passed.
    """
    exchange = RecordingExchange(permalink=OFF_DOMAIN_PERMALINK)
    with _client(exchange) as client:
        response = client.post(ACCEPT, json={"slot": _slot()})

    assert response.status_code == 502, response.text
    assert "attacker.example" in response.json()["detail"]
    assert exchange.calls, "the exchange was never asked, so this refusal proves nothing"


def test_an_on_domain_permalink_is_allowed_and_the_response_names_what_it_pinned() -> None:
    """Positive control: a route that refused everything would satisfy the test above."""
    with _client(RecordingExchange()) as client:
        response = client.post(ACCEPT, json={"slot": _slot()})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["permalink_url"] == ON_DOMAIN_PERMALINK
    assert body["pinned_to_domain"] == STORE_DOMAIN, (
        "the accept succeeded but does not say the host was pinned, so a caller cannot tell "
        "a checked redirect from an unchecked one"
    )


def test_the_expected_domain_the_caller_sends_wins_over_the_slots_own() -> None:
    """A caller that knows better than the slot is still obeyed, and a spoofed slot loses."""
    exchange = RecordingExchange(permalink=ON_DOMAIN_PERMALINK)
    with _client(exchange) as client:
        response = client.post(
            ACCEPT,
            json={"slot": _slot(store_domain="attacker.example"), "expected_domain": STORE_DOMAIN},
        )

    assert response.status_code == 200, response.text
    assert response.json()["pinned_to_domain"] == STORE_DOMAIN


# --- 3. an accept with nothing to pin: loud, not refused ----------------------------------


def test_an_accept_with_no_domain_completes_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Loud, and deliberately not a refusal.

    An exchange with no ``store_id -> domain`` registry configured publishes
    ``store_domain: null`` on every slot. Refusing here would take checkout away from every
    buyer on that deployment rather than telling anyone the registry is missing — and it
    would break the shipped browser client, which sends ``expected_domain: null`` when it has
    no domain to send. So the accept completes, and the two things a consumer can act on both
    say what happened: ``pinned_to_domain: null`` on the wire, and a WARNING in the log.
    """
    with caplog.at_level(logging.WARNING, logger="buyer_svc.accept.handoff"):
        with _client(RecordingExchange(permalink=OFF_DOMAIN_PERMALINK)) as client:
            response = client.post(ACCEPT, json={"slot": _slot(store_domain=None)})

    assert response.status_code == 200, response.text
    assert response.json()["pinned_to_domain"] is None, (
        "an unpinned accept must publish that it was unpinned; a caller that cannot tell is "
        "back where this ticket started"
    )

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "expected store domain" in record.getMessage()
    ]
    assert warnings, (
        "an accept whose checkout host was pinned to nothing was completed in silence. That "
        "silence is the defect: it is how the skip went unnoticed on every slot of every "
        f"deployment. Records seen: {[r.getMessage()[:80] for r in caplog.records]}"
    )
    assert "auc-domain-1" in warnings[0].getMessage(), (
        "the warning does not name the auction, so an operator cannot act on it"
    )


def test_an_explicitly_empty_expected_domain_is_still_the_callers_own_choice() -> None:
    """``expected_domain: ""`` means "I am deliberately not constraining the host".

    Kept distinct from ``null`` on purpose. ``null`` means "use the slot's"; ``""`` is an
    explicit opt-out a caller had to type. Collapsing them would have made the opt-out the
    default, which is the shape of the original defect.
    """
    with _client(RecordingExchange(permalink=OFF_DOMAIN_PERMALINK)) as client:
        response = client.post(ACCEPT, json={"slot": _slot(), "expected_domain": ""})

    assert response.status_code == 200, response.text
    assert response.json()["pinned_to_domain"] is None
