"""The ledger's UNAUTHENTICATED surface, graded on what one anonymous request can cost.

Two defects, both on ``apps/trust/src/events/**``, both reachable by anyone who can open the
port (there is not one ``Depends`` in ``apps/trust``):

``T-364`` (CRITICAL)
    ``GET /events/verify`` and ``GET /events/replay`` answered by calling
    ``read_events(connection)`` with **no limit**, so the whole append-only history was
    materialised as Python dicts to produce a 664-byte answer. Measured on a 77 MB / 515-row
    ledger: 8 bytes in, 525.1 MiB peak. ``apps/trust/compose.yaml`` sets ``mem_limit: 256m``,
    so one anonymous GET peaked at twice the container -- and since the ledger only grows,
    the service could not be restarted back into health.

``T-366`` (HIGH)
    ``POST /events`` had **no request-size cap of any kind**. ``payload`` is
    ``dict[str, Any]``, checked only for being a ``Mapping``, and persisted verbatim as
    ``jsonb`` into a table whose ``BEFORE UPDATE OR DELETE ... ENABLE ALWAYS`` trigger makes
    eviction structurally impossible. Measured: single payloads of 1M/10M/50M characters all
    returned ``201``; three of them took the table to 77 MB. The four INDEXED identifier
    columns were bounded only by Postgres' btree limit, which surfaced past ~2,691 characters
    as ``ProgramLimitExceeded`` -- and ``psycopg.errors.ProgramLimitExceeded`` is an
    ``OperationalError``, so ``classify_connection_error`` relabelled a malformed request as
    ``503 store_unavailable``: a 5xx, reachable unauthenticated, blaming the datastore.

**Every test here was written red against the code as it shipped.** The T-364 tests drive a
``PostgresEventStore`` over a *fake* connection that generates ledger rows on demand, so the
cost being measured is what the STORE holds, not what a fixture pre-built -- and so the gate
runs without Postgres. The T-366 tests drive the real router.

Nothing here asserts a number it invented: the caps are imported from the module that
declares them, and the "did it get cheaper" assertions are ratios against the synthetic
ledger's own size.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import tracemalloc
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from apps.trust.src.events import InMemoryEventStore, PostgresEventStore, anchored_report
from apps.trust.src.events.service import create_events_app
from apps.trust.src.ledger import GENESIS_HASH, seal_event
from proxyshop_support.asgi_server import serve

#: A fixed instant. Nothing in this file compares against the wall clock.
AS_OF = "2026-01-01T00:00:00.000Z"
OCCURRED_AT = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

#: The synthetic ledger the T-364 gates read. Big enough that materialising all of it is
#: unmistakable next to materialising a page, small enough to seal in a fraction of a second.
LEDGER_ROWS = 4_000

#: Characters of INCOMPRESSIBLE-shaped filler in each row's payload. The blob is rebuilt on
#: every read rather than shared, because a fixture that handed out one interned string to
#: every row would make "the whole ledger" and "one page" cost the same and the memory
#: assertion below would pass against the unfixed code.
BLOB_CHARS = 512

#: What the payload text of the whole ledger weighs. The peak-memory gates are stated as
#: fractions of this rather than as absolute byte counts, so they grade the *shape* of the
#: read (does the cost track the ledger?) and not this machine's allocator.
LEDGER_PAYLOAD_BYTES = LEDGER_ROWS * BLOB_CHARS


# ======================================================================================
# A synthetic ledger, generated on demand
# ======================================================================================
def _body(seq: int) -> dict[str, Any]:
    """One unsealed ``LedgerEvent``, with a payload blob allocated fresh on every call."""
    return {
        "event_id": f"ev-{seq:06d}",
        "ts": AS_OF,
        "kind": "feedback",
        "store_id": "s-1",
        "payload": {"n": seq, "blob": "x" * BLOB_CHARS + f"{seq:08d}"},
    }


class SyntheticLedger:
    """``LEDGER_ROWS`` sealed events that exist only while a row is being handed out.

    Only the 64-character link digests are retained (``LEDGER_ROWS`` * 2 * 64 bytes); the
    payloads are regenerated per read. That is what makes the memory measurements below
    measure the *reader*.
    """

    def __init__(self, tamper_at: int | None = None) -> None:
        self.tamper_at = tamper_at
        self._links: list[tuple[str, str]] = []
        prev = GENESIS_HASH
        for seq in range(1, LEDGER_ROWS + 1):
            sealed = seal_event(_body(seq), prev)
            digest = str(sealed["event_hash"])
            self._links.append((prev, digest))
            prev = digest
        self.head_hash = prev

    def row(self, seq: int) -> tuple[Any, ...]:
        """One ``ledger.commerce_events`` row, in ``trust.ledger.store._COLUMNS`` order."""
        body = _body(seq)
        payload = body["payload"]
        if seq == self.tamper_at:
            # Content rewritten AFTER sealing: the row still carries its original digest, so
            # this is the `tampered` reason and not `broken_link`.
            payload = {**payload, "n": -1}
        prev, digest = self._links[seq - 1]
        return (
            seq,
            body["event_id"],
            body["kind"],
            None,
            body["store_id"],
            None,
            OCCURRED_AT,
            payload,
            prev,
            digest,
        )

    def window(self, after_seq: int, limit: int | None) -> list[tuple[Any, ...]]:
        last = LEDGER_ROWS if limit is None else min(LEDGER_ROWS, after_seq + limit)
        return [self.row(seq) for seq in range(after_seq + 1, last + 1)]

    def anchor_row(self) -> tuple[Any, ...]:
        return (self.head_hash, LEDGER_ROWS, LEDGER_ROWS, OCCURRED_AT)

    def expected_events(self) -> list[dict[str, Any]]:
        """The same stream as ordinary in-memory dicts, for the equivalence assertions."""
        rows: list[dict[str, Any]] = []
        prev = GENESIS_HASH
        for seq in range(1, LEDGER_ROWS + 1):
            body = _body(seq)
            if seq == self.tamper_at:
                body["payload"] = {**body["payload"], "n": -1}
            sealed = seal_event(body, prev)
            prev_stored, digest = self._links[seq - 1]
            sealed["prev_hash"] = prev_stored
            sealed["event_hash"] = digest
            sealed["seq"] = seq
            rows.append(sealed)
            prev = digest
        return rows

    def expected_anchor(self) -> dict[str, Any]:
        head, length, last_seq, updated = self.anchor_row()
        return {
            "head_hash": head,
            "length": length,
            "last_seq": last_seq,
            "updated_at": updated,
        }


class _FakeCursor:
    """Serves ``read_events`` / ``chain_anchor`` / ``chain_tail`` out of a SyntheticLedger.

    Records ``(limit_asked_for, rows_handed_back)`` for every row-returning statement, which
    is what the "never asks for an unbounded read" gate reads.
    """

    def __init__(self, ledger: SyntheticLedger, log: list[tuple[int | None, int]]) -> None:
        self._ledger = ledger
        self._log = log
        self._kind = ""
        self._after = 0
        self._limit: int | None = None
        self._descending = False

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False

    def execute(self, query: Any, params: Any = None, **_: Any) -> _FakeCursor:
        sql = " ".join(str(query).split())
        if "ledger.chain_head" in sql:
            self._kind = "anchor"
            return self
        self._kind = "events"
        self._descending = "order by seq desc" in sql
        values = list(params or [])
        self._after = int(values[0]) if values else 0
        self._limit = int(values[-1]) if sql.endswith("limit %s") else None
        if self._descending:
            self._limit = 1
        return self

    def _rows(self) -> list[tuple[Any, ...]]:
        if self._descending:
            rows = [self._ledger.row(LEDGER_ROWS)]
        else:
            rows = self._ledger.window(self._after, self._limit)
        self._log.append((self._limit, len(rows)))
        return rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows()

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._kind == "anchor":
            return self._ledger.anchor_row()
        rows = self._rows()
        return rows[0] if rows else None


class _FakeConnection:
    def __init__(self, ledger: SyntheticLedger, log: list[tuple[int | None, int]]) -> None:
        self._ledger = ledger
        self._log = log

    def cursor(self, *_: Any, **__: Any) -> _FakeCursor:
        return _FakeCursor(self._ledger, self._log)

    def close(self) -> None:
        return None


def _store_over(ledger: SyntheticLedger) -> tuple[PostgresEventStore, list[tuple[int | None, int]]]:
    """A real ``PostgresEventStore`` whose connections come from the synthetic ledger."""
    log: list[tuple[int | None, int]] = []
    return PostgresEventStore(connect=lambda: _FakeConnection(ledger, log)), log


@pytest.fixture(scope="module")
def intact_ledger() -> SyntheticLedger:
    return SyntheticLedger()


@pytest.fixture(scope="module")
def tampered_ledger() -> SyntheticLedger:
    return SyntheticLedger(tamper_at=LEDGER_ROWS // 2)


# ======================================================================================
# T-364 -- one anonymous GET must not cost the whole ledger
# ======================================================================================
def test_verify_never_asks_the_ledger_for_an_unbounded_read(
    intact_ledger: SyntheticLedger,
) -> None:
    """``GET /events/verify``'s read must be paged, not ``read_events(connection)``.

    Red first: ``PostgresEventStore._read_with_anchor`` issued exactly one statement with no
    ``limit`` at all and turned every row in the ledger into a Python dict. The statement log
    below shows it as a single ``(None, 4000)`` entry.
    """
    store, log = _store_over(intact_ledger)

    report = store.verify()

    assert report["ok"] is True, report["detail"]
    reads = [(limit, rows) for limit, rows in log if rows > 0]
    assert reads, "verification issued no row-returning statement at all"
    assert all(limit is not None for limit, _ in reads), (
        f"verification asked the ledger for an UNBOUNDED read: {log}. An unauthenticated "
        f"caller decides how many rows that is, and the ledger only grows."
    )
    assert max(rows for _, rows in reads) <= LEDGER_ROWS // 4, (
        f"one statement handed back {max(rows for _, rows in reads)} of {LEDGER_ROWS} rows; "
        f"a whole-chain read has to arrive in pages or the peak tracks the ledger"
    )


def test_verify_peak_memory_does_not_track_the_size_of_the_ledger(
    intact_ledger: SyntheticLedger,
) -> None:
    """The amplifier itself: 8 bytes in, a peak proportional to everything ever written.

    Red first, measured by ``tracemalloc`` rather than argued: verifying this 2 MB synthetic
    ledger peaked at more than the ledger, to return a report of a few hundred bytes.
    """
    store, _ = _store_over(intact_ledger)

    tracemalloc.start()
    try:
        report = store.verify()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert report["ok"] is True, report["detail"]
    assert report["length"] == LEDGER_ROWS
    assert peak < LEDGER_PAYLOAD_BYTES // 4, (
        f"verifying a {LEDGER_PAYLOAD_BYTES:,}-byte ledger peaked at {peak:,} bytes. An "
        f"unauthenticated GET must cost a page, not the history."
    )


def test_replay_peak_memory_does_not_track_the_size_of_the_ledger(
    intact_ledger: SyntheticLedger,
) -> None:
    """``GET /events/replay`` had the same read behind it, plus it KEPT every row.

    ``limit`` bounded what replay serialised and never what it loaded -- its own docstring
    said so. Both halves have to be bounded.
    """
    store, log = _store_over(intact_ledger)

    tracemalloc.start()
    try:
        report = store.replay(after_seq=0, limit=50)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert report["ok"] is True, report["detail"]
    assert report["length"] == LEDGER_ROWS, "replay must still verify the WHOLE ledger"
    assert len(report["events"]) == 50, "replay must serialise the page it was asked for"
    assert report["events_truncated"] is True
    assert all(limit is not None for limit, rows in log if rows > 0), (
        f"replay asked the ledger for an unbounded read: {log}"
    )
    assert peak < LEDGER_PAYLOAD_BYTES // 4, (
        f"replaying a {LEDGER_PAYLOAD_BYTES:,}-byte ledger peaked at {peak:,} bytes"
    )


def test_the_paged_verifier_reports_exactly_what_the_whole_stream_verifier_did(
    intact_ledger: SyntheticLedger,
) -> None:
    """Cheaper is only acceptable if the answer is the same answer.

    ``anchored_report`` over the fully materialised stream is the reference implementation.
    The streamed report must equal it key for key -- ``ok``, ``length``, ``stream_hash``,
    ``head_hash``, ``anchor_ok`` and the human-readable ``detail`` included.
    """
    store, _ = _store_over(intact_ledger)

    streamed = store.verify()
    reference = anchored_report(intact_ledger.expected_events(), intact_ledger.expected_anchor())

    assert streamed == reference


def test_a_tampered_row_is_still_named_by_the_paged_verifier(
    tampered_ledger: SyntheticLedger,
) -> None:
    """The break must survive the refactor: same reason, same event, same sentence.

    A verifier that got cheap by forgetting how to point at the broken link would be worse
    than the unbounded one -- the endpoint exists to produce evidence.
    """
    store, log = _store_over(tampered_ledger)

    streamed = store.verify()
    reference = anchored_report(
        tampered_ledger.expected_events(), tampered_ledger.expected_anchor()
    )

    assert streamed["ok"] is False
    assert streamed["reason"] == "tampered"
    assert streamed["broken_event"] is not None
    assert streamed["broken_event"]["event_id"] == f"ev-{LEDGER_ROWS // 2:06d}"
    assert streamed == reference
    assert all(limit is not None for limit, rows in log if rows > 0), (
        f"the broken-chain path fell back to an unbounded read: {log}"
    )


def test_the_head_costs_one_row_however_long_the_ledger_is(
    intact_ledger: SyntheticLedger,
) -> None:
    """``GET /events/head`` returns 93 bytes and was measured reading 143 MiB.

    Not because it scans -- ``chain_tail`` is ``order by seq desc limit 1`` and always was --
    but because ONE row could be 50 MB of ``payload`` when nothing capped what a row may
    hold. The row count is what this file can pin; the byte count is pinned by
    ``MAX_EVENT_BODY_BYTES`` (T-366), and the two together are the bound.
    """
    store, log = _store_over(intact_ledger)

    assert store.head_hash == intact_ledger.head_hash
    assert store.length == LEDGER_ROWS
    reads = [(limit, rows) for limit, rows in log if rows > 0]
    assert reads and max(rows for _, rows in reads) == 1, (
        f"reading the head touched {max(rows for _, rows in reads)} rows: {log}"
    )


def test_no_route_on_this_router_issues_an_unbounded_read(
    intact_ledger: SyntheticLedger,
) -> None:
    """The catch-all: drive every GET the router publishes and audit the statement log.

    A per-endpoint gate stops covering the endpoint somebody adds next. This one grades the
    surface -- if any route ever answers by asking the ledger for all of it again, it fails
    here whether or not anyone wrote a test for that route.
    """
    from apps.trust.src.events.store import LEDGER_SCAN_CHUNK

    store, log = _store_over(intact_ledger)
    paths = [
        "/events",
        "/events?limit=10000",
        "/events/head",
        "/events/verify",
        "/events/replay",
        "/events/replay?include_events=false",
        "/events/ev-000001",
    ]

    answered: dict[str, int] = {}
    with _client_for(store) as client:
        for path in paths:
            del log[:]
            response = client.get(path)
            answered[path] = response.status_code
            assert response.status_code == 200, f"{path} -> {response.text}"
            reads = [(limit, rows) for limit, rows in log if rows > 0]
            assert all(limit is not None for limit, _ in reads), (
                f"{path} issued an UNBOUNDED read: {log}"
            )
            assert all(rows <= LEDGER_SCAN_CHUNK for _, rows in reads), (
                f"{path} pulled {max(rows for _, rows in reads)} rows in one statement, "
                f"past the {LEDGER_SCAN_CHUNK}-row window: {log}"
            )

    assert set(answered) == set(paths)


def test_the_ledger_scan_chunk_is_a_published_constant() -> None:
    """The bound is a named number with a docstring, not whatever the loop happens to do."""
    from apps.trust.src.events.store import LEDGER_SCAN_CHUNK, MAX_RESPONSE_EVENT_BYTES

    assert 1 <= LEDGER_SCAN_CHUNK <= 1_000
    assert 0 < MAX_RESPONSE_EVENT_BYTES <= 16 * 1024 * 1024


def test_a_page_of_events_is_bounded_in_bytes_and_not_only_in_rows() -> None:
    """``GET /events?limit=10000`` is a row bound that is not a byte bound.

    ``MAX_EVENT_PAGE`` caps rows at ten thousand. With ``MAX_EVENT_BODY_BYTES`` of accepted
    payload behind each row that is still two thirds of a gigabyte in one response, so the
    page has to stop on bytes as well -- and say ``truncated`` when it does, because a short
    page that reports ``truncated: false`` is the exact lie ``GET /events`` was fixed to
    stop telling.
    """
    from apps.trust.src.events.store import MAX_RESPONSE_EVENT_BYTES

    store = InMemoryEventStore()
    blob = "y" * 64_000
    rows = MAX_RESPONSE_EVENT_BYTES // 64_000 + 5
    for index in range(rows):
        store.append(
            {
                "event_id": f"big-{index:04d}",
                "ts": AS_OF,
                "kind": "feedback",
                "payload": {"blob": blob + f"{index:06d}"},
            }
        )

    with _client_for(store) as client:
        body = client.get("/events", params={"limit": 10_000}).json()

    assert body["count"] < rows, (
        f"a single page serialised all {body['count']} rows -- {body['count'] * 64_000:,} "
        f"bytes -- because only the ROW count was capped"
    )
    assert body["truncated"] is True, "a page cut short by the byte budget must say so"
    assert body["next_after_seq"] == body["events"][-1]["seq"]


# ======================================================================================
# T-366 -- POST /events must refuse an oversized body, cleanly
# ======================================================================================
def _client_for(store: Any) -> Any:
    """A real HTTP client against a real loopback server serving ``store``."""
    import contextlib

    @contextlib.contextmanager
    def _open() -> Iterator[httpx.Client]:
        with (
            serve(create_events_app(store)) as base_url,
            httpx.Client(base_url=base_url, timeout=60.0) as client,
        ):
            yield client

    return _open()


def _event(event_id: str = "ev-1", **extra: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": event_id,
        "ts": AS_OF,
        "kind": "feedback",
        "store_id": "s-1",
        "payload": {"n": 1},
    }
    event.update(extra)
    return event


def test_the_body_cap_and_the_identifier_ceiling_are_published_constants() -> None:
    from apps.trust.src.events.routes import MAX_EVENT_BODY_BYTES, MAX_IDENTIFIER_LENGTH

    assert 0 < MAX_EVENT_BODY_BYTES <= 1024 * 1024
    assert 0 < MAX_IDENTIFIER_LENGTH <= 2_000


def test_a_body_over_the_cap_is_refused_and_never_reaches_the_ledger() -> None:
    """Red first: a 50-million-character payload returned ``201`` and was stored forever.

    The table's ``BEFORE UPDATE OR DELETE ... ENABLE ALWAYS`` trigger means there is no
    eviction to fall back on, so the only place this can be stopped is the door.
    """
    from apps.trust.src.events.routes import MAX_EVENT_BODY_BYTES

    store = InMemoryEventStore()
    oversized = _event(payload={"blob": "z" * (MAX_EVENT_BODY_BYTES * 2)})

    with _client_for(store) as client:
        response = client.post("/events", json=oversized)

    assert 400 <= response.status_code < 500, (
        f"an oversized body answered {response.status_code}; a refusal on an unauthenticated "
        f"door must be a clean 4xx"
    )
    assert store.length == 0, "the oversized event was appended to an append-only ledger"


def test_the_oversized_refusal_does_not_echo_the_body_back() -> None:
    """A refusal that quotes the input is an amplifier with better manners."""
    from apps.trust.src.events.routes import MAX_EVENT_BODY_BYTES

    store = InMemoryEventStore()
    marker = "z" * (MAX_EVENT_BODY_BYTES * 2)

    with _client_for(store) as client:
        response = client.post("/events", json=_event(payload={"blob": marker}))

    assert len(response.content) < 4_096, f"the refusal was {len(response.content):,} bytes long"
    assert "zzzzzzzzzz" not in response.text, "the refusal echoed the body back"


def test_a_body_exactly_at_the_cap_is_still_accepted() -> None:
    """A cap that refuses the largest legal request is a smaller cap than it claims."""
    from apps.trust.src.events.routes import MAX_EVENT_BODY_BYTES

    store = InMemoryEventStore()
    skeleton = json.dumps(_event(payload={"blob": ""})).encode("utf-8")
    blob = "b" * (MAX_EVENT_BODY_BYTES - len(skeleton))
    body = json.dumps(_event(payload={"blob": blob})).encode("utf-8")
    assert len(body) == MAX_EVENT_BODY_BYTES

    with _client_for(store) as client:
        response = client.post(
            "/events", content=body, headers={"content-type": "application/json"}
        )

    assert response.status_code == 201, response.text
    assert store.length == 1


def test_an_oversized_identifier_is_a_clean_422_that_names_the_field() -> None:
    """Red first: a 30,000-character ``event_id`` was accepted by the service outright.

    Against Postgres the same event reached an INDEXED ``text`` column, blew the btree entry
    limit, and came back as ``503 store_unavailable`` -- an unauthenticated caller turning a
    malformed request into an accusation against the datastore.
    """
    store = InMemoryEventStore()

    with _client_for(store) as client:
        response = client.post("/events", json=_event(event_id="e" * 30_000))

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "identifier_too_long"
    assert response.json()["detail"]["field"] == "event_id"
    assert len(response.content) < 4_096, "the refusal echoed the identifier back"
    assert "eeeeeeeeee" not in response.text
    assert store.length == 0


@pytest.mark.parametrize("field", ["event_id", "auction_id", "store_id", "order_ref"])
def test_every_caller_chosen_identifier_is_bounded(field: str) -> None:
    """All four are indexed and all four are interpolated into refusals. Bound all four."""
    from apps.trust.src.events.routes import MAX_IDENTIFIER_LENGTH

    store = InMemoryEventStore()
    with _client_for(store) as client:
        over = client.post("/events", json=_event(**{field: "q" * (MAX_IDENTIFIER_LENGTH + 1)}))
        at_the_ceiling = client.post("/events", json=_event(**{field: "q" * MAX_IDENTIFIER_LENGTH}))

    assert over.status_code == 422, over.text
    assert over.json()["detail"]["field"] == field
    assert at_the_ceiling.status_code == 201, (
        f"an identifier of exactly MAX_IDENTIFIER_LENGTH ({MAX_IDENTIFIER_LENGTH}) was "
        f"refused: {at_the_ceiling.text}"
    )


def _asgi_exchange(app: Any, chunks: list[bytes], *, disconnect: bool) -> list[dict[str, Any]]:
    """Drive one POST /events through the ASGI app with a hand-built receive channel."""
    sent: list[dict[str, Any]] = []

    async def run() -> None:
        pending = list(chunks)

        async def receive() -> dict[str, Any]:
            if pending:
                chunk = pending.pop(0)
                return {"type": "http.request", "body": chunk, "more_body": True}
            if disconnect:
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/events",
                "raw_path": b"/events",
                "query_string": b"",
                "root_path": "",
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                ],
                "client": ("127.0.0.1", 5000),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )

    asyncio.run(run())
    return sent


def test_an_oversized_body_is_refused_WHILE_ARRIVING_and_not_after() -> None:
    """The distinction that decides whether the cap bounds the refusal or the memory.

    ``await request.body()`` followed by a length check answers ``413`` for a 64 MB upload
    *after* pulling all 64 MB into the process, which costs the anonymous caller nothing and
    the service everything. Counting the chunks the app actually consumed is the only way to
    tell the two implementations apart from the outside -- they return the same status code.
    """
    from apps.trust.src.events.routes import MAX_EVENT_BODY_BYTES

    chunk = b"z" * 65_536
    chunks = [b'{"event_id": "ev-1", "payload": {"blob": "'] + [chunk] * 1_000

    store = InMemoryEventStore()
    consumed: list[int] = []
    app = create_events_app(store)

    sent: list[dict[str, Any]] = []

    async def run() -> None:
        pending = list(chunks)

        async def receive() -> dict[str, Any]:
            if pending:
                body = pending.pop(0)
                consumed.append(len(body))
                return {"type": "http.request", "body": body, "more_body": True}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/events",
                "raw_path": b"/events",
                "query_string": b"",
                "root_path": "",
                "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
                "client": ("127.0.0.1", 5000),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )

    asyncio.run(run())

    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 413, starts
    assert sum(consumed) < MAX_EVENT_BODY_BYTES + 2 * len(chunk), (
        f"the app read {sum(consumed):,} bytes of a {sum(len(c) for c in chunks):,}-byte "
        f"upload before refusing it -- the cap bounded the REFUSAL, not the memory"
    )
    assert store.length == 0


def test_a_caller_that_hangs_up_mid_body_is_not_a_5xx() -> None:
    """Hanging up mid-upload is a fact of the internet, not a server fault."""
    store = InMemoryEventStore()
    app = create_events_app(store)

    sent = _asgi_exchange(app, [b'{"event_id": "ev-1",'], disconnect=True)

    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert starts, "the app produced no response at all for a client that disconnected"
    assert 400 <= starts[0]["status"] < 500, f"a disconnected upload answered {starts[0]['status']}"
    assert store.length == 0


def test_a_deeply_nested_payload_inside_the_cap_is_not_a_5xx() -> None:
    """``RecursionError`` is a ``RuntimeError``, so it is not caught by a ``ValueError`` clause."""
    from apps.trust.src.events.routes import MAX_EVENT_BODY_BYTES

    depth = MAX_EVENT_BODY_BYTES // 4
    nested = b'{"event_id": "ev-1", "ts": "' + AS_OF.encode() + b'", "kind": "feedback", '
    nested += b'"payload": {"deep": ' + b"[" * depth + b"]" * depth + b"}}"
    assert len(nested) <= MAX_EVENT_BODY_BYTES

    store = InMemoryEventStore()
    with _client_for(store) as client:
        response = client.post(
            "/events", content=nested, headers={"content-type": "application/json"}
        )

    assert 400 <= response.status_code < 500, (
        f"a deeply nested body answered {response.status_code}"
    )
    assert store.length == 0


@pytest.mark.docker
def test_an_oversized_identifier_never_reaches_postgres(
    events_client: Any, ledger_clean: Any
) -> None:
    """The measured 503: ``ProgramLimitExceeded`` is an ``OperationalError``.

    ``classify_connection_error`` therefore reported a malformed request as "the ledger
    writer could not reach its datastore", which is both a 5xx and a false accusation.
    """
    response = events_client.post("/events", json=_event(event_id="e" * 30_000))

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "identifier_too_long"

    with ledger_clean.cursor() as cur:
        cur.execute("select count(*) from ledger.commerce_events")
        assert cur.fetchone()[0] == 0
