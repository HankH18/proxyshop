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

**The lesson pass 3 added, and it is a lesson about THIS FILE.** Every case the matrix could
express put the code in the part of the URL ``redact_url`` throws away — the path or the
query — so the gate was blind to the one component it deliberately KEEPS. ``assert_on_domain``
builds a second, independently lower-cased copy of the host into its reason; a live code
spelled in the host walked into both published surfaces, the traceback and the C-level
excepthook while every test here passed. Two things follow, and both are structural rather
than "one more case":

* a gate whose cases all exercise the same discarded component is a gate that measures one
  thing and reports another, so the matrix now spans the host, the port, the scheme and the
  query — every component the URL has; and
* the redaction was moved to the sites that BUILD prose out of merchant input, because a
  redactor at the boundary cannot remove a value it was never handed. The tests that pin
  that are the ones naming ``_reason``'s individual fragments and the two post-mint handlers
  in ``provider.py`` and ``providers.py``.

Pass 3 also killed a defect that was not a leak at all: ``urlsplit().port`` raises on a
non-numeric port, and the redactor read it outside its own guard, so a permalink like
``https://attacker.tld:notaport/x`` took a bare ``ValueError`` out through
``provider.checkout()``. No ``OrphanedCheckoutCode`` was built, so no ``code_created`` event
was emitted and ``orphaned_code`` was ``None`` — the redaction crash destroyed T-202 on that
path. Every case in ``SPELLINGS`` therefore asserts the T-202 half too.
"""

from __future__ import annotations

import copy
import dataclasses
import io
import json
import re
import sys
import traceback
from collections.abc import Iterator
from typing import Any, NamedTuple
from urllib.parse import quote, unquote

import pytest
from exchange.accept import accept, use_registered_domains
from exchange.checkout import (
    CheckoutProvider,
    CheckoutRequest,
    MintedCheckout,
    OffDomainCheckout,
    OrphanedCheckoutCode,
    OrphanedCode,
    RedactedCause,
    SimulatedRedirectProvider,
    StaticRegisteredDomains,
    assert_on_domain,
    code_fingerprint,
    minting_ledger,
    record_minted_code,
    redact_code,
    redact_url,
    register_provider,
    render_can_publish,
    rendered_exception,
    resolve_provider,
    safe_token,
    spells_code,
)
from trust.ledger.canonical import canonical_event, canonical_json

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


#: The decodings a reader of a log line can apply for free. Deliberately implemented HERE
#: rather than imported from ``exchange.checkout.redaction``: a gate that shares its
#: recovery model with the code under test cannot catch the case where the model itself is
#: too narrow, which is the failure mode that shipped twice. If production widens, this must
#: be widened independently, and :func:`test_the_leak_detector_itself_detects_a_leak` is what
#: proves each transformation below is live.
def _percent(text: str) -> str:
    return unquote(text)


def _plus_as_space(text: str) -> str:
    """``+`` is a space in ``application/x-www-form-urlencoded`` — and a query string is one."""
    return text.replace("+", " ")


def _unicode_unescaped(text: str) -> str:
    r"""``PSX`` is ``PSX`` to a JSON reader, and the policy_event is serialised as JSON."""
    try:
        return text.encode("latin-1", "backslashreplace").decode("unicode_escape", "replace")
    except Exception:  # pragma: no cover - defensive; the encode above does not raise
        return text


_ACE = re.compile(r"xn--[A-Za-z0-9-]+", re.IGNORECASE)


def _punycode(text: str) -> str:
    """Decode ``xn--`` labels. A registrar will sell the host; a browser shows the Unicode.

    Matched wherever it appears in the prose, not only in a string that is entirely a
    hostname — a refusal message quotes the host inside a sentence, and a decoder that only
    understands a bare hostname reports "clean" on the surface that actually publishes.
    """

    def decode(match: re.Match[str]) -> str:
        try:
            return match.group(0)[4:].encode("ascii", "replace").decode("punycode")
        except Exception:
            return match.group(0)

    return _ACE.sub(decode, text) if "xn--" in text.casefold() else text


_DECODINGS = (_percent, _plus_as_space, _unicode_unescaped, _punycode)


def redeemable_spelling(text: str, code: str) -> str | None:
    """The form of ``code`` a reader of ``text`` could actually redeem, or ``None``.

    A plain ``code not in text`` is NOT this assertion, and believing it was is the whole
    of the first T-215 failure. What matters is not whether the exact bytes of ``code``
    appear — it is whether anything in ``text`` can be turned back into a string the
    merchant will honour. Every transformation below has been measured defeating some
    version of this redaction end to end:

    * **case** — this repo's own Shopify stub matches redemptions with
      ``candidate.strip().upper() == self.code.upper()``
      (``services/shopify-stub/src/codes.py``) and keys its table by ``.upper()``
      (``state.py``). ``summer10-live`` in a log IS ``SUMMER10-LIVE``. Case is what defeated
      the pass-2 fix, in the host: ``urlsplit().hostname`` lower-cases, so a case-sensitive
      literal replace could never have matched it.
    * **percent-encoding** — ``%50%53%58%2D%4C%49%56%45%30%31`` is one ``unquote`` away
      from ``PSX-LIVE01``, and a permalink is a URL, so encoding it is ordinary rather than
      exotic. Applied repeatedly: ``%2550`` is a percent-encoded percent-encoding.
    * **``+`` as space** — the encoding a form-encoded query uses for a code with a space in
      it, which Shopify permits.
    * **``\\uNNNN`` escapes** — what ``json.dumps`` writes and what a merchant that
      round-trips its reply through JSON hands back.
    * **punycode** — ``xn--psx--42-bkf`` decodes to ``psx-Ω-42``. ``urlsplit().hostname``
      returns the ACE form, so a case-folded substring search over the raw host misses a
      non-ASCII code entirely.

    Returns the offending decoding so a failure message can show what was recoverable.
    """
    needle = code.casefold()
    seen: set[str] = set()
    frontier = [text]
    while frontier and len(seen) < 64:  # bounded: the transformations compose
        candidate = frontier.pop()
        folded = candidate.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        if needle in folded:
            return candidate
        for decode in _DECODINGS:
            nxt = decode(candidate)
            if nxt != candidate:
                frontier.append(nxt)
    return None


def assert_code_is_unrecoverable(text: str, code: str, surface: str) -> None:
    found = redeemable_spelling(text, code)
    assert found is None, (
        f"a live discount code is recoverable from {surface}. The code is {code!r} and this "
        f"surface decodes to something containing it:\n{found}"
    )


def excepthook_output(exc: BaseException) -> str:
    """What the interpreter's DEFAULT excepthook prints — the C-level channel.

    ``PyErr_Display`` walks the chain through ``PyException_GetCause``, reading the C slot
    directly, so none of ``OrphanedCheckoutCode``'s Python-level properties run. It is the
    one reader that sees the exception objects exactly as they were chained.
    """
    captured = io.StringIO()
    original = sys.stderr
    sys.stderr = captured
    try:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    finally:
        sys.stderr = original
    rendered = captured.getvalue()
    assert rendered.strip(), "the excepthook printed nothing; the assertion would be vacuous"
    return rendered


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
class Spelling(NamedTuple):
    """One merchant reply: a code, a permalink, and what the refusal may still say.

    ``host_survives`` is the *diagnostic* expectation, and it is a property of where the
    merchant put the code rather than a knob. The refusal exists to tell an operator which
    host was refused, so the host is kept whenever it is safe to keep — but when the code is
    spelled IN the host there is no way to publish the host without publishing the code, and
    the redaction fails closed. Those cases assert the fingerprint instead, which is the
    join to the ``code_created`` record and is what an operator actually acts on next.

    ``detectable_raw`` says whether :func:`redeemable_spelling` must find the code in the
    RAW permalink. It is False only for the control that spells no code at all, and it is
    what :func:`test_the_leak_detector_itself_detects_a_leak` iterates.
    """

    label: str
    code: str
    permalink: str
    host_survives: bool = True
    detectable_raw: bool = True


#: The code and the URL are independent merchant fields. Nothing makes them agree.
SPELLINGS: list[Spelling] = [
    Spelling(
        "exact",
        "PSX-EXACT-01",
        f"https://{RIVAL_DOMAIN}/cart/1:1?discount=PSX-EXACT-01",
    ),
    Spelling(
        # No hostile intent and no exotic characters: lower-casing a URL is ordinary CDN
        # and link-building behaviour, and Shopify redeems case-insensitively.
        "lowercased-permalink",
        "SUMMER10-LIVE",
        f"https://{RIVAL_DOMAIN}/cart/1:1?discount=summer10-live",
    ),
    Spelling(
        # `unquote()`s straight back to `PSX-LIVE01`.
        "percent-encoded-permalink",
        "PSX-LIVE01",
        f"https://{RIVAL_DOMAIN}/cart/1:1?discount=%50%53%58%2D%4C%49%56%45%30%31",
    ),
    Spelling(
        # Shopify's OTHER discount-link shape puts the code in the PATH, not the query — so
        # a redaction that parses out the `discount` parameter and stops is still wrong.
        "code-in-the-path",
        "PSX-PATH-77",
        f"https://{RIVAL_DOMAIN}/discount/PSX-PATH-77?redirect=/cart/1:1",
    ),
    Spelling(
        # The control: no spelling of the code anywhere in the URL. It must pass for the
        # right reason, and the host assertion below is what proves the message survived.
        "no-code-in-permalink",
        "PSX-ABSENT-9",
        f"https://{RIVAL_DOMAIN}/cart/1:1",
        detectable_raw=False,
    ),
    # -----------------------------------------------------------------------------------
    # Pass 3. The cases above all keep the code in the part of the URL `redact_url` THROWS
    # AWAY — path, query — so they could not see the pass-2 hole, which was in the one
    # component it deliberately KEEPS. `assert_on_domain` builds a second, independently
    # lower-cased copy of the host into its reason; the boundary redactor never held that
    # string, and `.lower()` put it out of reach of the case-sensitive literal layer. An
    # ordinary uppercase Shopify-style code spelled in the host walked into `denial_reason`,
    # the persisted `policy_event`, `str(exc)`, the traceback and the C-level excepthook.
    # -----------------------------------------------------------------------------------
    Spelling(
        "code-in-the-host",
        "PSX-HOST-42",
        f"https://PSX-HOST-42.{RIVAL_DOMAIN}/cart/1:1",
        host_survives=False,
    ),
    Spelling(
        # A hostname a registrar will sell. The ACE form still contains the ASCII code
        # (punycode copies basic code points verbatim), so this is the host case wearing a
        # disguise — and it is the shape `_host_spells`' pass-2 docstring named as a
        # residual it did not cover.
        "punycode-host",
        "PSX-PUNY-9",
        f"https://xn--psx-puny-9-x1a.{RIVAL_DOMAIN}/cart/1:1",
        host_survives=False,
    ),
    Spelling(
        # ...and the version that punycode really does hide: a NON-ASCII code, whose ACE
        # form `xn--psx--42-bkf` contains no substring of it at all. Only decoding the label
        # recovers it, which is why the detector had to learn punycode rather than be told
        # the residual was acceptable.
        "punycode-hides-a-non-ascii-code",
        "PSX-Ω-42",
        f"https://xn--psx--42-bkf.{RIVAL_DOMAIN}/cart/1:1",
        host_survives=False,
    ),
    Spelling(
        # The CRITICAL pass-2 defect. `urlsplit().port` raises `ValueError` on a non-numeric
        # port, and pass 2 read it outside its own guard — so this shape did not leak, it
        # CRASHED `redact_url`, took a bare `ValueError` out through `provider.checkout()`,
        # and destroyed T-202: no `code_created` event, no `orphaned_code`, a live discount
        # the exchange could neither see nor revoke. The T-202 assertions at the bottom of
        # this test are what pin that.
        "non-numeric-port",
        "PSX-PORT-13",
        f"https://{RIVAL_DOMAIN}:notaport/cart/1:1?discount=PSX-PORT-13",
    ),
    Spelling(
        # The same crash, with the code IN the offending port — so the `ValueError`'s own
        # text ("Port could not be cast to integer value as 'PSX99'") published it verbatim
        # into `denial_reason` and the persisted event.
        "code-in-the-port",
        "PSX99",
        f"https://{RIVAL_DOMAIN}:PSX99/cart/1:1",
    ),
    Spelling(
        # `urlsplit` accepts any `[A-Za-z][A-Za-z0-9+.-]*` as a scheme, and the off-domain
        # reason quotes the scheme it refused. A third merchant-controlled fragment in the
        # same sentence, found by the sweep rather than by a failing test.
        "code-in-the-scheme",
        "PSX-SCHEME-8",
        f"PSX-SCHEME-8://{RIVAL_DOMAIN}/cart/1:1",
    ),
    Spelling(
        # `+` is a space in a form-encoded query, and Shopify permits a space in a code.
        "plus-encoded-permalink",
        "PSX LIVE 5",
        f"https://{RIVAL_DOMAIN}/cart/1:1?discount=PSX+LIVE+5",
    ),
    Spelling(
        # What a merchant that round-trips its own reply through JSON hands back.
        "unicode-escaped-permalink",
        "PSX-ESC-6",
        f"https://{RIVAL_DOMAIN}/cart/1:1?discount=" + r"\u0050\u0053\u0058-ESC-6",
    ),
]


def assert_diagnostic_survived(text: str, case: Spelling, surface: str) -> None:
    """The other half of every assertion here: redaction must not become deletion.

    A message that says nothing satisfies T-215 perfectly and is useless, so each surface is
    checked for the diagnostic it is still required to carry — the refused host where the
    host is safe to publish, and the fingerprint that joins to the ``code_created`` record
    always.
    """
    assert code_fingerprint(case.code) in text, (
        f"[{case.label}] {surface} carries no fingerprint joining it to the code_created "
        f"event that holds the live code — the refusal is unactionable: {text!r}"
    )
    if case.host_survives:
        assert RIVAL_DOMAIN in text, (
            f"[{case.label}] the refused host was redacted away with the code; {surface} is "
            f"now useless to an operator: {text!r}"
        )
    else:
        assert "redacted-host" in text, (
            f"[{case.label}] the code is spelled IN the host, so the host must be dropped "
            f"and SAID to have been dropped; {surface} was: {text!r}"
        )


@pytest.mark.parametrize("case", SPELLINGS, ids=[s.label for s in SPELLINGS])
def test_no_spelling_of_the_permalink_leaks_the_code(unwired: None, case: Spelling) -> None:
    """Neither surface may yield a redeemable code, however the merchant spelled the URL.

    The two surfaces are the two that publish: ``denial_reason`` goes back to the client,
    and the ``policy_event`` is persisted into the ledger with its ``reason`` typed as a
    bare string by the published OpenAPI. Both are asserted for every spelling, because the
    defect was that they agreed with each other — and both were wrong.
    """
    label, code, permalink = case.label, case.code, case.permalink
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
    # message. The refused HOST is what an operator acts on, where publishing it is safe.
    assert_diagnostic_survived(reason, case, "the client-visible denial_reason")

    # T-202 is untouched by any of this: the real code is still recorded, once. This is the
    # assertion the pass-2 `redact_url` crash broke outright on `non-numeric-port` — the
    # refusal never became an `OrphanedCheckoutCode`, so there was no orphan to record.
    assert "code_created" in kinds(result), (
        f"[{label}] the merchant issued a live discount and the exchange recorded no "
        f"code_created event for it; events were {kinds(result)}. T-202 is destroyed on "
        f"this path — the code is live at the merchant and the exchange cannot revoke it."
    )
    created = event_of(result, "code_created")
    assert created["payload"]["code"] == code, (
        f"[{label}] the recorded code is not the one the merchant minted"
    )
    assert created["payload"].get("orphaned") is True, (
        f"[{label}] the record must say this code belongs to a REFUSED checkout"
    )
    assert result.orphaned_code is not None and result.orphaned_code.code == code, (
        f"[{label}] T-202: the orphan was not carried out to the caller that can revoke it"
    )
    # The refusal must still point AT that record, or an operator holding only the reason
    # has a fingerprint that joins to nothing.
    pointer = event_of(result, "policy_event")["payload"].get("orphaned_code")
    assert isinstance(pointer, dict) and pointer.get("event_id") == created["event_id"], (
        f"[{label}] the refusal does not name the code_created event holding the live code"
    )


def test_the_leak_detector_itself_detects_a_leak() -> None:
    """The gate above is worthless if its detector cannot fail. Prove it can.

    Every assertion in this file is a negative — "the code is not recoverable" — and a
    negative passes just as happily when the check is broken as when the code is safe. This
    is the falsifiability control: the same helper, pointed at text that really does carry
    each spelling, must report every one of them.
    """
    for case in SPELLINGS:
        leaky = f"refused url={case.permalink!r}"
        found = redeemable_spelling(leaky, case.code)
        if case.detectable_raw:
            assert found is not None, (
                f"[{case.label}] the detector missed a recoverable {case.code!r} inside "
                f"{leaky!r} — every other assertion in this file is unfalsifiable until "
                f"this passes"
            )
        else:
            assert found is None, (
                f"[{case.label}] is the control: it spells no code, and a detector that "
                f"reports one here is crying wolf rather than working"
            )

    # Each transformation, on its own, against text that carries ONLY that encoding. A
    # detector that is wide on paper and narrow in fact is how pass 1 and pass 2 both
    # shipped: the widening is only real if every branch of it is exercised.
    each_encoding = [
        ("case", "SUMMER10-LIVE", "refused url='.../cart?discount=summer10-live'"),
        ("percent", "PSX-LIVE01", "refused '%50%53%58%2D%4C%49%56%45%30%31'"),
        (
            "double-percent",
            "PSX-LIVE01",
            "refused '%2550%2553%2558%252D%254C%2549%2556%2545%2530%2531'",
        ),
        ("plus-as-space", "PSX LIVE 5", "refused '.../cart?discount=PSX+LIVE+5'"),
        ("unicode-escape", "PSX-ESC-6", r"refused '?discount=\u0050\u0053\u0058-ESC-6'"),
        ("punycode", "PSX-Ω-42", "refused host='xn--psx--42-bkf.attacker.tld'"),
    ]
    for name, code, leaky in each_encoding:
        assert redeemable_spelling(leaky, code) is not None, (
            f"the {name} branch of the recovery model is dead: {leaky!r} really does yield "
            f"{code!r} and the detector said it did not. Every negative assertion that "
            f"relies on that branch is vacuous."
        )

    # ...and it must not cry wolf on the redacted forms or on the control.
    assert (
        redeemable_spelling(f"https://{RIVAL_DOMAIN}/<redacted:code:d72a725ef90a>", "PSX-LIVE01")
        is None
    )
    assert (
        redeemable_spelling(f"refused url='https://{RIVAL_DOMAIN}/cart/1:1'", "PSX-ABSENT-9")
        is None
    )
    assert redeemable_spelling("host '<redacted-host:code:bf3c4ccf737c>'", "PSX-HOST-42") is None


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


@pytest.mark.parametrize("case", SPELLINGS, ids=[s.label for s in SPELLINGS])
def test_a_naive_new_call_site_cannot_reopen_the_leak(case: Spelling) -> None:
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
    label, code, permalink = case.label, case.code, case.permalink
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
    # The orphan carried on the exception is itself a logging surface — `AcceptResult` holds
    # one and renders it — so it gets the same treatment for every spelling.
    assert_code_is_unrecoverable(repr(orphan), code, f"[{label}] repr() of the orphan itself")
    if case.host_survives:
        assert RIVAL_DOMAIN in str(exc), (
            f"[{label}] the host was redacted away with the code: {str(exc)!r}"
        )
    else:
        assert "redacted-host-url" in str(exc), (
            f"[{label}] the code is spelled in the host, so the whole URL must be dropped "
            f"and said to have been dropped: {str(exc)!r}"
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
# T-215 (d) — the redactor is TOTAL, and a redactor that raises destroys T-202
#
# Pass 2 read `urlsplit(...).port` outside its own `try`. `urlsplit().port` is a property
# that parses lazily and raises `ValueError` on a non-numeric or out-of-range port, so a
# merchant answering `https://attacker.tld:notaport/x` raised a bare ValueError out of
# `redact_chain` -> `provider.checkout()`. Two things followed, and the second is worse than
# the leak: the ValueError's own text quotes the offending port (so a code spelled THERE was
# published verbatim), and the `OrphanedCheckoutCode` that carries the orphan was never
# constructed — no `code_created`, no `orphaned_code`, a live discount nothing can revoke.
#
# A sanitiser that raises on the untrusted input it exists to sanitise is the wrong shape.
# =====================================================================================
#: URLs no merchant should send and every merchant may. Each one is fed to the redactor
#: directly; none of them may raise, whatever else they do.
HOSTILE_URLS: list[str] = [
    "https://attacker.tld:notaport/cart/1:1",
    "https://attacker.tld:PSX-LIVE01/cart",
    "https://attacker.tld:99999999999/cart",
    "https://attacker.tld:-1/cart",
    "https://attacker.tld:/cart",
    "https://[not-an-ipv6/cart",
    "https://[::1]:notaport/cart",
    "http://user:pw@attacker.tld:xx@evil.tld/cart",
    "javascript:PSX-LIVE01",
    "data:text/html,PSX-LIVE01",
    "PSX-LIVE01://attacker.tld/cart",
    "//attacker.tld:notaport/cart",
    "",
    " ",
    "\x00https://attacker.tld:notaport/",
    "https://" + "a" * 3000 + ":notaport/cart",
    "https://attacker.tld:0x50/cart",
    "https://attacker.tld:٤٢/cart",  # Arabic-Indic digits: int() takes them, urlsplit may not
    "://",
    "?discount=PSX-LIVE01",
]


@pytest.mark.parametrize("url", HOSTILE_URLS, ids=[f"url{i}" for i in range(len(HOSTILE_URLS))])
def test_redact_url_never_raises_on_merchant_input(url: str) -> None:
    """Totality, stated as the property it is rather than as one regression case.

    Everything reachable from a merchant reply must fail CLOSED, never by exception. The
    parametrisation is the point: `notaport` is the shape that was measured, and it is one
    member of a family (bad port, bad IPv6 literal, no host, hostile scheme, NUL byte,
    kilobyte-long host) that a single regression test would not have covered.
    """
    reduced = redact_url(url, "PSX-LIVE01")  # must not raise
    assert isinstance(reduced, str)
    assert_code_is_unrecoverable(reduced, "PSX-LIVE01", f"redact_url({url!r})")
    # ...and it must be idempotent, or redacting an already-redacted message corrupts it.
    assert_code_is_unrecoverable(
        redact_url(reduced, "PSX-LIVE01"), "PSX-LIVE01", f"re-reducing redact_url({url!r})"
    )


@pytest.mark.parametrize("url", HOSTILE_URLS, ids=[f"url{i}" for i in range(len(HOSTILE_URLS))])
def test_redact_code_never_raises_and_fails_closed(url: str) -> None:
    """The other half: the message-level redactor must not raise either.

    And when it cannot do its job it must drop the PROSE, not return it. Returning the
    unredacted message on an internal failure is failing open, which is the one outcome
    worse than losing a diagnostic.
    """
    code = "PSX-LIVE01"
    message = f"refused url={url!r} code={code} lower={code.lower()}"
    redacted = redact_code(message, code, urls=(url,))  # must not raise
    assert isinstance(redacted, str)
    assert_code_is_unrecoverable(redacted, code, f"redact_code(... urls=({url!r},))")


def test_a_hostile_port_leaves_the_orphaned_code_recorded_and_revocable(unwired: None) -> None:
    """T-202 on the exact path the pass-2 redactor crashed. Measured end to end.

    Before this fix the run below produced `kinds == ['policy_event']`, `orphaned_code is
    None` and `denial_reason == "checkout_refused: ValueError: Port could not be cast to
    integer value as 'notaport'"` — the buyer was protected, and a live single-use discount
    sat in the merchant's account with nothing in the exchange naming it.
    """
    code = "PSX-PORTKILL-1"
    live = auction("bid-a", "bid-b")
    merchant = OffDomainMerchant(code, f"https://{RIVAL_DOMAIN}:notaport/cart/1:1?discount={code}")

    result = accept(live, "bid-a", merchant, "shopify")

    # The refusal itself is unchanged — the buyer is protected and gets the next slot.
    assert merchant.calls, "the merchant must actually have minted"
    assert result.accepted is False, "an off-domain permalink was accepted"
    assert result.permalink_url is None and result.code is None
    assert result.reoffer_bid_ref == "bid-b", "A5: the buyer got no next slot"
    assert live["accepted_bid_ref"] is None

    # It refused for the RIGHT reason: an off-domain host, not a crash in the redactor.
    assert isinstance(result.denial_reason, str)
    assert "ValueError" not in result.denial_reason, (
        "the redaction machinery raised instead of redacting; the refusal is now a bug "
        f"report about urlsplit rather than about the merchant: {result.denial_reason!r}"
    )
    assert "OrphanedOffDomainCheckout" in result.denial_reason, (
        f"the post-mint host check did not produce an orphan refusal: {result.denial_reason!r}"
    )

    # ...and T-202 holds: the live code is recorded, marked orphaned, and reachable.
    assert "code_created" in kinds(result), (
        f"T-202 destroyed: the merchant minted and nothing recorded it. Events: {kinds(result)}"
    )
    created = event_of(result, "code_created")
    assert created["payload"]["code"] == code
    assert created["payload"].get("orphaned") is True
    assert result.orphaned_code is not None and result.orphaned_code.code == code

    # ...and the code is still nowhere in either published surface.
    assert_code_is_unrecoverable(result.denial_reason, code, "denial_reason (hostile port)")
    assert_code_is_unrecoverable(
        json.dumps(event_of(result, "policy_event"), default=str), code, "policy_event"
    )


# =====================================================================================
# T-215 (e) — the leak is closed where the prose is BUILT, not where it is published
#
# `assert_on_domain` lives in `checkout/domain.py` and was invisible to two passes because
# the redaction lived in `checkout/provider.py`. It formats FOUR merchant-controlled
# fragments into one sentence — the parsed host (lower-cased, so a second copy no boundary
# redactor holds), the scheme, the text of a urlsplit ValueError, and the URL — and after a
# mint that sentence is carried verbatim into `denial_reason` and the persisted
# `policy_event`. These assert the fragment-level contract directly, so a regression is
# caught at the source rather than three modules downstream.
# =====================================================================================
#: ``(label, permalink, code)`` — one per merchant-controlled fragment `_reason` formats.
REASON_FRAGMENTS: list[tuple[str, str, str]] = [
    ("host", f"https://PSX-FRAG-HOST.{RIVAL_DOMAIN}/cart", "PSX-FRAG-HOST"),
    ("scheme", f"PSX-FRAG-SCHEME://{RIVAL_DOMAIN}/cart", "PSX-FRAG-SCHEME"),
    ("port", f"https://{RIVAL_DOMAIN}:PSX-FRAG-PORT/cart", "PSX-FRAG-PORT"),
    ("query", f"https://{RIVAL_DOMAIN}/cart?discount=PSX-FRAG-QUERY", "PSX-FRAG-QUERY"),
    ("path", f"https://{RIVAL_DOMAIN}/discount/PSX-FRAG-PATH", "PSX-FRAG-PATH"),
    ("fragment", f"https://{RIVAL_DOMAIN}/cart#PSX-FRAG-HASH", "PSX-FRAG-HASH"),
    ("userinfo", f"https://PSX-FRAG-USER@{RIVAL_DOMAIN}/cart", "PSX-FRAG-USER"),
    ("bad-ipv6", "https://[PSX-FRAG-IPV6/cart", "PSX-FRAG-IPV6"),
]


@pytest.mark.parametrize(
    ("label", "permalink", "code"), REASON_FRAGMENTS, ids=[f[0] for f in REASON_FRAGMENTS]
)
def test_the_off_domain_reason_redacts_every_merchant_fragment_it_formats(
    label: str, permalink: str, code: str
) -> None:
    """Given the code, `assert_on_domain` must publish no fragment that spells it."""
    with pytest.raises(OffDomainCheckout) as raised:
        assert_on_domain(permalink, SELLER_DOMAIN, what="permalink_url", secret=code)

    message = str(raised.value)
    assert message, "a refusal with no reason is not debuggable"
    assert_code_is_unrecoverable(message, code, f"[{label}] the off-domain reason itself")
    assert (
        "registered seller domain" in message
        or "not one a checkout" in message
        or ("not a parsable URL" in message)
    ), f"[{label}] the reason no longer says WHY it refused: {message!r}"


def test_the_off_domain_reason_is_unchanged_when_nothing_has_been_minted() -> None:
    """The pre-mint call passes no secret, and that is deliberate rather than an oversight.

    `CheckoutProvider.checkout` checks the offer's own `checkout_url` BEFORE calling `mint`,
    so at that point no code exists anywhere in the world and there is nothing to keep out
    of the message. Redacting there would cost the diagnostic and buy nothing — and every
    existing caller of `assert_on_domain`/`is_on_domain` relies on the full message.
    """
    rival = f"https://{RIVAL_DOMAIN}/cart/1:1?discount=WHATEVER"
    with pytest.raises(OffDomainCheckout) as raised:
        assert_on_domain(rival, SELLER_DOMAIN, what="offer checkout_url")

    message = str(raised.value)
    assert RIVAL_DOMAIN in message, "the pre-mint refusal must still name the refused host"
    assert rival in message, (
        f"the pre-mint refusal must still quote the URL it refused: {message!r}"
    )
    assert "redacted" not in message, (
        f"nothing was minted, so nothing should have been redacted: {message!r}"
    )


@pytest.mark.parametrize(
    "case",
    [s for s in SPELLINGS if not s.host_survives] + [SPELLINGS[8], SPELLINGS[9]],
    ids=lambda s: s.label,
)
def test_the_c_level_excepthook_cannot_print_a_host_or_port_borne_code(case: Spelling) -> None:
    """The excepthook channel, re-measured against the shapes pass 2 could not survive.

    `test_the_c_level_excepthook_cannot_print_the_code` above covers a code in the query.
    These are the ones that reach it by a different route: spelled in the HOST, which
    `redact_url` deliberately keeps, and behind a non-numeric PORT, which used to abort the
    whole refusal before any exception object existed to print.
    """
    merchant = OffDomainMerchant(case.code, case.permalink)
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
        assert exc.orphan.code == case.code, "the payload must still hold the real code"
        assert_code_is_unrecoverable(
            rendered, case.code, f"[{case.label}] the default sys.excepthook output"
        )
        # ...and the traceback rendered through Python-level attribute lookup too.
        assert_code_is_unrecoverable(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            case.code,
            f"[{case.label}] traceback.format_exception",
        )
    else:  # pragma: no cover - the checkout must refuse
        raise AssertionError(f"[{case.label}] the off-domain permalink was not refused")


def test_a_merchant_reply_with_no_code_field_is_not_dumped_into_the_refusal() -> None:
    """The sweep's own finding: the one path where redaction is impossible in principle.

    `ShopifyCheckoutProvider.mint` refuses a reply that does not spell its code under
    ``code`` — and it used to put ``{reply!r}`` in the refusal to say what it got. That
    branch is by definition "the code is not where we look", so a merchant answering
    ``{"discount_code": "…"}`` had a live discount dumped verbatim into `denial_reason` and
    the persisted `policy_event`, with no value available to redact BY. The keys and the
    type are the diagnostic; the values are not.
    """
    live_code = "PSX-UNDECLARED-3"

    class WrongKeyMerchant:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def create_code(self, store_id: Any, offer: Any) -> dict[str, str]:
            self.calls.append(str(store_id))
            return {"discount_code": live_code, "url": f"https://{RIVAL_DOMAIN}/cart"}

        __call__ = create_code

    live = auction("bid-a", "bid-b")
    merchant = WrongKeyMerchant()
    result = accept(live, "bid-a", merchant, "shopify")

    assert merchant.calls, "the merchant must actually have been called"
    assert result.accepted is False
    reason = result.denial_reason or ""
    assert_code_is_unrecoverable(reason, live_code, "denial_reason for an unusable reply")
    assert_code_is_unrecoverable(
        json.dumps(event_of(result, "policy_event"), default=str),
        live_code,
        "policy_event for an unusable reply",
    )
    # The diagnostic an operator needs — WHICH key the merchant used — must survive.
    assert "discount_code" in reason, (
        f"the refusal no longer says which keys the merchant's reply carried: {reason!r}"
    )


# =====================================================================================
# T-215 (f) — the OTHER two post-mint prose sites, and the identifier fields
#
# The sweep found four places that format merchant input into a message a live code can
# reach, not one. `assert_on_domain` is the one that leaked; these three are the ones that
# were a spelling away from it, and they are gated here so "guarded" is a measurement rather
# than a claim. Each uses an encoding the literal blacklist layer cannot match — a punycode
# domain — because a guard whose only test case is the exact spelling is a guard whose
# removal nothing notices.
# =====================================================================================
#: `PSX-Ω-42` in ACE form. Contains no substring of the code; only decoding recovers it.
PUNY_CODE = "PSX-Ω-42"
PUNY_SPELLING = "xn--psx--42-bkf"


class OnceThenForgetful:
    """A registry that answers the first lookup and has never heard of the store after that.

    Not contrived: `ShopifyCheckoutProvider.mint` resolves the registered domain a SECOND
    time inside `default_permalink`, after the merchant has minted, and its own comment
    names this exact case — "a lookup that answers once and fails once (a dropped
    connection, a cache eviction) is all it takes".
    """

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self.calls = 0

    def domain_for(self, store_id: str) -> str | None:
        self.calls += 1
        return self.domain if self.calls == 1 else None


def test_a_post_mint_failure_inside_the_adapter_cannot_publish_the_code() -> None:
    """`providers.py` formats `{exc}` and `store_id` into its orphan refusal.

    `{exc}` is an arbitrary exception built by code this package does not own — here
    `registered_domain_for`, which quotes the bid's own `store_domain` claim. A store is
    free to make that claim a spelling of the code it is about to mint, and the literal
    blacklist downstream cannot match a punycode one.
    """
    registry = OnceThenForgetful(SELLER_DOMAIN)

    class NoPermalinkMerchant:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def create_code(self, store_id: Any, offer: Any) -> dict[str, str]:
            self.calls.append(str(store_id))
            return {"code": PUNY_CODE}  # no permalink: the adapter must build one

        __call__ = create_code

    merchant = NoPermalinkMerchant()
    with pytest.raises(OrphanedCheckoutCode) as raised:
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                # The bid's own claim about its domain, quoted verbatim by the second
                # lookup's failure message.
                store_domain=f"{PUNY_SPELLING}.example.com",
                offer=offer(SELLER_DOMAIN),
                mode="shopify",
                code_creator=merchant,
                now=T_NOW,
                registered_domains=registry,
            )
        )

    exc = raised.value
    assert merchant.calls, "the merchant must actually have minted"
    assert registry.calls >= 2, "the second, post-mint lookup did not happen"
    # T-202: the code is still carried out to whoever can revoke it.
    assert exc.orphan.code == PUNY_CODE
    assert_code_is_unrecoverable(str(exc), PUNY_CODE, "str() of an adapter post-mint orphan")
    assert_code_is_unrecoverable(repr(exc), PUNY_CODE, "repr() of an adapter post-mint orphan")
    # The C-level excepthook FIRST: `traceback.format_exception` reads `__cause__` through
    # the redacting property, which mutates the chain in place, so rendering it first would
    # clean the chain before the excepthook — the one reader that bypasses that property —
    # ever saw it, and this assertion would be measuring the property instead.
    assert_code_is_unrecoverable(
        excepthook_output(exc), PUNY_CODE, "the C-level excepthook, adapter post-mint orphan"
    )
    assert_code_is_unrecoverable(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        PUNY_CODE,
        "the traceback of an adapter post-mint orphan",
    )
    assert code_fingerprint(PUNY_CODE) in str(exc), "the joinable handle must survive"


class ShiftingOffer(dict):
    """An offer whose ``expires_at`` is fine when the port validates it and hostile after.

    The port runs `assert_offer_is_mintable` BEFORE the mint precisely so a malformed
    `expires_at` cannot reach `code_expiry` with a live code behind it. That ordering is
    correct and is not what this exercises: a `Mapping` is a merchant's own JSON-shaped
    object, and "the value I validated is the value I will read next" is an assumption, not
    a guarantee. `provider.checkout` wraps the result-building step for this reason — its
    own comment says "cannot normally fail is exactly the assumption that produced this
    ticket" — and this is the case that reaches that handler.
    """

    def __init__(self, hostile: str) -> None:
        super().__init__(
            product_ref="product-1", unit_price=100.0, total_price=100.0, expires_at=T_FUTURE
        )
        self.hostile = hostile
        self.reads = 0

    def get(self, key: str, default: Any = None) -> Any:
        if key == "expires_at":
            self.reads += 1
            return T_FUTURE if self.reads == 1 else self.hostile
        return super().get(key, default)


def test_a_post_mint_failure_building_the_result_cannot_publish_the_code() -> None:
    """`provider.checkout`'s own last-resort handler formats `{exc}` too.

    Reached here by `code_expiry` raising while the `CheckoutResult` is assembled — after
    the permalink has already passed the host check, so this is a refusal with a live code
    and an on-domain permalink, the one shape neither `redact_url` nor the host guard sees.
    `UnusableOffer` quotes the offending `expires_at`, and that value is the merchant's.
    """

    class PlainProvider(CheckoutProvider):
        name = "test-plain"

        def mint(self, request: CheckoutRequest) -> MintedCheckout:
            # On-domain permalink, and NO expires_at — so the port computes one itself from
            # the offer, which is the read that fails.
            return MintedCheckout(
                code=PUNY_CODE,
                permalink_url=f"https://{SELLER_DOMAIN}/cart/1:1",
                expires_at=None,
            )

    hostile = ShiftingOffer(f"{PUNY_SPELLING}.example.com")
    with pytest.raises(OrphanedCheckoutCode) as raised:
        PlainProvider().checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=hostile,
                mode="redirect",
                now=T_NOW,
                registered_domains=StaticRegisteredDomains({"store-a": SELLER_DOMAIN}),
            )
        )

    exc = raised.value
    assert hostile.reads >= 2, "the post-mint read of the offer did not happen"
    assert exc.orphan.code == PUNY_CODE, "T-202: the orphan must still carry the live code"
    assert_code_is_unrecoverable(str(exc), PUNY_CODE, "str() of a result-building orphan")
    # The C-level excepthook FIRST: `traceback.format_exception` reads `__cause__` through
    # the redacting property, which mutates the chain in place, so rendering it first would
    # clean the chain before the excepthook — the one reader that bypasses that property —
    # ever saw it, and this assertion would be measuring the property instead.
    assert_code_is_unrecoverable(
        excepthook_output(exc), PUNY_CODE, "the C-level excepthook, result-building orphan"
    )
    assert_code_is_unrecoverable(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        PUNY_CODE,
        "the traceback of a result-building orphan",
    )
    assert code_fingerprint(PUNY_CODE) in str(exc), "the joinable handle must survive"


def test_the_orphans_identifier_fields_cannot_spell_the_code_either() -> None:
    """`store_id`, `auction_id` and `bid_ref` are read off the bid — a merchant's own reply.

    `accept()` takes `store_id` as `str(_read(bid, "store_id"))`, so nothing stops a store
    naming itself after the discount it is about to mint. The `repr` is the surface that
    matters: `AcceptResult` holds an `OrphanedCode` and one `logger.debug("%r", result)`
    publishes whatever it renders.
    """
    orphan = OrphanedCode(
        code=PUNY_CODE,
        permalink_url=f"https://{RIVAL_DOMAIN}/cart/1:1",
        provider="shopify",
        store_id=f"{PUNY_SPELLING}.example.com",
        auction_id=f"auction-{PUNY_SPELLING}",
        bid_ref=f"bid-{PUNY_SPELLING}",
    )

    assert orphan.code == PUNY_CODE, "explicit field access is how the revoker reads it"
    assert_code_is_unrecoverable(repr(orphan), PUNY_CODE, "repr() of an orphan with hostile ids")
    assert_code_is_unrecoverable(f"{orphan}", PUNY_CODE, "an f-string of it")
    assert code_fingerprint(PUNY_CODE) in repr(orphan), "the joinable handle must survive"


class SelfRenderingFailure(Exception):
    """A cause whose message comes from its own fields, not from ``args``.

    Not exotic: ``OSError`` does this, and so does a great deal of client-library code — and
    a provider is explicitly allowed to delegate to a library this repo has never seen. It
    matters because BOTH of the mechanisms that redact a chained exception work by rewriting
    ``args``: ``_redact_chain`` mutates them in place (which is what closes the C-level
    excepthook) and the ``__cause__`` property re-runs the same rewrite on the way out.
    Neither can touch a ``__str__`` that ignores ``args`` entirely.
    """

    def __init__(self, detail: str) -> None:
        super().__init__("a failure occurred")  # the args say nothing
        self.detail = detail

    def __str__(self) -> str:  # ...and the rendering says everything
        return f"upstream refused: {self.detail}"


def test_a_cause_that_renders_itself_cannot_publish_the_code() -> None:
    """The channel neither ``args`` rewrite can reach, closed by not chaining that object.

    Both published surfaces AND both traceback renderers are asserted, because this is the
    shape where they disagree: ``str(orphan_exc)`` is built by this package and is safe by
    construction, while the traceback and the C-level excepthook render the CAUSE, which is
    not.
    """

    # The mint itself failing creates no orphan (nothing was minted), so this routes the
    # self-rendering failure through the post-mint handler the way a real adapter does:
    # mint succeeds, and the step that assembles the result raises.
    class LateFailureProvider(CheckoutProvider):
        name = "test-late-failure"

        def mint(self, request: CheckoutRequest) -> MintedCheckout:
            return MintedCheckout(
                code=PUNY_CODE,
                permalink_url=f"https://{SELLER_DOMAIN}/cart/1:1",
                expires_at=None,
            )

    class ExplodingOffer(dict):
        def __init__(self) -> None:
            super().__init__(product_ref="p", unit_price=1.0, total_price=1.0)
            self.reads = 0

        def get(self, key: str, default: Any = None) -> Any:
            if key == "expires_at":
                self.reads += 1
                if self.reads > 1:
                    # Spelled in punycode, so the literal-spelling layer cannot match it:
                    # what has to catch this is the guard at the site that FORMATS `{exc}`.
                    raise SelfRenderingFailure(
                        f"the discount at {PUNY_SPELLING}.example.com is already redeemed"
                    )
                return None
            return super().get(key, default)

    hostile = ExplodingOffer()
    with pytest.raises(OrphanedCheckoutCode) as raised:
        LateFailureProvider().checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=hostile,
                mode="redirect",
                now=T_NOW,
                registered_domains=StaticRegisteredDomains({"store-a": SELLER_DOMAIN}),
            )
        )

    exc = raised.value
    assert hostile.reads >= 2, "the post-mint read did not happen; the test proves nothing"
    assert exc.orphan.code == PUNY_CODE, "T-202: the orphan must still carry the live code"
    assert_code_is_unrecoverable(str(exc), PUNY_CODE, "str() of a self-rendering-cause orphan")

    # The excepthook FIRST — see the note in the adapter test: rendering the traceback runs
    # the redacting `__cause__` property, which would clean the chain before the C-level
    # reader that cannot use that property ever looked at it.
    assert_code_is_unrecoverable(
        excepthook_output(exc), PUNY_CODE, "the C-level excepthook with a self-rendering cause"
    )
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert_code_is_unrecoverable(rendered, PUNY_CODE, "traceback with a self-rendering cause")

    # The chain must survive as a chain — a redaction that severed `__cause__` would take
    # the "why" with it — and it must still name the type that failed.
    assert exc.__cause__ is not None, "the chained cause was dropped rather than replaced"
    assert "SelfRenderingFailure" in str(exc.__cause__), (
        f"the stand-in no longer says what type failed: {str(exc.__cause__)!r}"
    )
    assert code_fingerprint(PUNY_CODE) in str(exc), "the joinable handle must survive"


def test_a_self_rendering_failure_inside_the_adapter_cannot_publish_the_code() -> None:
    """The same channel, on the adapter's own post-mint handler rather than the port's.

    `ShopifyCheckoutProvider.mint` has its own `except Exception` — it has to, because the
    region between the merchant's answer and the return is where the code exists and the
    port cannot see it — and it formats `{exc}` into its refusal exactly as the port does.
    Two handlers, one shape, and a fix applied to only one of them is a fix that a provider
    with no permalink walks straight around.
    """

    class LateQuantityFailure(dict):
        """An offer whose `quantity` reads fine for the pre-mint check and then explodes.

        `default_permalink` — reached only when the merchant returns a code with no
        permalink — calls `offer_quantity` a second time, after the mint.
        """

        def __init__(self) -> None:
            super().__init__(product_ref="p", unit_price=1.0, total_price=1.0)
            self.reads = 0

        def get(self, key: str, default: Any = None) -> Any:
            if key == "quantity":
                self.reads += 1
                if self.reads > 1:
                    raise SelfRenderingFailure(
                        f"quantity for {PUNY_SPELLING}.example.com is not orderable"
                    )
                return 1
            return super().get(key, default)

    class NoPermalinkMerchant:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def create_code(self, store_id: Any, offer: Any) -> dict[str, str]:
            self.calls.append(str(store_id))
            return {"code": PUNY_CODE}

        __call__ = create_code

    hostile = LateQuantityFailure()
    merchant = NoPermalinkMerchant()
    with pytest.raises(OrphanedCheckoutCode) as raised:
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=hostile,
                mode="shopify",
                code_creator=merchant,
                now=T_NOW,
                registered_domains=StaticRegisteredDomains({"store-a": SELLER_DOMAIN}),
            )
        )

    exc = raised.value
    assert merchant.calls, "the merchant must actually have minted"
    assert hostile.reads >= 2, "the post-mint read did not happen; the test proves nothing"
    assert exc.orphan.code == PUNY_CODE, "T-202: the orphan must still carry the live code"
    assert_code_is_unrecoverable(str(exc), PUNY_CODE, "str() of an adapter self-rendering orphan")
    # Excepthook first — see the note in the adapter test above.
    assert_code_is_unrecoverable(
        excepthook_output(exc), PUNY_CODE, "the C-level excepthook, adapter self-rendering orphan"
    )
    assert_code_is_unrecoverable(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        PUNY_CODE,
        "the traceback of an adapter self-rendering orphan",
    )
    assert code_fingerprint(PUNY_CODE) in str(exc), "the joinable handle must survive"


def test_a_safe_cause_is_chained_unchanged() -> None:
    """The control for the stand-in: an ordinary cause must NOT be replaced.

    Substituting the chain is a real loss of information, so it must happen only when the
    original cannot be made safe. A refusal whose cause says nothing about the code keeps
    that cause, its type and its message.
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

    cause = raised.value.__cause__
    assert isinstance(cause, OffDomainCheckout), (
        f"an ordinary off-domain cause was replaced rather than kept: {cause!r}"
    )
    assert not isinstance(cause, RedactedCause), "the stand-in fired on a safe cause"
    assert RIVAL_DOMAIN in str(cause), "the cause no longer names the bad host"


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


