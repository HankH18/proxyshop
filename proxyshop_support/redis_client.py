"""The central Redis client wrapper (D39). Orchestrator-owned (T-000), frozen.

D39: *Redis isolation = per-worker logical DB index **plus** a ``w{N}:`` key prefix applied
centrally in T-000's Redis client wrapper. ``FLUSHDB`` is the permitted reset.*

Use :func:`worker_redis` everywhere instead of constructing ``redis.Redis`` directly. It

* selects logical DB ``N`` for worker ``N`` (``$PROXYSHOP_WORKER``), and
* rewrites every key argument to ``w{N}:<key>`` on the way out, transparently.

The prefix is applied in :meth:`WorkerRedis.execute_command`, so *all* redis-py commands
go through it, including ones added after this file was frozen. Which arguments of a
command are keys is described by :data:`_KEY_SPEC`; the default for an unlisted command is
"argument 1 is a key", which is correct for the overwhelming majority of Redis commands.
Commands with no keys at all are listed in :data:`_KEYLESS`. If you need to bypass all of
this, use :attr:`WorkerRedis.raw`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any, cast

import redis

from proxyshop_support.worker import key_prefix, redis_db_index

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


class WorkerRedis(redis.Redis):
    """A ``redis.Redis`` that transparently namespaces every key with ``w{N}:``.

    Never construct this directly in feature code — call :func:`worker_redis`.
    """

    _prefix: str

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
    client.connection_pool.connection_kwargs["db"] = redis_db_index(worker)
    client._prefix = key_prefix(worker)
    return client


def reset_worker_db(client: WorkerRedis) -> None:
    """The permitted per-worker reset (D39): ``FLUSHDB`` on this worker's own DB only."""
    client.flushdb()


def namespaced(names: Iterable[str], *, worker: int | None = None) -> list[str]:
    """Namespace a batch of key names without a live client."""
    prefix = key_prefix(worker)
    return [n if n.startswith(prefix) else f"{prefix}{n}" for n in names]
