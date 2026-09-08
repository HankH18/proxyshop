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
      "external_bid_keyring_file": "/run/secrets/exchange-external-bid-keyring.json",
      "checkout_mode": "redirect",
      "trust_url": "http://trust:8084",
      "merchant_url": "http://merchant-svc:8082",
      "merchant_admin_token_file": "/run/secrets/exchange-merchant-admin-token"
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
``external_bid_keyring_file``
    **A PATH, never the keys.** The signed external bid door
    (``POST /v1/auctions/{auction_id}/bids``) selects a secret out of a
    ``{signer_id: {key_id: secret}}`` keyring (R8/C10/D52), and until this key existed the only
    way to hand it one was :func:`~exchange.external_bids.routes.configure_external_bids`,
    which no deployment called — so the served door refused every submission in production
    with ``unknown_signing_key``. It could not be closed by adding the keys to this document:
    **this document carries no secret material of any kind**, by design, because it is a plain
    JSON file that gets pasted into tickets, chat and compose overrides. So the document names
    a file and the SECRETS LIVE IN THAT FILE — the same separation
    ``PROXYSHOP_ROLE_PASSWORD`` and the service DSNs already keep in this repository, where
    every document names a role and none of them names a password.

    *Where the secret lives*: a JSON file on the exchange's own filesystem, holding exactly
    ``{"signer-id": {"key-id": "secret"}}``. *Who can read it*: whoever can read that path —
    a deployment mounts it read-only at the same place it already mounts the document this key
    is written in (``EXCHANGE_DEPLOYMENT`` is itself a path inside the container, so this needs
    no new variable, no new compose line and no change to any shipped artifact), and a
    ``/run/secrets/…`` mount at mode ``0400`` owned by the service user is what that means on
    docker and kubernetes. Said exactly rather than generously: a deployment that states its
    document INLINE through ``EXCHANGE_DEPLOYMENT_JSON`` has no mount yet and has to add one
    for this file — the keys cannot ride in the variable, for the same reason they cannot ride
    in the document. *When it is absent*: nothing is bound, ``_keyring`` answers ``{}``
    and the door refuses every submission ``unknown_signing_key`` — an exchange nobody handed a
    keyring to admits nothing, which is the property this key may not weaken.

    A file that is NAMED and cannot be read, or that does not spell a keyring, is a **503**
    like every other malformed deployment — not a fail-closed shrug, because the shrug is
    indistinguishable from the correct behaviour for an exchange nobody configured, and that is
    precisely how a deployment ends up refusing an honest seller for a week. The message names
    the file, the signer and the key id (all three travel on the wire in every submission) and
    never a secret. Writing the keyring INLINE under ``external_bid_keyring`` is refused
    outright rather than ignored, because ignoring it would leave an operator believing the
    door was configured while it refused everything.

    Rotation is a change to that file plus a restart: it is read once per process, at the same
    moment the rest of the document is. Adding a signer's next key id alongside its current one
    needs no coordination — that is what the nested shape is for — and REMOVING a key id takes
    effect when the process next boots.
``checkout_mode``
    Optional; ``CHECKOUT_MODE`` still works and this overrides it for this app.
