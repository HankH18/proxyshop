"""T-060 -- every event lands once, chained, and replays exactly.

The three claims in the ticket's title are adversarial, not merely functional, and this file
tries to break each one before it asserts it:

**Lands once.** A sequential duplicate is easy. The test that matters fires twenty-four
*simultaneous* POSTs of one ``event_id`` at a real server over a real socket against real
Postgres and counts the rows. Two further tests establish that the guarantee is the
**database's** and not this process's: a raw duplicate INSERT is refused by
``commerce_events_idempotency_key_key``, and a *second, independent* store object -- sharing
no memory with the first -- still reports the duplicate as a no-op.

**Chained.** A chain that never rejects a tampered ledger is decoration. So a row is
mutated in place *in Postgres* (append-only trigger disabled for the statement, exactly as
somebody with database access would do it) and the verifier is required to fail, at the
right index, naming the event. The corresponding truncation test shows the link check alone
reporting the mutilated ledger as flawless, which is why the anchor exists.

**Replays exactly.** The fixture stream's hash is recorded on disk, so the replay is graded
against a frozen value rather than against itself. It is then recomputed in a **separate
process** that has nothing but the DSN -- no store object, no server, no surviving memory --
and required to produce the same hash and the same event order.

Every DB test is ``@pytest.mark.docker`` and runs against this worker's own database
(``PROXYSHOP_WORKER``); nothing here creates a container, a volume or a second stack.
"""

from __future__ import annotations

import contextlib
import copy
import json
import pathlib
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest

