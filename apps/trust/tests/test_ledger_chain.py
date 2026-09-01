"""T-011: the D16 hash chain -- canonicalisation, sealing, verification, and Postgres.

Owned by T-011. Acceptance 3 is the subject of this file: *commerce_events enforces
idempotency_key uniqueness and valid prev_hash; replay reproduces the stream hash.*

Three layers, in order:

* **Canonical form.** RFC-8785 JCS is what makes two readers agree on one digest. The tests
  here are about the ways ``json.dumps(sort_keys=True)`` would have been subtly wrong.
* **The chain in memory.** ``seal_event`` / ``verify_chain`` -- including the exact assertion
  T-060's frozen test makes (``broken_at == 2`` for a payload mutated at index 2), asserted
  here so T-011 finds a regression in its own library rather than leaving it for T-060.
* **The chain in Postgres.** Idempotency, the link constraint, append-only, no forks, and
  the property everything else rests on: a row that has been through ``timestamptz`` and
  ``jsonb`` still hashes to what was written.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import sys
import types
from typing import Any

import psycopg
import pytest

from apps.trust.src.ledger import (
    GENESIS_HASH,
    CanonicalisationError,
    LedgerError,
    append_event,
    canonical_event,
    canonical_json,
    chain_events,
    chain_head,
    compute_event_hash,
    db_stream_hash,
    head_hash,
    observations_from_events,
    read_events,
    replay,
    rfc3339_ms,
    seal_event,
    stream_hash,
    verify_chain,
    verify_chain_in_db,
)

AS_OF = "2026-01-01T00:00:00Z"

DIMS = (
    "price_honored",
    "discount_honored",
    "shipped_on_time",
    "not_returned",
    "feedback_match",
    "catalog_claim_accuracy",
)


def observation_event(index: int, store_id: str = "s-1", dim: str = "price_honored") -> dict:
    """A ``claim_verified`` LedgerEvent carrying exactly one trust observation."""
    return {
        "event_id": f"ev-{index}",
        "ts": AS_OF,
        "kind": "claim_verified",
        "store_id": store_id,
        "payload": {"dim": dim, "type": "verified", "observed_at": AS_OF},
    }


# =======================================================================================
# Canonical form (RFC-8785 JCS, D16)
# =======================================================================================


def test_canonical_json_sorts_keys_and_emits_no_whitespace() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonical_json({"a": [1, {"z": 0, "y": 1}]}) == '{"a":[1,{"y":1,"z":0}]}'
    assert " " not in canonical_json({"a": {"b": [1, 2, 3]}})


def test_canonical_json_is_independent_of_insertion_order_and_a_json_round_trip() -> None:
    """The property that makes the digest stable across processes and storage layers."""
    first = {"kind": "accepted", "payload": {"total": 12.5, "code": "X"}, "event_id": "ev-1"}
    second = {"event_id": "ev-1", "payload": {"code": "X", "total": 12.5}, "kind": "accepted"}
    assert canonical_json(first) == canonical_json(second)
    assert canonical_json(json.loads(json.dumps(first))) == canonical_json(first)


def test_canonical_json_uses_the_ecmascript_number_form() -> None:
    """JCS §3.2.2.3. ``json.dumps`` writes ``1.0``; ECMAScript -- and therefore JCS -- writes ``1``."""
    assert canonical_json(1.0) == "1"
    assert canonical_json(-0.0) == "0"
    assert canonical_json(2.5) == "2.5"
    assert canonical_json(10) == "10"
    assert canonical_json(True) == "true"  # bool before int: `True == 1` in Python
    assert canonical_json({"n": 1.0}) == '{"n":1}'
    assert json.dumps(1.0) == "1.0", "the contrast this test exists for has gone away"


def test_canonical_json_sorts_by_utf16_code_unit_not_code_point() -> None:
    """JCS §3.2.3. The two orders differ above the BMP, and Python's default is the wrong one.

    ``"\\uffff"`` is one UTF-16 code unit ``FFFF``; ``"\\U0001f600"`` is the surrogate pair
    ``D83D DE00``. By code point the emoji is larger; by UTF-16 code unit it is smaller.
    """
    keys = {"￿": 1, "\U0001f600": 2}
    assert canonical_json(keys) == '{"\U0001f600":2,"￿":1}'
    assert json.dumps(keys, sort_keys=True, ensure_ascii=False) == ('{"￿": 1, "\U0001f600": 2}'), (
        "sort_keys=True no longer differs here; re-derive why this test exists before editing"
    )


def test_canonical_json_leaves_non_ascii_literal_and_escapes_controls() -> None:
    assert canonical_json("café") == '"café"'
    assert canonical_json("a\nb\tc") == '"a\\nb\\tc"'
    assert canonical_json("\x00") == '"\\u0000"'
    assert canonical_json('he said "hi"\\') == '"he said \\"hi\\"\\\\"'


def test_canonical_json_refuses_values_with_no_canonical_form() -> None:
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(CanonicalisationError):
            canonical_json(value)
    with pytest.raises(CanonicalisationError):
        canonical_json({1: "int key"})
    with pytest.raises(CanonicalisationError):
        canonical_json(object())


def test_rfc3339_ms_normalises_every_spelling_of_one_instant() -> None:
    """D16: UTC RFC-3339 at millisecond precision, whatever came in."""
    expected = "2026-01-01T00:00:00.000Z"
    assert rfc3339_ms("2026-01-01T00:00:00Z") == expected
    assert rfc3339_ms("2026-01-01T00:00:00.000Z") == expected
    assert rfc3339_ms("2026-01-01T01:00:00+01:00") == expected
    assert rfc3339_ms("2025-12-31T19:00:00-05:00") == expected
    assert rfc3339_ms(dt.datetime(2026, 1, 1, tzinfo=dt.UTC)) == expected
    assert rfc3339_ms(dt.datetime(2026, 1, 1)) == expected  # naive is read as UTC
    # Sub-millisecond digits truncate, so a value that round-trips through timestamptz
    # (microsecond precision) normalises to the same string it did going in.
    assert rfc3339_ms("2026-01-01T00:00:00.123456Z") == "2026-01-01T00:00:00.123Z"
    assert rfc3339_ms(dt.datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=dt.UTC)) == (
        "2026-01-01T00:00:00.123Z"
    )
    with pytest.raises(CanonicalisationError):
        rfc3339_ms("yesterday")


def test_canonical_event_drops_chain_fields_and_absent_optionals() -> None:
    """An event cannot commit to its own digest, and ``None`` is not a value."""
    event = observation_event(1)
    sealed = seal_event(event)
    assert canonical_event(sealed) == canonical_event(event)
    assert canonical_event({**event, "order_ref": None}) == canonical_event(event)
    assert canonical_event({**event, "seq": 7}) == canonical_event(event)
    with pytest.raises(CanonicalisationError):
        canonical_event({"ts": AS_OF, "payload": {}})  # no event_id, no kind


def test_the_digest_is_sha256_of_prev_hash_concatenated_with_the_canonical_body() -> None:
    """D16's formula, spelled out independently of the implementation."""
    import hashlib

    event = observation_event(1)
    expected = hashlib.sha256(
        GENESIS_HASH.encode("ascii") + canonical_json(canonical_event(event)).encode("utf-8")
    ).hexdigest()
    assert compute_event_hash(GENESIS_HASH, event) == expected
    assert len(expected) == 64