# =====================================================================================
# T-215 (f) — the ORACLE has to be the renderer, not `str(exc)`
#
# Four passes have now been defeated by "a string nobody looked at": the query, the host,
# the non-numeric port's ValueError, and — here — PEP-678 notes and ExceptionGroup members.
# The common shape is not the attribute. It is that the decision "is this exception safe to
# chain?" was taken on `str(exc)`, while the surface being protected is what a TRACEBACK
# PRINTS, and the renderer prints strictly more than `str`:
#
#   * `exc.add_note(...)` appends to `__notes__`, which `format_exception_only` prints on
#     its own lines and `str(exc)` has never contained. `add_note` is ordinary in HTTP
#     client stacks — it is how a library says WHICH request failed, and a cart permalink is
#     exactly the sort of thing that goes in one.
#   * `str(ExceptionGroup(...))` is the summary line and the member COUNT
#     (`"two link attempts failed (2 sub-exceptions)"`); the renderer prints every member's
#     own message. `asyncio.TaskGroup` and `anyio` raise these routinely, and `.exceptions`
#     is not `__cause__`, so walking the chain never reached them either.
#
# Both were measured leaking through `sys.__excepthook__` AND `traceback.format_exception`
# while `str(exc)`, `denial_reason` and the persisted `policy_event` were all clean — so all
# 380 tests stayed green. Enumerating a fifth attribute would lose to whatever CPython
# renders next; these tests pin the oracle to the renderer instead.
# =====================================================================================
ORACLE_CODE = "PSX-RENDER-3F"


