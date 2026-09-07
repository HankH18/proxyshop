"""The dashboard's reads of the three services that hold R9's data, and how they refuse.

The merchant service is a **backend for the dashboard**, not a pass-through. Two properties
follow from that and neither is negotiable:

**The exchange's report bearer never reaches the browser.** ``exchange.reports.routes``
resolves the store FROM the token — there is no field in which to ask for another merchant's
losses — so that token IS the store's identity at the exchange. It is held here, in the
service's environment, and the browser is handed the report rather than the key to it.

**A rival's row is dropped here, on the server.** ``GET /snapshot`` answers EVERY store at
once and ``GET /events`` will answer another store's rows if asked for them. Narrowing in the
browser would mean the rival's numbers had already been served to a competitor's machine,
where a devtools tab reads them. So the narrowing is :func:`_only_this_store`, it happens
before anything is rendered, and ``test_the_dashboard_never_carries_a_rival_store_id_or_a_
rival_amount`` scans the served bytes rather than a list of field names.

Every failure is a **named state**, never an empty success. A panel is ``ok``,
``not_configured`` (with the variables to set), ``unauthorized`` (our credential was refused),
``unreachable`` (nothing answered) or ``upstream_refused`` (something answered, badly). The
one state that does not exist is "empty because we could not ask", which is the state a chart
of zero losses would be rendering.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from .config import DashboardConfig

__all__ = [
    "NOT_CONFIGURED",
    "OK",
    "UNAUTHORIZED",
    "UNREACHABLE",
    "UPSTREAM_REFUSED",
    "Panel",
    "fetch_losses",
    "fetch_trust_events",
    "fetch_trust_snapshot",
    "open_client",
    "solicit_bid",
]

_log = logging.getLogger(__name__)

OK: Final[str] = "ok"
NOT_CONFIGURED: Final[str] = "not_configured"
UNAUTHORIZED: Final[str] = "unauthorized"
UNREACHABLE: Final[str] = "unreachable"
UPSTREAM_REFUSED: Final[str] = "upstream_refused"

#: How long a dashboard read waits on a service. Short on purpose: this is a page a human is
#: looking at, and four upstreams behind a 30-second default is a page that never loads.
TIMEOUT_SECONDS: Final[float] = 5.0

#: How many trust events a panel carries. The ledger is append-only and unbounded; a page is
#: not. The far end caps its own page too (``trust.events.routes.MAX_EVENT_PAGE``).
EVENT_PAGE: Final[int] = 50

#: The header a store agent puts its decline reason in — a 204 has no body to carry one.
#: Spelled here rather than imported: ``packages/store-agent`` is a different deployable and
#: this service must not grow an import into it (the merchant image ships no ``store_agent``).
#: ``store_agent.solicitation.routes.DECLINE_REASON_HEADER`` is the other end of the wire.
DECLINE_REASON_HEADER: Final[str] = "x-proxyshop-decline-reason"


@dataclass(frozen=True)
class Panel:
    """One region of the dashboard, and why it looks the way it does.

    ``data`` is present only in :data:`OK`. That is deliberate and it is the whole point of
    the type: there is no way to be in ``not_configured`` and also carry rows, so a template
    cannot accidentally render a refusal as an empty result.
    """

    state: str
    detail: str = ""
    missing: tuple[str, ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"state": self.state, "detail": self.detail}
        if self.missing:
            body["missing"] = list(self.missing)
        if self.state == OK:
            body.update(self.data)
        return body


def open_client(base_url: str, *, timeout: float = TIMEOUT_SECONDS) -> httpx.AsyncClient:
    """The client this module reaches an upstream with.

    A module-level seam, and the only one: a test replaces this name to point an upstream at
    an in-process ASGI app, exactly as the rest of this service's tests replace ``ENVELOPES``.
    Everything above it — the narrowing, the refusal vocabulary, the timeout — is the real
    code path in both cases.
    """
    return httpx.AsyncClient(base_url=base_url, timeout=timeout)


def _unreachable(what: str, exc: Exception) -> Panel:
    # The exception text carries the address, which is configuration and not a secret; the
    # bearer is never in it because httpx does not put headers in transport errors.
    _log.warning("the dashboard could not reach %s: %s", what, exc)
    return Panel(
        state=UNREACHABLE,
        detail=f"{what} did not answer: {exc}. The address is configured; the service is not up.",
    )


def _refused(what: str, response: httpx.Response) -> Panel:
    detail = response.text.strip()
    if len(detail) > 400:
        detail = detail[:400] + "…"
    return Panel(
        state=UPSTREAM_REFUSED,
        detail=f"{what} answered {response.status_code}: {detail or '(no body)'}",
    )


def _only_this_store(rows: Sequence[Any], store_id: str) -> list[dict[str, Any]]:
    """Every row that is about ``store_id``, and nothing else.

    Applied even though the request already carried a ``store_id`` filter, because a filter is
    a request and this is a guarantee. A trust service that ignored the parameter, or a future
    one that widened it, must not be able to publish a rival's rows through this page.
    """
    kept: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping) and str(row.get("store_id", "")) == store_id:
            kept.append(dict(row))
    return kept


async def fetch_losses(
    config: DashboardConfig, store_id: str, *, start: float, end: float
) -> Panel:
    """R9's win/loss report for ONE store, over the exchange's published door.

    The store is resolved at the far end from the bearer, so this function cannot ask for
    another merchant's report even by mistake — there is no parameter for it. What comes back
    is reason categories and unmet buyer criteria; the amount suppression is the exchange's
    own projection and is not re-implemented here.
    """
    missing = config.losses_missing(store_id)
    if missing:
        return Panel(
            state=NOT_CONFIGURED,
            missing=missing,
            detail=(
                "no win/loss report can be fetched until this deployment states the exchange's "
                f"address and this store's report bearer: set {', '.join(missing)}. The bearer "
                "is what the exchange resolves the store from, so it is one token per store."
            ),
        )
    token = config.report_token_for(store_id)
    try:
        async with open_client(config.exchange_url) as client:
            response = await client.get(
                "/reports/losses",
                params={"start": start, "end": end},
                headers={"authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        return _unreachable(f"the exchange at {config.exchange_url}", exc)

    if response.status_code == 401:
        return Panel(
            state=UNAUTHORIZED,
            detail=(
                "the exchange refused this store's report bearer. The table this service holds "
                f"({', '.join(config.losses_missing(store_id)) or 'MERCHANT_REPORT_TOKENS'}) and "
                "the exchange's own `report_tokens_file` name the same secret; they disagree."
            ),
        )
    if response.status_code != 200:
        return _refused("the exchange's loss report", response)

    try:
        report = response.json()
    except ValueError as exc:
        return Panel(
            state=UPSTREAM_REFUSED, detail=f"the exchange's loss report was not JSON: {exc}"
        )
    if not isinstance(report, Mapping):
        return Panel(state=UPSTREAM_REFUSED, detail="the exchange's loss report was not an object")

    # The store id is taken from the REQUEST, not from the body: the body's is whatever the
    # far end resolved the bearer to, and echoing it would let a mis-provisioned token label
    # this page with somebody else's store.
    return Panel(
        state=OK,
        detail="",
        data={
            "store_id": store_id,
            "window": report.get("window") or {"start": start, "end": end},
            "by_cluster": list(report.get("by_cluster") or ()),
        },
    )


async def fetch_trust_snapshot(config: DashboardConfig, store_id: str) -> Panel:
    """This store's own row out of ``GET /snapshot``, with every other store discarded."""
    missing = config.trust_missing()
    if missing:
        return Panel(
            state=NOT_CONFIGURED,
            missing=missing,
            detail=(
                "no trust score can be shown until this deployment states the trust service's "
                f"address: set {', '.join(missing)}."
            ),
        )
    try:
        async with open_client(config.trust_url) as client:
            response = await client.get("/snapshot")
    except httpx.HTTPError as exc:
        return _unreachable(f"the trust service at {config.trust_url}", exc)
    if response.status_code != 200:
        return _refused("the trust service's snapshot", response)
    try:
        every_store = response.json()
    except ValueError as exc:
        return Panel(state=UPSTREAM_REFUSED, detail=f"the trust snapshot was not JSON: {exc}")
    if not isinstance(every_store, Mapping):
        return Panel(state=UPSTREAM_REFUSED, detail="the trust snapshot was not an object")

    mine = every_store.get(store_id)
    if not isinstance(mine, Mapping):
        return Panel(
            state=OK,
            detail=(
                "the trust service holds no observations for this store yet. A new store starts "
                "at a neutral low-confidence prior (R12) and earns a row from its first "
                "verification or transaction outcome."
            ),
            data={"snapshot": None},
        )
    # `dict(mine)` and nothing from the enclosing mapping: the rival rows are not narrowed,
    # they are never carried.
    return Panel(state=OK, data={"snapshot": dict(mine)})