# =======================================================================================
# The chain, in memory
# =======================================================================================


def test_seal_event_does_not_mutate_its_input() -> None:
    event = observation_event(1)
    before = copy.deepcopy(event)
    sealed = seal_event(event)
    assert event == before
    assert sealed["prev_hash"] == GENESIS_HASH
    assert sealed["event_hash"] != GENESIS_HASH


def test_verify_chain_accepts_a_clean_stream() -> None:
    sealed = chain_events(observation_event(i, dim=DIMS[i % len(DIMS)]) for i in range(5))
    result = verify_chain(sealed)
    assert result["ok"] is True
    assert result["broken_at"] is None
    assert result["head_hash"] == chain_head(sealed) == sealed[-1]["event_hash"]


def test_verify_chain_reports_the_index_of_a_tampered_event() -> None:
    """The exact assertion T-060's frozen test makes, proved here against this library.

    T-060 owns ``apps/trust/src/events`` and may not write this package, so if the verifier
    were wrong the failure would land on a ticket that cannot fix it.
    """
    sealed = chain_events(observation_event(i, dim=DIMS[i % len(DIMS)]) for i in range(5))
    tampered = copy.deepcopy(sealed)
    tampered[2]["payload"]["type"] = "contradicted"

    broken = verify_chain(tampered)
    assert broken["ok"] is False
    assert broken["broken_at"] == 2
    assert broken["reason"] == "tampered"
    assert verify_chain(sealed)["ok"] is True, "the clean stream must still verify"