class RenderOracleProvider(CheckoutProvider):
    """Mints cleanly on-domain, so the failure below is a genuinely post-mint one."""

    name = "test-render-oracle"

    def mint(self, request: CheckoutRequest) -> MintedCheckout:
        return MintedCheckout(
            code=ORACLE_CODE,
            permalink_url=f"https://{SELLER_DOMAIN}/cart/1:1",
            expires_at=None,
        )


class RaisingOffer(dict):
    """An offer that reads cleanly for the pre-mint check and raises after the mint.

    The port validates the offer's fields BEFORE minting, which is correct and is not what
    is under test here: this is the "the value I validated is the value I will read next"
    assumption, and `provider.checkout` wraps the result-building step precisely because it
    is an assumption. The second read is the one that happens with a live code behind it.
    """

    def __init__(self, make_exc: Any) -> None:
        super().__init__(product_ref="p", unit_price=1.0, total_price=1.0)
        self.make_exc = make_exc
        self.reads = 0

    def get(self, key: str, default: Any = None) -> Any:
        if key == "expires_at":
            self.reads += 1
            if self.reads > 1:
                raise self.make_exc()
            return None
        return super().get(key, default)


def post_mint_orphan(make_exc: Any) -> OrphanedCheckoutCode:
    """Drive one post-mint refusal whose CAUSE is ``make_exc()``, and hand back the orphan."""
    hostile = RaisingOffer(make_exc)
    with pytest.raises(OrphanedCheckoutCode) as raised:
        RenderOracleProvider().checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=hostile,
                mode="redirect",
                now=T_NOW,
                registered_domains=StaticRegisteredDomains({"store-a": SELLER_DOMAIN}),
            )
        )
    assert hostile.reads >= 2, "the post-mint read did not happen; the test proves nothing"
    return raised.value


