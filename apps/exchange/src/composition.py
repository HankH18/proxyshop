"""The composition root: the collaborators a *deployed* exchange binds at start-up.

Every part of this exchange is injected, and every default is fail-closed. That is the right
shape for the components — and, until this module existed, it was also the reason nothing
joined them. ``uvicorn exchange.main:app`` boots an exchange with

* ``seller_eligibility``  -> :class:`~exchange.eligibility.StaticSellerEligibility` with no
  rows, whose answer for every store is ``UNAVAILABLE``;
  **No longer** — see the note on the R12 seam below, which is the second entry in this list
  to leave it for the same reason the ledger sink did;
* ``bid_solicitor``       -> :class:`~exchange.auction.routes.NullSolicitor`, which asks nobody;
* ``trust_snapshot``      -> ``{}``, in which no store can be shown to be off the blacklist;
  **No longer either** — it is the third entry to leave this list, and it left for the reason
  the two before it did: see the note on R12's MIDDLE gate below;
* ``ranking_registered_domains`` -> nothing, so the platform vouches for no checkout host;
* ``auction_bids``        -> :class:`~exchange.accept.routes.NoRecordedBids`, which knows none;
* ``intent_clusters``     -> :class:`~exchange.retrieval.clusters.NoIntentClusters`, which
  names no catalogue cluster, so no intent is assigned one and every store agent whose
  policy document pursues NAMED clusters answers ``204 cluster_not_pursued``;
* ``ranking_catalog``     -> :class:`~exchange.ranking.verification.NoCatalogSnapshots`, which
  holds a snapshot for nobody, so no claim is ever checked, no hard constraint is ever
  satisfied, and a shopper who states a must-have is shown an empty shortlist.

There was an eighth seam in that list and it did not belong there, because losing it is not
fail-*closed*, it is fail-*silent*: ``auction_machine`` -> a bare
:class:`~exchange.auction.state.AuctionStateMachine`, whose
:class:`~exchange.auction.ledger.LedgerRecorder` installs an in-process
:class:`~exchange.auction.ledger.InMemoryLedgerSink` — so every transition a served auction
made was appended to a list discarded with the app, and the hash chaining, once-only landing
and replay in ``apps/trust/src/events`` graded a stream no request ever produced (T-150). It
is bound by DEFAULT now, by :func:`bind_ledger_sink`, to the trust service's published
``POST /events`` — through :mod:`proxyshop_support.trust_ledger`, the shared writer merchant
and buyer can adopt without depending on this service. See the comment where the trust-ledger
constants used to live for why the address is a default rather than a setting, and for why
this does not weaken the invariant three paragraphs below.

**And the R12 seam has now left the list for the same reason (T-303 b).** ``seller_eligibility``
was the first entry above, and its default was not "fail closed" so much as "ask nobody": a
:class:`~exchange.eligibility.StaticSellerEligibility` built with no rows is a deterministic
DOUBLE, and every store it denied was denied ``static-eligibility: <store> is unavailable`` —
a sentence that reads like a trust verdict and is not one. There was no trust-backed
implementation of the port anywhere in the tree, so ``configure_auctions(app, eligibility=…)``
had no product caller and the served exchange could not tell an honest store from a delisted
one. It is bound by DEFAULT now, by :func:`bind_seller_eligibility`, to the trust service's
published ``GET /snapshot`` — the read door ``packages/contracts/openapi/trust.openapi.json``
declares as "Every store's snapshot, for exchange consumption" and which nothing in this
repository had a client for. It does **not** weaken the invariant below: a trust service that
cannot be read yields no snapshot, a store with no snapshot row is ``UNAVAILABLE``, and
``UNAVAILABLE`` denies at all three gates. See :func:`bind_eligibility` for the full ladder.

**And R12's MIDDLE gate, which the paragraph above was quietly overclaiming about.** "Denies at
all three gates" was true of the eligibility PORT, and the ranking gate does not read the port
— it reads ``app.state.trust_snapshot`` (:func:`exchange.ranking.filters.blacklist_reason`),
deliberately, so one auction does not read the eligibility backend once per candidate inside
R10's synchronous window. Until :class:`LiveTrustSnapshot` existed the only thing that could
bind that key was a literal ``trust_snapshot`` object typed into the deployment document, so
every deployment that did not type one out consulted trust at two gates out of three and ranked
against ``{}`` — which excludes EVERY store ``blacklist_unreadable`` and empties the shortlist
for honest and dishonest stores alike. That is the fail-closed path firing on honest traffic,
which is a dead service rather than a strict one. :func:`_bind_live_ranking_snapshot` now points
it at the same trust service, and at the SAME reader when this module built one, so the three
gates cannot hold two opinions about one store inside one auction.

**And the deployment shape that made the R12 seam reachable at all.** A document states its
sellers to state their bid endpoints and registered domains, not only their eligibility — and
``bind_eligibility``'s first rung took the presence of ``sellers`` as the answer to all three.
So the only deployment that ever reached the trust-backed rung was one that named no sellers,
which is also one that solicits nobody and shortlists nobody: the trust engine was reachable
only by an exchange with no traffic to apply it to. A row may now say
``"eligibility": "trust"`` (:data:`TRUST_DEFERRED_ELIGIBILITY`) and hand that one question to
the trust service while still stating where the store bids.

Seven fail-closed defaults are a correct *deployment* posture and a dead *service*. Measured on
this tree, on the app exactly as ``create_app()`` builds it (the eligibility reason is the
pre-T-303 one, kept verbatim because it is what was measured)::

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
      "intent_clusters": [
        {"cluster_id": "cluster-espresso",
         "label": "Espresso machines",
         "category": "coffee",
         "terms": ["espresso machine", "espresso"],
         "attributes": {"brew_method": "espresso"}}
      ],
      "catalog": {
        "s1": {"snapshot_id": "snap-s1",
               "captured_at": "2026-01-01T00:00:00Z",
               "products": [
                 {"product_ref": "prod-1",
                  "attributes": {"water_tank_l": {"value": 2.0, "unit": "l"},
                                 "availability": {"value": "in_stock"}},
                  "offer": {"unit_price": 389.0, "currency": "USD"}}
               ]}
      },
      "checkout_mode": "redirect",
      "trust_url": "http://trust:8084"
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
    A document that states this key and **no** ``sellers`` also gets its R12 answer from it,
    through :class:`~exchange.eligibility.trust_backed.TrustBackedSellerEligibility` — see
    :func:`bind_eligibility` for why ``sellers`` still wins when both are stated.
``intent_clusters``
    The NAMED catalogue clusters this exchange addresses intents to, in the same
    ``{cluster_id, label}`` spelling ``apps/merchant``'s onboarding interview already uses for
    the option list a merchant's ``pursue_clusters`` is resolved against. It is configuration
    for the reason :mod:`~exchange.retrieval.clusters` states at length and does not repeat
    here: no service in this repository publishes that vocabulary — ``upsert_intent_cluster``
    has zero callers, and no table, node or registry holds the names — so the exchange cannot
    discover it and a person states it, exactly as a person states the trust snapshot.
    Omitted, nothing is assigned and the exchange behaves as it did before.
``catalog``
    The catalogue snapshots this exchange grades a store's CLAIMS against, ``{store_id:
    snapshot}``, in the shape :func:`claim_verification.verify` reads. It is the last of the
    seven and the one whose absence is least visible: with no catalog wired, every claim comes
    back ``ambiguous`` — R18's verdict for a claim this exchange could not check at all, not
    one it checked and found nothing behind — R19 refuses to let anything but ``verified``
    satisfy a hard constraint, and **an intent carrying any must-have shortlists nobody**.
    (It does NOT also sink those stores in the ranking: see
    :func:`exchange.ranking.features.verified_claim_ratio`, which counts the claims this
    exchange decided and leaves an undecided one out of the ratio rather than scoring it as a
    failure.) Measured on this tree over a
    real socket, two rostered stores that both bid, one auction, the same request differing
    only in whether this key is present::

        without "catalog": ranked [], 0 shortlist slots, both stores excluded
                           "hard_constraint_unsatisfied: 'capacity_l': the candidate carries
                            no such attribute, so the constraint is undecidable and does not
                            count as satisfied (R19) — only a verified supporting claim
                            satisfies a hard constraint (R19)"
        with    "catalog": ranked ['s1', 's2'], 2 shortlist slots, excluded []

    A real shopper sentence always yields at least a price constraint, so before this key
    existed a deployed exchange shortlisted nobody and the only green demonstrations were the
    ones whose fixtures happened to state no must-have. ``proxyshop_demo`` is that exactly:
    it prints the clarifier's ``brew_method eq espresso`` two beats before it opens its
    auction on ``e2e/support/s1/run.json``'s intent, whose ``hard_constraints`` is ``[]``.
    It is stated by a person for the same
    reason the trust snapshot is: the SNAPSHOT is the exchange's evidence and the claim is the
    store's, and a store that supplied both would be marking its own homework
    (:mod:`~exchange.ranking.verification`). ``apps/buyer/devstack/run.py`` had to reach past
    this module and call ``configure_ranking(catalog=…)`` by hand because this key did not
    exist; the shape it builds there is the shape this key takes.
    Omitted, nothing is bound and :func:`~exchange.ranking.serving.catalog_of` keeps its
    ``NoCatalogSnapshots`` default — exactly today's behaviour.
``checkout_mode``
    Optional; ``CHECKOUT_MODE`` still works and this overrides it for this app.
``trust_url``
    The BASE address of the trust service — both of its doors: the ``POST /events`` every
    auction transition is appended to (T-150) and the ``GET /snapshot`` R12 is read from
    (T-303). Optional, and the one key whose absence does **not** leave a seam unbound:
    :func:`default_ledger_sink` and :func:`default_seller_eligibility` both fall back to
    :data:`DEFAULT_TRUST_URL`, overridable by the :data:`ENV_TRUST_URL` variable, so an
    exchange nobody configured still produces an audit trail rather than a list it discards,
    and still asks trust who it is allowed to solicit rather than asking a double. One key for
    both doors on purpose: an exchange writing its audit trail to one trust service and taking
    its blacklist from another is a deployment mistake nothing else would catch. State it when
    the trust service is not at the compose service name.

Validation is loud, and every rule below was chosen because the silent version of it produces
an empty shortlist that looks like a policy decision:

* an unrecognised ``eligibility`` word is refused, not treated as "probably fine";
* a ``registered_domain`` carrying a scheme or a path is refused — ``is_on_domain`` compares a
  bare host, so ``"https://s1.example.com"`` matches nothing and excludes every candidate for
  that store with no hint as to why;
* a ``trust_snapshot`` row whose ``blacklisted`` is not a real ``bool`` is refused —
  ``ranking/filters.py`` reads ``0`` and ``"false"`` as *unreadable*, which denies;
* an unregistered ``checkout_mode`` is refused here rather than 503-ing once per accept;
* an ``intent_clusters`` row with no ``cluster_id``, or a name stated twice, is refused — a
  cluster nobody can name is not a member of any store's ``pursue_clusters``, and a
  duplicate would let the later row silently decide which terms find that cluster;
* a ``catalog`` snapshot with no ``snapshot_id``, with no ``products``, or holding a product
  row that names no ``product_ref``, is refused — the verifier resolves a claim by matching
  the auction's product against that field, so each of those is a store whose every claim
  comes back ``unsupported`` and which is therefore excluded from every constrained auction.

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
import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from proxyshop_support.trust_ledger import (
    DEFAULT_LEDGER_TIMEOUT_SECONDS,
    DEFAULT_TRUST_URL,
    ENV_TRUST_URL,
    MAX_UNDELIVERED_LEDGER_EVENTS,
    SOURCE_DEFAULT,
    SOURCE_ENVIRONMENT,
    SOURCE_STATED,
    TRUST_EVENTS_PATH,
    TrustLedgerPublisher,
    trust_endpoint,
    trust_events_url,
)

from . import describe, describe_exception
from .auction.collect import (
    REFUSAL_FIELD,
    STORE_DECLINED_REASON,
    STORE_REFUSED_REASON,
    refusal_reason,
)
from .auction.ledger import InMemoryLedgerSink
from .checkout.registry import registered_modes
from .checkout.sellers import StaticRegisteredDomains
from .eligibility import ELIGIBILITY_STATUSES, StaticSellerEligibility
from .eligibility.layered import LayeredSellerEligibility
from .eligibility.trust_backed import (
    TrustBackedSellerEligibility,
    TrustSnapshotUnavailable,
    snapshot_rows,
)

# NOTE ON THIS MODULE'S IMPORTS. The three `configure_*` seams and `InMemoryAuctionBids` live
# in route modules that import THIS module (deferred, inside their request hook), so importing
# them here at module scope would be a cycle waiting for the first caller to enter from the
# other side. They are imported inside :func:`configure_exchange` instead, which also keeps
# the hook cheap for the case that matters most: an exchange with no deployment configured
# reaches `read_deployment`, gets `None`, and imports nothing at all.
#
# `auction.collect` is the exception and is imported at module scope, because it is not a
# route module and imports nothing from here: it is the pure collector whose vocabulary this
# module's solicitor writes into. One spelling of a fallback reason, in the module that
# publishes the list of them.

__all__ = [
    "ANONYMOUS_PSEUDONYM_PREFIX",
    "DECLINE_REASON_HEADER",
    "DEFAULT_LEDGER_TIMEOUT_SECONDS",
    "DEFAULT_SOLICIT_TIMEOUT_SECONDS",
    "DEFAULT_TRUST_SNAPSHOT_TIMEOUT_SECONDS",
    "DEFAULT_TRUST_URL",
    "ENV_DEPLOYMENT",
    "ENV_DEPLOYMENT_JSON",
    "ELIGIBILITY_WORDS",
    "ENV_TRUST_URL",
    "Deployment",
    "DeploymentConfigurationError",
    "HttpBidSolicitor",
    "HttpTrustLedgerSink",
    "HttpTrustSnapshot",
    "LayeredSellerEligibility",
    "LiveTrustSnapshot",
    "MAX_BID_RESPONSE_BYTES",
    "MAX_DEPLOYMENT_BYTES",
    "MAX_DEPLOYMENT_SELLERS",
    "MAX_SOLICIT_WALL_CLOCK_SECONDS",
    "MAX_UNDELIVERED_LEDGER_EVENTS",
    "SellerRow",
    "TRUST_EVENTS_PATH",
    "TRUST_SNAPSHOT_PATH",
    "TRUST_SNAPSHOT_REFRESH_SECONDS",
    "TRUST_SNAPSHOT_RETRY_SECONDS",
    "TRUST_DEFERRED_ELIGIBILITY",
    "TrustBackedSellerEligibility",
    "TrustLedgerPublisher",
    "bind_ledger_sink",
    "bind_seller_eligibility",
    "bind_trust_snapshot_reader",
    "configure_exchange",
    "default_ledger_sink",
    "default_seller_eligibility",
    "ensure_configured",
    "read_deployment",
    "solicitation_profile",
    "trust_events_url",
    "trust_snapshot_endpoint",
]

#: This module's logger. Named ``exchange.composition``, which is what an operator greps to
#: learn what a started exchange actually bound — including, from :func:`bind_ledger_sink`,
#: which trust service its audit trail goes to, and from the sink underneath it, the moments
#: that trail stops and starts landing there.
_log = logging.getLogger(__name__)

#: The header a store agent's ``204`` decline states its reason in.
#:
#: Published on the 204 response of ``POST /v1/bid-requests`` in
#: ``packages/contracts/openapi/store-agent.openapi.json``: a 204 carries no body, so the
#: reason has nowhere else to travel. Restated here rather than imported from ``store_agent``
#: — the exchange does not depend on the seller's package, and the image it ships does not
#: contain it — which is the same arrangement every other cross-service constant in this
#: module has.
DECLINE_REASON_HEADER = "x-proxyshop-decline-reason"

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

#: The most bytes one store's ``POST /v1/bid-requests`` reply may occupy.
#:
#: A **memory bound on an unauthenticated path**, in the same house style as
#: :data:`~exchange.auction.routes.MAX_HARD_CONSTRAINT_BYTES`, and measured rather than feared.
#: A published ``Bid`` is a small document — an offer plus a handful of claims — and 256 KiB is
#: several hundred times the largest one this repository produces. The number that matters is
#: the one on the other side: with no cap, one agent answering a 64 MiB body took this process
#: from a 115.9 MiB peak RSS to 464.6 MiB against ``compose.yaml``'s ``mem_limit: 256m``, on a
#: single store in a single auction. See :meth:`HttpBidSolicitor.solicit`.
#:
#: Refused rather than truncated: half a JSON document is not a bid, and a store that sends one
#: is represented at its list price exactly like a store that stayed silent (R10).
MAX_BID_RESPONSE_BYTES = 256 * 1024

#: The most bytes a deployment document may occupy, and the most sellers it may register.
#:
#: The document is parsed on the REQUEST path (``main.py`` is frozen, so the composition root
#: runs as a request-time start-up hook), which makes its size time a buyer waits. A ceiling on
#: the seller count is the second half of the same bound: the duplicate-id check was O(n²) and
#: measured 14.72s at 20,000 rows, and a document that is large AND malformed re-parses on
#: every request because failures are deliberately not cached.
#:
#: These are operator-supplied values, not attacker-supplied, so this is a guard against a
#: mistake rather than against an adversary — which is why the numbers are generous.
MAX_DEPLOYMENT_BYTES = 4 * 1024 * 1024
MAX_DEPLOYMENT_SELLERS = 10_000

#: The wall-clock ceiling on ONE store's solicitation, from the request leaving to its last
#: byte arriving.
#:
#: Separate from :data:`DEFAULT_SOLICIT_TIMEOUT_SECONDS`, which is httpx's timeout and is
#: **per read** — it resets on every chunk, so it is not a deadline at all. Measured against an
#: agent dripping one chunked byte every 0.5s with the httpx timeout at 1.0s::
#:
#:     solicitor timeout       : 1.0s
#:     solicit() returned after: 21.12s   (answer=None)
#:
#: At that rate a hostile agent pins one of ``MAX_FAN_OUT_WORKERS = 32`` process-wide fan-out
#: workers for roughly 36 hours, and 32 of them end all outbound bidding for the process —
#: the residual risk ``BoundedFanOutPool`` names in its own docstring, and this solicitor is
#: the first thing in the tree that could actually drive it. The window is measured across the
#: whole stream instead, so a slow drip ends at the deadline with no bid, exactly like a store
#: that timed out.
MAX_SOLICIT_WALL_CLOCK_SECONDS = 15.0

# THE TRUST-LEDGER CONSTANTS ARE NOT DEFINED HERE ANY MORE, and their absence is the point.
#
# :data:`DEFAULT_TRUST_URL`, :data:`ENV_TRUST_URL`, :data:`TRUST_EVENTS_PATH`,
# :data:`DEFAULT_LEDGER_TIMEOUT_SECONDS` and :data:`MAX_UNDELIVERED_LEDGER_EVENTS` are
# imported at the top of this module from :mod:`proxyshop_support.trust_ledger` and re-exported
# through ``__all__``, so every reader of ``exchange.composition`` still finds them at the same
# names. What changed is that the exchange no longer OWNS them: they describe a door in another
# service, and the first cross-process writer to that door was written here — privately, in one
# service's composition root — which would have guaranteed a second copy the moment merchant's
# ``HANDOFF`` ring needed draining. Two spellings of "where does the audit trail go" is how two
# services end up disagreeing about it.
#
# `trust:8084` remains a DEFAULT rather than a required setting, for the reason the shared
# module's own header records: an exchange that has to be told where to write its audit trail
# before it writes one is an exchange that ships not writing one, which is the whole of T-150.
# That does NOT weaken the invariant this module's header states — "an exchange nobody has
# configured must still refuse everything rather than quietly admit anything" is a rule about
# the gates (eligibility, the trust snapshot, registered domains), every one of them untouched
# here. Writing down what an auction did admits nobody.


class DeploymentConfigurationError(RuntimeError):
    """The deployment document is missing, unreadable, or says something unusable.

    Raised rather than warned. A composition root that shrugs at a typo produces exactly the
    symptom this module exists to remove: an exchange that boots, answers ``201``, and
    shortlists nobody.
    """


# =====================================================================================
# The document
# =====================================================================================
#: The fourth word a seller row's ``eligibility`` may carry, and the only one that is not a
#: status: the platform declining to answer R12 for this store and handing the question to the
#: trust service.
#:
#: It exists because a document states its sellers for THREE independent reasons — where the
#: agent answers, which host the store checks out on, and whether the platform will have it —
#: and :func:`bind_eligibility` used to read the presence of ``sellers`` as the answer to all
#: three. So the only deployment that consulted trust was one that named no sellers, which is
#: also one that solicits nobody and shortlists nobody: the trust engine was reachable only by
#: an exchange with no traffic to apply it to. Measured, before this word existed, against a
#: real trust service scoring the manifest's own scripted adversary at 0.10 under a published
#: 0.35 threshold — the served exchange solicited it anyway, on the strength of a
#: ``static-eligibility`` row.
#:
#: Deliberately NOT a member of :data:`~exchange.eligibility.ELIGIBILITY_STATUSES`: it is
#: never an answer, only a statement about who answers. See
#: :attr:`Deployment.eligibility_rows`.
TRUST_DEFERRED_ELIGIBILITY = "trust"

#: Every word a seller row's ``eligibility`` may carry: the three statuses, plus the deferral.
ELIGIBILITY_WORDS: tuple[str, ...] = (*ELIGIBILITY_STATUSES, TRUST_DEFERRED_ELIGIBILITY)


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
    #: The NAMED catalogue clusters this exchange assigns intents to, or ``None`` when the
    #: document states none. ``None`` and an empty list are the same behaviour today — nothing
    #: is assigned — but they are different STATEMENTS, and only the second one is a person
    #: saying "this exchange has no cluster vocabulary" on purpose. See
    #: :mod:`~exchange.retrieval.clusters` for why the vocabulary is configuration.
    intent_clusters: tuple[Any, ...] | None = None
    #: The catalogue snapshots this exchange checks claims against, ``{store_id: snapshot}``,
    #: or ``None`` when the document states none. ``None`` leaves
    #: :func:`~exchange.ranking.serving.catalog_of` on ``NoCatalogSnapshots``; an empty object
    #: BINDS an empty catalog, which is the same behaviour and a different statement — the
    #: same distinction :attr:`intent_clusters` draws, for the same reason.
    catalog: Mapping[str, Any] | None = None
    #: Where this deployment's trust service answers — the BASE url, not the ``/events`` path.
    #: ``None`` leaves :func:`trust_events_url` to fall back to :data:`ENV_TRUST_URL` and then
    #: :data:`DEFAULT_TRUST_URL`. It is stated in the document as well as in the environment
    #: because ``apps/exchange/compose.yaml`` forwards no variable it does not name, and the
    #: two it does name for this module are the two deployment keys — so the document is the
    #: only knob that reaches the shipped container today.
    trust_url: str | None = None

    @property
    def eligibility_rows(self) -> dict[str, str]:
        """The rows the PLATFORM answered R12 for. Rows deferred to trust are not among them.

        :data:`TRUST_DEFERRED_ELIGIBILITY` is not an eligibility STATUS — it is the platform
        declining to state one — so putting it in this mapping would hand
        :class:`~exchange.eligibility.StaticSellerEligibility` a status it does not recognise,
        and ``read_eligibility`` would answer ``unavailable: … unrecognised status 'trust'``
        for exactly the stores the document meant to ask trust about.
        """
        return {
            row.store_id: row.eligibility
            for row in self.sellers
            if row.eligibility != TRUST_DEFERRED_ELIGIBILITY
        }

    @property
    def trust_deferred_stores(self) -> frozenset[str]:
        """The stores this document leaves to the trust service (``"eligibility": "trust"``)."""
        return frozenset(
            row.store_id for row in self.sellers if row.eligibility == TRUST_DEFERRED_ELIGIBILITY
        )

    @property
    def registered_domains(self) -> dict[str, str]:
        return {
            row.store_id: row.registered_domain for row in self.sellers if row.registered_domain
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
            f"assumed; write one of {list(ELIGIBILITY_WORDS)}"
        )
    eligibility = str(row.get("eligibility"))
    if eligibility not in ELIGIBILITY_WORDS:
        raise DeploymentConfigurationError(
            f"{source}: seller {store_id!r} states eligibility {eligibility!r}, which is not "
            f"one of {list(ELIGIBILITY_WORDS)}; an unrecognised status has an unknown "
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
        elif not bid_endpoint.lower().startswith(("http://", "https://")):
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
    rows = (
        _require_mapping(stores, "trust_snapshot.stores", source)
        if isinstance(stores, Mapping)
        else document
    )

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
                f'including 0 and "false" — as an unreadable blacklist, which DENIES the '
                f"store; write true or false"
            )
        snapshot[str(store_id)] = dict(row)
    return snapshot


def _intent_clusters(raw: Any, source: str) -> tuple[Any, ...] | None:
    """The named cluster vocabulary this deployment states, validated here rather than later.

    ``None`` when the document names none — which is the pre-existing exchange, assigning
    nothing. A stated-but-malformed vocabulary is REFUSED rather than degraded to "none", for
    the reason this whole module exists: an exchange that shrugs at a typo here boots, answers
    ``201``, and shortlists nobody, because every store declines ``cluster_not_pursued`` and
    the operator has no line of output pointing at the row that was wrong.

    Built through :class:`~.retrieval.clusters.StaticIntentClusterCatalogue`, so the shape
    rules live next to the rule that reads them and there is exactly one spelling of "what is
    a catalogue cluster".
    """
    if raw is None:
        return None
    from .retrieval.clusters import StaticIntentClusterCatalogue  # noqa: PLC0415

    try:
        catalogue = StaticIntentClusterCatalogue.from_rows(raw)
    except (ValueError, TypeError) as exc:
        raise DeploymentConfigurationError(f"{source}: {exc}") from exc
    return catalogue.rows


def _catalog(raw: Any, source: str) -> Mapping[str, Any] | None:
    """The catalogue snapshots this deployment states, validated where they are read.

    ``None`` when the document names none — the pre-existing exchange, holding a snapshot for
    nobody and therefore checking no claim. A stated-but-malformed catalog is REFUSED rather
    than degraded to "none", because the degraded version is invisible: the exchange still
    answers ``201`` and still returns a shortlist, and the shortlist is simply empty for every
    intent that states a must-have.

    Built through :meth:`~.ranking.verification.StaticCatalogSnapshots.from_document`, so the
    grammar sits beside the code that walks a snapshot rather than in a second copy here —
    the same arrangement :func:`_intent_clusters` uses.
    """
    if raw is None:
        return None
    from .ranking.verification import StaticCatalogSnapshots  # noqa: PLC0415

    try:
        catalog = StaticCatalogSnapshots.from_document(raw)
    except (ValueError, TypeError) as exc:
        raise DeploymentConfigurationError(f"{source}: {exc}") from exc
    return catalog.snapshots


def parse_deployment(document: Any, *, source: str) -> Deployment:
    """Validate one deployment document. Raises rather than degrading."""
    body = _require_mapping(document, "the deployment document", source)

    raw_sellers = body.get("sellers", ())
    if not isinstance(raw_sellers, Sequence) or isinstance(raw_sellers, (str, bytes)):
        raise DeploymentConfigurationError(
            f"{source}: 'sellers' must be a JSON array, got {type(raw_sellers).__name__}"
        )
    if len(raw_sellers) > MAX_DEPLOYMENT_SELLERS:
        raise DeploymentConfigurationError(
            f"{source}: 'sellers' names {len(raw_sellers)} rows; this exchange registers at "
            f"most {MAX_DEPLOYMENT_SELLERS}. The document is parsed on the request path"
        )
    sellers = tuple(_seller_row(raw, index, source) for index, raw in enumerate(raw_sellers))

    # `Counter`, not a `.count()` inside a comprehension: the latter rebuilt the id list once
    # per row and measured 14.72s at 20,000 sellers, on the request path.
    seen = Counter(row.store_id for row in sellers)
    duplicated = sorted(store_id for store_id, count in seen.items() if count > 1)
    if duplicated:
        raise DeploymentConfigurationError(
            f"{source}: sellers names {duplicated} more than once; one store cannot have two "
            f"registry rows, because the later one would silently decide its eligibility, its "
            f"registered domain and where its bids are solicited from"
        )

    snapshot = (
        None
        if body.get("trust_snapshot") is None
        else _trust_snapshot(body["trust_snapshot"], source)
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

    trust_url = body.get("trust_url")
    if trust_url is not None:
        trust_url = str(trust_url).strip() or None
        if trust_url is not None and not trust_url.lower().startswith(("http://", "https://")):
            raise DeploymentConfigurationError(
                f"{source}: trust_url {trust_url!r} is not an http(s) URL; this is the base "
                f"address of the trust service, whose {TRUST_EVENTS_PATH} door the exchange "
                f"appends every auction transition to. Refused here rather than at the first "
                f"emit, because that failure is swallowed by design and would be invisible"
            )

    return Deployment(
        source=source,
        sellers=sellers,
        trust_snapshot=snapshot,
        checkout_mode=checkout_mode,
        intent_clusters=_intent_clusters(body.get("intent_clusters"), source),
        catalog=_catalog(body.get("catalog"), source),
        trust_url=trust_url,
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

    if len(text) > MAX_DEPLOYMENT_BYTES:
        raise DeploymentConfigurationError(
            f"{source}: the deployment document is {len(text)} bytes; this exchange reads at "
            f"most {MAX_DEPLOYMENT_BYTES}. It is parsed on the request path, so its size is "
            f"time a buyer waits"
        )

    try:
        document = json.loads(text)
    except Exception as exc:
        # NOT `except ValueError`. `json.loads` on deeply nested input raises RecursionError,
        # which is not a ValueError and therefore escaped this function entirely — measured as
        # an HTTP **500** on every request to both served POST routes, rather than the 503 this
        # module documents. A document this exchange cannot parse is a misconfiguration
        # whatever the parser raised on it.
        raise DeploymentConfigurationError(
            f"{source}: not valid JSON ({type(exc).__name__}: {exc})"
        ) from exc
    return parse_deployment(document, source=source)


# =====================================================================================
# The outbound bid client — R10's `POST /v1/bid-requests`
# =====================================================================================
#: What the exchange calls a buyer who named no profile.
#:
#: A pseudonym is an opaque, rotating handle carrying no identity (R13) — it is the ONE thing
#: a ``BuyerProfile`` requires, and the exchange has to write something in it or the request is
#: not a ``BidRequest``. This mints one per auction, which rotates at least as often as the
#: published contract asks, and reveals nothing the solicitation did not already carry: the
#: ``auction_id`` is a field of the very same body.
ANONYMOUS_PSEUDONYM_PREFIX = "anon"


def solicitation_profile(profile: Any, *, auction_id: str) -> dict[str, Any]:
    """The ``BuyerProfile`` this solicitation carries, repaired where the EXCHANGE broke it.

    Stated exactly, because the first version of this docstring said "always a valid one" and
    that was measured false. What this guarantees is that **the exchange's own coercion no
    longer produces a body the published contract rejects** — the ``{}`` it used to write for
    every buyer who named no profile. What it does not and must not do is rewrite a profile the
    buyer DID state: ``ProfileBuckets`` is ``extra="forbid"``, so an undeclared bucket key, a
    bucket of the wrong type, or an extra field beside ``pseudonym``/``buckets`` still makes
    the store answer ``422``. Measured, one store, one auction each::

        omitted                201  fallback=False  reason=None                shortlist 1
        empty-object           201  fallback=False  reason=None                shortlist 1
        undeclared-bucket-key  201  fallback=True   reason='store_refused:422' shortlist 0
        extra-profile-field    201  fallback=True   reason='store_refused:422' shortlist 0

    Those three are the buyer's own statement and the exchange declines to invent a different
    one — which is bearable only because of the other half of this repair: the refusal reads
    ``store_refused:422`` rather than ``no_response``, so the caller can see that the profile
    it sent is what lost the auction. ``intent`` is treated the same way one layer up: the
    route takes whatever shape a buyer service sends and lets the reader of the field decide
    what it means.

    ``CreateAuctionRequest.profile`` is ``dict | None``: the buyer service may omit it, and
    the published ``BidRequest`` may not. This used to be written as ``profile if
    isinstance(profile, Mapping) else {}``, and ``{}`` is not a ``BuyerProfile`` — it states
    neither ``pseudonym`` nor ``buckets``, both required. Measured against the real store
    agent, which validates the body against the pinned model::

        POST /v1/bid-requests, profile={} -> 422
          {"detail":[{"type":"missing","loc":["body","profile","pseudonym"],
                      "msg":"Field required","input":{}},
                     {"type":"missing","loc":["body","profile","buckets"], ...}]}

    Every store on the roster answered that 422, and the exchange reported all of them as
    ``fallback_reason: "no_response"`` — a schema violation the exchange itself committed,
    reported as the stores' silence.

    Nothing about the BUYER is invented here. The buckets stay exactly as the caller wrote
    them, and empty when there were none: an empty bucket set says "this exchange knows
    nothing about this shopper", which is true, and it is what a store's learning grid reads
    as "no segment". Only the handle is minted, because a handle is a name and not a fact.
    """
    source: Mapping[str, Any] = profile if isinstance(profile, Mapping) else {}
    out = dict(source)

    pseudonym = out.get("pseudonym")
    if not isinstance(pseudonym, str) or not pseudonym.strip():
        out["pseudonym"] = (
            f"{ANONYMOUS_PSEUDONYM_PREFIX}-{auction_id}"
            if auction_id
            else (ANONYMOUS_PSEUDONYM_PREFIX)
        )

    if not isinstance(out.get("buckets"), Mapping):
        # Repaired rather than passed through, for the same reason the pseudonym is: a
        # `buckets` the contract cannot read costs the auction every bid, and the exchange
        # knows the difference between "the buyer stated no buckets" and "the buyer stated
        # buckets this exchange dropped" — it is the first one.
        out["buckets"] = {}
    return out


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

    **"Loud in the entry" was not true of the status code, and that is what
    :meth:`_refusal` fixes.** This method used to map every non-200 to ``None``, which
    ``collect_bids`` labels ``no_response`` — so a store that DECLINED with the contract's own
    ``204``, a store that rejected the request body, and a store that was switched off were
    one indistinguishable fact in the answer. Measured on this tree, one real store agent,
    ``POST /auctions`` carrying no ``profile``::

        agent, profile={} -> 422 {"detail":[{"type":"missing",
                                  "loc":["body","profile","pseudonym"], ...}]}
        entries -> [{"store_id": "store-alpha", "fallback": true,
                     "fallback_reason": "no_response"}]

    The 422 there was the exchange's OWN doing (see :meth:`for_auction`), and it was reported
    as the store's silence. Both halves are closed: the request is valid now, and a refusal
    that still happens is named.
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
        """A view of this solicitor bound to one auction's ``BidRequest`` fields.

        ``profile`` is passed through :func:`solicitation_profile`, which is the difference
        between a solicitation a store can answer and one it must refuse: this used to write
        ``{}`` whenever the buyer named no profile, and ``{}`` is not a ``BuyerProfile``.
        """
        bound = HttpBidSolicitor(self._endpoints, timeout=self._timeout, client=self._http_client())
        bound._context = {
            "auction_id": str(auction_id),
            "intent": intent if isinstance(intent, Mapping) else {},
            "profile": solicitation_profile(profile, auction_id=str(auction_id)),
            "respond_by": _rfc3339(respond_by),
        }
        return bound

    def solicit(self, store: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Ask one store, and answer in the shape ``collect_bids`` reads.

        The reply is read through a **bounded** stream rather than with ``response.json()``,
        and that is not defensive tidiness — it was measured. A store agent is a third party
        (a Tier-2 store does not run our code), the auction path is unauthenticated, and
        ``apps/exchange/compose.yaml`` caps the exchange at ``mem_limit: 256m``. With
        ``response.json()``, against an agent answering a single 64 MiB body::

            baseline peak RSS: 115.9 MiB
            answer accepted: True   serialized size: 64.0 MiB
            peak RSS after one solicitation: 464.6 MiB

        — one store, one auction, and the container is over its limit twice over; a 500-store
        roster (``MAX_ROSTER_ENTRIES``) multiplies it. Bounded, against an agent STREAMING
        512 MiB (streamed so the number below is this side's cost and not the probe's)::

            baseline peak RSS: 52.3 MiB
            answer accepted: False
            peak RSS after one solicitation: 66.8 MiB

        The store falls back to its list price like any other store that did not answer
        usefully (R10), and the auction is unharmed.
        """
        store_id = str(store.get("store_id") or "")
        endpoint = self._endpoints.get(store_id)
        if not endpoint:
            # Not an error: a Tier-0 store, or one the registry holds no agent for, is
            # represented at list price rather than asked a question nobody is home to hear.
            return None

        payload = dict(self._context) or {"auction_id": "", "intent": {}, "profile": {}}
        deadline = time.monotonic() + MAX_SOLICIT_WALL_CLOCK_SECONDS
        try:
            with self._http_client().stream(
                "POST", endpoint, json=payload, timeout=self._timeout
            ) as response:
                if response.status_code != 200:
                    return self._refusal(store_id, response)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BID_RESPONSE_BYTES:
                        # Stop READING, not merely stop using: a cap applied after the body is
                        # in memory is not a cap. Leaving the block closes the connection.
                        return None
                    if time.monotonic() >= deadline:
                        # The byte cap alone does not bound TIME: httpx's timeout resets on
                        # every chunk, so a store dripping one byte at a time never trips it.
                        # See MAX_SOLICIT_WALL_CLOCK_SECONDS.
                        return None
        except Exception:
            return None

        try:
            bid = json.loads(bytes(body))
        except ValueError:
            return None
        if not isinstance(bid, Mapping):
            return None
        # `store_id` and `received_at` are stamped authoritatively by `fanout._stamped`; they
        # are named here only so the reply is a well-formed response envelope.
        return {"store_id": store_id, "bid": dict(bid)}

    __call__ = solicit

    @staticmethod
    def _refusal(store_id: str, response: Any) -> dict[str, Any]:
        """A store's refusal, in the shape ``collect_bids`` can name it by.

        A record rather than ``None``, and that is the whole repair: ``None`` means "nothing
        arrived", and something did arrive — the store answered, and its answer was no. It
        carries no ``bid``, so every downstream rule that decides on a bid decides exactly as
        it did before; the only thing that changes is the sentence the operator reads.

        ``204`` is read as the store agent contract's DECLINE and its reason is taken from the
        header that contract publishes for it (a 204 has no body to carry one in). Any other
        status is a refusal of the SOLICITATION, and the status is the detail because that is
        the one fact the exchange actually holds: a 422 means this exchange sent something the
        store could not read, which is a defect on this side, and a 503 means the agent is
        down, which is a defect on that one. Reported as ``no_response``, they were the same
        sentence and neither operator could act on it.

        The header name is restated here rather than imported from ``store_agent``: it is a
        published contract detail (``store-agent.openapi.json`` documents it on the 204), and
        the exchange does not import the seller's package — the image it ships does not even
        contain it.
        """
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 204:
            headers = getattr(response, "headers", None)
            stated = None
            if headers is not None:
                try:
                    stated = headers.get(DECLINE_REASON_HEADER)
                except Exception:
                    stated = None
            reason = refusal_reason(STORE_DECLINED_REASON, stated)
        else:
            reason = refusal_reason(STORE_REFUSED_REASON, status)
        return {"store_id": store_id, REFUSAL_FIELD: reason}

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
# The trust-ledger seam — D16's `POST /events`, written through shared machinery
# =====================================================================================
#: How the wiring-time line names where the endpoint came from. The shared resolver answers in
#: tokens; an operator reads a sentence.
_ENDPOINT_SOURCES: Mapping[str, str] = {
    SOURCE_STATED: "the deployment document's trust_url",
    SOURCE_ENVIRONMENT: f"the {ENV_TRUST_URL} environment variable",
    SOURCE_DEFAULT: "the built-in default",
}


