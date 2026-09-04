"""Worker identity (D38). Orchestrator-owned (T-000), frozen."""

from __future__ import annotations

import os

ENV_VAR = "PROXYSHOP_WORKER"

#: Overrides :data:`REDIS_DB_COUNT` for a server known to be configured differently.
DB_COUNT_ENV_VAR = "PROXYSHOP_REDIS_DB_COUNT"

#: The logical-DB ceiling ASSUMED when nothing better is known. Redis's own default is 16,
#: so 16 is the safe assumption for any server nobody has asked.
#:
#: This is deliberately NOT kept in lockstep with ``--databases`` in docker-compose.yml.
#: A second copy of that number would drift the moment the compose file is edited, because
#: the running server only reads the flag at STARTUP — raising it in compose and restarting
#: nothing leaves the server at the old value while this constant claims the new one, which
#: is the worst of both. The number that cannot drift is the one the server itself reports,
#: and reading it needs a connection, so the authoritative check lives at client
#: construction in :func:`proxyshop_support.redis_client.worker_redis`, not here.
#:
#: Nothing in this module may open a Redis connection: worker identity is imported by
#: processes that never speak to Redis at all, and the offline tests build a client against
#: a dead address on purpose. A ceiling that required a live server would break both.
REDIS_DB_COUNT = 16


class WorkerNotConfiguredError(RuntimeError):
    """Raised when ``PROXYSHOP_WORKER`` is unset.

    Every ProxyShop process is per-worker isolated: the Postgres database name, the Redis
    logical DB index and the Redis key prefix all derive from this one number. Defaulting
    it would silently let two concurrent workers share state, so it is a hard error (D38).
    """


def worker_id(default: int | None = None) -> int:
    """Return the current worker index from ``$PROXYSHOP_WORKER``.

    Args:
        default: value to use when the variable is unset. ``None`` (the default) makes an
            unset variable a :class:`WorkerNotConfiguredError`.

    Raises:
        WorkerNotConfiguredError: the variable is unset and no ``default`` was given.
        ValueError: the variable is set to something that is not a positive integer.
    """
    raw = os.environ.get(ENV_VAR)
    if raw is None or raw.strip() == "":
        if default is not None:
            return default
        raise WorkerNotConfiguredError(
            f"{ENV_VAR} is unset. Every ProxyShop run is per-worker isolated (D38): the "
            f"Postgres database, the Redis logical DB index and the Redis key prefix all "
            f"derive from it. Export it before running anything, e.g. `{ENV_VAR}=1`."
        )
    try:
        value = int(raw)
    except ValueError as exc:  # noqa: TRY003 - message is the point
        raise ValueError(f"{ENV_VAR}={raw!r} is not an integer") from exc
    if value < 0:
        raise ValueError(f"{ENV_VAR}={raw!r} must be >= 0")
    return value


def key_prefix(worker: int | None = None) -> str:
    """Return the central Redis key prefix for a worker, e.g. ``"w1:"`` (D39)."""
    return f"w{worker_id() if worker is None else worker}:"


def redis_db_count() -> int:
    """Return the ASSUMED number of Redis logical DBs, without touching Redis.

    Resolution order: ``$PROXYSHOP_REDIS_DB_COUNT``, then :data:`REDIS_DB_COUNT` (16).

    This is the offline answer, and it is only ever a lower bound on what the server may
    really be configured with. It opens no connection — see :data:`REDIS_DB_COUNT` for why
    that is a hard constraint rather than a preference. The real ceiling is read from the
    running server by :func:`proxyshop_support.redis_client.worker_redis`, which refuses an
    index the server cannot isolate no matter what this function returned.

    Raises:
        ValueError: the override is set to something that is not an integer >= 1.
    """
    raw = os.environ.get(DB_COUNT_ENV_VAR)
    if raw is None or raw.strip() == "":
        return REDIS_DB_COUNT
    try:
        value = int(raw)
    except ValueError as exc:  # noqa: TRY003 - message is the point
        raise ValueError(f"{DB_COUNT_ENV_VAR}={raw!r} is not an integer") from exc
    if value < 1:
        raise ValueError(f"{DB_COUNT_ENV_VAR}={raw!r} must be >= 1")
    return value