from apps.trust.src.events import (
    LEDGER_EVENT_KINDS,
    BrokenChain,
    IdempotencyConflict,
    InMemoryEventStore,
    InvalidEvent,
    PostgresEventStore,
    UnknownEventKind,
    append,
    normalise_event,
    verify_stream,
)
from apps.trust.src.events.errors import ChainForked
from apps.trust.src.events.pg import classify_write_error
from apps.trust.src.ledger import (
    GENESIS_HASH,
    canonical_event,
    chain_events,
    chain_head,
    stream_hash,
    verify_chain,
    verify_chain_in_db,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATION = REPO_ROOT / "db" / "migrations" / "0002_ledger_tables.sql"

#: A fixed instant. Nothing in this file compares against the wall clock.
AS_OF = "2026-01-01T00:00:00.000Z"

#: How many threads the concurrency proofs use. Comfortably above the writer's pool size,
#: so some requests genuinely queue for a connection rather than all being served instantly.
RACERS = 24


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def _event(
    event_id: str,
    kind: str = "claim_verified",
    *,
    ts: str = AS_OF,
    store_id: str | None = "s-1",
    payload: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A plain ``LedgerEvent`` body, ready to POST."""
    event: dict[str, Any] = {
        "event_id": event_id,
        "ts": ts,
        "kind": kind,
        "payload": payload if payload is not None else {"n": 1},
    }
    if store_id is not None:
        event["store_id"] = store_id
    event.update(extra)
    return event


def _obs_event(event_id: str, store_id: str, dim: str, otype: str) -> dict[str, Any]:
    """A ledger event whose payload carries one trust observation."""
    return _event(
        event_id,
        "claim_verified",
        store_id=store_id,
        payload={"dim": dim, "type": otype, "observed_at": AS_OF},
    )


def _post(client: Any, event: dict[str, Any], **kwargs: Any) -> Any:
    return client.post("/events", json=event, **kwargs)


@contextlib.contextmanager
def _trigger_disabled(admin: Any, trigger: str) -> Iterator[None]:
    """Switch one ledger integrity trigger off, and put it back **exactly** as it was.

    Back with ``ENABLE ALWAYS``, never the plain spelling. The migration installs these
    triggers ``ENABLE ALWAYS`` (``pg_trigger.tgenabled = 'A'``); a plain
    ``ALTER TABLE ... ENABLE`` restores them to ``'O'``, and at ``'O'`` a single
    ``SET session_replication_role = 'replica'`` skips them entirely -- so a *tamper* test
    would have quietly disarmed the ledger's tamper defences for every test that ran after
    it, in the same session. That is not hypothetical: written the plain way, this helper
    broke three of T-011's trigger-integrity tests, which is how it was caught. The
    assertion below is this file's own copy of that guard.
    """
    with admin.cursor() as cur:
        cur.execute(f"alter table ledger.commerce_events disable trigger {trigger}")
        try:
            yield
        finally:
            cur.execute(f"alter table ledger.commerce_events enable always trigger {trigger}")
            cur.execute(
                "select tgenabled from pg_trigger where tgname = %s "
                "and tgrelid = 'ledger.commerce_events'::regclass",
                (trigger,),
            )
            assert cur.fetchone()[0] == "A", (
                f"{trigger} was left weaker than the migration installs it"
            )


def _tamper_in_postgres(admin: Any, seq: int, patch_sql: str, params: tuple[Any, ...]) -> None:
    """Rewrite a committed ledger row, the way somebody with database access would.

    ``ledger.commerce_events`` carries a ``BEFORE UPDATE OR DELETE`` trigger that refuses
    both outright, so the *only* way to mutate a row is to switch that trigger off first.
    Doing exactly that is the point: the question this ticket has to answer is not "can the
    schema stop tampering" (it can stop the casual kind) but "when a row is changed anyway,
    does verification notice, and does it say which row".
    """
    with _trigger_disabled(admin, "commerce_events_append_only_trigger"), admin.cursor() as cur:
        cur.execute(patch_sql, (*params, seq))


def _race(work: Any, count: int = RACERS) -> list[Any]:
    """Run ``work(index)`` on ``count`` threads that all start at the same instant.

    The barrier is what makes this a race rather than ``count`` sequential calls with extra
    steps: every thread is parked until the last one arrives, so the requests leave together.
    """
    barrier = threading.Barrier(count, timeout=60)

    def run(index: int) -> Any:
        barrier.wait()
        return work(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(run, index) for index in range(count)]
        return [future.result(timeout=180) for future in futures]


# ======================================================================================
# 1. The shape guards -- and what each one, and only it, rejects
# ======================================================================================
def test_normalisation_is_the_ledger_canonicaliser_and_not_a_second_one() -> None:
    """D16: the writer must not define hashing. It normalises by calling, not by copying."""
    raw = _event(
        "ev-norm",
        "accepted",
        ts="2026-01-01T00:00:00.123456+00:00",
        store_id=None,
        payload={"a": 1},
        order_ref="o-1",
        prev_hash="f" * 64,
        event_hash="e" * 64,
        seq=99,
    )
    body = normalise_event(raw)

    assert body == canonical_event(raw), "normalise_event diverged from the ledger's rule"
    assert body["ts"] == "2026-01-01T00:00:00.123Z", "sub-millisecond digits must truncate"
    assert "store_id" not in body, "a None-valued optional field must be omitted, not stored"
    assert {"prev_hash", "event_hash", "seq"}.isdisjoint(body), (
        "the chain stamps its own fields; an event cannot commit to its own digest"
    )


@pytest.mark.parametrize("kind", sorted(LEDGER_EVENT_KINDS))
def test_every_kind_in_the_frozen_vocabulary_is_accepted(kind: str) -> None:
    """The other half of the kind guard: it must admit all eighteen, not merely reject."""
    assert normalise_event(_event("ev-kind", kind))["kind"] == kind


@pytest.mark.parametrize(
    "kind",
    [
        "totally_made_up",
        "Accepted",  # the vocabulary is lower-case; case is not a spelling variant
        "accepted ",  # trailing space
        "order-paid",  # hyphen for underscore
        "",
        "bid_placed; drop table ledger.commerce_events",
    ],
)
def test_a_kind_outside_the_frozen_vocabulary_is_refused(kind: str) -> None:
    """What only this guard rejects: a `kind` that is not one of the eighteen."""
    with pytest.raises(UnknownEventKind) as caught:
        normalise_event(_event("ev-bad-kind", kind))
    assert "frozen" in str(caught.value)


def test_the_writers_kind_vocabulary_is_the_databases_kind_vocabulary() -> None:
    """The Python copy of the vocabulary must equal the SQL CHECK it duplicates.

    Two lists of the same eighteen strings drift the day somebody extends one of them. This
    turns that drift into a failing test rather than a service that accepts a kind the
    database refuses (a 500) or refuses one the database accepts (a lost event).
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    match = re.search(r"commerce_events_kind_check CHECK \(kind IN \((.*?)\)\s*\)", sql, re.S)
    assert match is not None, "commerce_events_kind_check is no longer where this test looks"
    from_sql = set(re.findall(r"'([a-z_]+)'", match.group(1)))
    assert from_sql, "parsed no kinds out of the CHECK constraint"
    assert from_sql == set(LEDGER_EVENT_KINDS), (
        f"the writer and the database disagree about the LedgerEvent vocabulary; "
        f"only in SQL: {sorted(from_sql - LEDGER_EVENT_KINDS)}, "
        f"only in Python: {sorted(set(LEDGER_EVENT_KINDS) - from_sql)}"
    )


def test_an_extra_top_level_field_is_named_rather_than_blamed_on_jsonb() -> None:
    """What only this guard rejects: a top-level field outside the LedgerEvent shape.

    Such a field IS hashed and has no column to be stored in, so without this the append
    reaches Postgres, comes back missing the field, fails the writer's own round-trip check
    and is reported as a jsonb number-renormalisation problem -- which it is not.
    """
    ok = _event("ev-clean")
    assert normalise_event(ok)["event_id"] == "ev-clean"

    with pytest.raises(InvalidEvent) as caught:
        normalise_event({**ok, "priority": "high"})
    assert "priority" in str(caught.value)


@pytest.mark.parametrize(
    "ts",
    [
        "not-a-time",
        "",
        "1767225600",
        "2026-01-01",
        # These four are the reason this guard catches `ValueError` and not only
        # `CanonicalisationError`: each satisfies the RFC-3339 *shape* and then fails inside
        # `datetime()`, which raises a bare `ValueError`. Caught too narrowly, an invalid
        # date reaches the caller as a 500 instead of a refusal.
        "2026-13-01T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-01-01T25:00:00Z",
        "2026-00-10T00:00:00Z",
    ],
)
def test_an_unparseable_timestamp_is_refused(ts: str) -> None:
    """What only this guard rejects: a `ts` with no deterministic canonical form."""
    with pytest.raises(InvalidEvent):
        normalise_event(_event("ev-ts", ts=ts))


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.000Z"),
        ("2026-01-01T00:00:00.5Z", "2026-01-01T00:00:00.500Z"),
        ("2026-01-01T01:00:00+01:00", "2026-01-01T00:00:00.000Z"),
    ],
)
def test_a_parseable_timestamp_is_normalised_and_not_refused(ts: str, expected: str) -> None:
    """The positive control for the timestamp guard: legal spellings all survive."""
    assert normalise_event(_event("ev-ts-ok", ts=ts))["ts"] == expected


@pytest.mark.parametrize("payload", [[], "text", 3, True])
def test_a_payload_that_is_not_a_json_object_is_refused(payload: Any) -> None:
    """``ledger.commerce_events.payload`` is jsonb with a `jsonb_typeof = 'object'` CHECK."""
    with pytest.raises(InvalidEvent):
        normalise_event({**_event("ev-payload"), "payload": payload})


def test_a_null_payload_is_the_empty_object_and_not_a_refusal() -> None:
    """`payload: null` normalises to `{}` -- deliberately, and not by this module's choice.

    ``canonical_event`` omits every optional field that is absent *or* ``None`` and then
    defaults ``payload`` to ``{}``, and the column carries ``DEFAULT '{}'``. So "absent",
    "null" and "empty object" are one event throughout the system. Refusing ``null`` here
    would make the writer disagree with the library about what a request means, and the
    disagreement would surface as a chain that fails verification with no tampering anywhere.
    """
    assert normalise_event({**_event("ev-null"), "payload": None})["payload"] == {}


