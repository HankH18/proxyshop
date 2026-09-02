"""The R12 solicitation gate: eligibility is read for every store, before anyone is asked.

D54 puts this gate at the **public orchestration boundary**, and the reason is worth keeping
in front of whoever reads this next. ``collect_bids(roster, responses, now)`` is a pure
function that is handed a roster and told to represent it; it has no idea who was asked, and
a gate inside it would have to deny whenever no eligibility port was injected — which is the
wrong default for a helper and the wrong layer for a system guarantee. This is the layer that
decides *who gets asked*, so this is where "may this store participate" is answered.

The ordering is the guarantee, not a detail:

1. **The interface version is checked first.** A source speaking a version this exchange does
   not know has an unknown status vocabulary, so consulting it is indistinguishable from not
   consulting one. Nobody is solicited; every rostered store is denied.
2. **Eligibility is read for every rostered store**, before a single store is asked. Not
   lazily, not only for the ones we were about to ask — R12's promise is that the gate ran.
3. **Only then** are the eligible stores solicited, in roster order.

Fail closed means three things, and all three land in ``denied``: ``BLACKLISTED`` denies,
``UNAVAILABLE`` denies, and an eligibility read that *raises* denies exactly like
``UNAVAILABLE``. An ineligible store is never asked and never appears in ``entries`` — not
even as a list-price fallback, because a fallback entry is still a bid a blacklisted store
could win with.

Tier-0 stores are never solicited: Tier-0 means catalog-only, with no bidding agent to
answer. An **eligible** Tier-0 store is still represented, at list price, by
``collect_bids`` — that is R10 — it just is not asked a question nobody is home to hear.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..auction.collect import BidEntry, collect_bids
from ..auction.fanout import FanOut, sequential_fan_out
from ..eligibility import (
    ELIGIBLE,
    UNAVAILABLE,
    EligibilityDecision,
    read_eligibility,
    speaks_supported_interface,
)

__all__ = ["Denial", "SolicitationResult", "solicit_bids"]


@dataclass(frozen=True)
class Denial:
    """One store the gate refused, and the condition it names."""

    store_id: str
    status: str
    reason: str


@dataclass
class SolicitationResult:
    """What the gate did: who was asked, what came back, and who was refused."""

    solicited: list[str] = field(default_factory=list)
    entries: list[BidEntry] = field(default_factory=list)
    denied: list[Denial] = field(default_factory=list)

    @property
    def denied_store_ids(self) -> list[str]:
        return [denial.store_id for denial in self.denied]


def _denial(decision: EligibilityDecision) -> Denial:
    """Build the recorded denial.

    The status word is put at the front of the reason deliberately. The reason a gate
    records must name the condition *itself*, and it may not depend on an eligibility
    source having written a helpful sentence — a source that answers ``BLACKLISTED`` with
    an empty reason, or with a reason in another language, must still produce a denial a
    reader (and an auditor) can classify.
    """
    reason = (decision.reason or "").strip()
    if not reason:
        reason = decision.status
    elif not reason.lower().startswith(decision.status.lower()):
        reason = f"{decision.status}: {reason}"
    return Denial(store_id=decision.store_id, status=decision.status, reason=reason)


def solicit_bids(
    *,
    roster: Sequence[Mapping[str, Any]],
    solicitor: Any,
    eligibility: Any,
    now: float,
    fan_out: FanOut | None = None,
) -> SolicitationResult:
    """Run the R12 gate over ``roster``, then fan out to whoever survives it.

    Args:
        roster: the stores selected for this auction, in the order they should be asked —
            ``{store_id, tier, product_ref, list_price}``.
        solicitor: the outbound ``POST /v1/bid-requests`` client. Exposes
            ``solicit(store)`` (or is callable) and returns
            ``{store_id, received_at, bid}`` or ``None`` for a decline/timeout.
        eligibility: a :class:`apps.exchange.src.eligibility.SellerEligibility`
            implementation declaring the interface version it speaks.
        now: the fan-out deadline, a float epoch, passed through to ``collect_bids``.
        fan_out: how the eligible stores are asked. Defaults to
            :func:`apps.exchange.src.auction.fanout.sequential_fan_out`, which asks in
            roster order — deterministic, and the order this result reports as
            ``solicited``. A live auction passes
            :func:`~apps.exchange.src.auction.fanout.parallel_fan_out` instead; the gate
            above is identical either way, which is why the strategy is a parameter rather
            than a branch.

    Returns:
        :class:`SolicitationResult` — ``solicited`` (store ids actually asked, in roster
        order), ``entries`` (one per *eligible* store), ``denied`` (one per refused store,
        each naming its condition).
    """
    result = SolicitationResult()

    if not speaks_supported_interface(eligibility):
        declared = getattr(eligibility, "interface_version", None)
        for rostered in roster:
            store_id = str(rostered["store_id"])
            result.denied.append(
                Denial(
                    store_id=store_id,
                    status=UNAVAILABLE,
                    reason=(
                        f"unavailable: the eligibility source speaks interface version "
                        f"{declared!r}, which this exchange does not support; refusing to "
                        f"solicit {store_id} against an interface it cannot read"
                    ),
                )
            )
        return result

    # Gate first, for EVERY rostered store, before anything is asked.
    eligible: list[Mapping[str, Any]] = []
    for rostered in roster:
        store_id = str(rostered["store_id"])
        decision = read_eligibility(eligibility, store_id)
        if decision.status == ELIGIBLE:
            eligible.append(rostered)
        else:
            result.denied.append(_denial(decision))

    # Only now does anyone get asked. Tier-0 is catalog-only — there is no agent to ask —
    # so it is skipped here and still represented at list price by `collect_bids` (R10).
    askable = [store for store in eligible if int(store.get("tier", 1)) > 0]
    result.solicited = [str(store["store_id"]) for store in askable]

    strategy: FanOut = fan_out if fan_out is not None else sequential_fan_out
    responses = strategy(askable, solicitor, deadline=now)

    result.entries = collect_bids(eligible, responses, now)
    return result
