"""The buyer service's composition root: the one place the buyer→exchange seam is joined.

``apps/buyer/svc/src/accept/routes.py`` states the defect this module closes, verbatim:

    The exchange client is read from ``app.state.exchange_client`` and is **not**
    constructed here [...] **Nothing in this repository sets that attribute yet** — the
    buyer→exchange seam has no composition root on either side.

Both halves of the buyer's HTTP surface are injected and both fail closed, which is the
right shape for the components and was, until this file existed, also the reason nothing
joined them. ``uvicorn buyer_svc.main:app`` boots a buyer service in which

* ``app.state.auction_client``  is unset, so ``POST /buyer/intent/confirm`` answers **503**
  (:class:`~buyer_svc.intent.errors.AuctionClientUnusable`, "confirm() was given no auction
  client, so the confirmed intent has nowhere to go");
* ``app.state.exchange_client`` is unset, so ``POST /buyer/shortlist/accept`` answers
  **503** for the same reason.

A buyer service that boots and 503s every route that matters is a correct *deployment*
posture and a dead *service*. So this module is where a **deployment** states the one
collaborator this service has — the exchange — and it states it in configuration rather
than in code, because a composition root that has to be edited to deploy is not a
composition root:

``BUYER_DEPLOYMENT``
    Path to a JSON document (below).
``BUYER_DEPLOYMENT_JSON``
    The same document, inline — for a container that would rather set a variable than mount
    a file. ``BUYER_DEPLOYMENT`` wins if both are set.
``BUYER_UI_DIST``
    Optional. The directory of the built single-page UI, mounted at ``/`` **after** every
    router so an API path can never be shadowed by a file. Unset, or naming a directory that
    is not there, mounts nothing and never crashes the app.

Neither deployment variable set is **exactly today's behaviour**: nothing is wired, both
routes keep answering 503 out of their own handlers, and this module invents no base URL to
paper over it. That is the one property this file may not break — a buyer service nobody has
configured must refuse to reach an exchange rather than quietly guess at one.

The document
------------

.. code-block:: json

    {
      "exchange_base_url": "http://127.0.0.1:8123",
      "roster": [
        {"store_id": "s1", "tier": 1, "product_ref": "prod-1",
         "list_price": 100.0, "max_discount_pct": 20.0}
      ],
      "request_timeout_seconds": 30.0
    }

``exchange_base_url``
    Required, and required to be an ``http(s)`` URL with a host. There is no default: a
    defaulted exchange address is a buyer service that silently talks to the wrong exchange,
    which is worse than one that refuses to talk to any.
``roster``
    The platform's candidate set — **deployment data**, not something a browser may author.
    See :meth:`ExchangeHttpClient.create_auction`.
``request_timeout_seconds``
    Optional; how long one call to the exchange may take.

A document that is present and unusable raises :class:`DeploymentConfigurationError` naming
its source. It is never downgraded to "unconfigured": a missing file, a typo in the JSON, an
absent ``exchange_base_url`` and an ``ftp://`` address are all misconfigurations, and the
silent version of each produces a service that boots, answers 503, and looks like a policy
decision.

R3 — this module builds no checkout URL
---------------------------------------
Not "does not currently build one": there is no template, no ``urljoin`` onto a store host,
and no f-string with a merchant domain in it anywhere in this file. The only URLs constructed
here are the **exchange's own API paths** (``/auctions``, ``/auctions/{id}/accept``,
``/auctions/{id}/shortlist``) against the operator-supplied ``exchange_base_url``, which is
the client owning the exchange's routing exactly as ``buyer_svc.accept.handoff`` says it must
("constructing ``/auctions/{id}/accept`` here would be a second place that knows the
exchange's routing"). Where the buyer *checks out* is the exchange's answer, byte for byte,
and this module neither reads nor mints it.

When it runs
------------
``apps/buyer/svc/src/main.py`` is orchestrator-frozen and its ``create_app`` globs
``*/routes.py``, so this flat module is **not** auto-mounted and cannot be. :func:`create_app`
here calls ``main.create_app()`` and then wires it, which is why the deployable entrypoint is
``uvicorn buyer_svc.composition:app`` rather than ``buyer_svc.main:app``. Unlike the
exchange's composition root — which had to bind from inside a request hook because *its*
entrypoint was the frozen file — this one binds at app construction, so a broken document
fails the process at start-up instead of once per buyer.

:func:`configure_buyer` never overwrites an attribute somebody already set, so a test or a
devstack that wires its own client still wins and this module composes the published
``app.state`` seams rather than reaching past them.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.staticfiles import StaticFiles

from .accept._spellings import bind_spellings
from .accept.routes import EXCHANGE_CLIENT_ATTR
from .intent.routes import AUCTION_CLIENT_ATTR
from .main import create_app as create_base_app

__all__ = [
    "AUCTION_VIEW_PATH",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "ENV_DEPLOYMENT",
    "ENV_DEPLOYMENT_JSON",
    "ENV_UI_DIST",
    "MAX_DEPLOYMENT_BYTES",
    "MAX_RECORDED_AUCTIONS",
    "MAX_ROSTER_ENTRIES",
    "STATE_FLAG",
    "BuyerDeployment",
    "DeploymentConfigurationError",
    "ExchangeCallFailed",
    "ExchangeHttpClient",
    "app",
    "configure_buyer",
    "create_app",
    "ensure_configured",
    "mount_ui",
    "parse_deployment",
    "read_deployment",
]

#: Path to the deployment document.
ENV_DEPLOYMENT = "BUYER_DEPLOYMENT"
#: The same document, inline. ``ENV_DEPLOYMENT`` outranks it.
ENV_DEPLOYMENT_JSON = "BUYER_DEPLOYMENT_JSON"
#: Directory of the built UI. Optional; absent means "serve the API only".
ENV_UI_DIST = "BUYER_UI_DIST"

#: ``app.state`` flag saying this app has been through :func:`ensure_configured`.
STATE_FLAG = "buyer_composition"

#: The read-only view this module adds. Not a feature router: it exists to serve the
#: diagnostics the exchange does not re-serve, which only the client wired here holds.
AUCTION_VIEW_PATH = "/buyer/auctions/{auction_id}"

#: How long one call to the exchange may take, end to end.
#:
#: Generous on purpose: ``POST /auctions`` waits out the exchange's own R10 bid window
#: (``DEFAULT_BID_TIMEOUT_SECONDS``, 3.0s) plus a fan-out to every rostered store agent, and a
#: buyer whose confirmation times out at the buyer service has an auction running at the
#: exchange that they can no longer name.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

#: The most bytes a deployment document may occupy.
#:
#: Operator-supplied rather than attacker-supplied, so this is a guard against a mistake — a
#: log file or a tarball named where a config file was meant — rather than against an
#: adversary, which is why the number is generous. The read itself is bounded (see
#: :func:`read_deployment`); a cap applied after the bytes are in memory is not a cap.
MAX_DEPLOYMENT_BYTES = 4 * 1024 * 1024

#: The most roster rows a deployment may register.
#:
#: The same 500 as ``exchange.auction.routes.MAX_ROSTER_ENTRIES``, which is the ceiling the
#: exchange's own ``CreateAuctionRequest`` enforces. Refused here, once, at start-up rather
#: than as a 422 on every confirmation.
MAX_ROSTER_ENTRIES = 500

#: How many auctions' diagnostics one client keeps.
#:
#: Bounded because it is a per-process cache on a path a browser drives, and the ring is the
#: reason it is bounded rather than a TTL: 64 auctions is far more than one shopper's session
#: and the oldest is always the one nobody is looking at. See
#: :meth:`ExchangeHttpClient.create_auction` for why the record exists at all.
MAX_RECORDED_AUCTIONS = 64


class DeploymentConfigurationError(RuntimeError):
    """The deployment document is missing, unreadable, or says something unusable.

    Raised rather than warned, and named after the exchange's error of the same name for the
    same reason: a composition root that shrugs at a typo produces exactly the symptom this
    module exists to remove — a buyer service that boots and 503s.
    """


class ExchangeCallFailed(RuntimeError):
    """The exchange could not be reached, or answered something this client cannot use.

    Distinct from a *refusal*. A 409 on the accept path is the exchange deciding, and
    :meth:`ExchangeHttpClient.accept_offer` returns its body so
    ``buyer_svc.accept.handoff`` can turn the ``denial_reason`` into a refusal the buyer is
    shown. This exception is for the answers that carry no decision at all.
    """


# =====================================================================================
# The document
# =====================================================================================
@dataclass(frozen=True)
class BuyerDeployment:
    """A parsed, validated deployment document."""

    exchange_base_url: str
    roster: tuple[Mapping[str, Any], ...] = ()
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    #: Where this document came from, for the message a later failure prints. Last and
    #: defaulted so ``BuyerDeployment("http://...")`` — the spelling every caller uses —
    #: keeps working.
    source: str = ""


def _require_mapping(value: Any, what: str, source: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeploymentConfigurationError(
            f"{source}: {what} must be a JSON object, got {type(value).__name__}"
        )
    return value


def _exchange_base_url(raw: Any, source: str) -> str:
    """The exchange's address, or a refusal. Never a default."""
    if raw is None or not str(raw).strip():
        raise DeploymentConfigurationError(
            f"{source}: the deployment states no 'exchange_base_url'. There is no default "
            f"for it: a buyer service that guesses at an exchange address either talks to "
            f"nobody or talks to the wrong exchange, and both look like an empty shortlist"
        )
    url = str(raw).strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise DeploymentConfigurationError(
            f"{source}: exchange_base_url {str(raw)!r} is not an http(s) URL with a host. "
            f"This is the base every exchange call is built onto — POST /auctions, "
            f"POST /auctions/{{id}}/accept, GET /auctions/{{id}}/shortlist — so an address "
            f"httpx cannot dial is a service that fails on the first confirmation instead "
            f"of at start-up"
        )
    return url