@pytest.mark.parametrize("event_id", ["", "   "])
def test_an_empty_event_id_is_refused(event_id: str) -> None:
    """An empty idempotency key would make two unrelated events the same event."""
    with pytest.raises(InvalidEvent):
        normalise_event(_event(event_id))


def test_append_names_the_mistake_when_its_arguments_are_swapped() -> None:
    with pytest.raises(TypeError) as caught:
        append(_event("ev-1"), InMemoryEventStore())  # type: ignore[arg-type]
    assert "FIRST argument" in str(caught.value)


# ======================================================================================
# 2. Lands once, and stays chained -- in memory
# ======================================================================================
def test_a_duplicate_event_id_appends_nothing_and_moves_no_hash(
    events_memory: InMemoryEventStore,
) -> None:
    """T-060 acceptance 1, in memory."""
    first = _obs_event("ev-1", "s-1", "price_honored", "verified")
    outcome = append(events_memory, first)
    assert outcome.inserted is True
    head = events_memory.head_hash

    again = append(events_memory, copy.deepcopy(first))
    assert again.inserted is False
    assert again.event["event_hash"] == outcome.event["event_hash"]
    assert len(events_memory.events) == 1
    assert events_memory.head_hash == head

    # ...and the store is not simply refusing everything.
    append(events_memory, _obs_event("ev-2", "s-1", "shipped_on_time", "fulfilled"))
    assert len(events_memory.events) == 2
    assert events_memory.head_hash != head


def test_the_same_id_with_different_content_is_a_conflict_not_a_silent_no_op(
    events_memory: InMemoryEventStore,
) -> None:
    """The failure mode a success-shaped no-op would hide: a write dropped without a trace."""
    append(events_memory, _event("ev-1", payload={"n": 1}))
    head = events_memory.head_hash

    with pytest.raises(IdempotencyConflict) as caught:
        append(events_memory, _event("ev-1", payload={"n": 2}))
    assert "DIFFERENT content" in str(caught.value)
    assert len(events_memory.events) == 1, "the conflicting event must not have landed"
    assert events_memory.head_hash == head


def test_every_appended_event_is_sealed_so_a_verifier_has_something_to_check(
    events_memory: InMemoryEventStore,
) -> None:
    """A store that kept only a running head hash would leave verify_chain nothing to do."""
    for index in range(4):
        append(events_memory, _event(f"ev-{index}"))
    events = list(events_memory.events)

    assert events[0]["prev_hash"] == GENESIS_HASH
    for index, row in enumerate(events):
        assert len(row["event_hash"]) == 64
        assert row["seq"] == index + 1
        if index:
            assert row["prev_hash"] == events[index - 1]["event_hash"]
    assert verify_chain(events)["ok"] is True


def test_mutating_the_events_a_reader_gets_back_does_not_move_the_store(
    events_memory: InMemoryEventStore,
) -> None:
    """The log is append-only in memory too: a reader must not be able to rewrite it."""
    append(events_memory, _event("ev-1"))
    stolen = events_memory.events
    stolen[0]["payload"]["n"] = 999
    stolen[0]["event_hash"] = "f" * 64

    assert events_memory.events[0]["payload"] == {"n": 1}
    assert events_memory.verify()["ok"] is True


def test_tampering_with_a_stored_event_is_detected_and_the_broken_link_is_named() -> None:
    """T-060 acceptance 2, in memory -- and the report must identify the event."""
    store = InMemoryEventStore()
    for index in range(5):
        append(store, _event(f"ev-{index}"))
    events = list(store.events)

    assert verify_stream(events)["ok"] is True

    events[2]["payload"]["n"] = 99
    report = verify_stream(events)

    assert report["ok"] is False
    assert report["reason"] == "tampered"
    assert report["broken_at"] == 2
    assert report["verified"] == 2, "the two events before the break did verify"
    broken = report["broken_event"]
    assert broken["event_id"] == "ev-2"
    assert broken["seq"] == 3
    assert broken["stored_event_hash"] != broken["recomputed_event_hash"]
    assert "ev-2" in report["detail"] and "tampered" in report["detail"]


def test_reordering_two_events_breaks_the_link_and_names_the_predecessor() -> None:
    """A different mutilation with a different diagnosis: the links, not the content."""
    store = InMemoryEventStore()
    for index in range(4):
        append(store, _event(f"ev-{index}"))
    events = list(store.events)
    events[1], events[2] = events[2], events[1]

    report = verify_stream(events)
    assert report["ok"] is False
    assert report["reason"] == "broken_link"
    assert report["broken_at"] == 1
    assert report["broken_event"]["event_id"] == "ev-2"
    assert report["broken_event"]["predecessor_event_id"] == "ev-0"
    assert "ev-0" in report["detail"]


def test_truncation_is_invisible_to_the_links_and_visible_to_the_anchor() -> None:
    """Why the anchor exists: a truncated chain's digest is a perfectly valid chain digest."""
    store = InMemoryEventStore()
    for index in range(6):
        append(store, _event(f"ev-{index}"))

    survivors = list(store.events)[:3]
    assert verify_chain(survivors)["ok"] is True, (
        "a cut chain verifies link-for-link -- this is the blindness the anchor covers"
    )

    store._events = store._events[:3]  # the tail, cut off by something with the object
    report = store.verify()
    assert report["ok"] is False
    assert report["reason"] == "truncated"
    assert report["anchor"]["length"] == 6
    assert report["length"] == 3


def test_a_store_cannot_be_built_on_a_prefix_that_does_not_verify() -> None:
    """Anything appended behind a broken prefix would be anchored to a broken link."""
    sealed = chain_events([normalise_event(_event(f"ev-{i}")) for i in range(3)])
    sealed[1]["payload"] = {"n": 42}

    with pytest.raises(BrokenChain) as caught:
        InMemoryEventStore(sealed)
    assert "ev-1" in str(caught.value)


