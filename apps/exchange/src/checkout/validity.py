"""R3's VALIDATE half: does the code that was just minted actually apply at the cart?

R3, verbatim: "WHEN the buyer accepts an offer, THE SYSTEM SHALL create a single-use
discount code on that store, **validate it (validity window, combinesWith)**, and redirect
to the store's checkout via cart permalink with the code pre-applied."

The minting half and the permalink half were built and served. The sentence between them
was not written at all on this side, and its absence was measured over the served route —
``POST /auctions/{auction_id}/accept`` with a bid whose offer had expired years earlier::

    200 {"permalink_url": "https://store-a.example.com/cart/1:1?discount=PSX-6SMY54PJ",
         "code": "PSX-6SMY54PJ", "notice": null}

``codes.code_expiry`` had *computed* that window — ``min(now + 48h, offer.expires_at)`` —
and the port published it in the ``code_created`` body and on ``CheckoutResult``. Nothing
compared it to ``now``. A shopper was handed a real single-use discount that was already
dead, and the only place the deadness was visible was a number in a ledger event.

Two questions, and the second one needs its scope stated
--------------------------------------------------------

**The validity window** is entirely the exchange's to answer: it holds ``now``, it holds
the offer's ``expires_at``, and D22 pins the arithmetic. :func:`window_reason` is that
comparison and nothing more.

**combinesWith** is Shopify's word for "may this discount sit on a cart beside the shop's
other discounts", and the authoritative answer lives where the shop's automatic discounts
are — ``apps/merchant/svc/src/codes/combines.py``, which asks the Admin API and is reached
by the merchant's own served ``POST /codes``. This module does **not** duplicate that and
must not: the exchange has no Admin token, no shop, and `contracts.Offer` (``extra:
"forbid"``) carries no combination policy, so any verdict invented here would be a guess
about somebody else's shop.

What the exchange *does* hold is the thing R3's last clause is actually about — **the cart
it is about to send the buyer to.** The permalink is ``…/cart/{variant}:{qty}?discount=
{code}``, one ``discount`` parameter, and on the Shopify path it is the merchant's own
string taken verbatim (``providers.ShopifyCheckoutProvider.mint`` prefers
``reply["permalink_url"]``). A permalink that pre-applies a *different* code is a cart that
already carries a discount, and the one the exchange just minted is not it. That is
observable, it is local, and it is consequential three ways over:

* the buyer redeems somebody else's discount, at a price nobody promised;
* the single-use code the exchange recorded is never redeemed, so it stays live; and
* ``apps/trust``'s reconciler joins an order to an offer on ``discount_codes[].code``, so
  the order can never be matched to the acceptance that caused it.

:func:`cart_conflict_reason` answers exactly that and claims nothing wider. "This cart
applies a discount that is not ours" is a fact; "this shop's automatic discounts refuse to
combine" is the merchant's call, and it stays the merchant's.

What is deliberately NOT refused
--------------------------------

**A permalink carrying no ``discount`` at all.** Absent and conflicting are different
conditions — the same distinction :mod:`.domain` draws about a missing host — and this
repo's own merchant doubles return bare cart URLs (``{"permalink_url":
"https://…/cart/1:1"}``). Nothing else is on that cart, so nothing conflicts. A code that
was not pre-applied is a *different* complaint about the same URL, and inventing a wall for
it here would refuse replies the port already accepts.

**An expiry this module cannot read.** ``MintedCheckout.expires_at`` is typed ``float |
None`` but a provider may return anything, and ``None`` genuinely means "the provider
stated no window". Refusing an unreadable one would orphan a live, revocable discount to
report a bookkeeping problem — the T-345 trade this package already refused once, for the
same reason. Only a window that reads as a number *and* has closed is refused. The
pre-mint call cannot reach that case at all: ``codes.code_expiry`` has already raised
``UnusableOffer`` on anything unparsable, one step earlier, with nothing minted.

Redaction (T-215)
-----------------

Post-mint, a live code exists and every fragment of prose built here is a fragment that
must not spell it. The *other* discount is merchant-authored and goes through
:func:`~.redaction.safe_token`; our own code is named only by
:func:`~.redaction.code_fingerprint`, the one-way join every other post-mint refusal in
this package publishes. The reasons below are formatted into ``denial_reason`` and into a
persisted ``policy_event``, exactly like :mod:`.domain`'s, so they are guarded at the site
that BUILDS them rather than at a boundary that never held the values.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .redaction import code_fingerprint, safe_token

__all__ = [
    "DISCOUNT_QUERY_KEYS",
    "DiscountDoesNotApply",
    "applied_discount_codes",
    "assert_the_cart_applies_this_code",
    "assert_the_window_is_open",
    "cart_conflict_reason",
    "same_code",
    "window_reason",
]

#: Query parameters a cart permalink pre-applies a discount through. D22 pins ``discount``;
#: it is a set rather than a literal so the spelling is named once and can be widened
#: without a second comparison appearing somewhere else.
DISCOUNT_QUERY_KEYS: frozenset[str] = frozenset({"discount"})


class DiscountDoesNotApply(ValueError):
    """The minted discount will not apply at the cart the buyer would be sent to.

    Raised for both halves of R3's validation clause — a window that has closed, and a cart
    that already applies a different discount — because to a buyer they are the same
    outcome: the price they were promised is not the price they would be charged.
    """


def _as_number(value: Any) -> float | None:
    """``float(value)`` for a value a provider supplied, or ``None``. **Cannot raise.**

    ``bool`` is excluded on purpose: ``True`` is ``1.0``, which would read as an epoch in
    1970 and turn every provider that answered ``expires_at=True`` into a closed window.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    # NaN fails every comparison, so it would silently read as "not expired"; it is not a
    # window this module can judge, and it says so by abstaining.
    if number != number:
        return None
    return number


