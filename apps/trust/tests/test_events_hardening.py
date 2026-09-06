"""W6 hardening of the ledger writer: availability, cost, and legible failure.

Every test here was written **red first**, against the code as it shipped, and each one
reproduces a defect an adversarial audit found in ``apps/trust/src/events/**``. They are in
a file of their own rather than appended to ``test_events.py`` because they grade a
different property: not "does the ledger tell the truth" (that file's subject) but "does
the ledger stay *available*, stay *cheap*, and say something *useful* when it cannot".

===============================================  =====================================
An unreachable datastore is a 503, not a 500     :func:`test_a_datastore_that_cannot_be_
                                                 reached_is_a_503_with_the_documented_body`
...and it is bounded, not a 30-second stall      :func:`test_an_unreachable_dsn_fails_
                                                 fast_rather_than_blocking_on_the_pool`
One event costs one row, not the whole ledger    :func:`test_reading_one_event_by_id_
                                                 reads_one_row_and_not_the_ledger`
An unparameterised read is capped                :func:`test_an_unparameterised_events_
                                                 read_is_capped_by_default`
``seq`` is outside the hash, on purpose          :func:`test_seq_is_a_position_label_the_
                                                 chain_deliberately_does_not_hash`
A break at genesis reads as a break at genesis   :func:`test_a_break_in_the_first_link_
                                                 does_not_blame_a_predecessor_that_
                                                 does_not_exist`
The writer connects as trust_rw, not as app      :func:`test_the_ledger_writer_resolves_
                                                 the_per_role_dsn_variable_d5_grants_it`
===============================================  =====================================
"""

from __future__ import annotations

import contextlib
import socket
import time
from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest

from apps.trust.src.events import (
    InMemoryEventStore,
    PostgresEventStore,
    append,
    normalise_event,
    verify_stream,
)
from apps.trust.src.events.service import create_events_app
from apps.trust.src.ledger import (
    CHAIN_FIELDS,
    EVENT_FIELDS,
    GENESIS_HASH,
    chain_events,
    compute_event_hash,
    seal_event,
    stream_hash,
)
from proxyshop_support.asgi_server import serve

#: A fixed instant. Nothing in this file compares against the wall clock.
AS_OF = "2026-01-01T00:00:00.000Z"


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def _event(event_id: str, kind: str = "claim_verified", **extra: Any) -> dict[str, Any]:
    """A plain ``LedgerEvent`` body, ready to append or POST."""
    event: dict[str, Any] = {
        "event_id": event_id,
        "ts": AS_OF,
        "kind": kind,
        "store_id": "s-1",
        "payload": {"n": 1},
    }
    event.update(extra)
    return event


@contextlib.contextmanager
def _client_for(store: Any) -> Iterator[httpx.Client]:
    """A real HTTP client against a real loopback server serving ``store``."""
    with (
        serve(create_events_app(store)) as base_url,
        httpx.Client(base_url=base_url, timeout=60.0) as client,
    ):
        yield client


