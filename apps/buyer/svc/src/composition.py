"""The composition root: the collaborators a *deployed* buyer service binds at start-up.

Every exchange-facing part of this service is injected, and every default is fail-closed.
That is the right shape for the components — and, until this module existed, it was also the
reason the shopper's confirmation had nowhere to go. ``uvicorn buyer_svc.main:app`` boots a
buyer service with

* ``auction_client``   -> unset, so ``POST /buyer/intent/confirm`` refuses **503**;
* ``exchange_client``  -> unset, so ``POST /buyer/shortlist/accept`` refuses **503**.

Measured against the real services over loopback, on the app exactly as ``create_app()``
builds it, before this module existed::

    >> POST http://127.0.0.1:60270/buyer/intent/confirm  -> 503
    {"detail": "confirm() was given no auction client, so the confirmed intent has nowhere
                to go. Pass the exchange client that owns POST /auctions."}

Two fail-closed defaults are a correct *deployment* posture and a dead *journey*: the buyer
can be asked three questions, can be shown a structured intent, can press the confirm button
— and the auction that the whole rest of the platform exists to run is never opened. Every
green demonstration of ``confirm`` in this repository supplies the join whose absence is the
defect, by handing ``confirm()`` a client the test itself wrote. A test that wires the app it
is testing is measuring the wiring it wrote.

So this module is the place a **deployment** states its collaborators, and it states them in
configuration rather than in code, because a composition root that has to be edited to deploy
is not a composition root. It is deliberately the same shape as
``apps/exchange/src/composition.py``, down to the variable names, so an operator configuring
both halves of the buyer↔exchange seam learns one convention:

``BUYER_DEPLOYMENT``
    Path to a JSON document (below).
``BUYER_DEPLOYMENT_JSON``
    The same document, inline — for a container that would rather set a variable than mount a
    file. ``BUYER_DEPLOYMENT`` wins if both are set.
``EXCHANGE_URL``
    Just the exchange's origin, lowest precedence of the three, and it is here because the
    repository's own deployment **already sets it**. ``apps/buyer/compose.yaml:47`` carries::

        EXCHANGE_URL: "${EXCHANGE_URL:-http://exchange:8083}"
        # The exchange is reached by service name inside the network, never by localhost.

    while ``grep -rn EXCHANGE_URL --include='*.py'`` over this repo returns **nothing**. The
    deploy lane declared the address and said what it was for; no line of code had ever read
    it. Reading it is the difference between a correct composition root and one the shipped
    ``docker compose up`` cannot reach: with it, the stack in ``docs/deploy.md`` carries a
    confirmed intent to the exchange with no operator action at all.

None of the three set is **exactly today's behaviour**: nothing is bound, both defaults above stand, and
both routes answer 503 with the message they answer today. That is deliberate and it is the
one property this module may not break — a buyer service nobody has configured must not
quietly invent an exchange to send a confirmed intent to.

The document
------------

.. code-block:: json

    {
      "exchange_url": "http://exchange:8083",
      "roster": [{"store_id": "demo-woolworks", "tier": 1, "product_ref": "beanie-1"}],
      "request_timeout_seconds": 15.0
    }

``exchange_url``
    The exchange's own origin — the service that owns ``POST /auctions`` and
    ``POST /auctions/{auction_id}/accept``. **Required**, because a deployment document that
    names no exchange binds nothing and is therefore indistinguishable from no document at
    all, which is the exact silent failure this module exists to remove.

    ONE url, feeding ONE client object, bound to BOTH seams. Two urls would be two opinions
    about where the exchange is, and the buyer would then be able to open an auction on one
    exchange and accept an offer on another — a shortlist whose auction the accepting process
    has never heard of.
``roster``
    Optional. The platform's candidate set — the stores the exchange is asked to solicit.
    It is **deployment data**, and that is the whole reason it is here rather than left to
    the caller: ``POST /buyer/intent/confirm`` takes a ``ConfirmBody.roster`` straight off
    the wire, so without this field a browser is the author of the list of merchants that
    compete for its own buyer. :meth:`HttpExchangeClient.create_auction` fills it in only
    when the caller's payload carries none, so a test or a devstack that means to drive a
    different candidate set still can. A document with no ``roster`` key is legal and
    changes nothing: an auction is opened with whatever roster the caller sent, exactly as
    before this field existed.
``request_timeout_seconds``
    Optional; how long one call to the exchange may take. Defaults to
    :data:`DEFAULT_EXCHANGE_TIMEOUT_SECONDS`.

Validation is loud, and every rule below was chosen because the silent version of it produces
a 503 or a 502 that looks like the exchange's fault:

* a document naming no ``exchange_url`` is refused rather than binding nothing;
* an ``exchange_url`` with no scheme is refused — ``urlsplit("exchange:8083").hostname`` is
  ``None``, so the request would be built against a URL with no host in it. This is the mirror
  image of the exchange's own ``registered_domain`` rule, which refuses a bare host that
  *carries* a scheme, and for the same reason: one field, one shape, refused where the operator
  can still see their typo;
* a query string or a fragment is refused — this module appends ``/auctions`` to what it is
  given, and ``http://exchange:8083/?x=1`` would silently become ``http://exchange:8083/?x=1/auctions``;
* a ``request_timeout_seconds`` that is not a positive, finite number is refused, ``True``
  included: ``isinstance(True, int)`` is ``True`` in Python, so ``{"request_timeout_seconds":
  true}`` would otherwise configure a one-second exchange timeout out of a boolean;
* a ``roster`` that is not a list of objects each naming a non-empty ``store_id`` is
  refused, and one longer than :data:`MAX_ROSTER_ENTRIES` is refused here rather than as a
  422 on every confirmation. A row naming no store solicits nobody while making the roster
  look one entry longer than it is;
* an unrecognised key is refused, naming it. The exchange's document has five keys and a typo
  in one still leaves four working; every key of this one is load-bearing, so
  ``{"exchange_uri": ...}`` is a deployment that reads as configured and behaves as
  unconfigured — and a misspelt ``roster`` is one that solicits nobody.

A malformed document raises :class:`DeploymentConfigurationError`, which the two routes turn
into a **503 naming the problem**, never a 500: a buyer service told to read a deployment it
cannot read is misconfigured, and that is a different thing from a buyer service nobody has
configured.

What the client remembers, and why it has to
--------------------------------------------
:meth:`HttpExchangeClient.create_auction` keeps the exchange's whole ``POST /auctions``
answer in a bounded ring (:data:`MAX_RECORDED_AUCTIONS`), keyed by the ``auction_id`` in
that answer. That record is not a cache — it is the only surviving copy of the exchange's
``entries`` / ``excluded`` / ``denied`` / ``ranked`` / ``solicited`` diagnostics. Measured on
this branch, ``apps/exchange/src/auction/routes.py`` at ``read_auction``::

    @router.get("/auctions/{auction_id}")
    async def read_auction(auction_id: str, request: Request) -> dict[str, Any]:
        # "The auction's current state - 404 once its 15-minute TTL has taken it away."
        ...
        return {"auction_id": ..., "state": ..., "intent_id": ..., "cluster_id": ...,
                "accepted_bid_ref": ..., "history": ...}

— state, and nothing else. No ``entries``, no ``excluded``, no ``denied``, no ``ranked``. So
"why is my shortlist empty" is answerable exactly once, in the body of the answer to the
call that opened the auction, and if this client does not keep it the answer survives
nowhere. :meth:`~HttpExchangeClient.outcome_for` hands that record back and
:meth:`~HttpExchangeClient.shortlist_for` fetches the shortlist **live**;
``buyer_svc.auctions.routes`` serves the two side by side without ever merging them.

The built UI
------------
``BUYER_UI_DIST`` names a directory of built static files, and :func:`mount_ui` mounts it at
``/``. It is **opt-in and manual**: nothing in :func:`configure_buyer` or
:func:`ensure_configured` calls it, because the shipped API image should not serve a demo
page, and a request-time hook that mounted a route would be mutating the router from inside a
request. ``apps/buyer/devstack/run.py`` calls it — after ``main.create_app()`` has mounted
every feature router, so a ``Mount`` at ``/`` cannot shadow an API path above it.

When it runs
------------
``apps/buyer/svc/src/main.py`` is orchestrator-frozen (B6(iii)), so this module cannot be
called from ``create_app``. :func:`ensure_configured` is called instead at the top of the two
routes that need it, and binds **once per app** — a request-time start-up hook, in the same
place and for the same reason ``apps/exchange/src/composition.py`` takes one.

It never overwrites anything already on ``app.state``, so a deployment (or a test) that sets
``auction_client`` / ``exchange_client`` itself still wins, and ``POST /buyer/intent/clarify``
and ``POST /buyer/shortlist/render`` do not take the hook at all — R1 and R2's "this handler
cannot reach a client" property is a statement about scope, and a composition hook in those
functions would be a client in their scope.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

_log = logging.getLogger(__name__)

__all__ = [
    "AUCTION_CLIENT_ATTR",
    "DEFAULT_EXCHANGE_TIMEOUT_SECONDS",
    "DOCUMENT_KEYS",
    "ENV_DEPLOYMENT",
    "ENV_DEPLOYMENT_JSON",
    "ENV_EXCHANGE_URL",
    "ENV_UI_DIST",
    "EXCHANGE_CLIENT_ATTR",
    "MAX_DEPLOYMENT_BYTES",
    "MAX_EXCHANGE_RESPONSE_BYTES",
    "MAX_EXCHANGE_WALL_CLOCK_SECONDS",
    "MAX_RECORDED_AUCTIONS",
    "MAX_ROSTER_ENTRIES",
    "STATE_FLAG",
    "Deployment",
    "DeploymentConfigurationError",
    "ExchangeCallFailed",
    "HttpExchangeClient",
    "configure_buyer",
    "ensure_configured",
    "mount_ui",
    "parse_deployment",
    "read_deployment",
]

#: Path to the deployment document.
ENV_DEPLOYMENT = "BUYER_DEPLOYMENT"
#: The same document, inline. ``ENV_DEPLOYMENT`` outranks it.
ENV_DEPLOYMENT_JSON = "BUYER_DEPLOYMENT_JSON"
#: Just the exchange's origin, with no document around it — the variable the compose fragment
#: already sets and nothing has ever read. Lowest precedence of the three.
ENV_EXCHANGE_URL = "EXCHANGE_URL"
#: Directory of the built UI. Optional, read by :func:`mount_ui` alone, and read by nothing
#: on the request path — an API deployment that never calls that function never looks at it.
ENV_UI_DIST = "BUYER_UI_DIST"

#: ``app.state`` flag saying this app has been through :func:`ensure_configured`.
STATE_FLAG = "buyer_composition"

#: Where the two exchange-facing clients live on the app.
#:
#: Spelled here rather than imported from the route modules, which import THIS module inside
#: their request hook — importing them back at module scope would be a cycle, and importing
#: them lazily just to read two string constants would drag FastAPI into every consumer of
#: :func:`read_deployment`. ``tests/test_composition_wiring.py`` asserts these are the same
#: strings the routes read, so the duplication is pinned rather than trusted.
AUCTION_CLIENT_ATTR = "auction_client"
EXCHANGE_CLIENT_ATTR = "exchange_client"

#: Every key this document may carry. An unrecognised key is refused against this set.
DOCUMENT_KEYS = frozenset({"exchange_url", "roster", "request_timeout_seconds"})

#: The most roster rows a deployment may register.
#:
#: The same 500 the exchange's own ``CreateAuctionRequest`` enforces
#: (``exchange.auction.routes.MAX_ROSTER_ENTRIES``). Refused here, once, when the document is
#: read, rather than as a 422 on every confirmation from a service that looks configured.
MAX_ROSTER_ENTRIES = 500

#: How many auctions' ``POST /auctions`` answers one client keeps.
#:
#: Bounded because it is a per-process record on a path a browser drives, and a ring rather
#: than a TTL because the oldest entry is always the one nobody is looking at. 64 is far more
#: than one shopper's session. See :meth:`HttpExchangeClient.create_auction` for why the
#: record exists at all — the exchange re-serves those diagnostics from no other door.
MAX_RECORDED_AUCTIONS = 64

#: How long one call to the exchange may take, when the document does not say.
#:
#: The exchange's ``POST /auctions`` holds the request open for R10's whole bid window while
#: it fans out to stores (``DEFAULT_BID_TIMEOUT_SECONDS`` is 3.0s, and a caller may ask for
#: more), so a buyer-side timeout tuned to a normal JSON round trip would abandon healthy
#: auctions. This is deliberately longer than any window the buyer service itself asks for.
DEFAULT_EXCHANGE_TIMEOUT_SECONDS = 15.0

#: The most bytes the exchange's answer to one call may occupy.
#:
#: A memory bound on the *response* side, in the same house style as
#: ``exchange.composition.MAX_BID_RESPONSE_BYTES``, and sized against a measured body rather
#: than a feared one. Against a real ``uvicorn exchange.main:app`` over loopback on this
#: branch::
#:
#:     POST /auctions, no roster        -> 201, 229 bytes
#:     POST /auctions, one-store roster -> 201, 331 bytes
#:
#: The answer grows with the roster — the exchange caps its own at ``MAX_ROSTER_ENTRIES =
#: 500`` — and with the exclusion reasons it quotes back per candidate, so the ceiling is set
#: four orders of magnitude above what was observed and is still a ceiling.
#:
#: Refused rather than truncated: half a JSON document is not an auction receipt, and a buyer
#: told "the exchange answered something too large to read" can retry, while one handed a
#: truncated parse would be told the exchange returned no ``auction_id``.
MAX_EXCHANGE_RESPONSE_BYTES = 4 * 1024 * 1024

#: The wall-clock ceiling on ONE call to the exchange, from the request leaving to its last
#: byte arriving.
#:
#: Separate from the httpx timeout, which is **per read** — it resets on every chunk, so it is
#: not a deadline at all and an exchange (or anything answering on its port) dripping one byte
#: per timeout-interval holds this worker open indefinitely. Same argument, same numbers and
#: same shape as ``exchange.composition.MAX_SOLICIT_WALL_CLOCK_SECONDS``.
MAX_EXCHANGE_WALL_CLOCK_SECONDS = 30.0

#: The most bytes a deployment document may occupy.
#:
#: The document is parsed on the REQUEST path (``main.py`` is frozen, so the composition root
#: runs as a request-time start-up hook), which makes its size time a buyer waits, and failures
#: are deliberately not cached so a large malformed document re-parses on every request. This
#: document carries one url and one number; 64 KiB is a guard against an operator's mistake —
#: a mounted file that is not the file they meant — rather than against an adversary.
MAX_DEPLOYMENT_BYTES = 64 * 1024


class DeploymentConfigurationError(RuntimeError):
    """The deployment document is missing, unreadable, or says something unusable.

    Raised rather than warned. A composition root that shrugs at a typo produces exactly the
    symptom this module exists to remove: a buyer service that boots, answers every clarify,
    and refuses every confirmation.
    """


class ExchangeCallFailed(RuntimeError):
    """The exchange could not be reached, or answered something that is not an answer.

    Deliberately **not** a :class:`~buyer_svc.intent.errors.IntentError` or an
    :class:`~buyer_svc.accept.errors.AcceptError`: those trees are domain refusals — "the
    buyer has not confirmed", "the exchange denied this bid" — and this is neither. It is the
    upstream failing, which the routes answer ``502``. Folding it into a domain tree would
    tell a buyer that their request was wrong when the request was fine.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        #: The exchange's HTTP status, when there was one. ``None`` means the call never
        #: completed — a refused connection, a timeout, a body that would not parse.
        self.status_code = status_code


