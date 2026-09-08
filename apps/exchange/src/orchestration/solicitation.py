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

The gate decides *who* is asked; it also owns the clock the answers are judged by. The
arrival stamp ``collect_bids`` enforces the deadline on is taken by the exchange, from the
:class:`~apps.exchange.src.auction.fanout.ArrivalClock` this function builds — never from
the bidder's payload. A store that could stamp its own reply would be choosing its own
deadline, and would win every auction by answering after the window with a price picked
once the field was visible.

Tier-0 stores are never solicited: Tier-0 means catalog-only, with no bidding agent to
answer. An **eligible** Tier-0 store is still represented, at list price, by
``collect_bids`` — that is R10 — it just is not asked a question nobody is home to hear.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..auction.collect import NO_AGENT_FIELD, BidEntry, collect_bids
from ..auction.fanout import (
    DEFAULT_BID_WINDOW_SECONDS,
    ArrivalClock,
    FanOut,
    sequential_fan_out,
)
from ..eligibility import (
    ELIGIBLE,
    UNAVAILABLE,
    EligibilityDecision,
    read_eligibility,
    speaks_supported_interface,
)

__all__ = ["Denial", "SolicitationResult", "solicit_bids", "stores_with_no_agent"]


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


def stores_with_no_agent(solicitor: Any, stores: Sequence[Mapping[str, Any]]) -> set[str]:
    """Which of these stores the SOLICITOR says it holds no way to reach.

    **The false statement this ends.** ``askable`` selected on tier alone, so a Tier-1 store
    the exchange holds no ``bid_endpoint`` for was named in ``solicited`` — the auction's own
    claim about who it asked — and then, because no response object existed for it, recorded
    ``no_response``. Measured on the deployed droplet: the ``201`` listed all six rostered
    stores as solicited with ``not_asked: 0``, while ``HttpBidSolicitor.solicit`` had returned
    ``None`` for ``bulksupplements.com`` and ``nutricost.com`` without opening a socket. Both
    reached the shopper as shops that had been asked and had said nothing. Neither had been
    asked, and ``no_response`` is glossed to a shopper as "switched off or too slow to reach".

    **Why the solicitor is asked rather than the roster read.** The endpoint registry lives on
    the outbound client (``HttpBidSolicitor._endpoints``, built in the composition root from
    the deployment document) and this module cannot see it — nor should it, since a different
    solicitor reaches stores by a different means entirely. So the port grows one optional
    question, ``can_solicit(store_id) -> bool``, answered by whoever actually holds the
    addresses.

    **Optional, and absent means reachable.** Every solicitor in this repository that does not
    implement it — ``NullSolicitor``, ``e2e``'s ``HostedAgentSolicitor``, the simulator's
    scripted doubles, every in-process test double — keeps exactly the behaviour it had. That
    direction is deliberate and it is NOT the usual fail-closed: the closed answer here is "ask
    nobody", which would silently empty the market on any deployment whose solicitor predates
    this hook. What is being decided is how truthfully a run is REPORTED, not who is allowed to
    trade, and a reporting hook that could stop an auction happening would be a worse fault
    than the one it fixes.

    A store whose ``can_solicit`` RAISES is treated as reachable and is asked — **per store,
    not per solicitor**. The distinction is worth stating because the first draft of this
    sentence said the hook was "treated as one that is not there", which is a different and
    weaker promise: a predicate that raises for one store and answers ``False`` for the next
    still has the second answer believed. That is the right behaviour — an answer this exchange
    did get is still an answer — but a reader relying on the stronger reading would be wrong
    about half the roster.
    """
    knows = getattr(solicitor, "can_solicit", None)
    if not callable(knows):
        return set()
    unreachable: set[str] = set()
    for store in stores:
        store_id = str(store.get("store_id") or "")
        if not store_id:
            continue
        try:
            reachable = knows(store_id)
        except Exception:
            continue
        if not reachable:
            unreachable.add(store_id)
    return unreachable


def solicit_bids(
    *,
    roster: Sequence[Mapping[str, Any]],
    solicitor: Any,
    eligibility: Any,
    now: float,
    fan_out: FanOut | None = None,
    window: float = DEFAULT_BID_WINDOW_SECONDS,
    clock: Any | None = None,
    started_at: float | None = None,
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
        window: how long the bidding window really lasts, in wall-clock seconds. ``now`` is
            the *logical* instant the window ends; this is the *real* duration it lasts, and
            the two together are what let the exchange stamp arrivals itself without
            breaking a frozen clock. A caller that knows its own timeout (the route does —
            it is ``bid_timeout_seconds``) should pass it; otherwise the platform default
            applies.
        clock: the authoritative arrival clock, injected into the fan-out. Defaults to an
            :class:`~apps.exchange.src.auction.fanout.ArrivalClock` over ``now``/``window``.
            **This is the exchange's clock, never a bidder's.** A solicited store's own
            ``received_at`` is discarded by the fan-out precisely because the deadline is
            enforced on that field: a store that set it would be choosing when its own
            auction closed.
        started_at: the :func:`time.monotonic` reading taken **when ``now`` was computed** —
            the real instant the window opened. This is what makes R10's timeout a bound on
            the *request* rather than only on the fan-out. Everything this function does
            before anyone is asked is I/O — the interface check, then an eligibility read
            for every rostered store — and until this argument existed the arrival clock
            was anchored to its own construction, so all of that time was spent *outside*
            the window and the fan-out still got the full ``window`` of real seconds after
            it. A slow eligibility backend could therefore double the request while R10
            reported a hard timeout. Omitted, the window starts here (the old behaviour),
            which is right only for a caller with nothing earlier to anchor to.

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
    tiered = [store for store in eligible if int(store.get("tier", 1)) > 0]

    # AND the stores this exchange holds no way to reach, which is the same fact arriving from
    # the other side: the merchant chose catalogue-only above, the deployment document is
    # silent about an endpoint here. Both mean "there is no agent to ask", and neither is a
    # store that stayed silent when spoken to. See `stores_with_no_agent`.
    no_agent = stores_with_no_agent(solicitor, tiered)
    askable = [store for store in tiered if str(store["store_id"]) not in no_agent]

    # `solicited` is the auction's own claim about WHO IT ASKED, published on the 201 and
    # counted by `market_summary`. It is written from `askable` and not from `tiered`, so it
    # cannot name a store no socket was ever opened to.
    result.solicited = [str(store["store_id"]) for store in askable]

    strategy: FanOut = fan_out if fan_out is not None else sequential_fan_out
    # Anchored to when the window OPENED, not to now: every eligibility read above happened
    # inside the window, and the fan-out gets only what is left of it.
    arrival = (
        clock if clock is not None else ArrivalClock(now, window=window, started_at=started_at)
    )
    responses = list(strategy(askable, solicitor, deadline=now, clock=arrival))

    # The exchange's own record of the stores it did not dial, minted here for the same reason
    # the fan-out mints its two markers: no response object exists, so the only honest account
    # of what happened is the one the exchange writes about itself. Without it these stores
    # still reach `collect_bids` through `eligible` and still take its `no_response` default —
    # dropping them from `askable` alone would move the false statement rather than end it.
    responses.extend(
        {"store_id": store["store_id"], NO_AGENT_FIELD: True}
        for store in tiered
        if str(store["store_id"]) in no_agent
    )

    result.entries = collect_bids(eligible, responses, now)
    return result
