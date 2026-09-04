"""Worker identity (D38). Orchestrator-owned (T-000), frozen."""

from __future__ import annotations

import os

ENV_VAR = "PROXYSHOP_WORKER"

#: Redis ships with 16 logical databases by default and the compose stack pins
#: ``--databases 16``; worker N therefore owns logical DB N.
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


def redis_db_index(worker: int | None = None) -> int:
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
    """
    value = worker_id() if worker is None else worker
    if value >= REDIS_DB_COUNT:
        collides_with = value % REDIS_DB_COUNT
        raise ValueError(
            f"{ENV_VAR}={value} cannot be isolated in Redis: this server has only "
            f"{REDIS_DB_COUNT} logical DBs, so index {value} shares DB {collides_with} "
            f"with worker {collides_with}. The `w{value}:` prefix would keep the keys "
            f"apart, but the per-test `flushdb()` in conftest.py ignores prefixes and "
            f"would wipe that worker's state mid-run. "
            f"Use {ENV_VAR}=0..{REDIS_DB_COUNT - 1}."
        )
    return value