``merchant_url``
    The BASE address of the merchant service, whose published ``POST /codes`` mints the real,
    spendable single-use discount on the store (R3). Optional, and it is the key R3 was
    missing: ``accept()`` takes a ``code_creator`` and ``configure_accept`` binds one, and
    **no composition root in this repository ever called that seam** — so a deployment that
    stated ``"checkout_mode": "shopify"`` (a mode this very module validates as registered)
    served, measured on this tree over a real socket::

        POST /auctions/{id}/accept -> 409
        {"accepted": false, "denial_reason": "checkout_refused: CheckoutCreatorError:
          shopify checkout needs an injected code creator (the merchant POST /codes client);
          accept() was called without one"}

    — every accept, for every buyer, in every shipped deployment. The consequence beyond the
    missing code is that ``apps/merchant/svc/src/codes/combines.py``, the ONLY place a
    discount's ``combinesWith`` is checked against the shop's running automatic discounts,
    was unreachable from any composed exchange.

    ``MERCHANT_URL`` is the environment fallback, and there is **no built-in default** —
    which is the one place this key deliberately differs from ``trust_url``. The audit trail
    must be written whether or not anybody configured it, so ``trust_url`` defaults to the
    compose service name. A code creator is the opposite: an address nobody chose is an
    address at which some *other* shop's real discounts might be minted, and the failure mode
    of a wrong guess is invisible — a connection error per accept, dressed as a refusal of the
    buyer. So an exchange whose checkout mode needs a merchant and names none is refused at
    wiring time, with a **503 naming this key**, exactly as an unregistered ``checkout_mode``
    is. See :func:`bind_code_creator` for the whole ladder, including why "mint locally
    instead" is not on the table: a code this exchange invents is one the store has never
    heard of, so the buyer is redirected to a checkout that rejects it.

    The mode decides whether this is needed at all, and the PROVIDER decides that rather than
    a list of spellings here (:attr:`~exchange.checkout.provider.CheckoutProvider
    .requires_code_creator`). ``redirect`` — D45's required starting implementation — mints
    locally and needs no merchant, which is why the starting-slice demo runs with neither this
    key nor the Shopify install lane.
``merchant_admin_token_file``
    **A PATH, never the token.** ``POST /codes`` creates real spendable discounts, so it sits
    behind the same bearer token the merchant's other administrative routes do, and an unset
    ``MERCHANT_ADMIN_TOKEN`` makes that route refuse *every* caller with
    ``503 admin-api-not-configured`` by design. This document therefore names a file holding
    the token and nothing else — the same separation ``external_bid_keyring_file`` keeps, for
    the same reason: a deployment document is a plain JSON file that gets pasted into tickets.
    Writing the token INLINE under ``merchant_admin_token`` is refused outright rather than
    ignored.

    ``MERCHANT_ADMIN_TOKEN`` in this exchange's own environment is the fallback, and it is the
    same variable name the merchant reads, so one value configures both sides. A merchant
    address stated with no token available either way is refused at wiring time as well: a
    creator that can only ever be answered ``401`` is a seam that looks bound and mints
    nothing, which is the shape of defect this module exists to end.
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
from .checkout.registry import (
    DEFAULT_CHECKOUT_MODE,
    UnknownCheckoutMode,
    registered_modes,
    resolve_provider,
)
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
    "AUCTION_STORE_KEY",
    "DECLINE_REASON_HEADER",
    "DEFAULT_LEDGER_TIMEOUT_SECONDS",
    "DEFAULT_MERCHANT_TIMEOUT_SECONDS",
    "DEFAULT_SOLICIT_TIMEOUT_SECONDS",
    "DEFAULT_TRUST_SNAPSHOT_TIMEOUT_SECONDS",
    "DEFAULT_TRUST_URL",
    "ENV_DEPLOYMENT",
    "ENV_DEPLOYMENT_JSON",
    "ELIGIBILITY_WORDS",
    "ENV_MERCHANT_ADMIN_TOKEN",
    "ENV_MERCHANT_URL",
    "ENV_TRUST_URL",
    "Deployment",
    "DeploymentConfigurationError",
    "HttpBidSolicitor",
    "HttpMerchantCodeCreator",
    "HttpTrustLedgerSink",
    "HttpTrustSnapshot",
    "KEYRING_FILE_KEY",
    "KEYRING_INLINE_KEY",
    "LayeredSellerEligibility",
    "LiveTrustSnapshot",
    "MAX_BID_RESPONSE_BYTES",
    "MAX_DEPLOYMENT_BYTES",
    "MAX_DEPLOYMENT_SELLERS",
    "MAX_KEYRING_BYTES",
    "MAX_REPORT_TOKENS_BYTES",
    "MAX_MERCHANT_REPLY_BYTES",
    "MAX_MERCHANT_TOKEN_BYTES",
    "MAX_SOLICIT_WALL_CLOCK_SECONDS",
    "MAX_UNDELIVERED_LEDGER_EVENTS",
    "MERCHANT_CODES_PATH",
    "MERCHANT_TOKEN_FILE_KEY",
    "REPORT_TOKENS_FILE_KEY",
    "REPORT_TOKENS_INLINE_KEY",
    "MERCHANT_TOKEN_INLINE_KEY",
    "MERCHANT_URL_KEY",
    "MerchantCodeCreationRefused",
    "SellerRow",
    "TRUST_EVENTS_PATH",
    "TRUST_SNAPSHOT_PATH",
    "TRUST_SNAPSHOT_REFRESH_SECONDS",
    "TRUST_SNAPSHOT_RETRY_SECONDS",
    "TRUST_DEFERRED_ELIGIBILITY",
    "TrustBackedSellerEligibility",
    "TrustLedgerPublisher",
    "bind_auction_machine",
    "bind_code_creator",
    "bind_ledger_sink",
    "bind_seller_eligibility",
    "bind_trust_snapshot_reader",
    "configure_exchange",
    "default_ledger_sink",
    "default_seller_eligibility",
    "effective_checkout_mode",
    "ensure_configured",
    "merchant_admin_token",
    "merchant_codes_endpoint",
    "read_deployment",
    "read_external_bid_keyring",
    "read_report_tokens",
    "read_merchant_admin_token",
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
#: Shorter than the auction's own default window (``DEFAULT_BID_TIMEOUT_SECONDS``, 5.0s) is
#: wrong and longer is pointless: the fan-out abandons the wait at the deadline anyway, so this
#: exists only to stop a socket outliving the request that opened it. It is the auction's
#: server-side ceiling (``MAX_BID_TIMEOUT_SECONDS``), which is the longest any one solicitation
#: can still be useful for — so it stays correct as the default moves, because no caller and no
#: operator can name a window above it. The default's move from 3.0 to 5.0 changed nothing here
#: except the size of the margin; :data:`MAX_SOLICIT_WALL_CLOCK_SECONDS` (15.0) still sits above
#: this per-store read timeout, which is what keeps the whole-roster bound the outer one.
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

#: The document key naming the external bid door's keyring FILE, and the key that is refused.
#:
#: Two names, one letter apart, and the refusal of the second is the reason both are constants:
#: an operator's first instinct is to type the keys into the document, and a composition root
#: that silently ignored that would leave them believing the door was configured while it
#: refused every submission ``unknown_signing_key``. See the module header.
KEYRING_FILE_KEY = "external_bid_keyring_file"
KEYRING_INLINE_KEY = "external_bid_keyring"

#: The most bytes an external bid keyring file may occupy.
#:
#: Read once per process, not per request, so this is a guard against a mistake — a path
#: pointing at a log, a core dump, a whole database export — rather than against an adversary,
#: which is why it is generous: :data:`MAX_DEPLOYMENT_SELLERS` signers with several rotating
#: keys each fit inside it many times over. A failure is NOT cached (see
#: :func:`ensure_configured`), so a document naming a large unusable file is re-read on every
#: request until it is fixed, and that is the case this ceiling is really about.
MAX_KEYRING_BYTES = 1024 * 1024

#: The merchant service's published code door, and the document/environment keys naming it.
#:
#: ``apps/merchant/svc/src/codes/routes.py`` serves ``POST /codes`` and
#: ``packages/contracts/openapi/merchant.openapi.json`` pins the path; spelled once, here,
#: because the exchange does not import the merchant's package (its image does not contain
#: it) — the same arrangement every other cross-service constant in this module has.
MERCHANT_CODES_PATH = "/codes"
MERCHANT_URL_KEY = "merchant_url"
ENV_MERCHANT_URL = "MERCHANT_URL"

#: The document key naming the merchant bearer token's FILE, and the key that is refused.
#:
#: Two names, one suffix apart, for the reason :data:`KEYRING_INLINE_KEY` is refused beside
#: :data:`KEYRING_FILE_KEY`: an operator's first instinct is to type the token into the
#: document, and a composition root that silently ignored it would leave them believing the
#: merchant was configured while every mint was answered ``401``.
MERCHANT_TOKEN_FILE_KEY = "merchant_admin_token_file"
MERCHANT_TOKEN_INLINE_KEY = "merchant_admin_token"

#: The environment fallback for that token — the SAME variable the merchant itself reads
#: (``apps/merchant/svc/src/install/routes.py``'s ``ADMIN_TOKEN_ENV``), so one value
#: configures both sides of the door and the two can never hold different opinions about it.
ENV_MERCHANT_ADMIN_TOKEN = "MERCHANT_ADMIN_TOKEN"

#: How long ONE ``POST /codes`` may hold the accept request open.
#:
#: Longer than the trust reads because this call is not a lookup: the merchant reads the
#: shop's live automatic-discount configuration from the Shopify Admin API and then issues the
#: discount, so two round trips to Shopify sit inside it. Finite because a buyer is waiting,
#: and because a merchant that has not answered in ten seconds is not going to.
#:
#: **What a timeout here cannot tell you**, said plainly rather than glossed: the merchant may
#: already have minted. The exchange refuses the accept with no code, which is the right
#: answer for the buyer, and the record of any live discount is on the merchant's own side —
#: ``CodeLedger``'s ``code_created`` — which is where a reconciliation looks for orphans.
DEFAULT_MERCHANT_TIMEOUT_SECONDS = 10.0

#: The most bytes the merchant's ``POST /codes`` reply may occupy.
#:
#: The reply is the pinned contract's three keys — ``{code, permalink_url, expires_at}`` — so
#: unlike the trust snapshot (whose size legitimately grows with the marketplace, which is why
#: it is uncapped) this one has a fixed small shape that cannot grow with anything. A cap is
#: therefore free of the "the day the marketplace outgrows it" hazard, and it bounds a read
#: that happens on the unauthenticated accept path inside a 256 MiB container.
MAX_MERCHANT_REPLY_BYTES = 64 * 1024

#: The most bytes the merchant bearer token's file may occupy. Read once per process.
#:
#: A guard against a path pointing at something else — a log, a key bundle, a database dump —
#: rather than against an adversary, in the same house style as :data:`MAX_KEYRING_BYTES`. A
#: bearer token is tens of characters; four kilobytes is a very generous ceiling for one.
MAX_MERCHANT_TOKEN_BYTES = 4096

#: The document key naming R9's report tokens FILE, and the key that is refused beside it.
#:
#: ``{store_id: token}``, and it is per-store rather than one shared administrative secret
#: because of WHAT the route it configures serves: ``GET /reports/losses`` hands one merchant
#: its own losses (R9). A single token would authenticate a caller and say nothing about which
#: store's report it may read, so the store id would have to arrive as a parameter beside it —
#: and "check that the caller owns the store it named" is a comparison that can be forgotten.
#: Resolving the store FROM the token means there is no parameter in which to ask the wrong
#: question. See :mod:`exchange.reports.routes`.
#:
#: The inline spelling is refused for the reason :data:`KEYRING_INLINE_KEY` is: this document
#: is a plain JSON file that gets pasted around, and silently ignoring a typed-in table would
#: leave an operator believing reports were configured while every merchant got ``503``.
REPORT_TOKENS_FILE_KEY = "report_tokens_file"
REPORT_TOKENS_INLINE_KEY = "report_tokens"

#: The document key naming WHERE THIS EXCHANGE'S AUCTIONS LIVE — ``memory`` or ``redis``.
#:
#: It did not exist, and its absence was the defect rather than a gap in the vocabulary.
#: ``exchange.auction.state`` has shipped two stores since T-158 and documents the Redis one as
#: the DESIGN-pinned choice ("a deployment that cares runs ``RedisAuctionStore``"), but no
#: deployment could: :func:`configure_exchange` built ``AuctionStateMachine`` without naming a
#: store, this record had no field to name one, and ``auction/routes.py``'s lazy default is the
#: same in-memory store. So every served exchange ran the process-local one, while
#: ``apps/exchange/compose.yaml``'s readiness note told the operator ``--redis`` was "the
#: MEASURED dependency" of this state machine. It was not; with this key stated it is.
#:
#: A WORD, not a URL: ``redis`` resolves through ``proxyshop_support.redis_client.worker_redis``
#: (``REDIS_URL`` plus ``PROXYSHOP_WORKER``), which is the one place D39 permits a client to be
#: built. A second spelling of the connection here would be a second source of truth for the
#: per-worker logical DB and the ``w{N}:`` prefix, which is exactly what D39 forbids.
AUCTION_STORE_KEY = "auction_store"

#: The most bytes the report token file may occupy. Read once per process, so this is the same
#: "a path pointing at something else" guard :data:`MAX_KEYRING_BYTES` is, sized for one short
#: token per store rather than for a keyring of rotating secrets.
MAX_REPORT_TOKENS_BYTES = 256 * 1024

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
    #: Where this deployment's MERCHANT answers — the BASE url, not the ``/codes`` path.
    #: ``None`` leaves :func:`merchant_codes_endpoint` to fall back to :data:`ENV_MERCHANT_URL`
    #: and then to nothing at all: unlike :attr:`trust_url` there is no built-in default, so an
    #: exchange whose checkout mode mints on the merchant and names none is refused rather than
    #: pointed at a guess. See :func:`bind_code_creator`.
    merchant_url: str | None = None
    #: Path to the file holding the merchant's administrative bearer token, or ``None``.
    #: A PATH and never the token, for the reason :attr:`external_bid_keyring_file` is one.
    #: Read by :func:`read_merchant_admin_token` at bind time rather than here — a parsed
    #: document is a description of a deployment, and reading a secret while validating one
    #: would put it in the hands of every caller that only wanted to check the syntax.
    merchant_admin_token_file: str | None = None
    #: Path to the file holding the external bid door's ``{signer_id: {key_id: secret}}``
    #: keyring, or ``None`` when this deployment states none and the door therefore admits
    #: nobody. A PATH and never the keys: this record carries no secret material, which is what
    #: makes a deployment document safe to paste into a ticket. Read by
    #: :func:`read_external_bid_keyring` at bind time, not here — a parsed document is a
    #: description of a deployment, and reading a secret while validating one would put the
    #: keys in every caller's hands, including the two that only wanted to check the syntax.
    external_bid_keyring_file: str | None = None
    #: Path to the file holding R9's ``{store_id: token}`` report tokens, or ``None`` when this
    #: deployment states none and ``GET /reports/losses`` therefore serves nobody. A PATH and
    #: never the tokens, and read at bind time rather than here, for the reasons the two
    #: attributes above are.
    report_tokens_file: str | None = None
    #: Which :class:`~exchange.auction.state.AuctionStore` this deployment holds its auctions
    #: in — ``"memory"``, ``"redis"``, or ``None`` when the document states neither and the
    #: environment's :data:`~exchange.auction.state.ENV_AUCTION_STORE` decides instead.
    #:
    #: ``None`` is genuinely "unstated" and not "memory", so the two sources compose rather
    #: than fight: a document that says nothing lets ``EXCHANGE_AUCTION_STORE`` choose, and a
    #: document that says ``"memory"`` is an operator deliberately overriding it. The same
    #: distinction :attr:`intent_clusters` and :attr:`catalog` draw between ``None`` and empty.
    auction_store: str | None = None

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

    merchant_url = body.get(MERCHANT_URL_KEY)
    if merchant_url is not None:
        merchant_url = str(merchant_url).strip() or None
        if merchant_url is not None and not merchant_url.lower().startswith(
            ("http://", "https://")
        ):
            raise DeploymentConfigurationError(
                f"{source}: {MERCHANT_URL_KEY} {merchant_url!r} is not an http(s) URL; this is "
                f"the base address of the merchant service, whose {MERCHANT_CODES_PATH} door "
                f"mints the single-use discount on the store (R3). Refused here rather than at "
                f"the first accept, because that failure reaches the buyer as a refusal of the "
                f"purchase rather than as a misconfiguration"
            )

    return Deployment(
        source=source,
        sellers=sellers,
        trust_snapshot=snapshot,
        checkout_mode=checkout_mode,
        intent_clusters=_intent_clusters(body.get("intent_clusters"), source),
        catalog=_catalog(body.get("catalog"), source),
        trust_url=trust_url,
        merchant_url=merchant_url,
        merchant_admin_token_file=_merchant_token_file(body, source),
        external_bid_keyring_file=_keyring_file(body, source),
        report_tokens_file=_report_tokens_file(body, source),
        auction_store=_auction_store_word(body, source),
    )


def _auction_store_word(body: Mapping[str, Any], source: str) -> str | None:
    """The store this document names its auctions live in, or ``None``.

    Refused at PARSE time rather than at bind time, exactly as ``checkout_mode`` is and for the
    same reason: a word with no implementation is a deployment that cannot hold an auction, and
    discovering that on the first ``POST /auctions`` turns a typo into a 503 per request.
    """
    from .auction.state import AUCTION_STORE_WORDS  # noqa: PLC0415 — sibling feature

    stated = body.get(AUCTION_STORE_KEY)
    if stated is None:
        return None
    word = str(stated).strip().lower()
    if word not in AUCTION_STORE_WORDS:
        raise DeploymentConfigurationError(
            f"{source}: {AUCTION_STORE_KEY} {stated!r} is not one of "
            f"{list(AUCTION_STORE_WORDS)}. This key says where an auction LIVES, so an "
            f"unrecognised word cannot be defaulted through: guessing 'memory' would give a "
            f"deployment that asked for durability a process-local store that forgets "
            f"everything on restart and constrains nothing across replicas"
        )
    return word


def _report_tokens_file(body: Mapping[str, Any], source: str) -> str | None:
    """The path this document names R9's report tokens at, or ``None``.

    Refuses the INLINE spelling first, exactly as :func:`_keyring_file` does and for the same
    reason: a document carrying ``{"report_tokens": {...}}`` is an operator who has typed live
    bearer tokens into a file that gets pasted around, and silently ignoring the key would
    leave them believing reports were configured while every merchant kept getting ``503``.
    The message quotes the key NAME and never a value.
    """
    if body.get(REPORT_TOKENS_INLINE_KEY) is not None:
        raise DeploymentConfigurationError(
            f"{source}: {REPORT_TOKENS_INLINE_KEY!r} is not a key this document may carry — a "
            f"deployment document holds no secret material, because it is a plain JSON file "
            f"that gets pasted around. State {REPORT_TOKENS_FILE_KEY!r} instead, naming a file "
            f"that holds {{store_id: token}} and that only this service can read"
        )
    stated = body.get(REPORT_TOKENS_FILE_KEY)
    if stated is None:
        return None
    if not isinstance(stated, str) or not stated.strip():
        raise DeploymentConfigurationError(
            f"{source}: {REPORT_TOKENS_FILE_KEY} must be a non-empty path to the file holding "
            f"this exchange's {{store_id: token}} report tokens, got {type(stated).__name__}. "
            f"Omit the key entirely to run an exchange that serves no merchant its losses"
        )
    return stated.strip()


def read_report_tokens(path: str, *, source: str) -> Mapping[str, str]:
    """The ``{store_id: token}`` table ``path`` holds. Raises rather than degrading.

    Every refusal below has the same symptom if it is tolerated — ``GET /reports/losses``
    answers ``401`` or ``503`` to a merchant holding a correct token — and that sentence names
    the merchant, not the deployment. So a table that was NAMED and cannot be used is a
    start-up refusal that says which file and which row.

    **Nothing here quotes a token**, only the store id and a type name. The store id already
    travels in every report body; the token is the one value in this file that must never reach
    a log, a 503 body or an exception's ``__str__``.

    An EMPTY table (``{}``) is accepted and binds: it is a person stating that this exchange has
    no merchant on reports yet, and it behaves exactly as omitting the key does — every caller
    is refused. An empty FILE is not JSON and is refused, which is the truncation case worth
    telling apart.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DeploymentConfigurationError(
            f"{source}: the report tokens at {path} could not be read "
            f"({exc.__class__.__name__}: {exc}). A named-but-unreadable table is a "
            f"misconfiguration, not an exchange with no merchants on reports, so it is refused "
            f"rather than answered `401` to every merchant who asks for its own losses"
        ) from exc

    if len(text) > MAX_REPORT_TOKENS_BYTES:
        raise DeploymentConfigurationError(
            f"{source}: the report tokens at {path} are {len(text)} bytes; this exchange reads "
            f"at most {MAX_REPORT_TOKENS_BYTES}. One short token per store fits inside that "
            f"many times over, so a file this size is a path pointing at something else"
        )

    try:
        document = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - RecursionError is not a ValueError
        raise DeploymentConfigurationError(
            f"{source}: the report tokens at {path} are not valid JSON ({type(exc).__name__})"
        ) from exc

    if not isinstance(document, Mapping):
        raise DeploymentConfigurationError(
            f"{source}: the report tokens at {path} must be a JSON object of "
            f"{{store_id: token}}, got {type(document).__name__}"
        )

    tokens: dict[str, str] = {}
    seen: dict[str, str] = {}
    for store_id, token in document.items():
        if not isinstance(store_id, str) or not store_id.strip():
            raise DeploymentConfigurationError(
                f"{source}: the report tokens at {path} name a store that is not a non-empty "
                f"string ({type(store_id).__name__}); no report could ever be addressed to it"
            )
        if not isinstance(token, str) or not token.strip():
            raise DeploymentConfigurationError(
                f"{source}: the report tokens at {path} hold no usable token for store "
                f"{store_id!r} ({type(token).__name__}). A blank token is not 'this store has "
                f"no access' — the route drops empty rows, so the store would be refused while "
                f"the file looked configured"
            )
        if token.strip() in seen:
            # Two stores sharing a token is not a duplicate row, it is a merchant able to read
            # another merchant's losses — the exact leak this table's shape exists to prevent.
            raise DeploymentConfigurationError(
                f"{source}: the report tokens at {path} give stores {seen[token.strip()]!r} and "
                f"{store_id!r} the same token. The token is what names the store on "
                f"`GET /reports/losses`, so a shared one lets one merchant read the other's "
                f"win/loss report (R9)"
            )
        seen[token.strip()] = store_id
        tokens[store_id.strip()] = token.strip()
    return tokens


def _merchant_token_file(body: Mapping[str, Any], source: str) -> str | None:
    """The path this document names the merchant's bearer token at, or ``None``.

    Refuses an INLINE token first, and that refusal is the more important half — the same
    shape, and the same reasoning, as :func:`_keyring_file`'s. A document carrying
    ``{"merchant_admin_token": "..."}`` is an operator who has just typed a live administrative
    credential into the file this module's header describes as the one that gets pasted around;
    ignoring the key (the default for anything this parser does not read) would leave them
    believing the merchant was configured while every mint came back ``401``.

    The message quotes the key NAME and never the value.
    """
    if body.get(MERCHANT_TOKEN_INLINE_KEY) is not None:
        raise DeploymentConfigurationError(
            f"{source}: {MERCHANT_TOKEN_INLINE_KEY!r} is not a key this document may carry — a "
            f"deployment document holds no secret material, because it is a plain JSON file "
            f"that gets pasted around. State {MERCHANT_TOKEN_FILE_KEY!r} instead, naming a file "
            f"that holds the bearer token and that only this service can read, or set "
            f"{ENV_MERCHANT_ADMIN_TOKEN} in this exchange's environment"
        )
    stated = body.get(MERCHANT_TOKEN_FILE_KEY)
    if stated is None:
        return None
    if not isinstance(stated, str) or not stated.strip():
        raise DeploymentConfigurationError(
            f"{source}: {MERCHANT_TOKEN_FILE_KEY} must be a non-empty path to the file holding "
            f"this exchange's merchant bearer token, got {type(stated).__name__}. Omit the key "
            f"entirely to take the token from {ENV_MERCHANT_ADMIN_TOKEN} instead"
        )
    return stated.strip()


def _keyring_file(body: Mapping[str, Any], source: str) -> str | None:
    """The path this document names its external bid keyring at, or ``None``.

    Refuses an INLINE keyring first, and that refusal is the more important half. A document
    carrying ``{"external_bid_keyring": {...}}`` is an operator who has just typed live signing
    secrets into the file this module's header describes as the one that gets pasted around;
    ignoring the key — the default behaviour for anything this parser does not read — would
    leave them believing the door was configured while it went on refusing every submission
    ``unknown_signing_key``, which is the exact failure this key exists to end. The message
    quotes the key NAME and never the value.
    """
    if body.get(KEYRING_INLINE_KEY) is not None:
        raise DeploymentConfigurationError(
            f"{source}: {KEYRING_INLINE_KEY!r} is not a key this document may carry — a "
            f"deployment document holds no secret material, because it is a plain JSON file "
            f"that gets pasted around. State {KEYRING_FILE_KEY!r} instead, naming a file that "
            f"holds {{signer_id: {{key_id: secret}}}} and that only this service can read"
        )
    stated = body.get(KEYRING_FILE_KEY)
    if stated is None:
        return None
    if not isinstance(stated, str) or not stated.strip():
        raise DeploymentConfigurationError(
            f"{source}: {KEYRING_FILE_KEY} must be a non-empty path to the file holding this "
            f"exchange's {{signer_id: {{key_id: secret}}}} keyring, got "
            f"{type(stated).__name__}. Omit the key entirely to run an exchange that admits no "
            f"external bid at all"
        )
    return stated.strip()


def read_external_bid_keyring(path: str, *, source: str) -> Mapping[str, Mapping[str, str]]:
    """The ``{signer_id: {key_id: secret}}`` keyring ``path`` holds. Raises rather than degrading.

    Every refusal below has the SAME symptom if it is tolerated — the door answers
    ``unknown_signing_key`` to a correctly signed bid — and that sentence names the seller, not
    the deployment. So a keyring that was NAMED and cannot be used is a 503 that says which
    file and which row, exactly as a malformed deployment document is.

    **Nothing here quotes a secret**, only the signer id, the key id and a type name. Both ids
    travel on the wire in every submission and are already echoed in refusals; the secret is
    the one value in this file that must never reach a log, a 503 body or an exception's
    ``__str__`` — which is where an unguarded ``f"{value!r}"`` puts it.

    An EMPTY keyring (``{}``) is accepted and binds: it is a person stating that this exchange
    registers no external signer yet, and it behaves exactly as omitting the key does. An empty
    FILE is not JSON and is refused, which is the truncation case worth telling apart.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DeploymentConfigurationError(
            f"{source}: the external bid keyring at {path} could not be read "
            f"({exc.__class__.__name__}: {exc}). A named-but-unreadable keyring is a "
            f"misconfiguration, not an exchange that registers no signer, so it is refused "
            f"rather than answered `unknown_signing_key` to every seller who submits"
        ) from exc

    if len(text) > MAX_KEYRING_BYTES:
        raise DeploymentConfigurationError(
            f"{source}: the external bid keyring at {path} is {len(text)} bytes; this exchange "
            f"reads at most {MAX_KEYRING_BYTES}. A keyring is a handful of short secrets per "
            f"signer, so a file this size is a path pointing at something else"
        )

    try:
        document = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - RecursionError is not a ValueError; see below
        # `except ValueError` would let `json.loads` on a deeply nested file escape as a
        # RecursionError and become a 500 on every route, which is the defect `read_deployment`
        # already had to fix one layer up. Nothing about this file's contents may reach the
        # message: `str(exc)` for a JSON error is a position and a reason, never the text.
        raise DeploymentConfigurationError(
            f"{source}: the external bid keyring at {path} is not valid JSON ({type(exc).__name__})"
        ) from exc

    if not isinstance(document, Mapping):
        raise DeploymentConfigurationError(
            f"{source}: the external bid keyring at {path} must be a JSON object of "
            f"{{signer_id: {{key_id: secret}}}}, got {type(document).__name__}"
        )

    keyring: dict[str, dict[str, str]] = {}
    for signer_id, keys in document.items():
        if not isinstance(signer_id, str) or not signer_id:
            raise DeploymentConfigurationError(
                f"{source}: the external bid keyring at {path} names a signer that is not a "
                f"string ({type(signer_id).__name__}); a submission's `signer_id` is a string "
                f"and would match nothing"
            )
        if not isinstance(keys, Mapping):
            raise DeploymentConfigurationError(
                f"{source}: the external bid keyring at {path} maps signer {signer_id!r} to "
                f"{type(keys).__name__}, not to a {{key_id: secret}} object. The nesting is the "
                f"contract (R8): two signers may use the same key id, and a flat table would "
                f"let one signer's key verify another's bid"
            )
        if not keys:
            raise DeploymentConfigurationError(
                f"{source}: the external bid keyring at {path} names signer {signer_id!r} with "
                f"no key at all, so every bid it signs is refused `unknown_signing_key`. Remove "
                f"the signer, or state the key id it signs with"
            )
        row: dict[str, str] = {}
        for key_id, secret in keys.items():
            if not isinstance(key_id, str) or not key_id:
                raise DeploymentConfigurationError(
                    f"{source}: the external bid keyring at {path} gives signer {signer_id!r} a "
                    f"key id that is not a string ({type(key_id).__name__})"
                )
            if not isinstance(secret, str) or not secret:
                raise DeploymentConfigurationError(
                    f"{source}: the external bid keyring at {path} holds no usable secret for "
                    f"signer {signer_id!r} key {key_id!r} ({type(secret).__name__}). A blank or "
                    f"non-string secret is not an HMAC key, and `keyring_secret` reads it as no "
                    f"such key — so the row would refuse the seller it was written for"
                )
            row[key_id] = secret
        keyring[signer_id] = row
    return keyring


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
        # D58: say what is being solicited a bid ON. `BidRequest.product_ref` is the roster
        # row's, so it is bound per STORE and cannot ride on `self._context`, which is bound
        # per auction by `for_auction`. Until this line existed the published `BidRequest` named
        # no product at all, so a solicited agent picked one out of its own catalogue and the
        # exchange then graded its claims against a product it had never been told about.
        #
        # **Written only when there IS one, and that is a compatibility decision rather than a
        # style one.** Every object in `protocol.schema.json` is `additionalProperties: false`
        # and the generated pydantic model is `extra="forbid"`, so a store agent pinned to the
        # previous schema generation answers `422` to a body carrying this key — measured, on a
        # real socket, against the verbatim previous `BidRequest` model. That degrades safely
        # (the refusal below turns it into an R10 list-price fallback, `store_refused:422`) but
        # it degrades SILENTLY, and an auction that names no product has nothing to gain by
        # breaking a stale agent. Omitting the key is what a `str | None = None` reader reads as
        # `null` anyway. See `contracts.SCHEMA_VERSION` for why this is a MAJOR bump.
        solicited = _solicited_product_ref(store)
        if solicited is not None:
            payload["product_ref"] = solicited
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


def _solicited_product_ref(store: Mapping[str, Any]) -> str | None:
    """This roster row's ``product_ref`` as ``BidRequest.product_ref`` publishes it.

    ``str | None``, never anything else. The roster reaches the exchange on an unauthenticated
    request body or out of the graph, and ``BidRequest.product_ref`` is typed
    ``["string", "null"]`` — a row carrying an int, a list or a mapping there would build a body
    the receiving agent's own pydantic door refuses, which turns one malformed roster row into a
    store that reads as silent. An unusable value is therefore ``None``: the exchange named no
    product, which is a legal solicitation.

    **Stripped, and that is load-bearing rather than tidy.** ``RosterEntry.product_ref`` has no
    ``min_length`` and no trim, so ``" prod-cap "`` arrives intact on an unauthenticated body.
    The receiving agent trims before matching it against a catalog key
    (``store_agent.runtime.context._solicited_ref``), so an exchange that ASKED with the padding
    and then COMPARED with the padding would refuse the agent's answer as being about another
    product — the exchange asking about X, the store answering exactly X, and the exchange's own
    price wall rejecting it. So every reader D58 added trims: this one, ``collect.
    _answers_about_another_product`` and ``ranking.verification.graded_product_ref``. Trimming
    only here would move the disagreement rather than close it.
    """
    stated = store.get("product_ref")
    if not isinstance(stated, str) or not stated.strip():
        return None
    return stated.strip()


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
# The MERCHANT seam — R3's `POST /codes`, the door that mints on the STORE
# =====================================================================================
class MerchantCodeCreationRefused(RuntimeError):
    """The merchant's ``POST /codes`` did not mint a code for this offer.

    Raised by :class:`HttpMerchantCodeCreator` and caught by nothing in this module: it travels
    out through :meth:`~exchange.checkout.providers.ShopifyCheckoutProvider.mint` and becomes
    the accept's ``denial_reason``, which is the right place for it — the buyer is told the
    checkout was refused, and the sentence names the merchant rather than the store.
    """


def merchant_codes_endpoint(
    base_url: str | None = None, env: Mapping[str, str] | None = None
) -> tuple[str | None, str]:
    """``(url, source)`` for the merchant's code door, or ``(None, "")`` when none is known.

    The precedence mirrors :func:`trust_snapshot_endpoint`'s — the document's
    :data:`MERCHANT_URL_KEY`, then :data:`ENV_MERCHANT_URL` — and then **stops**. There is no
    third rung, and the missing default is a decision rather than an omission: see the
    ``merchant_url`` entry in the module header. An address nobody chose would turn a
    deployment mistake into a per-accept connection error, which reaches the buyer as a
    refusal of their purchase and reaches the operator as nothing at all.

    A base url that already ends in :data:`MERCHANT_CODES_PATH` is accepted and not doubled,
    because "the merchant's address" and "the merchant's code door" are the same string to
    everyone except this function.
    """
    environ = os.environ if env is None else env

    stated = str(base_url or "").strip()
    source = SOURCE_STATED
    if not stated:
        stated = str(environ.get(ENV_MERCHANT_URL) or "").strip()
        source = SOURCE_ENVIRONMENT
    if not stated:
        return None, ""

    if not stated.lower().startswith(("http://", "https://")):
        raise DeploymentConfigurationError(
            f"the {ENV_MERCHANT_URL} environment variable is {stated!r}, which is not an "
            f"http(s) URL; this is the base address of the merchant service, whose "
            f"{MERCHANT_CODES_PATH} door mints the single-use discount on the store (R3)"
        )

    base = stated.rstrip("/")
    if base.endswith(MERCHANT_CODES_PATH):
        base = base[: -len(MERCHANT_CODES_PATH)]
    return f"{base}{MERCHANT_CODES_PATH}", source


def read_merchant_admin_token(path: str, *, source: str) -> str:
    """The merchant bearer token ``path`` holds. Raises rather than degrading.

    **Nothing here ever quotes the token** — not in a refusal, not in a length, not in an
    exception's ``__str__``. The file's PATH and the failure's type are the whole diagnostic,
    and they are what an operator can act on; the one value in this file is the one value that
    must never reach a log or a 503 body.

    A named-but-unusable token file is refused for the reason a named-but-unusable keyring is:
    every tolerated failure here has the same symptom, and that symptom is the merchant
    answering ``401`` to a correctly composed exchange — a sentence that names the deployment
    nowhere. Trailing whitespace is stripped, because a token file written by ``echo`` ends in
    a newline and a bearer header carrying one is a token that matches nothing.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DeploymentConfigurationError(
            f"{source}: the merchant bearer token at {path} could not be read "
            f"({exc.__class__.__name__}: {exc}). A named-but-unreadable token is a "
            f"misconfiguration, not an exchange that mints nowhere, so it is refused rather "
            f"than answered `401` by the merchant on every accept"
        ) from exc

    if len(text) > MAX_MERCHANT_TOKEN_BYTES:
        raise DeploymentConfigurationError(
            f"{source}: the merchant bearer token file at {path} is {len(text)} bytes; this "
            f"exchange reads at most {MAX_MERCHANT_TOKEN_BYTES}. A bearer token is tens of "
            f"characters, so a file this size is a path pointing at something else"
        )

    token = text.strip()
    if not token:
        raise DeploymentConfigurationError(
            f"{source}: the merchant bearer token file at {path} is empty. An empty token is "
            f"not 'no token configured' — it is a file somebody meant to fill in, and the "
            f"merchant would refuse every mint it was sent"
        )
    return token


def merchant_admin_token(
    deployment: Deployment | None, env: Mapping[str, str] | None = None
) -> str | None:
    """The bearer token this exchange presents to the merchant, or ``None`` when it has none.

    The document's :data:`MERCHANT_TOKEN_FILE_KEY` outranks :data:`ENV_MERCHANT_ADMIN_TOKEN`,
    for the reason every other key in this module outranks its environment fallback: the
    document is the deployment's own statement and the variable is the shipped default.
    """
    environ = os.environ if env is None else env
    stated = None if deployment is None else deployment.merchant_admin_token_file
    if stated:
        source = "the deployment document" if deployment is None else deployment.source
        return read_merchant_admin_token(stated, source=source)
    return str(environ.get(ENV_MERCHANT_ADMIN_TOKEN) or "").strip() or None


class HttpMerchantCodeCreator:
    """The exchange's client for the merchant's published ``POST /codes`` (R3).

    This is the outbound half of the join R3 names, and it lives here for the reason
    :class:`HttpBidSolicitor` and :class:`HttpTrustSnapshot` do: this module is where this
    service's outbound clients live, and ``apps/exchange/src/checkout/`` must stay
    transport-free and merchant-free — ``test_the_checkout_package_imports_nothing_shopify_or_
    merchant_shaped`` is that property, and it is what makes D45's simulated path reachable
    with no Shopify install lane in the tree at all.

    It exposes exactly ``create_code(store_id, offer)``, which is the call shape
    :class:`~exchange.checkout.providers.ShopifyCheckoutProvider` prefers, and it returns the
    merchant's reply **unflattened** — the adapter reads ``code`` and ``permalink_url`` off it
    and the contracts boundary judges the code, so inventing a shape here would put a second
    reading between the merchant's answer and the one that is graded.

    Failures are :class:`MerchantCodeCreationRefused`, and every message is built from three
    things and nothing else: the door's url, the store id, and the merchant's own
    machine-readable ``error`` slug. **The merchant's ``detail`` is deliberately dropped.**
    That field is ``str(exc)`` from the merchant's own refusals, those refusals are raised from
    inside its mint, and this sentence becomes the accept's ``denial_reason`` — republished in
    the 409 and persisted in the refusal event. A live discount code has no business in either,
    and "probably no code is in that string" is not the standard a publication path is held to.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = DEFAULT_MERCHANT_TIMEOUT_SECONDS,
        client: Any | None = None,
    ) -> None:
        self.url = str(url)
        self._token = str(token)
        self._timeout = float(timeout)
        self._client = client

    def __repr__(self) -> str:
        # Never the token: this object hangs off `app.state`, and `app.state` is rendered by
        # more debug surfaces than anyone can enumerate.
        return f"{type(self).__name__}({self.url!r})"

    def create_code(self, store_id: str, offer: Any) -> Mapping[str, Any]:
        """Ask the merchant to mint one single-use discount on ``store_id``'s own shop.

        Returns the merchant's 201 body — the pinned ``{code, permalink_url, expires_at}``.

        Raises:
            MerchantCodeCreationRefused: the merchant could not be reached, answered anything
                but ``201``, or answered a ``201`` this exchange cannot read as a record.
        """
        payload = {
            "store_id": str(store_id),
            "offer": dict(offer) if isinstance(offer, Mapping) else offer,
        }
        headers = {
            "authorization": f"Bearer {self._token}",
            "accept": "application/json",
        }

        try:
            with self._http_client().stream(
                "POST", self.url, json=payload, headers=headers, timeout=self._timeout
            ) as response:
                status = int(response.status_code)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_MERCHANT_REPLY_BYTES:
                        # Stop READING, not merely stop using. Leaving the block closes the
                        # connection; a reply this large is not the pinned three-key record,
                        # whatever else it is.
                        raise MerchantCodeCreationRefused(
                            f"the merchant at {self.url} answered more than "
                            f"{MAX_MERCHANT_REPLY_BYTES} bytes for store {str(store_id)!r}; "
                            f"POST {MERCHANT_CODES_PATH} answers a three-key record, so this "
                            f"is not one and no code can be read out of it"
                        )
        except MerchantCodeCreationRefused:
            raise
        except Exception as exc:
            # `describe_exception` rather than `{exc}`: a transport failure's message carries
            # addresses and default reprs (T-264), and this sentence is published to the buyer.
            raise MerchantCodeCreationRefused(
                f"the merchant at {self.url} could not be reached to mint a code for store "
                f"{str(store_id)!r} ({describe_exception(exc)}); no permalink is returned. If "
                f"the merchant had already minted, its own code_created ledger record is where "
                f"that discount is recoverable from — this exchange never saw it"
            ) from exc

        if status != 201:
            raise MerchantCodeCreationRefused(
                f"the merchant at {self.url} answered HTTP {status} "
                f"({self._error_slug(bytes(body))}) for store {str(store_id)!r}; no code was "
                f"created. The merchant's own detail is deliberately not repeated here — it is "
                f"written by the mint and this sentence is published to the buyer"
            )

        try:
            document = json.loads(bytes(body))
        except Exception as exc:  # noqa: BLE001 - RecursionError is not a ValueError
            raise MerchantCodeCreationRefused(
                f"the merchant at {self.url} answered a 201 that is not valid JSON "
                f"({type(exc).__name__}) for store {str(store_id)!r}"
            ) from exc
        if not isinstance(document, Mapping):
            raise MerchantCodeCreationRefused(
                f"the merchant at {self.url} answered a 201 carrying a "
                f"{type(document).__name__} rather than a code record, for store "
                f"{str(store_id)!r}"
            )
        return dict(document)

    @staticmethod
    def _error_slug(body: bytes) -> str:
        """The merchant's machine-readable ``error`` word, and nothing else it said.

        A short kebab-case slug — ``combines-with-conflict``, ``shop-not-installed``,
        ``unauthorized``, ``admin-api-not-configured`` — is the half of a merchant refusal an
        operator acts on, it is drawn from a fixed vocabulary in
        ``apps/merchant/svc/src/codes/routes.py``, and it carries no offer field and no code.
        Anything that is not such a word is reported as absent rather than quoted.
        """
        try:
            document = json.loads(body)
            slug = document.get("error") if isinstance(document, Mapping) else None
        except Exception:  # noqa: BLE001 - an unreadable body is simply not a slug
            return "no machine-readable reason"
        if not isinstance(slug, str) or not slug.strip():
            return "no machine-readable reason"
        word = slug.strip()
        if len(word) > 64 or not word.replace("-", "").replace("_", "").isalnum():
            return "no machine-readable reason"
        return word

    def _http_client(self) -> Any:
        """One pooled client for this creator, built on first use.

        Deferred for the reason :meth:`HttpBidSolicitor._http_client`'s is: constructing a
        creator — which a config check or a test does — must open no sockets.
        """
        if self._client is None:
            import httpx  # noqa: PLC0415 — see the docstring

            self._client = httpx.Client(timeout=self._timeout)
        return self._client


