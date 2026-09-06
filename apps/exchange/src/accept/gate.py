"""The R12 accept-time eligibility gate (D54, T-033).

``solicit_bids`` asks "may this store participate?" before anyone is solicited. That answer
has a shelf life. Between a bid arriving and a buyer choosing it, a store can be blacklisted —
by T-062's live lookup, by an operator, by a policy event — and an accept that trusts the
answer it got at solicitation time hands that store a real discount code and a buyer.

So eligibility is **re-read here**, per store, at the moment of checkout. Three properties,
all of them the same fail-closed rule the solicitation gate applies, read through the same
:func:`~apps.exchange.src.eligibility.read_eligibility` so the two gates cannot drift apart:

1. the interface version is checked **first** — a source whose status vocabulary this exchange
   does not speak is indistinguishable from no source at all, so it refuses;
2. ``BLACKLISTED`` and ``UNAVAILABLE`` both refuse, and so does a read that **raises**;
3. the refusal happens before the checkout port is reached, so an ineligible store has no
   discount code created for it — not locally, not by the merchant.

And it is a **gate, not a switch**: banning one store refuses that store's accept and leaves
every other store's alone. A blanket refusal would satisfy every negative assertion above and
would not be a gate, which is why the accept path keeps a positive control beside each denial.
"""

from __future__ import annotations

from typing import Any

from ..checkout import DEFAULT_CHECKOUT_MODE
from ..eligibility import (
    ELIGIBLE,
    EligibilityDecision,
    read_eligibility,
    speaks_supported_interface,
)

# `_UNSET` is IMPORTED, never re-created. A second `object()` sentinel here would not be
# identical to the one `accept()` compares against, so `registered_domains is _UNSET` would be
# False for every forwarded call and the sentinel itself would be passed on as the "platform
# lookup" — which is how this gate first shipped, and how it failed: every accept through it
# was refused with "exposes neither domain_for(store_id) nor __call__(store_id)".
from .offer import _UNSET, AcceptResult, _read, _refused, accept, next_slot
from .reasons import (
    DENIAL_REASONS,
    DENIAL_UNAVAILABLE,
    denial_code,
    denial_reason,
    describe,
    redact_addresses,
)

__all__ = ["accept_offer"]


def _denial_reason(decision: EligibilityDecision) -> str:
    """The recorded reason, guaranteed to begin with a **declared** denial code (T-204).

    The status word goes at the front rather than being left to the source's prose: an
    eligibility backend that answers ``BLACKLISTED`` with an empty reason — or with one in
    another language — must still produce a refusal an auditor can classify. This mirrors
    ``orchestration.solicitation._denial`` deliberately; two gates that describe the same
    condition differently are two policies wearing one name.

    The prefix test is now an exact match on the declared code rather than a case-insensitive
    ``startswith`` on the status word, and that is the T-204 repair: a source answering
    ``BLACKLISTED`` with the reason ``"Blacklisted for chargeback fraud"`` used to be passed
    through untouched, which put ``"Blacklisted for chargeback fraud"`` — the whole sentence —
    where the vocabulary term belongs, because nothing after it was a colon. It now reads
    ``blacklisted: Blacklisted for chargeback fraud``: one declared token, then the source's
    own words. A status this exchange does not speak degrades to ``unavailable``, which is
    the same fail-closed answer ``read_eligibility`` already gives it.
    """
    reason = (decision.reason or "").strip()
    code = decision.status if decision.status in DENIAL_REASONS else DENIAL_UNAVAILABLE
    if not reason:
        return code
    if denial_code(reason) == code:
        # THE BYPASS, and why it is redacted HERE rather than only in `denial_reason`. A
        # source that answers `unavailable` with prose that already begins `"unavailable: …"`
        # takes this branch and its sentence is returned VERBATIM — `denial_reason` is never
        # called, so a sanitiser installed only there closes the sibling shape below and
        # leaves this one live. That asymmetry is exactly what T-327 records: the pair
        # distinguishes "no sanitiser" from "sanitiser bypassed", and the whole point of the
        # branch is to keep the source's own words, which is precisely when the source's own
        # `<object object at 0x…>` travels with them.
        return redact_addresses(reason)
    return denial_reason(code, reason)


def accept_offer(
    *,
    auction: Any,
    bid_ref: Any,
    code_creator: Any = None,
    mode: str = DEFAULT_CHECKOUT_MODE,
    eligibility: Any = None,
    registered_domains: Any = _UNSET,
) -> AcceptResult:
    """Re-read the winning store's eligibility, then accept — or refuse, naming the condition.

    Args:
        auction: the auction being accepted on, in the shape :func:`.offer.accept` reads.
        bid_ref: which bid the buyer chose.
        code_creator: the merchant ``POST /codes`` client, forwarded untouched. It is never
            invoked here; a refusal must be able to prove no code was created for it.
        mode: ``CHECKOUT_MODE``, forwarded to the checkout registry.
        eligibility: a :class:`~apps.exchange.src.eligibility.SellerEligibility` declaring the
            interface version it speaks. ``None`` refuses — an accept with no eligibility
            source has not passed a gate, and "nobody wired one" is not evidence of innocence.
        registered_domains: the platform's registered-domain lookup, forwarded to
            :func:`.offer.accept`. See that module for why it decides whether the checkout
            host guard is real or decorative.

    Returns:
        :class:`AcceptResult` — ``accepted`` False with ``denial_reason`` naming the condition
        when the gate refuses, otherwise whatever the checkout port produced.
    """
    ref = str(bid_ref)
    bid = None
    for candidate in _read(auction, "bids") or ():
        if str(_read(candidate, "bid_id") or _read(candidate, "bid_ref") or "") == ref:
            bid = candidate
            break

    if bid is None:
        # Delegate rather than duplicate: `accept` already refuses an unknown bid with the
        # message and the re-offer this case wants, and there is no store to gate anyway.
        return accept(auction, ref, code_creator, mode, registered_domains=registered_domains)

    store_id = str(_read(bid, "store_id") or "")

    if not speaks_supported_interface(eligibility):
        declared = getattr(eligibility, "interface_version", None)
        return _refused(
            auction,
            ref,
            mode,
            denial_reason(
                DENIAL_UNAVAILABLE,
                # `describe`, not `{declared!r}`: `interface_version` is whatever the injected
                # source put there, and an object with the default `__repr__` would render a
                # memory address into a persisted, client-visible payload (T-264).
                f"the eligibility source speaks interface version {describe(declared)}, which "
                f"this exchange does not support; refusing to create a discount code for "
                f"{store_id!r} against an interface it cannot read",
            ),
            store_id=store_id,
            reoffer_bid_ref=next_slot(auction, ref),
        )

    decision = read_eligibility(eligibility, store_id)
    if decision.status != ELIGIBLE:
        return _refused(
            auction,
            ref,
            mode,
            _denial_reason(decision),
            store_id=store_id,
            # The next slot is a DIFFERENT store, so one store's ban is not the auction's end.
            reoffer_bid_ref=next_slot(auction, ref),
        )

    return accept(auction, ref, code_creator, mode, registered_domains=registered_domains)