def assert_no_renderer_publishes(exc: OrphanedCheckoutCode, code: str, label: str) -> None:
    """Every reader of this exception, with the C-level one asserted FIRST.

    The order is load-bearing and is the reason a previous version of this assertion could
    have been vacuous. `traceback.format_exception` reaches `__cause__` through
    `OrphanedCheckoutCode`'s redacting property, and that property MUTATES the chain in
    place (and, since pass 4, writes a substituted stand-in back into the C-level slot). So
    rendering the traceback first would clean the chain before `sys.__excepthook__` — the
    one reader that bypasses the property entirely, reading the C slot through
    `PyException_GetCause` — ever looked at it, and the excepthook assertion would then be
    measuring the property rather than the channel it cannot use.
    """
    assert_code_is_unrecoverable(
        excepthook_output(exc), code, f"[{label}] the C-level sys.excepthook"
    )
    assert_code_is_unrecoverable(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        code,
        f"[{label}] traceback.format_exception",
    )
    assert_code_is_unrecoverable(str(exc), code, f"[{label}] str() of the refusal")
    assert_code_is_unrecoverable(repr(exc), code, f"[{label}] repr() of the refusal")
    assert exc.orphan.code == code, f"[{label}] T-202: the orphan must still carry the code"
    assert code_fingerprint(code) in str(exc), f"[{label}] the joinable handle must survive"


def _note_cause() -> BaseException:
    exc = RuntimeError("link build failed")
    exc.add_note(f"upstream permalink was https://a.tld/cart?discount={ORACLE_CODE}")
    return exc


def _group_cause() -> BaseException:
    return ExceptionGroup(
        "two link attempts failed",
        [
            RuntimeError(f"link build failed for ?discount={ORACLE_CODE}"),
            RuntimeError("the second attempt timed out"),
        ],
    )


def _nested_group_cause() -> BaseException:
    """A group inside a group, with the code on a NOTE of an inner member.

    The sweep's shape rather than the measured one: nothing here is reached by walking
    `__cause__`, and nothing here is in any `args` — the member is two containers deep and
    the code is on its `__notes__`. An oracle that is the renderer covers it without being
    told it exists; an oracle that enumerates attributes has to have thought of it.
    """
    inner = RuntimeError("the retry also failed")
    inner.add_note(f"cart was https://a.tld/discount/{ORACLE_CODE}")
    return ExceptionGroup(
        "outer",
        [ExceptionGroup("inner", [inner, ValueError("unrelated")]), TimeoutError("slow")],
    )


