"""The D22 code shape, and the one place the merchant service mints a code.

D22 pins every ProxyShop discount code::

    PSX-XXXXXXXX      # 8 characters of *randomly generated* Crockford base32, uppercase
    usageLimit: 1
    expiry: min(now + 48h, offer.expires_at)

"Randomly generated" is the load-bearing half and it is the reason this module takes **no
offer identifier at all**. A code derived from ``offer_id`` — a hash, a truncation, an
encoding, a counter — is a *guessable* single-use redeemable, which turns a private
discount into a public one. :func:`mint_code` therefore draws from :mod:`secrets`, and the
only way to make it deterministic is to hand it an explicit ``rng``, which the tests do and
nothing in production does. ``apps/exchange/src/checkout/codes.py`` and
``services/shopify-stub/src/codes.py`` state the same rule for the two other places a code
can come into existence; the shape is deliberately identical across all three so that a
``discount_code`` join key means the same thing whichever path minted it.

Crockford base32 excludes ``I``, ``L``, ``O`` and ``U``, so a code read aloud or typed from
a screenshot cannot be transcribed into a *different* valid code. :func:`canonical_code`
completes that promise on the redemption side: it folds the confusable characters back and
upper-cases, so ``psx-1abcdefg`` and ``PSX-IABCDEFG`` are the same redeemable rather than
one redeemed code and one silent miss. That folding can never merge two *minted* codes,
because a minted body contains none of the folded characters.
"""

from __future__ import annotations

import secrets
import unicodedata
from typing import Any, Protocol

__all__ = [
    "CODE_ALPHABET",
    "CODE_BODY_LENGTH",
    "CODE_PREFIX",
    "RandomSource",
    "canonical_code",
    "is_well_formed",
    "mint_code",
]

#: Crockford base32, uppercase. 32 symbols; ``I``, ``L``, ``O``, ``U`` are absent.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: The prefix every ProxyShop discount code carries (D22).
CODE_PREFIX = "PSX-"

#: Number of random Crockford symbols after the prefix (D22).
CODE_BODY_LENGTH = 8

#: Crockford's own transcription rule: ``I`` and ``L`` read as ``1``, ``O`` reads as ``0``.
#: ``U`` is excluded from the alphabet but folds to nothing, so it is left alone.
_CROCKFORD_FOLD = str.maketrans({"I": "1", "L": "1", "O": "0"})


class RandomSource(Protocol):
    """Just enough of :class:`random.Random` to pick characters. Injected only by tests."""

    def choice(self, seq: Any) -> Any: ...


def mint_code(*, rng: RandomSource | None = None) -> str:
    """Mint one single-use discount code: ``PSX-`` + 8 random Crockford base32 characters.

    Args:
        rng: a random source exposing ``choice``. Left unset — which is every production
            call — the characters come from :func:`secrets.choice`, so the code is not
            derivable from the offer, the auction, or anything else an outsider can see.

    Returns:
        e.g. ``"PSX-7QK2ZB0M"``.
    """
    pick = rng.choice if rng is not None else secrets.choice
    body = "".join(str(pick(CODE_ALPHABET)) for _ in range(CODE_BODY_LENGTH))
    return f"{CODE_PREFIX}{body}"


def is_well_formed(code: Any) -> bool:
    """``True`` iff ``code`` matches the D22 shape exactly, as minted (uppercase)."""
    if not isinstance(code, str) or not code.startswith(CODE_PREFIX):
        return False
    body = code[len(CODE_PREFIX) :]
    if len(body) != CODE_BODY_LENGTH:
        return False
    return all(character in CODE_ALPHABET for character in body)


def canonical_code(raw: Any) -> str:
    """The one spelling of a code the redemption register is keyed by.

    Shopify matches discount codes case-insensitively at the till, so ``psx-7qk2zb0m`` off
    a webhook and ``PSX-7QK2ZB0M`` as minted are one redeemable. A register keyed by the
    raw string would treat them as two, and "single use" would then hold per spelling — the
    same shape of hole as the dual module identity this package's ``_spellings`` closes.

    NFKC first, because a full-width ``ＰＳＸ`` or a non-breaking space is a *different*
    string that renders identically; then whitespace out, then upper-case, then Crockford's
    confusable fold. A value that is not a string canonicalises to ``""``, which no minted
    code ever equals.
    """
    if not isinstance(raw, str):
        return ""
    folded = unicodedata.normalize("NFKC", raw)
    folded = "".join(character for character in folded if not character.isspace())
    return folded.upper().translate(_CROCKFORD_FOLD)
