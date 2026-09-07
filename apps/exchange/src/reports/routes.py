"""R9's merchant-facing loss report, on a served route (T-9b).

    GET /reports/losses?start=<instant>&end=<instant>
    Authorization: Bearer <this store's report token>

What was measured before this file existed
==========================================
``apps/exchange/src/reports/`` held 454 lines and 20 passing tests and **no ``routes.py``**.
``exchange.main.create_app``'s automount is a frozen ``glob("*/routes.py")``, so no request
could reach any of it; a started process logged ``5 feature router(s) mounted`` and
``exchange.reports.routes`` was not among them. R9's "win/loss reports aggregated by intent
cluster" was code with a test suite and no door.

Who calls it, stated plainly
============================
**R9 names a dashboard, and the dashboard does not exist in this repository.** There is no
merchant UI that reads this route today, and pretending otherwise is the failure mode this
whole batch is removing. What this route is NOT is an island, and the difference is the
producer: ``POST /auctions`` writes the loss log on every close (``reports.log.record_losses``),
so the data is real and observable, and the door onto it is the published one a dashboard —
or a merchant's own script, or ``curl`` — reaches with a bearer token. The alternative was to
leave 454 lines addressable only from a unit test, which is strictly worse: an unreachable
report cannot be wrong in a way anybody notices.

The merchant service's dashboard app (``apps/merchant``) is where such a page would live, and
it would call this URL. That work is not this ticket's and is not claimed here.

Authorisation: the token IS the store id
========================================
The gate follows the shape this repository already uses —
``merchant_svc.install.routes._refuse_unless_admin``: a bearer token, no default, no fallback,
and a deployment that has not configured one serves **nobody** (``503``), because an empty
expected value compared against an empty supplied one is a route that opens itself the moment
the environment is incomplete.

It differs in one way, and deliberately. The merchant service holds ONE administrative token
because its administrative routes are the operator's. This route serves **one merchant its own
losses**, so a single shared token would authenticate a caller and say nothing about which
store's report it may read — and a ``store_id`` path or query parameter beside a shared token
is exactly the shape that leaks the day somebody forgets the comparison. So the table is
``{store_id: token}`` and the store is **resolved from the bearer**. There is no field in which
to ask for another merchant's losses.

The comparison is constant-time and runs over EVERY row rather than stopping at the first
match, so the time it takes says nothing about which store's token was presented, or how much
of one was right.

Where the token table comes from
================================
``exchange.composition``: a deployment document may name ``report_tokens_file``, a path to a
file holding ``{store_id: token}``. The same shape, the same refusals and the same
"a named-but-unreadable file is a misconfiguration, not an exchange with no merchants" rule as
``external_bid_keyring_file`` and ``merchant_admin_token_file``. Secrets are never inline in the
deployment document, which is a plain JSON file that gets pasted around.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Mapping
from typing import Any

from contracts import LossReport, LossWindow
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .log import DEFAULT_LOSS_LOG_CAPACITY, LossLog
from .loss import LossReportLeak, build_loss_report

__all__ = [
    "REPORTS_PATH",
    "configure_reports",
    "loss_log_of",
    "report_tokens_of",
    "router",
]

_log = logging.getLogger(__name__)

router = APIRouter(tags=["reports"])

#: The published path. Named rather than spelled twice, because the test drives the same
#: constant the route registers — a path that agreed with itself only in two string literals is
#: one rename away from a suite that passes against a 404.
REPORTS_PATH = "/reports/losses"

#: What ``app.state`` calls each collaborator.
TOKENS_STATE = "report_tokens"
LOG_STATE = "loss_log"


def configure_reports(
    app: Any,
    *,
    tokens: Mapping[str, str] | None = None,
    log: LossLog | None = None,
) -> None:
    """Wire the reports collaborators. Anything omitted keeps what is already there.

    A deployment that never calls this has no token table, so the route refuses every caller —
    which is the correct posture for a service nobody has configured reports on, and is
    asserted as such rather than assumed.
    """
    if tokens is not None:
        app.state.report_tokens = {str(k): str(v) for k, v in tokens.items() if str(k) and str(v)}
    if log is not None:
        app.state.loss_log = log


def report_tokens_of(app: Any) -> Mapping[str, str]:
    """The ``{store_id: token}`` table, or an empty mapping when none is bound."""
    table = getattr(app.state, TOKENS_STATE, None)
    return table if isinstance(table, Mapping) else {}


def loss_log_of(app: Any) -> LossLog:
    """The loss log, creating the process-local default on first use.

    Created lazily and stored, rather than bound at import: ``POST /auctions`` writes through
    this function too, so an exchange nobody configured still collects the rows a later
    ``configure_reports`` would want — and an exchange that IS configured gets exactly one log
    rather than one per request.
    """
    existing = getattr(app.state, LOG_STATE, None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    created = LossLog(capacity=DEFAULT_LOSS_LOG_CAPACITY)
    app.state.loss_log = created
    return created


def _problem(status: int, reason: str, **detail: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": reason, **detail})


def _store_for(bearer: str, tokens: Mapping[str, str]) -> str | None:
    """The store whose token this is, or ``None``.

    Every row is compared, and each comparison is ``hmac.compare_digest``. Stopping at the first
    match would make the response time a function of where in the table the presented token sits
    — which is a store-ordering oracle, not a secret-recovery one, but it is free to close and
    the loop reads no worse for it.

    An empty ``bearer`` matches nothing: ``configure_reports`` drops empty tokens from the table,
    so there is no row an empty string could equal, and this returns ``None`` rather than
    the first store with a falsy secret.
    """
    if not bearer:
        return None
    found: str | None = None
    for store_id, token in tokens.items():
        if hmac.compare_digest(str(token), bearer):
            found = store_id
    return found


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return supplied.strip()


@router.get(REPORTS_PATH, response_model=LossReport)
async def read_loss_report(
    request: Request,
    start: float = Query(..., description="Window start, epoch seconds. Inclusive."),
    end: float = Query(..., description="Window end, epoch seconds. Inclusive."),
) -> Any:
    """One merchant's own win/loss report over ``[start, end]`` (R9).

    Reason categories and unmet buyer criteria only. No rival identities and no amounts —
    enforced by :func:`~.loss.build_loss_report`'s projection rather than by a list of forbidden
    field names here, and by this route reading only the rows whose subject is the calling
    store, so a report cannot contain another merchant's row to begin with.

    A store with no losses in the window is served an empty report, not a 404: "you lost
    nothing" is an answer, and a 404 would be indistinguishable from a store the exchange has
    never heard of — which is a fact about the merchant list this route has no business
    publishing.
    """
    tokens = report_tokens_of(request.app)
    if not tokens:
        return _problem(
            503,
            "reports-not-configured",
            detail=(
                "this exchange holds no report token table, so it serves no merchant its "
                "losses. State `report_tokens_file` in the deployment document"
            ),
        )

    store_id = _store_for(_bearer(request), tokens)
    if store_id is None:
        # Names no store and echoes nothing that arrived: a refusal that quoted the presented
        # bearer would put it in every proxy log between here and the caller.
        return _problem(401, "unauthorized")

    try:
        reports = build_loss_report(
            loss_log_of(request.app).rows_for(store_id),
            {
                "start": start,
                "end": end,
            },
        )
    except ValueError as exc:
        # A malformed window is the caller's; a malformed ROW is this exchange's own and is a
        # 500 rather than a 400 dressed up as one. `build_loss_report` raises ValueError for
        # both, so the two are told apart by whether the window itself reads.
        if start != start or end != end or end < start:  # NaN, or an inverted window
            return _problem(400, "invalid-window", detail=str(exc))
        raise
    except LossReportLeak:
        # The report's own egress scan refused to hand back a body carrying something the
        # projection never read. It is re-raised as a 500 with NOTHING from the report in it:
        # the exception's message names the value it matched, which is the value that must not
        # leave. Logged locally, at the one place an operator can see it.
        _log.exception("loss report for a merchant was refused by its own egress scan")
        return _problem(500, "report-refused-by-its-own-guard")

    if not reports:
        return LossReport(store_id=store_id, window=LossWindow(start=start, end=end), by_cluster=[])
    return reports[0]
