"""The two starting implementations of :class:`CheckoutProvider`.

:class:`SimulatedRedirectProvider` is the **required** starting implementation (D45). It
mints the code locally and builds the D22 cart permalink itself, so the whole accept path
runs with no Shopify, no merchant app, no network and no injected client — which is what
makes the starting-slice demo reachable without the Shopify install lane.

:class:`ShopifyCheckoutProvider` is the adapter for the ``shopify`` / ``shopify_stub``
spellings. It does **not** mint anything itself: it delegates to the injected merchant
``POST /codes`` client — exactly the client the frozen suite stands in for with its
``_RecordingCodeCreator`` — and adapts the reply to :class:`MintedCheckout`. T-052 owns
what lives behind that client (the real Shopify GraphQL discount mutation); swapping it in
changes nothing here, which is the whole reason the port exists.

Both providers pass through the same final :meth:`CheckoutProvider.checkout`, so both are
domain-checked before minting and both emit the identical ordered `LedgerEvent` kinds. That
is C11, held by construction rather than by two implementations agreeing.
"""

from __future__ import annotations

from typing import Any

from contracts.boundary import (
    MINTED_CODE_SITE,
    REASON_CODE_UNUSABLE,
    discount_code_reasons,
    minted_code_reasons,
)

from .. import describe
from ..auction.ledger import MalformedLedgerPayload, record_audit_anomaly
from .codes import code_expiry, mint_code, record_minted_code
from .provider import (
    CheckoutProvider,
    CheckoutRequest,
    MintedCheckout,
    OrphanedCheckoutCode,
    OrphanedCode,
    _sanitised_cause,
    code_fingerprint,
    default_permalink,
    safe_token,
)

__all__ = ["CheckoutCreatorError", "ShopifyCheckoutProvider", "SimulatedRedirectProvider"]


class CheckoutCreatorError(RuntimeError):
    """The injected code creator was missing, or answered in a shape we cannot use."""


def _reply_shape(reply: Any) -> str:
    """Describe a merchant reply without quoting any of its VALUES (T-215).

    Used on the one path where redaction is impossible in principle: the reply did not spell
    its code where this adapter looks, so there is no code value to redact by — and dumping
    the reply to find out what it *did* say is how the code gets published. The keys and the
    type are the whole diagnostic; the values are the merchant's, and unexamined.
    """
    try:
        keys = getattr(reply, "keys", None)
        if callable(keys):
            return f"a {type(reply).__name__} with keys {sorted(str(k) for k in keys())}"
        return f"a {type(reply).__name__} (no mapping keys; values not quoted)"
    except Exception:  # pragma: no cover - a hostile reply object must not crash the refusal
        return "an unreadable reply object (values not quoted)"


#: ``code_unusable:code:whitespace`` — the ONE boundary verdict this adapter does not act on by
#: itself, and the only place the exchange's mint declines to follow the contracts door.
#:
#: **It is declined because acting on it destroys a revocable discount.** A refusal here cannot
#: carry the code out: ``OrphanedCode.code`` is a ``str`` and the only string available is
#: ``str(<the unreadable value>)``, which is the coercion T-345 exists to refuse — so every code
#: this gate refuses is one the exchange can never record or revoke. That price is right for a
#: value that is not a code at all (an ``object()``, a mapping, an empty string): there is nothing
#: to record. It is wrong for a string the merchant really wrote, which records and revokes
#: perfectly well. ``test_orphaned_code.py::test_no_spelling_of_the_permalink_leaks_the_code
#: [plus-encoded-permalink]`` is that case measured — a live ``'PSX LIVE 5'`` on an off-domain
#: permalink, which must reach the port's host check, be refused there, and come back with a
#: ``code_created`` event and an orphan the caller can revoke (T-202). Refusing it one step earlier
#: turns that live discount into an unrecorded one, which is the T-157 harm this package is built
#: around, arriving through a wall meant to prevent harm.
#:
#: It is also what the door itself admits everywhere else: any non-blank printable string, in the
#: merchant's own vocabulary, because pinning a code SHAPE here would refuse the real adapter's
#: answers. A space is printable and Shopify permits one in a code.
#:
#: **The half of the verdict that is kept** is the half that truncates. ``whitespace`` covers the
#: whole shared trim set, so a ``\n``, a ``\t``, a NUL-adjacent C0 point or a non-breaking space
#: reports under this one name and nothing else (the boundary's blank test runs BEFORE its control
#: test, and they are mutually exclusive) — and a newline inside a code turns one code into two
#: somewhere downstream. ``str.isprintable`` is exactly the line between them: of the entire trim
#: set, the plain ASCII space is the only printable member, so a code admitted by this carve-out
#: differs from an ordinary one by spaces and nothing else, and is carried into the permalink
#: percent-encoded, verbatim, untrimmed.
_WHITESPACE_CODE_REASON = f"{REASON_CODE_UNUSABLE}:{MINTED_CODE_SITE}:whitespace"


