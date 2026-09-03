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
division of labour — the *real* code goes to the ``code_created`` ledger event, the kind
whose published shape exists to hold it and the only place it is written down; the refusal's
prose carries only a non-reversible fingerprint that joins the two. (That event is a durable
record, not a cleanup: nothing in this repo consumes it yet. See ``accept/offer.py``'s
``_orphan_record``.)

**The lesson this file exists to hold, added after the first fix was measured and failed.**
That fix redacted by replacing three literal spellings of the code, and this gate could not
see the hole because its own merchant double built the permalink by interpolating the same
literal it returned as ``code``. Every case it could express was the exact-spelling case —
the one case a blacklist survives. In reality ``code`` and ``permalink_url`` are two
independent merchant-authored fields, so a merchant that lower-cases its permalink (ordinary
CDN behaviour; Shopify redeems case-insensitively) or percent-encodes it walked the live code
straight into both published surfaces. So:

* the double takes ``code`` and ``permalink_url`` **separately**, and the spelling matrix in
  ``SPELLINGS`` is what makes them disagree; and
* every assertion goes through :func:`redeemable_spelling`, which asks whether a reader could
  RECOVER a redeemable code, not whether the exact bytes appear — and
  :func:`test_the_leak_detector_itself_detects_a_leak` proves that helper can fail.
