"""The four verification statuses, and the one that is allowed to satisfy a fact.

R18/R19. A claim check has four honest outcomes, and collapsing them to a boolean is how a
verification system quietly starts lying:

``verified``
    The catalog says the same thing. Positive evidence.
``contradicted``
    The catalog says otherwise. Negative evidence, and the heaviest published weight.
``unsupported``
    The catalog says nothing about it. **Not** false — absence of evidence.
``ambiguous``
    The catalog says something, but not something that decides this claim (the attribute
    admits several values and the claim names none of them; the claim itself is unparseable).

:func:`satisfies_hard_constraint` is the whole point of keeping four. A hard constraint is a
promise the buyer said they will not go without — "must be dairy free", "must ship this
week". Only ``verified`` may satisfy one. ``unsupported`` and ``ambiguous`` reading as true is
the exact failure R18/R19 name: it turns "we could not check" into "we checked and it is
fine", and every unfalsifiable pitch becomes a compliant one.

Note the asymmetry, which is deliberate: ``satisfies_hard_constraint`` is not the negation of
``contradicted``. Three of the four statuses fail a hard constraint, and only one of those
three is evidence that the seller was wrong.
"""

from __future__ import annotations

__all__ = [
    "DECIDED_STATUSES",
    "UNDECIDED_STATUSES",
    "VERIFICATION_STATUSES",
    "InvalidVerificationStatus",
    "is_decided",
    "require_status",
    "satisfies_hard_constraint",
]

#: The four, and exactly four. The order is severity-neutral; nothing depends on it.
VERIFICATION_STATUSES: tuple[str, ...] = ("verified", "contradicted", "unsupported", "ambiguous")

#: The two that actually decided something, and therefore count as evidence about the seller.
DECIDED_STATUSES: frozenset[str] = frozenset({"verified", "contradicted"})

#: The two that decided nothing. They move coverage and confidence in the trust engine, never
#: the dimension mean (D53/R19) — see ``trust.scoring``.
UNDECIDED_STATUSES: frozenset[str] = frozenset({"unsupported", "ambiguous"})


class InvalidVerificationStatus(ValueError):
    """A status outside :data:`VERIFICATION_STATUSES`."""


def satisfies_hard_constraint(status: object) -> bool:
    """Whether ``status`` may stand in for a fact the buyer refused to go without.

    ``True`` for ``verified`` and for nothing else — including for a status this module does
    not recognise. An unknown status is not a reason to admit a claim; if anything it is a
    stronger reason to refuse it.
    """
    return str(status) == "verified"


def is_decided(status: object) -> bool:
    """Whether ``status`` is evidence about the seller (``verified``/``contradicted``)."""
    return str(status) in DECIDED_STATUSES


def require_status(status: object) -> str:
    """``status`` as one of the four, or raise :class:`InvalidVerificationStatus`."""
    text = str(status)
    if text not in VERIFICATION_STATUSES:
        raise InvalidVerificationStatus(
            f"{status!r} is not one of the four verification statuses "
            f"{list(VERIFICATION_STATUSES)}. The vocabulary is closed (R18): a fifth status "
            "would reach the trust engine as an observation type nobody weighted."
        )
    return text