def _acted_on(refusals: list[str], code: Any) -> list[str]:
    """The refusals the mint enforces: every one the boundary gave, bar the carve-out above.

    ``refusals == [reason]`` and not a membership test: the carve-out applies to a code whose
    ONLY fault is a blank, so a code that is also too long, or that arrived on a reply this door
    cannot read at all, is refused with its whitespace complaint intact.
    """
    if refusals == [_WHITESPACE_CODE_REASON] and isinstance(code, str) and code.isprintable():
        return []
    return refusals


def _code_refusals(reply: Any, code: Any) -> list[str]:
    """Every way the boundary refuses to read this reply's code, judged at both readings.

    :func:`contracts.boundary.minted_code_reasons` is the door the contracts boundary publishes
    for exactly this reply, and it is the one that answers ``unreadable_reply`` — a merchant that
    sent back a bare string, a list or a number rather than a record at all.

    It is asked about the value it reads for itself, and this adapter is then asked about the value
    it read for itself, because **the two readings can differ and the one that matters is the one
    that would be coerced.** :func:`_read` here takes any object exposing ``get`` at its word;
    ``boundary._get`` reserves ``get`` for a real :class:`~collections.abc.Mapping` and falls back
    to ``getattr``. A reply that is not a ``Mapping`` but has a ``get`` method — ``HostileReply`` in
    the frozen suite is precisely that shape — is therefore read one way here and another way
    there, and a wall that judges a different value from the one the code is minted out of is not a
    wall. Both verdicts, deduplicated, order preserved.
    """
    refused = list(minted_code_reasons(reply))
    for reason in discount_code_reasons(code):
        if reason not in refused:
            refused.append(reason)
    return _acted_on(refused, code)


