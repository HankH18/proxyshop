"""The posterior book that survives a restart — D26's Redis-backed model, behind the seam.

WHAT WAS MEASURED, AND WHY IT MATTERED
--------------------------------------
The learning loop is closed at both ends. ``POST /auctions/{id}/accept``
(``accept/routes.py``) folds one win for the store the buyer chose and one loss for every
other store the same auction showed; ``policy/routes.py`` folds the same outcomes arriving
through ``POST /internal/outcomes``; ``policy/exploration.py`` reads the result and
``ranking/serving.py`` spends it on **one shortlist slot of four**
(:data:`~.exploration.EXPLORATION_SLOTS`), granted only to a store the trust snapshot
positively marks ``low_data``.

Nothing was persisted. ``policy/routes.py``'s own header said so — *"What is persisted, said
plainly: nothing"* — and :class:`~.routes.InMemoryBanditPosteriors` is a process-local
``dict`` built lazily on first use. Everything the market learned was lost on restart, was
not shared between replicas, and two uvicorn workers behind one load balancer kept two
divergent models. ``apps/buyer/app/learning/`` is a demo page whose entire purpose is showing
that loop move; an exchange restart wiped it mid-demonstration.

This module is the other implementation of the same port. It changes no contract:
:func:`~.routes.configure_outcomes` already accepts anything exposing
``record(store_id, cluster_id, converted, *, trust_snapshot=...)``, and
:func:`~.exploration._learned` already accepts anything exposing ``state()``.

WHAT IS STORED, AND IN WHAT SHAPE
---------------------------------
Two keys, both reached through :func:`proxyshop_support.redis_client.worker_redis` so D39's
``w{N}:`` prefix and per-worker logical DB apply centrally and nothing here builds a client
or spells a prefix:

``bandit:posteriors``
    One hash. Two fields per ``(cluster, store)`` pair — ``<pair>:a`` and ``<pair>:b`` —
    holding the Beta parameters as floats. ``<pair>`` is ``quote(cluster):quote(store)``,
    percent-encoded so an identifier carrying a ``:`` or a ``%`` cannot forge a second pair's
    field name. One ``HGETALL`` reads the whole book.

``bandit:pairs``
    A sorted set, member ``<pair>``, score the wall-clock of its last write. This is the
    eviction order and nothing else; see the bound below.

**The increments are atomic, and that is the point of the field layout.** A win is
``HINCRBYFLOAT bandit:posteriors <pair>:a 1``, executed by the server. Two replicas — or two
uvicorn workers, which is the case ``apps/exchange/compose.yaml`` actually ships — recording
concurrently both land. A read-modify-write of one JSON blob would have been half the code
and would have silently dropped one of the two outcomes, which is the divergence this module
exists to remove rather than relocate.

The seeding is atomic in the same way: a pair's prior is written with ``HSETNX`` from
:func:`~.bandit.initial_state` — the same function :class:`~.routes.InMemoryBanditPosteriors`
seeds through, so a trust-seeded prior means the same thing in both books — and ``HSETNX``
loses to whoever got there first instead of overwriting their learning.

THE BOUND, AND THE REASONING
----------------------------
A ``(cluster, store)`` pair is created **by a caller naming one**, and
``POST /internal/outcomes`` is unauthenticated (``policy/routes.py`` says so in its own
header). Unbounded, that is a memory leak with a public handle on it — and a worse one than
the in-memory book's, because this one survives the restart that used to reclaim it. Three
bounds, and each answers a different failure:

* :data:`DEFAULT_MAX_PAIRS` pairs, **evicted least-recently-written first** through the
  companion sorted set. Eviction rather than refusal, because refusal is the DoS: an attacker
  who fills the book once would otherwise lock real stores out of it until the TTL expired.
  Under eviction their junk is the coldest thing in the book and honest traffic pushes it
  straight back out. This is the same rule — "oldest touched first" — that
  :class:`~.routes.InMemoryBanditPosteriors` and ``ranking.serving.ShortlistStore`` evict by.
* :data:`DEFAULT_TTL_SECONDS`, refreshed on every write, so a deployment that stops recording
  outcomes stops holding them rather than holding them forever. Thirty days is long enough
  that "survives a restart" and "survives a redeploy" are both true, and short enough that a
  conversion rate nobody has confirmed in a month is not still steering exploration.
* :data:`MAX_DURABLE_IDENTIFIER_CHARS`, because a cap on the NUMBER of pairs is only a cap on
  bytes while the names are bounded too, and every served auction reads the whole book back.
  The door already holds one request to 128-character names; this holds the DATASTORE to 64,
  which measured as the difference between 80 ms and 20 ms of worst-case read on the auction
  path. A pair with longer names still learns, in the in-process fallback; it just does not
  persist.

REDIS BEING DOWN MAY NOT BREAK AN AUCTION
-----------------------------------------
This is the **opposite** choice from ``auction.state.build_auction_store``, which raises
rather than degrade, and the difference is what the two things cost. An exchange whose
auction store is down cannot hold an auction at all, so running the store the operator
rejected would be a silent lie on the money path. An exchange whose posterior book is down
can still run every auction it could run yesterday: the exploration slice is one slot of four,
granted only to a ``low_data`` store, and losing it costs exploration accuracy — never money,
never rank, never a wrong shortlist.

So every Redis failure here degrades to an in-process :class:`~.routes.InMemoryBanditPosteriors`
and says so at WARNING, naming the exception. Recovery is announced at INFO. **Nothing is
logged at all while Redis is healthy** — a book that warns on the honest path trains an
operator to filter the one line that matters, and
``test_a_healthy_redis_says_nothing_at_all`` is the assertion in that direction.

WHAT THIS DOES NOT TOUCH, WHICH IS D55
--------------------------------------
Rank. The five features the formula reads are named in ``contracts.ranking.RANK_FEATURES``
and nothing here is one of them; conversion moves the exploration slice and the exploration
slice only. A signal cannot buy rank here because there is no path from this book to
``rank_score`` — ``ranking/serving.py`` returns ``ranked["ranked"]`` exactly as ``rank()``
produced it and applies :func:`~.exploration.plan_exploration` to the shortlist afterwards.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote

from .bandit import DEFAULT_DRAWS, BanditState, Posterior, initial_state
from .routes import DEFAULT_EXPLORATION_FLOOR, InMemoryBanditPosteriors

__all__ = [
    "BANDIT_BOOK_MEMORY",
    "BANDIT_BOOK_REDIS",
    "BANDIT_BOOK_WORDS",
    "DEFAULT_MAX_PAIRS",
    "DEFAULT_TTL_SECONDS",
    "ENV_BANDIT_POSTERIORS",
    "MAX_DURABLE_IDENTIFIER_CHARS",
    "PAIRS_KEY",
    "POSTERIORS_KEY",
    "RedisBanditPosteriors",
    "bandit_posteriors_from_env",
]

_log = logging.getLogger(__name__)

#: The variable that selects where this exchange's posteriors live.
#:
#: A WORD, not a URL — ``redis`` resolves through
#: :func:`proxyshop_support.redis_client.worker_redis` (``REDIS_URL`` plus
#: ``PROXYSHOP_WORKER``), which is the one place D39 permits a client to be built. The twin of
#: :data:`~exchange.auction.state.ENV_AUCTION_STORE`, deliberately: the same operator sets both,
#: through the same compose fragment, and two spellings of "which datastore" is how a stack
#: ends up half-durable.
#:
#: Read from the ENVIRONMENT and from no deployment-document key, and that is the fix rather
#: than a shortcut. ``apps/exchange/compose.yaml``'s comment on ``EXCHANGE_SHOP_ROSTER`` records
#: the trap a document key would reopen — "⚠️ IT DOES NOTHING ON ITS OWN", because
#: ``ensure_configured`` returns early when no document is configured, so a document-only switch
#: is unreachable from the shipped container. This one binds on both branches.
ENV_BANDIT_POSTERIORS = "EXCHANGE_BANDIT_POSTERIORS"

#: The durable book: Redis, through ``worker_redis``.
BANDIT_BOOK_REDIS = "redis"

#: The process-local book — what every deployment ran before this module existed.
BANDIT_BOOK_MEMORY = "memory"

#: Every word :data:`ENV_BANDIT_POSTERIORS` accepts.
BANDIT_BOOK_WORDS = (BANDIT_BOOK_MEMORY, BANDIT_BOOK_REDIS)

#: The hash holding every ``(cluster, store)`` pair's Beta parameters.
#:
#: Un-prefixed on purpose: :class:`~proxyshop_support.redis_client.WorkerRedis` applies this
#: worker's ``w{N}:`` prefix in ``execute_command``, so a prefix spelled here would be applied
#: twice and would put this book somewhere no fixture and no sibling process can find it.
POSTERIORS_KEY = "bandit:posteriors"

#: The sorted set carrying each pair's last-write time. Eviction order, and nothing else.
PAIRS_KEY = "bandit:pairs"

#: How many ``(cluster, store)`` pairs the durable book holds before it evicts.
#:
#: 512 covers, for example, 32 stores across 16 clusters, or all ten storefronts in
#: ``deploy/demo/exchange-deployment.json`` across fifty clusters this repository does not have.
#:
#: **The number was set by measurement, not by taste, and the measurement is the WORST case
#: rather than the typical one** — a pair is created by a caller naming one on an
#: unauthenticated door, so the ceiling is the thing an attacker gets to choose. Every served
#: ``POST /auctions`` pays one ``HGETALL`` of the whole book (``exploration._learned``), so an
#: oversized cap is a permanent, restart-surviving latency amplifier with a public handle on it.
#: Measured on this repository at ``PROXYSHOP_WORKER=9`` against a filled book::
#:
#:     cap    identifiers      HGETALL payload   state() per call
#:     1024   128 chars each   1514 KiB          79.8 ms      <- rejected
#:      512    64 chars each    373 KiB          21.1 ms      <- shipped
#:      (the shipped demo: 10 pairs, 0.83 KiB, 0.30 ms)
#:
#: :data:`MAX_DURABLE_IDENTIFIER_CHARS` is the other half of that arithmetic and the two have to
#: move together — a cap on the number of pairs is only a cap on bytes while the identifiers are
#: bounded too.
DEFAULT_MAX_PAIRS = 512

#: The longest identifier this book will PERSIST, in characters.
#:
#: The door that admits an outcome already bounds both names at
#: :func:`~.routes.identifier_ceiling` — 128 characters, ``auction.routes.MAX_IDENTIFIER_LENGTH``
#: — and that is a bound on one request. This is the bound on the DATASTORE, which is a different
#: question because a durable structure keeps what it is given: 128-character names doubled the
#: worst-case read of a served auction from 20 ms to 80 ms in the measurement above, and they
#: survive the restart that used to reclaim them.
#:
#: 64 is roughly 2.5x the largest identifier the shipped stack can produce: measured on
#: ``deploy/demo/exchange-deployment.json``, the ten real storefronts key on hostnames of at most
#: 25 characters (``oregonswildharvest.com``) and the one named intent cluster is 21
#: (``cluster-liver-support``).
#:
#: A pair whose names are longer is **not refused** — it is folded into the in-process fallback,
#: so it still learns for the life of this process and simply does not persist. Refusing it
#: outright would be this module deciding an outcome did not happen, which is a bigger claim than
#: "we do not keep this one across restarts".
MAX_DURABLE_IDENTIFIER_CHARS = 64

#: How long the book survives with nothing written to it. Refreshed on every write.
DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60

#: How long a degraded book waits before trying Redis again.
#:
#: Not zero, because ``record`` is called once per store an auction showed — up to
#: ``ranking.shortlist.MAX_SLOTS`` times inside one ``POST /auctions/{id}/accept`` — and a
#: Redis that is hung rather than refusing costs ``worker_redis``'s 2.0s connect timeout on
#: every one of them. Retrying every ten seconds bounds that at one stall per ten seconds
#: while still picking the datastore back up within one page-refresh of it returning.
RETRY_AFTER_SECONDS = 10.0

#: The most often a still-degraded book repeats itself, in seconds.
DEGRADED_LOG_INTERVAL_SECONDS = 60.0

#: Field suffixes. ``alpha`` counts conversions, ``beta`` counts non-conversions.
_ALPHA = "a"
_BETA = "b"


def _pair_member(cluster_id: str, store_id: str) -> str:
    """``quote(cluster):quote(store)`` — the sorted-set member and the field-name stem.

    Percent-encoded rather than joined raw. Both halves are caller-chosen strings off an
    unauthenticated door, so an unencoded join lets ``store_id="x:y"`` in one cluster address
    the pair of a different cluster — a caller writing into a posterior it did not name.
    ``quote(..., safe="")`` escapes ``:`` and ``%`` themselves, so the split back is exact.
    """
    return f"{quote(cluster_id, safe='')}:{quote(store_id, safe='')}"


def _split_field(field: str) -> tuple[str, str, str] | None:
    """``<pair>:<a|b>`` back into ``(cluster, store, suffix)``. ``None`` if it is not one.

    Returns ``None`` rather than raising for anything that does not parse: this reads a
    datastore other processes and other versions of this file write into, and one unreadable
    field must cost that field and not the whole book.
    """
    parts = field.split(":")
    if len(parts) != 3 or parts[2] not in (_ALPHA, _BETA):
        return None
    try:
        return unquote(parts[0]), unquote(parts[1]), parts[2]
    except Exception:  # noqa: BLE001 - an undecodable field is simply not a pair
        return None


def _float(value: Any) -> float | None:
    """A finite float, or ``None``. Redis hands back ``str`` under ``decode_responses``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class RedisBanditPosteriors:
    """A durable Beta-Bernoulli book. Same seam as :class:`~.routes.InMemoryBanditPosteriors`.

    The two methods that make it that seam are :meth:`record` — what
    ``policy/routes.py::_fold_conversion`` calls, and therefore what the served accept reaches
    — and :meth:`state`, what ``policy/exploration.py::_learned`` reads on the served auction
    path. :attr:`exploration_floor` is read there too, by ``_floor_of``.

    Args:
        client: an already-built :class:`~proxyshop_support.redis_client.WorkerRedis`, or
            ``None`` to build one lazily on first use. Lazily by default because binding this
            book must not connect: ``ensure_configured`` runs on the request path, and a
            composition root that opened a socket would make a Redis outage a slow bind on
            every route rather than a degraded exploration slice.
        fallback: the book used while Redis is unreachable. Defaults to a fresh
            :class:`~.routes.InMemoryBanditPosteriors` with the same floor and no caps of its
            own beyond that class's.
        max_pairs: the eviction cap. See :data:`DEFAULT_MAX_PAIRS`.
        ttl_seconds: the sliding expiry on both keys.
        exploration_floor: seeded into every state this book builds, exactly as the in-memory
            book seeds it, and read off this object by ``exploration._floor_of``.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        fallback: Any | None = None,
        max_pairs: int = DEFAULT_MAX_PAIRS,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        exploration_floor: float = DEFAULT_EXPLORATION_FLOOR,
    ) -> None:
        floor = float(exploration_floor)
        # Clamped rather than validated, for the reason `InMemoryBanditPosteriors` clamps:
        # `initial_state` raises outside [0, 1] and a raise here would turn a deployment's
        # typo into a failed fold on every accept instead of a floor of 0 or 1.
        self.exploration_floor = 0.0 if not math.isfinite(floor) else min(1.0, max(0.0, floor))
        self.max_pairs = max(1, int(max_pairs))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._client = client
        self._fallback = (
            fallback
            if fallback is not None
            else InMemoryBanditPosteriors(exploration_floor=self.exploration_floor)
        )
        self._degraded = False
        self._blocked_until = 0.0
        self._last_complaint = 0.0
        #: When each rate-limited notice was last emitted. Two fixed keys, so it is bounded.
        self._said: dict[str, float] = {}

    # -- the seam ---------------------------------------------------------------------
    def record(
        self,
        store_id: str,
        cluster_id: str,
        converted: bool,
        *,
        trust_snapshot: Any = None,
    ) -> BanditState | None:
        """Fold one ``(cluster, store)`` Bernoulli outcome. Returns the state it produced.

        Never raises. Every failure — an unbuildable client, a refused connection, a reply
        this module cannot read — degrades to the in-process fallback and is logged once, so a
        learning write can never fail the ``POST /auctions/{id}/accept`` that produced it.
        """
        store = str(store_id)
        cluster = str(cluster_id)
        if len(store) > MAX_DURABLE_IDENTIFIER_CHARS or len(cluster) > MAX_DURABLE_IDENTIFIER_CHARS:
            # Folded, not dropped — see MAX_DURABLE_IDENTIFIER_CHARS. Logged at INFO rather than
            # WARNING because this is a bound working, not a fault: the alternative is letting an
            # anonymous caller choose how many bytes every future auction reads back.
            self._occasionally(
                "oversized",
                "exchange.policy: outcome for cluster/store names of %d/%d characters is not "
                "persisted (the durable book keeps names up to %d) — folded into the "
                "process-local book instead. Repeats within %.0fs are not logged",
                len(cluster),
                len(store),
                MAX_DURABLE_IDENTIFIER_CHARS,
                DEGRADED_LOG_INTERVAL_SECONDS,
            )
            return self._fallback.record(store, cluster, converted, trust_snapshot=trust_snapshot)
        client = self._reachable_client()
        if client is None:
            return self._fallback.record(store, cluster, converted, trust_snapshot=trust_snapshot)
        try:
            self._write(client, store, cluster, bool(converted), trust_snapshot)
            state = self._read(client)
        except Exception as exc:  # noqa: BLE001 - every Redis failure is the same decision
            self._degrade("record", exc)
            return self._fallback.record(store, cluster, converted, trust_snapshot=trust_snapshot)
        self._recovered()
        return state

    def state(self) -> BanditState | None:
        """The recorded posteriors, or ``None`` before the first outcome exists.

        **The R12 flags on the returned state are fail-closed rather than accurate, and that
        is a decision.** ``blacklisted`` is every store the book knows and ``low_data`` is
        empty, because this book stores conversion counts and holds no trust snapshot — there
        is no honest answer here and the two directions are not symmetric: a wrong "not
        blacklisted" admits a store R12 denies, a wrong "blacklisted" denies a store R12
        admits. Only the second is safe to be wrong about.

        It costs nothing on the served path, and the reason is worth stating rather than
        assuming. ``exploration.exposure_shares`` builds its own state with
        :func:`~.bandit.initial_state` over the auction's roster and the **live** trust
        snapshot, and carries only ``posteriors`` across from here — so the blacklist and the
        ``low_data`` marks a served shortlist is decided on come from trust, never from this
        object. ``test_the_fail_closed_flags_never_reach_a_served_shortlist`` is the assertion.
        """
        client = self._reachable_client()
        if client is None:
            return self._fallback.state()
        try:
            state = self._read(client)
        except Exception as exc:  # noqa: BLE001 - see `record`
            self._degrade("state", exc)
            return self._fallback.state()
        self._recovered()
        # A healthy but EMPTY book still defers to the fallback, so outcomes recorded during
        # an outage are not thrown away by the recovery that follows them.
        return state if state is not None else self._fallback.state()

    # -- Redis ------------------------------------------------------------------------
    def _write(
        self, client: Any, store: str, cluster: str, converted: bool, trust_snapshot: Any
    ) -> None:
        """One outcome into Redis. Raises whatever redis-py raises; :meth:`record` catches."""
        member = _pair_member(cluster, store)
        alpha_field = f"{member}:{_ALPHA}"
        beta_field = f"{member}:{_BETA}"

        # The touch comes FIRST so the pair this call names is the newest thing in the set and
        # can never be the pair the eviction below removes.
        client.zadd(PAIRS_KEY, {member: time.time()})
        overflow = int(client.zcard(PAIRS_KEY) or 0) - self.max_pairs
        if overflow > 0:
            evicted = client.zpopmin(PAIRS_KEY, overflow) or ()
            for entry in evicted:
                stale = entry[0] if isinstance(entry, (tuple, list)) else entry
                client.hdel(POSTERIORS_KEY, f"{stale}:{_ALPHA}", f"{stale}:{_BETA}")
            self._occasionally(
                "evicted",
                "exchange.policy: durable posterior book at its %d-pair cap — evicted %d "
                "least-recently-written pair(s) to make room for cluster=%s store=%s. "
                "Repeats within %.0fs are not logged",
                self.max_pairs,
                overflow,
                cluster,
                store,
                DEGRADED_LOG_INTERVAL_SECONDS,
            )

        prior = self._prior(store, cluster, trust_snapshot)
        # HSETNX, so a replica that seeded this pair a millisecond ago keeps its own prior and
        # its own learning. A plain HSET here would be a race that silently erases outcomes.
        client.hsetnx(POSTERIORS_KEY, alpha_field, repr(prior.alpha))
        client.hsetnx(POSTERIORS_KEY, beta_field, repr(prior.beta))
        client.hincrbyfloat(POSTERIORS_KEY, alpha_field if converted else beta_field, 1.0)
        client.expire(POSTERIORS_KEY, self.ttl_seconds)
        client.expire(PAIRS_KEY, self.ttl_seconds)

    def _read(self, client: Any) -> BanditState | None:
        """One ``HGETALL`` into a :class:`~.bandit.BanditState`, or ``None`` when empty."""
        raw = client.hgetall(POSTERIORS_KEY) or {}
        if not raw:
            return None

        parameters: dict[tuple[str, str], dict[str, float]] = {}
        for field, value in raw.items():
            name = field.decode() if isinstance(field, bytes) else str(field)
            parsed = _split_field(name)
            number = _float(value)
            if parsed is None or number is None:
                continue
            cluster, store, suffix = parsed
            parameters.setdefault((cluster, store), {})[suffix] = number
        if not parameters:
            return None

        clusters = sorted({cluster for cluster, _ in parameters})
        stores = sorted({store for _, store in parameters})
        # Every cluster carries every store, because `BanditState` is a cross product and
        # `bandit.exposure` indexes it as one — a sparse map would be a KeyError waiting for
        # the first caller that reads this state the way the model's own sampler does. The
        # filler is `Posterior(1.0, 1.0)`, which is not invented: it is exactly what
        # `bandit.initial_state` seeds a pair with when the snapshot says nothing about the
        # store (score 0.5, confidence 0.0), i.e. the "we know nothing" prior.
        posteriors = {
            cluster: {
                store: Posterior(
                    parameters.get((cluster, store), {}).get(_ALPHA, 1.0),
                    parameters.get((cluster, store), {}).get(_BETA, 1.0),
                )
                for store in stores
            }
            for cluster in clusters
        }
        return BanditState(
            stores=tuple(stores),
            clusters=tuple(clusters),
            posteriors=posteriors,
            # Fail-closed. See `state`'s docstring for why this is the safe direction to be
            # wrong in, and for the test that pins the one consumer to the live snapshot.
            blacklisted=frozenset(stores),
            low_data=frozenset(),
            exploration_floor=self.exploration_floor,
            draws=DEFAULT_DRAWS,
        )

    def _prior(self, store: str, cluster: str, trust_snapshot: Any) -> Posterior:
        """This pair's trust-seeded starting posterior, through the model's own seeder.

        :func:`~.bandit.initial_state` is called rather than reimplemented so a prior means
        the same thing in both books; ``routes.InMemoryBanditPosteriors.record`` seeds through
        the same function. A snapshot that is not a mapping is no snapshot — the same ruling
        that class makes, and the same one ``ranking.serving.trust_snapshot_of`` defaults to.
        """
        snapshot: Mapping[str, Any] = trust_snapshot if isinstance(trust_snapshot, Mapping) else {}
        try:
            seeded = initial_state(
                [store], [cluster], snapshot, {"exploration_floor": self.exploration_floor}
            )
        except (TypeError, ValueError):
            return Posterior(1.0, 1.0)
        return seeded.posteriors[cluster][store]

    # -- talking to the operator without becoming the operator's problem ---------------
    def _occasionally(self, key: str, message: str, *args: Any) -> None:
        """Log ``message`` at INFO at most once per :data:`DEGRADED_LOG_INTERVAL_SECONDS`.

        Both callers sit on a path an anonymous caller can drive as fast as it likes — the
        eviction notice fires once the book is full, which is a state a flood REACHES AND STAYS
        IN, and the oversized-name notice fires on every outcome carrying one. Unthrottled,
        either is a log line per unauthenticated request: a disk-fill vector wearing the clothes
        of an operator notice, and the thing that would make an operator stop reading the log
        this module's real warning goes to.
        """
        now = time.time()
        if now - self._said.get(key, 0.0) < DEGRADED_LOG_INTERVAL_SECONDS:
            return
        self._said[key] = now
        _log.info(message, *args)

    # -- degradation ------------------------------------------------------------------
    def _reachable_client(self) -> Any | None:
        """The client, or ``None`` while this book is inside its retry cooldown."""
        if self._client is not None:
            if self._degraded and time.time() < self._blocked_until:
                return None
            return self._client
        if time.time() < self._blocked_until:
            return None
        try:
            # Deferred: `proxyshop_support.redis_client` imports `redis`, and `.importlinter`
            # forbids this package importing it. `worker_redis` is the ONE permitted builder
            # (D39) — it selects this worker's logical DB and applies the `w{N}:` prefix.
            from proxyshop_support.redis_client import worker_redis  # noqa: PLC0415

            self._client = worker_redis()
        except Exception as exc:  # noqa: BLE001 - an unbuildable client is an outage
            self._degrade("client", exc)
            return None
        return self._client

    def _degrade(self, where: str, exc: BaseException) -> None:
        """Announce the outage — once on the way in, then at most once a minute."""
        now = time.time()
        self._blocked_until = now + RETRY_AFTER_SECONDS
        first = not self._degraded
        self._degraded = True
        if first or now - self._last_complaint >= DEGRADED_LOG_INTERVAL_SECONDS:
            self._last_complaint = now
            _log.warning(
                "exchange.policy: the durable posterior book is UNAVAILABLE (%s during %s) — "
                "conversion outcomes are being folded into a process-local in-memory book "
                "instead, so learning will be lost on restart and will not be shared between "
                "replicas. Auctions are unaffected: the exploration slice is one shortlist "
                "slot of four and never touches rank. Check REDIS_URL and PROXYSHOP_WORKER, "
                "and that Redis is reachable from this container.",
                f"{type(exc).__name__}: {exc}",
                where,
            )

    def _recovered(self) -> None:
        """Announce recovery, once. Silent — completely — while Redis has always been up."""
        if not self._degraded:
            return
        self._degraded = False
        self._blocked_until = 0.0
        _log.info(
            "exchange.policy: the durable posterior book is reachable again; outcomes are "
            "being written to Redis. Anything recorded during the outage stayed in the "
            "in-memory fallback and is still read from it while Redis holds nothing."
        )


def bandit_posteriors_from_env(env: Mapping[str, str] | None = None) -> Any | None:
    """The posterior book :data:`ENV_BANDIT_POSTERIORS` names, or ``None`` when it names none.

    ``None`` and not "the memory book", so a caller can tell "this environment said nothing"
    apart from "this environment said ``memory``". Mirrors
    :func:`~exchange.auction.state.auction_store_from_env`.

    **An unrecognised word is ``None``, not an exception**, and that is the one place this
    departs from the auction store's shape. ``build_auction_store`` raises on a word it does not
    know because a deployment that misspells its auction store must not silently run a
    process-local one on the money path. Here the fallback IS the old behaviour and costs one
    exploration slot, so a typo logs a WARNING naming this variable and the exchange keeps
    serving — the same ruling ``EXCHANGE_BID_WINDOW_SECONDS`` makes about a malformed value.

    Building the ``redis`` book connects to nothing: :class:`RedisBanditPosteriors` opens its
    client on the first outcome, so an exchange pointed at a Redis that is down still binds,
    still auctions, and degrades exactly as documented in this module's header.
    """
    import os  # noqa: PLC0415 — read at call time so a test can drive `env`

    source: Mapping[str, str] = os.environ if env is None else env
    word = str(source.get(ENV_BANDIT_POSTERIORS, "") or "").strip().lower()
    if not word:
        return None
    if word == BANDIT_BOOK_MEMORY:
        return InMemoryBanditPosteriors()
    if word != BANDIT_BOOK_REDIS:
        _log.warning(
            "exchange.policy: %s=%r is not one of %s — keeping the process-local posterior "
            "book, so everything this exchange learns will be lost on restart and will not be "
            "shared between replicas",
            ENV_BANDIT_POSTERIORS,
            word,
            list(BANDIT_BOOK_WORDS),
        )
        return None
    return RedisBanditPosteriors()
