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
    DENIAL_FALLBACK_NOT_PURCHASABLE,
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


def _fallback_auction() -> dict[str, Any]:
    """An auction whose `bid-a` is the exchange's own list-price fallback (R10).

    The flag is what `auction/routes.py::collected_bid_records` stamps off the `BidEntry`; the
    offer is left fully mintable on purpose, so the refusal below can only be the fallback gate
    and never an offer the checkout port would have rejected anyway.
    """
    record = auction()
    record["bids"][0]["fallback"] = True
    return record


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
        # R10's list-price fallback: shown, never sold. An INTERIM fail-closed default rather
        # than a rule R10 states — see `accept.offer`'s block for the measurement behind it.
        DENIAL_FALLBACK_NOT_PURCHASABLE: accept(_fallback_auction(), "bid-a", Creator(), "shopify"),
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


# =====================================================================================
# T-264 / T-326 — the PROPERTY, not the patch that landed
# =====================================================================================
#
# ``test_an_unusable_registry_does_not_render_a_memory_address`` above is GREEN at HEAD and
# the leak it names reproduces anyway. It hands ``registered_domains=object()`` — the one
# value the landed fix checks for — and never varies ``code_creator``, so it grades the patch
# rather than the property. Measured at HEAD ``21d1cba``, with everything else wired healthy:
#
#   registered_domains=object()          -> "…TypeError: registered_domains of type 'object'…"   clean
#   code_creator=object()                -> "…injected code creator <object object at 0x1033…>"  LEAK
#   registered_domains raising on use    -> "…lookup for 'store-a' failed (TypeError: backend
#                                             <object object at 0x102e…> is down)…"              LEAK
#   eligibility.check() raising          -> "unavailable: eligibility read … (<object at 0x…>)"  LEAK
#   offer['quantity'] is an object       -> "…UnusableOffer: offer quantity <object at 0x…>…"    LEAK
#
# Every one of those strings is in ``denial_reason``, in the persisted ``policy_event`` payload
# AND in the 409 body — ``_denied`` only re-labels an UNDECLARED token, so a declared
# ``checkout_refused``/``unavailable`` carries its prose, address included, to the client.
#
# What follows states the property instead: for every value a caller or an injected
# collaborator can put on the accept path, no default ``__repr__`` reaches any of the three
# sinks. The sweep is ARMED by its own test below rather than inside the ``xfail`` bodies —
# under ``xfail(strict=True)`` *any* exception in the body reads as ``xfailed``, which is
# green, so a probe that stopped working would be indistinguishable from the defect.

#: Every ``accept_offer`` parameter that carries an OBJECT the caller injected. Checked
#: against the live signature in the armed test, so a fourth collaborator cannot be added
#: without this sweep noticing that nothing covers it.
INJECTED_COLLABORATORS: tuple[str, ...] = ("code_creator", "eligibility", "registered_domains")

#: The rest of ``accept_offer``'s signature: data, not collaborators. The two tuples are
#: asserted to PARTITION the signature — neither a new parameter nor a renamed one can slip
#: past by belonging to neither list.
ACCEPT_OFFER_DATA_PARAMS: tuple[str, ...] = ("auction", "bid_ref", "mode")

#: The offer rides on the bid, so it is not an ``accept_offer`` parameter — but its values are
#: written by the bidding store and they reach the same f-strings, so the property covers it.
OFFER_CARRIER = "offer"


def _static_domains(store_id: str) -> str:
    """A healthy platform registry, so a case's own hostile value is what refuses."""
    return SELLER_DOMAIN


def _healthy_eligibility() -> Any:
    """A source that says yes. Imported locally: a module-level import here would be E402."""
    from exchange.eligibility import ELIGIBLE

    return Eligibility(ELIGIBLE)


class _ExposesNothing:
    """An instance with the default ``__repr__`` and none of the methods anyone looks for."""


class _RaisesQuotingAnObject:
    """Callable and correctly shaped — and raises when it is USED, quoting an object.

    This is T-264's own recorded reproduction ("a ``registered_domains`` value that raises
    TypeError on use"), and it is the shape the landed fix does not cover: that fix is a
    *callability* check, which this passes. The address in the message is the collaborator's,
    not the exchange's, and the exchange republishes it whole.
    """

    interface_version = SELLER_ELIGIBILITY_INTERFACE_VERSION

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError(f"the backend {object()!r} is unreachable")

    create_code = domain_for = check = __call__