def _record_unreadable_minted_code(
    request: CheckoutRequest, provider: str, reply: Any, code: Any, refusals: list[str]
) -> None:
    """Record that a merchant may hold a LIVE code this exchange could not read (T-345).

    Refusing the reply is only half the repair, and the other half is why this exists. A merchant
    that answers ``POST /codes`` at all has almost certainly minted something: the exchange is
    refusing the *description*, not the discount. That is an orphan by the definition
    :class:`~.provider.OrphanedCode` is built on — a code that exists in the merchant's system for a
    checkout that was refused — and dropping the refusal on the floor would restore T-157 through a
    new door.

    **It cannot be an** :class:`~.provider.OrphanedCheckoutCode`, and the reason is the ticket
    itself. That exception carries the code out so ``accept()`` can file a ``code_created`` event
    for it, and ``OrphanedCode.code`` is a ``str``. There is no string here that is not
    ``str(<unreadable value>)`` — the coercion this whole gate exists to refuse, whose output was
    measured landing in a persisted event and a ``?discount=`` query as
    ``'<object object at 0x100c312f0>'``. Manufacturing a code to report the absence of a readable
    one is the defect wearing a bandage.

    So the report goes down the channel built for a record that cannot be made
    (:class:`~..auction.ledger.AuditAnomaly`, T-283): ids and key NAMES only, never a payload value.
    Its own docstring names this exact distinction as its purpose — "the ledger has no
    ``code_created`` for this checkout" being distinguishable from "no code was ever created" — and
    that is the whole of what an operator can act on here. The join keys point at the merchant
    account and the bid; the ``problem`` names the machine-readable refusals and the reply's SHAPE
    (:func:`_reply_shape`: its type and its keys, never its values, because the value we are
    complaining about may itself be a live discount).

    ``fingerprint`` is the join, and it is the ONLY thing about the code itself that goes in: it
    is a one-way hash, it is what every other post-mint refusal in this package correlates on, and
    it is what matches this anomaly to the ``denial_reason`` the refusal publishes. For a code that
    is not a string it is a hash of that value's ``str``, which joins the two sides of this one
    refusal and nothing else — no worse than the ``code:unknown`` it degrades to, and never the
    value.

    Never raises. It is called from the one region where an exception loses the very thing it was
    reporting, and it is a report — not the refusal.
    """
    try:
        record_audit_anomaly(
            # A code-authored call site, never `provider`: an adapter is free to name itself
            # after anything at all (T-215), and its name goes in the context below where it is
            # one value among several rather than the label the anomaly is filed under.
            "ShopifyCheckoutProvider.mint",
            "code_created",
            MalformedLedgerPayload(
                f"the merchant code creator answered {_reply_shape(reply)}, whose code the "
                f"boundary refuses: {', '.join(refusals)}; no code_created record can be built "
                f"for this checkout, and the merchant may hold a live discount for it"
            ),
            provider=provider,
            auction_id=request.auction_id,
            store_id=request.store_id,
            bid_ref=request.bid_ref,
            code_reasons=tuple(refusals),
            fingerprint=code_fingerprint(code),
        )
    except Exception:  # pragma: no cover - reporting must never be the thing that fails
        pass


class SimulatedRedirectProvider(CheckoutProvider):
    """Mint locally, redirect to the seller's own cart permalink. No Shopify, no merchant.

    ``rng`` is a test seam only: unset, the code comes from :mod:`secrets`, so it is not
    derivable from the offer or the auction (D22).
    """

    name = "simulated-redirect"

    def __init__(self, *, rng: Any | None = None) -> None:
        self._rng = rng

    def mint(self, request: CheckoutRequest) -> MintedCheckout:
        code = mint_code(rng=self._rng)
        return MintedCheckout(
            code=code,
            permalink_url=default_permalink(request, code),
            expires_at=code_expiry(request.now, request.offer),
            details={"minted_by": self.name, "single_use": True},
        )