class HttpTrustLedgerSink(InMemoryLedgerSink, TrustLedgerPublisher):
    """A ledger sink that actually leaves the process: it POSTs to trust's ``POST /events``.

    This is the join T-150 names. The exchange's state machine has always recorded every
    transition — into :class:`~exchange.auction.ledger.InMemoryLedgerSink`, a list discarded
    with the app — so ``apps/trust/src/events``' hash chaining, once-only landing and replay
    were grading a stream **no served request produced**. The sim harness and the e2e support
    module each bridged their own events across by hand, which is precisely why nothing about
    the served wiring ever had to change for those to pass.

    **It is exactly the two halves and nothing else, which is why it is this short.** The
    in-process readback is the exchange's own — three live tests read
    ``app.state.auction_machine.ledger.sink.kinds`` back off the served app
    (``test_auction.py``'s fan-out test and both parameters of its over-long-identifier test),
    and ``InMemoryLedgerSink.for_auction`` is a published read — so this SUBCLASSES the stub
    rather than replacing it, and that is load-bearing. The delivery half is
    :class:`~proxyshop_support.trust_ledger.TrustLedgerPublisher`: shared machinery, because
    this was the first cross-process ledger writer in the repository and leaving it private to
    one composition root would have meant a second copy the moment merchant's ``HANDOFF`` ring
    needed draining. Keeping both keeps the two properties independent: what the auction
    recorded is still readable when the trust service is not.

    **A trust service that is down must not fail an auction, and must not fail it quietly
    either.** ``LedgerRecorder.record`` already swallows a raising sink — but into
    ``LedgerRecorder.failures``, an unbounded list on an unauthenticated path. So the transport
    failure is caught in the publisher instead, into a bounded ring, and reported when the
    delivery CONDITION changes rather than per event: one ``ERROR`` when events stop landing,
    one ``INFO`` when they land again. See the publisher's header for why that replaced the
    one-shot ``WARNING`` this class shipped with, and :meth:`~proxyshop_support.trust_ledger.
    TrustLedgerPublisher.status` for the reading that outlives the log line.
    """

    def __init__(self, url: str, *, timeout: float = DEFAULT_LEDGER_TIMEOUT_SECONDS) -> None:
        # Both bases explicitly, rather than one cooperative `super().__init__()`: they are two
        # independent halves that happen to be joined here, and a reader should not have to
        # work out an MRO to see that both ran.
        InMemoryLedgerSink.__init__(self)
        TrustLedgerPublisher.__init__(
            self,
            url,
            timeout=timeout,
            # T-264's redactor, not the shared module's plainer default: a collaborator's own
            # exception message can carry a default `__repr__` address that nothing quoted.
            describe_failure=describe_exception,
            log=_log,
            subject="exchange ledger",
        )

    def emit(self, event: Mapping[str, Any]) -> None:
        # Record locally FIRST. The in-process readback is what the served response and the
        # tests above are built on, and it must not become conditional on a network hop.
        InMemoryLedgerSink.emit(self, event)
        self.publish(event)