class _RaisesAnObjectAsItsArgument:
    """Raises with an object as the exception ARGUMENT — ``KeyError(<object>)``.

    A registry that does not know a store raises exactly this shape, and ``str(KeyError(obj))``
    is ``repr(obj)``, so nothing has to quote anything for the address to travel.
    """

    interface_version = SELLER_ELIGIBILITY_INTERFACE_VERSION

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise KeyError(object())

    create_code = domain_for = check = __call__


class _AnswersWithAnObject:
    """Usable, and answers — with an object where a string belongs."""

    interface_version = SELLER_ELIGIBILITY_INTERFACE_VERSION

    def domain_for(self, store_id: str) -> Any:
        return _ExposesNothing()

    def check(self, store_id: str) -> Any:
        return EligibilityDecision(store_id=store_id, status=object(), reason="")


class _QuotesAnObjectInItsReason:
    """A source whose own prose carries an address. The exchange republishes prose verbatim."""

    interface_version = SELLER_ELIGIBILITY_INTERFACE_VERSION

    def check(self, store_id: str) -> Any:
        return EligibilityDecision(
            store_id=store_id, status="unavailable", reason=f"the source {object()!r} refused"
        )


class _QuotesAnObjectBehindTheDeclaredCode:
    """The BYPASS: a reason that already begins with its declared code is returned VERBATIM.

    ``gate._denial_reason`` reads::

        if denial_code(reason) == code:
            return reason
        return denial_reason(code, reason)

    so a source answering ``unavailable`` with the prose ``"unavailable: … <object at 0x…> …"``
    takes the first branch and never reaches ``denial_reason`` at all. This case exists because
    it is the one shape a sanitiser installed inside ``denial_reason()`` would NOT close — the
    obvious single-point fix leaves it live, and a gate that omitted it would certify that fix
    as complete. ``_QuotesAnObjectInItsReason`` above is the sibling that DOES route through
    ``denial_reason``; the pair is what distinguishes "sanitiser absent" from
    "sanitiser present and bypassed".
    """

    interface_version = SELLER_ELIGIBILITY_INTERFACE_VERSION

    def check(self, store_id: str) -> Any:
        return EligibilityDecision(
            store_id=store_id,
            status=DENIAL_UNAVAILABLE,
            reason=f"{DENIAL_UNAVAILABLE}: the source {object()!r} refused",
        )


class _DeclaresAnObjectVersion:
    """``interface_version`` is whatever the source put there — the path ``describe`` covers."""

    interface_version = object()

    def check(self, store_id: str) -> Any:
        return EligibilityDecision(store_id=store_id, status="unavailable", reason="no")


def _hostile_cases() -> tuple[tuple[str, str, Any, str | None], ...]:
    """``(carrier, shape, factory, offer_key)`` for every unusable value that must refuse.

    Every entry was driven live at HEAD and every entry REFUSES — a case that accepted would
    make the sink checks vacuous, so the armed test asserts that too. Two measured shapes are
    deliberately absent because they do NOT refuse, and are reported as separate defects: a
    ``code_creator`` answering ``{"code": <object>}`` and an ``offer`` whose ``unit_price`` is
    an object both return ``accepted=True``.
    """
    return (
        # registered_domains — T-264's own collaborator, in provider.py
        ("registered_domains", "a bare object()", object, None),
        ("registered_domains", "an instance exposing nothing", _ExposesNothing, None),
        ("registered_domains", "callable, raises on use", _RaisesQuotingAnObject, None),
        ("registered_domains", "raises KeyError(<object>)", _RaisesAnObjectAsItsArgument, None),
        ("registered_domains", "answers with an object", _AnswersWithAnObject, None),
        # code_creator — T-326, the sibling injected argument, in providers.py
        ("code_creator", "a bare object()", object, None),
        ("code_creator", "an instance exposing nothing", _ExposesNothing, None),
        ("code_creator", "callable, raises on use", _RaisesQuotingAnObject, None),
        ("code_creator", "raises KeyError(<object>)", _RaisesAnObjectAsItsArgument, None),
        # eligibility — the third injected collaborator on the same call
        ("eligibility", "a bare object()", object, None),
        ("eligibility", "an instance exposing nothing", _ExposesNothing, None),
        ("eligibility", "declares an object interface version", _DeclaresAnObjectVersion, None),
        ("eligibility", "raises on use", _RaisesQuotingAnObject, None),
        ("eligibility", "answers with an object status", _AnswersWithAnObject, None),
        ("eligibility", "quotes an object in its own reason", _QuotesAnObjectInItsReason, None),
        (
            "eligibility",
            "quotes an object BEHIND the declared code",
            _QuotesAnObjectBehindTheDeclaredCode,
            None,
        ),
        # the offer the bidding store wrote, which reaches the same f-strings
        ("offer", "expires_at is an object", object, "expires_at"),
        ("offer", "quantity is an object", object, "quantity"),
    )


