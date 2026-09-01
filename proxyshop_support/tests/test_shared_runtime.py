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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from proxyshop_support import neo4j_lock
from proxyshop_support.asgi_server import serve
from proxyshop_support.clock import EPOCH, ManualClock
from proxyshop_support.embedding import EMBEDDING_DIM, cosine, hash_embed
from proxyshop_support.llm_double import LLMDouble
from proxyshop_support.neo4j_lock import Neo4jLockTimeout, held_depth, neo4j_flock
from proxyshop_support.postgres import ROLES, database_name, maintenance_dsn, role_dsn
from proxyshop_support.redis_client import WorkerRedis, namespaced, worker_redis
from proxyshop_support.worker import key_prefix, redis_db_index

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
    """
    assert key_prefix(worker) == f"w{worker}:"
    assert redis_db_index(worker) == worker % 16


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
    for env_var, _, _ in ROLES.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setenv("PG_PORT", "5432")
    assert role_dsn("exchange", 3) == "postgresql://exchange:x@localhost:5432/proxyshop_w3"
    with pytest.raises(KeyError):
        role_dsn("root", 3)


# --------------------------------------------------------------------------------------
# Neo4j lock (D37) — re-entrant in-process, exclusive between processes
# --------------------------------------------------------------------------------------


def test_neo4j_flock_is_re_entrant_within_one_process(tmp_path: Path) -> None:
    """Two session-scoped guards in one whole-repo pytest run must not deadlock.

    ``fcntl.flock`` is per open file description, so the naive implementation blocked on
    itself here and burned the full 600 s timeout — mid-run, looking exactly like a hang.
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