def bind_ledger_sink(
    base_url: str | None = None, env: Mapping[str, str] | None = None
) -> HttpTrustLedgerSink:
    """Build the sink, and say at ``INFO`` where this process will write its audit trail.

    **This line is the whole of what a required dependency owes an operator at wiring time**,
    and it is why nothing on the auction path warns any more. "Which trust service is this
    exchange writing to, and did anybody choose it" is a CONFIGURATION question: it has a
    settled answer the moment the seam is bound, it does not change afterwards, and start-up
    is where the person who can act on it is looking. The old shape could only answer it from
    a runtime warning, which meant the answer arrived after something had already gone wrong
    and never arrived at all for the deployments that were fine.

    ``INFO`` and not ``WARNING``, deliberately: a deployment that states no ``trust_url`` has
    not made a mistake. The default is the compose service name, which is the correct address
    in the deployment this repository ships — there is nothing to warn about, only something
    to state. A ``trust_url`` that is unusable *as configuration* is a different matter and is
    already refused at parse, loudly, by :func:`read_deployment`.
    """
    url, source = trust_endpoint(base_url, env)
    _log.info(
        "exchange ledger: auction transitions will be appended to the trust service at %s "
        "(from %s). Delivery failures are reported when they start and when they stop",
        url,
        _ENDPOINT_SOURCES.get(source, source),
    )
    return HttpTrustLedgerSink(url)