def redis_db_index(
    worker: int | None = None,
    *,
    db_count: int | None = None,
    ceiling_source: str | None = None,
) -> int:
    """Return the per-worker Redis logical DB index (D39).

    REFUSES an index Redis cannot isolate, instead of silently sharing one.

    This was ``worker % REDIS_DB_COUNT``. The modulo does not isolate — it
    COLLIDES, silently. Worker 17 landed on logical DB 1, the same DB as worker
    1; 16, 32 and 64 all landed on DB 0 alongside worker 0. The ``w{N}:`` key
    prefix keeps the KEYS apart, which is exactly why this stayed invisible —
    but ``conftest.py`` calls ``flushdb()`` before *and* after every test, and
    FLUSHDB ignores prefixes. Two colliding workers wipe each other's Redis
    state at every test boundary.

    Not hypothetical. Measured on this cluster: 37 of 53 ``proxyshop_w*``
    databases carry an index >= 16 (16, 17, 18, 22, 24, 25, 29-37, 39, 41-43,
    52, 61-66, 71-73, 81, 118, 130, 170, 707, 733, 973, 7071). The harness
    assigns nothing — the index is whatever an orchestrator writes into a task
    packet — so arbitrary values are in real use.

    Refusing is D38's posture applied to the one store that cannot honour an
    arbitrary index: an unset ``PROXYSHOP_WORKER`` is already a hard error
    rather than a default, for exactly this reason. A run that cannot be
    isolated should stop, not quietly share. Postgres is unaffected — it gets a
    real database per index and has no such ceiling — so the ceiling is
    Redis-specific and lives here rather than in ``worker_id()``.

    Called with no ``db_count`` this checks against the ASSUMED ceiling
    (:func:`redis_db_count`), because this function must not open a connection. That is a
    cheap early refusal, not the authoritative one:
    :func:`proxyshop_support.redis_client.worker_redis` reads the number the running server
    actually reports and calls back in here with it.

    The refusal message always names the ceiling that was actually applied and where it
    came from. An assumed 16 and a measured 16 justify the same refusal but are not the
    same claim, and an operator who is told "the server has 16" when nobody asked a server
    will go and check the wrong thing.

    Args:
        worker: worker index. Defaults to ``$PROXYSHOP_WORKER``.
        db_count: ceiling to check against. Defaults to :func:`redis_db_count`. Pass the
            server's real answer to make this the authoritative check.
        ceiling_source: where ``db_count`` came from, in words, for the refusal message.
            Ignored unless ``db_count`` is given; defaults to a caller-neutral phrasing
            precisely because this function cannot know.
    """
    value = worker_id() if worker is None else worker
    if db_count is not None:
        ceiling = db_count
        source = ceiling_source or "supplied by the caller"
    elif (os.environ.get(DB_COUNT_ENV_VAR) or "").strip():
        ceiling = redis_db_count()
        source = f"from ${DB_COUNT_ENV_VAR}; no server was asked"
    else:
        ceiling = REDIS_DB_COUNT
        source = "assumed default; no server was asked"
    if value >= ceiling:
        collides_with = value % ceiling
        raise ValueError(
            f"{ENV_VAR}={value} cannot be isolated in Redis: the ceiling in effect is "
            f"{ceiling} logical DBs ({source}), so index {value} shares DB {collides_with} "
            f"with worker {collides_with}. The `w{value}:` prefix would keep the keys "
            f"apart, but the per-test `flushdb()` in conftest.py ignores prefixes and "
            f"would wipe that worker's state mid-run. "
            f"Use {ENV_VAR}=0..{ceiling - 1}, or raise `--databases` in docker-compose.yml "
            f"and restart the redis service — the server reads that flag only at startup."
        )
    return value