def test_a_store_continues_an_existing_sealed_prefix() -> None:
    """Hash chain continuation: a new writer links behind the head it inherited."""
    sealed = chain_events([normalise_event(_event(f"ev-{i}")) for i in range(3)])
    store = InMemoryEventStore(sealed)

    assert store.head_hash == chain_head(sealed)
    outcome = append(store, _event("ev-3"))
    assert outcome.event["prev_hash"] == sealed[-1]["event_hash"]
    assert outcome.seq == 4
    assert store.verify()["ok"] is True


def test_concurrent_duplicate_appends_in_memory_land_exactly_once(
    events_memory: InMemoryEventStore,
) -> None:
    """ "A duplicate is a no-op" is a check-then-write, and two threads race it.

    The switch interval is turned down so the interpreter preempts inside that window; with
    the store's lock removed this test appends between two and twenty-four rows.
    """
    event = _obs_event("ev-race", "s-1", "price_honored", "verified")
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        outcomes = _race(lambda _: append(events_memory, copy.deepcopy(event)))
    finally:
        sys.setswitchinterval(previous)

    assert len(events_memory.events) == 1
    assert sum(1 for outcome in outcomes if outcome.inserted) == 1
    assert len({outcome.event["event_hash"] for outcome in outcomes}) == 1
    assert events_memory.verify()["ok"] is True


def test_concurrent_distinct_appends_in_memory_do_not_fork_the_chain(
    events_memory: InMemoryEventStore,
) -> None:
    """Two events must not be able to claim the same predecessor."""
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        _race(lambda index: append(events_memory, _event(f"ev-{index}")))
    finally:
        sys.setswitchinterval(previous)

    events = list(events_memory.events)
    assert len(events) == RACERS
    assert len({row["prev_hash"] for row in events}) == RACERS, "two events shared a predecessor"
    assert len({row["event_hash"] for row in events}) == RACERS
    assert events_memory.verify()["ok"] is True


# ======================================================================================
# 3. The fixture stream and its hash -- acceptance 2 and 3, without a datastore
# ======================================================================================
def test_the_fixture_stream_seals_to_its_recorded_stream_hash(
    events_fixture_stream: dict[str, Any],
) -> None:
    """T-060 acceptance 3: replaying the fixture stream reproduces the event-stream hash.

    The expectation is a value committed to disk, not one this test computes, so a change
    in canonicalisation or sealing turns it red instead of re-baselining itself.
    """
    store = InMemoryEventStore()
    for event in copy.deepcopy(events_fixture_stream["events"]):
        append(store, event)

    assert store.length == events_fixture_stream["length"]
    assert store.stream_hash() == events_fixture_stream["stream_hash"]
    assert store.head_hash == events_fixture_stream["head_hash"]
    assert list(store.events) == events_fixture_stream["sealed"]


def test_the_tampering_fixture_is_detected_at_the_event_it_names(
    events_fixture_stream: dict[str, Any],
) -> None:
    """T-060 acceptance 2, against the recorded tampering fixture."""
    clean = verify_stream(copy.deepcopy(events_fixture_stream["sealed"]))
    assert clean["ok"] is True, clean["detail"]

    expected = events_fixture_stream["tamper"]
    report = verify_stream(copy.deepcopy(events_fixture_stream["tampered"]))

    assert report["ok"] is False
    assert report["reason"] == expected["expected_reason"]
    assert report["broken_at"] == expected["index"]
    assert report["broken_event"]["event_id"] == expected["event_id"]
    assert report["broken_event"]["seq"] == expected["seq"]
    assert expected["event_id"] in report["detail"]


def test_the_stream_hash_commits_to_the_order_of_the_events(
    events_fixture_stream: dict[str, Any],
) -> None:
    """Replay reproduces the stream "exactly" only if order is part of the identity."""
    sealed = copy.deepcopy(events_fixture_stream["sealed"])
    assert stream_hash(sealed) == events_fixture_stream["stream_hash"]

    swapped = copy.deepcopy(sealed)
    swapped[3], swapped[4] = swapped[4], swapped[3]
    assert stream_hash(swapped) != events_fixture_stream["stream_hash"]

    dropped = copy.deepcopy(sealed)
    del dropped[5]
    assert stream_hash(dropped) != events_fixture_stream["stream_hash"]


def test_the_stream_hash_is_recomputed_from_content_and_not_read_off_the_last_row(
    events_fixture_stream: dict[str, Any],
) -> None:
    """The distinction T-011 draws between ``chain_head`` and ``stream_hash``, exercised.

    Rewriting an event while leaving its stored digest alone changes nothing that
    ``chain_head`` can see -- it reads the last row's column. ``stream_hash`` folds over the
    content, so it moves. If the replay endpoint returned the stored head, this forgery
    would replay "exactly".
    """
    forged = copy.deepcopy(events_fixture_stream["sealed"])
    forged[2]["payload"] = {"forged": True}

    assert chain_head(forged) == events_fixture_stream["head_hash"]
    assert stream_hash(forged) != events_fixture_stream["stream_hash"]


# ======================================================================================
# 4. The two spellings of this package are one package
# ======================================================================================
def test_the_two_spellings_share_one_module_object_and_one_exception_class() -> None:
    """``.pkgroot/trust`` makes this package reachable twice; it must not BE two packages.

    Without the binding, ``except IdempotencyConflict`` written against one spelling
    silently fails to catch what the other raises -- and both spellings are live in a full
    run, because ``trust.main`` discovers ``trust.events.routes`` while the frozen
    acceptance suite imports ``apps.trust.src.events``.
    """
    import importlib

    member = importlib.import_module("trust.events")
    rooted = importlib.import_module("apps.trust.src.events")

    assert member.store is rooted.store
    assert member.errors is rooted.errors
    assert member.IdempotencyConflict is rooted.IdempotencyConflict
    assert member.InMemoryEventStore is rooted.InMemoryEventStore
    assert member.__all__ == rooted.__all__

    caught = 0
    for spelling in (member, rooted):
        store = spelling.InMemoryEventStore()
        spelling.append(store, _event("ev-1", payload={"n": 1}))
        for other in (member, rooted):
            try:
                other.append(store, _event("ev-1", payload={"n": 2}))
            except other.IdempotencyConflict:
                caught += 1
    assert caught == 4, "a cross-spelling `except` failed to catch"