def _observe(carrier: str, shape: str, factory: Any, offer_key: str | None) -> dict[str, Any]:
    """Drive one refusal and read ALL THREE sinks a client or an operator actually sees."""
    board = auction()
    kwargs: dict[str, Any] = {
        "auction": board,
        "bid_ref": "bid-a",
        "mode": "shopify",
        "code_creator": Creator(),
        "eligibility": _healthy_eligibility(),
        "registered_domains": _static_domains,
    }
    if offer_key is None:
        kwargs[carrier] = factory()
    else:
        board["bids"][0]["offer"][offer_key] = factory()

    result = accept_offer(**kwargs)
    reason = str(result.denial_reason or "")
    persisted = [str(e["payload"]["reason"]) for e in result.events if e["kind"] == "policy_event"]
    body = json.loads(_denied(reason).body)["denial_reason"] if reason else ""

    sinks: dict[str, str] = {"denial_reason": reason, "409 body": body}
    for index, payload in enumerate(persisted):
        sinks[f"policy_event[{index}].reason"] = payload

    return {
        "carrier": carrier,
        "shape": shape,
        "accepted": bool(result.accepted),
        "reason": reason,
        "code": denial_code(reason),
        "persisted": persisted,
        "body": body,
        "leaking_sinks": sorted(n for n, s in sinks.items() if MEMORY_ADDRESS.search(s)),
    }