def _closed_loopback_port() -> int:
    """A port on 127.0.0.1 that nothing is listening on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _refuse_to_connect() -> Any:
    """A ``connect`` factory that fails exactly as psycopg fails on a dead server.

    ``psycopg.OperationalError`` is what an unreachable, restarting, out-of-connections or
    shutting-down Postgres produces (SQLSTATE class 08/53/57), and it is what
    ``psycopg_pool`` raises too -- ``PoolTimeout`` and ``PoolClosed`` are both subclasses of
    it. Simulating the outage this way keeps the test deterministic and instant while
    exercising the *same* exception the real outage delivers.
    """
    raise psycopg.OperationalError(
        "connection failed: connection to server at 127.0.0.1, port 1 failed: Connection refused"
    )


# ======================================================================================
# 1. [HIGH] a connection failure must become StoreUnavailable, and therefore a 503
# ======================================================================================
def test_a_datastore_that_cannot_be_reached_is_a_503_with_the_documented_body() -> None:
    """Every endpoint must refuse with the documented 503 body, not an empty 500.

    Red first: ``_connection()`` used to acquire the connection *outside* the guarded
    region in ``_append_on``, so ``psycopg.OperationalError`` walked straight out of the
    handler and Starlette turned it into a bare ``500 Internal Server Error`` with no body
    at all. A caller could not tell "the ledger is down, retry" from "your event broke the
    server, do not retry" -- which is the whole reason :class:`StoreUnavailable` and its
    503 exist.
    """
    store = PostgresEventStore(connect=_refuse_to_connect)

    with _client_for(store) as client:
        responses = {
            "POST /events": client.post("/events", json=_event("ev-1")),
            "GET /events": client.get("/events"),
            "GET /events/head": client.get("/events/head"),
            "GET /events/verify": client.get("/events/verify"),
            "GET /events/replay": client.get("/events/replay"),
            "GET /events/{id}": client.get("/events/ev-1"),
        }

    for name, response in responses.items():
        assert response.status_code == 503, f"{name} answered {response.status_code}"
        body = response.json()
        assert body["detail"]["error"] == "store_unavailable", f"{name}: {body}"
        message = body["detail"]["message"]
        assert message, f"{name} returned a 503 with an empty message"
        assert "Connection refused" in message, (
            f"{name}'s 503 does not carry the driver's diagnosis, so an operator cannot "
            f"tell an unreachable host from a wrong password: {message!r}"
        )


# T-172: no `docker` mark. This test's DSN points at a port it just closed itself, so it
# needs no compose service at all — exactly like its sibling
# `test_a_datastore_that_cannot_be_reached_is_a_503_with_the_documented_body` above, which
# has never carried the mark. It was marked `docker` with no argument, which the whole-stack
# fallback then read as "needs Postgres and Neo4j and Redis", so a Redis blip skipped a test
# that cannot touch Redis. Declaring a service would be a lie; dropping the mark is the fix.
def test_an_unreachable_dsn_fails_fast_rather_than_blocking_on_the_pool() -> None:
    """A real DSN pointing at a dead port: bounded wait, 503, no 30-second stall.

    Red first, twice over. ``psycopg_pool.ConnectionPool``'s default ``timeout`` is 30
    seconds, so every request against a Postgres that is down parked an HTTP worker for
    half a minute and *then* answered 500. The pool's wait is now derived from
    ``connect_timeout``, so this store gives up in roughly two seconds -- and reports the
    outage as the documented refusal.
    """
    dsn = f"postgresql://proxyshop:proxyshop@127.0.0.1:{_closed_loopback_port()}/nowhere"
    store = PostgresEventStore(dsn, connect_timeout=1)

    try:
        started = time.monotonic()
        with _client_for(store) as client:
            response = client.post("/events", json=_event("ev-1"))
        elapsed = time.monotonic() - started
    finally:
        store.close()

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == "store_unavailable"
    assert elapsed < 15.0, (
        f"the writer blocked {elapsed:.1f}s on an unreachable datastore; a request that "
        f"cannot be served must be refused promptly, not hold a worker for the pool's "
        f"whole default wait"
    )


def test_a_store_with_no_dsn_at_all_still_reports_store_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-existing 503 path must survive the new one: no DSN is still a 503.

    This is the control for the test above. If the connection-failure mapping had been
    written as a blanket ``except Exception`` it would also have swallowed the *configuration*
    failure into a different message, and the two outages -- "nothing is configured" and
    "the configured thing is down" -- read identically to the operator who has to fix one
    of them.
    """
    from apps.trust.src.events.pg import DEFAULT_DSN_ENV

    for name in DEFAULT_DSN_ENV:
        monkeypatch.delenv(name, raising=False)

    with _client_for(PostgresEventStore()) as client:
        response = client.post("/events", json=_event("ev-1"))

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "store_unavailable"
    assert "no DSN was passed" in detail["message"], (
        "a misconfigured writer must not be reported with the same message as a dead server"
    )


# ======================================================================================
# 2. [MEDIUM] one event by id costs one row, not the whole ledger
# ======================================================================================
class _CountingCursor:
    """A cursor that records the statement it runs and how many rows it hands back."""

    def __init__(self, inner: Any, log: list[tuple[str, int]]) -> None:
        self._inner = inner
        self._log = log
        self._statement = ""

    def __enter__(self) -> _CountingCursor:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        return self._inner.__exit__(*exc_info)

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        self._statement = str(query)
        self._log.append((self._statement, -1))
        return self._inner.execute(query, params, **kwargs)

    def fetchall(self) -> Any:
        rows = self._inner.fetchall()
        self._log[-1] = (self._statement, len(rows))
        return rows

    def fetchone(self) -> Any:
        row = self._inner.fetchone()
        self._log[-1] = (self._statement, 0 if row is None else 1)
        return row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CountingConnection:
    """A connection proxy that logs ``(sql, rows_returned)`` for every statement."""

    def __init__(self, inner: Any, log: list[tuple[str, int]]) -> None:
        self._inner = inner
        self._log = log

    def cursor(self, *args: Any, **kwargs: Any) -> _CountingCursor:
        return _CountingCursor(self._inner.cursor(*args, **kwargs), self._log)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.mark.docker