def default_ledger_sink(env: Mapping[str, str] | None = None) -> InMemoryLedgerSink:
    """The sink an exchange nobody has configured writes its transitions through.

    The DEFAULT has to be the trust-backed one, not an opt-in. T-150's gate builds the app
    ``exchange.main.create_app()`` returns with no deployment document and no environment at
    all — which is also what ``docker compose up`` starts, since neither ``EXCHANGE_DEPLOYMENT``
    variable has a value there — so a producer reachable only through configuration is a
    producer no deployment in this repository reaches.
    """
    return bind_ledger_sink(env=env)


# =====================================================================================
# The trust-SNAPSHOT seam — R12's `GET /snapshot`, read by the exchange at last
# =====================================================================================
#: The published read door. ``apps/trust/src/snapshot/routes.py`` serves it, it is declared in
#: ``packages/contracts/openapi/trust.openapi.json`` as "Every store's snapshot, for exchange
#: consumption", and until T-303 the exchange had no client for it — the server half landed in
#: T-261 whose own header says "no such client exists yet".
TRUST_SNAPSHOT_PATH = "/snapshot"

#: How long ONE snapshot read may hold the calling thread.
#:
#: Read inline on ``POST /auctions`` and on ``POST /auctions/{id}/accept``, once per refresh
#: window rather than once per store (see :class:`HttpTrustSnapshot`), so a buyer feels this at
#: most once per request. Larger than the ledger's 0.5s because the response is every store's
#: snapshot rather than one small event, and still finite: a trust service that has not
#: answered in a second is down, not thinking, and a down trust service must deny rather than
#: hold an auction open.
DEFAULT_TRUST_SNAPSHOT_TIMEOUT_SECONDS = 1.0

