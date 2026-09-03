"""Eligibility filters — everything that decides whether a candidate is scored at all.

R19 is explicit that a hard constraint is a **filter**, never a weighted term, and R12 is
explicit that a blacklist read **fails closed**. Both rules say the same thing about order:
a candidate that the filters cannot positively clear is excluded *before* the published
formula runs, so no `rank_score` exists for it at all. A number that existed but was ignored
would be a number some later reader could sort by; there is no such number here.

The four filters, and why each one denies rather than discounts:

* **Blacklist (R12).** A blacklisted store may not participate at any price. A store whose
  status cannot be read — no row in the snapshot, or a flag that is not a boolean — is
  denied on the same footing: an unreadable answer is not a permissive answer.
* **Expiry.** An offer whose `expires_at` has passed against the caller-supplied `now` is
  not an offer. `now` is passed in rather than read from the clock so a shortlist is
  reproducible from its inputs alone.
* **Checkout domain (C10/D22).** The offer's checkout URL is compared host-for-host against
  the seller's registered `store_domain`. Exact equality, never a prefix or a suffix test:
  a subdomain defeats `endswith`, a look-alike host defeats a substring test, and userinfo
  before an `@` defeats `startswith` on the raw URL.
* **Hard constraints (R19).** A constraint is satisfied by a `verified` supporting claim and
  by nothing else. Ambiguous, unsupported and contradicted evidence are all *absent*
  evidence as far as this filter is concerned — they are dropped before the constraint is
  decided, so an unproven claim can never carry a candidate through.

Deciding the constraint itself is delegated to
:class:`~exchange.retrieval.criteria.HardCriterion`, which is where the op vocabulary and
the undecidable-is-never-satisfied rule already live. Restating either here would be a
second answer to a question that has one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from ..retrieval.criteria import HardCriterion, MalformedIntent
from .reasons import (
    REASON_BLACKLIST_UNREADABLE,
    REASON_BLACKLISTED,
    REASON_ELIGIBILITY_DENIED,
    REASON_EXPIRED,
    REASON_HARD_CONSTRAINT,
    REASON_MALFORMED,
    REASON_OFF_DOMAIN,
    REASON_UNDECIDABLE_INTENT,
)

#: The one claim status that counts as supporting evidence (R19).
VERIFIED = "verified"

_MISSING = object()


def read(obj: Any, name: str, default: Any = None) -> Any:
    """Read `name` off a mapping or an object. Shape-agnostic: the callers of this package
    hand it plain dicts today and pydantic records tomorrow, and neither spelling changes
    what the rules mean."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    value = getattr(obj, name, _MISSING)
    return default if value is _MISSING else value


def _enum_value(raw: Any) -> Any:
    """Unwrap an enum member to its value, leaving everything else alone."""
    return getattr(raw, "value", raw)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------------
# R12 — the blacklist, and the unreadable blacklist
# ---------------------------------------------------------------------------------
def blacklist_reason(store_id: str, trust_snapshot: Any) -> str | None:
    """The reason this store may not participate, or `None` when it may.

    Three denials, deliberately collapsed into one function so no caller can disagree with
    another about which of them is fatal:

    * the snapshot has no row for the store   -> unreadable, denies;
    * the row's `blacklisted` is not a bool   -> unreadable, denies (this is where `None`
      lands, which is the shape a failed upstream read actually arrives in);
    * the row's `blacklisted` is `True`       -> blacklisted, denies.
    """
    row = trust_row(store_id, trust_snapshot)
    if row is None:
        return (
            f"{REASON_BLACKLIST_UNREADABLE}: no trust snapshot row for {store_id!r}, so its "
            f"blacklist status could not be established; failing closed (R12)"
        )
    flag = read(row, "blacklisted", _MISSING)
    if not isinstance(flag, bool):
        return (
            f"{REASON_BLACKLIST_UNREADABLE}: {store_id!r} has a blacklist flag of "
            f"{'<absent>' if flag is _MISSING else repr(flag)}, which is not a readable "
            f"yes/no; failing closed (R12)"
        )
    if flag:
        return f"{REASON_BLACKLISTED}: {store_id!r} is blacklisted and may not participate (R12)"
    return None