def test_reading_one_event_by_id_reads_one_row_and_not_the_ledger(
    events_dsn: str, ledger_clean: Any
) -> None:
    """``GET /events/{event_id}`` must be an indexed lookup, not a full scan plus a loop.

    Red first: ``PostgresEventStore.get`` called ``read_events(connection)`` with no
    predicate and no limit, pulled **every** row into Python, and linear-scanned the list.
    On a ledger of any size that is the difference between one index probe and reading the
    whole append-only history to answer "is this one event there" -- once per request, on
    the endpoint a retrying client hits hardest.
    """
    seeded = PostgresEventStore(events_dsn)
    try:
        for index in range(25):
            seeded.append(_event(f"ev-{index:03d}"))
    finally:
        seeded.close()

    log: list[tuple[str, int]] = []

    def connect() -> Any:
        return _CountingConnection(
            psycopg.connect(events_dsn, autocommit=True, connect_timeout=5), log
        )

    store = PostgresEventStore(connect=connect)

    found = store.get("ev-007")
    assert found is not None and found["event_id"] == "ev-007"
    read = [(sql, rows) for sql, rows in log if rows > 0]
    assert read, "the lookup issued no row-returning statement at all"
    assert max(rows for _, rows in read) == 1, (
        f"looking one event up read {max(rows for _, rows in read)} rows; it must read one. "
        f"statements: {[sql for sql, _ in log]}"
    )
    assert any("idempotency_key" in sql for sql, _ in log), (
        "the lookup does not select on the unique idempotency_key index, so it cannot be "
        "O(1) however few rows happen to exist right now"
    )

    log.clear()
    assert store.get("ev-nope") is None
    assert all(rows <= 0 for _, rows in log), "a miss must not read the ledger either"


# ======================================================================================
# 3. [MEDIUM] an unparameterised read must not serialise the whole ledger
# ======================================================================================
#: Longer than any sane default page, and fixed here rather than derived from the cap, so
#: that the tests below fail on the *behaviour* rather than on an import of the new name.
LONG_STREAM = 1500


@pytest.fixture
def events_long_stream() -> InMemoryEventStore:
    """An in-memory ledger comfortably longer than any sane default page."""
    store = InMemoryEventStore()
    for index in range(LONG_STREAM):
        append(store, _event(f"ev-{index:05d}"))
    return store


def test_an_unparameterised_events_read_is_capped_by_default(
    events_long_stream: InMemoryEventStore,
) -> None:
    """``GET /events`` with no parameters must page, and must say that it paged.

    Red first: ``limit`` was ``Query(None, ge=1, le=10_000)``, so the ceiling bound only
    the callers who *asked* for one. A caller who asked for nothing got every row in the
    ledger serialised into a single response -- unbounded memory in the server, unbounded
    bytes on the wire, and a denial of service available to anyone who can reach the port.
    """
    with _client_for(events_long_stream) as client:
        body = client.get("/events").json()

    assert body["count"] < LONG_STREAM, (
        f"an unparameterised read serialised all {body['count']} rows in the ledger"
    )
    assert len(body["events"]) == body["count"]
    assert body["truncated"] is True, "a capped response that does not say so is a lie by omission"
    assert body["is_chain"] is False, "a prefix of the ledger is not the ledger"
    assert body["next_after_seq"] == body["count"], (
        "a truncated page must hand the caller the cursor that continues it"
    )


def test_the_default_page_is_the_documented_one(
    events_long_stream: InMemoryEventStore,
) -> None:
    """The cap is a published number, not whatever the handler happens to do today."""
    from apps.trust.src.events.routes import DEFAULT_EVENT_PAGE, MAX_EVENT_PAGE

    assert 1 <= DEFAULT_EVENT_PAGE <= MAX_EVENT_PAGE
    assert LONG_STREAM > DEFAULT_EVENT_PAGE, (
        "the fixture must exceed the cap or this proves nothing"
    )

    with _client_for(events_long_stream) as client:
        body = client.get("/events").json()
        over = client.get("/events", params={"limit": MAX_EVENT_PAGE + 1})

    assert body["count"] == DEFAULT_EVENT_PAGE
    assert body["limit"] == DEFAULT_EVENT_PAGE
    assert over.status_code == 422, "the ceiling on an explicit limit must still bind"