# =====================================================================================
# The document
# =====================================================================================
@dataclass(frozen=True)
class Deployment:
    """A parsed, validated deployment document."""

    source: str
    exchange_url: str
    #: The platform's candidate set, as the deployment states it. Empty when the document
    #: named none, which is a legal document and means "send whatever roster the caller sent".
    roster: tuple[Mapping[str, Any], ...] = ()
    request_timeout_seconds: float = DEFAULT_EXCHANGE_TIMEOUT_SECONDS


def _require_mapping(value: Any, what: str, source: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeploymentConfigurationError(
            f"{source}: {what} must be a JSON object, got {type(value).__name__}"
        )
    return value


def _exchange_url(raw: Any, source: str) -> str:
    """The exchange origin, or a refusal naming what is wrong with it."""
    if not isinstance(raw, str):
        raise DeploymentConfigurationError(
            f"{source}: exchange_url must be a string, got {type(raw).__name__} ({raw!r})"
        )
    url = raw.strip()
    if not url:
        raise DeploymentConfigurationError(
            f"{source}: exchange_url is empty. This is the origin of the service that owns "
            f"POST /auctions; a buyer service with nowhere to send a confirmed intent is "
            f"the state this document exists to leave."
        )

    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise DeploymentConfigurationError(
            f"{source}: exchange_url {url!r} states scheme {parts.scheme!r}; it must be "
            f"http or https. A value with no scheme has no host either — "
            f"urlsplit({url!r}).hostname is {parts.hostname!r} — so every call to the "
            f"exchange would be built against a URL naming no server at all."
        )
    if not parts.hostname:
        raise DeploymentConfigurationError(
            f"{source}: exchange_url {url!r} names no host; there is nothing to connect to."
        )
    if parts.query or parts.fragment:
        raise DeploymentConfigurationError(
            f"{source}: exchange_url {url!r} carries a "
            f"{'query string' if parts.query else 'fragment'}. This value has '/auctions' "
            f"appended to it, so the query would end up in the middle of the path and the "
            f"request would go somewhere nobody serves."
        )
    # Trailing slash removed once, here, so `f"{base}/auctions"` is the only join anywhere in
    # this module and it cannot produce `//auctions`.
    return url.rstrip("/")


def _roster(raw: Any, source: str) -> tuple[Mapping[str, Any], ...]:
    """The platform's candidate set, validated the way the exchange will read it.

    Refused rather than filtered, and refused naming the source, exactly as every other
    validator here does: a roster row the buyer service quietly dropped is a store the
    operator believes is competing and which is never asked.
    """
    if raw is None:
        return ()
    # `str`/`bytes` are Sequences. A roster spelled as a string is a typo, not a one-store
    # roster whose rows are characters.
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise DeploymentConfigurationError(
            f"{source}: roster must be a JSON array of store objects, got "
            f"{type(raw).__name__} ({raw!r})"
        )
    if len(raw) > MAX_ROSTER_ENTRIES:
        raise DeploymentConfigurationError(
            f"{source}: roster names {len(raw)} stores; the exchange's own "
            f"CreateAuctionRequest accepts at most {MAX_ROSTER_ENTRIES}, so a longer one is "
            f"a 422 on every confirmation. Refused here, once, instead."
        )

    rows: list[Mapping[str, Any]] = []
    for index, entry in enumerate(raw):
        row = _require_mapping(entry, f"roster[{index}]", source)
        if not str(row.get("store_id") or "").strip():
            raise DeploymentConfigurationError(
                f"{source}: roster[{index}] names no store_id. The roster is the list of "
                f"stores the exchange solicits, and a row naming no store solicits nobody "
                f"while making the roster look one entry longer than it is."
            )
        rows.append(dict(row))
    return tuple(rows)


def _timeout_seconds(raw: Any, source: str) -> float:
    """A positive, finite call timeout, or a refusal."""
    # `bool` FIRST: `isinstance(True, int)` is True, so `{"request_timeout_seconds": true}`
    # would otherwise pass every numeric check below and configure a 1.0s timeout out of a
    # value that is not a duration.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise DeploymentConfigurationError(
            f"{source}: request_timeout_seconds must be a number of seconds, got "
            f"{type(raw).__name__} ({raw!r})"
        )
    seconds = float(raw)
    if not math.isfinite(seconds) or seconds <= 0:
        raise DeploymentConfigurationError(
            f"{source}: request_timeout_seconds is {raw!r}; it must be a positive, finite "
            f"number of seconds. A zero or negative timeout abandons every call to the "
            f"exchange before it is made, which reads to a buyer exactly like an exchange "
            f"that is down."
        )
    return seconds