def _noted_group_cause() -> BaseException:
    """A note on the GROUP itself — the container, not a member."""
    group = ExceptionGroup("attempts failed", [RuntimeError("first"), RuntimeError("second")])
    group.add_note(f"the discount in play was {ORACLE_CODE}")
    return group


def _self_rendering_with_note() -> BaseException:
    """Both channels at once: a `__str__` that ignores args AND a note.

    Pass 3 closed the first by substituting the object. This proves the substitution is
    driven by the render rather than by `__str__` — the note alone would be enough.
    """
    exc = SelfRenderingFailure("nothing interesting")
    exc.add_note(f"context: ?discount={ORACLE_CODE}")
    return exc


class LazilyNotedFailure(Exception):
    """A cause whose ``__notes__`` are COMPUTED, not stored.

    The note analogue of :class:`SelfRenderingFailure`, and natural for the same reason: a
    client library that wants to say which request failed can build that line when it is
    asked for rather than when it raises. PEP 678 fixes what ``add_note`` does; it does not
    make ``__notes__`` a plain list, and every renderer reads it by ordinary attribute
    lookup.

    It is here because it is the shape the in-place rewrite CANNOT fix: redacting the list
    it returns edits a temporary, so the object still renders the original. Only an oracle
    that asks the renderer sees it — which is the whole claim of this section.
    """

    def __init__(self, detail: str) -> None:
        super().__init__("a failure occurred")  # the args say nothing
        self.detail = detail

    @property
    def __notes__(self) -> list[str]:  # type: ignore[override]
        return [f"while fetching {self.detail}"]


def _lazy_note_cause() -> BaseException:
    return LazilyNotedFailure(f"https://a.tld/cart?discount={ORACLE_CODE}")


def _group_of_self_rendering_cause() -> BaseException:
    """A group whose MEMBER renders itself — args rewriting cannot reach it either.

    ``asyncio.TaskGroup`` wrapping an ``OSError`` (whose ``__str__`` is built from
    ``errno``/``strerror``/``filename``, not from ``args``) is this shape verbatim. Neither
    half of the in-place walk helps: the member is behind ``.exceptions`` rather than
    ``__cause__``, and its message is behind ``__str__`` rather than ``args``.
    """
    return ExceptionGroup(
        "two link attempts failed",
        [SelfRenderingFailure(f"?discount={ORACLE_CODE}"), TimeoutError("slow")],
    )


RENDER_ORACLE_CAUSES: list[tuple[str, Any]] = [
    ("pep678-note-on-the-cause", _note_cause),
    ("exception-group-member", _group_cause),
    ("note-on-a-member-of-a-nested-group", _nested_group_cause),
    ("note-on-the-group-itself", _noted_group_cause),
    ("self-rendering-cause-with-a-note", _self_rendering_with_note),
    # The two below are the ones ONLY the render-faithful oracle closes. Everything above
    # is also reachable by rewriting `args`/`__notes__` in place, so a gate made only of
    # those measures the backstop and reports the oracle.
    ("lazily-computed-notes", _lazy_note_cause),
    ("self-rendering-member-of-a-group", _group_of_self_rendering_cause),
]


def test_the_oracle_reads_what_the_renderer_prints_not_what_str_returns() -> None:
    """The falsifiability control for the whole section, and the reason it exists.

    If `rendered_exception` ever regresses to `str(exc)` — or to any proxy for it — this
    fails first and says why, rather than the leak being found by a sixth verifier. Each
    case below is a string the RENDERER prints and `str()` does not contain, so the two
    assertions together pin the gap the oracle exists to close.
    """
    noted = RuntimeError("plain")
    noted.add_note("NOTE-VISIBLE-ONLY-TO-THE-RENDERER")
    assert "NOTE-VISIBLE-ONLY-TO-THE-RENDERER" not in str(noted), (
        "str() suddenly includes __notes__; the premise of this section has changed"
    )
    assert "NOTE-VISIBLE-ONLY-TO-THE-RENDERER" in rendered_exception(noted), (
        "the oracle cannot see a PEP-678 note, so it disagrees with every renderer"
    )

    group = ExceptionGroup("summary", [RuntimeError("MEMBER-VISIBLE-ONLY-TO-THE-RENDERER")])
    assert "MEMBER-VISIBLE-ONLY-TO-THE-RENDERER" not in str(group), (
        "str() suddenly includes group members; the premise of this section has changed"
    )
    assert "MEMBER-VISIBLE-ONLY-TO-THE-RENDERER" in rendered_exception(group), (
        "the oracle cannot see an ExceptionGroup member, so it disagrees with every renderer"
    )

    # ...and it agrees with the renderer it is standing in for, on both.
    for exc in (noted, group):
        printed = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        for line in rendered_exception(exc).splitlines():
            assert line.strip() in printed, (
                f"the oracle reports {line.strip()!r}, which the renderer does not print — "
                f"an oracle that is not the renderer is the defect this section is about"
            )

    # Totality: the oracle is fed merchant-shaped exceptions on a refusal path.
    assert rendered_exception(None) == ""
    assert isinstance(rendered_exception(RuntimeError()), str)


@pytest.mark.parametrize(
    ("label", "make_exc"), RENDER_ORACLE_CAUSES, ids=[c[0] for c in RENDER_ORACLE_CAUSES]
)
def test_no_renderable_attribute_of_a_chained_cause_can_publish_the_code(
    label: str, make_exc: Any
) -> None:
    """Measured leaking at a5a41a7 through BOTH renderers, with every other surface clean."""
    assert_no_renderer_publishes(post_mint_orphan(make_exc), ORACLE_CODE, label)


@pytest.mark.parametrize(
    ("label", "make_exc"), RENDER_ORACLE_CAUSES, ids=[c[0] for c in RENDER_ORACLE_CAUSES]
)
def test_no_renderable_attribute_reaches_the_published_surfaces(label: str, make_exc: Any) -> None:
    """The same causes, driven through `accept()` to the two surfaces that PUBLISH.

    The exception-level assertions above are the sharp ones; these are the ones that say why
    anybody cares. `denial_reason` goes back to the client and the `policy_event` is
    persisted with its reason typed as a bare string by the published OpenAPI.
    """

    class NotedMerchant:
        """Mints, then fails while the adapter builds the permalink, carrying `make_exc()`."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def create_code(self, store_id: Any, offer: Any) -> dict[str, str]:
            self.calls.append(str(store_id))
            return {"code": ORACLE_CODE, "permalink_url": f"https://{RIVAL_DOMAIN}/cart/1:1"}

        __call__ = create_code

    live = auction("bid-a", "bid-b")
    # The offer's own expiry read raises after the mint, so the failure is post-mint and its
    # cause is the shape under test.
    live["bids"][0]["offer"] = RaisingOffer(make_exc)
    merchant = NotedMerchant()

    result = accept(live, "bid-a", merchant, "shopify")

    assert merchant.calls, f"[{label}] the merchant must actually have minted"
    assert result.accepted is False, f"[{label}] a post-mint failure was accepted"
    reason = result.denial_reason or ""
    assert reason, f"[{label}] a refusal with no reason is not debuggable"
    assert_code_is_unrecoverable(reason, ORACLE_CODE, f"[{label}] the denial_reason")
    assert_code_is_unrecoverable(
        json.dumps(event_of(result, "policy_event"), default=str),
        ORACLE_CODE,
        f"[{label}] the persisted policy_event",
    )
    # T-202: the code is still recorded and still revocable.
    assert "code_created" in kinds(result), (
        f"[{label}] the merchant minted and nothing recorded it: {kinds(result)}"
    )
    assert event_of(result, "code_created")["payload"]["code"] == ORACLE_CODE
    assert result.orphaned_code is not None and result.orphaned_code.code == ORACLE_CODE


def test_a_visible_context_cannot_publish_the_code() -> None:
    """`__suppress_context__` is not a lock, so the context must be safe on its own.

    `raise X from cause` sets `__suppress_context__ = True`, and CPython's renderer skips
    `__context__` whenever a `__cause__` is present — which is why this channel is quiet
    today. Both of those are properties of how the refusal happens to be raised, not of the
    redaction, and the object sitting in `__context__` is the ORIGINAL exception rather than
    the stand-in that replaced it as the cause. So the flag is flipped here deliberately: if
    a future change stops chaining a cause, this is what fails.
    """
    exc = post_mint_orphan(_note_cause)
    assert exc.__suppress_context__ is True, "raise ... from no longer suppresses the context"
    exc.__suppress_context__ = False
    assert_no_renderer_publishes(exc, ORACLE_CODE, "context-visible")


def test_the_render_oracle_terminates_on_a_cyclic_chain() -> None:
    """Totality again: the oracle renders a CHAIN, and a chain can be a cycle.

    Nothing in this repo builds one, and a merchant's client library is not obliged to be
    careful. An oracle that renders a cycle forever turns a refusal into a hang, which is
    strictly worse than the leak it was added to close.
    """
    first = RuntimeError(f"?discount={ORACLE_CODE}")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__cause__ = first  # the cycle

    rendered = rendered_exception(first)  # must terminate
    assert isinstance(rendered, str)
    assert ORACLE_CODE in rendered, "the oracle must still SEE the code it terminates on"


# =====================================================================================
# T-215 (g) — the `store_id` guard, which had no test at all
#
# Mutation V15: replacing `safe_token(request.store_id, minted.code, label='store')!r` with
# `request.store_id!r` at BOTH post-mint handlers left all 380 tests green. `store_id` is
# read straight off the bid (`accept()` does `str(_read(bid, "store_id"))`), so a store is
# free to name itself after the discount it is about to mint — and a fully percent-encoded
# spelling is not one of the three literal spellings the boundary blacklist replaces.
#
# The surface asserted for the persisted event is `payload.reason` — the PROSE, which is
# what the published OpenAPI types as a bare string. The event's top-level `store_id` field
# is the store's own name verbatim by design: it is the ledger's join key, written
# identically on the `code_created` record that legitimately holds the live code. Redacting
# an identifier that every event in the auction is keyed by would break the join and hide
# nothing, since the code is in that same event stream by construction.
# =====================================================================================
STORE_CODE = "PSX-STORE-11"
#: Every character percent-encoded. `redact_code`'s literal layer replaces `raw`,
#: `quote(raw, safe="")` and `quote(raw)` — for a code of only URL-safe characters all three
#: ARE the raw spelling, so this form is one the boundary cannot match and only the
#: build-site guard removes.
STORE_ID_SPELLING = "".join(f"%{ord(c):02X}" for c in STORE_CODE)


def policy_reason(result: Any) -> str:
    return str(event_of(result, "policy_event")["payload"]["reason"])


def test_a_store_id_that_spells_the_code_is_not_published_by_the_host_check_handler(
    unwired: None,
) -> None:
    """`provider.checkout`'s post-mint host-check handler formats `store_id` into its prose."""
    live = auction("bid-a", "bid-b")
    live["bids"][0]["store_id"] = STORE_ID_SPELLING
    merchant = OffDomainMerchant(STORE_CODE, f"https://{RIVAL_DOMAIN}/cart/1:1")

    result = accept(live, "bid-a", merchant, "shopify")

    assert merchant.calls, "the merchant must actually have minted"
    assert result.accepted is False
    assert_code_is_unrecoverable(
        result.denial_reason or "", STORE_CODE, "denial_reason with a code-spelling store_id"
    )
    assert_code_is_unrecoverable(
        policy_reason(result), STORE_CODE, "the persisted policy_event's reason"
    )
    # ...and the refusal is still the diagnostic it was.
    assert code_fingerprint(STORE_CODE) in (result.denial_reason or "")
    assert RIVAL_DOMAIN in (result.denial_reason or ""), "the refused host must survive"
    # T-202 is untouched.
    assert result.orphaned_code is not None and result.orphaned_code.code == STORE_CODE


def test_a_store_id_that_spells_the_code_is_not_published_by_the_adapter_handler() -> None:
    """`ShopifyCheckoutProvider.mint`'s own post-mint handler formats `store_id` too.

    Two handlers, one shape — a guard applied to only one of them is a guard a merchant that
    returns no permalink walks straight around.
    """
    registry = OnceThenForgetful(SELLER_DOMAIN)

    class NoPermalinkMerchant:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def create_code(self, store_id: Any, offer: Any) -> dict[str, str]:
            self.calls.append(str(store_id))
            return {"code": STORE_CODE}

        __call__ = create_code

    merchant = NoPermalinkMerchant()
    with pytest.raises(OrphanedCheckoutCode) as raised:
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id=STORE_ID_SPELLING,
                store_domain=SELLER_DOMAIN,
                offer=offer(SELLER_DOMAIN),
                mode="shopify",
                code_creator=merchant,
                now=T_NOW,
                registered_domains=registry,
            )
        )

    exc = raised.value
    assert merchant.calls, "the merchant must actually have minted"
    assert registry.calls >= 2, "the second, post-mint lookup did not happen"
    assert_no_renderer_publishes(exc, STORE_CODE, "adapter handler, code-spelling store_id")


def test_a_store_id_that_spells_the_code_is_not_published_by_the_result_handler() -> None:
    """...and `provider.checkout`'s last-resort result-building handler, the third site."""
    hostile = RaisingOffer(lambda: RuntimeError("assembling the result failed"))
    store_id = "".join(f"%{ord(c):02X}" for c in ORACLE_CODE)
    with pytest.raises(OrphanedCheckoutCode) as raised:
        RenderOracleProvider().checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id=store_id,
                store_domain=SELLER_DOMAIN,
                offer=hostile,
                mode="redirect",
                now=T_NOW,
                # The platform knows this store by the name the store chose, whatever that
                # name spells — the registry is keyed by store_id, not by taste.
                registered_domains=StaticRegisteredDomains({store_id: SELLER_DOMAIN}),
            )
        )

    assert hostile.reads >= 2, "the post-mint read did not happen"
    assert_no_renderer_publishes(
        raised.value, ORACLE_CODE, "result handler, code-spelling store_id"
    )


