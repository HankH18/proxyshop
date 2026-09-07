"""The exclusion-reason vocabulary, published once so two filters cannot phrase the same
denial two ways.

An exclusion reason is not a log line. It is the only record of *why* a candidate never
reached the formula, and a buyer-facing exclusion label is built from it, so the token that
names the filter is part of the contract rather than incidental prose. Each constant below
carries the filter's name as its prefix; the free text after the colon is the detail, and
readers match on the prefix.
"""

from __future__ import annotations

#: R12 — the trust snapshot says this store is blacklisted.
REASON_BLACKLISTED = "blacklisted_store"

#: R12 — the blacklist flag could not be read (no row, or a non-boolean value). Failing
#: closed means an unreadable read denies, exactly as a positive blacklist would.
REASON_BLACKLIST_UNREADABLE = "blacklist_unreadable"

#: R12 — an injected `SellerEligibility` source denied, or could not answer.
REASON_ELIGIBILITY_DENIED = "blacklist_source_denied"

#: The offer's own expiry has passed against the caller-supplied `now`.
REASON_EXPIRED = "expired_offer"

#: C10/D22 — the offer's checkout URL points somewhere other than the seller's registered
#: domain, compared host-for-host.
REASON_OFF_DOMAIN = "off_domain_checkout"

#: R19 — a hard constraint is not satisfied by a `verified` supporting claim.
REASON_HARD_CONSTRAINT = "hard_constraint_unsatisfied"

#: The intent's own constraint list could not be read. An intent this module cannot decide
#: excludes every candidate rather than admitting them all.
REASON_UNDECIDABLE_INTENT = "undecidable_hard_constraint"

#: The candidate record itself is malformed (no bid id, no offer, no store).
REASON_MALFORMED = "malformed_candidate"

#: NOT an exclusion reason, and deliberately outside :data:`EXCLUSION_REASON_PREFIXES`: it
#: names a constraint this auction SET ASIDE rather than a candidate it refused. It is
#: published on the ranking result (``relaxed_constraints``) and never on a row's
#: ``exclusion_reasons``, because the two say opposite things about the same candidate.
REASON_UNEVIDENCED_CONSTRAINT = "hard_constraint_unevidenced_by_every_candidate"

#: Every reason prefix this package can emit.
EXCLUSION_REASON_PREFIXES: tuple[str, ...] = (
    REASON_BLACKLISTED,
    REASON_BLACKLIST_UNREADABLE,
    REASON_ELIGIBILITY_DENIED,
    REASON_EXPIRED,
    REASON_OFF_DOMAIN,
    REASON_HARD_CONSTRAINT,
    REASON_UNDECIDABLE_INTENT,
    REASON_MALFORMED,
)

__all__ = [
    "EXCLUSION_REASON_PREFIXES",
    "REASON_BLACKLISTED",
    "REASON_BLACKLIST_UNREADABLE",
    "REASON_ELIGIBILITY_DENIED",
    "REASON_EXPIRED",
    "REASON_HARD_CONSTRAINT",
    "REASON_MALFORMED",
    "REASON_OFF_DOMAIN",
    "REASON_UNDECIDABLE_INTENT",
    "REASON_UNEVIDENCED_CONSTRAINT",
]