@pytest.mark.parametrize("index", [0, 1, 4])
def test_verify_chain_finds_tampering_at_any_position(index: int) -> None:
    sealed = chain_events(observation_event(i) for i in range(5))
    tampered = copy.deepcopy(sealed)
    tampered[index]["store_id"] = "s-impostor"
    assert verify_chain(tampered)["broken_at"] == index


def test_verify_chain_detects_a_reordered_or_truncated_stream() -> None:
    """Rewriting the sequence breaks the *links*, even though every event is untouched."""
    sealed = chain_events(observation_event(i) for i in range(5))

    reordered = [sealed[0], sealed[2], sealed[1], sealed[3], sealed[4]]
    assert verify_chain(reordered) == {
        "ok": False,
        "broken_at": 1,
        "reason": "broken_link",
        "head_hash": sealed[0]["event_hash"],
    }

    dropped = sealed[:2] + sealed[3:]
    result = verify_chain(dropped)
    assert result["ok"] is False and result["broken_at"] == 2
    assert result["reason"] == "broken_link"


def test_verify_chain_refuses_an_unsealed_stream_rather_than_passing_it() -> None:
    """An unverifiable stream is not a verified one.

    This is the contract T-060's ``append`` has to meet: stamp the chain fields with
    ``seal_event``. A store that keeps only a running head hash leaves the verifier nothing
    to check, and the honest answer to "is this stream intact?" is then "unknown", which
    here is reported as not-ok rather than silently as ok.
    """
    raw = [observation_event(i) for i in range(3)]
    assert verify_chain(raw) == {
        "ok": False,
        "broken_at": 0,
        "reason": "unsealed",
        "head_hash": GENESIS_HASH,
    }
    assert verify_chain([])["ok"] is True  # ...but an empty stream is intact


def test_stream_hash_commits_to_every_event_in_order() -> None:
    """The "event-stream hash" of acceptance 3: it moves for any change to any event."""
    sealed = chain_events(observation_event(i) for i in range(4))
    baseline = stream_hash(sealed)
    assert baseline == chain_head(sealed)
    assert stream_hash([]) == GENESIS_HASH

    assert stream_hash(chain_events(observation_event(i) for i in range(3))) != baseline
    reordered = chain_events(
        [observation_event(0), observation_event(2), observation_event(1), observation_event(3)]
    )
    assert stream_hash(reordered) != baseline, "the stream hash must depend on order"


# =======================================================================================
# The chain, in Postgres -- acceptance 3
# =======================================================================================


@pytest.mark.docker
def test_append_event_chains_from_genesis(ledger_clean) -> None:
    connection = ledger_clean
    assert head_hash(connection) == GENESIS_HASH

    first = append_event(connection, observation_event(0))
    assert first.inserted is True
    assert first.event["prev_hash"] == GENESIS_HASH
    assert first.event["seq"] == 1

    second = append_event(connection, observation_event(1))
    assert second.event["prev_hash"] == first.event["event_hash"]
    assert head_hash(connection) == second.event["event_hash"] == second.head_hash
    assert verify_chain_in_db(connection)["ok"] is True