"""

from __future__ import annotations

import dataclasses
import io
import json
import sys
import traceback
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote

import pytest
from exchange.accept import accept, use_registered_domains
from exchange.checkout import (
    CheckoutRequest,
    OrphanedCheckoutCode,
    OrphanedCode,
    StaticRegisteredDomains,
    code_fingerprint,
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

    ``permalink_url`` is a SEPARATE constructor argument on purpose, and the omission of
    that separation is what let the first T-215 fix ship broken. This double used to build
    its permalink by f-string interpolating the same literal it returned as ``code``, so
    every case it could express was one where the two fields spelled the code identically —
    which is exactly the only case a spelling blacklist survives. ``code`` and
    ``permalink_url`` are two independent, merchant-authored fields off one reply
    (``providers.py`` validates the shape of neither), and nothing makes them agree.
    """

    def __init__(self, code: str = ORPHAN_CODE, permalink_url: str | None = None) -> None:
        self.code = code
        self.permalink_url = (
            permalink_url
            if permalink_url is not None
            else f"https://{RIVAL_DOMAIN}/cart/1:1?discount={code}"
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_code(self, store_id: Any, offer: Any) -> dict[str, str]:
        self.calls.append((str(store_id), dict(offer)))
        return {"code": self.code, "permalink_url": self.permalink_url}

    __call__ = create_code


def redeemable_spelling(text: str, code: str) -> str | None:
    """The form of ``code`` a reader of ``text`` could actually redeem, or ``None``.

    A plain ``code not in text`` is NOT this assertion, and believing it was is the whole
    of the first T-215 failure. What matters is not whether the exact bytes of ``code``
    appear — it is whether anything in ``text`` can be turned back into a string the
    merchant will honour. Two transformations do that, and both were measured defeating the
    original redaction end to end:

    * **case** — this repo's own Shopify stub matches redemptions with
      ``candidate.strip().upper() == self.code.upper()``
      (``services/shopify-stub/src/codes.py``) and keys its table by ``.upper()``
      (``state.py``). ``summer10-live`` in a log IS ``SUMMER10-LIVE``.
    * **percent-encoding** — ``%50%53%58%2D%4C%49%56%45%30%31`` is one ``unquote`` away
      from ``PSX-LIVE01``, and a permalink is a URL, so encoding it is ordinary rather than
      exotic.

    Returns the offending decoding so a failure message can show what was recoverable.
    """
    needle = code.casefold()
    candidate = text
    for _ in range(4):  # bounded; `%2550` is a percent-encoded percent-encoding
        if needle in candidate.casefold():
            return candidate
        decoded = unquote(candidate)
        if decoded == candidate:
            return None
        candidate = decoded
    return None


def assert_code_is_unrecoverable(text: str, code: str, surface: str) -> None:
    found = redeemable_spelling(text, code)
    assert found is None, (
        f"a live discount code is recoverable from {surface}. The code is {code!r} and this "
        f"surface decodes to something containing it:\n{found}"
    )


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
# T-215 (c) — the permalink may spell the code ANY way it likes, or not at all
#
# The blind spot that let the first fix ship. Its double interpolated the same literal into
# both fields, so every case it could reach was the exact-spelling one — the only case a
# substring blacklist survives. These are the cases it could not express.
# =====================================================================================
#: ``(label, code, permalink_url)``. The code and the URL are independent merchant fields.
SPELLINGS: list[tuple[str, str, str]] = [
    (
        "exact",
        "PSX-EXACT-01",
        f"https://{RIVAL_DOMAIN}/cart/1:1?discount=PSX-EXACT-01",
    ),
    (
        # No hostile intent and no exotic characters: lower-casing a URL is ordinary CDN
        # and link-building behaviour, and Shopify redeems case-insensitively.
        "lowercased-permalink",
        "SUMMER10-LIVE",
        f"https://{RIVAL_DOMAIN}/cart/1:1?discount=summer10-live",
    ),
    (
        # `unquote()`s straight back to `PSX-LIVE01`.
        "percent-encoded-permalink",
        "PSX-LIVE01",
        f"https://{RIVAL_DOMAIN}/cart/1:1?discount=%50%53%58%2D%4C%49%56%45%30%31",
    ),
    (
        # Shopify's OTHER discount-link shape puts the code in the PATH, not the query — so
        # a redaction that parses out the `discount` parameter and stops is still wrong.
        "code-in-the-path",
        "PSX-PATH-77",
        f"https://{RIVAL_DOMAIN}/discount/PSX-PATH-77?redirect=/cart/1:1",
    ),
    (
        # The control: no spelling of the code anywhere in the URL. It must pass for the
        # right reason, and the host assertion below is what proves the message survived.
        "no-code-in-permalink",
        "PSX-ABSENT-9",
        f"https://{RIVAL_DOMAIN}/cart/1:1",
    ),
]


@pytest.mark.parametrize(("label", "code", "permalink"), SPELLINGS, ids=[s[0] for s in SPELLINGS])
def test_no_spelling_of_the_permalink_leaks_the_code(
    unwired: None, label: str, code: str, permalink: str
) -> None:
    """Neither surface may yield a redeemable code, however the merchant spelled the URL.

    The two surfaces are the two that publish: ``denial_reason`` goes back to the client,
    and the ``policy_event`` is persisted into the ledger with its ``reason`` typed as a
    bare string by the published OpenAPI. Both are asserted for every spelling, because the
    defect was that they agreed with each other — and both were wrong.
    """
    live = auction("bid-a", "bid-b")
    merchant = OffDomainMerchant(code, permalink)

    result = accept(live, "bid-a", merchant, "shopify")

    assert merchant.calls, f"[{label}] the merchant must actually have minted"
    assert result.accepted is False, f"[{label}] an off-domain permalink was accepted"

    reason = result.denial_reason or ""
    assert reason, f"[{label}] a refusal with no reason is not debuggable"
    assert_code_is_unrecoverable(reason, code, f"[{label}] the client-visible denial_reason")

    policy = json.dumps(event_of(result, "policy_event"), default=str)
    assert_code_is_unrecoverable(policy, code, f"[{label}] the persisted policy_event")

    # ...and the diagnostic still has to be there, or the redaction has merely deleted the
    # message. The refused HOST is what an operator acts on.
    assert RIVAL_DOMAIN in reason, (
        f"[{label}] the refused host was redacted away with the code; the message is now "
        f"useless to an operator: {reason!r}"
    )
    # T-202 is untouched by any of this: the real code is still recorded, once.
    created = event_of(result, "code_created")
    assert created["payload"]["code"] == code, (
        f"[{label}] the recorded code is not the one the merchant minted"
    )
    assert result.orphaned_code is not None and result.orphaned_code.code == code


def test_the_leak_detector_itself_detects_a_leak() -> None:
    """The gate above is worthless if its detector cannot fail. Prove it can.

    Every assertion in this file is a negative — "the code is not recoverable" — and a
    negative passes just as happily when the check is broken as when the code is safe. This
    is the falsifiability control: the same helper, pointed at text that really does carry
    each spelling, must report every one of them.
    """
    for _, code, permalink in SPELLINGS[:-1]:
        leaky = f"refused url={permalink!r}"
        assert redeemable_spelling(leaky, code) is not None, (
            f"the detector missed a recoverable {code!r} inside {leaky!r} — every other "
            f"assertion in this file is unfalsifiable until this passes"
        )
    # ...and it must not cry wolf on the redacted form or on the control.
    assert (
        redeemable_spelling(f"https://{RIVAL_DOMAIN}/<redacted:code:d72a725ef90a>", "PSX-LIVE01")
        is None
    )
    assert (
        redeemable_spelling(f"refused url='https://{RIVAL_DOMAIN}/cart/1:1'", "PSX-ABSENT-9")
        is None
    )


def test_the_default_string_form_of_the_orphan_does_not_spell_the_code() -> None:
    """The code must be reachable by explicit field access only, never by formatting.

    ``OrphanedCode`` is a frozen dataclass, so it arrived with the generated ``repr`` — and
    ``AcceptResult`` holds one, with a generated ``repr`` of its own that renders the nested
    field. A single ``logger.debug("%r", result)`` therefore published a live discount and
    the merchant's permalink, in the very object that produces the client-visible refusal.
    That surface did not exist before the orphan was carried out, so the fix for T-202
    introduced it.
    """
    live = auction("bid-a", "bid-b")
    code = "PSX-REPR-4242"
    result = accept(
        live,
        "bid-a",
        OffDomainMerchant(code, f"https://{RIVAL_DOMAIN}/cart/1:1?discount={code}"),
        "shopify",
    )

    orphan = result.orphaned_code
    assert orphan is not None, "T-202: the orphan must still be carried on the result"
    assert orphan.code == code, "explicit field access is how the revoker reads it"

    assert_code_is_unrecoverable(repr(orphan), code, "repr(AcceptResult.orphaned_code)")
    assert_code_is_unrecoverable(str(orphan), code, "str(AcceptResult.orphaned_code)")
    assert_code_is_unrecoverable(f"{orphan}", code, "an f-string of the orphan")
    assert_code_is_unrecoverable(
        repr(dataclasses.replace(result, events=())),
        code,
        "repr(AcceptResult) with the deliberate code_created ledger record removed",
    )
    # The permalink is merchant-controlled too and travels with the code; its path and query
    # are where a code hides, so the repr keeps only the host.
    assert_code_is_unrecoverable(repr(orphan), code, "the permalink inside repr(orphan)")
    assert RIVAL_DOMAIN in repr(orphan), (
        f"the orphan's repr no longer says which host it came from: {repr(orphan)}"
    )
    # The fingerprint is the join to the recorded code, and must survive.
    assert code_fingerprint(code) in repr(orphan), (
        f"repr(orphan) carries no handle joining it to the code_created event: {repr(orphan)}"
    )
    # NOTE what is deliberately NOT asserted: `repr(result.events)` DOES spell the code,
    # because `events` holds the `code_created` ledger record whose entire purpose (T-202)
    # is to carry the revocable code. That is the same shape `accept()` already returns on
    # the SUCCESS path on main, where `AcceptResult.code`, `.permalink_url` and two events
    # all name the live code. The ledger is the sanctioned home for it; the refusal prose
    # and the client-visible reason are not.


@pytest.mark.parametrize(("label", "code", "permalink"), SPELLINGS, ids=[s[0] for s in SPELLINGS])
def test_a_naive_new_call_site_cannot_reopen_the_leak(
    label: str, code: str, permalink: str
) -> None:
    """The "safe by construction" claim, gated directly instead of assumed.

    The port's own raise sites scrub the cause before chaining it, so the message they hand
    to ``OrphanedCheckoutCode`` is already clean — which means the tests above would pass
    even if the constructor did nothing. That is precisely the gap the first fix's report
    claimed to have closed and had not: its constructor redaction was a blacklist over
    three spellings of ``orphan.code``, so a *new* call site formatting the merchant's
    permalink into its own message reopened the leak for any spelling that disagreed.

    This is that new call site. It hands the constructor the raw URL, the way somebody who
    has never read this module would, and nothing but the constructor stands between that
    and the message.
    """
    orphan = OrphanedCode(
        code=code,
        permalink_url=permalink,
        provider="shopify",
        store_id="store-a",
        auction_id="auction-1",
        bid_ref="bid-a",
    )
    exc = OrphanedCheckoutCode(f"a brand new call site says: refused {permalink}", orphan=orphan)

    assert exc.orphan.code == code, "the payload must still reach whoever can revoke it"
    assert_code_is_unrecoverable(str(exc), code, f"[{label}] str() of a naively-built orphan")
    assert_code_is_unrecoverable(repr(exc), code, f"[{label}] repr() of a naively-built orphan")
    assert_code_is_unrecoverable(
        "".join(str(a) for a in exc.args), code, f"[{label}] the args of a naively-built orphan"
    )
    assert RIVAL_DOMAIN in str(exc), (
        f"[{label}] the host was redacted away with the code: {str(exc)!r}"
    )


def test_the_c_level_excepthook_cannot_print_the_code() -> None:
    """The channel a redacting ``property`` cannot close, measured rather than reasoned.

    ``OrphanedCheckoutCode`` overrides ``__cause__``/``__context__`` as properties, which
    fixes every reader that goes through Python attribute lookup — ``traceback.format_exc``,
    ``logging(exc_info=True)``, ``TracebackException``. The interpreter's DEFAULT excepthook
    is not one of them: C-level ``PyErr_Display`` reads the cause through
    ``PyException_GetCause``, straight off the C slot, and no property runs. Measured before
    the fix: ``sys.excepthook`` printed ``?discount=PSX-SECRET-ABC123`` in full while
    ``str(exc)`` was already clean.

    It is closed by redacting the cause **in place, before** it is chained, so the object in
    the C slot is itself clean — not by a reader the excepthook does not use.
    """
    code = "PSX-EXCEPTHOOK-1"
    merchant = OffDomainMerchant(code, f"https://{RIVAL_DOMAIN}/cart/1:1?discount={code}")
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
        captured = io.StringIO()
        original = sys.stderr
        sys.stderr = captured
        try:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        finally:
            sys.stderr = original
        rendered = captured.getvalue()
        assert rendered.strip(), "the excepthook printed nothing; the test proves nothing"
        assert exc.orphan.code == code, "the payload must still hold the real code"
        assert_code_is_unrecoverable(rendered, code, "the default sys.excepthook output")
        assert RIVAL_DOMAIN in rendered, "the refused host must survive into the traceback"
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
