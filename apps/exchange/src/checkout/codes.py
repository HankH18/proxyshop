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

__all__ = [
    "CODE_ALPHABET",
    "CODE_BODY_LENGTH",
    "CODE_PREFIX",
    "MAX_CODE_TTL_SECONDS",
    "RandomSource",
    "build_cart_permalink",
    "code_expiry",
    "mint_code",
]

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


def code_expiry(now: float, offer: Mapping[str, Any] | None = None) -> float:
    """D22: the code dies at ``min(now + 48h, offer.expires_at)``.

    A code outliving the offer it discounts is a discount the seller never agreed to.
    """
    ceiling = float(now) + MAX_CODE_TTL_SECONDS
    expires_at = (offer or {}).get("expires_at")
    if expires_at is None:
        return ceiling
    return min(ceiling, float(expires_at))


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