def test_importing_the_writer_pulls_in_neither_a_database_driver_nor_a_web_framework() -> None:
    """The frozen suite imports the in-memory store; it must not need psycopg or fastapi."""
    program = (
        "import sys\n"
        "from apps.trust.src.events import InMemoryEventStore, append\n"
        "assert 'psycopg' not in sys.modules, 'importing the writer pulled in psycopg'\n"
        "assert 'fastapi' not in sys.modules, 'importing the writer pulled in fastapi'\n"
        "print('lazy')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={"PYTHONPATH": f"{REPO_ROOT}:{REPO_ROOT / '.pkgroot'}", "PATH": "/usr/bin:/bin"},
        timeout=120,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "lazy"


def test_the_frozen_entrypoint_discovers_and_mounts_the_events_router() -> None:
    """B6(iii): the router is found by ``apps/trust/src/main.py``'s glob, not by an edit."""
    import importlib

    main = importlib.import_module("trust.main")
    app = main.create_app()
    assert "trust.events.routes" in app.state.mounted_routers
    paths = set(app.openapi()["paths"])
    assert {"/events", "/events/verify", "/events/replay", "/events/head"} <= paths


# ======================================================================================
# 5. Over real Postgres: lands once
# ======================================================================================
@pytest.mark.docker
def test_an_event_posted_through_the_service_lands_in_the_ledger_sealed(
    events_client: Any, ledger_clean: Any
) -> None:
    response = _post(events_client, _event("ev-1", "accepted", payload={"checkout": "ck-1"}))
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["inserted"] is True
    assert body["seq"] == 1
    assert body["length"] == 1
    assert body["event"]["prev_hash"] == GENESIS_HASH
    assert body["event"]["event_hash"] == body["head_hash"]
    assert response.headers["Idempotent-Replay"] == "false"

    with ledger_clean.cursor() as cur:
        cur.execute(
            "select idempotency_key, kind, prev_hash, event_hash "
            "from ledger.commerce_events order by seq"
        )
        rows = cur.fetchall()
    assert rows == [("ev-1", "accepted", GENESIS_HASH, body["event"]["event_hash"])]


@pytest.mark.docker
def test_a_duplicate_post_is_a_no_op_over_real_postgres(
    events_client: Any, ledger_clean: Any
) -> None:
    """T-060 acceptance 1, sequentially, end to end."""
    event = _event("ev-dup")
    first = _post(events_client, event)
    assert first.status_code == 201

    second = _post(events_client, copy.deepcopy(event))
    assert second.status_code == 200, second.text
    assert second.json()["inserted"] is False
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json()["event"] == first.json()["event"]
    assert second.json()["head_hash"] == first.json()["head_hash"]

    with ledger_clean.cursor() as cur:
        cur.execute("select count(*) from ledger.commerce_events")
        assert cur.fetchone()[0] == 1


@pytest.mark.docker
def test_concurrent_duplicate_posts_land_exactly_one_row(
    events_client: Any, ledger_clean: Any
) -> None:
    """ "Lands once" under real concurrency: 24 simultaneous POSTs of one id, one row.

    Sequential idempotency is the easy half, and it is not the half that breaks in
    production. These requests leave together from 24 threads (a barrier holds every one of
    them until the last arrives), cross a real loopback socket to a real server, and are
    served on different database connections.
    """
    event = _event("ev-race", store_id="s-race")
    results = _race(lambda _: _post(events_client, copy.deepcopy(event)))

    statuses = [response.status_code for response in results]
    assert statuses.count(201) == 1, f"expected exactly one create, got {statuses}"
    assert statuses.count(200) == RACERS - 1, f"statuses were {statuses}"

    bodies = [response.json() for response in results]
    assert len({body["event"]["event_hash"] for body in bodies}) == 1
    assert len({body["seq"] for body in bodies}) == 1

    with ledger_clean.cursor() as cur:
        cur.execute(
            "select count(*) from ledger.commerce_events where idempotency_key = %s",
            ("ev-race",),
        )
        assert cur.fetchone()[0] == 1, "a duplicate event_id appended a second row"
        cur.execute(
            "select length, head_hash from ledger.chain_head where chain = %s", ("commerce_events",)
        )
        length, head = cur.fetchone()
    assert length == 1
    assert head == bodies[0]["event"]["event_hash"]


@pytest.mark.docker
def test_the_database_and_not_the_writer_refuses_a_duplicate_event_id(
    events_client: Any, ledger_clean: Any
) -> None:
    """The guarantee is a UNIQUE constraint, not an in-process check that two workers dodge."""
    response = _post(events_client, _event("ev-1"))
    assert response.status_code == 201
    stored = response.json()["event"]

    with ledger_clean.cursor() as cur, pytest.raises(psycopg.errors.UniqueViolation) as caught:
        cur.execute(
            "insert into ledger.commerce_events "
            "  (idempotency_key, kind, store_id, occurred_at, payload, prev_hash, event_hash) "
            "values (%s, %s, %s, %s::timestamptz, %s, %s, %s)",
            (
                "ev-1",
                "shown",
                "s-9",
                AS_OF,
                "{}",
                stored["event_hash"],
                "a" * 64,
            ),
        )
    assert caught.value.diag.constraint_name == "commerce_events_idempotency_key_key"


def _insert_raw(admin: Any, **columns: Any) -> None:
    """Insert straight into ``ledger.commerce_events``, bypassing the writer entirely."""
    names = ", ".join(columns)
    holders = ", ".join("%s" for _ in columns)
    with admin.cursor() as cur:
        cur.execute(
            f"insert into ledger.commerce_events ({names}) values ({holders})",  # noqa: S608
            tuple(columns.values()),
        )


@pytest.mark.docker
def test_the_chain_guard_refuses_a_prev_hash_that_is_not_the_tail(
    events_client: Any, ledger_clean: Any
) -> None:
    """A fork is what a hash chain exists to prevent, so the database prevents it.

    And the writer must classify the refusal as *retryable*: the event was fine, only its
    predecessor moved. This is the exception the service used to mishandle -- the guard
    raises ``USING ERRCODE = 'integrity_constraint_violation'``, not the P0001 a bare
    plpgsql ``RAISE`` produces, so an ``except RaiseException`` written for it caught
    nothing and a real fork surfaced as an unhandled 500.
    """
    _post(events_client, _event("ev-1"))
    second = _post(events_client, _event("ev-2"))
    assert second.status_code == 201
    stale_prev = second.json()["event"]["prev_hash"]  # the tail as it was one event ago

    with pytest.raises(psycopg.Error) as caught:
        _insert_raw(
            ledger_clean,
            idempotency_key="ev-fork",
            kind="shown",
            store_id="s-1",
            occurred_at=AS_OF,
            payload="{}",
            prev_hash=stale_prev,
            event_hash="b" * 64,
        )
    assert str(caught.value.sqlstate).startswith("23"), "a fork must be an integrity refusal"

    refusal = classify_write_error(caught.value, "ev-fork")
    assert isinstance(refusal, ChainForked), (
        f"the writer does not recognise a real chain-guard refusal: {type(caught.value)}"
    )


@pytest.mark.docker
def test_the_unique_constraint_refuses_a_fork_even_with_the_chain_guard_disabled(
    events_client: Any, ledger_clean: Any
) -> None:
    """The backstop under the trigger and under the lock: ``commerce_events_prev_hash_key``.

    The trigger normally fires first, so this constraint is unreachable through any writer
    -- which is exactly why it is worth proving it is real. With the guard switched off, a
    writer that skipped every lock STILL cannot branch the chain.
    """
    landed = _post(events_client, _event("ev-1")).json()["event"]

    with (
        _trigger_disabled(ledger_clean, "commerce_events_chain_guard_trigger"),
        pytest.raises(psycopg.errors.UniqueViolation) as caught,
    ):
        _insert_raw(
            ledger_clean,
            idempotency_key="ev-fork",
            kind="shown",
            store_id="s-1",
            occurred_at=AS_OF,
            payload="{}",
            prev_hash=landed["prev_hash"],  # the genesis link, already claimed
            event_hash="c" * 64,
        )

    assert caught.value.diag.constraint_name == "commerce_events_prev_hash_key"
    assert isinstance(classify_write_error(caught.value, "ev-fork"), ChainForked)


@pytest.mark.docker
def test_idempotency_lives_in_the_database_not_in_the_writer_process(
    events_running: Any, events_service: Any, ledger_clean: Any
) -> None:
    """A second writer that has never seen the event still recognises it as a duplicate.

    This is what an in-process "seen ids" set could not do, and the reason the writer keeps
    no such set: two service replicas do not share one.
    """
    first_client, _ = events_running
    event = _event("ev-shared")
    assert _post(first_client, event).status_code == 201

    second_client, second_store = events_service()
    assert second_store is not events_running[1]

    response = _post(second_client, copy.deepcopy(event))
    assert response.status_code == 200, response.text
    assert response.json()["inserted"] is False

    with ledger_clean.cursor() as cur:
        cur.execute("select count(*) from ledger.commerce_events")
        assert cur.fetchone()[0] == 1


@pytest.mark.docker
def test_concurrent_distinct_posts_do_not_fork_the_chain(
    events_client: Any, events_store: PostgresEventStore
) -> None:
    """Every racer lands, each behind a different predecessor, in one unbroken chain."""
    results = _race(lambda index: _post(events_client, _event(f"ev-{index:02d}")))
    assert [response.status_code for response in results] == [201] * RACERS

    events = events_store.read()
    assert len(events) == RACERS
    assert len({row["prev_hash"] for row in events}) == RACERS, "two events shared a predecessor"
    assert events[0]["prev_hash"] == GENESIS_HASH
    for earlier, later in zip(events, events[1:], strict=False):
        assert later["prev_hash"] == earlier["event_hash"]
    assert events_store.verify()["ok"] is True


@pytest.mark.docker
def test_the_same_id_with_different_content_is_a_conflict_over_http(
    events_client: Any, ledger_clean: Any
) -> None:
    assert _post(events_client, _event("ev-1", payload={"n": 1})).status_code == 201

    conflict = _post(events_client, _event("ev-1", payload={"n": 2}))
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["error"] == "idempotency_conflict"

    with ledger_clean.cursor() as cur:
        cur.execute("select payload from ledger.commerce_events")
        assert cur.fetchall() == [({"n": 1},)]


@pytest.mark.docker
def test_an_unknown_kind_is_refused_before_it_reaches_the_database(
    events_client: Any, ledger_clean: Any
) -> None:
    response = _post(events_client, _event("ev-1", "not_a_kind"))
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_event_kind"
    assert sorted(LEDGER_EVENT_KINDS) == detail["kinds"]

    with ledger_clean.cursor() as cur:
        cur.execute("select count(*) from ledger.commerce_events")
        assert cur.fetchone()[0] == 0


@pytest.mark.docker
def test_an_idempotency_key_header_that_disagrees_with_the_body_is_refused(
    events_client: Any,
) -> None:
    """What only this guard rejects: a header and a body that name different events."""
    event = _event("ev-1")

    agreeing = _post(events_client, event, headers={"Idempotency-Key": "ev-1"})
    assert agreeing.status_code == 201, agreeing.text

    disagreeing = _post(events_client, _event("ev-2"), headers={"Idempotency-Key": "ev-1"})
    assert disagreeing.status_code == 422, disagreeing.text
    assert disagreeing.json()["detail"]["error"] == "idempotency_key_mismatch"


# ======================================================================================
# 6. Over real Postgres: chained, and tampering is detectable
# ======================================================================================
@pytest.mark.docker
def test_tampering_with_a_row_in_postgres_is_detected_and_names_the_broken_link(
    events_client: Any, events_store: PostgresEventStore, ledger_clean: Any
) -> None:
    """T-060 acceptance 2, against a mutated **row**, not a mutated copy in memory."""
    for index in range(5):
        assert _post(events_client, _event(f"ev-{index}", payload={"n": index})).status_code == 201

    before = events_store.verify()
    assert before["ok"] is True, before["detail"]

    _tamper_in_postgres(
        ledger_clean,
        seq=3,
        patch_sql="update ledger.commerce_events set payload = %s::jsonb where seq = %s",
        params=('{"n": 999}',),
    )

    report = events_store.verify()
    assert report["ok"] is False, "a mutated row passed chain verification"
    assert report["reason"] == "tampered"
    assert report["broken_at"] == 2
    assert report["verified"] == 2
    broken = report["broken_event"]
    assert broken["event_id"] == "ev-2"
    assert broken["seq"] == 3
    assert broken["stored_event_hash"] != broken["recomputed_event_hash"]
    assert "ev-2" in report["detail"]

    over_http = events_client.get("/events/verify")
    assert over_http.status_code == 200, "a broken ledger is an answer, not a server error"
    assert over_http.json()["broken_event"]["event_id"] == "ev-2"


@pytest.mark.docker
def test_the_services_verdict_agrees_with_the_ledger_librarys_own_verifier(
    events_client: Any, events_store: PostgresEventStore, ledger_clean: Any
) -> None:
    """This module must not grow a second opinion about integrity (D16).

    ``verify_chain_in_db`` is T-011's composition of links plus anchor; the service's report
    adds names to a break and nothing else, so the two must agree on both a clean and a
    tampered ledger.
    """
    for index in range(4):
        _post(events_client, _event(f"ev-{index}"))

    library = verify_chain_in_db(ledger_clean)
    service = events_store.verify()
    assert (library["ok"], library["reason"]) == (service["ok"], service["reason"])
    assert library["recomputed"] == service["stream_hash"]

    _tamper_in_postgres(
        ledger_clean,
        seq=2,
        patch_sql="update ledger.commerce_events set store_id = %s where seq = %s",
        params=("s-forged",),
    )

    library = verify_chain_in_db(ledger_clean)
    service = events_store.verify()
    assert (library["ok"], library["reason"]) == (service["ok"], service["reason"])
    assert service["ok"] is False and service["reason"] == "tampered"


@pytest.mark.docker
def test_truncating_the_ledger_is_caught_by_the_anchor_alone(
    events_client: Any, events_store: PostgresEventStore, ledger_clean: Any
) -> None:
    """Cut the tail off and every surviving link still verifies. Only the anchor notices."""
    for index in range(6):
        _post(events_client, _event(f"ev-{index}"))

    with (
        _trigger_disabled(ledger_clean, "commerce_events_append_only_trigger"),
        ledger_clean.cursor() as cur,
    ):
        cur.execute("delete from ledger.commerce_events where seq > 3")

    survivors = events_store.read()
    assert len(survivors) == 3
    assert verify_chain(survivors)["ok"] is True, (
        "the links alone cannot see truncation -- that is the point of the anchor"
    )

    report = events_store.verify()
    assert report["ok"] is False
    assert report["reason"] == "truncated"
    assert report["anchor"]["length"] == 6
    assert report["anchor_ok"] is False


@pytest.mark.docker
def test_the_chain_continues_across_a_restart_of_the_writer(
    events_running: Any, events_service: Any
) -> None:
    """Hash chain continuation: a brand-new writer links behind the ledger's head.

    The second writer has its own store, its own connections and its own application, so
    the only thing it can be continuing is the ledger.
    """
    first_client, first_store = events_running
    for index in range(3):
        _post(first_client, _event(f"ev-{index}"))
    head_before = first_store.head_hash

    second_client, second_store = events_service()
    assert second_store is not first_store
    assert second_store.head_hash == head_before

    landed = _post(second_client, _event("ev-3"))
    assert landed.status_code == 201, landed.text
    assert landed.json()["event"]["prev_hash"] == head_before
    assert landed.json()["seq"] == 4
    assert second_store.verify()["ok"] is True
    assert first_store.verify()["ok"] is True


# ======================================================================================
# 7. Over real Postgres: replays exactly
# ======================================================================================
@pytest.mark.docker
def test_replay_reproduces_the_fixture_streams_hash_from_the_ledger(
    events_client: Any, events_fixture_stream: dict[str, Any]
) -> None:
    """T-060 acceptance 3, end to end: POST the fixture stream, replay it, compare hashes."""
    for event in copy.deepcopy(events_fixture_stream["events"]):
        response = _post(events_client, event)
        assert response.status_code == 201, response.text

    replay = events_client.get("/events/replay")
    assert replay.status_code == 200, replay.text
    report = replay.json()

    assert report["ok"] is True, report["detail"]
    assert report["length"] == events_fixture_stream["length"]
    assert report["stream_hash"] == events_fixture_stream["stream_hash"]
    assert report["head_hash"] == events_fixture_stream["head_hash"]

    replayed = [
        {key: value for key, value in row.items() if key != "seq"} for row in report["events"]
    ]
    recorded = [
        {key: value for key, value in row.items() if key != "seq"}
        for row in events_fixture_stream["sealed"]
    ]
    assert replayed == recorded, "the replayed events are not byte-for-byte the sealed stream"


@pytest.mark.docker
def test_replay_reproduces_the_stream_hash_in_a_process_with_nothing_but_the_dsn(
    events_client: Any,
    events_dsn: str,
    events_fixture_stream: dict[str, Any],
    tmp_path: pathlib.Path,
) -> None:
    """ "From the ledger alone": a fresh interpreter, no store, no server, no memory.

    A replay that reads a list the writer has been holding since the write reproduces the
    writer, not the ledger -- and goes on succeeding after the ledger has been emptied. The
    only thing handed to this subprocess is a connection string.
    """
    for event in copy.deepcopy(events_fixture_stream["events"]):
        assert _post(events_client, event).status_code == 201

    program = tmp_path / "replay_from_the_ledger.py"
    program.write_text(
        "import json, os, sys\n"
        "from apps.trust.src.events import PostgresEventStore\n"
        "store = PostgresEventStore(os.environ['LEDGER_DSN'])\n"
        "report = store.replay()\n"
        "store.close()\n"
        "print(json.dumps({\n"
        "    'ok': report['ok'],\n"
        "    'length': report['length'],\n"
        "    'stream_hash': report['stream_hash'],\n"
        "    'head_hash': report['head_hash'],\n"
        "    'event_ids': [row['event_id'] for row in report['events']],\n"
        "    'seqs': [row['seq'] for row in report['events']],\n"
        "}))\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(program)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={
            "LEDGER_DSN": events_dsn,
            "PYTHONPATH": f"{REPO_ROOT}:{REPO_ROOT / '.pkgroot'}",
            "PATH": "/usr/bin:/bin",
        },
        timeout=180,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    replayed = json.loads(proc.stdout.strip().splitlines()[-1])

    assert replayed["ok"] is True
    assert replayed["stream_hash"] == events_fixture_stream["stream_hash"]
    assert replayed["head_hash"] == events_fixture_stream["head_hash"]
    assert replayed["length"] == events_fixture_stream["length"]
    assert replayed["event_ids"] == [row["event_id"] for row in events_fixture_stream["sealed"]]
    assert replayed["seqs"] == sorted(replayed["seqs"]), "replay must be in insertion order"


@pytest.mark.docker
def test_the_in_memory_and_postgres_writers_agree_on_the_stream_hash(
    events_client: Any, events_store: PostgresEventStore, events_fixture_stream: dict[str, Any]
) -> None:
    """One hashing rule, two stores (D16). If they ever disagree, one of them grew its own."""
    memory = InMemoryEventStore()
    for event in copy.deepcopy(events_fixture_stream["events"]):
        append(memory, event)
        assert _post(events_client, copy.deepcopy(event)).status_code == 201

    assert events_store.stream_hash() == memory.stream_hash()
    assert events_store.head_hash == memory.head_hash
    assert [row["event_hash"] for row in events_store.read()] == [
        row["event_hash"] for row in memory.events
    ]


@pytest.mark.docker
def test_replay_with_snapshots_names_the_missing_scorer_rather_than_returning_nothing(
    events_client: Any,
) -> None:
    """T-062 owns the arithmetic (D49). Until it lands, the answer is a 503 that says so.

    An empty mapping would compare equal to nothing and read as a pass, which is exactly the
    failure the ledger's replay seam raises rather than returns.
    """
    _post(events_client, _obs_event("ev-1", "s-1", "price_honored", "verified"))

    missing_as_of = events_client.get("/events/replay", params={"snapshots": "true"})
    assert missing_as_of.status_code == 422
    assert missing_as_of.json()["detail"]["error"] == "as_of_required"

    response = events_client.get("/events/replay", params={"snapshots": "true", "as_of": AS_OF})
    if response.status_code == 503:
        assert response.json()["detail"]["error"] == "scorer_unavailable"
        assert "T-062" in response.json()["detail"]["message"]
    else:  # T-062 has landed; the seam must then actually produce snapshots
        assert response.status_code == 200, response.text
        assert "s-1" in response.json()["snapshots"]


# ======================================================================================
# 8. The rest of the HTTP surface
# ======================================================================================
@pytest.mark.docker
def test_the_head_endpoint_reports_the_link_the_next_append_writes_behind(
    events_client: Any,
) -> None:
    empty = events_client.get("/events/head").json()
    assert empty == {"head_hash": GENESIS_HASH, "length": 0}

    landed = _post(events_client, _event("ev-1")).json()
    assert events_client.get("/events/head").json() == {
        "head_hash": landed["event"]["event_hash"],
        "length": 1,
    }


@pytest.mark.docker
def test_an_event_can_be_read_back_by_its_idempotency_key(events_client: Any) -> None:
    stored = _post(events_client, _event("ev-1")).json()["event"]
    assert events_client.get("/events/ev-1").json()["event"] == stored

    missing = events_client.get("/events/ev-nope")
    assert missing.status_code == 404
    assert missing.json()["detail"]["error"] == "unknown_event"


@pytest.mark.docker
def test_a_store_filtered_read_is_labelled_a_projection_and_not_a_chain(
    events_client: Any,
) -> None:
    """A filtered read skips events, so its links skip too. Handing it to a verifier would
    produce a ``broken_link`` the caller caused itself, which is why the response says so."""
    _post(events_client, _event("ev-1", store_id="s-1"))
    _post(events_client, _event("ev-2", store_id="s-2"))
    _post(events_client, _event("ev-3", store_id="s-1"))

    whole = events_client.get("/events").json()
    assert whole["count"] == 3
    assert whole["is_chain"] is True

    projection = events_client.get("/events", params={"store_id": "s-1"}).json()
    assert [row["event_id"] for row in projection["events"]] == ["ev-1", "ev-3"]
    assert projection["is_chain"] is False
    assert verify_chain(projection["events"])["reason"] == "broken_link"


@pytest.mark.docker
def test_an_empty_ledger_reports_empty_rather_than_intact(events_store: PostgresEventStore) -> None:
    """Verifying nothing is not verifying: a wiped ledger and a healthy one must differ."""
    report = events_store.verify()
    assert report["length"] == 0
    assert report["anchor"]["length"] == 0
    assert report["ok"] is True, "an anchor that says zero is the witness that zero is right"

    # ...but without that witness the same read is not assertable as intact: an empty read
    # arrives for reasons that have nothing to do with integrity, and calling it `ok` makes
    # "the ledger was wiped" and "the ledger is fine" the same answer.
    bare = verify_stream([])
    assert bare["ok"] is False
    assert bare["reason"] == "empty"