@pytest.mark.docker
def test_duplicate_event_id_is_a_no_op(ledger_clean) -> None:
    """Acceptance 3, first half. D16: ``idempotency_key`` IS ``event_id``.

    Length and head hash both have to stay put -- a second row would advance the chain even
    if the duplicate were otherwise harmless, and the head hash is what every downstream
    consumer compares against.
    """
    connection = ledger_clean
    original = append_event(connection, observation_event(0))
    head_before = head_hash(connection)

    again = append_event(connection, copy.deepcopy(observation_event(0)))
    assert again.inserted is False
    assert again.event["event_hash"] == original.event["event_hash"]
    assert again.event["seq"] == original.event["seq"]
    assert head_hash(connection) == head_before
    assert len(read_events(connection)) == 1

    # ...and the store is not simply refusing everything.
    fresh = append_event(connection, observation_event(1))
    assert fresh.inserted is True
    assert head_hash(connection) != head_before
    assert len(read_events(connection)) == 2

    # The constraint is real, not just a Python-side check.
    with pytest.raises(psycopg.errors.UniqueViolation) as raised:
        with connection.cursor() as cur:
            cur.execute(
                "insert into ledger.commerce_events "
                "(idempotency_key, kind, occurred_at, prev_hash, event_hash) "
                "values ('ev-0', 'shown', now(), %s, %s)",
                (head_hash(connection), "f" * 64),
            )
    assert "idempotency_key" in str(raised.value)


@pytest.mark.docker
def test_prev_hash_must_link_to_the_chain_tail(ledger_clean) -> None:
    """Acceptance 3, second half: "valid prev_hash", enforced by the database.

    Enforced server-side on purpose. A Python-only rule protects the chain from the library
    and from nothing else; the trigger protects it from psql, from a future service, and
    from a writer that skipped the advisory lock.
    """
    connection = ledger_clean
    append_event(connection, observation_event(0))

    for bad_prev in (GENESIS_HASH, "a" * 64):
        with pytest.raises(psycopg.errors.IntegrityError) as raised:
            with connection.cursor() as cur:
                cur.execute(
                    "insert into ledger.commerce_events "
                    "(idempotency_key, kind, occurred_at, prev_hash, event_hash) "
                    "values (%s, 'shown', now(), %s, %s)",
                    (f"ev-bad-{bad_prev[:4]}", bad_prev, "b" * 64),
                )
        assert "does not link to the chain tail" in str(raised.value)

    # A non-hex digest is refused by the format CHECK. (The link check is a BEFORE INSERT
    # trigger and therefore runs *first*, so the malformed value under test here has to be
    # the one the trigger does not look at.)
    with pytest.raises(psycopg.errors.CheckViolation) as check:
        with connection.cursor() as cur:
            cur.execute(
                "insert into ledger.commerce_events "
                "(idempotency_key, kind, occurred_at, prev_hash, event_hash) "
                "values ('ev-bad-hex', 'shown', now(), %s, %s)",
                (head_hash(connection), "NOT A HASH".ljust(64, "z")),
            )
    assert "event_hash_hex" in str(check.value)
    assert verify_chain_in_db(connection)["ok"] is True
    assert len(read_events(connection)) == 1


