"""Behavioural tests for the shared runtime helpers. Orchestrator-owned (T-000), frozen.

Why this file exists
--------------------
An adversarial pass over the scaffold sabotaged twenty-four things and found that nine
survived a green ``make verify``. Every survivor was in this layer, and all for one reason:
``hash_embed``, ``ManualClock``, ``LLMDouble``, ``WorkerRedis``'s prefixing and the ASGI
server were **exercised by nothing**. Concretely, all of these were green:

* ``hash_embed`` returning un-normalised vectors (only the *dimension* was asserted);
* ``ManualClock.advance()`` running the clock **backwards**;
* ``LLMDouble.queue()`` discarding every queued response;
* ``WorkerRedis.key()`` returning the key unprefixed — worker isolation removed entirely;
* ``key_prefix()`` hard-coded to ``"w1:"``, which the one existing assertion could not see
  because it ran at worker 1.

Roughly forty tickets stand on these helpers. Nothing here needs Docker: these are the
properties that must hold with no stack at all, so ``make check`` — the per-ticket gate,
which deselects ``docker`` — runs every one of them.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import textwrap
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from proxyshop_support import neo4j_lock
from proxyshop_support.asgi_server import serve
from proxyshop_support.clock import EPOCH, ManualClock
from proxyshop_support.embedding import EMBEDDING_DIM, cosine, hash_embed
from proxyshop_support.llm_double import LLMDouble
from proxyshop_support.neo4j_lock import Neo4jLockTimeout, held_depth, neo4j_flock
from proxyshop_support.postgres import (
    ROLE_PASSWORD_ENV,
    ROLES,
    SEEDED_ROLES,
    database_name,
    maintenance_dsn,
    role_dsn,
)
from proxyshop_support.redis_client import (
    RedisDbCeilingUnknown,
    WorkerRedis,
    namespaced,
    worker_redis,
)
from proxyshop_support.worker import (
    DB_COUNT_ENV_VAR,
    REDIS_DB_COUNT,
    key_prefix,
    redis_db_count,
    redis_db_index,
)

# --------------------------------------------------------------------------------------
# embeddings (D6/D18)
# --------------------------------------------------------------------------------------


def test_hash_embed_is_unit_length() -> None:
    """The property that makes cosine similarity equal the dot product in the Neo4j index."""
    for text in ("espresso", "a much longer sentence about single-origin coffee", "x"):
        vector = hash_embed(text)
        assert len(vector) == EMBEDDING_DIM == 1024
        assert math.isclose(sum(c * c for c in vector), 1.0, rel_tol=1e-9)


def test_hash_embed_is_deterministic_and_discriminating() -> None:
    assert hash_embed("espresso") == hash_embed("espresso")
    assert hash_embed("espresso") != hash_embed("ristretto")
    assert math.isclose(cosine(hash_embed("espresso"), hash_embed("espresso")), 1.0, rel_tol=1e-9)
    assert abs(cosine(hash_embed("espresso"), hash_embed("ristretto"))) < 0.2


def test_hash_embed_is_stable_across_processes() -> None:
    """Determinism has to survive a fresh interpreter, or fixtures are not reproducible."""
    script = textwrap.dedent(
        """
        import sys
        from proxyshop_support.embedding import hash_embed
        print(sum(hash_embed("espresso")[:8]))
        """
    )
    # T-122 sweep: deliberately no `env=`. Passing none means the child INHERITS this
    # session's environment whole, so any PYTHONPATH already carrying `.pkgroot` survives —
    # the opposite of the clobber that broke the ledger surface tests. `proxyshop_support`
    # itself sits at the repo root, which `cwd` puts on the path for a `-c` child.
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert float(result.stdout.strip()) == pytest.approx(sum(hash_embed("espresso")[:8]))


def test_hash_embed_edge_cases() -> None:
    assert hash_embed("") == [0.0] * EMBEDDING_DIM
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    with pytest.raises(ValueError):
        hash_embed("x", dim=0)
    with pytest.raises(ValueError):
        cosine([1.0], [1.0, 2.0])


# --------------------------------------------------------------------------------------
# clocks
# --------------------------------------------------------------------------------------


def test_manual_clock_only_moves_forward(manual_clock: ManualClock) -> None:
    assert manual_clock.now() == EPOCH
    assert manual_clock.monotonic() == 0.0
    manual_clock.advance(90)
    assert manual_clock.now() == EPOCH + timedelta(seconds=90)
    assert manual_clock.monotonic() == 90.0
    manual_clock.advance(0)
    assert manual_clock.now() == EPOCH + timedelta(seconds=90)
    with pytest.raises(ValueError, match="negative"):
        manual_clock.advance(-1)
    assert manual_clock.now() == EPOCH + timedelta(seconds=90), "a rejected advance must not move"


def test_manual_clock_set_is_the_only_way_back(manual_clock: ManualClock) -> None:
    manual_clock.advance(10)
    manual_clock.set(datetime(2025, 6, 1, 12, 0, 0))
    assert manual_clock.now() == datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert manual_clock.now().tzinfo is UTC, "a naive datetime must be read as UTC"
    assert manual_clock.timestamp() == manual_clock.now().timestamp()


def test_frozen_clock_stops_the_global_wall_clock(frozen_clock) -> None:
    """``datetime.now()``/``time.time()`` freeze; ``time.monotonic()` does NOT.

    time-machine patches the wall clock only. That distinction is load-bearing for the
    auction-timeout tickets: code that measures a deadline with ``time.monotonic()`` (or
    ``asyncio``, which does) keeps running normally under this fixture, and the test will
    wait in real time. Use ``manual_clock`` for anything you can inject a clock into.
    """
    import time

    first = datetime.now(tz=UTC)
    assert first == EPOCH
    assert datetime.now(tz=UTC) == first, "the wall clock must not advance under tick=False"
    assert datetime.fromtimestamp(time.time(), tz=UTC) == EPOCH
    assert time.monotonic() != time.monotonic(), "documented: monotonic is NOT frozen"
    frozen_clock.shift(60)
    assert datetime.now(tz=UTC) == EPOCH + timedelta(seconds=60)


# --------------------------------------------------------------------------------------
# LLM double (D19)
# --------------------------------------------------------------------------------------


def test_llm_double_queue_is_fifo_and_consumed(llm_double: LLMDouble) -> None:
    llm_double.queue("first", "second")
    assert llm_double.complete("a") == "first"
    assert llm_double.complete("b") == "second"
    # exhausted: falls through to the deterministic default
    assert llm_double.complete("c") == LLMDouble.deterministic("default", "c")


def test_llm_double_records_prompt_assembly(llm_double: LLMDouble) -> None:
    llm_double.complete("static context\ndynamic tail", role="store_agent", system="be brief")
    assert len(llm_double.calls) == 1
    call = llm_double.calls[0]
    assert call.role == "store_agent"
    assert call.system == "be brief"
    assert llm_double.last_prompt.index("static context") < llm_double.last_prompt.index("dynamic")


def test_llm_double_canned_matches_and_precedence(llm_double: LLMDouble) -> None:
    llm_double.when("refund", '{"decision": "approve"}')
    assert llm_double.complete_json("please process the refund") == {"decision": "approve"}
    llm_double.queue("queued wins")
    assert llm_double.complete("please process the refund") == "queued wins"


def test_llm_double_default_is_stable_and_role_sensitive() -> None:
    assert LLMDouble().complete("p") == LLMDouble().complete("p")
    assert LLMDouble().complete("p", role="buyer") != LLMDouble().complete("p", role="seller")
    assert LLMDouble(default="fixed").complete("anything") == "fixed"


def test_llm_double_reset_keeps_canned_answers(llm_double: LLMDouble) -> None:
    llm_double.when("k", "v").queue("q")
    llm_double.complete("k")
    llm_double.reset()
    assert llm_double.calls == []
    assert llm_double.complete("k") == "v", "reset() drops calls and queue, not canned answers"


# --------------------------------------------------------------------------------------
# Redis worker isolation (D39) — no live Redis required
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("worker", [0, 2, 7, 15, 16, 17])
def test_worker_prefix_and_db_index_track_the_worker(worker: int) -> None:
    """Parameterised over indices that are NOT the running worker on purpose.

    The one pre-existing assertion compared ``key("cart")`` with ``f"w{worker_index}:cart"``
    at the running worker, so ``key_prefix()`` hard-coded to ``"w1:"`` passed at worker 1.

    The db-index half USED TO ASSERT ``worker % 16`` for every index here, which
    pinned the collision as correct behaviour: it required ``redis_db_index(16)
    == 0`` and ``(17) == 1`` — the exact sharing that lets one worker's per-test
    FLUSHDB wipe another's state, since FLUSHDB ignores the ``w{N}:`` prefix.
    Changed deliberately, and not to turn a red test green: the assertion was
    wrong. ``redis_db_index`` now refuses an index it cannot isolate. The prefix
    half is unchanged and still covers 16 and 17, because prefixes DO track any
    index — that was never the broken part.
    """
    assert key_prefix(worker) == f"w{worker}:"
    if worker < 16:
        assert redis_db_index(worker) == worker
    else:
        with pytest.raises(ValueError, match="cannot be isolated in Redis"):
            redis_db_index(worker)


@pytest.mark.parametrize("worker", [16, 17, 32, 64, 7071])
def test_redis_db_index_refuses_an_index_it_cannot_isolate(worker: int) -> None:
    """An index past the logical-DB ceiling must fail loudly, not share silently.

    Measured on the live cluster when this was found: 37 of 53 ``proxyshop_w*``
    databases carried an index >= 16, so this was the common case rather than a
    corner. 16, 32 and 64 all mapped onto worker 0's DB; 17 onto worker 1's. The
    message must name the worker actually collided with, because the operator's
    next question is whose run they just corrupted.
    """
    with pytest.raises(ValueError) as exc:
        redis_db_index(worker)
    msg = str(exc.value)
    assert f"shares DB {worker % 16}" in msg
    assert f"with worker {worker % 16}" in msg
    assert "PROXYSHOP_WORKER=0..15" in msg


def _offline_client(worker: int) -> WorkerRedis:
    """A wired-up client that never connects — connections are lazy in redis-py."""
    return worker_redis("redis://127.0.0.1:1/9", worker=worker)


@pytest.mark.parametrize("worker", [2, 3, 5])
def test_worker_redis_selects_the_workers_db_over_the_url(worker: int) -> None:
    """The URL's ``/9`` must lose. ``ConnectionPool.from_url`` does ``kwargs.update(
    url_options)``, so a ``db=`` keyword is silently discarded — every worker landed on the
    URL's database and ``redis_client``'s FLUSHDB wiped its siblings."""
    client = _offline_client(worker)
    assert client.get_connection_kwargs()["db"] == worker
    assert client.prefix == f"w{worker}:"


def test_worker_redis_namespaces_every_key_shape() -> None:
    client = _offline_client(4)
    assert client.key("cart") == "w4:cart"
    assert client.key(b"cart") == "w4:cart"
    assert client.key("w4:cart") == "w4:cart", "prefixing must be idempotent"
    assert client.unkey("w4:cart") == "cart"
    assert client.unkey("cart") == "cart"
    assert namespaced(["a", "w4:b"], worker=4) == ["w4:a", "w4:b"]


def test_worker_redis_rewrites_keys_in_the_command_it_sends(monkeypatch) -> None:
    """The prefix is applied in ``execute_command``, so prove it there rather than in ``key``."""
    client = _offline_client(6)
    sent: list[tuple] = []

    def record(self, *args, **kwargs) -> str:
        sent.append(args)
        return "OK"

    monkeypatch.setattr("redis.Redis.execute_command", record)
    client.set("cart", "1")
    client.mget("a", "b")
    client.rename("old", "new")
    client.publish("bids", "x")
    client.ping()
    client.dbsize()
    assert sent[0][:2] == ("SET", "w6:cart")
    assert sent[1] == ("MGET", "w6:a", "w6:b")
    assert sent[2] == ("RENAME", "w6:old", "w6:new")
    assert sent[3] == ("PUBLISH", "w6:bids", "x"), "channels are namespaced too"
    assert sent[4] == ("PING",), "keyless commands are untouched"
    assert sent[5] == ("DBSIZE",)


def test_worker_redis_refuses_the_banned_global_reset() -> None:
    with pytest.raises(RuntimeError, match="banned repo-wide"):
        _offline_client(1).execute_command("FLUSH" + "ALL")


# --------------------------------------------------------------------------------------
# The logical-DB ceiling: assumed offline, DISCOVERED from the server at connection time
# --------------------------------------------------------------------------------------
#
# The number of logical databases lives in exactly one authoritative place — the running
# Redis process — and everything else is a copy that can drift from it. `--databases` in
# docker-compose.yml is read only at container startup, so an edited compose file and a
# live server disagree until someone restarts it; a Python constant agrees with neither.
# So: `worker.py` keeps an ASSUMED ceiling and never opens a connection (offline callers
# and these very tests depend on that), and `redis_client.worker_redis` asks the server.
#
# These tests simulate the server's answer rather than requiring one, because this whole
# file is deliberately Docker-free (see the module docstring) — `make check` deselects
# `docker`, and a property that only holds when a stack is up would silently stop being
# checked there. `_server_db_count` builds a real client and calls the real `config_get`,
# so everything but the TCP round trip is the production path. The round trip itself was
# verified by hand against the live server; that is the one claim these cannot make.


def _answer_db_count(count: int):
    """A ``config_get`` that answers like a server with ``count`` logical databases."""

    def config_get(self, pattern: str = "*", **kwargs) -> dict[str, str]:
        assert pattern == "databases", f"asked for {pattern!r}, not the database count"
        return {"databases": str(count)}

    return config_get


def _config_get_denied(self, *args, **kwargs) -> dict[str, str]:
    """A server that will not answer: renamed command, ACL, old version, or a double."""
    raise RuntimeError("ERR unknown command 'CONFIG'")


def test_the_module_default_ceiling_is_not_a_copy_of_the_compose_flag() -> None:
    """``docker-compose.yml`` now says ``--databases 64``; this constant still says 16.

    That is the point, not an oversight. Raising the constant in lockstep with the compose
    file would make it claim 64 on every server that has not been restarted since — which
    is every server, until someone recreates the container. 16 is Redis's own default and
    therefore the only safe assumption about a server nobody has asked. The real number is
    read from the server at connection time, where it cannot be wrong.
    """
    assert REDIS_DB_COUNT == 16
    assert redis_db_count() == REDIS_DB_COUNT


def test_worker_identity_never_opens_a_socket(monkeypatch) -> None:
    """The main trap in making the ceiling discoverable.

    Worker identity is imported by processes that never speak to Redis, and the offline
    tests above build clients against a dead address on purpose. If resolving the ceiling
    required a live server, every one of them would break. So nothing in ``worker.py`` may
    connect — proved here by making any connection attempt an immediate failure.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("worker identity opened a socket")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.connect_ex", forbidden)
    assert key_prefix(3) == "w3:"
    assert redis_db_index(3) == 3
    assert redis_db_count() == REDIS_DB_COUNT
    with pytest.raises(ValueError, match="cannot be isolated in Redis"):
        redis_db_index(99)


def test_importing_worker_identity_does_not_even_pull_in_redis() -> None:
    """A fresh interpreter: ``proxyshop_support.worker`` must not import ``redis`` at all.

    Stronger than the socket test and immune to how a connection might be opened — a module
    that has not imported the client library cannot have connected with it. Run in a child
    because ``redis`` is long since imported in this session's ``sys.modules``.
    """
    script = textwrap.dedent(
        """
        import sys
        import proxyshop_support.worker as w
        assert w.redis_db_index(3) == 3
        print("redis" in sys.modules)
        """
    )
    # No `env=`, for the reason given on the hash_embed subprocess test above.
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.stdout.strip() == "False"


def test_a_supplied_ceiling_is_honoured_rather_than_quietly_ignored() -> None:
    """``db_count`` must reach the comparison AND the message it produces.

    A parameter that is accepted and then discarded is worse than one that does not exist:
    the caller has evidence it was considered. Index 20 is legal on a 64-database server
    and illegal on a 16-database one, and this asks for both answers.
    """
    assert redis_db_index(20, db_count=64) == 20
    assert redis_db_index(63, db_count=64) == 63
    with pytest.raises(ValueError) as exc:
        redis_db_index(64, db_count=64)
    msg = str(exc.value)
    assert "64 logical DBs" in msg, "the message must report the ceiling actually applied"
    assert "PROXYSHOP_WORKER=0..63" in msg


@pytest.mark.parametrize(
    ("ceiling_setup", "expected_source"),
    [
        (None, "assumed default; no server was asked"),
        ("env", f"from ${DB_COUNT_ENV_VAR}; no server was asked"),
        ("caller", "reported by the server at redis://example:6379"),
    ],
)
def test_the_refusal_says_where_its_ceiling_came_from(
    ceiling_setup: str | None, expected_source: str, monkeypatch
) -> None:
    """An assumed 16 and a measured 16 justify the same refusal but are not the same claim.

    An operator told "the server has 16" when no server was asked goes and checks the wrong
    thing — and the compose file, the constant and the running process can all disagree, so
    "which of you said 16?" is the operator's actual next question.
    """
    if ceiling_setup == "env":
        monkeypatch.setenv(DB_COUNT_ENV_VAR, "16")
    with pytest.raises(ValueError) as exc:
        if ceiling_setup == "caller":
            redis_db_index(
                16,
                db_count=16,
                ceiling_source="reported by the server at redis://example:6379 via CONFIG",
            )
        else:
            redis_db_index(16)
    assert expected_source in str(exc.value)


def test_the_db_count_override_moves_the_offline_ceiling(monkeypatch) -> None:
    """The escape hatch for a server known to be configured differently."""
    monkeypatch.setenv(DB_COUNT_ENV_VAR, "64")
    assert redis_db_count() == 64
    assert redis_db_index(20) == 20, "legal on a 64-database server"
    with pytest.raises(ValueError, match="64 logical DBs"):
        redis_db_index(64)


@pytest.mark.parametrize("bad", ["", "   ", "sixteen", "0", "-1", "1.5"])
def test_a_bad_db_count_override_never_silently_falls_back(bad: str, monkeypatch) -> None:
    """Empty means "unset" and falls back; anything else nonsensical is an error.

    Silently ignoring a malformed override would give the operator who typed it a ceiling
    they did not ask for, which is how a worker ends up sharing a database again.
    """
    monkeypatch.setenv(DB_COUNT_ENV_VAR, bad)
    if not bad.strip():
        assert redis_db_count() == REDIS_DB_COUNT
    else:
        with pytest.raises(ValueError, match=DB_COUNT_ENV_VAR):
            redis_db_count()


def test_the_offline_client_still_works_and_reports_its_ceiling_as_unknown() -> None:
    """The offline path: nothing listens on 127.0.0.1:1, and that must remain fine.

    The client is still fully wired — right database, right prefix — but its isolation was
    never confirmed, so ``db_ceiling`` is ``None`` and a warning says so out loud.
    """
    with pytest.warns(RedisDbCeilingUnknown, match="UNKNOWN"):
        client = _offline_client(3)
    assert client.db_ceiling is None, "None means UNCONFIRMED, never 'no limit'"
    assert client.prefix == "w3:"
    assert client.get_connection_kwargs()["db"] == 3


def test_worker_redis_refuses_an_index_the_server_says_it_cannot_isolate(monkeypatch) -> None:
    """The check that cannot drift: the ceiling comes from the server, not from a constant.

    The offline ceiling is raised to 4096 first, so it CANNOT be the thing doing the
    refusing — this pins the connection-time check specifically, which is the whole point
    of the change. Index 20 is exactly the case that a compose file saying 64 and a server
    still running 16 produces.
    """
    monkeypatch.setenv(DB_COUNT_ENV_VAR, "4096")
    monkeypatch.setattr("redis.Redis.config_get", _answer_db_count(16))
    assert redis_db_index(20) == 20, "the assumed ceiling permits it; the server must not"
    with pytest.raises(ValueError) as exc:
        worker_redis("redis://127.0.0.1:1/9", worker=20)
    msg = str(exc.value)
    assert "PROXYSHOP_WORKER=20" in msg, "name the worker"
    assert "16 logical DBs" in msg, "name the server's actual ceiling"
    assert "CONFIG GET databases" in msg, "name where that number came from"
    assert "PROXYSHOP_WORKER=0..15" in msg, "say what to do"
    assert "--databases" in msg and "restart" in msg, "say how to raise it"
    assert "shares DB 4 with worker 4" in msg, "name whose run this would have corrupted"


def test_a_raised_server_ceiling_is_usable_without_touching_any_python_constant(
    monkeypatch,
) -> None:
    """The half of "discovered" that a refusal-only test cannot see.

    Same index, same code path as the test above, a server that answers 64 instead of 16 —
    and crucially NO ``$PROXYSHOP_REDIS_DB_COUNT``, so the assumed ceiling is still 16 and
    would refuse worker 20 on its own. If the server's answer only ever narrowed what the
    constant allowed, raising ``--databases`` to 64 and restarting would buy exactly
    nothing, and the ceiling would not be discovered at all — merely double-checked.
    """
    assert redis_db_count() == 16, "the assumed ceiling alone would refuse worker 20"
    with pytest.raises(ValueError):
        redis_db_index(20)
    monkeypatch.setattr("redis.Redis.config_get", _answer_db_count(64))
    with warnings.catch_warnings():
        warnings.simplefilter("error", RedisDbCeilingUnknown)  # a confirmed pass must not warn
        client = worker_redis("redis://127.0.0.1:1/9", worker=20)
    assert client.db_ceiling == 64, "the ceiling recorded is the one the SERVER reported"
    assert client.get_connection_kwargs()["db"] == 20
    assert client.prefix == "w20:"


def test_an_unreadable_db_count_is_unknown_and_never_a_silent_pass(monkeypatch) -> None:
    """The failure mode that would quietly undo this whole mechanism.

    Same worker, same server, same call — the ONLY difference between the two halves below
    is whether ``CONFIG GET databases`` could be read. A server that answers refuses index
    20 outright. A server that will not answer must not therefore approve it: the ceiling
    is UNKNOWN, which is a third state, and it is announced rather than assumed away.
    """
    monkeypatch.setenv(DB_COUNT_ENV_VAR, "4096")
    monkeypatch.setattr("redis.Redis.config_get", _answer_db_count(16))
    with pytest.raises(ValueError, match="16 logical DBs"):
        worker_redis("redis://127.0.0.1:1/9", worker=20)

    monkeypatch.setattr("redis.Redis.config_get", _config_get_denied)
    with pytest.warns(RedisDbCeilingUnknown) as recorded:
        client = worker_redis("redis://127.0.0.1:1/9", worker=20)
    assert client.db_ceiling is None, "an unread config must not resolve to a real ceiling"
    text = str(recorded[0].message)
    assert "UNKNOWN" in text and "NOT confirmed" in text
    assert "PROXYSHOP_WORKER=20" in text
    assert "CONFIG GET databases" in text, "tell the operator how to check by hand"


def test_an_unreadable_db_count_still_gets_the_conservative_refusal(monkeypatch) -> None:
    """UNKNOWN warns *and* falls back — it does not warn *instead of* checking.

    With no override in play the assumed ceiling is 16, so worker 20 is refused even though
    nothing could be measured. The warning and the refusal are both required: dropping the
    refusal would make an unreachable server the easiest way past the ceiling.
    """
    monkeypatch.setattr("redis.Redis.config_get", _config_get_denied)
    with pytest.warns(RedisDbCeilingUnknown, match="UNKNOWN"):
        with pytest.raises(ValueError) as exc:
            worker_redis("redis://127.0.0.1:1/9", worker=20)
    assert "assumed default; no server was asked" in str(exc.value)


@pytest.mark.parametrize(
    "reply", [{"databases": "not-a-number"}, {"databases": None}, {}, "16", None, 16]
)
def test_a_nonsense_db_count_reply_is_unknown_rather_than_trusted(reply, monkeypatch) -> None:
    """A double, a proxy or an old server can answer with anything at all.

    Every shape that is not a positive integer under ``databases`` has to land in UNKNOWN.
    Coercing one of them into a number would produce a confident ceiling nobody measured.
    """
    monkeypatch.setattr("redis.Redis.config_get", lambda self, *a, **k: reply)
    with pytest.warns(RedisDbCeilingUnknown):
        client = worker_redis("redis://127.0.0.1:1/9", worker=1)
    assert client.db_ceiling is None


# --------------------------------------------------------------------------------------
# Postgres per-worker isolation (D38) — DSN derivation, no server required
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("worker", [0, 1, 4])
def test_dsns_are_derived_from_the_worker_not_copied_from_env(worker: int, monkeypatch) -> None:
    """A ``.env`` that names ``proxyshop_w1`` must not put worker 4 in worker 1's database."""
    monkeypatch.setenv(
        "PROXYSHOP_PG_DSN_ADMIN", "postgresql://proxyshop:pw@db.example:6000/proxyshop_w1"
    )
    dsn = role_dsn("admin", worker)
    assert dsn.endswith(f"/{database_name(worker)}")
    assert database_name(worker) == f"proxyshop_w{worker}"
    assert "db.example:6000" in dsn, "host/port/credentials still come from the environment"
    assert maintenance_dsn(worker).endswith("/postgres")


def test_role_dsns_default_without_any_env(monkeypatch) -> None:
    """The dev default, in the environment this test's own name promises: nothing set.

    T-120. This used to delete only the four ``PROXYSHOP_PG_DSN_*`` override variables and
    then assert the dev default *unconditionally*, which stopped being true the moment T-112
    gave ``$PROXYSHOP_ROLE_PASSWORD`` authority over the seeded roles' password
    (``proxyshop_support/postgres.py``, "T-112: the role password has ONE source of truth").
    A developer who exported that variable — that is, who USED the feature T-112 was written
    to deliver — turned the whole repo gate red right here, on a test whose name says it is
    about the case where nothing is exported.

    The assertion below is byte-for-byte what it always was. What changed is that the test
    now actually ESTABLISHES the "without any env" precondition it claims, instead of
    inheriting whatever the shell happened to carry. The case removed from it is not
    dropped: it is graded, more strictly, by
    :func:`test_role_dsns_use_the_role_password_variable_when_it_is_set` directly below.
    """
    for env_var, _, _ in ROLES.values():
        monkeypatch.delenv(env_var, raising=False)
    # T-120: the variable that decides the password, not just the ones that decide the DSN.
    monkeypatch.delenv(ROLE_PASSWORD_ENV, raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setenv("PG_PORT", "5432")
    assert role_dsn("exchange", 3) == "postgresql://exchange:x@localhost:5432/proxyshop_w3"
    with pytest.raises(KeyError):
        role_dsn("root", 3)


def test_role_dsns_use_the_role_password_variable_when_it_is_set(monkeypatch) -> None:
    """T-120 acceptance 3: the SET branch, so the two branches cannot drift apart again.

    The default above holds only while ``$PROXYSHOP_ROLE_PASSWORD`` is unset, and that is a
    claim about half of the behaviour. This is the other half, deliberately adjacent to it:
    what has to be true the moment somebody sets the variable. Before T-120 the SET branch
    was asserted nowhere on the connect side, so a change to :func:`role_password` that
    served one branch and broke the other passed the suite; now it fails a test.
    """
    for env_var, _, _ in ROLES.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setenv("PG_PORT", "5432")
    monkeypatch.setenv(ROLE_PASSWORD_ENV, "t120-not-the-default")

    # The four roles `db/init/00-roles.sql` seeds connect with what created them ...
    for role in sorted(SEEDED_ROLES):
        user = ROLES[role][1]
        assert role_dsn(role, 3) == (
            f"postgresql://{user}:t120-not-the-default@localhost:5432/proxyshop_w3"
        ), (
            f"{role} still connects with the dev default after ${ROLE_PASSWORD_ENV} was set, "
            f"so the connect side disagrees with the volume that seeded the role"
        )

    # ... and `admin` does NOT: it is initdb's superuser, created from compose's
    # POSTGRES_PASSWORD and never touched by 00-roles.sql. Rewriting it here would break
    # every admin connection — including the one that CREATEs each worker database.
    assert role_dsn("admin", 3) == (
        "postgresql://proxyshop:proxyshop_dev_pw@localhost:5432/proxyshop_w3"
    )

    # A password is a URI component, so it is percent-encoded rather than interpolated:
    # `postgresql://app:p@ss@host/db` names the host `ss`, which is a silent re-point.
    monkeypatch.setenv(ROLE_PASSWORD_ENV, "p@ss/w:rd")
    assert role_dsn("app", 3) == "postgresql://app:p%40ss%2Fw%3Ard@localhost:5432/proxyshop_w3"