def effective_checkout_mode(
    app: Any, deployment: Deployment | None, env: Mapping[str, str] | None = None
) -> str:
    """The mode this app will actually resolve a provider for, in the route's own precedence.

    ``app.state.checkout_mode`` first (what :func:`configure_accept` bound), then the
    document's ``checkout_mode``, then :data:`CHECKOUT_MODE_ENV`, then
    :data:`~exchange.checkout.registry.DEFAULT_CHECKOUT_MODE`. That is exactly
    ``accept/routes.py::_checkout_mode`` plus the one rung this module owns, and it is spelled
    against the route's own constant rather than a second ``"CHECKOUT_MODE"`` literal — two
    spellings of "which mode is this exchange in" is how the composition root ends up refusing
    a deployment the route would have served, or composing one it would not.
    """
    from .accept.routes import CHECKOUT_MODE_ENV  # noqa: PLC0415 — see the import note

    environ = os.environ if env is None else env
    configured = getattr(app.state, "checkout_mode", None)
    if configured:
        return str(configured)
    if deployment is not None and deployment.checkout_mode:
        return str(deployment.checkout_mode)
    return str(environ.get(CHECKOUT_MODE_ENV) or DEFAULT_CHECKOUT_MODE)


def _mode_mints_on_the_merchant(mode: str) -> bool:
    """Whether the provider for ``mode`` needs the injected merchant client.

    Asked of the REGISTRY rather than of a list of spellings kept here. A second list would
    stop agreeing with :func:`~exchange.checkout.registry.register_provider` the first time a
    deployment adds a provider, and the disagreement is silent in the dangerous direction: a
    mode this module has never heard of would compose with no creator and refuse every accept.

    An unregistered mode answers ``False``, because it is not this function's refusal to make:
    the document's ``checkout_mode`` is refused at parse, and an unregistered
    ``CHECKOUT_MODE`` variable is a 503 from the accept route's own
    :class:`~exchange.checkout.registry.UnknownCheckoutMode` handler, which names the
    registered modes. Raising a second, differently worded refusal here would only hide that.
    """
    try:
        provider = resolve_provider(mode)
    except UnknownCheckoutMode:
        return False
    return bool(getattr(provider, "requires_code_creator", False))