#: How long a snapshot that WAS read is served before it is revalidated.
#:
#: A cache is not an optimisation here, it is a correctness requirement: :meth:`SellerEligibility
#: .check` is asked once per rostered store and three times per purchase (solicitation, ranking,
#: checkout), so an uncached reader would turn one auction into a dozen round trips to trust and
#: could answer differently for two stores in the same auction.
TRUST_SNAPSHOT_REFRESH_SECONDS = 30.0

#: How long a FAILED read is remembered before the next attempt.
#:
#: Deliberately much shorter than the refresh window: a failure denies every store, so an
#: exchange must notice a trust service coming back quickly. Non-zero because ``check`` is
#: driven from an unauthenticated request path — without it, one auction against a trust
#: service that is down is one connection attempt per rostered store, which is a socket storm
#: anybody can start by posting a large roster in a loop.
TRUST_SNAPSHOT_RETRY_SECONDS = 5.0


def trust_snapshot_endpoint(
    base_url: str | None = None, env: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """``(url, source)`` for the trust service's snapshot door.

    Resolved THROUGH :func:`~proxyshop_support.trust_ledger.trust_endpoint` so the precedence
    — the document's ``trust_url``, then :data:`ENV_TRUST_URL`, then :data:`DEFAULT_TRUST_URL`
    — has exactly one implementation in the repository and the write door and the read door
    can never disagree about which trust service this exchange is talking to. That function
    takes no path argument today and appends :data:`TRUST_EVENTS_PATH` itself, so the suffix is
    swapped here rather than the precedence being forked; giving it a ``path`` parameter
    belongs beside the write client in ``proxyshop_support`` and is reported rather than done
    here.
    """
    events_url, source = trust_endpoint(base_url, env)
    base = (
        events_url[: -len(TRUST_EVENTS_PATH)]
        if events_url.endswith(TRUST_EVENTS_PATH)
        else events_url
    )
    return f"{base}{TRUST_SNAPSHOT_PATH}", source


class HttpTrustSnapshot:
    """Reads trust's published ``GET /snapshot``, caching on the version trust publishes.

    This is the transport half of the R12 read; the rule that turns a snapshot into an
    eligibility answer is :class:`~exchange.eligibility.trust_backed.TrustBackedSellerEligibility`,
    which holds one of these as a callable. Split for the reason ``HttpTrustLedgerSink`` and
    ``TrustLedgerPublisher`` are split: the port's package must stay transport-free, and this
    module is where this service's outbound clients already live (``HttpBidSolicitor``,
    ``HttpTrustLedgerSink``).

    **Caching, and what it is allowed to do when trust is down.** T-064 acceptance 3 specifies
    that the exchange caches the snapshot and refreshes on a version bump; the served route
    publishes that version as an ``ETag`` (and, spelled out, ``X-Trust-Snapshot-Version``), so a
    revalidation is a conditional ``GET`` and a ``304`` costs a header exchange. What it may
    NOT do is serve a snapshot it failed to revalidate: an exchange that cannot reach trust
    knows nothing current about any store, and R12 says knowing nothing denies. So a failed
    refresh DISCARDS the cache and every store reads ``unavailable`` until trust answers again
    — the same direction ``trust.snapshot.delisting`` fails in, and the opposite of the
    "probably still fine" reading that would quietly keep asking a store trust has just
    delisted.

    **No response-size bound, deliberately, and it is the one place this differs from
    ``HttpBidSolicitor``.** That client streams with a hard :data:`MAX_BID_RESPONSE_BYTES` cap
    because it reads from a STORE AGENT — a third party with an incentive, reached at an
    address a seller supplied. This reads the deployment's own trust service, named by
    ``trust_url``/:data:`ENV_TRUST_URL`/:data:`DEFAULT_TRUST_URL` and never by anything in a
    request, and the body is legitimately every store's snapshot: a marketplace with more
    stores has a larger one, without bound and without anything wrong. A cap invented here
    would therefore not protect the exchange from an adversary, it would become the day the
    marketplace outgrows it a refusal of EVERY store — the fail-closed path firing on honest
    traffic, which is worse than the hazard it would be guarding.

    **Failure is never silent and never chatty**, exactly as
    :class:`~proxyshop_support.trust_ledger.TrustLedgerPublisher` is: one ``ERROR`` the moment
    reads stop landing, one ``INFO`` the moment they land again, nothing at all in a steady
    state. :meth:`status` renders the whole condition for a caller who wants it without a log
    line.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TRUST_SNAPSHOT_TIMEOUT_SECONDS,
        refresh_seconds: float = TRUST_SNAPSHOT_REFRESH_SECONDS,
        retry_seconds: float = TRUST_SNAPSHOT_RETRY_SECONDS,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.url = str(url)
        self._timeout = float(timeout)
        self._refresh = float(refresh_seconds)
        self._retry = float(retry_seconds)
        self._monotonic = monotonic
        self._client: Any = None
        self._rows: Mapping[str, Any] | None = None
        self._version: str | None = None
        self._read_at: float | None = None
        self._failure: str | None = None
        self._failed_at: float | None = None
        self._reads = 0
        self._readable: bool | None = None

    # -- the seam the eligibility source calls ----------------------------------------
    def __call__(self) -> Mapping[str, Any]:
        """The current snapshot, or raise :class:`TrustSnapshotUnavailable`."""
        now = float(self._monotonic())
        if (
            self._rows is not None
            and self._read_at is not None
            and now - self._read_at < self._refresh
        ):
            return self._rows
        if (
            self._failure is not None
            and self._failed_at is not None
            and now - self._failed_at < self._retry
        ):
            # Inside the retry window a second store in the same auction gets the SAME answer
            # without a second socket. Re-raised rather than remembered as a decision, so the
            # denial reason still names the failure that produced it.
            raise TrustSnapshotUnavailable(self._failure)
        try:
            rows, version = self._fetch()
        except TrustSnapshotUnavailable as exc:
            self._record_failure(str(exc), now)
            raise
        except Exception as exc:  # a blanket catch IS the fail-closed rule
            failure = describe_exception(exc)
            self._record_failure(failure, now)
            raise TrustSnapshotUnavailable(failure) from exc
        self._record_read(rows, version, now)
        return rows

    def status(self) -> dict[str, Any]:
        """The whole delivery condition, readable after the log line has scrolled away."""
        return {
            "url": self.url,
            "readable": self._readable,
            "reads": self._reads,
            "snapshot_version": self._version,
            "stores": None if self._rows is None else len(self._rows),
            "last_failure": self._failure,
        }

    # -- plumbing ---------------------------------------------------------------------
    def _fetch(self) -> tuple[Mapping[str, Any], str | None]:
        headers = {"accept": "application/json"}
        if self._version and self._rows is not None:
            # The conditional GET T-064 acceptance 3 asks for: a version that has not moved
            # costs a 304 and the cached document stands.
            headers["if-none-match"] = self._version
        response = self._http_client().get(self.url, headers=headers)
        if response.status_code == 304 and self._rows is not None:
            return self._rows, self._version
        if response.status_code != 200:
            raise TrustSnapshotUnavailable(f"{self.url} answered HTTP {response.status_code}")
        document = response.json()
        rows = snapshot_rows(document)
        if rows is None:
            raise TrustSnapshotUnavailable(
                f"{self.url} answered {describe(document)}, which is not a trust snapshot"
            )
        version = response.headers.get("etag") or response.headers.get("x-trust-snapshot-version")
        return rows, (str(version) if version else None)

    def _record_read(self, rows: Mapping[str, Any], version: str | None, now: float) -> None:
        self._rows, self._version, self._read_at = rows, version, now
        self._failure, self._failed_at = None, None
        self._reads += 1
        if self._readable is not True:
            if self._readable is False:
                _log.info(
                    "exchange eligibility: %s is answering again (%d store(s), version %s). "
                    "R12 is being read from the trust snapshot once more",
                    self.url,
                    len(rows),
                    version or "<unpublished>",
                )
            self._readable = True

    def _record_failure(self, failure: str, now: float) -> None:
        # The cache is DISCARDED, not held: see the class docstring. A snapshot that could not
        # be revalidated is not evidence that a store is still listed.
        self._rows, self._read_at, self._version = None, None, None
        self._failure, self._failed_at = failure, now
        if self._readable is not False:
            _log.error(
                "exchange eligibility: %s stopped answering (%s). The exchange cannot read the "
                "trust snapshot, so R12 denies EVERY store 'unavailable' until it can — "
                "status() now reads readable=false. The next read that lands is logged; the "
                "ones in between are not",
                self.url,
                failure,
            )
            self._readable = False

    def _http_client(self) -> Any:
        """One pooled client for this reader, built on first use.

        Deferred for the reason :meth:`HttpBidSolicitor._http_client`'s is: constructing an
        eligibility source — which a config check or a test does — must open no sockets, and
        ``httpx`` should be imported only by a deployment that actually reaches out.
        """
        if self._client is None:
            import httpx  # noqa: PLC0415 — see the docstring

            self._client = httpx.Client(timeout=self._timeout)
        return self._client


class LiveTrustSnapshot(Mapping[str, Any]):
    """The RANKING gate's live view of the same snapshot the eligibility source reads.

    R12 is consulted at three gates, and the middle one does not read the eligibility port:
    ``rank()`` derives the blacklist from the ``trust_snapshot`` mapping it is handed
    (:func:`exchange.ranking.filters.blacklist_reason`), deliberately, so one auction does not
    read the eligibility backend once per candidate inside the synchronous window R10 bounds.
    That mapping is resolved by :func:`exchange.ranking.serving.trust_snapshot_of` off
    ``app.state.trust_snapshot``, and until this class existed the ONLY thing that could bind
    it was a literal ``trust_snapshot`` key typed into the deployment document. A deployment
    that did not type one out ranked against ``{}`` — and an empty snapshot excludes EVERY
    store ``blacklist_unreadable``, so the fail-closed path fired on honest traffic and the
    shortlist was empty no matter who bid.

    So this is a ``Mapping`` and not a callable, because that is the shape the ranking reads,
    and it is backed by the SAME :class:`HttpTrustSnapshot` the eligibility source holds: one
    reader, one cache, one version, one round trip per refresh window. Two readers would be two
    opinions about the same store inside one auction, which is the thing
    :func:`exchange.ranking.serving.registered_domains_of` is careful about one seam over.

    **Unreadable is empty, and empty denies.** A read that fails yields no rows, so
    :func:`~exchange.ranking.filters.blacklist_reason` finds no row for any store and answers
    ``blacklist_unreadable`` — the same direction, and the same denial, that
    :class:`~exchange.eligibility.trust_backed.TrustBackedSellerEligibility` answers at the
    other two gates for the same failure. The exception is swallowed HERE rather than at the
    gate because ``rank()`` is not written to catch one, and an exception escaping the ranker
    would turn a trust outage into a 500 on ``POST /auctions`` instead of an empty shortlist.
    """

    def __init__(self, reader: Callable[[], Any]) -> None:
        self._reader = reader

    def _rows(self) -> Mapping[str, Any]:
        try:
            rows = snapshot_rows(self._reader())
        except Exception:  # noqa: BLE001 - a blanket catch IS the fail-closed rule
            return {}
        return {} if rows is None else rows

    def __getitem__(self, store_id: str) -> Any:
        return self._rows()[store_id]

    def __iter__(self) -> Any:
        return iter(self._rows())

    def __len__(self) -> int:
        return len(self._rows())

    def __repr__(self) -> str:
        url = getattr(self._reader, "url", None)
        return f"{type(self).__name__}({url!r})" if url else f"{type(self).__name__}(...)"


def bind_seller_eligibility(
    base_url: str | None = None, env: Mapping[str, str] | None = None
) -> TrustBackedSellerEligibility:
    """Build the R12 source, and say at ``INFO`` where this process will read trust from.

    The wiring-time twin of :func:`bind_ledger_sink`, for the same reason and at the same
    level: "which trust service decides who this exchange is allowed to ask, and did anybody
    choose it" is a CONFIGURATION question with a settled answer the moment the seam is bound,
    and ``INFO`` rather than ``WARNING`` because a deployment that states no ``trust_url`` has
    not made a mistake — the default is the compose service name.
    """
    reader, url = bind_trust_snapshot_reader(base_url, env)
    return TrustBackedSellerEligibility(reader, source=f"the trust snapshot at {url}")


def bind_trust_snapshot_reader(
    base_url: str | None = None, env: Mapping[str, str] | None = None
) -> tuple[HttpTrustSnapshot, str]:
    """The ONE snapshot reader this process reads R12 from, and the url it reads.

    Split out of :func:`bind_seller_eligibility` because the reader now feeds two things and
    must not become two readers: the eligibility port at the solicitation and checkout gates
    (through :class:`~exchange.eligibility.trust_backed.TrustBackedSellerEligibility`) and the
    ranking gate's mapping (through :class:`LiveTrustSnapshot`). Sharing the object is what
    keeps one auction to one round trip per refresh window, and — the part that is correctness
    rather than cost — what stops the middle gate from reading a different snapshot version
    than the two either side of it.
    """
    url, source = trust_snapshot_endpoint(base_url, env)
    _log.info(
        "exchange eligibility: R12 will be read from the trust service's snapshot at %s "
        "(from %s). A store trust holds no row for is denied, and a trust service that "
        "cannot be read denies every store",
        url,
        _ENDPOINT_SOURCES.get(source, source),
    )
    return HttpTrustSnapshot(url), url


def default_seller_eligibility(
    env: Mapping[str, str] | None = None,
) -> TrustBackedSellerEligibility:
    """The R12 source an exchange nobody has configured consults.

    The DEFAULT has to be the trust-backed one, for exactly the reason
    :func:`default_ledger_sink`'s does: ``exchange.main.create_app()`` with no deployment
    document and no environment is both what T-303's gate builds and what ``docker compose up``
    starts, so a source reachable only through configuration is a source no deployment in this
    repository reaches — which is the whole finding (``configure_auctions(app,
    eligibility=...)`` had no product caller, and the served exchange therefore asked nobody).

    It does NOT weaken the invariant this module is built on. An exchange that cannot reach
    trust reads no snapshot, a store with no snapshot row is ``UNAVAILABLE``, and ``UNAVAILABLE``
    denies at all three gates — so an unconfigured exchange still refuses everything, and now
    it refuses with a reason that names the trust service it could not reach instead of naming
    a deterministic double nobody deployed.
    """
    return bind_seller_eligibility(env=env)


# =====================================================================================
# Binding
# =====================================================================================
def bind_eligibility(
    app: Any, deployment: Deployment | None, env: Mapping[str, str] | None = None
) -> bool:
    """Bind this app's R12 source unless it already has one. Returns whether it bound.

    **The ladder, and why it is in this order.** All three rungs are fail-closed; they differ
    only in who is answering.

    1. ``sellers``, every row stating a STATUS — the platform's own registry, answered by a
       person. It stays FIRST even when the document also states a ``trust_snapshot``, because
       the module header says why: R12's eligibility and the trust snapshot are two independent
       reads by design, the ranking calls them ``blacklisted_store`` and
       ``blacklist_unreadable`` separately, and a composition root that manufactured one from
       the other would be inventing an answer the trust service never gave.
       ``test_a_store_the_ranking_excluded_cannot_be_bought`` is exactly that distinction: it
       marks ``s1`` blacklisted in the SNAPSHOT and requires the store to still be solicited
       and still be excluded by the ranking.
    2. ``trust_snapshot`` with no ``sellers`` — a document that states trust's verdict and no
       registry of its own. Then trust's verdict IS the eligibility answer, read through
       :class:`~exchange.eligibility.trust_backed.TrustBackedSellerEligibility` rather than
       transcribed into rows: a store the snapshot delists is denied, and a store the snapshot
       does not carry is denied too.
    3. Neither, or no document at all — :func:`bind_seller_eligibility`, which reads trust's
       published ``GET /snapshot`` over HTTP. This is the rung T-303 is about: before it, the
       served exchange fell through to ``auction/routes.py``'s lazy
       ``StaticSellerEligibility()`` with no rows, and answered ``static-eligibility: <store>
       is unavailable`` for every store alive — which is not "consulted trust and refused", it
       is "asked nobody".

    **The rung that was missing, and it is why rung 1 now says "every row stating a status".**
    Rung 1 used to take ANY document with ``sellers``, and a document has to state its sellers
    to state their bid endpoints and registered domains at all. So the only deployment that
    ever reached rung 3 was one that named no sellers — an exchange that asks nobody to bid,
    ranks nothing and shortlists nobody. The trust engine was reachable only by a deployment
    with no traffic to apply it to, which is the same island one layer up. A row may now say
    ``"eligibility": "trust"`` (:data:`TRUST_DEFERRED_ELIGIBILITY`), and a document with any
    such row binds :class:`~exchange.eligibility.layered.LayeredSellerEligibility`: the stated
    rows answered by the platform, every deferred row — and every store the document never
    mentions — answered by the trust service.

    **And the ranking gate.** R12 is three gates, and the middle one does not read the
    eligibility port at all — it reads ``app.state.trust_snapshot`` (see
    :class:`LiveTrustSnapshot`). Until a live view of that existed, the ONLY thing that could
    bind it was a literal ``trust_snapshot`` key typed into the document, so every deployment
    that did not type one out consulted trust at two gates out of three and ranked against
    ``{}`` — which excludes every store ``blacklist_unreadable`` and empties the shortlist for
    honest and dishonest stores alike. So :func:`_bind_live_ranking_snapshot` now points that
    gate at the trust service on EVERY rung, sharing this function's reader when it built one.
    A document that DOES state a ``trust_snapshot`` keeps it untouched: that key is a person's
    statement and this function does not overrule one.

    Idempotent, and it never overwrites: an app a test or a deployment has already handed a
    source through ``configure_auctions`` keeps it. That matters more here than elsewhere
    because ``ensure_configured`` does NOT cache the "no deployment configured" answer, so this
    runs on every request until something is bound.
    """
    from .auction.routes import configure_auctions  # noqa: PLC0415 — see the import note

    if getattr(app.state, "seller_eligibility", None) is not None:
        return False

    stated = {} if deployment is None else deployment.eligibility_rows
    deferred = frozenset() if deployment is None else deployment.trust_deferred_stores

    if deployment is not None and stated and not deferred:
        configure_auctions(app, eligibility=StaticSellerEligibility(stated))
        _bind_live_ranking_snapshot(app, deployment, env=env)
        return True
    if deployment is not None and not deferred and deployment.trust_snapshot is not None:
        configure_auctions(
            app,
            eligibility=TrustBackedSellerEligibility(
                dict(deployment.trust_snapshot),
                source=f"the trust snapshot in {deployment.source}",
            ),
        )
        return True

    reader, url = bind_trust_snapshot_reader(
        None if deployment is None else deployment.trust_url, env
    )
    live = TrustBackedSellerEligibility(reader, source=f"the trust snapshot at {url}")
    configure_auctions(
        app,
        eligibility=(
            LayeredSellerEligibility(
                stated,
                live,
                source=f"the seller registry in {deployment.source}",
            )
            if stated and deployment is not None
            else live
        ),
    )
    _bind_live_ranking_snapshot(app, deployment, reader, env)
    return True


def _bind_live_ranking_snapshot(
    app: Any,
    deployment: Deployment | None,
    reader: Callable[[], Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Point the RANKING gate at the trust service unless a person already stated a snapshot.

    ``reader`` is :func:`bind_eligibility`'s, when that function built one — the two gates must
    read one snapshot, not two. On the rung where eligibility comes from a stated seller
    registry there is no such reader, and one is built here rather than the gate being left on
    ``{}``: the registry answers "may this store participate", the ranking's blacklist read
    answers "has trust delisted it", and the module header is explicit that those are two
    independent questions. A document that answers the first says nothing about the second.

    Deliberately silent when the document states ``trust_snapshot``: that key is the operator's
    own statement about who is blacklisted, ``configure_exchange`` binds it a few lines later,
    and a live view installed here would take the ``unset('trust_snapshot')`` guard first and
    make the document's key unreadable.

    Silent too when something already bound one, because
    :func:`exchange.ranking.serving.trust_snapshot_of` installs ``{}`` on first read: an app
    that has already served a ranking keeps what it answered with rather than changing its
    mind mid-process.
    """
    from .ranking.serving import configure_ranking  # noqa: PLC0415 — see the import note

    if deployment is not None and deployment.trust_snapshot is not None:
        return
    if getattr(app.state, "trust_snapshot", None) is not None:
        return
    if reader is None:
        reader, _url = bind_trust_snapshot_reader(
            None if deployment is None else deployment.trust_url, env
        )
    configure_ranking(app, trust_snapshot=LiveTrustSnapshot(reader))


def configure_exchange(
    app: Any, deployment: Deployment, env: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Bind everything ``deployment`` states that this app has not already been given.

    Composed out of the three published wiring seams — :func:`configure_auctions`,
    :func:`configure_ranking` and :func:`configure_accept` — rather than by assigning to
    ``app.state`` directly, so this module can never drift from what those functions mean (the
    accept seam, for one, also turns the process-wide registered-domain seam, and a
    hand-written assignment here would silently skip it).

    Returns the names it bound, so a caller can say what a deployment actually turned on.
    """
    from .accept.routes import InMemoryAuctionBids, configure_accept  # noqa: PLC0415
    from .auction.routes import configure_auctions  # noqa: PLC0415
    from .auction.state import AuctionStateMachine  # noqa: PLC0415
    from .ranking.serving import configure_ranking  # noqa: PLC0415

    bound: list[str] = []

    def unset(name: str) -> bool:
        return getattr(app.state, name, None) is None

    if unset("auction_machine"):
        # The ledger seam (T-150). Bound with NO key required, unlike every other branch here,
        # for two reasons. The document states only WHERE — `trust_url`, falling back to the
        # environment and then to `DEFAULT_TRUST_URL` — because an exchange that has to be told
        # to keep an audit trail is an exchange that ships without one.
        #
        # And binding it here rather than leaving it to `auction/routes.py::_machine` closes a
        # narrower hole than it looks: `accept/routes.py` has its own `_machine` accessor with
        # its own bare `AuctionStateMachine()` default, so an app whose FIRST auction request
        # is an accept — an auction another process created against a shared store — would
        # otherwise install a sink that goes nowhere and drop its `accepted` event. This hook
        # runs before both accessors in both routes.
        #
        # It is a whole machine because `configure_auctions` takes one; the store is the same
        # `InMemoryAuctionStore` either lazy default builds, so nothing else moves.
        configure_auctions(
            app, machine=AuctionStateMachine(ledger=bind_ledger_sink(deployment.trust_url))
        )
        bound.append("auction_machine")

    if bind_eligibility(app, deployment, env):
        bound.append("seller_eligibility")

    endpoints = deployment.bid_endpoints
    if endpoints and unset("bid_solicitor"):
        configure_auctions(app, solicitor=HttpBidSolicitor(endpoints))
        bound.append("bid_solicitor")

    if deployment.intent_clusters is not None and unset("intent_clusters"):
        # `is not None`, not truthiness: a document that states `"intent_clusters": []` has
        # said "this exchange has no cluster vocabulary", and binding the empty catalogue
        # records that decision on `app.state` instead of leaving the seam looking unwired.
        from .retrieval.clusters import StaticIntentClusterCatalogue  # noqa: PLC0415

        configure_auctions(
            app, clusters=StaticIntentClusterCatalogue(rows=deployment.intent_clusters)
        )
        bound.append("intent_clusters")

    if deployment.trust_snapshot is not None and unset("trust_snapshot"):
        configure_ranking(app, trust_snapshot=dict(deployment.trust_snapshot))
        bound.append("trust_snapshot")

    if deployment.catalog is not None and unset("ranking_catalog"):
        # `is not None`, not truthiness, for the same reason `intent_clusters` above is: a
        # document stating `"catalog": {}` has said "this exchange holds no snapshot for
        # anybody", and binding the empty source records that decision rather than leaving the
        # seam looking unwired. Bound through `configure_ranking`, never by assigning to
        # `app.state.ranking_catalog`, so this module cannot drift from what that seam means.
        from .ranking.verification import StaticCatalogSnapshots  # noqa: PLC0415

        configure_ranking(app, catalog=StaticCatalogSnapshots(deployment.catalog))
        bound.append("ranking_catalog")

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
    if deployment is None:
        # An exchange nobody configured still ASKS (T-303). The R12 source is bound here rather
        # than left to `auction/routes.py::_eligibility`'s lazy `StaticSellerEligibility()`,
        # which is a deterministic double built with no rows: it answers "unavailable" for
        # every store alive, which reads as a trust verdict and is not one. The trust-backed
        # default denies just as completely when trust is unreachable — see
        # `default_seller_eligibility` — so the invariant this module may not break is intact,
        # and a denial now names the trust service instead of the double. `bind_eligibility` is
        # idempotent, so this costs one bind and not one per request.
        bind_eligibility(app, None, env)
        # Deliberately NOT cached. "No deployment configured" is two `os.environ` lookups to
        # re-establish, and caching it meant a document that appeared after the first request
        # was ignored for the life of the process — a real trap for an operator who starts the
        # exchange and then writes the file. Only a SUCCESSFUL bind is remembered; a failure is
        # not cached either, so a fixed document is picked up by the next request.
        return ()
    bound = configure_exchange(app, deployment, env)
    setattr(app.state, STATE_FLAG, bound)
    return bound