def _sweep(carriers: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Every case (or just the named carriers'), driven with the process-wide source unwired."""
    previous = use_registered_domains(None)
    try:
        return [
            _observe(carrier, shape, factory, offer_key)
            for carrier, shape, factory, offer_key in _hostile_cases()
            if carriers is None or carrier in carriers
        ]
    finally:
        use_registered_domains(previous)


def _leak_report(leaks: list[dict[str, Any]], scanned: int) -> str:
    lines = [
        f"{len(leaks)} of {scanned} driven refusal(s) rendered a memory address into a "
        "persisted, client-visible denial payload:"
    ]
    for entry in leaks:
        lines.append(
            f"  · {entry['carrier']} — {entry['shape']} — leaked into {entry['leaking_sinks']}"
        )
        lines.append(f"      {entry['reason']!r}")
    return "\n".join(lines)


def test_the_injected_collaborator_leak_sweep_is_armed() -> None:
    """Not xfail, and not optional: the three gates below are worthless without it.

    Six ways the sweep could pass while measuring nothing, all closed here:

    * the case table empties, or a carrier quietly drops out of it (three sweeps in this repo
      went from 6/8, 70/79 and 48/66 covered to ZERO covered and stayed green);
    * ``accept_offer`` grows a fourth injected collaborator that no case exercises;
    * a case stops REFUSING — an accept carries no denial reason, so every sink check on it is
      vacuously true;
    * a case refuses without persisting a ``policy_event``, so the durable sink is never read;
    * the address detector stops matching, so nothing can ever be found;
    * the address detector starts matching everything, so the gates could never go green.
    """
    import inspect

    assert MEMORY_ADDRESS.search(repr(object())), (
        "the detector no longer matches a default __repr__; every gate below is blind"
    )
    assert not MEMORY_ADDRESS.search(
        "checkout_refused: TypeError: registered_domains of type 'object' exposes neither "
        "domain_for(store_id) nor __call__(store_id)"
    ), "the detector matches a clean reason; the gates below could never go green"

    signature = set(inspect.signature(accept_offer).parameters)
    assert signature == set(INJECTED_COLLABORATORS) | set(ACCEPT_OFFER_DATA_PARAMS), (
        f"accept_offer's signature is {sorted(signature)}, which is not partitioned by "
        f"INJECTED_COLLABORATORS {sorted(INJECTED_COLLABORATORS)} and ACCEPT_OFFER_DATA_PARAMS "
        f"{sorted(ACCEPT_OFFER_DATA_PARAMS)} — a parameter belonging to neither list is a "
        "collaborator nothing below drives, or a rename that silently emptied a carrier"
    )

    cases = _hostile_cases()
    assert len(cases) >= 17, f"the case table holds {len(cases)} case(s); it held 18 when written"
    assert len(cases) == len({(c[0], c[1]) for c in cases}), "two cases share a label"

    # The BYPASS PAIR, pinned by name. `gate._denial_reason` returns a reason that already
    # carries its declared code VERBATIM, skipping `denial_reason()` — so a sanitiser installed
    # there closes one of these two shapes and not the other. Losing either half turns this
    # sweep back into a test of one code path.
    shapes = {shape for _c, shape, _f, _k in cases}
    for half in ("quotes an object in its own reason", "quotes an object BEHIND the declared code"):
        assert half in shapes, (
            f"the case {half!r} is gone from the sweep. It is one half of the pair that tells "
            "'no sanitiser' apart from 'sanitiser bypassed': gate._denial_reason short-circuits "
            "on a reason that already names its code, so a fix inside reasons.denial_reason() "
            "closes only the other half"
        )

    covered = {carrier for carrier, _shape, _factory, _key in cases}
    missing = (set(INJECTED_COLLABORATORS) | {OFFER_CARRIER}) - covered
    assert not missing, f"nothing in the sweep drives {sorted(missing)}"
    per_carrier = {c: sum(1 for e in cases if e[0] == c) for c in sorted(covered)}
    thin = {c: n for c, n in per_carrier.items() if n < 2}
    assert not thin, (
        f"these carriers are down to a single shape: {thin} (all counts: {per_carrier}) — one "
        "shape per collaborator is how the HEAD gate came to grade a patch instead of a property"
    )

    results = _sweep()
    assert len(results) == len(cases), "the sweep did not drive every case in the table"

    accepted = [f"{r['carrier']}/{r['shape']}" for r in results if r["accepted"]]
    assert not accepted, (
        f"these cases were ACCEPTED rather than refused: {accepted} — they carry no denial "
        "reason, so every sink assertion on them is vacuously true"
    )
    silent = [f"{r['carrier']}/{r['shape']}" for r in results if not r["reason"]]
    assert not silent, f"these refusals named no reason at all: {silent}"
    undeclared = [f"{r['carrier']}/{r['shape']}={r['code']}" for r in results if r["code"] is None]
    assert not undeclared, f"these refusals did not name a declared code: {undeclared}"
    unpersisted = [f"{r['carrier']}/{r['shape']}" for r in results if not r["persisted"]]
    assert not unpersisted, (
        f"these refusals persisted no policy_event: {unpersisted} — the durable sink the "
        "gates below claim to check was never written, so that half of each gate is blind"
    )
    unpublished = [f"{r['carrier']}/{r['shape']}" for r in results if not r["body"]]
    assert not unpublished, f"these refusals produced an empty 409 body: {unpublished}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-264: the landed fix is a CALLABILITY check on registered_domains, and T-264's own "
        "recorded reproduction is a value that RAISES ON USE — which passes that check. "
        "Measured at HEAD: a lookup raising TypeError, one raising KeyError(<object>), and one "
        "ANSWERING with an object all render '<object object at 0x…>' through "
        "provider.py:1100-1106 and offer.py:541 into denial_reason, the persisted policy_event "
        "and the 409 body. Remove this marker with the fix"
    ),
)
def test_t264_an_unusable_registered_domains_source_leaks_no_memory_address(
    unwired: None,
) -> None:
    """T-264, over every unusable shape — not only the one the patch checks for."""
    results = _sweep(("registered_domains",))
    leaks = [r for r in results if r["leaking_sinks"]]
    assert not leaks, _leak_report(leaks, len(results))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-326: providers.py:110 formats f'injected code creator {creator!r} …' — the "
        "byte-identical shape T-264 repaired one file over, on the sibling injected argument. "
        "Measured at HEAD: accept(..., code_creator=object()) yields 'checkout_refused: "
        "CheckoutCreatorError: injected code creator <object object at 0x…> …', persisted in a "
        "policy_event and republished verbatim in the 409 body because checkout_refused is a "
        "DECLARED code, so _denied never re-labels it. Remove this marker with the fix"
    ),
)
def test_t326_an_unusable_code_creator_leaks_no_memory_address(unwired: None) -> None:
    """T-326 — the case the green HEAD gate never drives, because it never varies this argument."""
    results = _sweep(("code_creator",))
    leaks = [r for r in results if r["leaking_sinks"]]
    assert not leaks, _leak_report(leaks, len(results))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The property both tickets are instances of, stated once over every collaborator: "
        "registered_domains, code_creator, eligibility AND the offer the bidding store wrote. "
        "Measured 13 leaking cases of 18 at HEAD, across four f-string sites — "
        "checkout/providers.py:110, checkout/provider.py:1103-1105, checkout/codes.py:187+224 "
        "and eligibility/__init__.py:175+194 — every one amplified by the pass-through at "
        "accept/offer.py:541. A sanitising pass inside reasons.denial_reason() closes twelve "
        "of the thirteen; the thirteenth is the BYPASS — accept/gate.py::_denial_reason "
        "returns a reason that already names its declared code VERBATIM and never calls "
        "denial_reason() at all, so that site needs its own repair. Remove this marker with "
        "the fix"
    ),
)
def test_no_injected_collaborators_repr_reaches_any_denial_sink(unwired: None) -> None:
    """The whole property. Strictly stronger than either ticket node above, and unentangled:

    neither T-264 nor T-326 is pointed here, because a gate a lane cannot turn green by doing
    its own ticket's work is the T-325 defect, and manufacturing one while fixing T-325 would
    be a poor joke.
    """
    results = _sweep()
    leaks = [r for r in results if r["leaking_sinks"]]
    assert not leaks, _leak_report(leaks, len(results))


