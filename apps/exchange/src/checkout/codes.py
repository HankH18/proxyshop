"""The one place a discount code is minted, and the one place a permalink is built (D22).

Code shape, pinned by D22: ``PSX-`` plus eight characters of **randomly generated** Crockford
base32, upper case, single use, expiring at ``min(now + 48h, offer.expires_at)``.

*Randomly generated* is the load-bearing half. Deriving a code from the ``offer_id`` — or
from the auction id, or a counter — makes every outstanding redeemable guessable from public
identifiers, which turns a single-use discount into a public one. :func:`mint_code` therefore
draws from :mod:`secrets`, and the only way to make it deterministic is to hand it an
explicit ``rng``, which the tests do and nothing in production does.

Permalink shape, also D22, used identically by the stub parser (T-013), the merchant builder
(T-052) and this module::

    https://{shop_domain}/cart/{variant_id}:{quantity}?discount={code}

**This module is the whole of the exchange's code-minting surface.** Nothing outside
``apps/exchange/src/checkout/`` may mint a code or call an injected code creator — see
:mod:`apps.exchange.src.checkout.lint`, which enforces that mechanically over the source
tree rather than by convention.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import quote

from contracts.boundary import parse_timestamp

__all__ = [
    "CODE_ALPHABET",
    "CODE_BODY_LENGTH",
    "CODE_PREFIX",
    "MAX_CODE_TTL_SECONDS",
    "RandomSource",
    "UnusableOffer",
    "assert_offer_is_mintable",
    "build_cart_permalink",
    "code_expiry",
    "expiry_epoch",
    "mint_code",
    "offer_quantity",
]


class UnusableOffer(ValueError):
    """An offer field a code cannot be built from — caught *before* anything is minted."""


#: Crockford base32: no I, L, O or U — no transcription ambiguity, and no accidental words.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_PREFIX = "PSX-"
CODE_BODY_LENGTH = 8
#: D22: expiry is `min(48h, offer.expires_at)`.
MAX_CODE_TTL_SECONDS = 48 * 60 * 60


class RandomSource(Protocol):
    """Just enough of :class:`random.Random` to pick characters. Injected only by tests."""

    def choice(self, seq: Any) -> Any: ...


def mint_code(*, rng: RandomSource | None = None) -> str:
    """Mint one single-use discount code: ``PSX-`` + 8 random Crockford base32 characters.

    ``rng`` exists so a test can pin the output. Left unset — which is every production
    call — the characters come from :func:`secrets.choice`, so the code is not derivable
    from the offer, the auction, or anything else an outsider can see.
    """
    pick = rng.choice if rng is not None else secrets.choice
    body = "".join(str(pick(CODE_ALPHABET)) for _ in range(CODE_BODY_LENGTH))
    return f"{CODE_PREFIX}{body}"


def expiry_epoch(expires_at: Any) -> float:
    """One offer expiry, as epoch seconds, in every spelling the *schema* permits.

    The contradiction this function exists to end (T-182): ``contracts.Offer.expires_at`` is
    typed ``str | None`` with ``format: date-time``, and ``validate_bid`` parses it with
    :func:`contracts.parse_timestamp`. This module used to parse it with ``float()``. So the
    only expiry shape the schema allowed — an ISO-8601 instant — was the one the minting path
    refused, and the only shape the mint accepted — a bare epoch number — was one the schema
    forbids. Every schema-valid offer carrying an expiry was therefore unmintable.

    The fix is to read it the way the boundary already reads it rather than to invent a
    second parser: two parsers eventually disagree about some instant, and the instant they
    disagree about is one where an offer validates at the door and dies at the till.
    ``float()`` is still tried first, so every epoch spelling that worked before still works
    — this widens what is accepted and narrows nothing.

    Raises:
        UnusableOffer: the value is neither a number nor an instant anything can read.
    """
    try:
        return float(expires_at)
    except (TypeError, ValueError):
        pass
    parsed = parse_timestamp(expires_at)
    if parsed is None:
        raise UnusableOffer(
            f"offer expires_at {expires_at!r} is neither an epoch number nor an RFC-3339 "
            f"instant, so the code's D22 expiry cannot be computed"
        )
    return parsed.timestamp()


def code_expiry(now: float, offer: Mapping[str, Any] | None = None) -> float:
    """D22: the code dies at ``min(now + 48h, offer.expires_at)``.

    A code outliving the offer it discounts is a discount the seller never agreed to.

    Raises:
        UnusableOffer: ``expires_at`` is present but is not an instant — see
            :func:`expiry_epoch` for the spellings that are. The offer is a bidder's own
            JSON, so this is reachable input, and it must be refused **before** minting — see
            :func:`assert_offer_is_mintable`, which the port runs ahead of every provider for
            exactly this reason.
    """
    ceiling = float(now) + MAX_CODE_TTL_SECONDS
    expires_at = (offer or {}).get("expires_at")
    if expires_at is None:
        return ceiling
    return min(ceiling, expiry_epoch(expires_at))


def offer_quantity(offer: Mapping[str, Any] | None = None) -> int:
    """The permalink's quantity: a positive integer, or 1 when the offer does not say.

    Raises:
        UnusableOffer: ``quantity`` is present but is not a positive whole number.
    """
    quantity = (offer or {}).get("quantity")
    if quantity is None or quantity == "":
        return 1
    try:
        value = int(quantity)
    except (TypeError, ValueError) as exc:
        raise UnusableOffer(f"offer quantity {quantity!r} is not a whole number") from exc
    if value < 1:
        raise UnusableOffer(f"offer quantity {quantity!r} is not a positive quantity")
    return value


def assert_offer_is_mintable(offer: Mapping[str, Any] | None) -> None:
    """Refuse an offer whose code-shaping fields will not parse, before a code exists.

    The order this runs in is the whole point. ``code_expiry`` and the permalink builder are
    both reached *after* the provider has minted — after the merchant's ``POST /codes`` has
    already issued a real single-use discount, in the Shopify adapter's case. A malformed
    ``expires_at`` therefore used to raise with a live code loose in the merchant's account
    and no ``code_created`` event recorded for it: the exchange had handed out a discount it
    had no record of and no way to expire. Validating here makes that unreachable — the
    request is refused with nothing minted anywhere.
    """
    code_expiry(0.0, offer)
    offer_quantity(offer)


def build_cart_permalink(
    *,
    shop_domain: str,
    code: str,
    variant_id: str | int = 1,
    quantity: int = 1,
) -> str:
    """Build the D22 cart permalink. The host is the caller's registered domain, verbatim."""
    return (
        f"https://{shop_domain}/cart/{quote(str(variant_id), safe='')}:{int(quantity)}"
        f"?discount={quote(str(code), safe='')}"
    )