@pytest.mark.docker
def test_the_chain_cannot_fork(ledger_clean) -> None:
    """Two rows sharing a ``prev_hash`` is a fork, and it is refused twice over.

    The trigger catches it first, because a fork always re-uses a link that is no longer the
    tail. The ``UNIQUE (prev_hash)`` constraint is the layer underneath, and it is asserted
    independently -- with the trigger disabled -- because "belt and braces" is only true if
    somebody has checked the braces.
    """
    connection = ledger_clean
    first = append_event(connection, observation_event(0))
    append_event(connection, observation_event(1))

    with pytest.raises(psycopg.errors.IntegrityError) as raised:
        with connection.cursor() as cur:
            cur.execute(
                "insert into ledger.commerce_events "
                "(idempotency_key, kind, occurred_at, prev_hash, event_hash) "
                "values ('ev-fork', 'shown', now(), %s, %s)",
                (first.event["event_hash"], "d" * 64),
            )
    assert "does not link to the chain tail" in str(raised.value)

    with connection.cursor() as cur:
        cur.execute(
            "alter table ledger.commerce_events disable trigger commerce_events_chain_guard_trigger"
        )
    try:
        with pytest.raises(psycopg.errors.UniqueViolation) as unique:
            with connection.cursor() as cur:
                cur.execute(
                    "insert into ledger.commerce_events "
                    "(idempotency_key, kind, occurred_at, prev_hash, event_hash) "
                    "values ('ev-fork', 'shown', now(), %s, %s)",
                    (first.event["event_hash"], "d" * 64),
                )
        assert "prev_hash" in str(unique.value)
    finally:
        with connection.cursor() as cur:
            cur.execute(
                "alter table ledger.commerce_events enable trigger "
                "commerce_events_chain_guard_trigger"
            )
    assert verify_chain_in_db(connection)["ok"] is True


@pytest.mark.docker
def test_commerce_events_is_append_only(ledger_clean) -> None:
    connection = ledger_clean
    append_event(connection, observation_event(0))
    for statement in (
        "update ledger.commerce_events set kind = 'shown'",
        "delete from ledger.commerce_events",
    ):
        with pytest.raises(psycopg.errors.IntegrityError) as raised:
            with connection.cursor() as cur:
                cur.execute(statement)
        assert "append-only" in str(raised.value)
    assert len(read_events(connection)) == 1


@pytest.mark.docker
def test_an_unknown_event_kind_is_refused(ledger_clean) -> None:
    """C11/D24 froze the vocabulary; the column enforces it."""
    connection = ledger_clean
    with pytest.raises(psycopg.errors.CheckViolation):
        append_event(connection, {**observation_event(0), "kind": "invented_kind"})
    for kind in ("bid_placed", "auction_opened", "blacklist_expired", "offer_integrity"):
        result = append_event(connection, {**observation_event(0), "event_id": kind, "kind": kind})
        assert result.inserted is True


@pytest.mark.docker
def test_a_round_trip_through_postgres_preserves_the_digest(ledger_clean) -> None:
    """The property everything else rests on.

    ``ts`` goes through ``timestamptz`` and the payload through ``jsonb``, and both come
    back re-rendered by the server. If either renormalised, the stored ``event_hash`` would
    no longer match the row it is on and the chain would fail verification with no tampering
    anywhere -- so the round trip is asserted over a payload holding every JSON shape.
    """
    connection = ledger_clean
    event = {
        "event_id": "ev-roundtrip",
        "ts": "2026-03-04T05:06:07.089Z",
        "kind": "order_paid",
        "auction_id": "auction-1",
        "store_id": "s-1",
        "order_ref": "o-1",
        "payload": {
            "checkout_token": "ck-1",
            "total_price": 120.5,
            "quantity": 3,
            "zero": 0,
            "negative": -2.25,
            "flag": True,
            "absent": None,
            "note": "café — ünicode",
            "discountApplications": [{"type": "percentage", "value": 10}],
            "nested": {"b": [1, 2, {"c": "d"}], "a": {}},
        },
    }
    written = append_event(connection, event)
    [row] = read_events(connection)

    assert row["event_hash"] == written.event["event_hash"]
    assert compute_event_hash(GENESIS_HASH, row) == row["event_hash"], (
        "the row read back out of Postgres no longer hashes to its stored digest"
    )
    assert canonical_event(row) == canonical_event(event)
    assert row["ts"] == "2026-03-04T05:06:07.089Z"
    assert verify_chain_in_db(connection)["ok"] is True