def test_the_chained_arg_guard_fails_closed_on_a_spelling_redact_code_cannot_reach() -> None:
    """Mutation M11: `_redact_arg` must CHECK its own output, not just best-effort it.

    `redact_code` is a blacklist plus a structural URL reduction; a chained cause is neither
    prose this package formatted nor a URL it holds, so a punycode spelling in some other
    module's message survives both layers. `_redact_arg` therefore re-checks what it
    produced and drops the ARGUMENT whole when the check still finds a code.

    **What is asserted is the diagnostic, and that is deliberate.** Since pass 4 the
    render-faithful oracle would catch this argument too — by replacing the entire cause
    with a stand-in. Safety is therefore no longer the thing this guard buys; PRECISION is.
    Dropping one argument keeps the cause's real type, its other arguments and its place in
    the chain, and an operator reading the refusal still learns that a domain lookup failed.
    Letting it fall through to the substitution costs all of that. So the test says what the
    guard is now for: the cause survives AS ITSELF, and carries nothing redeemable.
    """
    registry = OnceThenForgetful(SELLER_DOMAIN)

    class NoPermalinkMerchant:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def create_code(self, store_id: Any, offer: Any) -> dict[str, str]:
            self.calls.append(str(store_id))
            return {"code": PUNY_CODE}

        __call__ = create_code

    merchant = NoPermalinkMerchant()
    with pytest.raises(OrphanedCheckoutCode) as raised:
        resolve_provider("shopify").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                # Quoted verbatim by the failing second lookup's own message.
                store_domain=f"{PUNY_SPELLING}.example.com",
                offer=offer(SELLER_DOMAIN),
                mode="shopify",
                code_creator=merchant,
                now=T_NOW,
                registered_domains=registry,
            )
        )

    assert merchant.calls, "the merchant must actually have minted"
    cause = raised.value.__cause__
    assert cause is not None, "the chained cause was dropped rather than made safe"
    assert isinstance(cause, OffDomainCheckout) and not isinstance(cause, RedactedCause), (
        f"the offending ARGUMENT was not dropped, so the whole cause had to be replaced and "
        f"the refusal lost the type that says what failed: {cause!r}"
    )
    for index, arg in enumerate(cause.args):
        assert_code_is_unrecoverable(
            str(arg), PUNY_CODE, f"the chained cause's args[{index}] as an ARGUMENT"
        )


# =====================================================================================
# T-215 (h) — `{expected!r}`, the last unguarded fragment in `_reason`
#
# Every merchant fragment in that sentence goes through `safe_token` except the registered
# domain, which reads like the PLATFORM's value. On a call site that wired
# `CheckoutRequest.registered_domains` it is. On the DEFAULT unwired path —
# the legacy behaviour the frozen contract pins, and what the `unwired` fixture in this file
# exercises — `registered_domain_for` returns `request.store_domain`, read straight off the
# bid. It is then a THIRD independently normalised string (`.strip().lower().rstrip(".")`)
# that no boundary redactor holds as a value, reaching both published surfaces.
# =====================================================================================
def test_the_registered_domain_the_reason_quotes_is_guarded_at_its_build_site() -> None:
    """Measured at a5a41a7: recoverable straight out of `assert_on_domain`."""
    with pytest.raises(OffDomainCheckout) as raised:
        assert_on_domain(
            f"https://{RIVAL_DOMAIN}/cart",
            f"{PUNY_SPELLING}.example.com",
            what="permalink_url",
            secret=PUNY_CODE,
        )

    message = str(raised.value)
    assert_code_is_unrecoverable(message, PUNY_CODE, "the off-domain reason's registered domain")
    assert RIVAL_DOMAIN in message, "the refused host is the diagnostic and must survive"


def test_a_bid_authored_registered_domain_cannot_publish_the_code_end_to_end(
    unwired: None,
) -> None:
    """The same fragment on the path it is actually merchant-authored on.

    With no platform registry wired, the domain the refusal quotes as "the registered seller
    domain" is the bid's own `store_domain` claim — which is why the guard cannot be skipped
    on the grounds that the value is the platform's.
    """
    live = auction("bid-a", "bid-b")
    live["bids"][0]["store_domain"] = f"{PUNY_SPELLING}.example.com"
    merchant = OffDomainMerchant(PUNY_CODE, f"https://{RIVAL_DOMAIN}/cart/1:1")

    result = accept(live, "bid-a", merchant, "shopify")

    assert merchant.calls, "the merchant must actually have minted"
    assert result.accepted is False
    assert_code_is_unrecoverable(
        result.denial_reason or "", PUNY_CODE, "denial_reason with a bid-authored domain"
    )
    assert_code_is_unrecoverable(
        json.dumps(event_of(result, "policy_event"), default=str),
        PUNY_CODE,
        "the persisted policy_event with a bid-authored domain",
    )
    assert result.orphaned_code is not None and result.orphaned_code.code == PUNY_CODE


def test_the_pre_mint_reason_still_quotes_the_registered_domain_unchanged() -> None:
    """The control for the guard above: nothing minted means nothing redacted.

    `safe_token` with an empty secret returns its input untouched, so the pre-mint message
    every existing caller of `assert_on_domain`/`is_on_domain` reads is byte-identical.
    """
    with pytest.raises(OffDomainCheckout) as raised:
        assert_on_domain(f"https://{RIVAL_DOMAIN}/cart", SELLER_DOMAIN, what="offer checkout_url")

    message = str(raised.value)
    assert SELLER_DOMAIN in message, "the pre-mint refusal must still name the expected domain"
    assert RIVAL_DOMAIN in message, "...and the host it refused"
    assert "redacted" not in message, f"nothing was minted, so nothing should go: {message!r}"


# =====================================================================================
# T-202 (b) — the guarantee belongs to the PORT, not to the one adapter that remembered
#
# `CheckoutProvider.checkout` guards everything after `mint` RETURNS. But a code comes into
# existence in the MIDDLE of `mint`, and `mint`'s only channel back is its return value — so
# an exception raised between the mint and the return carries nothing and the refusal has no
# orphan. `SimulatedRedirectProvider` is D45's REQUIRED starting implementation and does
# exactly that: `mint_code()`, then `default_permalink()`, which resolves the registered
# domain a second time. Only `ShopifyCheckoutProvider` had closed this, with an `except` of
# its own — a guarantee one implementation remembered is not a guarantee.
# =====================================================================================
class OnceThenEvicted:
    """Answers the first lookup and raises on the second.

    Precisely the case `providers.py` documents as the reason its own handler exists — "a
    lookup that answers once and fails once (a dropped connection, a cache eviction)" — and
    it is the port, not that adapter, that has to survive it for every provider.
    """

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self.calls = 0

    def domain_for(self, store_id: str) -> str | None:
        self.calls += 1
        if self.calls == 1:
            return self.domain
        raise RuntimeError("registry cache evicted")


def simulated_request(registry: Any) -> CheckoutRequest:
    return CheckoutRequest(
        auction_id="auction-1",
        bid_ref="bid-a",
        store_id="store-a",
        store_domain=SELLER_DOMAIN,
        offer=offer(SELLER_DOMAIN),
        mode="redirect",
        now=T_NOW,
        registered_domains=registry,
    )


def test_the_simulated_provider_cannot_lose_a_code_when_the_registry_evicts() -> None:
    """Measured at a5a41a7: a bare `OffDomainCheckout`, and the minted code simply gone."""
    registry = OnceThenEvicted(SELLER_DOMAIN)

    with pytest.raises(OrphanedCheckoutCode) as raised:
        SimulatedRedirectProvider().checkout(simulated_request(registry))

    exc = raised.value
    assert registry.calls >= 2, "the second, post-mint lookup did not happen"
    assert exc.orphan.code.startswith("PSX-"), (
        f"T-202: the port did not carry the minted code out: {exc.orphan.code!r}"
    )
    assert exc.orphan.provider == "simulated-redirect", "the orphan must name who minted it"
    # It is still an off-domain refusal, so every existing `except OffDomainCheckout`
    # keeps catching it — the fix must not narrow what callers can catch.
    assert isinstance(exc, OffDomainCheckout), (
        f"the port turned an off-domain refusal into something else: {type(exc).__name__}"
    )
    # ...and the refusal publishes nothing the merchant could redeem.
    assert_no_renderer_publishes(exc, exc.orphan.code, "simulated provider, registry evicted")


def test_the_simulated_providers_orphan_is_recorded_end_to_end(unwired: None) -> None:
    """The half that makes T-202 mean something: `accept()` files the revocable record."""
    registry = OnceThenEvicted(SELLER_DOMAIN)
    live = auction("bid-a", "bid-b")

    result = accept(live, "bid-a", None, "redirect", registered_domains=registry)

    assert result.accepted is False, "a failed permalink build was accepted"
    assert registry.calls >= 2, "the second, post-mint lookup did not happen"
    assert "code_created" in kinds(result), (
        f"T-202: a code was minted and nothing recorded it. Events: {kinds(result)}. The "
        f"discount is live and the exchange cannot revoke it."
    )
    created = event_of(result, "code_created")
    assert created["payload"].get("orphaned") is True, (
        "the record must say this code belongs to a REFUSED checkout"
    )
    assert result.orphaned_code is not None, "the orphan never reached the caller"
    assert created["payload"]["code"] == result.orphaned_code.code
    assert result.orphaned_code.code.startswith("PSX-")
    # The refusal points at the record that holds the live code.
    pointer = event_of(result, "policy_event")["payload"].get("orphaned_code")
    assert isinstance(pointer, dict) and pointer.get("event_id") == created["event_id"]
    # A5: the buyer still gets the next slot rather than a stack trace.
    assert result.reoffer_bid_ref == "bid-b"


def test_a_provider_that_mints_nothing_still_refuses_without_an_orphan() -> None:
    """The control, and the reason the port cannot simply wrap `mint` in a blanket handler.

    A failure BEFORE anything is minted must stay exactly what it was. Inventing an orphan
    for it would file a `code_created` event for a discount that does not exist, which is a
    reconciler's nightmare in the opposite direction.
    """

    class MintsNothing(CheckoutProvider):
        name = "test-mints-nothing"

        def mint(self, request: CheckoutRequest) -> MintedCheckout:
            raise RuntimeError("the merchant is down")

    with pytest.raises(RuntimeError) as raised:
        MintsNothing().checkout(
            simulated_request(StaticRegisteredDomains({"store-a": SELLER_DOMAIN}))
        )

    assert not isinstance(raised.value, OrphanedCheckoutCode), (
        "the port invented an orphan for a checkout that minted nothing"
    )
    assert str(raised.value) == "the merchant is down", "the refusal shape changed"


def test_the_adapters_own_orphan_is_not_re_wrapped_by_the_port() -> None:
    """The other control: a provider that already carried its code out knows more than we do.

    `ShopifyCheckoutProvider` raises `OrphanedCheckoutCode` from inside `mint`, holding the
    merchant's permalink and its own account of the failure. The port must re-raise that
    untouched rather than replacing it with a poorer one built from the ledger.
    """
    registry = OnceThenForgetful(SELLER_DOMAIN)

    class NoPermalinkMerchant:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def create_code(self, store_id: Any, offer: Any) -> dict[str, str]:
            self.calls.append(str(store_id))
            return {"code": ORPHAN_CODE}

        __call__ = create_code

    merchant = NoPermalinkMerchant()
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
                registered_domains=registry,
            )
        )

    exc = raised.value
    assert merchant.calls, "the merchant must actually have minted"
    assert exc.orphan.code == ORPHAN_CODE
    assert exc.orphan.provider == "shopify", (
        f"the port replaced the adapter's own orphan: provider is {exc.orphan.provider!r}"
    )
    assert "mint()" not in str(exc), (
        f"the port re-wrapped an orphan the adapter had already built: {str(exc)!r}"
    )


# =====================================================================================
# T-215 (i) — the two mechanisms are not interchangeable, so both are pinned
#
# The redaction of a chained cause now has two layers, and a gate that only asserts "no
# code is recoverable" cannot tell them apart — either layer alone satisfies it, so removing
# EITHER leaves the suite green and the fix silently half-gone. The layers do different
# jobs and the difference is the diagnostic:
#
#   * `_redact_chain` rewrites `args`, `__notes__` and group members IN PLACE. When it
#     succeeds the cause survives as itself — its real type, its other arguments, its place
#     in the chain — and only the offending fragment is a placeholder.
#   * `_sanitised_cause` renders the exception the way a traceback does and, if anything is
#     still recoverable, replaces the WHOLE object with a `RedactedCause`. That is total,
#     and it costs the type and everything else the object was going to say.
#
# So these assert on WHICH outcome happened, not only that the code is gone.
# =====================================================================================
def test_a_note_that_can_be_rewritten_is_rewritten_rather_than_costing_the_cause() -> None:
    """A note quoting a permalink must be reduced in place, not answered by substitution."""
    exc = post_mint_orphan(_note_cause)
    cause = exc.__cause__

    assert cause is not None, "the cause was dropped entirely"
    assert not isinstance(cause, RedactedCause), (
        f"a note this package CAN rewrite cost the refusal its whole cause; the type that "
        f"failed and every other thing it said are gone: {cause!r}"
    )
    assert type(cause).__name__ == "RuntimeError", f"the cause changed type: {cause!r}"
    notes = list(getattr(cause, "__notes__", []))
    assert notes, "the note was deleted rather than redacted; the diagnostic is gone"
    for note in notes:
        assert_code_is_unrecoverable(str(note), ORACLE_CODE, "the rewritten note")
    assert any("upstream permalink" in str(n) for n in notes), (
        f"the note no longer says what it was about: {notes!r}"
    )


