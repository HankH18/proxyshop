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
                raise CheckoutCreatorError(
                    f"injected code creator {creator!r} exposes neither create_code(...) "
                    f"nor __call__(...)"
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