@pytest.mark.docker
def test_a_payload_that_jsonb_renormalises_is_refused_at_write_time(ledger_clean) -> None:
    """Loud at the moment of the mistake, rather than as a mystery verification failure.

    ``jsonb`` stores numbers as ``numeric``, so ``1e100`` comes back as a 101-digit integer
    and canonicalises differently. That would corrupt the chain silently.
    """
    connection = ledger_clean
    with pytest.raises(LedgerError) as raised:
        append_event(
            connection,
            {**observation_event(0), "payload": {"huge": 1e100}},
        )
    assert "jsonb round trip" in str(raised.value)
    assert read_events(connection) == [], "the failed append must leave nothing behind"


@pytest.mark.docker
def test_replay_from_postgres_reproduces_the_stream_hash(ledger_clean) -> None:
    """Acceptance 3, third clause: "replay reproduces the stream hash".

    The stream is written, read back from genesis, and re-verified: the head recomputed from
    what the database returns is the head that was recorded at write time, event for event.
    """
    connection = ledger_clean
    written = [append_event(connection, observation_event(i, dim=DIMS[i % 6])) for i in range(8)]
    recorded_head = written[-1].head_hash

    replayed = read_events(connection)
    assert [row["event_id"] for row in replayed] == [f"ev-{i}" for i in range(8)]
    assert stream_hash(replayed) == recorded_head == db_stream_hash(connection)
    assert verify_chain(replayed)["ok"] is True

    # An incremental read resumes the same chain rather than starting a new one.
    tail = read_events(connection, after_seq=5)
    assert [row["seq"] for row in tail] == [6, 7, 8]
    assert tail[0]["prev_hash"] == replayed[4]["event_hash"]


@pytest.mark.docker
def test_appending_under_the_advisory_lock_keeps_one_linear_chain(ledger_clean) -> None:
    """A second writer waits for the tail rather than racing it (D16).

    Both connections append; the chain that results is linear and verifies, and the second
    writer's ``prev_hash`` is the first writer's ``event_hash`` -- not the tail it saw when
    it started.
    """
    from proxyshop_support.postgres import role_dsn
    from proxyshop_support.worker import worker_id

    connection = ledger_clean
    first = append_event(connection, observation_event(0))

    dsn = role_dsn("admin", worker_id(), database=connection.info.dbname)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as other:
        second = append_event(other, observation_event(1))
    assert second.event["prev_hash"] == first.event["event_hash"]

    third = append_event(connection, observation_event(2))
    assert third.event["prev_hash"] == second.event["event_hash"]
    assert verify_chain_in_db(connection)["ok"] is True
    assert [row["seq"] for row in read_events(connection)] == [1, 2, 3]


@pytest.mark.docker
def test_the_writer_leaves_no_advisory_lock_behind(ledger_clean) -> None:
    """``pg_advisory_xact_lock`` is transaction-scoped, so the commit releases it.

    A session-scoped lock here would be held for the life of the connection and would
    deadlock the next writer on a pooled connection.
    """
    from apps.trust.src.ledger.store import CHAIN_LOCK_KEY

    connection = ledger_clean
    append_event(connection, observation_event(0))
    with connection.cursor() as cur:
        cur.execute(
            "select count(*) from pg_locks where locktype = 'advisory' and objid = %s",
            (CHAIN_LOCK_KEY,),
        )
        assert cur.fetchone() == (0,)


@pytest.mark.docker
def test_a_malformed_event_is_rejected_before_it_reaches_the_database(ledger_clean) -> None:
    connection = ledger_clean
    with pytest.raises(LedgerError):
        append_event(connection, {"event_id": "ev-x", "kind": "shown"})  # no ts
    with pytest.raises(CanonicalisationError):
        append_event(connection, {"ts": AS_OF, "kind": "shown", "payload": {}})  # no event_id
    assert read_events(connection) == []