def test_a_group_whose_members_can_be_rewritten_survives_as_a_group() -> None:
    """The same contract for `ExceptionGroup.exceptions`, which is not part of the chain."""
    exc = post_mint_orphan(_group_cause)
    cause = exc.__cause__

    assert cause is not None, "the cause was dropped entirely"
    assert not isinstance(cause, RedactedCause), (
        f"group members this package CAN rewrite cost the refusal its whole cause: {cause!r}"
    )
    assert isinstance(cause, BaseExceptionGroup), f"the group was flattened away: {cause!r}"
    members = list(cause.exceptions)
    assert len(members) == 2, f"a member was dropped rather than redacted: {members!r}"
    for index, member in enumerate(members):
        assert_code_is_unrecoverable(
            str(member), ORACLE_CODE, f"the rewritten group member [{index}]"
        )
    assert any("timed out" in str(m) for m in members), (
        f"the innocent member was destroyed with the guilty one: {members!r}"
    )


@pytest.mark.parametrize(
    ("label", "make_exc"),
    [
        ("lazily-computed-notes", _lazy_note_cause),
        ("self-rendering-member-of-a-group", _group_of_self_rendering_cause),
    ],
    ids=["lazily-computed-notes", "self-rendering-member-of-a-group"],
)
def test_a_cause_the_rewrite_cannot_reach_is_replaced_whole(label: str, make_exc: Any) -> None:
    """...and the other outcome, asserted as an outcome rather than inferred from silence.

    These are the shapes no in-place edit can fix — a computed `__notes__`, a group member
    that renders from its own fields. The contract there is substitution: the object goes,
    and what is left still names the type that failed and the fingerprint that joins to the
    `code_created` record, so the refusal is poorer but not useless.
    """
    exc = post_mint_orphan(make_exc)
    cause = exc.__cause__

    assert isinstance(cause, RedactedCause), (
        f"[{label}] a cause the rewrite cannot reach was chained unchanged: {cause!r}"
    )
    assert code_fingerprint(ORACLE_CODE) in str(cause), (
        f"[{label}] the stand-in does not join to the code_created record: {str(cause)!r}"
    )
    assert exc.__cause__ is cause, (
        "reading the cause twice built a second stand-in; the substitution must be stored"
    )


def test_a_naive_new_call_site_is_closed_by_the_reading_properties_too() -> None:
    """The LAZY half of the defence, on a shape only the render-faithful oracle catches.

    Every `raise ... from` site in this package sanitises the cause eagerly, because that is
    the only edit the C-level excepthook can see. The redacting `__cause__`/`__context__`
    properties exist so that a site which forgets — a new one, written next year — is still
    safe for every reader that goes through attribute lookup. That half used to run only the
    in-place rewrite, which is strictly weaker: a computed `__notes__` walked straight
    through it. Both halves now make the same decision from the same oracle.
    """
    naive_cause = _lazy_note_cause()
    orphan = OrphanedCode(
        code=ORACLE_CODE,
        permalink_url=f"https://{RIVAL_DOMAIN}/cart/1:1",
        provider="naive",
        store_id="store-a",
        auction_id="auction-1",
        bid_ref="bid-a",
    )
    try:
        try:
            raise naive_cause
        except LazilyNotedFailure as exc:
            # No `_sanitised_cause` anywhere: exactly what a call site that has not read
            # this module writes.
            raise OrphanedCheckoutCode("post-mint refusal", orphan=orphan) from exc
    except OrphanedCheckoutCode as exc:
        assert_code_is_unrecoverable(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            ORACLE_CODE,
            "traceback of a naive call site's refusal",
        )
        assert isinstance(exc.__cause__, RedactedCause), (
            f"the reading property chained the unreachable cause unchanged: {exc.__cause__!r}"
        )
        assert exc.orphan.code == ORACLE_CODE, "T-202: the orphan must still carry the code"
    else:  # pragma: no cover - the raise above cannot fall through
        raise AssertionError("nothing was raised")


def test_the_merchants_code_is_reported_into_the_ports_minting_ledger() -> None:
    """The adapter's half of the port-level T-202 guarantee.

    `mint_code` reports itself, so every provider that mints locally is covered without
    knowing the ledger exists. A merchant's `POST /codes` does not go through `mint_code`,
    so `ShopifyCheckoutProvider` reports the code the moment it reads it — before anything
    that could raise. Without that line the port's backstop is blind on the one path where
    the code is somebody else's, and the adapter's own handler is again the only cover.
    """
    merchant = OffDomainMerchant(ORPHAN_CODE, f"https://{SELLER_DOMAIN}/cart/1:1")
    request = CheckoutRequest(
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

    with minting_ledger() as recorded:
        minted = resolve_provider("shopify").mint(request)

    assert minted.code == ORPHAN_CODE
    assert recorded == [ORPHAN_CODE], (
        f"the merchant's code never reached the port's ledger, so a failure between the "
        f"merchant's answer and mint()'s return has nothing to carry out: {recorded!r}"
    )

    # ...and the same for a provider that mints locally, which reports from `mint_code`.
    with minting_ledger() as locally:
        SimulatedRedirectProvider().mint(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain=SELLER_DOMAIN,
                offer=offer(SELLER_DOMAIN),
                mode="redirect",
                now=T_NOW,
                registered_domains=StaticRegisteredDomains({"store-a": SELLER_DOMAIN}),
            )
        )
    assert len(locally) == 1 and locally[0].startswith("PSX-"), (
        f"mint_code did not report into the ledger: {locally!r}"
    )


def test_the_ledger_is_empty_outside_a_checkout_and_does_not_leak_between_them() -> None:
    """Two controls, both of which a global would fail.

    `record_minted_code` outside a ledger must be a no-op rather than an error — it is
    called from the exact region where raising would lose the code it is reporting — and one
    checkout must never see another's code, or the port would attach the wrong live discount
    to a refusal.
    """
    record_minted_code("PSX-NO-LEDGER")  # must not raise

    with minting_ledger() as outer:
        record_minted_code("PSX-OUTER")
        with minting_ledger() as inner:
            record_minted_code("PSX-INNER")
        assert inner == ["PSX-INNER"], f"the inner ledger saw the outer's code: {inner!r}"
        assert outer == ["PSX-OUTER"], f"the outer ledger saw the inner's code: {outer!r}"


def test_the_oracle_terminates_on_a_cycle_through_the_redacting_properties() -> None:
    """The cycle the re-entrancy guard actually exists for.

    `test_the_render_oracle_terminates_on_a_cyclic_chain` uses plain exceptions, and
    :mod:`traceback` handles those cycles itself. This one runs the cycle through
    `OrphanedCheckoutCode`'s redacting properties, which is the path the standard library's
    own bookkeeping cannot see: each read asks the oracle, and the oracle's render performs
    the next read. Without the guard in `rendered_exception` the pair recurses until the
    interpreter stops it.
    """
    orphan = OrphanedCode(
        code=ORACLE_CODE,
        permalink_url=f"https://{RIVAL_DOMAIN}/cart/1:1",
        provider="cyclic",
        store_id="store-a",
        auction_id="auction-1",
        bid_ref="bid-a",
    )
    first = OrphanedCheckoutCode("first refusal", orphan=orphan)
    second = OrphanedCheckoutCode("second refusal", orphan=orphan)
    first.__cause__ = second
    second.__cause__ = first  # the cycle, through two redacting properties

    rendered = rendered_exception(first)  # must terminate
    assert isinstance(rendered, str)
    assert_code_is_unrecoverable(rendered, ORACLE_CODE, "a cyclic chain of orphan refusals")


# =====================================================================================
# T-215 (j) — the ORACLE asks the question the PERSISTED EVENT asks, byte for byte
# =====================================================================================
"""Why this section exists after nine others, and what is different about it.

Every oracle above searches a string this file chose: ``denial_reason``,
``json.dumps(event, default=str)``, ``str(exc)``, a rendered traceback. Those are *proxies*
for the surface the ticket is about, and this defect has now been defeated five times, each
time through a channel the previous pass had not enumerated. The pattern is always the same
shape — **the check asked a narrower question than the surface it protects.**

So the check below asks the surface's own question. ``apps/trust/src/events/store.py``
persists a ``LedgerEvent`` by hashing and storing ``canonical_json(canonical_event(event))``
(store.py:347). That function pair, imported from the module the store imports it from, is
the definition of "what gets written down". :func:`persisted_bytes` runs the events an
accept actually emitted through it and returns the resulting characters, and every assertion
here is "the code is not recoverable from those characters".

Two properties follow that no amount of case-adding buys:

* it covers fields nobody thought to name. ``json.dumps(..., default=str)`` and
  ``canonical_json`` disagree about number formatting, key order, escaping and non-ASCII;
  the ledger's rules (RFC-8785) are the ones that decide what a reader of the event stream
  sees, and this reads exactly those; and
* it covers **every event the refusal emits**, minus exactly one — the single event the
  refusal itself names as the code's sanctioned home. That exclusion is computed from the
  result's own ``orphaned_code`` pointer rather than hard-coded to ``code_created``, so if
  the code is ever written into a second event, or into a different kind, this gate fails
  instead of silently agreeing with the change.
"""


def sanctioned_home(result: Any) -> str:
    """The ``event_id`` of the one event this refusal declares as the live code's home.

    Read off the refusal's own ``orphaned_code`` pointer, never assumed. T-215's whole
    division of labour is "the code goes to the ledger event shaped to hold it, and the
    prose carries a fingerprint that joins to it" — so the refusal already has to say WHICH
    event that is, and a gate that instead hard-codes ``kind == "code_created"`` would keep
    passing if a future change filed the code somewhere else as well.
    """
    for event in result.events:
        pointer = dict(event.get("payload") or {}).get("orphaned_code")
        if isinstance(pointer, dict) and pointer.get("event_id"):
            return str(pointer["event_id"])
    return ""


def persisted_bytes(result: Any) -> str:
    """Everything this accept will WRITE DOWN, serialised the way the ledger serialises it.

    Not ``json.dumps``: :func:`~trust.ledger.canonical.canonical_json` is the RFC-8785
    canonicalisation ``apps/trust/src/events/store.py`` hashes and stores events with, and
    the bytes it produces are the bytes anything reading the event stream reads.
    """
    return "\n".join(
        canonical_json(canonical_event(event))
        for event in result.events
        if str(event.get("event_id")) != sanctioned_home(result)
    )


def test_the_gates_serialiser_is_the_one_the_event_store_actually_persists_with() -> None:
    """Pin the oracle to the production serialiser, so it cannot drift into a stand-in.

    This is the assertion that keeps :func:`persisted_bytes` honest. Its value comes
    entirely from being the *same* function the store calls; the moment it is a lookalike —
    a local ``json.dumps``, a vendored copy, a second canonicaliser — it is a proxy again,
    and a proxy is what has been defeated five times. Identity, not equality of output on
    the examples this file happens to try.
    """
    from trust.events import store as event_store

    assert event_store.canonical_json is canonical_json, (
        "the gate is canonicalising with a different function than the event store "
        "persists with, so it is measuring a stand-in for the surface it protects"
    )
    assert event_store.canonical_event is canonical_event, (
        "the gate is normalising events differently than the event store does"
    )


def test_the_persisted_bytes_oracle_detects_a_planted_leak() -> None:
    """The falsifiability control: this gate must be able to FAIL.

    Same duty as :func:`test_the_leak_detector_itself_detects_a_leak`, one layer up. An
    oracle that never fires is indistinguishable from a fixed bug, and a gate that
    canonicalises the wrong events — or excludes too many of them — would be exactly that.
    Three plants, each aimed at a different way this helper could be vacuous.
    """
    live = auction("bid-a", "bid-b")
    result = accept(live, "bid-a", OffDomainMerchant(), "shopify")
    assert persisted_bytes(result), "the oracle serialised nothing at all — vacuous"

    # 1. The code planted in the refusal's own prose, percent-encoded so a bare substring
    #    search over the raw bytes would miss it but a reader would not.
    leaky = copy.deepcopy(list(result.events))
    for event in leaky:
        if event["kind"] == "policy_event":
            event["payload"]["reason"] = f"refused https://x.tld/c?discount={quote(ORPHAN_CODE)}"
    planted = dataclasses.replace(result, events=tuple(leaky))
    assert redeemable_spelling(persisted_bytes(planted), ORPHAN_CODE) is not None, (
        "a percent-encoded code planted in the persisted reason was not detected"
    )

    # 2. The code planted in a SECOND event of the sanctioned kind. Only ONE event is
    #    excluded — the one the refusal points at — so a duplicate must still be caught.
    duplicate = copy.deepcopy(event_of(result, "code_created"))
    duplicate["event_id"] = "a-different-event-id"
    doubled = dataclasses.replace(result, events=(*result.events, duplicate))
    assert redeemable_spelling(persisted_bytes(doubled), ORPHAN_CODE) is not None, (
        "a second code_created event carrying the code was excluded from the oracle; the "
        "exclusion must name ONE event, not a kind"
    )

    # 3. Nothing excluded at all: the sanctioned event itself carries the code, so an
    #    oracle that forgot to honour the pointer would be permanently red rather than
    #    permanently green. This proves the exclusion is doing real work.
    unpointed = dataclasses.replace(
        result,
        events=tuple(e for e in result.events if e["kind"] != "policy_event"),
    )
    assert redeemable_spelling(persisted_bytes(unpointed), ORPHAN_CODE) is not None, (
        "with no refusal event to name the home, the code_created event must NOT be "
        "excluded — otherwise the exclusion is unconditional"
    )


