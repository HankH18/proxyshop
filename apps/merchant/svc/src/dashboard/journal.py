"""What this service saw its store's agent answer, kept so a merchant can read it back.

WHAT THIS IS, AND — MORE IMPORTANTLY — WHAT IT IS NOT
====================================================
R9 asks the dashboard to show *every bid with its rationale*. The rationale exists: it is
``store_agent.modes.runner.BidLogEntry.rationale``, written on every auction in every mode,
and it is the audit trail R7's shadow mode is *for*. **It has no served door.** The log is a
``BoundedBidLog`` — a 256-entry ring on ``app.state`` inside the store-agent process
(``store_agent.solicitation.advocate``) — and ``packages/store-agent`` publishes exactly two
operations, ``POST /v1/bid-requests`` and ``POST /v1/trust-events``. Neither reads it. So
there is no way, from this service or any other, to fetch the bids a live auction produced.

This journal is therefore **not** that log and never claims to be. It records the
solicitations the *merchant* made from their own dashboard: a real ``POST /v1/bid-requests``
against the store's real agent, answered by the real bidding runtime, recorded verbatim. It is
a rehearsal, and every row says so — ``origin: "dashboard"`` — because a merchant who mistook
these for the auctions they lost would be reading their own clicks as market data.

It is worth having for the reason R9's kill switch is worth having: an activation state that
cannot be observed from outside is one nobody can trust. A merchant hits the kill switch and
solicits again; the agent answers ``204 store_killed``; that row is the proof, and it is the
same proof an operator would get with ``curl``.

The row carries the merchant's OWN offer and nothing about anyone else. There is no rival in
a solicitation — one store, one agent, one answer — so unlike the loss report there is no
projection to make here, and that is a property to keep rather than a gap to fill.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

__all__ = ["MAX_ENTRIES", "SOLICITATIONS", "SolicitationJournal", "SolicitationRecord"]

#: How many rows one store keeps. A ring, for the reason every other in-process log in this
#: repository is one: this is written on a request path, and an unbounded list behind a
#: request path is a memory bound the caller chooses.
MAX_ENTRIES: Final[int] = 100

#: Every row this journal holds was produced by the merchant's own dashboard.
ORIGIN: Final[str] = "dashboard"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SolicitationRecord:
    """One solicitation: what was asked, what came back, and the state it was asked in.

    ``activation`` is recorded from the MERCHANT's envelope at the moment of the probe, beside
    the agent's own answer, because the two together are the finding: "I killed it and it
    still bid" and "I killed it and it stopped" are the same request with different rows.
    """

    recorded_at: str
    store_id: str
    auction_id: str
    outcome: str
    activation: str
    may_bid: bool
    decline_reason: str | None = None
    offer: Mapping[str, Any] | None = None
    claims: int = 0
    message: str | None = None
    detail: str = ""

    @property
    def contradiction(self) -> bool:
        """The merchant said this store may not bid, and its agent bid anyway.

        **This is a real state and it is reachable today**, which is why it is a field rather
        than an assertion. Measured on two real uvicorn processes, this repo's own
        ``fixtures/envelopes/store-alpha.approved.json``, nothing stubbed:

            PUT  /stores/store-alpha/envelope   -> 200  (version 1, shadow)
            POST /stores/store-alpha/kill       -> 200  {"activation": "killed"}
            POST /stores/store-alpha/bids/solicit -> 200 {"outcome": "bid", ...}

        The merchant service holds the authoritative envelope; a *separately deployed* agent
        holds its own copy, read once from ``STORE_AGENT_CONTEXT``, and **nothing delivers one
        to the other**. ``merchant_svc.bidding.store_agent_context`` is the producer of exactly
        that hand-over and, measured with ``grep`` over every service, it has no caller outside
        this package. The exchange cannot close the gap either: C3 forbids it a code path that
        reads envelopes at all.

        So the kill switch stops a store whose agent reads a killed envelope, and does not stop
        one that does not. A dashboard that showed the merchant "killed" beside a row saying
        "bid" without naming the disagreement would be the most expensive kind of quiet: the
        merchant would believe the store was off.
        """
        return self.outcome == "bid" and not self.may_bid

    def as_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "recorded_at": self.recorded_at,
            "origin": ORIGIN,
            "store_id": self.store_id,
            "auction_id": self.auction_id,
            "outcome": self.outcome,
            "activation": self.activation,
            "may_bid": self.may_bid,
            "claims": self.claims,
            "contradiction": self.contradiction,
        }
        if self.decline_reason is not None:
            body["decline_reason"] = self.decline_reason
        if self.offer is not None:
            body["offer"] = dict(self.offer)
        if self.message is not None:
            body["message"] = self.message
        if self.detail:
            body["detail"] = self.detail
        return body


def offer_projection(bid: Mapping[str, Any]) -> dict[str, Any]:
    """The merchant-facing shape of their own offer.

    A projection rather than the whole `Bid` for the same reason the loss report projects: a
    row that keeps a live reference to the answer is a row a future field rides out on. The
    fields kept are the ones a merchant reads to decide whether the agent is offering what
    they authorised — the product, the price, the discount and the commitments.
    """
    offer = bid.get("offer")
    if not isinstance(offer, Mapping):
        return {}
    discount = offer.get("discount")
    commitments = offer.get("commitments")
    return {
        "product_ref": offer.get("product_ref"),
        "variant_ref": offer.get("variant_ref"),
        "unit_price": offer.get("unit_price"),
        "total_price": offer.get("total_price"),
        "currency": offer.get("currency"),
        "discount": dict(discount) if isinstance(discount, Mapping) else None,
        "commitments": [
            str(item.get("key"))
            for item in (commitments or ())
            if isinstance(item, Mapping) and item.get("key")
        ],
        "expires_at": offer.get("expires_at"),
    }


class SolicitationJournal:
    """Per-store rings of :class:`SolicitationRecord`, newest first on read."""

    __slots__ = ("_maxlen", "_rows")

    def __init__(self, maxlen: int = MAX_ENTRIES) -> None:
        self._maxlen = maxlen if maxlen > 0 else MAX_ENTRIES
        self._rows: dict[str, deque[SolicitationRecord]] = {}

    def record(self, entry: SolicitationRecord) -> SolicitationRecord:
        self._rows.setdefault(entry.store_id, deque(maxlen=self._maxlen)).append(entry)
        return entry

    def entries(self, store_id: str) -> tuple[SolicitationRecord, ...]:
        """This store's rows, newest first — the order a merchant reads a log in."""
        return tuple(reversed(self._rows.get(store_id, ())))

    def append_bid(
        self,
        *,
        store_id: str,
        auction_id: str,
        activation: str,
        may_bid: bool,
        bid: Mapping[str, Any],
    ) -> SolicitationRecord:
        claims = bid.get("claims")
        detail = ""
        if not may_bid:
            detail = (
                f"this store's envelope is {activation!r} here and its agent bid anyway. The "
                "merchant service holds the authoritative envelope and nothing delivers it to "
                "a separately deployed agent, which reads its own STORE_AGENT_CONTEXT once at "
                "start-up. Until that hand-over exists, the kill switch stops an agent whose "
                "own copy is killed and does not stop one whose copy is stale."
            )
        return self.record(
            SolicitationRecord(
                recorded_at=_now(),
                store_id=store_id,
                auction_id=auction_id,
                outcome="bid",
                activation=activation,
                may_bid=may_bid,
                offer=offer_projection(bid),
                claims=len(claims) if isinstance(claims, (list, tuple)) else 0,
                message=bid.get("message"),
                detail=detail,
            )
        )

    def append_decline(
        self,
        *,
        store_id: str,
        auction_id: str,
        activation: str,
        may_bid: bool,
        reason: str | None,
        detail: str = "",
    ) -> SolicitationRecord:
        return self.record(
            SolicitationRecord(
                recorded_at=_now(),
                store_id=store_id,
                auction_id=auction_id,
                outcome="declined",
                activation=activation,
                may_bid=may_bid,
                decline_reason=reason,
                detail=detail,
            )
        )


#: The service-wide journal. Process-local and forgotten on restart, which is the honest
#: property for a rehearsal log: losing it can only remove evidence a merchant collected, never
#: change what their agent will do next.
SOLICITATIONS = SolicitationJournal()