def trust_row(store_id: str, trust_snapshot: Any) -> Any | None:
    """One store's row out of the snapshot, or `None` when there is not one."""
    if trust_snapshot is None:
        return None
    if isinstance(trust_snapshot, Mapping):
        return trust_snapshot.get(store_id)
    if isinstance(trust_snapshot, Sequence) and not isinstance(trust_snapshot, (str, bytes)):
        for row in trust_snapshot:
            if str(read(row, "store_id", "")) == store_id:
                return row
        return None
    return getattr(trust_snapshot, store_id, None)


def eligibility_source_reason(source: Any, store_id: str) -> str | None:
    """The reason an injected `SellerEligibility` source denies this store, or `None`.

    The source is OPTIONAL. `rank()` derives the blacklist from the trust snapshot it is
    handed — that is the published four-argument surface every caller uses — and this hook
    exists so a deployment that also has a live eligibility service can hand it in without
    changing that surface. When it is handed in, every unhappy path denies: an unsupported
    interface version, a raised read, an empty answer and an unrecognised status are all
    `UNAVAILABLE`, and `UNAVAILABLE` is a denial (R12).
    """
    if source is None:
        return None
    # Imported here rather than at module scope: the eligibility port is only consulted when
    # a caller opts in, and an optional collaborator should not be an unconditional import.
    from ..eligibility import read_eligibility, speaks_supported_interface

    if not speaks_supported_interface(source):
        declared = getattr(source, "interface_version", None)
        return (
            f"{REASON_ELIGIBILITY_DENIED}: the eligibility source declares interface version "
            f"{declared!r}, which this exchange does not speak; failing closed (R12)"
        )
    decision = read_eligibility(source, store_id)
    if decision.eligible:
        return None
    return f"{REASON_ELIGIBILITY_DENIED}: {decision.reason or decision.status}"


# ---------------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------------
def expiry_reason(offer: Any, now: float) -> str | None:
    """The reason this offer is too old to rank, or `None` when it is live."""
    raw = read(offer, "expires_at", _MISSING)
    if raw is _MISSING or raw is None:
        return (
            f"{REASON_EXPIRED}: the offer carries no expires_at, so it cannot be shown to be "
            f"live; failing closed"
        )
    try:
        expires_at = float(raw)
    except (TypeError, ValueError):
        return f"{REASON_EXPIRED}: expires_at {raw!r} is not a readable instant; failing closed"
    if expires_at <= now:
        return (
            f"{REASON_EXPIRED}: the offer expired at {expires_at!r}, which is not after the "
            f"caller-supplied now={now!r}"
        )
    return None


# ---------------------------------------------------------------------------------
# C10 / D22 — the checkout URL never leaves the registered seller domain
# ---------------------------------------------------------------------------------
def domain_reason(candidate: Any, offer: Any) -> str | None:
    """The reason this offer's checkout URL is refused, or `None` when it is on-domain.

    The comparison is `urlsplit(...).hostname` against the registered `store_domain`, lower
    cased on both sides. `hostname` is what strips a port and any `user:pass@` prefix, so
    the exact comparison below is genuinely exact rather than exact-looking.
    """
    registered = read(candidate, "store_domain", None) or read(candidate, "domain", None)
    url = read(offer, "checkout_url", None) or read(candidate, "checkout_url", None)
    if not registered:
        return (
            f"{REASON_OFF_DOMAIN}: the candidate names no registered seller domain, so its "
            f"checkout URL cannot be shown to be on-domain; failing closed (C10)"
        )
    if not url:
        return (
            f"{REASON_OFF_DOMAIN}: the offer carries no checkout URL to compare against the "
            f"registered domain {registered!r}; failing closed (C10)"
        )
    try:
        host = urlsplit(str(url)).hostname
    except ValueError:
        host = None
    if host is None or host.lower() != str(registered).strip().lower():
        return (
            f"{REASON_OFF_DOMAIN}: the checkout URL host {host!r} is not the registered "
            f"seller domain {registered!r} (C10/D22)"
        )
    return None


