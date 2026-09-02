"""D22's code shape: the alphabet, the length, the expiry rule, and non-derivability.

These are pure-function tests with no server. They exist because D22 is the one part of this
ticket that three other tickets (T-033, T-052, T-036) have to agree with byte-for-byte, and
because the most damaging failure here — a code that can be computed from ``offer_id`` — is
invisible in any end-to-end test: a derived code redeems perfectly.
"""

from __future__ import annotations

import inspect
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from shopify_stub import codes
from shopify_stub.codes import (
    CODE_BODY_LENGTH,
    CODE_PREFIX,
    CROCKFORD_ALPHABET,
    MAX_CODE_LIFETIME,
    CombinesWith,
    DiscountCode,
    RejectionReason,
    code_expiry,
    is_well_formed,
    mint_code,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_crockford_alphabet_excludes_the_ambiguous_letters() -> None:
    """Crockford base32 drops I, L, O and U so a code cannot be mis-transcribed.

    Asserted as an exact string rather than "len == 32" because a 32-symbol alphabet that
    happened to include ``O`` would pass a length check and still let ``PSX-0000000O`` and
    ``PSX-00000000`` be confused for each other.
    """
    assert CROCKFORD_ALPHABET == "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    assert len(CROCKFORD_ALPHABET) == 32
    assert len(set(CROCKFORD_ALPHABET)) == 32
    for excluded in "ILOU":
        assert excluded not in CROCKFORD_ALPHABET


def test_minted_codes_match_the_pinned_shape() -> None:
    """``PSX-`` plus exactly 8 Crockford symbols, uppercase."""
    for _ in range(200):
        code = mint_code()
        assert code.startswith(CODE_PREFIX)
        assert len(code) == len(CODE_PREFIX) + CODE_BODY_LENGTH
        assert code == code.upper()
        assert is_well_formed(code)


def test_mint_code_cannot_be_derived_from_an_offer_id() -> None:
    """D22's "never derived from offer_id", enforced on the signature itself.

    A behavioural test cannot distinguish a random code from a well-hashed one, so this
    asserts the property structurally: :func:`mint_code` accepts no offer identifier, so no
    implementation of it can depend on one. If someone later adds such a parameter, this
    fails and the review conversation happens before the guessable-redeemable ships.
    """
    parameters = inspect.signature(mint_code).parameters
    assert set(parameters) == {"rng"}, (
        "mint_code must take no offer identifier: a code derived from offer_id is a "
        f"guessable single-use redeemable (D22). Signature is now {parameters}"
    )
    # Belt and braces: nothing the compiled function touches — no global it reads, no local
    # it binds, no constant it carries — may mention an offer. This survives a rename that
    # a signature check alone would miss (e.g. taking the id through a module-level global).
    docstrings = {mint_code.__doc__}
    referenced: set[str] = set()
    pending = [mint_code.__code__]
    while pending:
        code_object = pending.pop()
        referenced.update(code_object.co_names)
        referenced.update(code_object.co_varnames)
        referenced.update(
            c for c in code_object.co_consts if isinstance(c, str) and c not in docstrings
        )
        pending.extend(c for c in code_object.co_consts if inspect.iscode(c))
    offending = sorted(name for name in referenced if "offer" in name.lower())
    assert not offending, f"mint_code references {offending}; D22 forbids it"


def test_minted_codes_do_not_repeat() -> None:
    """1000 draws from 32**8 symbols: a collision means the source is not random.

    The birthday probability of any collision here is about 4e-13, so a failure is a real
    defect (a seeded RNG, a truncated hash) rather than bad luck.

    Uniqueness alone is NOT enough — see :func:`test_minted_codes_are_unpredictable`. A
    counter passes this test perfectly.
    """
    minted = {mint_code() for _ in range(1000)}
    assert len(minted) == 1000


def test_minted_codes_are_unpredictable() -> None:
    """D22's real requirement: *randomly generated*, not merely distinct.

    This test exists because uniqueness is the wrong property to check, and checking only
    uniqueness is a trap I walked into: a plain counter (``PSX-00000001``, ``PSX-00000002``,
    …) produces a thousand distinct, well-formed, Crockford-legal codes and satisfies both
    ``test_minted_codes_match_the_pinned_shape`` and ``test_minted_codes_do_not_repeat``. It
    is also perfectly guessable, which is the precise thing D22 forbids: "a derivable
    single-use redeemable is a guessable one."

    Two independent checks, either of which a counter, a timestamp, or a truncated hash of a
    monotone input fails:

    1. **Full alphabet coverage.** Over 500 draws (4000 symbols) every one of the 32
       Crockford symbols must appear somewhere. A decimal counter emits only ``0``-``9`` and
       a hex one only ``0``-``9A``-``F``, so both fail here. The chance a genuinely random
       source misses a given symbol is ``(31/32)**4000`` ≈ 1e-55, so this cannot flake.
    2. **No monotone ordering.** A counter emits an ascending sequence. The chance 200
       random draws arrive already sorted is ``1/200!``.
    """
    draws = [mint_code() for _ in range(500)]
    bodies = [code[len(CODE_PREFIX) :] for code in draws]

    seen_symbols = set("".join(bodies))
    missing = sorted(set(CROCKFORD_ALPHABET) - seen_symbols)
    assert not missing, (
        f"symbols {missing} never appeared in 4000 draws — the source is not uniform over "
        f"the alphabet (a decimal or hex counter looks exactly like this)"
    )

    sample = draws[:200]
    assert sample != sorted(sample), "a monotonically increasing code is a guessable one"


def test_is_well_formed_rejects_near_misses() -> None:
    """Negative control for the format check."""
    assert not is_well_formed("PSX-ABCDEFG")  # 7 symbols
    assert not is_well_formed("PSX-ABCDEFGHI")  # 9 symbols
    assert not is_well_formed("PS-ABCDEFGH")  # wrong prefix
    assert not is_well_formed("psx-abcdefgh")  # lower case
    assert not is_well_formed("PSX-ABCDEFGI")  # 'I' is not in the alphabet
    assert not is_well_formed("PSX-ABCDEFGU")  # nor is 'U'
    assert not is_well_formed("PSXABCDEFGH")  # missing separator
    assert is_well_formed("PSX-ABCDEFGH")


@pytest.mark.parametrize(
    ("offer_expiry", "expected"),
    [
        (None, NOW + MAX_CODE_LIFETIME),
        (NOW + timedelta(hours=1), NOW + timedelta(hours=1)),
        (NOW + timedelta(hours=47, minutes=59), NOW + timedelta(hours=47, minutes=59)),
        (NOW + timedelta(hours=48), NOW + timedelta(hours=48)),
        (NOW + timedelta(hours=49), NOW + MAX_CODE_LIFETIME),
        (NOW + timedelta(days=30), NOW + MAX_CODE_LIFETIME),
    ],
)
def test_expiry_is_the_minimum_of_48h_and_the_offer(
    offer_expiry: datetime | None, expected: datetime
) -> None:
    """``min(48h, offer.expires_at)`` — including both sides of the 48-hour boundary."""
    assert code_expiry(now=NOW, offer_expires_at=offer_expiry) == expected


def test_expiry_refuses_naive_datetimes() -> None:
    """A naive datetime shifts expiry by the host's UTC offset — a bug only seen off-UTC."""
    with pytest.raises(ValueError, match="timezone-aware"):
        code_expiry(now=datetime(2026, 1, 1), offer_expires_at=None)  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        code_expiry(now=NOW, offer_expires_at=datetime(2026, 1, 2))  # noqa: DTZ001


def _code(**overrides: object) -> DiscountCode:
    base: dict[str, object] = {
        "code": "PSX-ABCDEFGH",
        "starts_at": NOW,
        "ends_at": NOW + timedelta(hours=48),
        "usage_limit": 1,
        "percentage": 0.1,
    }
    base.update(overrides)
    return DiscountCode(**base)  # type: ignore[arg-type]


def test_rejection_reasons_are_distinguishable() -> None:
    """Each invalid state reports its own reason, not a shared "invalid"."""
    assert _code().rejection(now=NOW) is None
    assert _code().rejection(now=NOW - timedelta(seconds=1)) is RejectionReason.NOT_YET_ACTIVE
    assert _code().rejection(now=NOW + timedelta(hours=48)) is RejectionReason.EXPIRED, (
        "expiry is exclusive: the code is dead at ends_at, not one second later"
    )
    assert _code(usage_count=1).rejection(now=NOW) is RejectionReason.USAGE_LIMIT_REACHED
    assert (
        _code().rejection(now=NOW, cart_has_order_discount=True)
        is RejectionReason.CONFLICTS_WITH_EXISTING_DISCOUNT
    )
    combinable = _code(combines_with=CombinesWith(order_discounts=True))
    assert combinable.rejection(now=NOW, cart_has_order_discount=True) is None


def test_unlimited_usage_limit_never_exhausts() -> None:
    """``usageLimit: null`` is unlimited in Shopify; the stub must not treat it as zero."""
    unlimited = _code(usage_limit=None, usage_count=9999)
    assert unlimited.rejection(now=NOW) is None


def test_code_matching_is_case_insensitive() -> None:
    """Shopify looks discount codes up case-insensitively at redemption."""
    code = _code()
    assert code.matches("psx-abcdefgh")
    assert code.matches("  PSX-ABCDEFGH  ")
    assert not code.matches("PSX-ABCDEFGX")


def test_combines_with_round_trips_through_the_wire_shape() -> None:
    """The three booleans keep their camelCase names in both directions."""
    original = CombinesWith(order_discounts=True, product_discounts=False, shipping_discounts=True)
    wire = original.to_wire()
    assert wire == {
        "orderDiscounts": True,
        "productDiscounts": False,
        "shippingDiscounts": True,
    }
    assert CombinesWith.from_wire(wire) == original
    assert CombinesWith.from_wire(None) == CombinesWith(False, False, False)
    assert CombinesWith.from_wire({}) == CombinesWith(False, False, False)


def test_mint_code_draws_from_the_cryptographic_source_not_a_seeded_prng() -> None:
    """D22's "randomly generated" clause, given teeth it did not have.

    Every other test in this file is behavioural, and behaviour cannot tell a
    cryptographically random stream from a *predictable* one. Replacing
    ``secrets.SystemRandom()`` with a module-level ``random.Random(20260901)`` — a fully
    seeded, fully reproducible stream an attacker can run themselves — left the suite at 164
    passed with the attacker's predicted codes identical to the server's, because a seeded
    Mersenne Twister is uniform over the alphabet and non-monotone, which is everything
    ``test_minted_codes_are_unpredictable`` checks. ``grep -rn "secrets\\|SystemRandom"`` over
    this directory returned nothing at all.

    So the property is asserted the same way non-derivability already is: structurally, on
    the compiled function, where uniformity cannot stand in for unpredictability.
    """
    referenced: set[str] = set()
    pending = [mint_code.__code__]
    while pending:
        code_object = pending.pop()
        referenced.update(code_object.co_names)
        pending.extend(c for c in code_object.co_consts if inspect.iscode(c))
    assert "secrets" in referenced, (
        "mint_code must draw from `secrets`; a seeded PRNG produces codes that are uniform, "
        "distinct and non-monotone -- and perfectly predictable, which is the one property "
        f"D22 actually requires. It references {sorted(referenced)}"
    )
    assert not {"random", "seed"} & referenced, (
        "the `random` module is not a code source: a discount code is a single-use redeemable"
    )


def test_the_default_random_source_is_actually_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: the import is not enough, the call has to happen.

    ``import secrets`` satisfies a name check while the body quietly uses something else, so
    the module's own ``secrets`` is replaced and the draw is observed. This also pins the
    default: :func:`mint_code` called with no ``rng`` must not fall back to a shared,
    seedable generator.
    """
    calls: list[str] = []

    class _Watched(secrets.SystemRandom):
        def choice(self, seq):  # type: ignore[no-untyped-def]
            calls.append("choice")
            return super().choice(seq)

    class _Module:
        SystemRandom = _Watched

    monkeypatch.setattr(codes, "secrets", _Module)
    minted = mint_code()
    assert is_well_formed(minted)
    assert len(calls) == CODE_BODY_LENGTH, (
        "every symbol must come from the injected source; a body built any other way is a "
        "body the seeded-PRNG sabotage would have produced unnoticed"
    )
