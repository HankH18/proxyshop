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
    """Return the per-worker Redis logical DB index (D39)."""
    return (worker_id() if worker is None else worker) % REDIS_DB_COUNT
