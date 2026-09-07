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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol
from urllib.parse import quote

from contracts.boundary import (
    OFFER_UNIT_PRICE_SITE,
    REASON_PRICE_UNRECONCILABLE,
    parse_timestamp,
    price_reasons,
)

from .. import describe

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
    "minting_ledger",
    "offer_quantity",
    "record_minted_code",
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


#: Codes minted inside the innermost :func:`minting_ledger` block, in mint order.
#:
#: A ``ContextVar`` rather than a module global: two checkouts running in the same process
#: must not see each other's codes, and the port opens one ledger per call.
_MINTED_CODES: ContextVar[list[str] | None] = ContextVar("proxyshop_minted_codes", default=None)


@contextmanager
def minting_ledger() -> Iterator[list[str]]:
    """Record every code minted inside this block, so a failure can still name it (T-202).

    **The problem this exists for.** ``CheckoutProvider.checkout`` guards everything that
    happens *after* ``mint`` returns, because that is where a refusal can strand a live
    discount. But a code comes into existence in the MIDDLE of ``mint``, and the port has no
    way to see it: ``mint``'s only channel back is its return value, and an exception raised
    after the code exists carries nothing. ``SimulatedRedirectProvider`` — D45's required
    starting implementation — does exactly that: :func:`mint_code` first,
    ``default_permalink`` second, and the second resolves the registered domain again. A
    lookup that answers once and fails once (a dropped connection, a cache eviction) loses
    the code, and the port raises an ordinary refusal with no orphan attached.

    ``ShopifyCheckoutProvider`` closed the same hole with its own ``except`` block. That is
    a fix one provider remembered, and D45 invites more providers: the guarantee belongs to
    the port, where no implementation can opt out of it, exactly like the domain check.

    The ledger is that channel. It is opened by the port around its call to ``mint``, and
    anything that mints reports into it — :func:`mint_code` does so unconditionally, so
    every provider that uses this package's own minting surface is covered without
    knowing this exists. A provider that mints somewhere else (the merchant's
    ``POST /codes``) calls :func:`record_minted_code` itself.

    Yields the live list, so the caller reads what was minted even when ``mint`` raised.
    """
    recorded: list[str] = []
    token = _MINTED_CODES.set(recorded)
    try:
        yield recorded
    finally:
        _MINTED_CODES.reset(token)


def record_minted_code(code: str) -> None:
    """Report a code into the enclosing :func:`minting_ledger`, if there is one.

    A no-op outside one, so calling it is always safe — and it never raises, because it is
    called from the exact region where a raised exception loses the code it was reporting.
    """
    try:
        ledger = _MINTED_CODES.get()
        if ledger is not None and code:
            ledger.append(str(code))
    except Exception:  # pragma: no cover - reporting must never be the thing that fails
        pass