# --------------------------------------------------------------------------------------
# Neo4j lock (D37) — re-entrant in-process, exclusive between processes
# --------------------------------------------------------------------------------------


def test_neo4j_flock_is_re_entrant_within_one_process(tmp_path: Path) -> None:
    """Two session-scoped guards in one whole-repo pytest run must not deadlock.

    ``fcntl.flock`` is per open file description, so the naive implementation blocked on
    itself here and burned the whole ``neo4j_lock.DEFAULT_TIMEOUT`` — mid-run, looking
    exactly like a hang. (That default was 600 s when this test was written and is 240 s
    since T-191, which brought it under pytest's own ``--timeout``; the number is quoted
    from the module now rather than restated here, because it moved once already.)
    """
    lock = tmp_path / "neo4j.lock"
    assert held_depth(lock) == 0
    with neo4j_flock(timeout=5.0, path=lock):
        assert held_depth(lock) == 1
        with neo4j_flock(timeout=1.0, poll=0.05, path=lock):  # would block forever if not
            assert held_depth(lock) == 2
        assert held_depth(lock) == 1
    assert held_depth(lock) == 0


def test_neo4j_flock_still_excludes_a_second_process(tmp_path: Path) -> None:
    """The point of D37: a different worker process waits, and times out loudly."""
    lock = tmp_path / "neo4j.lock"
    ready = tmp_path / "ready"
    # T-122 sweep: deliberately no `env=` — the holder inherits this session's environment
    # whole, so an inherited PYTHONPATH is preserved rather than replaced, and `cwd` (the
    # repo root) is what makes `proxyshop_support.neo4j_lock` importable in the child.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import pathlib, sys, time
                from proxyshop_support.neo4j_lock import neo4j_flock
                lock, ready = sys.argv[1], sys.argv[2]
                with neo4j_flock(timeout=10.0, path=lock):
                    pathlib.Path(ready).write_text("held")
                    time.sleep(30)
                """
            ),
            str(lock),
            str(ready),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PROXYSHOP_WORKER": "9"},
    )
    try:
        deadline = __import__("time").monotonic() + 20
        while not ready.exists():
            assert holder.poll() is None, "the holder process died before taking the lock"
            assert __import__("time").monotonic() < deadline, "holder never took the lock"
            __import__("time").sleep(0.05)

        assert held_depth(lock) == 0, "this process holds nothing; the other one does"
        with pytest.raises(Neo4jLockTimeout, match="another ProxyShop worker process"):
            with neo4j_flock(timeout=1.0, poll=0.05, path=lock):
                pytest.fail("acquired a lock another process holds — D37 is not enforced")
    finally:
        holder.kill()
        holder.wait(timeout=10)

    # …and once that process is gone the lock is free again.
    with neo4j_flock(timeout=10.0, poll=0.05, path=lock):
        assert held_depth(lock) == 1


def test_neo4j_flock_releases_on_an_exception(tmp_path: Path) -> None:
    lock = tmp_path / "neo4j.lock"
    with pytest.raises(ZeroDivisionError):
        with neo4j_flock(timeout=5.0, path=lock):
            1 / 0
    assert held_depth(lock) == 0
    # No dangling file handle. (The real /tmp lock may legitimately be held by the graph
    # lanes' session guards during a whole-repo run, so only this lock is checked.)
    assert lock.resolve() not in neo4j_lock._HELD


# --------------------------------------------------------------------------------------
# ephemeral ASGI server (D40) — the machinery behind `shopify_stub_url`
# --------------------------------------------------------------------------------------


async def _tiny_app(scope, receive, send) -> None:
    """A minimal ASGI app. It MUST speak the lifespan protocol: ``serve`` runs uvicorn with
    ``lifespan="on"``, which is a hard startup failure for an app that does not."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    assert scope["type"] == "http"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"stub": true}'})


def test_serve_binds_an_ephemeral_port_and_answers() -> None:
    """``shopify_stub_url`` is this function plus an import; D40 forbids a hard-coded port."""
    import httpx

    with serve(_tiny_app) as base_url:
        assert base_url.startswith("http://127.0.0.1:")
        port = int(base_url.rsplit(":", 1)[1])
        assert port != 0 and port > 1024
        response = httpx.get(f"{base_url}/anything", timeout=10.0)
        assert response.status_code == 200
        assert response.json() == {"stub": True}

    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{base_url}/anything", timeout=5.0)


def test_shopify_stub_url_fixture_is_wired_to_the_stub(request: pytest.FixtureRequest) -> None:
    """Until T-013 ships ``shopify_stub.app:app`` the fixture must SKIP, never error.

    Requesting it here is the only thing in the repo that proves the fixture is importable
    and that its skip path is a skip. Once T-013 lands, this turns into a live check that
    the agreed entry point exists.
    """
    try:
        base_url = request.getfixturevalue("shopify_stub_url")
    except pytest.skip.Exception as skipped:
        assert "shopify_stub.app:app" in str(skipped)
        return
    assert base_url.startswith("http://127.0.0.1:")
