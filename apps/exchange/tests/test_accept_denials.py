"""``denial_reason`` is a contract, not a log line (T-204, T-264).

The field is written into the ``policy_event`` payload ``_refusal_event`` persists AND is the
body of the 409 the published contract declares for ``POST /auctions/{auction_id}/accept``.
Its vocabulary used to be whatever ``type(exc).__name__`` happened to be: six reachable
leading tokens, nothing in the repo asserting on the set, and one value that rendered a
process **memory address** into that persisted, client-visible payload.

This file pins the two properties that make it a contract:

1. every reason the package can emit begins with a code from ``DENIAL_REASONS`` — and the
   test drives the branches rather than reading the source, so a new refusal that invents a
   seventh token fails here;
2. no reason ever renders an object's default ``repr``, whatever object a caller hands in.

The prose after the code is deliberately NOT pinned. It names the auction, the bid, the
refused host and the exception class, and it is what makes a refusal investigable; freezing
it would turn every improved diagnostic into a test failure. Only the token is the contract.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from exchange.accept import (
    DENIAL_ALREADY_ACCEPTED,
    DENIAL_AUCTION_NOT_ACCEPTABLE,
    DENIAL_BLACKLISTED,
    DENIAL_CHECKOUT_REFUSED,
    DENIAL_REASONS,
    DENIAL_UNAVAILABLE,
    DENIAL_UNKNOWN_BID,
    DENIAL_UNRECORDABLE_ACCEPTANCE,
    DENIAL_UNSPECIFIED,
    accept,
    accept_offer,
    denial_code,
    denial_reason,
    use_registered_domains,
)
from exchange.accept.reasons import describe
from exchange.accept.routes import _denied
from exchange.eligibility import (
    BLACKLISTED,
    SELLER_ELIGIBILITY_INTERFACE_VERSION,
    EligibilityDecision,
    SellerEligibility,
    StaticSellerEligibility,
)

SELLER_DOMAIN = "store-a.example.com"

#: A default ``__repr__`` — ``<Foo object at 0x104e0a170>``. The address is what T-264 is.
MEMORY_ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")


@pytest.fixture
def unwired() -> Iterator[None]:
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


def auction() -> dict[str, Any]:
    return {
        "auction_id": "auction-1",
        "bids": [
            {
                "bid_id": "bid-a",
                "store_id": "store-a",
                "store_domain": SELLER_DOMAIN,
                "offer": {
                    "product_ref": "product-1",
                    "unit_price": 100.0,
                    "total_price": 100.0,
                    "checkout_url": f"https://{SELLER_DOMAIN}/cart/1:1",
                    "expires_at": 2_000_000_000.0,
                },
            },
            {
                "bid_id": "bid-b",
                "store_id": "store-b",
                "store_domain": "store-b.example.com",
                "offer": {
                    "product_ref": "product-1",
                    "unit_price": 110.0,
                    "total_price": 110.0,
                    "checkout_url": "https://store-b.example.com/cart/1:1",
                    "expires_at": 2_000_000_000.0,
                },
            },
        ],
        "accepted_bid_ref": None,
        "now": 1_700_000_000.0,
    }


class Creator:
    def __init__(self, *, explode: bool = False) -> None:
        self.explode = explode
        self.calls: list[str] = []

    def create_code(self, store_id: Any, offer: Any) -> dict[str, Any]:
        self.calls.append(str(store_id))
        if self.explode:
            raise RuntimeError("merchant POST /codes is down")
        base = str(dict(offer).get("checkout_url") or f"https://{SELLER_DOMAIN}/cart/1:1")
        return {"code": "PSX-TESTCODE", "permalink_url": f"{base}?discount=PSX-TESTCODE"}

    __call__ = create_code


class Eligibility(SellerEligibility):
    """A source that answers whatever it was handed, including a hostile ``reason``."""

    interface_version = SELLER_ELIGIBILITY_INTERFACE_VERSION

    def __init__(self, status: str, reason: str = "") -> None:
        self._status = status
        self._reason = reason

    def check(self, store_id: str) -> EligibilityDecision:
        return EligibilityDecision(store_id=store_id, status=self._status, reason=self._reason)


def unaccepted() -> Any:
    """An object that cannot be stamped — the ``unrecordable_acceptance`` branch."""

    class Frozen:
        __slots__ = ("auction_id", "bids", "now")

        def __init__(self) -> None:
            self.auction_id = "auction-1"
            self.bids = auction()["bids"]
            self.now = 1_700_000_000.0

    return Frozen()


class _RouteRefusal:
    """Just enough of an ``AcceptResult`` to carry the served route's own refusal.

    ``auction_not_acceptable`` is emitted by ``routes.py`` rather than by ``accept()``, and
    the sweep below covers every code in the vocabulary, so it is reached the way the route
    reaches it: through ``_denied``, which is also what normalises the published token.
    """

    accepted = False

    def __init__(self) -> None:
        body = json.loads(
            _denied(
                denial_reason(
                    DENIAL_AUCTION_NOT_ACCEPTABLE,
                    "auction 'auction-1' is 'open', from which 'accepted' is not a legal move",
                )
            ).body
        )
        self.denial_reason = body["denial_reason"]
        self.events = ()


def _route_refusal() -> Any:
    return _RouteRefusal()


# =====================================================================================
# The vocabulary itself
# =====================================================================================
def test_every_declared_code_is_a_bare_lowercase_token() -> None:
    """A code carrying the separator would split into something the vocabulary never lists."""
    for code in DENIAL_REASONS:
        assert code and code == code.strip().lower()
        assert ":" not in code, f"{code!r} would not survive its own round trip"


def test_a_reason_round_trips_through_its_declared_code() -> None:
    built = denial_reason(DENIAL_CHECKOUT_REFUSED, "RuntimeError: the merchant said no")
    assert built == "checkout_refused: RuntimeError: the merchant said no"
    assert denial_code(built) == DENIAL_CHECKOUT_REFUSED


def test_an_undeclared_code_is_refused_rather_than_quietly_published() -> None:
    with pytest.raises(ValueError, match="not a declared denial reason"):
        denial_reason("teapot", "the exchange is a teapot")
    assert denial_code("teapot: the exchange is a teapot") is None
    assert denial_code("") is None
    assert denial_code(None) is None


# =====================================================================================
# Every branch that can refuse, driven — not read off the source
# =====================================================================================
def test_every_reason_accept_can_emit_names_a_declared_code(unwired: None) -> None:
    """The assertion T-204 asks for, over every refusal branch this package has."""
    already = auction()
    already["accepted_bid_ref"] = "bid-a"

    emitted = {
        DENIAL_UNKNOWN_BID: accept(auction(), "bid-nowhere", Creator(), "shopify"),
        DENIAL_ALREADY_ACCEPTED: accept(already, "bid-b", Creator(), "shopify"),
        DENIAL_UNRECORDABLE_ACCEPTANCE: accept(unaccepted(), "bid-a", Creator(), "shopify"),
        DENIAL_CHECKOUT_REFUSED: accept(auction(), "bid-a", Creator(explode=True), "shopify"),
        DENIAL_BLACKLISTED: accept_offer(
            auction=auction(),
            bid_ref="bid-a",
            code_creator=Creator(),
            mode="shopify",
            eligibility=Eligibility(BLACKLISTED),
        ),
        # An exchange with no eligibility source at all: "nobody wired one" is not evidence
        # of innocence, and the gate refuses before the checkout port is reached.
        DENIAL_UNAVAILABLE: accept_offer(
            auction=auction(),
            bid_ref="bid-a",
            code_creator=Creator(),
            mode="shopify",
            eligibility=None,
        ),
        DENIAL_AUCTION_NOT_ACCEPTABLE: _route_refusal(),
    }

    assert set(emitted) == set(DENIAL_REASONS) - {DENIAL_UNSPECIFIED}, (
        "this sweep no longer drives every declared code — a vocabulary term with no branch "
        "behind it, or a branch with no term, is the T-204 defect coming back"
    )

    for expected, result in emitted.items():
        assert result.accepted is False, f"{expected}: this branch did not refuse"
        assert denial_code(result.denial_reason) == expected, (
            f"expected a {expected!r} refusal, got {result.denial_reason!r}"
        )
        # ...and it is the same string the persisted policy_event carries, where there is
        # one (the served route's own refusal happens before any event is built).
        events = [e["payload"] for e in result.events if e["kind"] == "policy_event"]
        assert not events or events[0]["reason"] == result.denial_reason


def test_an_eligibility_source_whose_prose_omits_a_colon_still_names_a_code(
    unwired: None,
) -> None:
    """The hole the old case-insensitive ``startswith`` left open.

    ``"Blacklisted for chargeback fraud"`` starts with the status word, so it used to be
    passed through untouched — which put the whole sentence where the vocabulary term
    belongs, because nothing after it was a colon.
    """
    result = accept_offer(
        auction=auction(),
        bid_ref="bid-a",
        code_creator=Creator(),
        mode="shopify",
        eligibility=Eligibility(BLACKLISTED, "Blacklisted for chargeback fraud"),
    )
    assert result.accepted is False
    assert denial_code(result.denial_reason) == DENIAL_BLACKLISTED
    assert "chargeback fraud" in (result.denial_reason or ""), "the source's own words survived"


def test_a_status_this_exchange_does_not_speak_degrades_to_unavailable(unwired: None) -> None:
    result = accept_offer(
        auction=auction(),
        bid_ref="bid-a",
        code_creator=Creator(),
        mode="shopify",
        eligibility=Eligibility("on_probation", "the store is on probation"),
    )
    assert result.accepted is False
    assert denial_code(result.denial_reason) == DENIAL_UNAVAILABLE


# =====================================================================================
# T-264 — a memory address in a persisted, client-visible payload
# =====================================================================================
def test_an_unusable_registry_does_not_render_a_memory_address(unwired: None) -> None:
    """The measured defect: ``registered_domains <object object at 0x104e0a170> exposes …``.

    That string was formatted into ``denial_reason``, persisted into the ``policy_event`` and
    returned to an unauthenticated caller — a process memory-layout leak, and a value that
    rendered differently on every run so nothing downstream could group two of them.
    """
    result = accept(auction(), "bid-a", Creator(), "shopify", registered_domains=object())

    assert result.accepted is False
    reason = result.denial_reason or ""
    assert not MEMORY_ADDRESS.search(reason), f"a memory address reached denial_reason: {reason!r}"
    assert "object at" not in reason
    assert "TypeError" in reason and "domain_for(store_id)" in reason, (
        f"the diagnosis was thrown away with the address: {reason!r}"
    )
    payload = [e["payload"] for e in result.events if e["kind"] == "policy_event"][0]
    assert not MEMORY_ADDRESS.search(str(payload["reason"]))


def test_an_eligibility_source_declaring_an_object_version_leaks_no_address(
    unwired: None,
) -> None:
    """The same shape one file over: ``interface_version`` is whatever the source put there."""

    class OpaqueVersion:
        interface_version = object()

    result = accept_offer(
        auction=auction(),
        bid_ref="bid-a",
        code_creator=Creator(),
        mode="shopify",
        eligibility=OpaqueVersion(),
    )
    assert result.accepted is False
    reason = result.denial_reason or ""
    assert denial_code(reason) == DENIAL_UNAVAILABLE
    assert not MEMORY_ADDRESS.search(reason), f"a memory address reached denial_reason: {reason!r}"


def test_describe_keeps_the_reprs_that_carry_information() -> None:
    """Naming everything by its type would be safe and useless."""
    assert describe("1.0.0") == "'1.0.0'"
    assert describe(None) == "None"
    assert describe(7) == "7"
    assert describe(object()) == "<object>"
    assert describe(StaticSellerEligibility()) == "<StaticSellerEligibility>"


# =====================================================================================
# The published boundary normalises, so a client never sees an undeclared token
# =====================================================================================
def test_the_route_republishes_an_undeclared_reason_under_unspecified() -> None:
    """A future refusal that invents a token cannot reach a client as one."""
    body = json.loads(_denied("teapot: something nobody declared").body)
    assert body == {
        "accepted": False,
        "denial_reason": f"{DENIAL_UNSPECIFIED}: teapot: something nobody declared",
    }
    assert json.loads(_denied("").body)["denial_reason"].startswith(DENIAL_UNSPECIFIED)


def test_the_route_leaves_a_declared_reason_exactly_as_it_found_it() -> None:
    """The positive control: normalising everything would satisfy the test above."""
    declared = denial_reason(DENIAL_AUCTION_NOT_ACCEPTABLE, "auction 'a-1' is 'open'")
    assert json.loads(_denied(declared).body)["denial_reason"] == declared