def _roster(raw: Any, source: str) -> tuple[Mapping[str, Any], ...]:
    """The platform's candidate set, validated the way the exchange will read it."""
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise DeploymentConfigurationError(
            f"{source}: 'roster' must be a JSON array, got {type(raw).__name__}"
        )
    if len(raw) > MAX_ROSTER_ENTRIES:
        raise DeploymentConfigurationError(
            f"{source}: 'roster' names {len(raw)} stores; the exchange's own "
            f"CreateAuctionRequest accepts at most {MAX_ROSTER_ENTRIES}, so a longer one is "
            f"a 422 on every confirmation. Refused here, once, instead"
        )

    rows: list[Mapping[str, Any]] = []
    for index, entry in enumerate(raw):
        row = _require_mapping(entry, f"roster[{index}]", source)
        if not str(row.get("store_id") or "").strip():
            raise DeploymentConfigurationError(
                f"{source}: roster[{index}] names no store_id. The roster is the list of "
                f"stores the exchange solicits, and a row naming no store solicits nobody "
                f"while making the roster look one entry longer than it is"
            )
        rows.append(dict(row))
    return tuple(rows)


def _request_timeout(raw: Any, source: str) -> float:
    if raw is None:
        return DEFAULT_REQUEST_TIMEOUT_SECONDS
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise DeploymentConfigurationError(
            f"{source}: request_timeout_seconds must be a number, got {raw!r}"
        )
    timeout = float(raw)
    if not math.isfinite(timeout) or timeout <= 0:
        raise DeploymentConfigurationError(
            f"{source}: request_timeout_seconds is {raw!r}; it must be a positive, finite "
            f"number of seconds. A zero or negative timeout is not 'wait forever', it is a "
            f"call that fails before it is sent"
        )
    return timeout