def bind_code_creator(
    app: Any, deployment: Deployment | None, env: Mapping[str, str] | None = None
) -> bool:
    """Bind the merchant ``POST /codes`` client unless this app already has one (R3).

    **This is the seam R3 was missing.** ``accept()`` has always taken a ``code_creator`` and
    :func:`~exchange.accept.routes.configure_accept` has always bound one — and nothing in this
    repository ever called it, so ``app.state.code_creator`` was ``None`` in every shipped
    deployment. The measured consequence is in the module header: a deployment stating
    ``"checkout_mode": "shopify"`` refused every accept it served, and the merchant's
    ``combinesWith`` check was unreachable from any composed exchange.

    The ladder, and what each rung refuses:

    1. **Something already bound one** — a test, or a deployment that called
       ``configure_accept`` itself. Kept, untouched; this module never overwrites wiring.
    2. **An address is known** (:func:`merchant_codes_endpoint`) — bind. The token must then
       resolve (:func:`merchant_admin_token`) or this **raises**: the merchant refuses a
       tokenless caller by design, so a creator bound without one is a seam that looks wired
       and mints nothing, which is the exact shape of defect this module exists to end.
    3. **No address, and this app's mode mints locally** (``redirect`` — D45's required
       starting implementation) — bind nothing and return ``False``. That is the starting
       slice, and it must keep running with no merchant, no Shopify and no network.
    4. **No address, and this app's mode mints on the merchant** — **raise**, so both served
       routes answer a 503 naming :data:`MERCHANT_URL_KEY`.

    Rung 4 is the one worth being explicit about, because there is a tempting third option and
    it is the worst of the three. Falling back to a local mint would let the accept SUCCEED:
    the buyer would be handed a real-looking ``PSX-`` code and a permalink, and the store — who
    was never asked and holds no such discount — would reject it at the till. A refusal costs
    that buyer the purchase; a silent local mint costs them the purchase *and* tells them it
    worked, and tells the operator nothing at all.

    Returns whether it bound.
    """
    from .accept.routes import configure_accept  # noqa: PLC0415 — see the import note

    if getattr(app.state, "code_creator", None) is not None:
        return False

    stated = "the deployment document" if deployment is None else deployment.source
    url, source = merchant_codes_endpoint(
        None if deployment is None else deployment.merchant_url, env
    )

    if url is None:
        mode = effective_checkout_mode(app, deployment, env)
        if not _mode_mints_on_the_merchant(mode):
            return False
        raise DeploymentConfigurationError(
            f"{stated}: checkout_mode {mode!r} mints the discount code on the merchant's own "
            f"store, through its POST {MERCHANT_CODES_PATH} door, and this exchange has been "
            f"given no address for it — so every accept it serves is refused "
            f"`checkout_refused`. State {MERCHANT_URL_KEY!r} in the deployment document (or "
            f"set {ENV_MERCHANT_URL}), and give it the merchant's bearer token through "
            f"{MERCHANT_TOKEN_FILE_KEY!r} (or {ENV_MERCHANT_ADMIN_TOKEN}). Refused rather than "
            f"minted locally: a code this exchange invents is one the store has never heard "
            f"of, so the buyer would be redirected to a checkout that rejects it"
        )

    token = merchant_admin_token(deployment, env)
    if not token:
        raise DeploymentConfigurationError(
            f"{stated}: this exchange is pointed at the merchant's POST {MERCHANT_CODES_PATH} "
            f"door at {url} and has no bearer token for it. That door creates real spendable "
            f"discounts, so it refuses every caller who presents none — an exchange bound "
            f"without one is a seam that looks wired and mints nothing. State "
            f"{MERCHANT_TOKEN_FILE_KEY!r} (a file holding the token, never the token itself), "
            f"or set {ENV_MERCHANT_ADMIN_TOKEN} in this exchange's environment"
        )

    configure_accept(app, code_creator=HttpMerchantCodeCreator(url, token))
    _log.info(
        "exchange checkout: discount codes will be created on the store by the merchant at %s "
        "(from %s). A merchant that refuses or cannot be reached refuses the accept; no code "
        "is ever minted locally in a mode that mints on the merchant",
        url,
        _MERCHANT_SOURCES.get(source, source),
    )
    return True


