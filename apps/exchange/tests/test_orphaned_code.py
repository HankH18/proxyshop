"""The gate for T-202 and T-215 — the orphaned discount code is RECORDED, and never LEAKED.

Both halves are one code path: a merchant that answers with an off-domain permalink has
already had ``POST /codes`` issue a real single-use discount, and the port refuses *after*
the mint. Two separate things must then be true, and neither was:

**T-202 — the code must be carried out and recorded.** The port carries it on
``OrphanedCheckoutCode.orphan``. Before this ticket the accept handler caught a bare
``except Exception`` and dropped it, so the code was live at the merchant with no
``code_created`` event anywhere — nothing to revoke it by and nothing for reconciliation to
notice. The original T-157 defect had simply moved one frame up the stack.

**T-215 — the code must not reach the persisted refusal event.** The denial reason is built
as ``f"checkout_refused: {type(exc).__name__}: {exc}"`` and is written into a
``policy_event`` payload that the published OpenAPI types as a bare string. The exception's
message embedded the code verbatim — twice over, once as ``{minted.code!r}`` and again
inside the off-domain reason's ``url=`` (a cart permalink carries ``?discount=<code>``) — so
a code this layer cannot revoke was being logged where anything reading the event stream
could read it.

The two pull against each other, which is why they are gated together: "delete the message"
satisfies T-215 and destroys the diagnostic T-202 needs. What is asserted here is the
division of labour — the *real* code goes to the ``code_created`` ledger event, which is the
revocable path and the kind whose published shape exists to hold it; the refusal's prose
carries only a non-reversible fingerprint that joins the two.
"""

from __future__ import annotations

import json
import traceback
from collections.abc import Iterator
from typing import Any

import pytest
from exchange.accept import accept, use_registered_domains
from exchange.checkout import (
    CheckoutRequest,
    OrphanedCheckoutCode,
    StaticRegisteredDomains,
    resolve_provider,
)

SELLER_DOMAIN = "store-a.example.com"
RIVAL_DOMAIN = "attacker.tld"
T_NOW = 1_700_000_000.0
T_FUTURE = 2_000_000_000.0

#: Deliberately distinctive: every assertion below is a substring search, and a code that
#: could occur incidentally in a URL or a class name would make them unfalsifiable.
ORPHAN_CODE = "PSX-ORPHAN-7QZK4M"


# =====================================================================================
# Doubles
# =====================================================================================
class OffDomainMerchant:
    """A merchant whose ``POST /codes`` succeeds and answers on somebody else's host.

    This is the shape that produces an orphan: the code is REAL (it exists in the
    merchant's system the moment this returns) and the permalink cannot be honoured.
    """

    def __init__(self, code: str = ORPHAN_CODE) -> None:
        self.code = code
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_code(self, store_id: Any, offer: Any) -> dict[str, str]:
        self.calls.append((str(store_id), dict(offer)))
        return {
            "code": self.code,
            "permalink_url": f"https://{RIVAL_DOMAIN}/cart/1:1?discount={self.code}",
        }

    __call__ = create_code