def test_the_events_cursor_walks_the_whole_ledger_in_pages(
    events_long_stream: InMemoryEventStore,
) -> None:
    """The cap is only acceptable if the rest is still reachable."""
    seen: list[str] = []
    after = 0
    with _client_for(events_long_stream) as client:
        for _ in range(10):
            body = client.get("/events", params={"after_seq": after, "limit": 400}).json()
            seen.extend(row["event_id"] for row in body["events"])
            if not body["truncated"]:
                break
            after = body["next_after_seq"]

    assert seen == [row["event_id"] for row in events_long_stream.events]


def test_a_short_ledger_read_whole_is_still_labelled_a_chain() -> None:
    """The cap must not turn every complete answer into a suspected prefix."""
    store = InMemoryEventStore()
    for index in range(3):
        append(store, _event(f"ev-{index}"))

    with _client_for(store) as client:
        body = client.get("/events").json()

    assert body["count"] == 3
    assert body["truncated"] is False
    assert body["is_chain"] is True


def test_replay_caps_the_events_it_serialises_without_narrowing_what_it_verified(
    events_long_stream: InMemoryEventStore,
) -> None:
    """``GET /events/replay`` had the same hole, and its fix must not weaken the evidence.

    Red first: ``/events/replay`` took no ``limit`` at all and serialised every row.
    Capping the *serialised* list is safe; capping what is *verified* would not be, because
    ``stream_hash``, ``length`` and ``ok`` are the evidence the endpoint exists to produce
    and they are only meaningful over the whole ledger. So the numbers below must still
    describe all of it.
    """
    total = events_long_stream.length
    with _client_for(events_long_stream) as client:
        report = client.get("/events/replay").json()

    assert report["ok"] is True, report["detail"]
    assert report["length"] == total, "replay must verify the whole ledger, not the page"
    assert report["stream_hash"] == stream_hash(events_long_stream.events)
    assert report["head_hash"] == events_long_stream.head_hash
    assert len(report["events"]) < total, (
        f"replay serialised all {len(report['events'])} rows into one response"
    )
    assert report["events_truncated"] is True
    assert report["events_returned"] == len(report["events"])


# ======================================================================================
# 4. [LOW] `seq` is outside the hash, and this is the decision being pinned
# ======================================================================================
def test_seq_is_a_position_label_the_chain_deliberately_does_not_hash() -> None:
    """``seq`` is stamped after sealing, and it must stay out of the hashed body.

    This test pins the *choice*, not an accident. ``seq`` cannot be hashed:

    * in Postgres it is a ``bigserial`` the database assigns when the row lands, which is
      strictly after the digest is computed -- an event cannot commit to a number that does
      not exist yet;
    * and if the in-memory store hashed a ``seq`` it made up while Postgres hashed one the
      database made up, the two writers would produce different digests for the same event
      and "one hashing rule, two stores" (D16) would be false.

    What commits to an event's *position* is ``prev_hash``: every event names its
    predecessor's digest, so the order is sealed even though the label of the order is not.
    The two assertions below are the two halves of that claim.
    """
    assert "seq" in CHAIN_FIELDS, "seq is a chain field: written onto the event, not into it"
    assert "seq" not in EVENT_FIELDS

    body = normalise_event(_event("ev-1"))
    sealed = seal_event(body, GENESIS_HASH)
    assert "seq" not in sealed, "seal_event must not invent a position"

    # Half one: relabelling moves no digest. That is what "outside the hash" means, and it
    # is why the label alone is not evidence of anything.
    relabelled = {**sealed, "seq": 4321}
    assert compute_event_hash(GENESIS_HASH, relabelled) == sealed["event_hash"]

    # Half two: the ORDER the label names is sealed anyway. Renumber two events so that a
    # `order by seq` read hands them back the other way round, and the links catch it.
    stream = chain_events([normalise_event(_event(f"ev-{i}")) for i in range(4)])
    for index, row in enumerate(stream):
        row["seq"] = index + 1
    assert verify_stream(stream)["ok"] is True

    stream[1]["seq"], stream[2]["seq"] = stream[2]["seq"], stream[1]["seq"]
    reread = sorted(stream, key=lambda row: int(row["seq"]))
    report = verify_stream(reread)
    assert report["ok"] is False, (
        "renumbering seq changed the order a reader gets and nothing noticed -- if this "
        "ever passes, seq has become an unverifiable field that decides what a reader sees"
    )
    assert report["reason"] == "broken_link"