def parse_deployment(document: Any, *, source: str) -> Deployment:
    """Validate one deployment document. Raises rather than degrading."""
    body = _require_mapping(document, "the deployment document", source)

    unknown = sorted(str(key) for key in body if str(key) not in DOCUMENT_KEYS)
    if unknown:
        raise DeploymentConfigurationError(
            f"{source}: the deployment document states {unknown}, which this buyer service "
            f"does not read; it reads {sorted(DOCUMENT_KEYS)}. Refused rather than ignored: "
            f"this document has one meaningful key, so a misspelt one is a deployment that "
            f"looks configured and behaves exactly like an unconfigured one."
        )

    if "exchange_url" not in body:
        raise DeploymentConfigurationError(
            f"{source}: the deployment document names no 'exchange_url'. A buyer deployment "
            f"that states nothing binds nothing, which is indistinguishable from having no "
            f"document at all — and the symptom is a 503 on every confirmed intent."
        )
    exchange_url = _exchange_url(body["exchange_url"], source)
    roster = _roster(body.get("roster"), source)

    raw_timeout = body.get("request_timeout_seconds")
    timeout = (
        DEFAULT_EXCHANGE_TIMEOUT_SECONDS
        if raw_timeout is None
        else _timeout_seconds(raw_timeout, source)
    )

    return Deployment(
        source=source,
        exchange_url=exchange_url,
        roster=roster,
        request_timeout_seconds=timeout,
    )