async def fetch_trust_events(config: DashboardConfig, store_id: str) -> Panel:
    """R13's payloads: the full pseudonymous events that moved this store's score."""
    missing = config.trust_missing()
    if missing:
        return Panel(
            state=NOT_CONFIGURED,
            missing=missing,
            detail=(
                "no trust event payloads can be shown until this deployment states the trust "
                f"service's address: set {', '.join(missing)}."
            ),
        )
    try:
        async with open_client(config.trust_url) as client:
            response = await client.get(
                "/events", params={"store_id": store_id, "limit": EVENT_PAGE}
            )
    except httpx.HTTPError as exc:
        return _unreachable(f"the trust service at {config.trust_url}", exc)
    if response.status_code != 200:
        return _refused("the trust service's event ledger", response)
    try:
        page = response.json()
    except ValueError as exc:
        return Panel(state=UPSTREAM_REFUSED, detail=f"the trust event page was not JSON: {exc}")
    if not isinstance(page, Mapping):
        return Panel(state=UPSTREAM_REFUSED, detail="the trust event page was not an object")

    rows = page.get("events")
    events = _only_this_store(rows if isinstance(rows, Sequence) else (), store_id)
    return Panel(
        state=OK,
        detail=(
            "a projection of the chained ledger, not the chain: the links skip whatever the "
            "store filter removed, so this page cannot be handed to a verifier."
        ),
        data={"events": events, "count": len(events), "truncated": bool(page.get("truncated"))},
    )