def test_both_stores_stamp_the_same_seq_onto_the_same_event(events_memory: Any) -> None:
    """The in-memory ``seq`` is 1-based insertion order, exactly as ``bigserial`` is.

    The label is not hashed, so the only thing keeping the two stores' labels comparable is
    that both count the same way. ``test_events.py`` asserts the Postgres side end to end;
    this is the in-memory half, so a change to either is a failing test rather than a
    quietly divergent ``seq`` in an API response.
    """
    for index in range(3):
        outcome = append(events_memory, _event(f"ev-{index}"))
        assert outcome.seq == index + 1
        assert outcome.event["seq"] == index + 1


# ======================================================================================
# 5. [LOW] a break in the first link must not blame a predecessor that does not exist
# ======================================================================================
def test_a_break_in_the_first_link_does_not_blame_a_predecessor_that_does_not_exist() -> None:
    """Index 0 has no predecessor, so the message must talk about genesis.

    Red first: ``describe_break`` interpolated ``index - 1`` and the predecessor's
    ``event_id`` unconditionally, so a stream whose first event does not link to genesis --
    which is exactly what "events were deleted from the front of the ledger" looks like --
    was reported as "the event before it (index -1, event_id=None) hashes to 000...0". The
    one case where the reader most needs to be told *what* is missing was the one case the
    sentence was nonsense.
    """
    stream = chain_events([normalise_event(_event(f"ev-{i}")) for i in range(4)])
    beheaded = stream[1:]  # the front of the ledger, removed

    report = verify_stream(beheaded)
    assert report["ok"] is False
    assert report["reason"] == "broken_link"
    assert report["broken_at"] == 0

    detail = report["detail"]
    assert "index -1" not in detail, f"the report invented an event at index -1: {detail}"
    assert "event_id=None" not in detail, f"the report blamed a nonexistent event: {detail}"
    assert "genesis" in detail.lower(), (
        f"a first-link break is a claim about the genesis link; the message must say so: {detail}"
    )
    assert GENESIS_HASH in detail
    assert str(beheaded[0]["prev_hash"]) in detail, (
        "the message must show what the event actually stores, or it cannot be acted on"
    )

    broken = report["broken_event"]
    assert broken["event_id"] == "ev-1"
    assert broken["predecessor_event_id"] is None
    assert broken["expected_prev_hash"] == GENESIS_HASH


def test_a_break_in_a_later_link_still_names_its_real_predecessor() -> None:
    """The control for the fix above: the ordinary case must keep naming the predecessor."""
    stream = chain_events([normalise_event(_event(f"ev-{i}")) for i in range(4)])
    stream[1], stream[2] = stream[2], stream[1]

    report = verify_stream(stream)
    assert report["reason"] == "broken_link"
    assert report["broken_at"] == 1
    assert "index 0" in report["detail"]
    assert "'ev-0'" in report["detail"]


# ======================================================================================
# 6. [HIGH] the ledger writer must connect as the role D5 grants it, not as `app`
# ======================================================================================
#: Every environment variable that could hand the writer a DSN. A test that cleared only
#: ``DEFAULT_DSN_ENV`` would track whatever that tuple happens to say and could therefore
#: never observe a variable missing *from* it -- which is precisely the defect below.
_ALL_LEDGER_DSN_ENV = (
    "PROXYSHOP_LEDGER_DSN",
    "PROXYSHOP_PG_DSN_TRUST_RW",
    "PROXYSHOP_PG_DSN_APP",
)


