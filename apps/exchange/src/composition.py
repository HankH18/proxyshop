"""The composition root: the collaborators a *deployed* exchange binds at start-up.

Every part of this exchange is injected, and every default is fail-closed. That is the right
shape for the components — and, until this module existed, it was also the reason nothing
joined them. ``uvicorn exchange.main:app`` boots an exchange with

* ``seller_eligibility``  -> :class:`~exchange.eligibility.StaticSellerEligibility` with no
  rows, whose answer for every store is ``UNAVAILABLE``;
* ``bid_solicitor``       -> :class:`~exchange.auction.routes.NullSolicitor`, which asks nobody;
* ``trust_snapshot``      -> ``{}``, in which no store can be shown to be off the blacklist;
* ``ranking_registered_domains`` -> nothing, so the platform vouches for no checkout host;
* ``auction_bids``        -> :class:`~exchange.accept.routes.NoRecordedBids`, which knows none.

Five fail-closed defaults are a correct *deployment* posture and a dead *service*. Measured on
this tree, on the app exactly as ``create_app()`` builds it::

    POST /auctions -> 201
    {"solicited": [], "entries": [],
     "denied": [{"store_id": "s1", "status": "unavailable",
                 "reason": "unavailable: static-eligibility: s1 is unavailable"}, ...],
     "ranked": [], "shortlist": {"slots": []}}

    POST /auctions/{id}/accept {"bid_ref": "..."} -> 409
    {"denial_reason": "unknown_bid: auction '...' carries no bid '...'; nothing to accept"}

Every green demonstration in this repository supplies the join whose absence is the defect:
``e2e/support/s1/flow.py:369`` calls ``configure_auctions(app, machine=..., solicitor=...,
eligibility=...)`` itself, and ``_t294_corpus`` calls ``configure_auctions`` **and**
``configure_accept``. A test that wires the app it is testing is measuring the wiring it wrote.

So this module is the place a **deployment** states its collaborators, and it states them in
configuration rather than in code, because a composition root that has to be edited to deploy
is not a composition root:

``EXCHANGE_DEPLOYMENT``
    Path to a JSON document (below).
``EXCHANGE_DEPLOYMENT_JSON``
    The same document, inline — for a container that would rather set a variable than mount a
    file. ``EXCHANGE_DEPLOYMENT`` wins if both are set.

Neither set is **exactly today's behaviour**: nothing is bound and every default above stands.
That is deliberate and it is the one property this module may not break — an exchange nobody
has configured must still refuse everything rather than quietly admit anything.

The document
------------

.. code-block:: json

    {
      "sellers": [
        {"store_id": "s1",
         "eligibility": "eligible",
         "registered_domain": "s1.example.com",
         "bid_endpoint": "http://store-agent-s1:8080/v1/bid-requests"}
      ],
      "trust_snapshot": {"s1": {"store_id": "s1", "blacklisted": false, "score": 0.8}},
      "checkout_mode": "redirect"
    }

``sellers``
    The platform's own seller registry, and it feeds three collaborators that must agree:
    R12's eligibility gate, C10/D22's registered-domain check (both the ranking's and the
    accept path's), and the outbound solicitor's endpoint table. They are read from ONE list
    because an exchange that thinks a store is eligible, holds no domain for it and cannot
    reach its agent has three different opinions about one seller.
``trust_snapshot``
    The trust service's blacklist projection, ``{store_id: row}``. **Not** derived from
    ``sellers``: R12's eligibility and the trust snapshot are two independent reads by design
    (``ranking/filters.py`` calls them ``blacklist_unreadable`` and ``blacklisted_store``
    separately), and a composition root that manufactured one from the other would be
    inventing an answer the trust service never gave. The *served* trust document
    (``{"version": ..., "stores": {...}}``) is accepted and unwrapped here — that unwrap is
    the seam ``e2e/support/s1/flow.py`` documents as "nothing in the tree performs".
``checkout_mode``
    Optional; ``CHECKOUT_MODE`` still works and this overrides it for this app.

Validation is loud, and every rule below was chosen because the silent version of it produces
an empty shortlist that looks like a policy decision:

* an unrecognised ``eligibility`` word is refused, not treated as "probably fine";
* a ``registered_domain`` carrying a scheme or a path is refused — ``is_on_domain`` compares a
  bare host, so ``"https://s1.example.com"`` matches nothing and excludes every candidate for
  that store with no hint as to why;
* a ``trust_snapshot`` row whose ``blacklisted`` is not a real ``bool`` is refused —
  ``ranking/filters.py`` reads ``0`` and ``"false"`` as *unreadable*, which denies;
* an unregistered ``checkout_mode`` is refused here rather than 503-ing once per accept.

A malformed document raises :class:`DeploymentConfigurationError`, which the two routes turn
into a **503 naming the problem**. That is the same answer this service already gives for an
unregistered ``CHECKOUT_MODE``: a misconfigured deployment is not a decision about the buyer.

When it runs
------------
``apps/exchange/src/main.py`` is orchestrator-frozen (B6(iii)), so this module cannot be
called from ``create_app``. :func:`ensure_configured` is called instead at the top of the two
routes that need it, and binds **once per app** — a request-time start-up hook, in the same
place and for the same reason ``accept/routes.py`` reads ``CHECKOUT_MODE`` per request.

It never overwrites anything already on ``app.state``, so a deployment (or a test) that calls
``configure_auctions`` / ``configure_ranking`` / ``configure_accept`` itself still wins, and
this module composes those three published seams rather than reaching past them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .accept.routes import InMemoryAuctionBids, configure_accept
from .auction.routes import configure_auctions
from .checkout.registry import registered_modes
from .checkout.sellers import StaticRegisteredDomains
from .eligibility import ELIGIBILITY_STATUSES, StaticSellerEligibility
from .ranking.serving import configure_ranking

__all__ = [
    "DEFAULT_SOLICIT_TIMEOUT_SECONDS",
    "ENV_DEPLOYMENT",
    "ENV_DEPLOYMENT_JSON",
    "Deployment",
    "DeploymentConfigurationError",
    "HttpBidSolicitor",
    "SellerRow",
    "configure_exchange",
    "ensure_configured",
    "read_deployment",
]

#: Path to the deployment document.
ENV_DEPLOYMENT = "EXCHANGE_DEPLOYMENT"
#: The same document, inline. ``ENV_DEPLOYMENT`` outranks it.
ENV_DEPLOYMENT_JSON = "EXCHANGE_DEPLOYMENT_JSON"

#: ``app.state`` flag saying this app has been through :func:`ensure_configured`.
STATE_FLAG = "exchange_composition"

#: How long the outbound bid client waits on one store.
#:
#: Shorter than the auction's own default window (``DEFAULT_BID_TIMEOUT_SECONDS``, 3.0s) is
#: wrong and longer is pointless: the fan-out abandons the wait at the deadline anyway, so this
#: exists only to stop a socket outliving the request that opened it. It is the auction's
#: server-side ceiling, which is the longest any one solicitation can still be useful for.
DEFAULT_SOLICIT_TIMEOUT_SECONDS = 10.0


class DeploymentConfigurationError(RuntimeError):
    """The deployment document is missing, unreadable, or says something unusable.

    Raised rather than warned. A composition root that shrugs at a typo produces exactly the
    symptom this module exists to remove: an exchange that boots, answers ``201``, and
    shortlists nobody.
    """


# =====================================================================================
# The document
# =====================================================================================
@dataclass(frozen=True)
class SellerRow:
    """One seller as the PLATFORM states it — never as the seller states it."""

    store_id: str
    eligibility: str
    registered_domain: str | None = None
    bid_endpoint: str | None = None


@dataclass(frozen=True)
class Deployment:
    """A parsed, validated deployment document."""

    source: str
    sellers: tuple[SellerRow, ...] = ()
    trust_snapshot: Mapping[str, Any] | None = None
    checkout_mode: str | None = None

    @property
    def eligibility_rows(self) -> dict[str, str]:
        return {row.store_id: row.eligibility for row in self.sellers}

    @property
    def registered_domains(self) -> dict[str, str]:
        return {
            row.store_id: row.registered_domain
            for row in self.sellers
            if row.registered_domain
        }

    @property
    def bid_endpoints(self) -> dict[str, str]:
        return {row.store_id: row.bid_endpoint for row in self.sellers if row.bid_endpoint}


def _require_mapping(value: Any, what: str, source: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeploymentConfigurationError(
            f"{source}: {what} must be a JSON object, got {type(value).__name__}"
        )
    return value


def _seller_row(raw: Any, index: int, source: str) -> SellerRow:
    row = _require_mapping(raw, f"sellers[{index}]", source)

    store_id = str(row.get("store_id") or "").strip()
    if not store_id:
        raise DeploymentConfigurationError(
            f"{source}: sellers[{index}] states no store_id; a registry row that names no "
            f"store vouches for nothing"
        )

    # Required rather than defaulted. A row whose status is a typo would otherwise be read as
    # "eligible" (convenient, and the wrong direction to fail in) or as "unavailable"
    # (fail-closed, but silently — the operator's file says the store is live and the
    # exchange denies it with no signal at all).
    if "eligibility" not in row:
        raise DeploymentConfigurationError(
            f"{source}: seller {store_id!r} states no 'eligibility'. R12 is a fail-closed "
            f"gate and this file is what opens it, so the word is required rather than "
            f"assumed; write one of {list(ELIGIBILITY_STATUSES)}"
        )
    eligibility = str(row.get("eligibility"))
    if eligibility not in ELIGIBILITY_STATUSES:
        raise DeploymentConfigurationError(
            f"{source}: seller {store_id!r} states eligibility {eligibility!r}, which is not "
            f"one of {list(ELIGIBILITY_STATUSES)}; an unrecognised status has an unknown "
            f"meaning and this exchange will not guess at one"
        )

    domain = row.get("registered_domain")
    registered_domain: str | None = None
    if domain is not None:
        registered_domain = str(domain).strip().lower().rstrip(".")
        if not registered_domain:
            registered_domain = None
        elif "/" in registered_domain or ":" in registered_domain:
            # Measured: `exchange.checkout.domain` compares `urlsplit(url).hostname` against
            # this value verbatim, so "https://s1.example.com" and "s1.example.com:8443"
            # match no checkout URL at all and every candidate for the store is excluded
            # `off_domain_checkout` — with a reason that reads like a policy refusal.
            raise DeploymentConfigurationError(
                f"{source}: seller {store_id!r} states registered_domain "
                f"{str(domain)!r}. This is compared against a checkout URL's HOST, so it "
                f"must be a bare host — no scheme, no port, no path — or every candidate "
                f"for this store is excluded off-domain with no hint that the registry is "
                f"the reason"
            )

    endpoint = row.get("bid_endpoint")
    bid_endpoint: str | None = None
    if endpoint is not None:
        bid_endpoint = str(endpoint).strip()
        if not bid_endpoint:
            bid_endpoint = None
        elif not bid_endpoint.startswith(("http://", "https://")):
            raise DeploymentConfigurationError(
                f"{source}: seller {store_id!r} states bid_endpoint {bid_endpoint!r}, which "
                f"is not an http(s) URL; this is the store agent's POST /v1/bid-requests door"
            )

    return SellerRow(
        store_id=store_id,
        eligibility=eligibility,
        registered_domain=registered_domain,
        bid_endpoint=bid_endpoint,
    )


def _trust_snapshot(raw: Any, source: str) -> Mapping[str, Any]:
    """The flat ``{store_id: row}`` mapping the ranking's filters read.

    The trust service's own ``GET /snapshot`` answers the SERVED document
    ``{version, score_version, dimensions, as_of, stores: {...}}``, while
    ``exchange.ranking.filters.trust_row`` reads a flat mapping. Handing the ranker the served
    document denies every store ``blacklist_unreadable`` — the whole auction, not just a
    dishonest store. Both shapes are accepted here, and the unwrap happens once, in the one
    place that is allowed to know the exchange has no client for that endpoint yet.
    """
    document = _require_mapping(raw, "trust_snapshot", source)
    stores = document.get("stores")
    rows = _require_mapping(stores, "trust_snapshot.stores", source) if isinstance(
        stores, Mapping
    ) else document

    snapshot: dict[str, Any] = {}
    for store_id, raw_row in rows.items():
        row = _require_mapping(raw_row, f"trust_snapshot[{store_id!r}]", source)
        flag = row.get("blacklisted")
        if not isinstance(flag, bool):
            # `filters.blacklist_reason` demands a real bool: `0`, `"false"` and a missing key
            # are all read as UNREADABLE, which denies. Refusing here turns a whole silently
            # empty shortlist into one sentence naming the row.
            raise DeploymentConfigurationError(
                f"{source}: trust_snapshot[{str(store_id)!r}] states blacklisted="
                f"{flag!r}. The ranking reads this as a boolean and treats anything else — "
                f"including 0 and \"false\" — as an unreadable blacklist, which DENIES the "
                f"store; write true or false"
            )
        snapshot[str(store_id)] = dict(row)
    return snapshot


def parse_deployment(document: Any, *, source: str) -> Deployment:
    """Validate one deployment document. Raises rather than degrading."""
    body = _require_mapping(document, "the deployment document", source)

    raw_sellers = body.get("sellers", ())
    if not isinstance(raw_sellers, Sequence) or isinstance(raw_sellers, (str, bytes)):
        raise DeploymentConfigurationError(
            f"{source}: 'sellers' must be a JSON array, got {type(raw_sellers).__name__}"
        )
    sellers = tuple(_seller_row(raw, index, source) for index, raw in enumerate(raw_sellers))

    duplicated = sorted(
        {row.store_id for row in sellers if [r.store_id for r in sellers].count(row.store_id) > 1}
    )
    if duplicated:
        raise DeploymentConfigurationError(
            f"{source}: sellers names {duplicated} more than once; one store cannot have two "
            f"registry rows, because the later one would silently decide its eligibility, its "
            f"registered domain and where its bids are solicited from"
        )

    snapshot = None if body.get("trust_snapshot") is None else _trust_snapshot(
        body["trust_snapshot"], source
    )

    checkout_mode = body.get("checkout_mode")
    if checkout_mode is not None:
        checkout_mode = str(checkout_mode).strip().lower()
        if checkout_mode not in registered_modes():
            raise DeploymentConfigurationError(
                f"{source}: checkout_mode {checkout_mode!r} has no registered provider; "
                f"registered modes are {registered_modes()}. Refused here rather than once "
                f"per accept, because a mode nobody serves is a deployment that mints nothing"
            )

    return Deployment(
        source=source,
        sellers=sellers,
        trust_snapshot=snapshot,
        checkout_mode=checkout_mode,
    )


def read_deployment(env: Mapping[str, str] | None = None) -> Deployment | None:
    """The configured deployment, or ``None`` when this exchange was given none."""
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
                f"exchange, so it is refused rather than answered fail-closed"
            ) from exc
    else:
        inline = str(environ.get(ENV_DEPLOYMENT_JSON) or "").strip()
        if not inline:
            return None
        source = ENV_DEPLOYMENT_JSON
        text = inline

    try:
        document = json.loads(text)
    except ValueError as exc:
        raise DeploymentConfigurationError(f"{source}: not valid JSON ({exc})") from exc
    return parse_deployment(document, source=source)


# =====================================================================================
# The outbound bid client — R10's `POST /v1/bid-requests`
# =====================================================================================
class HttpBidSolicitor:
    """The real outbound solicitor: one ``POST /v1/bid-requests`` per rostered store.

    ``NullSolicitor`` — the default this replaces — asks nobody, so every eligible store is
    represented by the exchange's own list-price fallback (R10). That fallback carries neither
    ``expires_at`` nor ``checkout_url``, so it is excluded ``expired_offer`` and
    ``off_domain_checkout`` before it can be ranked. An exchange with no outbound client is
    therefore not an exchange with cheaper offers; it is one with an empty shortlist.

    **The auction context has to be bound before this can ask anything.** The published
    ``BidRequest`` carries ``{auction_id, intent, profile, respond_by}`` and the solicitor port
    is ``solicit(store)`` — a roster row, and nothing else. So this class exposes an optional
    :meth:`for_auction` hook that ``POST /auctions`` calls when the wired solicitor has one,
    returning a view bound to this auction. Solicitors that need no context (``NullSolicitor``,
    every in-process test double, ``e2e``'s ``HostedAgentSolicitor``) expose no such method and
    are used exactly as before.

    Failures are silent *to the auction* and loud in the entry: a store that refuses the
    connection, answers 500, declines with 204 or sends something unreadable simply did not
    bid, and ``collect_bids`` represents it at its list price with a ``fallback_reason``. That
    is R10's own degradation, and it is why this returns ``None`` rather than raising — the
    fan-out discards a raising solicitor's future anyway, which would lose the distinction
    between "declined" and "crashed" for every store at once.
    """

    def __init__(
        self,
        endpoints: Mapping[str, str],
        *,
        timeout: float = DEFAULT_SOLICIT_TIMEOUT_SECONDS,
        client: Any | None = None,
    ) -> None:
        self._endpoints = {str(k): str(v) for k, v in endpoints.items()}
        self._timeout = float(timeout)
        self._client = client
        self._context: dict[str, Any] = {}

    # -- the port -------------------------------------------------------------------
    def for_auction(
        self,
        *,
        auction_id: str,
        intent: Any,
        profile: Any = None,
        respond_by: float | None = None,
    ) -> HttpBidSolicitor:
        """A view of this solicitor bound to one auction's ``BidRequest`` fields."""
        bound = HttpBidSolicitor(
            self._endpoints, timeout=self._timeout, client=self._http_client()
        )
        bound._context = {
            "auction_id": str(auction_id),
            "intent": intent if isinstance(intent, Mapping) else {},
            "profile": profile if isinstance(profile, Mapping) else {},
            "respond_by": _rfc3339(respond_by),
        }
        return bound

    def solicit(self, store: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Ask one store, and answer in the shape ``collect_bids`` reads."""
        store_id = str(store.get("store_id") or "")
        endpoint = self._endpoints.get(store_id)
        if not endpoint:
            # Not an error: a Tier-0 store, or one the registry holds no agent for, is
            # represented at list price rather than asked a question nobody is home to hear.
            return None

        payload = dict(self._context) or {"auction_id": "", "intent": {}, "profile": {}}
        try:
            response = self._http_client().post(endpoint, json=payload, timeout=self._timeout)
        except Exception:
            return None

        if response.status_code != 200:
            return None
        try:
            bid = response.json()
        except ValueError:
            return None
        if not isinstance(bid, Mapping):
            return None
        # `store_id` and `received_at` are stamped authoritatively by `fanout._stamped`; they
        # are named here only so the reply is a well-formed response envelope.
        return {"store_id": store_id, "bid": dict(bid)}

    __call__ = solicit

    # -- plumbing -------------------------------------------------------------------
    def _http_client(self) -> Any:
        """One pooled client for this process, built on first use.

        Deferred rather than built in ``__init__`` so that constructing a solicitor — which a
        test or a config check may do — opens no sockets, and so that ``httpx`` is imported
        only by a deployment that actually reaches out.
        """
        if self._client is None:
            import httpx  # noqa: PLC0415 — see the docstring

            self._client = httpx.Client(timeout=self._timeout)
        return self._client


def _rfc3339(moment: float | None) -> str:
    """A deadline epoch as the ``date-time`` string ``BidRequest.respond_by`` publishes."""
    if moment is None:
        return ""
    return (
        datetime.fromtimestamp(float(moment), tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# =====================================================================================
# Binding
# =====================================================================================
def configure_exchange(app: Any, deployment: Deployment) -> tuple[str, ...]:
    """Bind everything ``deployment`` states that this app has not already been given.

    Composed out of the three published wiring seams — :func:`configure_auctions`,
    :func:`configure_ranking` and :func:`configure_accept` — rather than by assigning to
    ``app.state`` directly, so this module can never drift from what those functions mean (the
    accept seam, for one, also turns the process-wide registered-domain seam, and a
    hand-written assignment here would silently skip it).

    Returns the names it bound, so a caller can say what a deployment actually turned on.
    """
    bound: list[str] = []

    def unset(name: str) -> bool:
        return getattr(app.state, name, None) is None

    if deployment.sellers and unset("seller_eligibility"):
        configure_auctions(
            app, eligibility=StaticSellerEligibility(deployment.eligibility_rows)
        )
        bound.append("seller_eligibility")

    endpoints = deployment.bid_endpoints
    if endpoints and unset("bid_solicitor"):
        configure_auctions(app, solicitor=HttpBidSolicitor(endpoints))
        bound.append("bid_solicitor")

    if deployment.trust_snapshot is not None and unset("trust_snapshot"):
        configure_ranking(app, trust_snapshot=dict(deployment.trust_snapshot))
        bound.append("trust_snapshot")

    domains = deployment.registered_domains
    if domains:
        # ONE object for both doors. The ranking reads `ranking_registered_domains` and the
        # accept path reads `registered_domains`; two sources would be two opinions about
        # which host a store owns, and the ranking would shortlist a candidate the accept
        # path then refuses off-domain.
        registry = StaticRegisteredDomains(domains)
        if unset("ranking_registered_domains"):
            configure_ranking(app, registered_domains=registry)
            bound.append("ranking_registered_domains")
        if unset("registered_domains"):
            configure_accept(app, registered_domains=registry)
            bound.append("registered_domains")

    if deployment.checkout_mode and unset("checkout_mode"):
        configure_accept(app, checkout_mode=deployment.checkout_mode)
        bound.append("checkout_mode")

    if unset("auction_bids"):
        # The auction's own record of what it collected. `POST /auctions` installs one anyway
        # (see `auction/routes.py::_bid_book`); binding it here as well means an accept that
        # arrives first — an auction created by another process against a shared store — meets
        # a book rather than installing `NoRecordedBids` in front of one.
        configure_accept(app, bids=InMemoryAuctionBids())
        bound.append("auction_bids")

    return tuple(bound)


def ensure_configured(app: Any, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Bind this app's deployment once. Idempotent, and a no-op when none is configured.

    Called from the routes rather than from ``create_app`` because ``main.py`` is
    orchestrator-frozen (B6(iii)). The guard is on ``app.state``, so two apps in one process
    (which is every test module in this repository) are configured independently.

    A failure is **not** cached: the flag is set only on success, so an operator who fixes a
    malformed document is served by the next request without restarting the process — the same
    property ``CHECKOUT_MODE`` already has, for the same reason.
    """
    already = getattr(app.state, STATE_FLAG, None)
    if already is not None:
        return already
    deployment = read_deployment(env)
    bound = () if deployment is None else configure_exchange(app, deployment)
    setattr(app.state, STATE_FLAG, bound)
    return bound