class ShopifyCheckoutProvider(CheckoutProvider):
    """Delegate minting to the injected merchant ``POST /codes`` client.

    The client is whatever ``accept()`` was handed — a real merchant HTTP client in a
    deployment, the frozen suite's recorder in a test, T-052's Shopify-backed creator once
    it lands. Two call styles are supported because the suite's double exposes both
    (``create_code(store_id, offer)`` and ``__call__``); ``create_code`` is preferred.

    A creator that answers with no ``code`` is an error, not a silent fallback to minting
    locally: falling back would mean the buyer walks away with a code the merchant never
    issued, which is a discount nobody agreed to honour.
    """

    name = "shopify"

    def mint(self, request: CheckoutRequest) -> MintedCheckout:
        creator = request.code_creator
        if creator is None:
            raise CheckoutCreatorError(
                f"{self.name} checkout needs an injected code creator (the merchant "
                f"POST /codes client); accept() was called without one"
            )

        create = getattr(creator, "create_code", None)
        if not callable(create):
            if not callable(creator):
                # T-326: `describe`, not `{creator!r}`. `creator` is an object the CALLER
                # injected, this message becomes `denial_reason` through
                # `accept/offer.py`'s checkout handler, and `checkout_refused` is a
                # DECLARED code — so `_denied` republishes the sentence verbatim in the 409
                # and `_refusal_event` persists it. A default `__repr__` here put this
                # process's memory layout in both. The class name is the half an operator
                # can act on and it is kept.
                raise CheckoutCreatorError(
                    f"injected code creator {describe(creator)} exposes neither "
                    f"create_code(...) nor __call__(...)"
                )
            create = creator

        reply = create(request.store_id, request.offer)
        code = _read(reply, "code")
        if not code:
            # T-215 pass 3: the SHAPE of the reply, not the reply. `{reply!r}` published the
            # merchant's entire answer into `denial_reason` and the persisted `policy_event`
            # — and the branch we are in is precisely "this reply does not spell its code
            # where we look", so a merchant that answered `{"discount_code": "PSX-…"}` had a
            # live discount dumped verbatim into both. Nothing here can redact it: with no
            # `code` field there is no value to redact BY. Naming the keys and the type
            # keeps the whole diagnostic — an operator debugging this needs to know which
            # key the merchant used, not what was in it.
            raise CheckoutCreatorError(
                f"the merchant code creator returned no code for {request.store_id!r}: "
                f"{_reply_shape(reply)}"
            )

        # T-345 — IS THIS A CODE? Nothing used to ask, and `str()` never fails, so the answer
        # was manufactured rather than demanded. Measured through `accept()` at HEAD, a creator
        # answering `{"code": object()}`::
        #
        #     accepted=True  denial_reason=None
        #     code='<object object at 0x100c312f0>'
        #     code_created payload={'code': '<object object at 0x100c312f0>', ...}
        #     permalink 'https://…/cart/1:1?discount=%3Cobject%20object%20at%200x100c312f0%3E'
        #
        # — a LIVE single-use discount whose spelling is this process's memory layout, in a
        # persisted event and in a query string handed to a buyer. Accepting is worse than
        # leaking here: a wrongly ACCEPTED checkout leaves no denial reason for anyone to read,
        # which is why the denial-leak sweep that found this could not see the class at all.
        #
        # It runs BEFORE the coercion below, and that ordering is the fix — `str(code)` is
        # exactly the manufacture being refused, and one call to it publishes the address into
        # the minting ledger the port reads on failure.
        #
        # The falsy branch above stays in front of it deliberately: "the merchant answered no
        # code" and "the merchant answered something that is not one" are different repairs for
        # an operator (one is a missing field, the other a wrong value), and the first has its
        # own measured diagnostic and its own frozen refusal shape.
        refusals = _code_refusals(reply, code)
        if refusals:
            # The merchant was reached and answered, so a discount plausibly exists that this
            # exchange cannot name. Recorded before the raise, for the same reason
            # `record_minted_code` is called before anything that could raise.
            _record_unreadable_minted_code(request, self.name, reply, code, refusals)
            # `_reply_shape` and the boundary's own reason strings, and NOTHING else. This
            # sentence becomes `denial_reason` through `accept/offer.py`'s checkout handler, so
            # `_denied` republishes it verbatim in the 409 and `_refusal_event` persists it
            # (T-326) — and the value being complained about is the one value in the process
            # that must not be published, since it may be a redeemable discount. The keys, the
            # type and the machine-readable refusals are the whole diagnostic.
            #
            # The FINGERPRINT is the one thing about the code itself that may be published,
            # and it is required rather than decorative: this refusal is a post-mint one in
            # every way that matters to an operator, so it carries the same join every other
            # post-mint refusal in this package carries — the handle that matches this
            # sentence to the audit anomaly recorded just above. `code_fingerprint` is a
            # one-way hash and fails closed on a hostile `__str__`; hashing the value for a
            # correlation token is not the coercion this gate refuses, which is the value
            # BECOMING the code — it reaches no ledger, no permalink and no event.
            raise CheckoutCreatorError(
                f"the merchant code creator answered {_reply_shape(reply)} for "
                f"{request.store_id!r}, and its code {code_fingerprint(code)} is not one this "
                f"exchange can read ({', '.join(refusals)}); refusing rather than coercing it "
                f"— the merchant may hold a live discount for this checkout, recorded as an "
                f"audit anomaly against the same fingerprint"
            )

        # From here the merchant's code EXISTS — so the FIRST thing done with it is to
        # report it into the port's minting ledger, before anything that could raise. The
        # port opens that ledger around its call to `mint` and reads it in its own handler,
        # so this adapter is covered by the port's T-202 guarantee the same way
        # `SimulatedRedirectProvider` is, and the handler below is the more informative
        # inner layer rather than the only one. A merchant's code is not minted through
        # `mint_code`, which reports itself — so it has to be reported here.
        record_minted_code(str(code))

        # `CheckoutProvider.checkout` cannot guard this
        # region — it only wraps what happens after `mint` returns — so anything that raises
        # between the merchant's answer and that return would lose the code exactly the way
        # T-157 lost it one level up. Nothing below is *expected* to raise: the port already
        # resolved the registered domain and already proved the offer's fields parse. But
        # "already proved" is a claim about a previous call, and `default_permalink` resolves
        # the domain a *second* time — a lookup that answers once and fails once (a dropped
        # connection, a cache eviction) is all it takes.
        permalink: Any = ""
        try:
            permalink = _read(reply, "permalink_url") or _read(reply, "permalink")
            if not permalink:
                # The merchant may return only the code; the permalink shape is pinned by D22
                # and identical on both paths, so building it here is not a divergence.
                permalink = default_permalink(request, str(code))

            return MintedCheckout(
                code=str(code),
                permalink_url=str(permalink),
                expires_at=code_expiry(request.now, request.offer),
                details={"minted_by": self.name, "delegated_to": type(creator).__name__},
            )
        except Exception as exc:
            # The cause is redacted BEFORE it is chained, for the reason spelled out at the
            # matching site in `provider.py`: the default excepthook reads the C-level cause
            # slot and never runs `OrphanedCheckoutCode`'s reading properties, so only an
            # in-place edit of this object's `args` closes that channel.
            cause = _sanitised_cause(exc, str(code), (str(permalink or ""),))
            raise OrphanedCheckoutCode(
                # `{exc}` is arbitrary post-mint text — `default_permalink` and
                # `code_expiry` both format merchant-authored offer fields into their own
                # messages — and `store_id` is read off the bid the merchant wrote. Both go
                # through the fail-closed guard at the site that BUILDS this sentence,
                # because the constructor downstream can only remove values it was handed.
                f"{safe_token(type(exc).__name__, str(code), label='cause-type')}: "
                f"{safe_token(exc, str(code), label='cause')} — "
                f"raised AFTER the merchant issued {code_fingerprint(str(code))} for store "
                f"{safe_token(request.store_id, str(code), label='store')!r}; the code is "
                f"live and must be recorded and revoked",
                orphan=OrphanedCode(
                    code=str(code),
                    # Read off the local, never back off `reply`: if `_read(reply, ...)` is
                    # what raised, reading it again raises inside the handler and the orphan
                    # is lost — which is the exact failure this block exists to prevent. The
                    # permalink is a nicety here anyway; the CODE is what has to be revoked.
                    permalink_url=str(permalink or ""),
                    provider=self.name,
                    store_id=request.store_id,
                    auction_id=request.auction_id,
                    bid_ref=request.bid_ref,
                ),
            ) from cause


def _read(reply: Any, key: str) -> Any:
    """Read ``key`` off a mapping-or-object reply, tolerating either shape."""
    if reply is None:
        return None
    if hasattr(reply, "get"):
        return reply.get(key)
    return getattr(reply, key, None)