def _isolate_ledger_dsn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every DSN variable the writer could read, from either source of truth."""
    from apps.trust.src.events.pg import DEFAULT_DSN_ENV

    for name in (*_ALL_LEDGER_DSN_ENV, *DEFAULT_DSN_ENV):
        monkeypatch.delenv(name, raising=False)


def test_the_ledger_writer_resolves_the_per_role_dsn_variable_d5_grants_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment that sets only the documented per-role variable must be served by it.

    Red first. ``DEFAULT_DSN_ENV`` was ``("PROXYSHOP_LEDGER_DSN", "PROXYSHOP_PG_DSN_APP")``
    and never named ``PROXYSHOP_PG_DSN_TRUST_RW`` -- the variable
    ``proxyshop_support.postgres.ROLES["trust_rw"]`` designates for this exact writer and
    the one ``.env.example`` documents. So a deployment that configured the ledger the
    documented way got :class:`StoreUnavailable` ("no database to write to") while the
    correct DSN sat in the environment unread.

    The variable name is taken from the role registry rather than typed as a literal, so
    this test grades "the writer reads *its own role's* variable", not "the writer reads
    some string that happens to be spelled this way today".
    """
    from apps.trust.src.events.pg import PostgresEventStore
    from proxyshop_support.postgres import ROLES

    per_role_env = ROLES["trust_rw"][0]
    trust_rw_dsn = "postgresql://trust_rw:pw@db.example:5432/proxyshop_w0"

    _isolate_ledger_dsn_env(monkeypatch)
    monkeypatch.setenv(per_role_env, trust_rw_dsn)

    assert PostgresEventStore()._resolve_dsn() == trust_rw_dsn, (
        f"the ledger writer ignored {per_role_env}, the per-role DSN D5 grants it; a "
        f"deployment configured the documented way cannot write the ledger at all"
    )


def test_the_per_role_ledger_dsn_outranks_the_generic_app_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both set, the writer connects as ``trust_rw`` -- never as ``app``.

    This is the privilege-confusion half, and it is the half that fails *silently*: with
    ``PROXYSHOP_PG_DSN_APP`` also present the writer connected happily, just as the wrong
    role, so nothing in the logs or the response said a thing. ``app`` is a different grant
    set from the one D5 gives the ledger; a writer that silently borrows it is exercising
    privileges the deployment did not intend to give it.
    """
    from apps.trust.src.events.pg import DEFAULT_DSN_ENV, PostgresEventStore
    from proxyshop_support.postgres import ROLES

    per_role_env, app_env = ROLES["trust_rw"][0], ROLES["app"][0]
    trust_rw_dsn = "postgresql://trust_rw:pw@db.example:5432/proxyshop_w0"
    app_dsn = "postgresql://app:pw@db.example:5432/proxyshop_w0"

    _isolate_ledger_dsn_env(monkeypatch)
    monkeypatch.setenv(per_role_env, trust_rw_dsn)
    monkeypatch.setenv(app_env, app_dsn)

    resolved = PostgresEventStore()._resolve_dsn()
    assert resolved == trust_rw_dsn, (
        f"the ledger writer resolved {resolved!r}; with {per_role_env} set it must connect "
        f"as trust_rw, not fall through to the generic {app_env} role"
    )
    assert DEFAULT_DSN_ENV.index(per_role_env) < DEFAULT_DSN_ENV.index(app_env), (
        "the per-role variable must be consulted before the generic app DSN, or the "
        "precedence above holds only by accident of which one a deployment sets"
    )


def test_an_explicit_ledger_dsn_override_still_outranks_the_per_role_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented escape hatch survives the fix: ``PROXYSHOP_LEDGER_DSN`` wins.

    ``apps/trust/compose.yaml`` sets it deliberately to point the writer somewhere without
    disturbing the per-role variables. Inserting ``trust_rw`` ahead of it would have turned
    that deployment's explicit instruction into a no-op.
    """
    from apps.trust.src.events.pg import PostgresEventStore
    from proxyshop_support.postgres import ROLES

    override = "postgresql://ledger_override:pw@elsewhere.example:5432/ledger"

    _isolate_ledger_dsn_env(monkeypatch)
    monkeypatch.setenv("PROXYSHOP_LEDGER_DSN", override)
    monkeypatch.setenv(ROLES["trust_rw"][0], "postgresql://trust_rw:pw@db.example:5432/w0")

    assert PostgresEventStore()._resolve_dsn() == override


