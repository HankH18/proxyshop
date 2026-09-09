#!/usr/bin/env python3
"""Put the demo's sellers into the LIVE trust service, through its own published doors.

Why this script exists
----------------------
``deploy/demo/exchange-deployment.json`` used to carry a ``trust_snapshot`` key: ten stores,
fixed alpha/beta, one hand-written ``score`` each. ``exchange.composition
._bind_live_ranking_snapshot`` returns without binding a live reader whenever the document
states that key, so the demo's whole ``trust`` column was a sorted copy of a typed-in table
and nothing that happened during a demo could move it.

The key is gone. The ranking gate now reads ``GET /snapshot`` on the running trust service,
which is fail-closed by design: :func:`exchange.ranking.filters.blacklist_reason` excludes a
store the snapshot holds no row for (R12), so on a stack where trust knows nobody the
shortlist is EMPTY. That is strictly worse than a frozen column, and it is what this script
prevents. Run it once after the stack is up, before the demo.

What "in the snapshot" actually requires
----------------------------------------
Two things, and the first one has no product writer anywhere in this tree:

1. **a row in ``app.sellers``.** ``trust.snapshot.routes._STORES_SQL`` is
   ``app.sellers LEFT JOIN ledger.trust_observations``, so a store with no seller row is not
   in the snapshot however many events name it. Nothing outside tests inserts into that
   table, so this script does. It connects as ``admin`` — the cluster superuser, which writes
   by bypassing grants rather than by holding one; ``db/migrations/0004_object_grants.sql``
   gives the ``app`` role INSERT on the schema, and ``trust_rw`` (the role the trust service
   itself uses) only SELECT.
2. **observations**, which arrive as sealed ledger events through ``POST /events``.
   ``trust.events.observations.persist_event_observations`` projects each appended event into
   ``ledger.trust_observations`` on the append path, which is the row ``GET /snapshot`` reads.

Both are done here over the service's own doors, and this script never touches
``ledger.trust_observations`` itself. Three product writers do (``trust.events.observations``,
``trust.reconcile.routes``, ``trust.verification.persistence``) and each writes a row some
event implies; a row written from HERE would imply nothing, and R15/S3 (*replaying the ledger
reproduces the served scores*) would be false in a way no test could see, permanently, because
the chain has no delete.

Marked, permanently, inside the hash chain
------------------------------------------
Every event this script writes carries an ``order_ref`` beginning
:data:`~seed.provenance.SEED_MARKER_PREFIX` (``sim-fb-``) — the same marker
``apps/buyer/app/feedback/seed-data/collection.json`` declares and the buyer service copies
verbatim onto its own sealed events. ``order_ref`` is a top-level ledger column inside the
digest, so a manufactured observation cannot be un-marked and an earned one cannot be marked
without breaking ``GET /events/verify``. ``payload.simulated`` is a second, weaker courtesy
for a human reading one event; the ``order_ref`` is the load-bearing one.

Two kinds of seller, and the difference is which dimensions exist at all
------------------------------------------------------------------------
:data:`~build_demo_deployment.DEMO_SELLERS` is two tables, and this script writes whatever
:func:`build_demo_deployment.dimension_posteriors` returns for each — it has no rule of its
own about which dimensions a store gets.

**The ten TRANSACTING storefronts** (``TRUST_SCORES``) get five of the six:
``price_honored``, ``discount_honored``, ``shipped_on_time``, ``not_returned`` and
``catalog_claim_accuracy``. They carry each store's stated posture, read from the same
``TRUST_SCORES`` and ``DISPATCH_POSTERIOR`` tables the removed ``trust_snapshot`` key was
built from — so the demo's story about who is more reliable survives the switch to a live read.

**The nine CRAWLED storefronts** (``CATALOGUE_ACCURACY``) get ONE: ``catalog_claim_accuracy``.
They are organic results under D55 — shops the platform scraped, that bought nothing and
promised nothing — so the four dispatch-and-payment dimensions have no evidence behind them
and are seeded with none. They are served ``low_data: True`` as a result, which
``trust.snapshot.builder`` defines as "treat as *unknown* rather than *average*" and is the
honest reading of a shop nobody has ever ordered from. :func:`verify` therefore expects that
flag for them and fails on it only for a transacting store.

The NUMBERS are lower than the ones that key stated, and the reason is the correction of a
real defect rather than a compromise. That key wrote an absolute posterior — ``alpha = mean *
14`` — which for ``gaiaherbs.com``'s dispatch record was ``Beta(13.02, 0.98)``, a ``beta``
BELOW the ``Beta(2, 2)`` prior and therefore a posture no quantity of observations can reach.
It read as a fact and was unreachable, and nothing noticed while the exchange read the
document instead of the service. What is seeded is evidence, over the prior, so 0.86 of
positive evidence is SERVED as about 0.73. Same order, same story, arithmetic that closes.

``feedback_match`` is seeded with **nothing at all** and sits at the neutral ``Beta(2, 2)``
prior. That is both the honest answer — no buyer has ever left feedback for these
storefronts, which is what ``deploy/demo-seed/README.md`` already says of the four hosted ones
("no feedback chain because nobody has ever left them feedback") — and the one choice that
makes the demo's button visible:

* ``POST /buyer/feedback`` is the only door a demo audience can push, and every event it
  seals names ``dim: feedback_match`` and nothing else — the buyer service writes it from its
  own ``buyer_svc.feedback.submission.FEEDBACK_DIMENSION`` (deliberately its own copy, not an
  import from ``apps/trust``), and ``trust.ledger.replay.observations_from_events`` emits at
  most one observation per event. One press moves ONE of the six dimensions.
* a store's ``score`` is the unweighted mean of the six dimension MEANS
  (``trust.scoring.engine.score``), not a mass-weighted pool over all evidence. So evidence on
  the other five dimensions does not damp the button AT ALL, and seeding them lighter would
  buy nothing while pushing the stated posture out of reach. The only thing that damps the
  button is prior evidence on ``feedback_match``, and there is none.

Measured consequence, on the served route: the 24 events one press of the learning page's
button feeds a store (``OUTCOME_ROUNDS`` 6 x ``REVIEWERS_PER_ROUND`` 4, in
``apps/buyer/app/learning``) take ``feedback_match`` from 0.5 to 0.929 if every reviewer is
positive, or to 0.050 if every one is negative — a ceiling of +0.071 / -0.075 on the store's
published ``score``.

A press is a MIX, not a sweep: ``apps/buyer/app/learning/loop.ts`` sets each store's verdict
from whether it discounted that round. Measured on the served route, one press came out 20
positive / 4 negative for ``oregonswildharvest.com`` (+0.038888) and 16/8 for
``paradiseherbs.com`` (+0.010416). The seeded gaps between adjacent HOSTED stores — the four
that bid, and so the only four a press reaches — are 0.0245 to 0.0312, which the 20/4 mix
clears and the 16/8 mix does not. ``scripts/tests/test_seed_demo_trust.py`` asserts both the
ceiling and the measured mix against the real scorer, so a seed too heavy to move fails a test
rather than a demo.

``low_data``, which is not a detail
------------------------------------
``trust.snapshot.builder.clean_episodes`` derives a store's episode count as the minimum,
over :data:`~trust.snapshot.builder.EPISODE_FLOOR_DIMENSIONS` (the five a transacting store is
seeded on, feedback excluded), of how many POSITIVE observations that dimension carries;
``low_data`` is that count under ``NEW_STORE_PRIOR_N`` (5). So the positive observations are
emitted as whole full-weight rows rather than one fat weighted row: seven ``fulfilled``
events read as seven episodes, one event of weight 7.0 is impossible (the channel caps at
1.0) and one of weight 1.0 would read as a single episode and flag every store low-data.
:func:`observations_for` refuses rather than silently seeding a low-data store.

For a CRAWLED seller the minimum is over four dimensions carrying nothing, so it is 0 and the
flag is ``True`` however good its catalogue posture is. That is the intended answer and not a
shortfall in the seed: the exchange should treat a shop it has only read as unknown. What it
costs is the exploration slice — one shortlist slot of four, and only when such a store is on
the bench — which is the exchange doing the thing that flag exists for.

Idempotent
----------
Event ids are derived from ``(store, dim, index)``, so a second run re-sends the same ids and
the ledger answers 200 to every one of them. ``observed_at`` is what makes that true rather
than a 409: ``event_id`` is the idempotency key and re-sending an id with DIFFERENT content is
refused (D16), so a run that stamped a fresh clock reading would fail on its second event —
measured, by running ``make demo-trust`` twice. :func:`seeded_instant` reads the instant this
script already sealed back off the chain and reuses it, so the whole re-run is byte-identical.
A first run on a clean stack uses the clock; ``--observed-at`` overrides both.

Decay is a 30-day half-life against the serve instant, so a seeded stack drifts gently toward
neutral over weeks — the trust engine behaving correctly, not this script decaying.
``--check`` prints that drift and does not fail on it.

Usage
-----
::

    ./.venv/bin/python scripts/seed_demo_trust.py            # seed, then verify
    ./.venv/bin/python scripts/seed_demo_trust.py --check    # verify only, write nothing

Exit status is the finding: **0** every demo seller is in the live snapshot, unblacklisted,
not ``low_data``, and every seeded dimension on the side of neutral the seed put it; **1** one
of those is false; **2** the stack could not be reached. Numeric movement away from the seeded
value is PRINTED and never fails -- decay and earned evidence both cause it, and a check that
went red because the platform had been used would be the fourth defect class this repository
keeps rediscovering.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.error
import urllib.request
from collections.abc import Collection, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_demo_deployment import (  # noqa: E402 - after the sys.path line above
    CATALOGUE_ACCURACY,
    DEMO_SELLERS,
    HOSTED,
    TRUST_DIMENSIONS,
    dimension_posteriors,
)

#: The marker every event this script writes carries in ``order_ref``.
#:
#: Restated rather than imported, because this script must run from a checkout that has not put
#: ``services/sim`` on the path — and a second literal IS a marker that stops matching the day
#: one of them is edited, so the agreement is a test rather than a hope:
#: ``scripts/tests/test_seed_demo_trust.py`` imports ``seed.provenance.SEED_MARKER_PREFIX`` and
#: asserts this equals it.
SEED_MARKER_PREFIX = "sim-fb-"

#: The trust service this seeds, and the database role that owns ``app.sellers``.
DEFAULT_TRUST_URL = "http://localhost:8084"

#: The dimension this script deliberately leaves at the prior. See the module docstring.
FEEDBACK_DIMENSION = "feedback_match"

#: The dimensions a ``claim_verified`` event carries, which is where the seed puts them.
#:
#: Both are in ``trust.scoring.dimensions.CLAIM_TYPE_DIMENSIONS``'s range — ``catalog_claim
#: _accuracy`` for the content claims and ``not_returned`` for ``return_policy`` and
#: ``warranty`` — so both really are announced on ``claim_verified`` by
#: ``exchange.ranking.verification`` and ``trust.claims.routes``. Restated here rather than
#: imported because this script must run without the trust package installed;
#: ``scripts/tests/test_seed_demo_trust.py`` pins that this set is the range of that table
#: minus the three ``trust.reconcile.engine`` grades.
CLAIM_VERIFIED_DIMENSIONS = frozenset({"catalog_claim_accuracy", "not_returned"})

#: The published observation types this script emits, and their polarity. Kept as literals
#: rather than imported from ``trust.scoring`` because this script must run from a checkout
#: that has not installed the trust package; :func:`observations_for` re-derives the numbers
#: those types imply and the verification step compares against what the SERVICE actually
#: served, so a drift in the published weights shows up as a failed ``--check`` rather than as
#: a wrong seed nobody noticed.
POSITIVE_TYPE = "fulfilled"
CLAIM_POSITIVE_TYPE = "verified"
NEGATIVE_TYPE = "contradicted"
NEGATIVE_TYPE_WEIGHT = 2.0

#: ``trust.scoring.engine.PRIOR_ALPHA`` / ``PRIOR_BETA`` — the neutral prior every dimension
#: starts at, which is what the seeded observations are added ON TOP of.
PRIOR_ALPHA = 2.0
PRIOR_BETA = 2.0

#: ``trust.scoring.engine.NEW_STORE_PRIOR_N``. Below this many clean episodes a store is
#: served ``low_data: True``, which turns on the exchange's exploration floor. See the module
#: docstring for why that is a seeding constraint and not a detail.
NEW_STORE_PRIOR_N = 5

#: How far a served dimension mean may sit from the seeded value before :func:`verify` REPORTS
#: it as drift. Reported, never failed -- decay and earned evidence both move it honestly, and
#: what ``--check`` fails on is the decay-proof half (the side of neutral). Sized to absorb the
#: minutes between seeding and verifying without narrating them.
POSTURE_TOLERANCE = 0.02


class SeedError(RuntimeError):
    """The stack could not be reached, or answered something unusable."""


# =====================================================================================
# what to write
# =====================================================================================
def _order_ref(store_id: str, dimension: str, index: int) -> str:
    """The marked, unique ``order_ref`` for one seeded observation.

    Unique per observation because ``order_ref`` is what a reader joins on: it is the marker,
    a top-level ledger column, and the handle ``trust.reconcile`` and ``trust.feedback`` use to
    say "these events are about the same purchase". One ref across a store's whole posture
    would state that its entire record came from a single fictional order.

    Not for the refund reason, which does not reach here: ``trust.ledger.replay`` discounts a
    positive report about an already-refunded order only for ``kind == "feedback"``, and this
    script emits none. The database does not enforce it either — ``ledger.commerce_events``
    indexes ``order_ref`` without a unique constraint.
    """
    return f"{SEED_MARKER_PREFIX}seed-{store_id}-{dimension}-{index:02d}"


def _split(mass: float, per_observation: float) -> list[float]:
    """``mass`` as a list of per-observation weights in ``(0, 1]``, each worth ``per_observation``.

    The scorer multiplies a type's published weight by the observation's own ``weight``, which
    is capped at 1.0 (``trust.scoring.MAX_OBSERVATION_WEIGHT`` — R14's "a single account cannot
    outvote the network"), so a mass larger than one observation is spread over several. The
    remainder rides on the LAST one so the leading rows are whole, full-strength observations:
    ``clean_episodes`` counts rows, not mass.
    """
    if mass <= 1e-9:
        return []
    whole = int(math.floor(mass / per_observation + 1e-9))
    remainder = mass - whole * per_observation
    weights = [1.0] * whole
    if remainder > 1e-9:
        weights.append(remainder / per_observation)
    return weights


def observations_for(
    store_id: str, dimension: str, alpha: float, beta: float
) -> list[dict[str, Any]]:
    """The observations that move ``dimension`` from the prior to ``(alpha, beta)``.

    Raises:
        SeedError: the target needs fewer than :data:`NEW_STORE_PRIOR_N` positive observations.
            For a store whose whole posture is seeded that means it would be flagged
            ``low_data`` no matter what its score said, and the refusal is a refusal rather
            than a clamp: a seeded store served ``low_data: True`` turns on the exchange's
            exploration floor, and a demo that silently acquired a second randomised mechanism
            would be very hard to read. For a CRAWL-ONLY store (:data:`CATALOGUE_ACCURACY`,
            one dimension) ``low_data`` is the intended answer and this floor is instead what
            stops a catalogue posture being stated on evidence too thin to mean anything; the
            fix in both cases is the same, so the message is the same.
    """
    positive_mass = float(alpha) - PRIOR_ALPHA
    negative_mass = float(beta) - PRIOR_BETA
    if positive_mass < -1e-9 or negative_mass < -1e-9:
        raise SeedError(
            f"{store_id}/{dimension} asks for Beta({alpha}, {beta}), which is BELOW the "
            f"Beta({PRIOR_ALPHA}, {PRIOR_BETA}) prior every dimension starts at. Observations "
            f"only ever add; there is no way to seed a store into knowing less than nothing."
        )

    positive_type = CLAIM_POSITIVE_TYPE if dimension in CLAIM_VERIFIED_DIMENSIONS else POSITIVE_TYPE
    rows: list[dict[str, Any]] = []
    positive_weights = _split(positive_mass, 1.0)
    if len(positive_weights) < NEW_STORE_PRIOR_N:
        raise SeedError(
            f"{store_id}/{dimension} needs only {len(positive_weights)} positive observation(s) "
            f"to reach Beta({alpha}, {beta}), but trust.snapshot.builder.clean_episodes takes the "
            f"MINIMUM positive count across the five non-feedback dimensions and flags a store "
            f"low_data below {NEW_STORE_PRIOR_N}. Raise DIMENSION_EVIDENCE in "
            f"scripts/build_demo_deployment.py, or lower this store's stated posture."
        )
    for weight in positive_weights:
        rows.append({"dim": dimension, "type": positive_type, "weight": weight})
    for weight in _split(negative_mass, NEGATIVE_TYPE_WEIGHT):
        rows.append({"dim": dimension, "type": NEGATIVE_TYPE, "weight": weight})
    return rows


def _payload(
    store_id: str, order_ref: str, row: Mapping[str, Any], observed_at: str
) -> tuple[str, dict[str, Any]]:
    """``(kind, payload)`` for one observation, on the kind that really carries that dimension.

    ``trust.scoring.dimensions.CLAIM_TYPE_DIMENSIONS`` is the routing table, and it decides two
    of the five: ``catalog_claim_accuracy`` and ``not_returned`` (the dimension
    ``return_policy`` and ``warranty`` claims land on) both ride ``claim_verified``, which is
    the kind ``exchange.ranking.verification`` and ``trust.claims.routes`` really emit for them
    and the only kind whose frozen payload (``contracts.LEDGER_PAYLOAD_SHAPES``) names a
    ``dim`` at all.

    The other three — ``price_honored``, ``discount_honored``, ``shipped_on_time`` — are
    exactly ``trust.reconcile.engine.RECONCILED_DIMENSIONS``, so they ride ``reconciled``.

    **Where this seed departs from a real producer, stated rather than implied.** A product
    ``reconciled`` event carries no ``dim`` and no ``type`` (``trust.reconcile.engine``'s module
    header says so, and its observations are announced separately on ``offer_integrity``), so
    the three fields that make these events scorable are this script's, not a producer's. That
    is unavoidable — the seed manufactures evidence no transaction produced, and there is no
    kind in the vocabulary that carries a dimension for those three — and it is why every one
    of these events is marked in ``order_ref``. What is NOT invented is the pairing: each
    dimension is on the kind that grades it.

    ``price_honored`` and ``discount_honored`` are required by ``reconciled``'s published
    shape, so they are always present, and each states the truth of its OWN dimension: on a
    ``dim: price_honored`` event ``price_honored`` carries this observation's polarity, and on
    a ``dim: shipped_on_time`` event both read ``True`` because this fiction is a late
    delivery and not a broken price. An earlier version set BOTH from the polarity of whatever
    dimension the event was about, which sealed "the price was dishonoured" into the chain
    every time a store shipped late.

    ``weight`` is written only when it is below full strength: ``trust.ledger.replay`` treats
    an absent weight as exactly 1.0 and compares VALUES for S3, so an explicit ``1.0`` would be
    a different event from the one a real producer emits for the same evidence.
    """
    dimension = str(row["dim"])
    payload: dict[str, Any] = {
        "dim": dimension,
        "type": str(row["type"]),
        "observed_at": observed_at,
        # Not the marker — `order_ref` is (see the module docstring). This is here so a human
        # reading one event out of `GET /events` sees what it is without knowing the prefix.
        "simulated": True,
    }
    weight = float(row.get("weight", 1.0))
    if weight < 1.0 - 1e-9:
        payload["weight"] = round(weight, 12)

    if dimension in CLAIM_VERIFIED_DIMENSIONS:
        payload["claim_ref"] = order_ref
        payload["status"] = payload["type"]
        return "claim_verified", payload

    honoured = payload["type"] != NEGATIVE_TYPE
    payload["order_ref"] = order_ref
    payload["price_honored"] = honoured if dimension == "price_honored" else True
    payload["discount_honored"] = honoured if dimension == "discount_honored" else True
    payload["pixel_missing"] = False
    return "reconciled", payload


def seed_events(observed_at: str, stores: Sequence[str] | None = None) -> Iterator[dict[str, Any]]:
    """Every ledger event this seed writes, in a fixed order.

    Order is fixed because the chain is ordered and a reseed of a partially-seeded stack must
    land the missing events where the first run would have put them.

    A CRAWL-ONLY seller yields events for ONE dimension, because
    :func:`~build_demo_deployment.dimension_posteriors` returns one entry for it. The five
    transaction dimensions are not skipped here by a rule in this function; there is simply
    nothing to write, which is the point — see :data:`~build_demo_deployment.CATALOGUE_ACCURACY`.
    """
    for store_id in stores if stores is not None else sorted(DEMO_SELLERS):
        posteriors = dimension_posteriors(store_id)
        for dimension in sorted(posteriors):
            if dimension == FEEDBACK_DIMENSION:
                continue
            target = posteriors[dimension]
            rows = observations_for(
                store_id, dimension, float(target["alpha"]), float(target["beta"])
            )
            for index, row in enumerate(rows):
                order_ref = _order_ref(store_id, dimension, index)
                kind, payload = _payload(store_id, order_ref, row, observed_at)
                yield {
                    "event_id": f"{SEED_MARKER_PREFIX}seed-{store_id}-{dimension}-{index:02d}",
                    "ts": observed_at,
                    "kind": kind,
                    "store_id": store_id,
                    "order_ref": order_ref,
                    "payload": payload,
                }


def expected_posture() -> dict[str, dict[str, float]]:
    """``{store_id: {dim: posterior mean}}`` the seed implies.

    A dimension this seed writes nothing for is at the bare prior, 0.5, and that covers both
    ``feedback_match`` (every seller: nobody has left feedback) and the five transaction
    dimensions of a crawl-only seller (nobody has transacted). Only the dimensions
    :func:`~build_demo_deployment.dimension_posteriors` actually returns carry a stated mean.
    """
    neutral = PRIOR_ALPHA / (PRIOR_ALPHA + PRIOR_BETA)
    posture: dict[str, dict[str, float]] = {}
    for store_id in sorted(DEMO_SELLERS):
        dims = dimension_posteriors(store_id)
        means: dict[str, float] = dict.fromkeys(TRUST_DIMENSIONS, neutral)
        for dimension, target in dims.items():
            if dimension == FEEDBACK_DIMENSION:
                continue
            alpha, beta = float(target["alpha"]), float(target["beta"])
            means[dimension] = alpha / (alpha + beta)
        posture[store_id] = means
    return posture


# =====================================================================================
# the two doors
# =====================================================================================
def _http(url: str, body: Any = None, *, timeout: float = 30.0) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - a URL this script was given
            return response.status, json.loads(response.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:500]
    except OSError as exc:
        raise SeedError(f"cannot reach {url}: {exc}") from exc


def trust_database(trust_url: str) -> str:
    """The DSN of the database the RUNNING trust service is using.

    Resolved by matching ``GET /events/head`` — the chain length and head digest the service
    itself reports — against each ``proxyshop_w*`` database on the cluster, rather than by
    reading ``PROXYSHOP_WORKER`` and hoping. That variable is a per-shell value; a stack
    brought up in one shell and seeded from another writes ``app.sellers`` into a database no
    running service reads, and the only symptom is an empty shortlist. The service's own chain
    head is not ambiguous in that way.

    Falls back to :func:`proxyshop_support.postgres.role_dsn` when the head matches several
    databases (every empty ledger looks alike) and that DSN is one of them. That fallback is
    resolved defensively rather than eagerly: ``role_dsn`` raises ``WorkerNotConfiguredError``
    when ``PROXYSHOP_WORKER`` is unset, and an unset worker is exactly the case this function
    exists to survive — the seeding operator's shell is not the shell the stack came up in.
    """
    from proxyshop_support.postgres import role_dsn

    status, head = _http(trust_url.rstrip("/") + "/events/head")
    if status != 200 or not isinstance(head, Mapping):
        raise SeedError(f"{trust_url}/events/head answered {status}: {head!r}")
    length, head_hash = int(head.get("length") or 0), str(head.get("head_hash") or "")

    try:
        configured: str | None = role_dsn("admin")
    except Exception:  # noqa: BLE001 - an unset PROXYSHOP_WORKER is a normal state here
        configured = None
    import psycopg

    with psycopg.connect(role_dsn("admin", database="postgres"), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select datname from pg_database where datname like 'proxyshop_w%' order by datname"
            )
            candidates = [str(row[0]) for row in cursor.fetchall()]

    matches: list[str] = []
    for database in candidates:
        dsn = role_dsn("admin", database=database)
        try:
            with psycopg.connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "select count(*), coalesce("
                    "(select event_hash from ledger.commerce_events order by seq desc limit 1), '')"
                    " from ledger.commerce_events"
                )
                rows = cursor.fetchone()
        except Exception:  # noqa: BLE001 - a database without the schema is simply not the one
            continue
        if rows and int(rows[0]) == length and str(rows[1]) == head_hash:
            matches.append(dsn)

    if not matches:
        raise SeedError(
            f"no database on this cluster holds the chain {trust_url} is serving "
            f"(length {length}, head {head_hash[:12] or 'empty'}...). Is the trust service "
            f"pointed at a different Postgres than {configured or 'this one'}? Pass --dsn."
        )
    if len(matches) == 1:
        return matches[0]
    if configured in matches:
        return configured
    # Several databases hold an identical chain, which in practice means several hold an EMPTY
    # one: measured on a development cluster, 27 of 53 `proxyshop_w*` databases had a zero-length
    # ledger, so a fresh stack matched all 27 and picking `matches[0]` wrote the seller rows into
    # `proxyshop_w118` — a database nothing reads — while the events went over HTTP to the right
    # service. The run then failed its own verification with a message that never said "I
    # guessed". Refusing is the only honest answer: the caller knows which stack they brought up
    # and this function does not.
    raise SeedError(
        f"{len(matches)} databases on this cluster hold a chain identical to the one "
        f"{trust_url} is serving (length {length}), so the head does not say which one the "
        f"trust service is using — an empty ledger looks the same everywhere. Export "
        f"PROXYSHOP_WORKER to the value the stack was started with, or pass --dsn. Candidates: "
        + ", ".join(sorted(dsn.split("/")[-1] for dsn in matches)[:6])
        + ("..." if len(matches) > 6 else "")
    )


def write_sellers(dsn: str, stores: Sequence[str]) -> int:
    """Insert the demo sellers into ``app.sellers``. Returns how many rows were new.

    Nothing in the product writes this table — the merchant service registers an envelope, not
    a seller row — so a compose stack has none and every store is invisible to ``GET /snapshot``
    regardless of its ledger. ``ON CONFLICT DO NOTHING`` so a reseed is a no-op and so a store
    a human registered by hand is not overwritten.

    ``tier`` is ``hosted`` for the four storefronts that run a store agent in compose and
    ``external`` for every other seller, which is the distinction
    ``build_demo_deployment.HOSTED`` already draws and the only two values
    ``sellers_tier_check`` admits. It is NOT the transacting/crawled split: that one is
    expressed in evidence rather than in a column, which is the only place it can be checked.
    """
    import psycopg

    written = 0
    try:
        connection_cm = psycopg.connect(dsn, autocommit=False)
    except Exception as exc:  # noqa: BLE001 - reported as an outage, never as a traceback
        raise SeedError(
            f"cannot open {dsn.split('@')[-1]} as admin to register the demo sellers: {exc}"
        ) from exc
    with connection_cm as connection, connection.cursor() as cursor:
        for store_id in stores:
            cursor.execute(
                "insert into app.sellers (store_id, domain, business_identity, tier) "
                "values (%(store_id)s, %(domain)s, %(identity)s, %(tier)s) "
                "on conflict (store_id) do nothing",
                {
                    "store_id": store_id,
                    "domain": store_id,
                    "identity": store_id,
                    "tier": "hosted" if store_id in HOSTED else "external",
                },
            )
            written += cursor.rowcount or 0
        try:
            connection.commit()
        except Exception as exc:  # noqa: BLE001 - same reason as the connect above
            raise SeedError(f"could not write app.sellers: {exc}") from exc
    return written


def seeded_instant(trust_url: str, event_id: str) -> str | None:
    """The ``observed_at`` a previous run of this script already sealed, or ``None``.

    Read from the chain rather than assumed, and this is a repair rather than a nicety.
    ``event_id`` is the ledger's idempotency key and re-sending an id with DIFFERENT content is
    a 409 (``trust.events.store.IdempotencyConflict``, D16) — so a second run that stamped a
    fresh ``observed_at`` was not idempotent at all, it was a hard failure on the second event
    it tried. Measured by running ``make demo-trust`` twice::

        FATAL: POST /events answered 409 for 'sim-fb-seed-bulksupplements.com-…-00':
        event_id … is already in the chain with DIFFERENT content

    That is a refusal firing on the healthy path: re-running the documented step after a stack
    restart is the ordinary thing to do. Adopting the instant the chain already holds makes the
    whole run byte-identical to the first, which is what "idempotent" has to mean when the
    events are hashed.
    """
    status, body = _http(f"{trust_url.rstrip('/')}/events/{event_id}", timeout=30.0)
    if status != 200 or not isinstance(body, Mapping):
        return None
    event = body.get("event")
    if not isinstance(event, Mapping):
        return None
    payload = event.get("payload")
    stamped = payload.get("observed_at") if isinstance(payload, Mapping) else None
    return str(stamped or event.get("ts") or "") or None


def ranked_stores(trust_url: str) -> set[str]:
    """Every store the live snapshot already carries a row for.

    Read once, BEFORE anything is appended, because it is what :func:`post_events` decides a
    conflict on and a decision that changed halfway through a run would be worse than either
    answer. An unreadable snapshot returns the empty set, which makes every conflict fatal —
    the conservative direction, and the one that matches "I could not check".
    """
    status, body = _http(trust_url.rstrip("/") + "/snapshot", timeout=60.0)
    if status != 200 or not isinstance(body, Mapping):
        return set()
    return {str(store) for store, row in body.items() if isinstance(row, Mapping)}


def post_events(
    trust_url: str, events: Sequence[Mapping[str, Any]], *, already_ranked: Collection[str] = ()
) -> tuple[int, int, dict[str, str]]:
    """Append every event. Returns ``(inserted, already present, {store: conflicting id})``.

    A non-2xx is fatal and names the event: a half-seeded store is a store with a posture
    nobody chose, and it is much better to stop on the first one than to serve it.

    A 409 IS NOT ALWAYS FATAL, and which way it goes is the whole of this function
    -----------------------------------------------------------------------------
    A 409 means the chain already holds that ``event_id`` with DIFFERENT content
    (``trust.events.store.IdempotencyConflict``, D16). :func:`seeded_instant` removes the
    ordinary cause — a re-run stamping a fresh clock reading — so what is left is a stack seeded
    by an OLDER VERSION of this script: the ids are stable across versions, the bodies are not.
    An append-only ledger has no delete, so that store's posture cannot be rewritten to match.

    This used to abort the run on the first one, and **that refusal fired on the healthy path
    the moment the demo roster grew.** Measured on the running demo stack: its chain was seeded
    before ``_payload`` stopped writing ``price_honored: false`` onto a ``discount_honored``
    event, so ``sim-fb-seed-bulksupplements.com-discount_honored-09`` conflicts — and the run
    died there, having appended eleven events, with **nine newly promoted storefronts left with
    no ledger rows at all**. R12 excludes a store the trust snapshot holds no row for, so the
    refusal's cost was that every one of those nine was invisible on every shortlist. The
    conflicting store, meanwhile, was already in the snapshot with a posture that works.

    So the rule is about consequence, not about tidiness:

    * **the store is already in the live snapshot** — its old-version posture stands, this run
      skips the rest of that store's events, records it, and carries on. Nothing is lost that
      was not already lost, and every other store still gets seeded.
    * **the store is NOT in the snapshot** — fatal, with the message this function has always
      had. That store cannot be ranked at all, half a chain for it cannot be repaired, and
      continuing would serve a posture nobody chose.

    Skipping the REST of that store's events rather than only the conflicting one is deliberate:
    its remaining events are the same older-version disagreement, and appending the subset that
    happens to be byte-identical would leave a posture that is neither version.

    ``already_ranked`` is the store ids ``GET /snapshot`` returned BEFORE this run started. Read
    once by the caller rather than per conflict, so the decision cannot change halfway through.
    """
    url = trust_url.rstrip("/") + "/events"
    ranked = {str(store) for store in already_ranked}
    inserted = repeated = 0
    conflicted: dict[str, str] = {}
    for event in events:
        store_id = str(event["store_id"])
        if store_id in conflicted:
            continue  # this store is on its older seed; see the docstring
        status, body = _http(url, event, timeout=60.0)
        if status == 201:
            inserted += 1
        elif status == 200:
            repeated += 1
        elif status == 409 and store_id in ranked:
            conflicted[store_id] = str(event["event_id"])
        elif status == 409:
            raise SeedError(
                f"the chain already holds {event['event_id']!r} with DIFFERENT content, so this "
                f"stack was seeded by a version of this script that built that event another "
                f"way. An append-only ledger has no delete and D16 will not let an id be "
                f"rewritten, so there is nothing to repair here — and {store_id} is NOT in the "
                f"live snapshot, so it cannot be left on the seed it has either. Wind the stack "
                f"back with `make deps-down && make deps-up && make demo-up` and seed it afresh. "
                f"{inserted} event(s) were appended before this one and nothing after it was."
            )
        else:
            raise SeedError(
                f"POST /events answered {status} for {event['event_id']!r}: {body!r}. "
                f"{inserted} event(s) were appended before this one and nothing after it was."
            )
    return inserted, repeated, conflicted


def verify(trust_url: str) -> tuple[list[str], list[str]]:
    """Read ``GET /snapshot`` back. Returns ``(problems, drift)``.

    This is the step that makes a wrong database loud instead of silent. The service reads
    ``app.sellers`` out of ITS OWN database, so a seller row written to the wrong one simply
    does not appear here, and the message says so. Measured: a stack restarted under a
    different ``PROXYSHOP_WORKER`` moved the trust service to another database mid-session, and
    this is what said so rather than the demo quietly emptying.

    **What is a FAILURE and what is merely drift**, because getting this line wrong is how a
    check starts firing on healthy stacks:

    * FAILURES are structural and nothing but structural: a demo seller with no row in the
      snapshot, a blacklisted one, a ``low_data`` one, a missing dimension, an unreadable
      route. Each of those means the exchange cannot rank that store, which is what this seed
      exists to prevent.
    * EVERY numeric difference is drift — reported, never failed. Three honest causes, and the
      third is why an earlier version of this function was wrong: exponential decay against a
      30-day half life; earned evidence (``exchange.ranking.verification`` announces
      ``claim_verified`` on the served auction path, and ``trust.reconcile.engine`` grades
      three more dimensions); and the two together, since decay scales the SEEDED evidence and
      not a fresh observation, so an old seed is moved by very little new evidence.

      That version failed when a served mean crossed the neutral 0.5 away from its seeded
      side. It looked decay-proof and was not evidence-proof: measured against the real scorer,
      **two on-time deliveries** flip ``toniiq.com``'s ``shipped_on_time`` (seeded 0.4625) — and
      toniiq is the store whose whole demo story is that it ships late, so the most ordinary
      possible event would have turned ``make demo-trust`` permanently red, blaming the seed.
      A check that goes red because the platform was used is the fourth defect class this
      repository keeps rediscovering. What actually guards the seed's polarity is
      ``scripts/tests/test_seed_demo_trust.py``, which folds the seeded events through the real
      scorer offline where no earned evidence can reach them.

    ``feedback_match`` is outside even the drift report, for the reason the loop states.
    """
    status, body = _http(trust_url.rstrip("/") + "/snapshot", timeout=60.0)
    if status != 200 or not isinstance(body, Mapping):
        return [f"GET {trust_url}/snapshot answered {status}: {body!r}"], []

    problems: list[str] = []
    drift: list[str] = []
    for store_id, means in sorted(expected_posture().items()):
        row = body.get(store_id)
        if not isinstance(row, Mapping):
            problems.append(
                f"{store_id} has no row in the live snapshot. Either its app.sellers row was "
                f"written to a database this trust service does not read, or the seed did not run."
            )
            continue
        if row.get("blacklisted"):
            problems.append(f"{store_id} is blacklisted in the live snapshot")
        # `low_data` IS THE EXPECTED ANSWER FOR A CRAWL-ONLY SELLER, and asserting it the other
        # way round would be asserting a fiction. Those nine have been crawled and never
        # transacted with, so `clean_episodes` — the minimum positive-observation count across
        # the five dimensions the network obtains on its own initiative — is 0 by construction.
        # `trust.snapshot.builder` says the flag "marks a store the exchange should treat as
        # *unknown* rather than *average*", which is exactly right about them. It does not
        # exclude anybody; R12's exclusion is for a store with no row at all.
        #
        # It is NOT checked in the other direction either. A crawl-only seller that has stopped
        # being low_data has been transacted with, which is the platform working.
        if row.get("low_data") and store_id not in CATALOGUE_ACCURACY:
            problems.append(
                f"{store_id} is served low_data: fewer than {NEW_STORE_PRIOR_N} clean episodes, "
                f"which turns on the exchange's exploration floor"
            )
        dims = row.get("dims")
        if not isinstance(dims, Mapping):
            problems.append(f"{store_id} carries no dims")
            continue
        for dimension, expected in sorted(means.items()):
            if dimension == FEEDBACK_DIMENSION:
                # NEVER checked, and the reason is the whole point of the seed. This dimension
                # is the one `POST /buyer/feedback` moves, so a stack where anyone has pressed
                # the learning page's button legitimately serves something other than the
                # prior. Requiring 0.5 here made a re-run of `make demo-trust` FAIL on a stack
                # that had been demonstrated on -- a refusal firing on the healthy path,
                # measured the first time this script met a store with real feedback.
                continue
            served = dims.get(dimension)
            if not isinstance(served, Mapping):
                problems.append(f"{store_id}/{dimension} is missing from the live snapshot")
                continue
            alpha, beta = float(served.get("alpha", 0.0)), float(served.get("beta", 0.0))
            actual = alpha / (alpha + beta) if alpha + beta else 0.0
            if abs(actual - expected) > POSTURE_TOLERANCE:
                drift.append(
                    f"{store_id}/{dimension} serves mean {actual:.4f}, seeded for "
                    f"{expected:.4f} (Beta({alpha:.3f}, {beta:.3f}))"
                )
    return problems, drift


def untouched_feedback(trust_url: str) -> int:
    """How many demo sellers still carry no buyer feedback at all.

    This is the demo's headroom, measured rather than assumed: `feedback_match` at the bare
    prior means one press of the learning page's button owns the whole dimension. A store that
    has left the prior has been demonstrated on, which is not a fault.
    """
    status, body = _http(trust_url.rstrip("/") + "/snapshot", timeout=60.0)
    if status != 200 or not isinstance(body, Mapping):
        return 0
    untouched = 0
    for store_id in DEMO_SELLERS:
        row = body.get(store_id)
        dims = row.get("dims") if isinstance(row, Mapping) else None
        entry = dims.get(FEEDBACK_DIMENSION) if isinstance(dims, Mapping) else None
        if not isinstance(entry, Mapping):
            continue
        if (
            abs(float(entry.get("alpha", 0.0)) - PRIOR_ALPHA) < 1e-6
            and abs(float(entry.get("beta", 0.0)) - PRIOR_BETA) < 1e-6
        ):
            untouched += 1
    return untouched


def report(trust_url: str) -> None:
    """Print what the live service now says about each demo seller."""
    status, body = _http(trust_url.rstrip("/") + "/snapshot", timeout=60.0)
    if status != 200 or not isinstance(body, Mapping):
        return
    print()
    print(
        f"{'store':<28}{'posture':<12}{'score':>9}{'confidence':>12}"
        f"{'feedback_match':>17}  low_data"
    )
    for store_id in sorted(DEMO_SELLERS):
        posture = "crawled" if store_id in CATALOGUE_ACCURACY else "transacting"
        row = body.get(store_id)
        if not isinstance(row, Mapping):
            print(f"{store_id:<28}{posture:<12}{'ABSENT':>9}")
            continue
        dims = row.get("dims") or {}
        feedback = dims.get(FEEDBACK_DIMENSION) or {}
        alpha, beta = float(feedback.get("alpha", 0.0)), float(feedback.get("beta", 0.0))
        mean = alpha / (alpha + beta) if alpha + beta else float("nan")
        print(
            f"{store_id:<28}{posture:<12}{float(row.get('score', 0.0)):>9.4f}"
            f"{float(row.get('confidence', 0.0)):>12.4f}{mean:>17.4f}"
            f"  {bool(row.get('low_data'))}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed_demo_trust",
        description=(
            "Put the demo sellers into the live trust service so the exchange's ranking "
            "gate has something to read. EVERY observation it writes is SIMULATED and is "
            "marked as such inside the hash chain, permanently."
        ),
    )
    parser.add_argument("--trust-url", default=DEFAULT_TRUST_URL)
    parser.add_argument("--dsn", default=None, help="override the app.sellers database")
    parser.add_argument(
        "--observed-at",
        default=None,
        help=(
            "RFC-3339 instant every seeded observation is stamped with. Default: the instant "
            "a previous run already sealed, or the clock on a stack with no seed yet."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the live snapshot against this seed and write nothing",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    stores = sorted(DEMO_SELLERS)
    try:
        if not args.check:
            # Order matters: an instant this script already sealed wins over the clock, or the
            # second run of a hashed, idempotency-keyed seed is a 409 rather than a no-op.
            # An explicit `--observed-at` still wins over both, because that is a person
            # deliberately restating it.
            # ONE PROBE PER STORE, and this is the line that makes adding a seller safe.
            #
            # `seeded_instant` falls back to the clock when the id it asks about is absent, and
            # a fresh clock reading on a chain that already holds this seed is a 409 on the
            # first event it re-sends (`event_id` is the idempotency key and the same id with
            # different content is refused, D16). An append-only chain has no repair for that.
            #
            # This list used to be the first EIGHT events of the run, which is the first two
            # stores in `sorted(DEMO_SELLERS)` order. That is safe only while the roster never
            # changes: promoting `branchfurniture.com` puts a store alphabetically FIRST, all
            # eight probes then ask about events no previous run wrote, every one misses, the
            # clock wins, and the very next `POST /events` for `bulksupplements.com` -- already
            # in the chain, stamped with the old instant -- comes back 409 naming an id nobody
            # can rewrite. Measured by seeding the ten and then re-seeding the nineteen.
            #
            # Probing the first event of EVERY store cannot fail that way: any store the chain
            # already carries answers, and the answer is authoritative because every event of
            # one run carries the same stamp. The generator short-circuits on the first hit, so
            # the ordinary re-run costs one request, not nineteen. It also keeps the property
            # the eight probes were there for -- a run that died partway leaves SOME store's
            # first event on the chain, and this finds it wherever it is.
            first_of_each: dict[str, str] = {}
            for event in seed_events("", stores):
                first_of_each.setdefault(str(event["store_id"]), str(event["event_id"]))
            probes = list(first_of_each.values())
            sealed = next(
                (found for found in (seeded_instant(args.trust_url, p) for p in probes) if found),
                None,
            )
            observed_at = (
                args.observed_at or sealed or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            )
            dsn = args.dsn or trust_database(args.trust_url)
            shown = dsn.split("@")[-1]
            print(f"app.sellers -> {shown}")
            new_rows = write_sellers(dsn, stores)
            print(f"  {len(stores)} demo sellers, {new_rows} newly registered")

            events = list(seed_events(observed_at, stores))
            print(
                f"POST {args.trust_url}/events  ({len(events)} observations, observed_at {observed_at})"
            )
            inserted, repeated, conflicted = post_events(
                args.trust_url, events, already_ranked=ranked_stores(args.trust_url)
            )
            print(f"  {inserted} appended, {repeated} already in the chain")
            if conflicted:
                # Printed loudly and not fatal — `post_events` explains which way a 409 goes and
                # why. These stores keep the posture an older version of this script sealed;
                # `verify` below still has to find each of them rankable.
                print(
                    f"\n  {len(conflicted)} store(s) were seeded by an OLDER version of this "
                    f"script and keep the posture already in the chain (an append-only ledger "
                    f"cannot be rewritten). Everything else was seeded:"
                )
                for store_id, event_id in sorted(conflicted.items()):
                    print(f"    - {store_id}: first conflict at {event_id}")

        problems, drift = verify(args.trust_url)
    except SeedError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    report(args.trust_url)
    if drift:
        # Printed, never fatal. See `verify` for the two honest causes; a demo stack a week old
        # shows every one of these purely from decay.
        print(
            f"\n{len(drift)} dimension(s) have moved off the seeded value, by decay or by "
            f"earned evidence:"
        )
        for line in drift:
            print(f"  - {line}")
    if problems:
        print("\nFAIL: the live snapshot does not match this seed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    # The count is reported rather than asserted: a store whose `feedback_match` has left the
    # prior has been demonstrated on, which is the system working and not the seed failing.
    untouched = untouched_feedback(args.trust_url)
    print(
        f"\nOK: all {len(stores)} demo sellers are in the live trust snapshot; {untouched} of "
        f"them carry no buyer feedback at all, so a press of the learning page's button has "
        f"{FEEDBACK_DIMENSION} to itself."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
