"""The central Redis client wrapper (D39). Orchestrator-owned (T-000), frozen.

D39: *Redis isolation = per-worker logical DB index **plus** a ``w{N}:`` key prefix applied
centrally in T-000's Redis client wrapper. ``FLUSHDB`` is the permitted reset.*

Use :func:`worker_redis` everywhere instead of constructing ``redis.Redis`` directly. It

* selects logical DB ``N`` for worker ``N`` (``$PROXYSHOP_WORKER``),
* rewrites every key argument to ``w{N}:<key>`` on the way out, transparently, and
* refuses, at construction, an ``N`` the running server cannot isolate — the ceiling is
  read from the server itself (``CONFIG GET databases``) rather than from a constant, so it
  cannot drift out of step with ``--databases`` in docker-compose.yml. An unreadable config
  is UNKNOWN and warns; it is never treated as a pass.

The prefix is applied in :meth:`WorkerRedis.execute_command`, so *all* redis-py commands
go through it, including ones added after this file was frozen. Which arguments of a
command are keys is described by :data:`_KEY_SPEC`; the default for an unlisted command is
"argument 1 is a key", which is correct for the overwhelming majority of Redis commands.
Commands with no keys at all are listed in :data:`_KEYLESS`. If you need to bypass all of
this, use :attr:`WorkerRedis.raw`.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Iterable
from typing import Any, cast

import redis

from proxyshop_support.worker import (
    DB_COUNT_ENV_VAR,
    ENV_VAR,
    key_prefix,
    redis_db_count,
    redis_db_index,
    worker_id,
)

#: The repo-wide banned reset command (D39). Assembled from two halves so this file does
#: not itself trip the repo-wide lint gate that greps for the literal token. Do NOT collapse
#: it into one string literal: `ruff format` folds implicit concatenation, `+` it leaves alone.
_BANNED_RESET = "FLUSH" + "ALL"

#: Commands that take no key arguments at all.
_KEYLESS = frozenset(
    {
        "AUTH",
        "BGREWRITEAOF",
        "BGSAVE",
        "CLIENT",
        "CLUSTER",
        "COMMAND",
        "CONFIG",
        "DBSIZE",
        "DEBUG",
        "DISCARD",
        "ECHO",
        "EXEC",
        "FLUSHDB",
        "HELLO",
        "INFO",
        "LASTSAVE",
        "LOLWUT",
        "MEMORY",
        "MONITOR",
        "MULTI",
        "PING",
        "PSUBSCRIBE",
        "PUNSUBSCRIBE",
        "QUIT",
        "RANDOMKEY",
        "REPLICAOF",
        "RESET",
        "ROLE",
        "SAVE",
        "SCAN",
        "SELECT",
        "SHUTDOWN",
        "SLAVEOF",
        "SLOWLOG",
        "SWAPDB",
        "TIME",
        "UNWATCH",
        "WAIT",
    }
)

#: command -> key argument positions (1-based, relative to the command name), or the
#: sentinel ``"ALL"`` meaning "every remaining argument is a key".
_KEY_SPEC: dict[str, tuple[int, ...] | str] = {
    "BITOP": "TAIL2",
    "BLPOP": "ALLBUTLAST",
    "BRPOP": "ALLBUTLAST",
    "COPY": (1, 2),
    "DEL": "ALL",
    "EXISTS": "ALL",
    "MGET": "ALL",
    "PFCOUNT": "ALL",
    "PFMERGE": "ALL",
    "RENAME": (1, 2),
    "RENAMENX": (1, 2),
    "SDIFF": "ALL",
    "SDIFFSTORE": "ALL",
    "SINTER": "ALL",
    "SINTERSTORE": "ALL",
    "SMOVE": (1, 2),
    "SUNION": "ALL",
    "SUNIONSTORE": "ALL",
    "TOUCH": "ALL",
    "UNLINK": "ALL",
    "WATCH": "ALL",
    # PUB/SUB channels are namespaced exactly like keys so two workers cannot cross-talk.
    "PUBLISH": (1,),
    "SUBSCRIBE": "ALL",
    "UNSUBSCRIBE": "ALL",
}


def _positions(command: str, argc: int) -> tuple[int, ...]:
    spec = _KEY_SPEC.get(command)
    if spec is None:
        return (1,) if argc >= 1 else ()
    if spec == "ALL":
        return tuple(range(1, argc + 1))
    if spec == "ALLBUTLAST":  # BLPOP key [key ...] timeout
        return tuple(range(1, argc))
    if spec == "TAIL2":  # BITOP op destkey key [key ...]
        return tuple(range(2, argc + 1))
    assert isinstance(spec, tuple)
    return tuple(p for p in spec if p <= argc)


class RedisDbCeilingUnknown(RuntimeWarning):
    """The live logical-DB ceiling could not be read, so isolation is UNCONFIRMED.

    Emitted by :func:`worker_redis` when ``CONFIG GET databases`` cannot be answered — the
    server is unreachable, the command is disabled or ACL-denied, the client is a double,
    or the reply is not a number. It is a warning and not an error on purpose: the offline
    path is legitimate (identity resolves without Redis, and the tests build clients against
    a dead address deliberately). What is NOT legitimate is an unreadable ceiling passing
    silently, because "we could not check" and "we checked and it is fine" are the two
    states this whole mechanism exists to keep apart.
    """


class WorkerRedis(redis.Redis):
    """A ``redis.Redis`` that transparently namespaces every key with ``w{N}:``.

    Never construct this directly in feature code — call :func:`worker_redis`.
    """

    _prefix: str
    #: Logical DBs the server reported at construction; ``None`` means UNKNOWN, never "ok".
    _db_ceiling: int | None = None

    def key(self, name: str | bytes) -> str:
        """Return the fully namespaced form of ``name`` (useful for assertions)."""
        text = name.decode() if isinstance(name, bytes) else str(name)
        return text if text.startswith(self._prefix) else f"{self._prefix}{text}"

    def unkey(self, name: str | bytes) -> str:
        """Strip the ``w{N}:`` prefix from a key returned by Redis."""
        text = name.decode() if isinstance(name, bytes) else str(name)
        return text[len(self._prefix) :] if text.startswith(self._prefix) else text

    @property
    def prefix(self) -> str:
        """The ``w{N}:`` prefix this client applies."""
        return self._prefix

    @property
    def db_ceiling(self) -> int | None:
        """Logical DBs the server reported when this client was built.

        ``None`` means the config read did not succeed, i.e. UNKNOWN — read it as "this
        client's isolation was never confirmed", never as "there is no limit".
        """
        return self._db_ceiling

    @property
    def raw(self) -> redis.Redis:
        """An un-prefixed client on the same connection pool. Use sparingly."""
        return redis.Redis(connection_pool=self.connection_pool)

    def execute_command(self, *args: Any, **options: Any) -> Any:
        if not args:
            return super().execute_command(*args, **options)
        command = str(args[0]).upper()
        if command == _BANNED_RESET:
            raise RuntimeError(
                f"{_BANNED_RESET} is banned repo-wide (D39): it would wipe every other "
                f"worker's logical database. Use FLUSHDB, which is scoped to this "
                f"worker's own DB."
            )
        if command in _KEYLESS:
            return super().execute_command(*args, **options)
        rewritten = list(args)
        for pos in _positions(command, len(args) - 1):
            rewritten[pos] = self.key(rewritten[pos])
        return super().execute_command(*rewritten, **options)


def _server_db_count(url: str) -> int | None:
    """Ask the SERVER how many logical DBs it has. ``None`` means UNKNOWN, not "fine".

    ``CONFIG GET databases`` is the one answer that cannot drift from reality: it is what
    the running process is using, not what a compose file or a Python constant says it
    should be using. Those three disagree routinely — ``--databases`` is read only at
    container startup, so an edited docker-compose.yml means nothing until a restart.

    Every failure mode collapses to ``None`` on purpose, because they are indistinguishable
    from here and equally uninformative: server down, ``CONFIG`` renamed or ACL-denied
    (common in managed Redis), a test double that returns something else, a reply that is
    not an integer. The caller must treat ``None`` as "not checked", never as "checked, ok".

    Uses its OWN short-lived client pinned to logical DB 0 rather than the caller's. The
    caller's pool is about to be pinned to a possibly out-of-range index, and a connection
    made on it would fail its own ``SELECT`` before it could ask anything — and worse, a
    probe run on the caller's pool before the index is written would leave a pooled
    connection sitting on the wrong database for every later command to reuse.
    """
    try:
        probe = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=5.0,
        )
        # `from_url` lets the URL's database component win over any `db=` keyword (see
        # worker_redis's implementation note), so pin DB 0 on the pool the same way.
        probe.connection_pool.connection_kwargs["db"] = 0
    except Exception:  # noqa: BLE001 - an unbuildable probe is simply UNKNOWN
        return None
    try:
        reply = probe.config_get("databases")
    except Exception:  # noqa: BLE001 - unreachable, disabled, ACL-denied, doubled: UNKNOWN
        return None
    finally:
        try:
            probe.close()
            probe.connection_pool.disconnect()
        except Exception:  # noqa: BLE001,S110 - best-effort teardown of a throwaway probe
            pass
    if not isinstance(reply, dict):
        return None
    raw = reply.get("databases")
    if raw is None:
        return None
    try:
        count = int(raw)
    except (TypeError, ValueError):  # a double may hand back anything at all
        return None
    return count if count > 0 else None


def _checked_db_index(url: str, index: int) -> tuple[int, int | None]:
    """Validate ``index`` against the LIVE ceiling. Returns ``(index, ceiling_or_None)``.

    **The server's answer is the ceiling — in both directions.** When it answers, the
    assumed ceiling is not consulted at all: it exists only to cover the case where nothing
    could be asked. That matters as much for permitting as for refusing. Raising
    ``--databases`` to 64 and restarting buys nothing if a hardcoded 16 still refuses worker
    20 — the number that decides has to be the server's, or the ceiling is not really
    discovered, just double-checked.

    The assumed ceiling (:func:`~proxyshop_support.worker.redis_db_count`) applies **only**
    on the UNKNOWN path, and there it applies alongside the warning, never instead of it: a
    server that cannot say how many databases it has still gets the conservative refusal,
    and the operator still gets told the number was never confirmed.
    """
    count = _server_db_count(url)
    if count is None:
        warnings.warn(
            f"Redis at {url} did not answer `CONFIG GET databases`, so the logical-DB "
            f"ceiling is UNKNOWN and {ENV_VAR}={index} was NOT confirmed to be isolated. "
            f"Falling back to the assumed ceiling of {redis_db_count()} (override with "
            f"${DB_COUNT_ENV_VAR}). If this server really has {index} or fewer logical DBs, "
            f"this client is sharing DB {index} with another worker and their FLUSHDBs will "
            f"wipe each other mid-run. Confirm with `redis-cli CONFIG GET databases`.",
            RedisDbCeilingUnknown,
            stacklevel=3,
        )
        return redis_db_index(index), None
    return (
        redis_db_index(
            index,
            db_count=count,
            ceiling_source=f"reported by the server at {url} via CONFIG GET databases",
        ),
        count,
    )


def worker_redis(url: str | None = None, *, worker: int | None = None) -> WorkerRedis:
    """Build the per-worker Redis client (D39).

    Args:
        url: Redis URL. Defaults to ``$REDIS_URL``, then ``redis://localhost:6379``.
            Any database component in the URL is overridden by the per-worker index —
            see the implementation note below; this is enforced, not assumed.
        worker: worker index. Defaults to ``$PROXYSHOP_WORKER``.

    Returns:
        A :class:`WorkerRedis` bound to logical DB ``worker`` with the ``w{worker}:``
        key prefix already applied. ``decode_responses`` is on, so commands return ``str``.

    Raises:
        WorkerNotConfiguredError: ``$PROXYSHOP_WORKER`` is unset and no ``worker`` was
            given (D38) — checked before anything touches the network.
        ValueError: the worker's index is >= the number of logical databases the server
            reports for ``CONFIG GET databases``, or >= the assumed ceiling
            (:func:`~proxyshop_support.worker.redis_db_count`) when the server would not
            say. Refusing is the point: the alternative is two workers on one logical DB,
            where either one's ``FLUSHDB`` silently erases the other mid-run.

    Warns:
        RedisDbCeilingUnknown: the server would not report its database count, so the
            ceiling could not be confirmed. Not an error — the offline path is legitimate —
            but never silent, because "could not check" is not "checked and fine".

    Implementation note — why the ``db`` is written *after* ``from_url``:
        redis-py's ``ConnectionPool.from_url`` parses the URL into ``url_options`` and then
        does ``kwargs.update(url_options)``, so **the URL wins over an explicit ``db=``
        keyword**. Passing ``db=redis_db_index(worker)`` to ``from_url`` therefore does
        nothing at all whenever ``$REDIS_URL`` carries a database component (and the
        shipped one used to: ``redis://localhost:6379/1``), silently landing every worker
        on the same logical DB — where ``redis_client``'s ``FLUSHDB`` wipes its siblings.
        The pool's ``connection_kwargs`` are the single place a connection reads ``db``
        from, and no connection has been created yet at this point, so overwriting the key
        here is both authoritative and safe.
    """
    url = url or os.environ.get("REDIS_URL") or "redis://localhost:6379"
    # Identity first: an unset PROXYSHOP_WORKER is a hard error (D38) and should not cost a
    # network round trip to discover. Then the ceiling, from the server itself.
    index, ceiling = _checked_db_index(url, worker_id() if worker is None else worker)
    # redis-py types `from_url` as returning the base `Redis`, but it is a classmethod and
    # really returns an instance of `cls`.
    client = cast(
        WorkerRedis,
        WorkerRedis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=5.0,
        ),
    )
    client.connection_pool.connection_kwargs["db"] = index
    client._prefix = key_prefix(worker)
    client._db_ceiling = ceiling
    return client


def reset_worker_db(client: WorkerRedis) -> None:
    """The permitted per-worker reset (D39): ``FLUSHDB`` on this worker's own DB only."""
    client.flushdb()


def namespaced(names: Iterable[str], *, worker: int | None = None) -> list[str]:
    """Namespace a batch of key names without a live client."""
    prefix = key_prefix(worker)
    return [n if n.startswith(prefix) else f"{prefix}{n}" for n in names]