# =======================================================================================
# The replay seam (D49) -- projection here, every number in T-062
# =======================================================================================


def test_observations_from_events_projects_only_observation_payloads() -> None:
    """A ledger concern: what an event *carries*. Nothing here decides a score."""
    events = [
        observation_event(0, store_id="s-1", dim="price_honored"),
        {"event_id": "ev-a", "ts": AS_OF, "kind": "accepted", "store_id": "s-1", "payload": {}},
        observation_event(1, store_id="s-2", dim="catalog_claim_accuracy"),
        {"event_id": "ev-b", "ts": AS_OF, "kind": "auction_opened", "payload": {"x": 1}},
    ]
    assert observations_from_events(events) == [
        {"store_id": "s-1", "dim": "price_honored", "type": "verified", "observed_at": AS_OF},
        {
            "store_id": "s-2",
            "dim": "catalog_claim_accuracy",
            "type": "verified",
            "observed_at": AS_OF,
        },
    ]


def test_observations_preserve_stream_order_and_fall_back_to_the_event_timestamp() -> None:
    """Order is load-bearing: decay is a function of the recorded sequence (D17/S3)."""
    events = [
        {
            "event_id": "ev-0",
            "ts": "2026-01-02T00:00:00Z",
            "kind": "claim_verified",
            "store_id": "s-1",
            "payload": {"dim": "not_returned", "type": "mismatch_return"},
        },
        observation_event(1, dim="feedback_match"),
    ]
    projected = observations_from_events(events)
    assert [row["dim"] for row in projected] == ["not_returned", "feedback_match"]
    assert projected[0]["observed_at"] == "2026-01-02T00:00:00Z"


def test_replay_hands_every_number_to_the_scorer_grouped_by_store(monkeypatch) -> None:
    """D49: the scoring lives in T-062's files; this module is only the seam.

    The scorer is stubbed rather than imported, which is the point -- if ``replay`` computed
    anything itself, the stub's recorded arguments would not be the whole input and the
    returned object would not be the whole output.
    """
    calls: list[tuple[list[dict], Any]] = []

    def fake_score(observations, *, as_of):
        calls.append((list(observations), as_of))
        return {"score": 0.5, "score_version": "stub-1", "observations": len(observations)}

    stub = types.ModuleType("trust.scoring")
    stub.score = fake_score  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "trust.scoring", stub)

    events = [
        observation_event(0, store_id="s-1", dim="price_honored"),
        observation_event(1, store_id="s-2", dim="shipped_on_time"),
        observation_event(2, store_id="s-1", dim="feedback_match"),
    ]
    result = replay(events, as_of=AS_OF)

    assert set(result) == {"s-1", "s-2"}
    assert result["s-1"]["observations"] == 2
    assert result["s-2"]["observations"] == 1
    assert result["s-1"]["score_version"] == "stub-1"
    assert [as_of for _, as_of in calls] == [AS_OF, AS_OF]
    assert [row["dim"] for row in calls[0][0]] == ["price_honored", "feedback_match"]


def test_replay_names_the_scoring_ticket_when_the_scorer_is_absent() -> None:
    """An unbuilt scorer must be a loud failure, never an empty mapping.

    ``{}`` compares equal to nothing and would read as a pass in whichever test asked for it.
    """
    with pytest.raises(ModuleNotFoundError) as raised:
        replay([observation_event(0)], as_of=AS_OF)
    message = str(raised.value)
    assert "T-062" in message
    assert "score(observations" in message


def test_the_ledger_verifier_is_not_the_claim_verifier() -> None:
    """``verify_chain`` (ledger) and ``packages.verification.verify`` (T-065) are different.

    Asserted because the two names are one word apart and merging them would silently make
    a chain assertion into a claim assertion.
    """
    from apps.trust.src import ledger

    assert ledger.verify_chain.__module__.endswith("ledger.chain")
    assert not hasattr(ledger, "verify")