def read_deployment(env: Mapping[str, str] | None = None) -> Deployment | None:
    """The configured deployment, or ``None`` when this buyer service was given none."""
    environ = os.environ if env is None else env

    path = str(environ.get(ENV_DEPLOYMENT) or "").strip()
    if path:
        source = f"{ENV_DEPLOYMENT}={path}"
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise DeploymentConfigurationError(
                f"{source}: the deployment document could not be read ({exc.__class__.__name__}"
                f": {exc}). A named-but-missing file is a misconfiguration, not an unconfigured "
                f"buyer service, so it is refused rather than answered fail-closed"
            ) from exc
    else:
        inline = str(environ.get(ENV_DEPLOYMENT_JSON) or "").strip()
        if not inline:
            # LAST, and lowest precedence: the bare origin the compose fragment ALREADY hands
            # this service. `apps/buyer/compose.yaml:47` sets
            #
            #     EXCHANGE_URL: "${EXCHANGE_URL:-http://exchange:8083}"
            #     # The exchange is reached by service name inside the network, never by localhost.
            #
            # and `grep -rn EXCHANGE_URL --include='*.py'` over this repo returns **nothing**:
            # the deploy lane declared the address, said what it was for, and no line of code
            # has ever read it. Reading it here is what makes the shipped `docker compose up`
            # stack carry a confirmed intent with no operator action at all — the alternative
            # is a correct composition root that the repository's own deployment cannot reach.
            #
            # It is a bare origin rather than a document, so it goes through the SAME
            # `_exchange_url` validation: `EXCHANGE_URL=exchange:8083` is a 503 naming the
            # problem here exactly as it is inside a document, not a request to a URL with no
            # host in it.
            origin = str(environ.get(ENV_EXCHANGE_URL) or "").strip()
            if not origin:
                return None
            return Deployment(
                source=f"{ENV_EXCHANGE_URL}={origin}",
                exchange_url=_exchange_url(origin, f"{ENV_EXCHANGE_URL}={origin}"),
            )
        source = ENV_DEPLOYMENT_JSON
        text = inline

    if len(text) > MAX_DEPLOYMENT_BYTES:
        raise DeploymentConfigurationError(
            f"{source}: the deployment document is {len(text)} bytes; this buyer service reads "
            f"at most {MAX_DEPLOYMENT_BYTES}. It is parsed on the request path, so its size is "
            f"time a buyer waits"
        )

    try:
        document = json.loads(text)
    except Exception as exc:
        # NOT `except ValueError`. `json.loads` on deeply nested input raises RecursionError,
        # which is not a ValueError and would escape this function entirely — an HTTP 500 on
        # every confirm rather than the 503 this module documents. A document this service
        # cannot parse is a misconfiguration whatever the parser raised on it.
        raise DeploymentConfigurationError(
            f"{source}: not valid JSON ({type(exc).__name__}: {exc})"
        ) from exc
    return parse_deployment(document, source=source)