# ---------------------------------------------------------------------------------
# R19 — hard constraints, decided against verified evidence only
# ---------------------------------------------------------------------------------
def verified_attributes(claims: Any) -> list[dict[str, Any]]:
    """The candidate's `verified` claims, projected into the attribute shape
    :meth:`HardCriterion.decide` reads.

    Anything that is not `verified` is simply not here. That is the whole of R19's evidence
    rule: an ambiguous, unsupported or contradicted claim is not weaker support for a hard
    constraint, it is no support at all, and a constraint with no support is undecidable —
    which :class:`HardCriterion` already refuses to count as satisfied.
    """
    attributes: list[dict[str, Any]] = []
    for claim in claims or ():
        if str(_enum_value(read(claim, "status", ""))) != VERIFIED:
            continue
        key = read(claim, "key", None)
        if key is None:
            continue
        value = read(claim, "value", None)
        attribute: dict[str, Any] = {
            "key": str(key),
            "value_string": None,
            "value_number": None,
            "value_bool": None,
            "unit": read(claim, "unit", None),
        }
        # bool before number: `isinstance(True, int)` is True, so a bool tested as a number
        # would be filed under value_number and never match an `eq` on a bool.
        if isinstance(value, bool):
            attribute["value_bool"] = value
        elif _is_number(value):
            attribute["value_number"] = float(value)
        elif value is not None:
            attribute["value_string"] = str(value)
        attributes.append(attribute)
    return attributes


def read_criteria(intent: Any) -> tuple[list[HardCriterion], str | None]:
    """The intent's hard constraints as decidable criteria, plus the reason they could not
    be read.

    An intent this module cannot parse denies every candidate rather than admitting them
    all: "I could not evaluate the constraint" and "the constraint is satisfied" must never
    be the same outcome (R19).
    """
    raw = read(intent, "hard_constraints", None) or ()
    criteria: list[HardCriterion] = []
    for entry in raw:
        try:
            criteria.append(HardCriterion.from_mapping(entry))
        except MalformedIntent as exc:
            return [], (
                f"{REASON_UNDECIDABLE_INTENT}: {exc}; a constraint this exchange cannot decide "
                f"excludes rather than counting as satisfied (R19)"
            )
    return criteria, None


def hard_constraint_reasons(
    claims: Any, criteria: Sequence[HardCriterion]
) -> tuple[list[str], int]:
    """`(reasons, verified_hard_fit_count)` for one candidate.

    `verified_hard_fit_count` is the number of hard constraints this candidate meets on
    verified evidence. It is the first published tie-breaker (D13), which is why it is
    counted here rather than recomputed by the sorter.
    """
    attributes = verified_attributes(claims)
    reasons: list[str] = []
    satisfied = 0
    for criterion in criteria:
        verdict = criterion.decide(attributes)
        if verdict.satisfied:
            satisfied += 1
            continue
        reasons.append(
            f"{REASON_HARD_CONSTRAINT}: {verdict.reason} — only a verified supporting claim "
            f"satisfies a hard constraint (R19)"
        )
    return reasons, satisfied


# ---------------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------------
def exclusion_reasons(
    candidate: Any,
    *,
    criteria: Sequence[HardCriterion],
    intent_reason: str | None,
    trust_snapshot: Any,
    now: float,
    eligibility: Any = None,
) -> tuple[list[str], int]:
    """Every reason this candidate may not be scored, and its verified hard-fit count.

    Every filter runs, rather than the first denial short-circuiting the rest. A candidate
    that is both blacklisted and off-domain has two things wrong with it, and recording one
    of them would make the second invisible to whoever fixes the first.
    """
    reasons: list[str] = []

    bid_id = read(candidate, "bid_id", None)
    store_id = read(candidate, "store_id", None)
    offer = read(candidate, "offer", None)
    if not bid_id:
        reasons.append(f"{REASON_MALFORMED}: the candidate carries no bid_id")
    if not store_id:
        reasons.append(f"{REASON_MALFORMED}: the candidate carries no store_id")
    if offer is None:
        reasons.append(f"{REASON_MALFORMED}: the candidate carries no offer to rank")

    if store_id:
        blacklist = blacklist_reason(str(store_id), trust_snapshot)
        if blacklist is not None:
            reasons.append(blacklist)
        source = eligibility_source_reason(eligibility, str(store_id))
        if source is not None:
            reasons.append(source)

    if offer is not None:
        expiry = expiry_reason(offer, now)
        if expiry is not None:
            reasons.append(expiry)
        domain = domain_reason(candidate, offer)
        if domain is not None:
            reasons.append(domain)

    if intent_reason is not None:
        reasons.append(intent_reason)
        return reasons, 0

    hard, satisfied = hard_constraint_reasons(read(candidate, "claims", None), criteria)
    reasons.extend(hard)
    return reasons, satisfied


__all__ = [
    "VERIFIED",
    "blacklist_reason",
    "domain_reason",
    "eligibility_source_reason",
    "exclusion_reasons",
    "expiry_reason",
    "hard_constraint_reasons",
    "read",
    "read_criteria",
    "trust_row",
    "verified_attributes",
]