# =====================================================================================
# T-325 — a ticket whose gate is selected only by ANOTHER ticket's test nodes
# =====================================================================================
#
# Not a denial-reason property, and it is here rather than in
# ``apps/exchange/tests/test_repro_open_tickets.py`` — the file T-325's own scope names —
# because a sibling lane owns that file in this batch and two lanes appending to one file
# corrupt each other. Nothing below reads that file; the scan is over ``tickets.json`` alone.

#: A test node named ``test_t<NNN>_…`` announces which ticket it grades. This reads that
#: announcement back out of a ``-k`` selector.
TICKET_TEST_NODE = re.compile(r"test_t(\d{3})[a-z0-9_]*")


def _ticket_gate_ownership() -> tuple[list[dict[str, Any]], int]:
    """``(selectors that name ticket-numbered nodes, total tickets)`` read from tickets.json."""
    import shlex
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    document = json.loads((root / "tickets.json").read_text())
    tickets = document["tickets"] if isinstance(document, dict) else document

    entries: list[dict[str, Any]] = []
    for ticket in tickets:
        verify = str(ticket.get("verify") or "")
        try:
            tokens = shlex.split(verify)
        except ValueError:
            continue
        if "-k" not in tokens:
            continue
        selector = tokens[tokens.index("-k") + 1] if tokens[-1] != "-k" else ""
        named = sorted({n.lstrip("0") for n in TICKET_TEST_NODE.findall(selector)})
        if not named:
            continue
        entries.append(
            {
                "ticket": ticket["id"],
                "own": str(ticket["id"]).split("-")[-1].lstrip("0"),
                "names": named,
                "selector": selector,
            }
        )
    return entries, len(tickets)


def test_the_ticket_gate_ownership_scan_is_armed() -> None:
    """Not xfail. Four ways the scan below could find nothing and report success anyway."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    assert (root / "tickets.json").is_file(), f"{root / 'tickets.json'} does not exist"

    entries, total = _ticket_gate_ownership()
    assert total >= 200, f"tickets.json holds {total} ticket(s); it held 292 when this was written"
    assert len(entries) >= 20, (
        f"only {len(entries)} verify selector(s) name a test_t<NNN> node; 33 did when this was "
        "written, and a scan that parses none of them cannot find an entangled one"
    )
    assert all(e["names"] for e in entries), "an entry was recorded with no named node"
    assert TICKET_TEST_NODE.findall("-k test_t312_the_exchange_serves"), "the node pattern is dead"
    assert not TICKET_TEST_NODE.findall("-k test_a_reason_round_trips"), (
        "the node pattern matches an unnumbered node; every ticket would look entangled"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-325: amendment 19 repointed T-266 and T-296 at test_t312_* nodes, which are T-312's "
        "own tests, and amendment 18 gave T-312 the selector `-k test_t312` — which selects "
        "both. Measured at HEAD over all 292 tickets: 33 selectors name a test_t<NNN> node and "
        "exactly 2 of them name only ANOTHER ticket's, so fixing T-312 turns T-266's and "
        "T-296's gates green while neither ticket is resolved. Remove this marker with the fix"
    ),
)
def test_t325_no_tickets_gate_is_selected_only_by_another_tickets_nodes() -> None:
    """A ticket must be able to turn its own gate green by doing its own work."""
    entries, _total = _ticket_gate_ownership()
    entangled = [e for e in entries if e["own"] not in e["names"]]
    assert not entangled, "\n".join(
        [f"{len(entangled)} ticket gate(s) are selected only by another ticket's test nodes:"]
        + [
            f"  · {e['ticket']} is graded by test_t{e['names'][0]}_… "
            f"(names {e['names']}, not {e['own']}): {e['selector'][:70]}"
            for e in entangled
        ]
    )