def window_reason(expires_at: Any, now: Any) -> str | None:
    """Why this discount's validity window is shut at ``now``, or ``None`` when it is open.

    ``None`` also means "no window I can read" — see the module docstring on why an
    unreadable expiry abstains instead of refusing.
    """
    deadline = _as_number(expires_at)
    moment = _as_number(now)
    if deadline is None or moment is None:
        return None
    if deadline > moment:
        return None
    return (
        f"the discount's validity window closed at {deadline!r} and the checkout is being "
        f"built at {moment!r}; D22 makes the code expire at min(now + 48h, "
        f"offer.expires_at), so an offer that has already expired can only mint a code that "
        f"is already dead"
    )


def assert_the_window_is_open(expires_at: Any, now: Any, *, what: str = "the discount") -> None:
    """Raise :class:`DiscountDoesNotApply` unless the validity window is still open."""
    reason = window_reason(expires_at, now)
    if reason is not None:
        raise DiscountDoesNotApply(f"{what}: {reason}")


def applied_discount_codes(permalink_url: Any) -> list[str]:
    """Every discount code the permalink pre-applies, in order. **Cannot raise.**

    ``parse_qsl`` does the decoding this comparison would otherwise get wrong: it
    percent-decodes (``%50%53%58-01`` -> ``PSX-01``) and reads ``+`` as a space, which is
    how a merchant round-tripping ``'PSX LIVE 5'`` through a form encoder spells it. Both
    spellings are in ``test_orphaned_code.py``'s matrix as REAL merchant behaviour, so a
    naive string compare would call our own code a conflict.
    """
    try:
        text = "" if permalink_url is None else str(permalink_url)
    except Exception:
        return []
    if not text.strip():
        return []
    try:
        query = urlsplit(text).query
        pairs = parse_qsl(query, keep_blank_values=False)
    except Exception:
        # An unparsable URL is `domain.assert_on_domain`'s refusal, taken one step earlier;
        # there is no cart here to have a discount on.
        return []
    return [value for key, value in pairs if key.strip().lower() in DISCOUNT_QUERY_KEYS]


def same_code(applied: str, code: Any) -> bool:
    """Whether ``applied`` names the same discount as ``code``.

    Case-insensitively, because Shopify redeems case-insensitively and lower-casing a URL is
    ordinary CDN and link-building behaviour — the ``lowercased-permalink`` case in
    ``test_orphaned_code.py``'s spelling matrix is there because a merchant really does it.
    """
    try:
        return applied.strip().casefold() == str(code).strip().casefold()
    except Exception:  # pragma: no cover - a hostile `__str__` is not a match
        return False


def cart_conflict_reason(permalink_url: Any, code: Any) -> str | None:
    """Why the cart this permalink opens will not apply ``code``, or ``None``.

    ``None`` when the permalink applies our code (in any spelling), and when it applies no
    discount at all — absent is not conflicting.
    """
    applied = applied_discount_codes(permalink_url)
    rival = [value for value in applied if value.strip() and not same_code(value, code)]
    if not rival:
        return None
    shown = ", ".join(
        repr(safe_token(value, str(code), label="applied-discount")) for value in rival
    )
    return (
        f"the cart permalink already pre-applies {shown}, which is not the single-use code "
        f"{code_fingerprint(str(code))} this checkout minted; one Shopify cart applies one "
        f"code, so the buyer would redeem that discount and never the one the exchange "
        f"recorded (R3 combinesWith)"
    )


def assert_the_cart_applies_this_code(
    permalink_url: Any, code: Any, *, what: str = "the cart permalink"
) -> None:
    """Raise :class:`DiscountDoesNotApply` when the permalink pre-applies a rival discount."""
    reason = cart_conflict_reason(permalink_url, code)
    if reason is not None:
        raise DiscountDoesNotApply(f"{what}: {reason}")