# =====================================================================================
# The outbound client — the exchange's `POST /auctions` and `POST /auctions/{id}/accept`
# =====================================================================================
@dataclass(frozen=True)
class _AuctionRecord:
    """One auction's ``POST /auctions`` answer, kept verbatim beside our own timestamp.

    The envelope keeps our clock out of the exchange's document: a ``recorded_at`` merged
    into their answer would read as something the exchange said.
    """

    auction_id: str
    recorded_at: str
    response: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "auction_id": self.auction_id,
            "recorded_at": self.recorded_at,
            "response": dict(self.response),
        }


class HttpExchangeClient:
    """The real outbound client: one object, both doors of the exchange.

    It satisfies both published client contracts at once, and that is why there is one object
    rather than two:

    * ``create_auction(payload)`` is the first name in
      :data:`~buyer_svc.intent.confirmation.AUCTION_CLIENT_METHODS`, so
      :func:`~buyer_svc.intent.confirm` picks it;
    * ``accept_offer(payload)`` is the first name in
      :data:`~buyer_svc.accept.handoff.EXCHANGE_ACCEPT_METHODS`, so
      :func:`~buyer_svc.accept.accept` picks it.

    Neither route may build a URL — R3 is a rule about *authority*, and
    ``buyer_svc.accept.handoff`` states plainly that "nothing in this package builds a URL —
    that is the whole rule, and it applies to the API URL as much as to the checkout one".
    This class is the one place that knows the exchange's routing, it is outside that package,
    and the only URL it produces is an API path under the operator's own configured origin. It
    never touches, echoes or reconstructs a checkout URL.

    **Status handling is per door, because the exchange's two doors mean different things by
    a non-2xx.**

    ``POST /auctions`` -> anything but 2xx raises. A 422 body carries a ``detail``, not an
    ``auction_id``, and returning it would take ``confirm()`` down the "the exchange accepted
    the auction but returned no id" branch: HTTP 201 to the buyer, a receipt naming no
    auction, and a WARNING in a log nobody is reading.

    ``POST /auctions/{id}/accept`` -> a **409 is returned, parsed**, because it is a documented
    answer rather than a failure. ``buyer_svc.accept.handoff._refuse_if_denied`` says so in as
    many words — *"a client that returns the parsed body rather than raising for status hands
    it straight here"* — and the exchange's own route returns
    ``{"accepted": false, "denial_reason": ...}`` at 409 for every refusal that is about this
    buyer's offer. Raising on it would turn "the store was blacklisted since it bid" into a
    502 that blames the exchange for working correctly. Every other non-2xx (404 for an
    auction that has expired, 503 for a misconfigured exchange) raises.
    """

    #: The statuses every door reads as an answer. A door that also reads a specific refusal
    #: — the accept's 409 — names it at the call site, so the exception is per door and
    #: visible there rather than hidden in a shared set.
    OK_STATUSES = range(200, 300)

    def __init__(
        self,
        base_url: str,
        *,
        roster: Sequence[Mapping[str, Any]] = (),
        timeout: float = DEFAULT_EXCHANGE_TIMEOUT_SECONDS,
        client: Any | None = None,
    ) -> None:
        self._base_url = str(base_url).rstrip("/")
        self._roster: tuple[dict[str, Any], ...] = tuple(dict(row) for row in roster)
        self._timeout = float(timeout)
        self._client = client
        #: The exchange's `POST /auctions` answers, newest last. Bounded; see
        #: `MAX_RECORDED_AUCTIONS` and `create_auction`.
        self._records: OrderedDict[str, _AuctionRecord] = OrderedDict()
        #: One `HttpExchangeClient` is shared by every request this app serves, and uvicorn
        #: runs the handlers on a thread pool, so the ring is touched concurrently. An
        #: `OrderedDict` is not safe across the read-modify-write that eviction needs.
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return self._base_url

    # -- the two doors ---------------------------------------------------------------
    def create_auction(self, payload: Mapping[str, Any]) -> Any:
        """``POST {exchange}/auctions`` — R1's one side effect, over the wire.

        **The deployment's roster is filled in only when the caller's payload carries none**
        — absent, ``null`` or ``[]``. The platform's candidate set is deployment data: which
        merchants compete for a buyer is not a question the browser gets to answer, and
        ``buyer_svc.intent.routes.ConfirmBody`` takes a ``roster`` straight off the wire, so
        without this rule a page could nominate its own field of stores. A payload that names
        a roster of its own is honoured unchanged — that is how a test, or the devstack,
        drives a different candidate set — and an empty one is treated as "none" because
        "solicit nobody" is not a statement a browser is entitled to make.

        **The whole answer is recorded** under its ``auction_id``. Not a cache: the exchange's
        ``GET /auctions/{id}`` returns auction *state* only (see this module's docstring for
        the measured shape), so the ``entries`` / ``excluded`` / ``denied`` / ``ranked`` /
        ``solicited`` diagnostics in this body are published exactly once and survive nowhere
        else. Without the record, a buyer whose shortlist came back empty cannot be told which
        stores were asked, which declined, and why.
        """
        body = dict(payload)
        if not body.get("roster") and self._roster:
            body["roster"] = [dict(row) for row in self._roster]

        answer = self._post("/auctions", body, what="POST /auctions")
        auction_id = str(answer.get("auction_id") or "").strip()
        if auction_id:
            self._record(auction_id, answer)
        return answer

    def accept_offer(self, payload: Mapping[str, Any]) -> Any:
        """``POST {exchange}/auctions/{auction_id}/accept`` — R3's handoff, over the wire.

        ``handoff.accept`` calls this with ``{"auction_id": ..., "bid_ref": ...}`` and states
        that it does so because "the client owns the URL shape and this module must not".
        This is that client, so it is here that the two parts are put where the exchange
        expects them: **the auction id in the PATH and the body carrying ``bid_ref`` alone.**

        The body is not a place the id may also ride, and that is measured rather than
        stylistic — ``apps/exchange/src/accept/routes.py``::

            class AcceptBidRequest(BaseModel):
                '''{"bid_ref": "bid-0001"}, and nothing else: the contract forbids extras.'''
                model_config = ConfigDict(extra="forbid")
                bid_ref: str

        An echoed ``auction_id`` is therefore a **422** from the exchange, which this client
        raises on and ``accept/routes.py`` answers as a 502 — "the exchange is broken" for a
        request this service made wrong. Measured against the real stack over loopback before
        this line said so::

            POST /buyer/shortlist/accept -> 502
            {"detail": "the exchange answered 422 to POST /auctions/<id>/accept:
                        {\"detail\": ... 'auction_id' ... extra_forbidden ...}"}
        """
        body = payload if isinstance(payload, Mapping) else {}
        auction_id = str(body.get("auction_id") or "").strip()
        if not auction_id:
            # `handoff.accept` already refuses a blank auction_id *before* the client is
            # touched (MissingAuctionReference), so reaching here means a different caller.
            # Refused rather than sent: `/auctions//accept` is a request to a route that does
            # not exist, and its 404 would read as "your auction expired".
            raise ExchangeCallFailed(
                "the accept names no auction_id, so there is no /auctions/{id}/accept to "
                "call; nothing was sent to the exchange"
            )
        return self._post(
            f"/auctions/{quote(auction_id, safe='')}/accept",
            {"bid_ref": str(body.get("bid_ref") or "")},
            what=f"POST /auctions/{auction_id}/accept",
            also_accept=(409,),
        )

    # -- the two reads ---------------------------------------------------------------
    # Read-only, and deliberately NOT symmetric: `outcome_for` is what this service recorded
    # when it opened the auction, `shortlist_for` is what the exchange says right now. The
    # route that serves them keeps them apart for exactly that reason.
    def outcome_for(self, auction_id: str) -> Mapping[str, Any] | None:
        """This client's RECORD of the exchange's answer when it opened the auction.

        ``{"auction_id", "recorded_at", "response"}``, where ``response`` is the exchange's
        body verbatim. ``None`` when this client never opened that auction — which is not the
        same as an auction with no diagnostics, and stays a different answer.
        """
        with self._lock:
            record = self._records.get(str(auction_id))
        return None if record is None else record.to_dict()

    def shortlist_for(self, auction_id: str) -> Mapping[str, Any] | None:
        """``GET {exchange}/auctions/{id}/shortlist`` — LIVE, or ``None`` if the exchange 404s.

        ``None`` rather than an empty shortlist, because the exchange draws that distinction
        deliberately: an auction whose every candidate was excluded has a real shortlist with
        no slots, and that is not the same answer as an auction this exchange has forgotten.
        The 404's body is never parsed — it carries a ``detail``, not a shortlist.
        """
        what = f"GET /auctions/{auction_id}/shortlist"
        status, body = self._get(
            f"/auctions/{quote(str(auction_id), safe='')}/shortlist",
            what=what,
            also_accept=(404,),
        )
        if status == 404:
            return None
        return dict(self._parse(status, body, what=what))

    # -- plumbing -------------------------------------------------------------------
    def _record(self, auction_id: str, answer: Mapping[str, Any]) -> None:
        """Keep this answer, evicting the OLDEST once the ring is full."""
        record = _AuctionRecord(
            auction_id=auction_id,
            recorded_at=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            response=dict(answer),
        )
        with self._lock:
            # Popped first so a re-opened id moves to the young end rather than keeping the
            # insertion position of the answer it replaces.
            self._records.pop(auction_id, None)
            self._records[auction_id] = record
            while len(self._records) > MAX_RECORDED_AUCTIONS:
                self._records.popitem(last=False)

    def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        what: str,
        also_accept: tuple[int, ...] = (),
    ) -> Any:
        """One bounded POST, returning the parsed body or raising :class:`ExchangeCallFailed`."""
        status, body = self._call("POST", path, payload=payload, what=what, also_accept=also_accept)
        return self._parse(status, body, what=what)

    def _get(self, path: str, *, what: str, also_accept: tuple[int, ...] = ()) -> tuple[int, bytes]:
        """One bounded GET, returning ``(status, raw body)`` for the caller to read.

        The status comes back rather than only a parsed body because the one read this client
        does — :meth:`shortlist_for` — has to tell "the exchange has no such auction" from
        "the auction exists and its shortlist has no slots", and those are different answers
        to a buyer looking at an empty page.
        """
        return self._call("GET", path, payload=None, what=what, also_accept=also_accept)

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None,
        what: str,
        also_accept: tuple[int, ...] = (),
    ) -> tuple[int, bytes]:
        """One bounded call to the exchange: ``(status, raw body)``, or a raise.

        The body is read through a **bounded** stream rather than with ``response.json()``,
        for the reason ``exchange.composition.HttpBidSolicitor.solicit`` measured on its own
        side of this seam: a response read whole into memory is a memory bound set by the
        peer, and ``apps/buyer/compose.yaml`` caps this container. Here the peer is a
        first-party service rather than a third-party store agent, so this is a bound against
        a runaway exchange rather than against an adversary — which is why the cap is 4 MiB
        and not 256 KiB.
        """
        url = f"{self._base_url}{path}"
        deadline = time.monotonic() + MAX_EXCHANGE_WALL_CLOCK_SECONDS
        # No `json=` at all for a bodiless call: `json=None` would be indistinguishable from
        # a request whose body is the JSON literal `null`, and only one of those is a GET.
        sent: dict[str, Any] = {} if payload is None else {"json": dict(payload)}
        try:
            with self._http_client().stream(method, url, timeout=self._timeout, **sent) as response:
                status = int(response.status_code)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_EXCHANGE_RESPONSE_BYTES:
                        # Stop READING, not merely stop using: a cap applied after the body is
                        # in memory is not a cap. Leaving the block closes the connection.
                        raise ExchangeCallFailed(
                            f"the exchange's answer to {what} exceeded "
                            f"{MAX_EXCHANGE_RESPONSE_BYTES} bytes and was not read",
                            status_code=status,
                        )
                    if time.monotonic() >= deadline:
                        # The byte cap alone does not bound TIME: httpx's timeout resets on
                        # every chunk. See MAX_EXCHANGE_WALL_CLOCK_SECONDS.
                        raise ExchangeCallFailed(
                            f"the exchange took longer than "
                            f"{MAX_EXCHANGE_WALL_CLOCK_SECONDS}s to finish answering {what}",
                            status_code=status,
                        )
        except ExchangeCallFailed:
            raise
        except Exception as exc:
            # A refused connection, a DNS failure, a timeout, a TLS error. The buyer's request
            # was fine; the upstream is not there.
            raise ExchangeCallFailed(
                f"{what} to {self._base_url} failed before an answer arrived "
                f"({type(exc).__name__}: {exc})"
            ) from exc

        if status not in self.OK_STATUSES and status not in also_accept:
            raise ExchangeCallFailed(
                f"the exchange answered {status} to {what}: {_excerpt(bytes(body))}",
                status_code=status,
            )
        return status, bytes(body)

    def _parse(self, status: int, body: bytes, *, what: str) -> Any:
        """The JSON object in a body whose status this client has already accepted."""
        try:
            answer = json.loads(body) if body else None
        except ValueError as exc:
            raise ExchangeCallFailed(
                f"the exchange answered {status} to {what} with a body this service could "
                f"not parse as JSON ({exc}): {_excerpt(body)}",
                status_code=status,
            ) from exc
        if not isinstance(answer, Mapping):
            raise ExchangeCallFailed(
                f"the exchange answered {status} to {what} with "
                f"{type(answer).__name__}, not a JSON object: {_excerpt(body)}",
                status_code=status,
            )
        return dict(answer)

    def _http_client(self) -> Any:
        """One pooled client for this process, built on first use.

        Deferred rather than built in ``__init__`` so that constructing a client — which a
        test or a config check may do — opens no sockets, and so that ``httpx`` is imported
        only by a deployment that actually reaches out.
        """
        if self._client is None:
            import httpx  # noqa: PLC0415 — see the docstring

            self._client = httpx.Client(timeout=self._timeout)
        return self._client