def parse_deployment(document: Any, *, source: str) -> BuyerDeployment:
    """Validate one deployment document. Raises rather than degrading.

    ``source`` names the document in every message this raises — the path from
    ``BUYER_DEPLOYMENT``, or the variable name ``BUYER_DEPLOYMENT_JSON`` — because the one
    thing an operator needs from a configuration error is *which file*.
    """
    body = _require_mapping(document, "the deployment document", source)
    return BuyerDeployment(
        exchange_base_url=_exchange_base_url(body.get("exchange_base_url"), source),
        roster=_roster(body.get("roster"), source),
        request_timeout_seconds=_request_timeout(body.get("request_timeout_seconds"), source),
        source=source,
    )


def read_deployment(env: Mapping[str, str] | None = None) -> BuyerDeployment | None:
    """The configured deployment, or ``None`` when this buyer service was given none.

    ``None`` is returned for exactly one situation — neither variable is set — and never for
    a variable that is set to something unusable. A named-but-missing file is a
    misconfiguration, not an unconfigured service, and answering it with ``None`` would wire
    nothing and let the routes 503 with a message about a client nobody asked to be missing.
    """
    environ = os.environ if env is None else env

    path = str(environ.get(ENV_DEPLOYMENT) or "").strip()
    if path:
        source = f"{ENV_DEPLOYMENT}={path}"
        try:
            with Path(path).open("rb") as handle:
                # Bounded READ, not a bounded check afterwards: `read_text()` on a file
                # named by mistake (a log, a tarball) is already in memory by the time its
                # length can be looked at. One byte over the cap is enough to know.
                raw = handle.read(MAX_DEPLOYMENT_BYTES + 1)
        except OSError as exc:
            raise DeploymentConfigurationError(
                f"{source}: the deployment document could not be read "
                f"({exc.__class__.__name__}: {exc}). A named-but-missing file is a "
                f"misconfiguration, not an unconfigured buyer service, so it is refused "
                f"rather than answered fail-closed"
            ) from exc
        if len(raw) > MAX_DEPLOYMENT_BYTES:
            raise DeploymentConfigurationError(
                f"{source}: the deployment document is larger than {MAX_DEPLOYMENT_BYTES} "
                f"bytes. This is a small configuration file; something else is at that path"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DeploymentConfigurationError(
                f"{source}: the deployment document is not UTF-8 text ({exc})"
            ) from exc
    else:
        inline = str(environ.get(ENV_DEPLOYMENT_JSON) or "").strip()
        if not inline:
            return None
        source = ENV_DEPLOYMENT_JSON
        if len(inline) > MAX_DEPLOYMENT_BYTES:
            raise DeploymentConfigurationError(
                f"{source}: the inline deployment document is {len(inline)} bytes; at most "
                f"{MAX_DEPLOYMENT_BYTES} are read"
            )
        text = inline

    try:
        document = json.loads(text)
    except Exception as exc:
        # NOT `except ValueError`, for the reason `exchange.composition.read_deployment`
        # records: `json.loads` on deeply nested input raises RecursionError, which is not a
        # ValueError and would escape this function as a 500 rather than as the named
        # configuration error this module documents.
        raise DeploymentConfigurationError(
            f"{source}: not valid JSON ({type(exc).__name__}: {exc})"
        ) from exc
    return parse_deployment(document, source=source)


# =====================================================================================
# The one outbound client
# =====================================================================================
@dataclass(frozen=True)
class _AuctionRecord:
    """One auction's ``POST /auctions`` answer, kept verbatim beside our own timestamp."""

    auction_id: str
    recorded_at: str
    response: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "auction_id": self.auction_id,
            "recorded_at": self.recorded_at,
            "response": dict(self.response),
        }