def test_a_deployment_that_sets_only_the_generic_app_dsn_keeps_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility control: adding a name must not remove one.

    Three of the four services still hand the writer only ``PROXYSHOP_PG_DSN_APP``. A fix
    that dropped the generic fallback instead of ranking below it would take those
    deployments from "wrong role" to "no ledger at all", which is worse.
    """
    from apps.trust.src.events.pg import PostgresEventStore
    from proxyshop_support.postgres import ROLES

    app_dsn = "postgresql://app:pw@db.example:5432/proxyshop_w0"

    _isolate_ledger_dsn_env(monkeypatch)
    monkeypatch.setenv(ROLES["app"][0], app_dsn)

    assert PostgresEventStore()._resolve_dsn() == app_dsn


@pytest.mark.docker
def test_the_writer_connects_to_the_real_database_as_trust_rw_from_the_per_role_var(
    monkeypatch: pytest.MonkeyPatch,
    ledger_migrated: str,
    worker_database: str,
    worker_index: int,
) -> None:
    """The ticket's reproduction, automated against the live database.

    Every other test here grades a *string*: which value ``_resolve_dsn`` hands back. That
    is necessary and not sufficient -- the finding was reported as "start the trust service
    with only the documented per-role variable and observe it connects as the app role",
    and only a real connection can answer which role it connected as. So this one sets the
    environment the way the deployment does, opens a connection through the store's own
    pool, and asks Postgres.

    Red on the code as shipped for the first reason a deployment would notice: with only
    ``PROXYSHOP_PG_DSN_TRUST_RW`` set there was no DSN at all and the store raised
    :class:`StoreUnavailable` before reaching a connection.

    ``ledger_migrated`` is requested because the ``has_table_privilege`` call below names
    ``ledger.commerce_events``, and that schema has to exist *on purpose*. Without the
    declaration this test was graded against whatever the persistent ``proxyshop_w<N>``
    database happened to hold when it ran -- which meant a fresh database failed it outright
    with ``InvalidSchemaName``, and a session that rebuilt the schemas underneath it failed
    it intermittently, four times across four workers, always blamed on an unrelated lane
    (T-216). It also makes the privilege assertion mean something: the grant being read back
    is the one *this checkout's* ``0004_object_grants.sql`` just applied, not a leftover.
    """
    from apps.trust.src.events.pg import PostgresEventStore
    from proxyshop_support.postgres import ROLES, role_dsn

    _isolate_ledger_dsn_env(monkeypatch)
    monkeypatch.setenv(
        ROLES["trust_rw"][0], role_dsn("trust_rw", worker_index, database=worker_database)
    )

    store = PostgresEventStore()
    try:
        with store._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            connected_as = cursor.fetchone()[0]
            cursor.execute(
                "SELECT has_table_privilege(current_user, 'ledger.commerce_events', 'INSERT')"
            )
            may_append = cursor.fetchone()[0]
    finally:
        store.close()

    assert connected_as == "trust_rw", (
        f"the deployed ledger writer connected as {connected_as!r}; the environment named "
        f"trust_rw and nothing else, so any other role is a privilege the deployment did "
        f"not grant it"
    )
    assert may_append, "the role the writer resolved cannot append to the ledger at all"


@pytest.mark.docker
def test_the_writer_appends_to_the_real_ledger_as_trust_rw(
    monkeypatch: pytest.MonkeyPatch,
    ledger_clean: Any,
    worker_database: str,
    worker_index: int,
) -> None:
    """Resolving the right role is only worth anything if that role can do the work.

    The privilege sets are genuinely different, not nested: 0004 grants ``app``
    ``SELECT, INSERT`` on ``ledger`` plus full DML on the ``app`` and ``sealed`` schemas,
    while ``trust_rw`` gets full DML on ``ledger``, read-only on ``app``, and nothing in
    ``sealed``. So swapping the writer's role is a real change to what a write can touch,
    and "it connected" would not prove the append path -- advisory lock, insert, the
    ``AFTER INSERT`` trigger that maintains ``ledger.chain_head`` -- still works under it.
    This appends through the store's public entry point and reads the chain back.

    This test predates T-154, when every other database-backed test in this package
    connected as ``app`` and this was the only one exercising the shipped role.
    ``_fixtures_events.EVENTS_ROLE`` is now ``trust_rw`` too, so the rest of the package
    grades the same principal; what remains distinctive here is that this test resolves the
    role from the ENVIRONMENT through ``PostgresEventStore()``'s own ``DEFAULT_DSN_ENV``
    walk rather than being handed a DSN by a fixture.
    """
    from proxyshop_support.postgres import ROLES, role_dsn

    _isolate_ledger_dsn_env(monkeypatch)
    monkeypatch.setenv(
        ROLES["trust_rw"][0], role_dsn("trust_rw", worker_index, database=worker_database)
    )

    store = PostgresEventStore()
    try:
        outcome = append(store, _event("ev-trust-rw-1"))
        assert outcome.seq == 1
        assert outcome.event["prev_hash"] == GENESIS_HASH

        anchor = store.anchor()
        assert anchor["length"] == 1
        assert anchor["head_hash"] == outcome.event["event_hash"], (
            "the chain_head trigger did not fire for a row written by trust_rw"
        )
        assert store.verify()["ok"] is True
    finally:
        store.close()


@pytest.mark.docker
def test_the_writer_connects_as_trust_rw_when_both_dsn_variables_are_set(
    monkeypatch: pytest.MonkeyPatch,
    ledger_migrated: str,
    worker_database: str,
    worker_index: int,
) -> None:
    """T-181: the ORDER, graded against a real connection instead of against a string.

    Every other precedence test in this file grades what ``_resolve_dsn`` HANDS BACK, and the
    one that grades a real connection isolates the environment down to a single variable
    first — so it defends the OMISSION T-151 filed (the per-role variable was not read at
    all) and never the ORDER (which of two set variables wins). That gap is what T-181 is.

    The arrangement below is the one a real deployment presents, and the reason it is not
    hypothetical: three of the four services hand this process ``PROXYSHOP_PG_DSN_APP``, and
    a trust deployment additionally sets ``PROXYSHOP_PG_DSN_TRUST_RW``. Both are set, both
    are live, both reach the same database — and the only thing that decides which principal
    the ledger writer ends up holding is ``DEFAULT_DSN_ENV``'s order.

    WHY THIS IS NOT THE STRING TEST AGAIN. ``test_the_per_role_ledger_dsn_outranks_the_
    generic_app_dsn`` sets both too, and asserts on the returned string. It cannot see the
    consequence: which grant set the writer actually holds. ``app`` and ``trust_rw`` are
    genuinely different grants (0004 gives ``app`` ``SELECT, INSERT`` on ``ledger`` and full
    DML on ``sealed``; ``trust_rw`` gets full DML on ``ledger`` and nothing in ``sealed``),
    so this asks Postgres which one it is talking to and reads a ``sealed`` privilege back as
    the discriminator — a value that differs between the two roles and would therefore be
    wrong, not merely absent, if the order flipped.

    RED UNDER THE SABOTAGE THIS EXISTS FOR: reorder ``DEFAULT_DSN_ENV`` to put
    ``PROXYSHOP_PG_DSN_APP`` ahead of ``PROXYSHOP_PG_DSN_TRUST_RW`` (the T-151 defect,
    re-shipped) and ``current_user`` reads ``app`` here. No expectation in this test is read
    off ``DEFAULT_DSN_ENV``: ``trust_rw`` is named because ``proxyshop_support.postgres.ROLES``
    and D5 designate it for this writer, which is an authority outside the tuple under test.
    """
    from apps.trust.src.events.pg import PostgresEventStore
    from proxyshop_support.postgres import ROLES, role_dsn

    _isolate_ledger_dsn_env(monkeypatch)
    trust_rw_dsn = role_dsn("trust_rw", worker_index, database=worker_database)
    app_dsn = role_dsn("app", worker_index, database=worker_database)
    assert trust_rw_dsn != app_dsn, (
        "the two role DSNs are indistinguishable, so this test could not tell which one the "
        "writer used even if it connected"
    )
    monkeypatch.setenv(ROLES["trust_rw"][0], trust_rw_dsn)
    monkeypatch.setenv(ROLES["app"][0], app_dsn)

    store = PostgresEventStore()
    try:
        with store._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            connected_as = cursor.fetchone()[0]
            cursor.execute("SELECT has_schema_privilege(current_user, 'sealed', 'USAGE')")
            may_touch_sealed = cursor.fetchone()[0]
    finally:
        store.close()

    assert connected_as == "trust_rw", (
        f"with BOTH {ROLES['trust_rw'][0]} and {ROLES['app'][0]} set — the arrangement a real "
        f"deployment presents — the ledger writer opened its connection as {connected_as!r}. "
        f"D5 grants this writer trust_rw; falling through to the generic app role is the "
        f"privilege confusion T-151 was filed as, and it is silent: the write succeeds."
    )
    assert not may_touch_sealed, (
        f"the principal the writer resolved holds USAGE on `sealed`, which trust_rw is not "
        f"granted and `app` is. current_user reported {connected_as!r}, so either the role "
        f"name and its grants disagree or this connection is not the one it claims to be."
    )