def mint_code(*, rng: RandomSource | None = None) -> str:
    """Mint one single-use discount code: ``PSX-`` + 8 random Crockford base32 characters.

    ``rng`` exists so a test can pin the output. Left unset — which is every production
    call — the characters come from :func:`secrets.choice`, so the code is not derivable
    from the offer, the auction, or anything else an outsider can see.

    The code is reported to the enclosing :func:`minting_ledger` **before** it is returned:
    from this line on a real discount exists, and everything between here and the caller's
    ``return`` is a region where an exception would otherwise strand it (T-202).
    """
    pick = rng.choice if rng is not None else secrets.choice
    body = "".join(str(pick(CODE_ALPHABET)) for _ in range(CODE_BODY_LENGTH))
    code = f"{CODE_PREFIX}{body}"
    record_minted_code(code)
    return code


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

    **The order below is the whole of that guarantee, and the obvious order is wrong.** Trying
    ``float()`` first and falling back to :func:`parse_timestamp` reads ``"20260903"`` — ISO
    8601 basic format, which ``datetime.fromisoformat`` accepts and the boundary therefore
    admits as 2026-09-03 — as the epoch second 20260903, i.e. **23 August 1970**. The bid
    passes ``validate_bid`` with a future expiry and mints a code that expired fifty-six years
    ago. So for anything that is not already a number, the boundary's parser goes first and
    wins; ``float()`` is only the fallback, which keeps numeric strings (``"1700100000"``,
    which ``parse_timestamp`` refuses) reading exactly as they did before.

    Real numbers skip both and go through ``float()`` unchanged, so no epoch spelling that
    worked before changes value — including the ones ``parse_timestamp`` would refuse
    outright, such as an epoch too large for :meth:`datetime.fromtimestamp`.

    Raises:
        UnusableOffer: the value is neither a number nor an instant anything can read.
    """
    # A real number is already epoch seconds. Exact, and not routed through a datetime,
    # whose microsecond resolution would quietly round a sub-microsecond float.
    if isinstance(expires_at, (int, float)):
        return float(expires_at)

    parsed = parse_timestamp(expires_at)
    if parsed is not None:
        return parsed.timestamp()

    try:
        # Numeric strings: `parse_timestamp` refuses them, and they worked here before.
        return float(expires_at)
    except (TypeError, ValueError) as exc:
        raise UnusableOffer(
            f"offer expires_at {describe(expires_at)} is neither an epoch number nor an "
            f"RFC-3339 "
            f"instant, so the code's D22 expiry cannot be computed"
        ) from exc
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
        raise UnusableOffer(f"offer quantity {describe(quantity)} is not a whole number") from exc
    if value < 1:
        raise UnusableOffer(f"offer quantity {describe(quantity)} is not a positive quantity")
    return value


#: The two ``offer.unit_price`` verdicts :func:`_unreadable_unit_price_reasons` acts on.
#:
#: :func:`contracts.boundary.price_reasons` answers a whole price walk, and almost everything it
#: can say is about a ROSTER this module does not hold and is not entitled to ask for. With no
#: catalog passed, ``price_unreconcilable:offer.unit_price:list_price_unavailable`` comes back for
#: *every* offer (T-306), and ``price_under_declared_depth:offer.unit_price`` is the auction door's
#: adjudication of a declared discount (``auction/collect.py``), not the mint's. Acting on either
#: here would refuse every honest checkout, which is closed rather than fail-closed.
#:
#: So the mint reads only the two verdicts that need no catalog at all and are about the VALUE the
#: offer states: it is not a number (``object()``, ``"cheap"``, ``True``, ``NaN``, ``inf``, absent),
#: or it is a number no price can be. Both are the boundary's own spelling rather than a second
#: vocabulary invented here — and because they are leaf strings rather than published constants,
#: ``test_the_mint_reads_the_boundarys_own_unreadable_price_verdict`` drives the boundary and goes
#: red if either name moves, so a rename surfaces as a failing test rather than as a gate that
#: quietly stops refusing anything.
_UNREADABLE_UNIT_PRICE_WHYS = frozenset({"not_a_number", "negative"})

#: ``price_unreconcilable:offer.unit_price:`` — the site half of the reasons above, composed from
#: the boundary's published constants so only the leaf ``why`` is spelled here.
_UNIT_PRICE_REFUSAL_PREFIX = f"{REASON_PRICE_UNRECONCILABLE}:{OFFER_UNIT_PRICE_SITE}:"


def _unreadable_unit_price_reasons(offer: Mapping[str, Any] | None) -> list[str]:
    """The boundary's refusals of this offer's ``unit_price``, as a value. Empty when it is one.

    **Absent is not this gate's subject, and the distinction is T-345's own.** The ticket separates
    itself from T-306/T-307 in as many words: those were an *absent* argument abstaining, this is a
    *present* malformed one being accepted. An offer that states no ``unit_price`` is refused by
    nothing here — the R10 list-price fallback and every direct :func:`accept` caller are entitled
    to hand the port an offer with no price on it, and ``price_reasons`` says ``not_a_number``
    about an absent field exactly as it does about ``object()``. Judging the absent case would
    refuse the required starting slice; judging only the stated one closes the hole the ticket
    measured.

    ``price_reasons`` never raises, so this never does either.
    """
    stated = (offer or {}).get("unit_price")
    if stated is None:
        return []
    return [
        reason
        for reason in price_reasons({"offer": offer})
        if reason.startswith(_UNIT_PRICE_REFUSAL_PREFIX)
        and reason[len(_UNIT_PRICE_REFUSAL_PREFIX) :] in _UNREADABLE_UNIT_PRICE_WHYS
    ]


def assert_offer_is_mintable(offer: Mapping[str, Any] | None, *, now: float = 0.0) -> float:
    """Refuse an offer whose code-shaping fields — or whose stated price — will not parse.

    Returns the D22 deadline this offer would mint into — ``min(now + 48h,
    offer.expires_at)`` — so the caller can judge R3's validity window **without reading
    ``expires_at`` a second time.** That is not a micro-optimisation: an ``offer`` is a
    merchant's own JSON-shaped object, "the value I validated is the value I will read next"
    is an assumption rather than a guarantee, and ``test_orphaned_code.py``'s
    ``ShiftingOffer`` is that assumption failing on purpose. A gate that read the field twice
    would judge a different value from the one the code is minted out of, which is the same
    defect ``providers._code_refusals`` documents about a reply read two ways.

    ``now`` defaults to ``0.0`` — the value this function has always used internally — so
    every existing caller gets exactly the answer it got before. The port passes
    ``request.now``, which is the only ``now`` a real window can be judged against.

    The order this runs in is the whole point. ``code_expiry`` and the permalink builder are
    both reached *after* the provider has minted — after the merchant's ``POST /codes`` has
    already issued a real single-use discount, in the Shopify adapter's case. A malformed
    ``expires_at`` therefore used to raise with a live code loose in the merchant's account
    and no ``code_created`` event recorded for it: the exchange had handed out a discount it
    had no record of and no way to expire. Validating here makes that unreachable — the
    request is refused with nothing minted anywhere.

    **The price is checked here too, and that is T-345's second half.** This gate is the port's
    documented pre-mint door and it used to look at ``expires_at`` and ``quantity`` only — the two
    fields the *code* is built out of — so nothing on the published four-positional
    :func:`~apps.exchange.src.accept.offer.accept` surface ever asked whether the offer's price was
    a price. Measured at HEAD, an offer whose ``unit_price`` was a bare ``object()`` came back
    ``accepted=True, denial_reason=None`` with a real single-use discount minted for it, and the
    address of that object was written verbatim into the persisted ``accepted`` event::

        payload={'offer': {'unit_price': <object object at 0x100c31350>, ...}}

    The SERVED path already refuses this — ``auction/collect.py``'s ``_price_is_unreadable`` asks
    the same question of every bid it collects, deliberately with no roster term in it — but the
    direct accept surface skips collection entirely, so the wall existed on one route to the mint
    and not on the other. Both now consult the same boundary, which is the point of having one:
    two doors that disagree about which values are prices is one door with a hole.
    """
    deadline = code_expiry(now, offer)
    offer_quantity(offer)
    refused = _unreadable_unit_price_reasons(offer)
    if refused:
        raise UnusableOffer(
            f"offer unit_price {describe((offer or {}).get('unit_price'))} is not a price the "
            f"exchange can read ({', '.join(refused)}), so this offer cannot be minted"
        )
    return deadline


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