class ExchangeHttpClient:
    """The buyer's ONE client onto the exchange — auctions, accepts and shortlists.

    One object behind both ``app.state`` attributes, and that is the point rather than an
    economy: ``buyer_svc.intent.confirmation`` reads ``auction_client`` and
    ``buyer_svc.accept.handoff`` reads ``exchange_client``, and two clients would be two
    opinions about which exchange this service is talking to — with the diagnostics recorded
    on one of them and looked up on the other.

    It exposes ``create_auction`` (first in
    :data:`~buyer_svc.intent.confirmation.AUCTION_CLIENT_METHODS`) and ``accept_offer``
    (first in :data:`~buyer_svc.accept.handoff.EXCHANGE_ACCEPT_METHODS`), so each package
    resolves its entrypoint on the first name it looks for.

    Nothing here builds a checkout URL (R3). See this module's docstring.
    """

    def __init__(
        self,
        base_url: str,
        *,
        roster: Sequence[Mapping[str, Any]] = (),
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        client: Any | None = None,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self._roster: tuple[dict[str, Any], ...] = tuple(dict(row) for row in roster)
        self._timeout = float(timeout)
        self._client = client
        self._records: OrderedDict[str, _AuctionRecord] = OrderedDict()
        self._lock = threading.Lock()

    # -- the two ports ---------------------------------------------------------------
    def create_auction(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """``POST {base}/auctions`` — R1's one side effect, and the only place diagnostics exist.

        The deployment's roster is filled in **only when the caller's payload carries none**.
        The platform's candidate set is deployment data: which stores are solicited is not a
        question the browser gets to answer, and ``POST /buyer/intent/confirm`` accepts a
        ``roster`` field straight off the wire. A payload that names its own roster is
        honoured unchanged (that is how a test, or a devstack, drives a different candidate
        set); a payload whose roster is absent, ``null`` **or empty** gets the deployment's,
        because "solicit nobody" is not a statement a browser is entitled to make and an
        empty roster is what an unconfigured client would send.

        The whole answer is recorded under its ``auction_id``. That record is not a cache —
        it is the **only** surviving copy of the exchange's ``entries`` / ``excluded`` /
        ``denied`` / ``ranked`` diagnostics, because ``GET /auctions/{id}`` on the exchange
        answers with auction *state* and ``GET /auctions/{id}/shortlist`` with the shortlist
        alone. Without it, a buyer whose shortlist came back empty cannot be told which
        stores were asked, which declined, and why — which is the failure this whole service
        is about.
        """
        body = dict(payload)
        if not body.get("roster") and self._roster:
            body["roster"] = [dict(row) for row in self._roster]

        answer = self._json(
            self._request("POST", "/auctions", json=body),
            expected=(200, 201),
            what="POST /auctions",
        )
        auction_id = str(answer.get("auction_id") or "").strip()
        if auction_id:
            self._record(auction_id, answer)
        return answer

    def accept_offer(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """``POST {base}/auctions/{auction_id}/accept`` with a body of exactly ``{"bid_ref"}``.

        ``handoff.accept`` calls this with one positional mapping carrying ``auction_id`` and
        ``bid_ref``; the id rides in the body because that module must not know the
        exchange's routing. This client does, so the id goes in the **path** and the body
        carries the bid alone — the exchange's ``AcceptRequest`` is ``extra="forbid"`` and an
        ``auction_id`` echoed into the body is a 422, not a redundancy.

        A **409 is returned, not raised**: the exchange's denial body
        ``{"accepted": false, "denial_reason": ...}`` is a decision, and
        ``handoff._refuse_if_denied`` is the one place that turns it into a refusal the buyer
        is shown. Every other non-200 raises :class:`ExchangeCallFailed`.
        """
        auction_id = str(payload.get("auction_id") or "").strip()
        bid_ref = str(payload.get("bid_ref") or "").strip()
        if not auction_id or not bid_ref:
            raise ExchangeCallFailed(
                f"accept_offer needs both an auction_id and a bid_ref; got "
                f"{{'auction_id': {auction_id!r}, 'bid_ref': {bid_ref!r}}}"
            )
        response = self._request(
            "POST",
            f"/auctions/{quote(auction_id, safe='')}/accept",
            json={"bid_ref": bid_ref},
        )
        return self._json(
            response,
            expected=(200, status.HTTP_409_CONFLICT),
            what=f"POST /auctions/{auction_id}/accept",
        )

    # -- the two reads ---------------------------------------------------------------
    def outcome_for(self, auction_id: str) -> Mapping[str, Any] | None:
        """This client's record of what the exchange answered when it opened the auction.

        ``{"auction_id", "recorded_at", "response"}``, where ``response`` is the exchange's
        body **verbatim**. The envelope keeps our timestamp out of the exchange's document:
        a ``recorded_at`` merged into their answer would read as something the exchange said.
        """
        with self._lock:
            record = self._records.get(str(auction_id))
        return None if record is None else record.to_dict()

    def shortlist_for(self, auction_id: str) -> Mapping[str, Any] | None:
        """``GET {base}/auctions/{id}/shortlist`` — live, or ``None`` if the exchange 404s.

        ``None`` rather than an empty shortlist, because the exchange draws that distinction
        deliberately: an auction whose every candidate was excluded has a real shortlist with
        no slots, and it is not the same answer as an auction this exchange has forgotten
        (its 15-minute TTL, or eviction after 512 more recent auctions).
        """
        response = self._request("GET", f"/auctions/{quote(str(auction_id), safe='')}/shortlist")
        if response.status_code == status.HTTP_404_NOT_FOUND:
            return None
        return self._json(response, expected=(200,), what=f"GET /auctions/{auction_id}/shortlist")

    # -- plumbing --------------------------------------------------------------------
    def _record(self, auction_id: str, answer: Mapping[str, Any]) -> None:
        """Keep this answer, evicting the oldest once the ring is full."""
        record = _AuctionRecord(
            auction_id=auction_id,
            recorded_at=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            response=dict(answer),
        )
        with self._lock:
            self._records.pop(auction_id, None)
            self._records[auction_id] = record
            while len(self._records) > MAX_RECORDED_AUCTIONS:
                self._records.popitem(last=False)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return self._http().request(method, f"{self.base_url}{path}", **kwargs)
        except ExchangeCallFailed:
            raise
        except Exception as exc:
            raise ExchangeCallFailed(
                f"{method} {self.base_url}{path} could not be completed "
                f"({type(exc).__name__}: {exc})"
            ) from exc

    def _json(self, response: Any, *, expected: tuple[int, ...], what: str) -> Mapping[str, Any]:
        status_code = int(response.status_code)
        if status_code not in expected:
            raise ExchangeCallFailed(
                f"the exchange answered {what} with HTTP {status_code} "
                f"(expected {list(expected)}): {_excerpt(response)}"
            )
        try:
            body = response.json()
        except Exception as exc:
            raise ExchangeCallFailed(
                f"the exchange answered {what} with HTTP {status_code} and a body this "
                f"client cannot parse ({type(exc).__name__}: {exc}): {_excerpt(response)}"
            ) from exc
        if not isinstance(body, Mapping):
            raise ExchangeCallFailed(
                f"the exchange answered {what} with a JSON {type(body).__name__} rather "
                f"than an object: {body!r}"
            )
        return body

    def _http(self) -> Any:
        """One pooled client for this object, built on first use.

        Deferred rather than built in ``__init__`` so that constructing this class — which a
        configuration check, or a test, may do — opens no sockets, and so ``httpx`` is
        imported only by a deployment that actually reaches out.
        """
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self._timeout)
        return self._client


def _excerpt(response: Any, limit: int = 400) -> str:
    """A short, safe rendering of a response body for an error message."""
    try:
        text = str(response.text)
    except Exception:  # noqa: BLE001 - a body we cannot even render is still an error
        return "<unreadable body>"
    return text if len(text) <= limit else f"{text[:limit]}..."


# =====================================================================================
# Binding
# =====================================================================================
def configure_buyer(app: FastAPI, deployment: BuyerDeployment) -> tuple[str, ...]:
    """Bind what ``deployment`` states, and nothing this app has already been given.

    Returns the ``app.state`` attribute names it bound, so a caller can say what a deployment
    actually turned on — and so an empty tuple is a visible answer rather than a silent one.

    It never overwrites: a devstack, an e2e harness or a test that set its own
    ``auction_client`` still wins, exactly as ``exchange.composition.configure_exchange``
    leaves wiring a deployment already chose.
    """
    client = ExchangeHttpClient(
        deployment.exchange_base_url,
        roster=deployment.roster,
        timeout=deployment.request_timeout_seconds,
    )
    bound: list[str] = []
    for attribute in (AUCTION_CLIENT_ATTR, EXCHANGE_CLIENT_ATTR):
        if getattr(app.state, attribute, None) is None:
            setattr(app.state, attribute, client)
            bound.append(attribute)
    return tuple(bound)


def ensure_configured(app: FastAPI, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Bind this app's deployment once. Idempotent, and a no-op when none is configured.

    The guard lives on ``app.state``, so two apps in one process — which is every test module
    in this repository — are configured independently. Only a *successful* bind is
    remembered: a malformed document raises every time it is asked for, so an operator who
    fixes it is served by the next call rather than by the next process.
    """
    already = getattr(app.state, STATE_FLAG, None)
    if already is not None:
        return tuple(already)
    deployment = read_deployment(env)
    if deployment is None:
        # Fail closed, and say nothing else. Both routes answer 503 out of their own
        # handlers with a message naming the client they were not given, which is a better
        # error than anything this module could invent a base URL to avoid.
        return ()
    bound = configure_buyer(app, deployment)
    setattr(app.state, STATE_FLAG, bound)
    return bound


def configure_auction_view(app: FastAPI) -> None:
    """Add ``GET /buyer/auctions/{auction_id}``: the live shortlist plus the recorded run.

    It lives here rather than in a feature router because it is the composition root's own
    view: the diagnostics it serves exist only on the client this module wired, and a router
    under ``intent/`` or ``accept/`` would either duplicate that record or reach for a client
    it has no business owning.

    The two halves are kept apart on purpose. ``shortlist`` is fetched from the exchange on
    every request and is what the exchange says *now*; ``entries``/``excluded``/``denied``/
    ``ranked``/``solicited`` come from the answer this service recorded when the auction was
    opened, and ``recorded_at`` says when. Nothing is merged, nothing is derived from the
    other, and a missing half is ``null`` rather than an empty list pretending to be an
    answer.
    """

    @app.get(AUCTION_VIEW_PATH, tags=["buyer-auctions"])
    async def read_auction(auction_id: str, request: Request) -> dict[str, Any]:
        """One auction as this buyer service can honestly describe it."""
        client = getattr(request.app.state, EXCHANGE_CLIENT_ATTR, None)
        if client is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"this buyer service has no exchange client, so it cannot say anything "
                    f"about auction {auction_id!r}. Point {ENV_DEPLOYMENT} at a deployment "
                    f"document naming an exchange_base_url"
                ),
            )

        recorded = client.outcome_for(auction_id) if hasattr(client, "outcome_for") else None
        try:
            shortlist = (
                client.shortlist_for(auction_id) if hasattr(client, "shortlist_for") else None
            )
        except ExchangeCallFailed as exc:
            # 502: this request was fine and the upstream's answer was not. A 500 would
            # blame this service for the exchange's reply.
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        if recorded is None and shortlist is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"no auction {auction_id!r}: this buyer service holds no record of "
                    f"opening it, and the exchange has no shortlist for it either"
                ),
            )

        answer: Mapping[str, Any] = {}
        if recorded is not None:
            candidate = recorded.get("response")
            if isinstance(candidate, Mapping):
                answer = candidate
        return {
            "auction_id": auction_id,
            "shortlist": dict(shortlist) if shortlist is not None else None,
            "entries": _rows(answer.get("entries")),
            "excluded": _rows(answer.get("excluded")),
            "denied": _rows(answer.get("denied")),
            "ranked": _rows(answer.get("ranked")),
            "solicited": _rows(answer.get("solicited")),
            "recorded_at": recorded.get("recorded_at") if recorded is not None else None,
        }


def _rows(value: Any) -> list[Any]:
    """A JSON array off the recorded answer, or ``[]`` when it carried none."""
    if isinstance(value, list):
        return list(value)
    return []


def mount_ui(app: FastAPI, env: Mapping[str, str] | None = None) -> str | None:
    """Mount the built UI at ``/`` when ``BUYER_UI_DIST`` names a real directory.

    Returns the directory that was mounted, or ``None``. Called LAST, after every router, so
    an API path can never be shadowed by a file that happens to share its name — Starlette
    matches routes in registration order and a ``Mount`` at ``/`` matches everything.

    A missing, blank or nonexistent ``BUYER_UI_DIST`` mounts nothing and is not an error: the
    API is the service and the UI is a build artefact that may simply not have been built
    yet. That is the one place in this module where silence is right, and it is why the check
    is ``is_dir()`` rather than letting ``StaticFiles`` raise.
    """
    environ = os.environ if env is None else env
    directory = str(environ.get(ENV_UI_DIST) or "").strip()
    if not directory:
        return None
    try:
        if not Path(directory).is_dir():
            return None
        app.mount("/", StaticFiles(directory=directory, html=True), name="buyer-ui")
    except OSError:
        # An unreadable path, a broken symlink, a permission error: the UI is not there. A
        # buyer service that will not start because a static directory is odd is worse than
        # one that serves its API.
        return None
    return directory


def create_app(env: Mapping[str, str] | None = None) -> FastAPI:
    """The deployable buyer service: the frozen app, wired, plus the UI.

    ``uvicorn buyer_svc.composition:app``. In order, and the order is load-bearing:

    1. ``buyer_svc.main.create_app()`` — the frozen entrypoint, which globs and mounts every
       ``<feature>/routes.py``. This module is flat, so it is not one of them and cannot
       mount itself by accident;
    2. :func:`ensure_configured` — the deployment's client, or nothing at all;
    3. :func:`configure_auction_view` — the read-only view over what step 2 recorded;
    4. :func:`mount_ui` — LAST, so every route above wins over the static mount.

    A broken deployment document raises out of step 2, which fails the process at start-up.
    That is deliberate: the alternative is a service that boots, answers 503 to every buyer,
    and gives the operator no reason why.
    """
    app = create_base_app()
    ensure_configured(app, env)
    configure_auction_view(app)
    mount_ui(app, env)
    return app


#: The module-level application ``uvicorn buyer_svc.composition:app`` serves.
app = create_app()

# This module is reachable under both dotted spellings of `apps/buyer/svc/src` (see
# `accept/_spellings.py`). Left alone, Python executes it a SECOND time under the second
# name, which builds a second `app` — a whole second FastAPI application, with its own
# wiring and its own diagnostics ring, for a service that has exactly one.
bind_spellings(sys.modules[__name__])