# -------------------------------------------------------------------------------------
# The channels pass 5 was killed before it could try. Inputs are enumerated; the CHECK is
# not — every one of them ends at the same two assertions.
# -------------------------------------------------------------------------------------
HOSTILE_CODE = "PSX-HOSTILE-4RTX9"


class _StrRaises(Exception):
    """An exception whose ``__str__`` raises. Its ``repr`` still spells the code."""

    def __str__(self) -> str:
        raise RuntimeError(f"rendering exploded, and this text names {HOSTILE_CODE}")


class _UnreadableValue:
    """A non-string exception ARGUMENT that cannot be stringified but can be repr'd.

    The shape the pass-5 lane reported and died before confirming. ``_redact_arg`` asked
    ``spells_code(str(arg), code)``, and that conversion sat outside every guard, so this
    object made the SANITISER raise — no ``OrphanedCheckoutCode``, no ``code_created``
    event, an unrevokable discount, and the raw exception propagating with this ``repr`` in
    it.
    """

    def __str__(self) -> str:
        raise RuntimeError("no str for you")

    def __repr__(self) -> str:
        return f"<merchant-reply {HOSTILE_CODE}>"


class _HostileEquality:
    """An argument whose ``__eq__`` raises — reached by the walk's change detection.

    ``redacted != node.args`` compares TUPLES, and a tuple comparison runs the elements'
    ``__eq__``. Nothing about redaction is involved; the sanitiser simply asked a hostile
    object a question it was free to answer with an exception.
    """

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("equality exploded")

    __hash__ = None  # type: ignore[assignment]

    def __str__(self) -> str:
        return f"merchant reply for {HOSTILE_CODE}"

    def __repr__(self) -> str:
        return f"<HostileEquality {HOSTILE_CODE}>"


class _UnprintableMember(Exception):
    """A group member that no renderer can format. Neither ``str`` nor ``repr`` survives.

    Its purpose is not to leak — it cannot — but to make ``TracebackException.format``
    RAISE, which used to send the render oracle down its ``str(exc)`` fallback and let a
    *sibling* member's message through as "clean".
    """

    def __str__(self) -> str:
        raise RuntimeError("unstringable")

    def __repr__(self) -> str:
        raise RuntimeError("unreprable")


@dataclasses.dataclass
class _ReprOnlyReply:
    """``str`` hides the code; ``repr`` prints it — and a multi-arg exception renders ``repr``."""

    code: str

    def __str__(self) -> str:
        return "<merchant reply>"


def _hostile_str_cause() -> BaseException:
    return _StrRaises(f"the code is {HOSTILE_CODE}")


def _unreadable_arg_cause() -> BaseException:
    return ValueError("merchant reply rejected", _UnreadableValue())


def _hostile_equality_cause() -> BaseException:
    return ValueError("merchant reply rejected", _HostileEquality())


def _unprintable_group_member_cause() -> BaseException:
    return ExceptionGroup(
        "two link attempts failed",
        [_UnprintableMember(), ValueError(f"the second attempt used {HOSTILE_CODE}")],
    )


def _hostile_note_object_cause() -> BaseException:
    exc = RuntimeError("the merchant call failed")
    # `__notes__` is a plain attribute, and nothing requires its members to be strings —
    # `format_exception_only` stringifies whatever is in the list.
    exc.__notes__ = [_UnreadableValue()]  # type: ignore[attr-defined]
    return exc


def _nested_container_cause() -> BaseException:
    """The code inside a dict inside a list, never as a top-level string argument."""
    return ValueError("merchant reply rejected", {"discounts": [{"code": HOSTILE_CODE}]})


def _repr_only_cause() -> BaseException:
    return ValueError("merchant reply rejected", _ReprOnlyReply(HOSTILE_CODE))


def _implicit_context_cause() -> BaseException:
    """The code reachable ONLY through ``__context__`` — nobody wrote ``raise … from``."""
    try:
        raise ValueError(f"the inner failure named {HOSTILE_CODE}")
    except ValueError:
        return RuntimeError("the outer message is clean")


def _cyclic_cause() -> BaseException:
    first = ValueError(f"the first names {HOSTILE_CODE}")
    second = ValueError("the second is clean")
    first.__cause__ = second
    second.__cause__ = first
    return first


def _named_after_the_code_cause() -> BaseException:
    """A merchant library whose EXCEPTION CLASS is named after the code it just minted.

    A merchant's ``POST /codes`` returns a code the merchant chose, so it need not look like
    ``PSX-…`` at all — and a type name is printed by ``repr``, by the traceback's final
    line, and by every handler in this package that formats ``type(exc).__name__``. It reads
    like metadata, which is why it survived five passes.
    """
    kind = type(_IDENTIFIER_CODE, (Exception,), {})
    return kind("the merchant client refused")


#: A merchant-chosen code that is also a legal Python identifier, so it can BE a class name.
_IDENTIFIER_CODE = "SUMMER10LIVE"


HOSTILE_CAUSES: list[tuple[str, Any, str]] = [
    ("cause-whose-str-raises", _hostile_str_cause, HOSTILE_CODE),
    ("argument-that-cannot-be-stringified", _unreadable_arg_cause, HOSTILE_CODE),
    ("argument-whose-eq-raises", _hostile_equality_cause, HOSTILE_CODE),
    ("group-with-an-unprintable-member", _unprintable_group_member_cause, HOSTILE_CODE),
    ("note-that-is-not-a-string", _hostile_note_object_cause, HOSTILE_CODE),
    ("code-nested-in-a-dict-in-a-list", _nested_container_cause, HOSTILE_CODE),
    ("code-only-in-repr-not-in-str", _repr_only_cause, HOSTILE_CODE),
    ("code-only-through-implicit-context", _implicit_context_cause, HOSTILE_CODE),
    ("cyclic-chain-of-causes", _cyclic_cause, HOSTILE_CODE),
    ("exception-class-named-after-the-code", _named_after_the_code_cause, _IDENTIFIER_CODE),
]


class LosesTheCodeAfterMinting(CheckoutProvider):
    """Mints a real code, then fails with whatever cause the case supplies.

    Drives the ``_mint_recording_orphans`` path — the deepest of the three post-mint
    handlers, and the one where the code exists but the permalink does not, so the port has
    only the minting ledger to learn the code from.
    """

    name = "loses-the-code"

    def __init__(self, code: str, make_exc: Any) -> None:
        self.code = code
        self.make_exc = make_exc

    def mint(self, request: CheckoutRequest) -> MintedCheckout:
        record_minted_code(self.code)
        raise self.make_exc()


def _accept_against(provider: CheckoutProvider, mode: str) -> Any:
    register_provider(mode, provider)
    live = auction("bid-a", "bid-b")
    return accept(
        live,
        "bid-a",
        None,
        mode,
        registered_domains=StaticRegisteredDomains(
            {"store-a": SELLER_DOMAIN, "store-b": "store-b.example.com"}
        ),
    )


@pytest.mark.parametrize(
    ("label", "make_exc", "code"), HOSTILE_CAUSES, ids=[c[0] for c in HOSTILE_CAUSES]
)
def test_no_hostile_cause_reaches_the_persisted_event_or_costs_the_record(
    label: str, make_exc: Any, code: str
) -> None:
    """The two invariants, together, for every shape a merchant's library can raise.

    They are asserted TOGETHER on purpose, and that pairing is the lesson of this pass. Each
    of these shapes used to break BOTH at once, through one mechanism: the sanitiser itself
    raised. A gate that only checked "the code is not published" would have gone green on a
    tree where the exception never reached ``accept()`` as an orphan at all — no
    ``code_created`` event, a live discount nobody could revoke, and a suite that could not
    tell the difference between "redacted" and "never got there".
    """
    result = _accept_against(LosesTheCodeAfterMinting(code, make_exc), f"hostile-{label}")

    # T-202 — the record survived. `accept()` must have seen an OrphanedCheckoutCode.
    assert result.accepted is False, f"{label}: a failed mint was accepted"
    assert result.orphaned_code is not None, (
        f"{label}: the port did not carry the code out — the sanitiser probably raised, "
        f"which loses the orphan entirely. denial_reason was {result.denial_reason!r}"
    )
    assert result.orphaned_code.code == code, f"{label}: the wrong code was recorded"
    created = event_of(result, "code_created")
    assert created["payload"]["code"] == code, f"{label}: the ledger record lost the code"
    assert created["payload"].get("orphaned") is True

    # T-215 — and nothing that gets written down spells it.
    assert_code_is_unrecoverable(
        persisted_bytes(result), code, f"the persisted events of a {label} refusal"
    )
    assert_code_is_unrecoverable(
        str(result.denial_reason or ""), code, f"the denial_reason of a {label} refusal"
    )


@pytest.mark.parametrize(
    ("label", "make_exc", "code"), HOSTILE_CAUSES, ids=[c[0] for c in HOSTILE_CAUSES]
)
def test_no_hostile_cause_reaches_any_renderer_of_the_refusal(
    label: str, make_exc: Any, code: str
) -> None:
    """...and no renderer prints it either, C-level excepthook first.

    Separate from the test above because it asserts about a different surface — the
    exception object rather than the ledger — and because the excepthook channel is the one
    that bypasses every Python-level property this package installs.
    """
    provider = LosesTheCodeAfterMinting(code, make_exc)
    request = CheckoutRequest(
        auction_id="auction-1",
        bid_ref="bid-a",
        store_id="store-a",
        store_domain=SELLER_DOMAIN,
        offer=offer(SELLER_DOMAIN),
        mode=f"renderer-{label}",
        now=T_NOW,
        registered_domains=StaticRegisteredDomains({"store-a": SELLER_DOMAIN}),
    )
    with pytest.raises(OrphanedCheckoutCode) as raised:
        provider.checkout(request)

    assert_no_renderer_publishes(raised.value, code, f"a {label} refusal")


def test_the_redaction_machinery_cannot_be_made_to_raise() -> None:
    """The property behind every case above, asserted directly on the redactor.

    A sanitiser that raises on hostile input is worse than no sanitiser — this module's
    header already says so about pass 2's ``redact_url``, and pass 4 reintroduced exactly
    that failure one layer in. The rule is not "handle these ten shapes"; it is that no
    object a merchant's library can construct may make the redaction path raise, because the
    redaction path is the only thing standing between a live discount and the event stream,
    and it runs on a path that is *already refusing*.
    """
    hostile: list[Any] = [
        _UnreadableValue(),
        _HostileEquality(),
        _ReprOnlyReply(HOSTILE_CODE),
        {"nested": [HOSTILE_CODE]},
        object(),
        b"\xff\xfe not utf-8",
        None,
    ]
    for value in hostile:
        # The detector must answer, not raise — and it must answer "redact" when it cannot
        # read the value at all.
        answer = spells_code(value, HOSTILE_CODE)
        assert isinstance(answer, bool), f"spells_code({value!r}) did not answer"
        assert safe_token(value, HOSTILE_CODE, label="probe") is not None

    for _, make_exc, code in HOSTILE_CAUSES:
        exc = make_exc()
        # Never raises, whatever is in there.
        assert isinstance(rendered_exception(exc), str)
        assert isinstance(render_can_publish(exc, code), bool)


def test_an_exception_no_renderer_can_format_is_treated_as_unsafe() -> None:
    """Not looking is not the same as seeing nothing, and must not be recorded as it.

    This is the pass-5 position reversed, and it is reversed because it was measured false.
    Pass 5 argued a render that failed has published nothing. But ``rendered_exception``
    falls back to ``str(exc)`` when ``TracebackException.format`` raises, and for an
    ``ExceptionGroup`` that fallback is the summary line and the member COUNT — clean, while
    the C-level ``PyErr_Display`` is *more* tolerant than ``TracebackException``: it prints
    a placeholder for the member it cannot format and goes on to print the next one. So the
    render this process could not obtain says nothing about what another reader prints, and
    the only safe reading of it is "replace the object".
    """
    group = _unprintable_group_member_cause()

    # The proxy is clean — this is the fail-open that the composition used to walk into.
    assert redeemable_spelling(str(group), HOSTILE_CODE) is None, (
        "the premise of this test is gone: str() of the group now spells the code, so it "
        "no longer demonstrates the gap between the two readers"
    )
    # ...and the oracle refuses to certify it anyway.
    assert render_can_publish(group, HOSTILE_CODE) is True, (
        "an exception this process cannot render was reported as safe to publish"
    )

    # A plain, fully renderable, code-free exception is still publishable — otherwise the
    # assertion above would be satisfied by a function that always says True.
    assert render_can_publish(ValueError("nothing interesting here"), HOSTILE_CODE) is False