def _excerpt(body: bytes, limit: int = 400) -> str:
    """A short, safe rendering of an error body, for a message a human will read."""
    text = body[: limit + 1].decode("utf-8", errors="replace")
    return text if len(text) <= limit else f"{text[:limit]}…"


# =====================================================================================
# Binding
# =====================================================================================
def configure_buyer(app: Any, deployment: Deployment) -> tuple[str, ...]:
    """Bind everything ``deployment`` states that this app has not already been given.

    Returns the names it bound, so a caller can say what a deployment actually turned on.

    ONE :class:`HttpExchangeClient` is bound to both attributes rather than two clients to
    one attribute each. The two seams are two doors of one service: a buyer that opened an
    auction on one exchange and accepted an offer on another would be accepting an offer in
    an auction the accepting process has never heard of.
    """
    bound: list[str] = []

    def unset(name: str) -> bool:
        """Never SET, as opposed to set to ``None``. The difference is load-bearing.

        ``getattr(app.state, name, None) is None`` cannot tell "nobody has wired a client" from
        "somebody wired ``None`` on purpose", and the second is a real gesture in this tree:
        ``tests/test_intent_routes.py::test_a_service_with_no_exchange_wired_says_so`` writes
        ``app.state.auction_client = None`` to assert the 503. Now that an ambient
        ``EXCHANGE_URL`` is enough to configure this service, reading that as "unset" would
        bind a client over a test — and over a deployment — that had said no.

        Starlette's ``State`` raises ``AttributeError`` for a name it does not hold, so
        ``hasattr`` separates the two exactly.
        """
        return not hasattr(app.state, name)

    client = HttpExchangeClient(
        deployment.exchange_url,
        roster=deployment.roster,
        timeout=deployment.request_timeout_seconds,
    )
    for attr in (AUCTION_CLIENT_ATTR, EXCHANGE_CLIENT_ATTR):
        if unset(attr):
            setattr(app.state, attr, client)
            bound.append(attr)

    if bound:
        _log.info(
            "buyer composition: bound %s against exchange %s (%s)",
            ", ".join(bound),
            deployment.exchange_url,
            deployment.source,
        )
    return tuple(bound)