#: How the wiring-time line names where the merchant's address came from. The twin of
#: :data:`_ENDPOINT_SOURCES`, and separate from it because that one names ``trust_url`` in as
#: many words — one mapping for two doors would print the wrong key at exactly the moment an
#: operator is reading the line to find out which key to set.
_MERCHANT_SOURCES: Mapping[str, str] = {
    SOURCE_STATED: f"the deployment document's {MERCHANT_URL_KEY}",
    SOURCE_ENVIRONMENT: f"the {ENV_MERCHANT_URL} environment variable",
}


# =====================================================================================
# Binding
# =====================================================================================
def bind_auction_machine(
    app: Any, deployment: Deployment | None, env: Mapping[str, str] | None = None
) -> bool:
    """Bind this app's auction state machine unless it has one. Returns whether it bound.

    Two things are decided here, and until this function existed only the first of them was.

    **The ledger sink (T-150).** Bound with NO key required, unlike almost every other branch
    in :func:`configure_exchange`, for two reasons. The document states only WHERE —
    ``trust_url``, falling back to the environment and then to :data:`DEFAULT_TRUST_URL` —
    because an exchange that has to be told to keep an audit trail is an exchange that ships
    without one. And binding it here rather than leaving it to ``auction/routes.py::_machine``
    closes a narrower hole than it looks: ``accept/routes.py`` has its own ``_machine``
    accessor with its own bare ``AuctionStateMachine()`` default, so an app whose FIRST auction
    request is an accept — an auction another process created against a shared store — would
    otherwise install a sink that goes nowhere and drop its ``accepted`` event. The composition
    hook runs before both accessors in both routes.

    **The STORE**, which is new, and whose absence was the defect. The comment this docstring
    replaces read "the store is the same ``InMemoryAuctionStore`` either lazy default builds,
    so nothing else moves" — accurate, and it described an exchange in which the operator had
    no way to move it. ``exchange.auction.state`` documents ``RedisAuctionStore`` as the
    DESIGN-pinned store and says "a deployment that cares runs" it; no deployment could, so
    every served exchange kept every auction it had ever opened in a process-local ``dict``.

    The order is document, then environment, then neither:

    * ``deployment.auction_store`` (the document's :data:`AUCTION_STORE_KEY`) wins where it is
      stated, because a document is a deliberate statement about one deployment while an
      environment variable is inherited by every process that happens to have it set.
    * :data:`~exchange.auction.state.ENV_AUCTION_STORE` otherwise. It exists BESIDE the
      document key rather than instead of it because the two reach different operators:
      ``apps/exchange/compose.yaml`` forwards named variables into a container whose deployment
      document is generated by ``scripts/build_demo_deployment.py``, so a compose operator has
      no document to edit and a document author has no compose file to edit.
    * Neither: ``None``, and :class:`~exchange.auction.state.AuctionStateMachine` builds its
      own bounded :class:`~exchange.auction.state.InMemoryAuctionStore`. That default is safe
      to leave in place NOW in a way it was not before — it holds
      :data:`~exchange.auction.state.DEFAULT_AUCTION_CAPACITY` auctions and evicts, so an
      unconfigured exchange no longer grows without limit under an unauthenticated caller.

    A store that is named and cannot be built is a :class:`DeploymentConfigurationError` — a
    503 naming the store, through both routes' hooks — and never a silent fall back to the
    in-memory one. An operator who wrote ``redis`` asked for auctions that outlive one process
    and reservations that constrain every replica; handing them neither, quietly, on the money
    path, is the failure this whole seam exists to make impossible.
    """
    from .auction.routes import configure_auctions  # noqa: PLC0415
    from .auction.state import (  # noqa: PLC0415
        ENV_AUCTION_STORE,
        AuctionStateMachine,
        AuctionStore,
        AuctionStoreUnavailable,
        auction_store_from_env,
        build_auction_store,
    )

    if getattr(app.state, "auction_machine", None) is not None:
        return False

    source = deployment.source if deployment is not None else "the environment"
    stated = deployment.auction_store if deployment is not None else None
    store: AuctionStore | None
    try:
        if stated is not None:
            store = build_auction_store(stated, env=env)
            named = f"the deployment document's {AUCTION_STORE_KEY}"
        else:
            store = auction_store_from_env(env)
            named = f"the {ENV_AUCTION_STORE} environment variable"
    except AuctionStoreUnavailable as exc:
        raise DeploymentConfigurationError(f"{source}: {exc}") from exc

    if store is not None:
        # Logged, because "which store is this exchange holding auctions in" is the one fact
        # about this binding an operator cannot read back off any route: both stores answer
        # `POST /auctions` identically until the process restarts or a second replica appears.
        _log.info("auction store: %s, from %s", type(store).__name__, named)

    trust_url = deployment.trust_url if deployment is not None else None
    configure_auctions(
        app, machine=AuctionStateMachine(store=store, ledger=bind_ledger_sink(trust_url))
    )
    return True


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
    from .external_bids.routes import configure_external_bids  # noqa: PLC0415
    from .ranking.serving import configure_ranking  # noqa: PLC0415

    bound: list[str] = []

    def unset(name: str) -> bool:
        return getattr(app.state, name, None) is None

    if bind_auction_machine(app, deployment, env):
        bound.append("auction_machine")

    if bind_eligibility(app, deployment, env):
        bound.append("seller_eligibility")

    endpoints = deployment.bid_endpoints
    if endpoints and unset("bid_solicitor"):
        configure_auctions(app, solicitor=HttpBidSolicitor(endpoints))
        bound.append("bid_solicitor")

    if unset("shop_roster"):
        # D55's ORGANIC HALF, and the only seam in this service that reads the catalogue
        # graph on a served request. Bound from the ENVIRONMENT rather than from the
        # deployment document, and opt-in (`EXCHANGE_SHOP_ROSTER=graph`), for a reason that is
        # a deployment fact rather than a preference: `apps/exchange/Dockerfile` ships
        # `ingest.graph` but the `neo4j` DRIVER is imported lazily inside
        # `ingest.graph.reembed.graph_driver`, so an image without that wheel resolves every
        # module it imports and only this path would ever notice. Making the operator say
        # "this deployment reads the graph" keeps the wheel and the wiring one decision.
        #
        # `graph_roster_from_env` answers `None` when unconfigured and CONNECTS TO NOTHING
        # when it does answer — the driver is built on the first solicitation, so an exchange
        # pointed at a Neo4j that is down still serves every caller who brings its own roster.
        # Nothing is bound when it answers `None`, which leaves `auction/routes.py`'s
        # `NoShopRoster` default: finds nobody, says so, changes no existing behaviour.
        from .retrieval.roster import graph_roster_from_env  # noqa: PLC0415

        graph_roster = graph_roster_from_env(env)
        if graph_roster is not None:
            configure_auctions(app, shop_roster=graph_roster)
            bound.append("shop_roster")

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

    if unset("ranking_catalog"):
        # D55's EVIDENCE HALF, and the other end of the seam the graph roster above opened.
        # The roster decides who may make their case; this decides what their case is CHECKED
        # AGAINST, which is the asymmetry that makes a persuasion market safe — a seller's
        # purchased message is graded against the platform's own snapshot of the catalogue.
        # Until this line the only snapshots this exchange could hold were the ones an operator
        # typed into the block above, so an exchange whose roster came out of a crawl of 3,093
        # real products verified every claim against a hand-authored document, or against
        # `NoCatalogSnapshots` and therefore against nothing.
        #
        # SECOND, so a stated `catalog` wins. A document that names snapshots has made a
        # statement about what this exchange holds, and a `neo4j` wheel being present must not
        # overrule it.
        #
        # Bound from the ENVIRONMENT rather than from the document for the reason the roster is
        # — reading the graph is a deployment fact, not a preference — but through its OWN env
        # var, `EXCHANGE_RANKING_CATALOG`, which DEFAULTS to the roster's setting. Sharing one
        # switch would make each decision unstateable without the other, and they are genuinely
        # different powers: a deployment can serve request-stated rosters of crawled store ids
        # and still want their claims checked against the crawl, and an operator who turned on
        # organic discovery has not thereby said "believe my crawl about every sponsored
        # claim". Defaulting it on with the roster is the fix rather than a convenience: an
        # exchange that reads the graph to find shops and holds no catalogue verifies nothing
        # about anybody it just found. See `graph_catalog_from_env` for the full argument.
        #
        # `graph_catalog_from_env` answers `None` when unconfigured and CONNECTS TO NOTHING
        # when it does answer — the driver is built on the first lookup, so an exchange pointed
        # at a Neo4j that is down still serves every caller. FAIL CLOSED at both ends: nothing
        # is bound when it answers `None`, which leaves `ranking.serving.catalog_of`'s
        # `NoCatalogSnapshots` — a snapshot for nobody, every claim `unsupported`, R19 refusing
        # to let one satisfy a hard constraint — and an unreachable graph answers `None` per
        # lookup, which is the same denial. There is no path here to a permissive catalogue.
        from .retrieval.catalogue import graph_catalog_from_env  # noqa: PLC0415

        graph_catalog = graph_catalog_from_env(env)
        if graph_catalog is not None:
            configure_ranking(app, catalog=graph_catalog)
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

    if deployment.report_tokens_file and unset("report_tokens"):
        # R9's merchant-facing door. Bound only when the document names a file, so an exchange
        # that has not asked for reports serves nobody — which the route answers as a 503 that
        # names this key, rather than as an empty report that reads like "you never lose".
        from .reports.routes import configure_reports  # noqa: PLC0415

        configure_reports(
            app,
            tokens=read_report_tokens(deployment.report_tokens_file, source=deployment.source),
        )
        bound.append("report_tokens")

    if deployment.checkout_mode and unset("checkout_mode"):
        configure_accept(app, checkout_mode=deployment.checkout_mode)
        bound.append("checkout_mode")

    # R3's missing wire, and it runs AFTER the mode is on `app.state` on purpose:
    # `effective_checkout_mode` reads that key first, so binding the mode above is what lets
    # this decide whether the deployment needs a merchant at all. Nothing is bound in the
    # starting slice (`redirect` mints locally, D45); a mode that mints on the merchant with no
    # address configured raises here and both routes answer 503.
    if bind_code_creator(app, deployment, env):
        bound.append("code_creator")

    if unset("external_bid_queue"):
        # The signed external door's verification queue (R8/R18). Bound with NO document key
        # required, for the reason the ledger sink above is: the door refuses
        # `verification_queue_unavailable` when it finds nothing, so an exchange that has to be
        # told to keep the bids it admitted is an exchange that admits none — and this seam,
        # like the keyring, had no production caller at all until now. WHERE it lands is not a
        # setting for the same reason `trust_url` defaults: Redis is this service's own
        # datastore (D39), already declared and health-checked in `apps/exchange/compose.yaml`.
        #
        # It admits nothing on its own. A deployment that states no keyring still refuses every
        # submission at the gate before this one, so binding a queue unconditionally changes
        # what an ADMITTED bid does and never who is admitted.
        from .external_bids.verification_queue import RedisVerificationQueue  # noqa: PLC0415

        configure_external_bids(app, queue=RedisVerificationQueue())
        bound.append("external_bid_queue")

    if deployment.external_bid_keyring_file and unset("external_bid_keyring"):
        # THE SECRETS ARE READ HERE AND NOWHERE ELSE. `deployment` names the file; this is the
        # only line in the process that opens it, so the keys exist in one object hanging off
        # `app.state` and never in a parsed document that a caller might log, echo or re-serve.
        configure_external_bids(
            app,
            keyring=read_external_bid_keyring(
                deployment.external_bid_keyring_file, source=deployment.source
            ),
        )
        bound.append("external_bid_keyring")

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
        # And an exchange nobody configured still MINTS WHERE IT SAYS IT DOES. `CHECKOUT_MODE`
        # is a plain environment variable that `accept/routes.py` reads per request, so an
        # exchange with no document at all can still be running a mode that mints on the
        # merchant — and `MERCHANT_URL`/`MERCHANT_ADMIN_TOKEN` are enough to compose one. With
        # neither, this raises and both routes answer a 503 naming the key, instead of the
        # per-accept `checkout_refused` a buyer used to be handed. `bind_code_creator` is
        # idempotent and returns immediately once something is bound, so this costs one bind
        # and not one per request.
        bind_code_creator(app, None, env)
        # And an exchange nobody configured still HOLDS ITS AUCTIONS SOMEWHERE. This is the
        # branch the shipped container can actually land in: `apps/exchange/compose.yaml`
        # forwards named variables into an image whose deployment document is generated by a
        # script, so `EXCHANGE_AUCTION_STORE` has to work with no document at all or the seam
        # is unreachable from compose — the `EXCHANGE_SHOP_ROSTER` trap, which that file's own
        # comment records as "⚠️ IT DOES NOTHING ON ITS OWN".
        #
        # It also binds the LEDGER SINK, which this branch did not do at all: an exchange with
        # no document previously left `auction/routes.py::_machine` to install one on the first
        # write, and that accessor's own docstring records the door it cannot cover —
        # `GET /auctions/{id}` reaches it with no composition hook in front of it, so a read
        # arriving first installed the fallback for the life of the process. `bind_ledger_sink`
        # falls back to `ENV_TRUST_URL` and then `DEFAULT_TRUST_URL` exactly as that accessor
        # does, so the sink is the same one and only its timing moves.
        bind_auction_machine(app, None, env)
        # Deliberately NOT cached. "No deployment configured" is two `os.environ` lookups to
        # re-establish, and caching it meant a document that appeared after the first request
        # was ignored for the life of the process — a real trap for an operator who starts the
        # exchange and then writes the file. Only a SUCCESSFUL bind is remembered; a failure is
        # not cached either, so a fixed document is picked up by the next request.
        return ()
    bound = configure_exchange(app, deployment, env)
    setattr(app.state, STATE_FLAG, bound)
    return bound