class ExplodingMerchant:
    """A merchant that is simply down. A refusal here creates NO code — the control case."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_code(self, store_id: Any, offer: Any) -> dict[str, str]:
        self.calls.append(str(store_id))
        raise RuntimeError("merchant POST /codes is down")

    __call__ = create_code


def offer(domain: str = SELLER_DOMAIN, *, checkout_url: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "product_ref": "product-1",
        "unit_price": 100.0,
        "total_price": 100.0,
        "expires_at": T_FUTURE,
    }
    if checkout_url is not None:
        body["checkout_url"] = checkout_url
    return body


def auction(*bid_ids: str) -> dict[str, Any]:
    bids = []
    for bid_id in bid_ids or ("bid-a", "bid-b"):
        store_id = bid_id.replace("bid", "store")
        domain = f"{store_id}.example.com"
        bids.append(
            {
                "bid_id": bid_id,
                "store_id": store_id,
                "store_domain": domain,
                # No `checkout_url`: the R10 list-price fallback shape `collect_bids`
                # manufactures for every Tier-0 and silent store. It is legal, and it is
                # what leaves the port's pre-mint host check with nothing to look at.
                "offer": offer(domain),
            }
        )
    return {
        "auction_id": "auction-1",
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "bids": bids,
        "accepted_bid_ref": None,
        "now": T_NOW,
    }


def kinds(result: Any) -> list[str]:
    return [str(event["kind"]) for event in result.events]


def event_of(result: Any, kind: str) -> dict[str, Any]:
    for event in result.events:
        if event["kind"] == kind:
            return dict(event)
    raise AssertionError(f"no {kind!r} event in {kinds(result)}")


@pytest.fixture
def unwired() -> Iterator[None]:
    """No platform registry wired, restored afterwards — module state must not leak."""
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


# =====================================================================================
# T-202 (a) — the minted code is carried out and RECORDED, not dropped
# =====================================================================================
def test_an_off_domain_permalink_leaves_a_code_the_exchange_has_recorded(
    unwired: None,
) -> None:
    """The merchant minted. The exchange must hold a record it can revoke it by."""
    live = auction("bid-a", "bid-b")
    merchant = OffDomainMerchant()

    result = accept(live, "bid-a", merchant, "shopify")

    # The buyer is protected exactly as before: nothing handed over, auction still open.
    assert result.accepted is False, "an off-domain permalink from the merchant was accepted"
    assert result.permalink_url is None
    assert result.code is None
    assert live["accepted_bid_ref"] is None
    assert result.reoffer_bid_ref == "bid-b", "A5: the buyer got no next slot"
    assert len(merchant.calls) == 1, "the merchant must actually have minted"

    # ...and THIS is the half that was missing: a real code exists, so an event names it.
    assert "code_created" in kinds(result), (
        "the merchant issued a live single-use discount and the exchange recorded no "
        f"code_created event for it; events were {kinds(result)}"
    )
    created = event_of(result, "code_created")
    payload = created["payload"]
    assert payload["code"] == ORPHAN_CODE, (
        "the code_created event does not carry the code that was actually minted"
    )
    assert payload.get("orphaned") is True, (
        "the record must say this code belongs to a REFUSED checkout, or reconciliation "
        "will read it as a completed one"
    )
    assert created["store_id"] == "store-a"
    assert created["auction_id"] == "auction-1"


def test_the_recorded_code_is_reachable_from_the_refusal_without_string_matching(
    unwired: None,
) -> None:
    """An operator holding the refusal must be able to reach the revocable record."""
    live = auction("bid-a", "bid-b")
    result = accept(live, "bid-a", OffDomainMerchant(), "shopify")

    policy = event_of(result, "policy_event")
    pointer = policy["payload"].get("orphaned_code")
    assert isinstance(pointer, dict), (
        "the refusal event carries no structured pointer to the orphaned code; "
        f"payload was {policy['payload']}"
    )
    created = event_of(result, "code_created")
    assert pointer.get("event_id") == created["event_id"], (
        "the refusal does not name the code_created event that holds the live code"
    )
    assert pointer.get("fingerprint") == created["payload"].get("fingerprint"), (
        "the fingerprint in the refusal does not join to the recorded code"
    )
    assert pointer.get("fingerprint"), "the fingerprint is empty — nothing to join on"


# =====================================================================================
# T-215 (b) — the code is NOWHERE in the persisted refusal or the client-visible reason
# =====================================================================================
def test_the_denial_reason_does_not_carry_the_discount_code(unwired: None) -> None:
    live = auction("bid-a", "bid-b")
    result = accept(live, "bid-a", OffDomainMerchant(), "shopify")

    reason = result.denial_reason or ""
    assert reason, "a refusal with no reason is not debuggable"
    assert ORPHAN_CODE not in reason, (
        "the live discount code was written verbatim into the client-visible denial "
        f"reason: {reason!r}"
    )


def test_the_persisted_refusal_event_does_not_carry_the_discount_code(unwired: None) -> None:
    """`policy_event` is the persisted, published-as-a-bare-string surface. Nothing in it."""
    live = auction("bid-a", "bid-b")
    result = accept(live, "bid-a", OffDomainMerchant(), "shopify")

    policy = json.dumps(event_of(result, "policy_event"), default=str)
    assert ORPHAN_CODE not in policy, (
        f"the live discount code appears in the persisted refusal event: {policy}"
    )


def test_the_exception_itself_cannot_leak_the_code_at_any_call_site() -> None:
    """The redaction has to live on the exception, not on one caller's format string.

    A fix that only scrubbed ``accept()``'s reason would leave the identical trap armed for
    the next call site that logs the exception naively — and the message leaked the code
    TWICE: once as ``{minted.code!r}`` and once inside the ``url=`` of the off-domain reason,
    because a cart permalink spells the code in its ``?discount=`` query.
    """
    merchant = OffDomainMerchant()
    with pytest.raises(OrphanedCheckoutCode) as raised:
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=offer(SELLER_DOMAIN),
                mode="shopify",
                code_creator=merchant,
                now=T_NOW,
                registered_domains=StaticRegisteredDomains({"store-a": SELLER_DOMAIN}),
            )
        )

    exc = raised.value
    assert merchant.calls, "the merchant must actually have minted"
    # The payload is still intact for whoever CAN revoke it.
    assert exc.orphan.code == ORPHAN_CODE
    # The prose is not.
    assert ORPHAN_CODE not in str(exc), f"str(exc) leaks the code: {str(exc)!r}"
    assert ORPHAN_CODE not in "".join(str(arg) for arg in exc.args), (
        f"exc.args leaks the code: {exc.args!r}"
    )
    assert ORPHAN_CODE not in repr(exc), f"repr(exc) leaks the code: {repr(exc)!r}"
    # The diagnostic that must survive: which host was refused, and a joinable handle.
    assert RIVAL_DOMAIN in str(exc), "the refused host is the whole point of the message"


def test_the_chained_cause_cannot_leak_the_code_into_a_traceback() -> None:
    """The second channel, and the one a message-only redaction misses entirely.

    ``raise OrphanedOffDomainCheckout(...) from exc`` attaches the exception that named the
    offending permalink — and a cart permalink spells the discount in its ``?discount=``. So
    even with the orphan's own message clean, one ``traceback.format_exc()`` in a log handler
    republishes the live code. Measured, not theorised: before the ``__cause__`` property this
    printed the code in full while ``str(exc)`` was already clean.
    """
    merchant = OffDomainMerchant()
    try:
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=offer(SELLER_DOMAIN),
                mode="shopify",
                code_creator=merchant,
                now=T_NOW,
                registered_domains=StaticRegisteredDomains({"store-a": SELLER_DOMAIN}),
            )
        )
    except OrphanedCheckoutCode as exc:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        assert exc.orphan.code == ORPHAN_CODE, "the payload must still hold the real code"
        assert ORPHAN_CODE not in rendered, (
            f"the full traceback republishes the live discount code:\n{rendered}"
        )
        # The chain itself must survive — a redaction that severs `__cause__` would take the
        # "why" of the refusal with it.
        assert exc.__cause__ is not None, "the chained cause was dropped, not redacted"
        assert RIVAL_DOMAIN in str(exc.__cause__), "the cause no longer names the bad host"
    else:  # pragma: no cover - the checkout must refuse
        raise AssertionError("the off-domain permalink was not refused")


# =====================================================================================
# The control — a refusal that minted NOTHING must behave exactly as it did before
# =====================================================================================
def test_a_refusal_with_no_orphan_is_unchanged(unwired: None) -> None:
    """A5: the deliberate breadth of `except Exception` is preserved, not narrowed.

    A merchant that is merely down created no code, so there is nothing to record — and the
    buyer must still get the next slot rather than a stack trace.
    """
    live = auction("bid-a", "bid-b")
    merchant = ExplodingMerchant()

    result = accept(live, "bid-a", merchant, "shopify")

    assert result.accepted is False
    assert result.reoffer_bid_ref == "bid-b"
    assert result.permalink_url is None
    assert merchant.calls == ["store-a"]
    assert kinds(result) == ["policy_event"], (
        f"a refusal that minted nothing recorded a code anyway: {kinds(result)}"
    )
    assert (result.denial_reason or "").startswith("checkout_refused: RuntimeError:"), (
        f"the buyer-facing refusal shape changed: {result.denial_reason!r}"
    )
    assert "merchant POST /codes is down" in (result.denial_reason or "")


def test_an_off_domain_checkout_url_still_refuses_before_any_mint(unwired: None) -> None:
    """The positive control for the whole file: the PRE-mint check still creates no code."""
    live = auction("bid-a", "bid-b")
    live["bids"][0]["offer"]["checkout_url"] = f"https://{RIVAL_DOMAIN}/cart/1:1"
    merchant = OffDomainMerchant()

    result = accept(live, "bid-a", merchant, "shopify")

    assert result.accepted is False
    assert merchant.calls == [], "a code was minted before the pre-mint host check"
    assert "code_created" not in kinds(result), (
        "a code_created event was recorded for a checkout that never minted anything"
    )