@dataclass(frozen=True)
class Solicitation:
    """What this store's own agent answered when the merchant asked it for a bid."""

    outcome: str
    status_code: int
    decline_reason: str | None = None
    bid: Mapping[str, Any] | None = None
    detail: str = ""


async def solicit_bid(
    config: DashboardConfig, bid_request: Mapping[str, Any]
) -> Solicitation | Panel:
    """Ask this store's agent for a bid, exactly the way the exchange asks it.

    Returns a :class:`Solicitation` when the agent answered at all — a 200 with a `Bid` or the
    contract's 204 decline, whose reason travels in a header because a 204 has no body. A
    :class:`Panel` comes back instead when there was nobody to ask, so the caller renders the
    same "unconfigured / unreachable" vocabulary the read panels use.

    **The answer is never invented.** A 204 with no reason header is reported as a decline with
    no reason rather than as a refusal, because that is what the agent said; a transport
    failure is `unreachable`, and neither is ever rendered as "this store did not want to bid".
    """
    missing = config.store_agent_missing()
    if missing:
        return Panel(
            state=NOT_CONFIGURED,
            missing=missing,
            detail=(
                "this deployment states no address for the store's own agent, so no bid can be "
                f"solicited and no bid history exists to show: set {', '.join(missing)}. One "
                "agent process advocates for one store."
            ),
        )
    try:
        async with open_client(config.store_agent_url) as client:
            response = await client.post("/v1/bid-requests", json=dict(bid_request))
    except httpx.HTTPError as exc:
        return _unreachable(f"the store agent at {config.store_agent_url}", exc)

    if response.status_code == 204:
        return Solicitation(
            outcome="declined",
            status_code=204,
            decline_reason=response.headers.get(DECLINE_REASON_HEADER) or None,
            detail="the agent answered the contract's decline; the reason is in its header.",
        )
    if response.status_code != 200:
        return _refused("the store agent", response)
    try:
        bid = response.json()
    except ValueError as exc:
        return Panel(state=UPSTREAM_REFUSED, detail=f"the store agent's bid was not JSON: {exc}")
    if not isinstance(bid, Mapping):
        return Panel(state=UPSTREAM_REFUSED, detail="the store agent's bid was not an object")
    return Solicitation(outcome="bid", status_code=200, bid=dict(bid))
