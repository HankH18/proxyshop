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
* an unrecognised key is refused, naming it. The exchange's document has five keys and a typo
  in one still leaves four working; this document has **one meaningful key**, so
  ``{"exchange_uri": ...}`` is a deployment that reads as configured and behaves as
  unconfigured.

A malformed document raises :class:`DeploymentConfigurationError`, which the two routes turn
into a **503 naming the problem**, never a 500: a buyer service told to read a deployment it
cannot read is misconfigured, and that is a different thing from a buyer service nobody has
configured.

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
import time
from collections.abc import Mapping
from dataclasses import dataclass
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
    "EXCHANGE_CLIENT_ATTR",
    "MAX_DEPLOYMENT_BYTES",
    "MAX_EXCHANGE_RESPONSE_BYTES",
    "MAX_EXCHANGE_WALL_CLOCK_SECONDS",
    "STATE_FLAG",
    "Deployment",
    "DeploymentConfigurationError",
    "ExchangeCallFailed",
    "HttpExchangeClient",
    "configure_buyer",
    "ensure_configured",
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
DOCUMENT_KEYS = frozenset({"exchange_url", "request_timeout_seconds"})

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

    raw_timeout = body.get("request_timeout_seconds")
    timeout = (
        DEFAULT_EXCHANGE_TIMEOUT_SECONDS
        if raw_timeout is None
        else _timeout_seconds(raw_timeout, source)
    )

    return Deployment(
        source=source,
        exchange_url=exchange_url,
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
        timeout: float = DEFAULT_EXCHANGE_TIMEOUT_SECONDS,
        client: Any | None = None,
    ) -> None:
        self._base_url = str(base_url).rstrip("/")
        self._timeout = float(timeout)
        self._client = client

    @property
    def base_url(self) -> str:
        return self._base_url

    # -- the two doors ---------------------------------------------------------------
    def create_auction(self, payload: Mapping[str, Any]) -> Any:
        """``POST {exchange}/auctions`` — R1's one side effect, over the wire."""
        return self._post("/auctions", payload, what="POST /auctions")

    def accept_offer(self, payload: Mapping[str, Any]) -> Any:
        """``POST {exchange}/auctions/{auction_id}/accept`` — R3's handoff, over the wire.

        The auction id rides in the path AND stays in the body: ``handoff.accept`` calls this
        with ``{"auction_id": ..., "bid_ref": ...}`` and states that it does so because "the
        client owns the URL shape and this module must not". This is that client.
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
            {"bid_ref": str(body.get("bid_ref") or ""), "auction_id": auction_id},
            what=f"POST /auctions/{auction_id}/accept",
            also_accept=(409,),
        )

    # -- plumbing -------------------------------------------------------------------
    def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        what: str,
        also_accept: tuple[int, ...] = (),
    ) -> Any:
        """One bounded POST, returning the parsed body or raising :class:`ExchangeCallFailed`.

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
        try:
            with self._http_client().stream(
                "POST", url, json=dict(payload), timeout=self._timeout
            ) as response:
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

        try:
            answer = json.loads(bytes(body)) if body else None
        except ValueError as exc:
            raise ExchangeCallFailed(
                f"the exchange answered {status} to {what} with a body this service could "
                f"not parse as JSON ({exc}): {_excerpt(bytes(body))}",
                status_code=status,
            ) from exc
        if not isinstance(answer, Mapping):
            raise ExchangeCallFailed(
                f"the exchange answered {status} to {what} with "
                f"{type(answer).__name__}, not a JSON object: {_excerpt(bytes(body))}",
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

    client = HttpExchangeClient(deployment.exchange_url, timeout=deployment.request_timeout_seconds)
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