def ensure_configured(app: Any, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Bind this app's deployment once. Idempotent, and a no-op when none is configured.

    Called from the routes rather than from ``create_app`` because ``main.py`` is
    orchestrator-frozen (B6(iii)). The guard is on ``app.state``, so two apps in one process
    (which is every test module in this repository) are configured independently.

    A failure is **not** cached: the flag is set only on success, so an operator who fixes a
    malformed document is served by the next request without restarting the process.
    """
    already = getattr(app.state, STATE_FLAG, None)
    if already is not None:
        return already
    deployment = read_deployment(env)
    if deployment is None:
        # Deliberately NOT cached. "No deployment configured" is two `os.environ` lookups to
        # re-establish, and caching it would mean a document that appeared after the first
        # request is ignored for the life of the process. Only a SUCCESSFUL bind is
        # remembered; a failure is not cached either, so a fixed document is picked up by the
        # next request.
        return ()
    bound = configure_buyer(app, deployment)
    setattr(app.state, STATE_FLAG, bound)
    return bound


# =====================================================================================
# The built UI — opt-in, and never on the request path
# =====================================================================================
def mount_ui(app: Any, env: Mapping[str, str] | None = None) -> str | None:
    """Mount the built UI at ``/`` when :data:`ENV_UI_DIST` names a real directory.

    Returns the directory that was mounted, or ``None``.

    **Called by nobody in this module.** Not by :func:`configure_buyer`, not by
    :func:`ensure_configured`, and that is the point rather than an omission:

    * the shipped API image should not serve a demo page. ``uvicorn buyer_svc.main:app`` is
      what ``apps/buyer/Dockerfile`` runs, and an API container that grew a static mount
      because a variable happened to be set in its environment is a deployment surprise;
    * the two functions above are **request-time** hooks (``main.py`` is frozen, B6(iii)), and
      adding a route to an application from inside a request it is already serving mutates the
      router under Starlette's own matching loop.

    So this is a deliberate call a *launcher* makes — ``apps/buyer/devstack/run.py`` — and it
    makes it LAST, after ``main.create_app()`` has mounted every ``<feature>/routes.py``.
    Starlette matches in registration order and a ``Mount`` at ``/`` matches everything, so
    an earlier call would let a built file shadow ``/buyer/intent/clarify``.

    It never raises. A missing, blank or nonexistent directory mounts nothing and is not an
    error: the API is the service and the UI is a build artefact that may simply not have been
    built yet. That is the one place in this module where silence is right, which is why the
    check is an explicit ``is_dir()`` rather than letting ``StaticFiles`` raise at import time.
    """
    environ = os.environ if env is None else env
    directory = str(environ.get(ENV_UI_DIST) or "").strip()
    if not directory:
        return None
    try:
        if not Path(directory).is_dir():
            return None
        # Imported here, not at module scope: `read_deployment` and the client above are
        # importable with no web framework present, and `StaticFiles` drags Starlette in.
        from fastapi.staticfiles import StaticFiles  # noqa: PLC0415 — see above

        app.mount("/", StaticFiles(directory=directory, html=True), name="buyer-ui")
    except OSError:
        # An unreadable path, a broken symlink, a permission error. A buyer service that
        # refuses to start because a static directory is odd is worse than one serving its API.
        return None
    _log.info("buyer composition: serving the built UI at / from %s", directory)
    return directory


# LAST, and for the reason every `routes.py` in this tree ends the same way: this file is
# reachable as `buyer_svc.composition` AND as `apps.buyer.svc.src.composition`, and no
# package `__init__` imports it, so left alone Python executes it a SECOND time under the
# second name. Two `ExchangeCallFailed` classes from one `class` statement is not a
# curiosity — `except ExchangeCallFailed` written against one spelling does not catch the
# one the other raises, and the route that maps it to a 502 would let it escape as a 500.
# `buyer_svc.intent._spellings` is that mechanism; it is imported HERE, at the bottom, so
# this module's own namespace is complete first, and it reaches nothing that imports this
# module back. Asserted in `tests/test_composition_wiring.py`.
from .intent._spellings import bind_spellings  # noqa: E402  (see above)

bind_spellings(sys.modules[__name__])
