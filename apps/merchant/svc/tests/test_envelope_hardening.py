"""Positive tests for the envelope repairs made in cycle 20 (T-243, T-248, T-239, T-246).

``test_repro_open_tickets.py`` holds the *reproduction* gates — each one written to fail
against the defect and to stop failing when it is closed. This file holds the other half:
what the repaired code is now supposed to do, asserted directly, so a future change that
satisfies a reproduction gate vacuously still has to answer for the behaviour.

**Nothing here mutates process-global state.** Every probe builds its own
:class:`~merchant_svc.envelope.store.EnvelopeVersions`; ``ENVELOPES`` is read for identity
only, never written, for the same reason the reproduction file gives.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from merchant_svc.bidding import store_agent_context, store_may_bid
from merchant_svc.envelope.digest import approval_digest
from merchant_svc.envelope.model import ACTIVE, KILLED, SHADOW, ApprovalRejected, Envelope
from merchant_svc.envelope.repository import (
    InMemoryEnvelopeRepository,
    PostgresEnvelopeRepository,
)
from merchant_svc.envelope.store import EnvelopeVersions
from merchant_svc.envelope.versions import activate_envelope, edit_envelope


def _envelope(store_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "store_id": store_id,
        "version": 1,
        "floors": [],
        "max_discount_pct": 10.0,
        "budget_cap": 100.0,
        "pursue_clusters": [],
        "standing_commitments": [],
        "activation": SHADOW,
    }
    body.update(overrides)
    return body


def _approval(envelope: Any, **overrides: Any) -> dict[str, Any]:
    artifact = {
        "approver": "owner@store.example",
        "approved_at": "2026-09-05T10:00:00+00:00",
        "envelope_hash": approval_digest(envelope),
    }
    artifact.update(overrides)
    return artifact


# ======================================================================================
# T-248 — record() is the door, so the approval rule is enforced at the door
# ======================================================================================
def test_record_refuses_an_envelope_that_asserts_its_own_activation() -> None:
    """The whole point: `active` is granted by an artifact, never claimed by a document."""
    versions = EnvelopeVersions()
    with pytest.raises(ApprovalRejected):
        versions.record(_envelope("s-claim", activation=ACTIVE))
    # Refused means refused: nothing was filed, so the store is not merely "not live" — it
    # has no history at all and `current` still raises.
    assert versions.stores() == (), "a refused record still appended to the history"


def test_record_refuses_an_approval_bound_to_a_different_version() -> None:
    """The edit-after-approve substitution, arriving through ``record`` instead of activate.

    v1 is approved and live. v2 changes the discount ceiling. Carrying v1's artifact onto v2
    and filing that directly is exactly what ``activate_envelope`` refuses, and ``record``
    must refuse it too or the polite entry point is the only thing enforcing R6.
    """
    versions = EnvelopeVersions()
    first = versions.record(_envelope("s-swap"))
    live = versions.record(activate_envelope(first, _approval(first)))
    assert live.is_live

    edited = edit_envelope(live, {"max_discount_pct": 99.0})
    forged = edited.with_activation(ACTIVE, live.approval)
    with pytest.raises(ApprovalRejected):
        versions.record(forged)
    assert versions.current("s-swap").max_discount_pct == 10.0, (
        "a 99% ceiling was filed live under an approval taken over a 10% one"
    )


def test_the_polite_entry_points_still_work_end_to_end() -> None:
    """Control. A guard that refused everything would satisfy the gate and break the product.

    put -> shadow, activate -> live, edit -> back to shadow, kill -> killed, and every one of
    those goes through the tightened ``record``.
    """
    versions = EnvelopeVersions()
    stored = versions.put("s-happy", _envelope("s-happy", activation=ACTIVE))
    assert stored.activation == SHADOW, "put() honoured a body's activation claim"

    live = versions.activate("s-happy", _approval(stored))
    assert live.is_live and versions.is_live("s-happy")

    edited = versions.put("s-happy", _envelope("s-happy", max_discount_pct=5.0))
    assert edited.activation == SHADOW, "an edit left the store live under a stale approval"
    assert not versions.is_live("s-happy")

    versions.activate("s-happy", _approval(edited))
    assert versions.kill("s-happy").activation == KILLED
    assert not versions.is_live("s-happy")


def test_kill_still_works_on_a_live_envelope() -> None:
    """The kill switch takes no artifact, and the new guard must not start demanding one.

    ``kill_envelope`` keeps the approval that was on file — it is the record of what the
    store *was* running — and moves the state to ``killed``, so the guard's ACTIVE-only
    condition is what keeps a paperwork check off the one control that must always work.
    """
    versions = EnvelopeVersions()
    first = versions.record(_envelope("s-kill"))
    versions.record(activate_envelope(first, _approval(first)))
    killed = versions.kill("s-kill")
    assert killed.activation == KILLED
    assert killed.approval is not None, "the record of what the store was running was dropped"


# ======================================================================================
# T-243 — one file, one module, whichever spelling reaches it
# ======================================================================================
@pytest.mark.parametrize(
    "module",
    ["", ".model", ".store", ".digest", ".versions", ".frozen"],
)
def test_both_spellings_of_every_envelope_module_are_one_object(module: str) -> None:
    short = importlib.import_module(f"merchant_svc.envelope{module}")
    long_ = importlib.import_module(f"apps.merchant.svc.src.envelope{module}")
    assert short is long_, (
        f"merchant_svc.envelope{module} and apps.merchant.svc.src.envelope{module} are two "
        "module objects over one file"
    )


def test_a_kill_through_one_spelling_is_visible_through_the_other() -> None:
    """The consequence the identity is FOR, asserted rather than inferred.

    This is the failure the split made possible: the merchant kills a store through the
    FastAPI app's spelling and the acceptance suite's spelling still reports it live. It uses
    a locally built store recorded through one spelling and read through the other's class,
    so it never writes the module-level ``ENVELOPES``.
    """
    short = importlib.import_module("merchant_svc.envelope.store")
    long_ = importlib.import_module("apps.merchant.svc.src.envelope.store")

    versions = short.EnvelopeVersions()
    first = versions.record(_envelope("s-two-spellings"))
    versions.record(activate_envelope(first, _approval(first)))
    assert long_.EnvelopeVersions.is_live(versions, "s-two-spellings") is True

    versions.kill("s-two-spellings")
    assert long_.EnvelopeVersions.is_live(versions, "s-two-spellings") is False

    # The error classes are the other half: `except UnknownStore` written against one
    # spelling has to catch what the other raises, or a promised 409 becomes a 500.
    with pytest.raises(long_.UnknownStore):
        versions.current("s-never-seen")


# ======================================================================================
# T-239 — the history crosses the persistence boundary with its invariants intact
# ======================================================================================
class _FakeCursor:
    """Enough of a DB-API cursor to capture what the Postgres repository actually sends."""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self._connection.statements.append((statement, parameters))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._connection.rows)


class _FakeConnection:
    """A connection that records statements and replays rows. NOT a database.

    Stated plainly because it matters: this proves the SQL this repository *sends* and the
    documents it rebuilds from rows, not that Postgres accepts either. The compose stack is
    routinely down in this repo and a skipping test is not a gate, so the live-database
    coverage of ``PostgresEnvelopeRepository`` is honestly zero and is reported as such.
    """

    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


def test_a_fresh_store_over_the_same_repository_sees_the_whole_history() -> None:
    """The seam's whole purpose: a restart is a new object over the same backing store."""
    backing = InMemoryEnvelopeRepository()

    first = EnvelopeVersions(backing)
    first.put("s-durable", _envelope("s-durable"))
    first.put("s-durable", _envelope("s-durable", max_discount_pct=7.5))

    # The "restart": nothing is shared but the repository.
    after_restart = EnvelopeVersions(backing)
    assert after_restart.stores() == ("s-durable",)
    assert [version.version for version in after_restart.history("s-durable")] == [1, 2]
    assert after_restart.current("s-durable").max_discount_pct == 7.5


def test_the_default_store_shares_nothing_which_is_the_old_behaviour_kept() -> None:
    """Control. Two bare stores must still be independent, or every existing test is a lie."""
    first = EnvelopeVersions()
    first.put("s-default", _envelope("s-default"))
    assert EnvelopeVersions().stores() == ()


def test_a_backing_store_that_hands_back_a_backwards_history_is_refused() -> None:
    """The version rule is re-checked on the way IN, not trusted for coming from a table."""
    versions = EnvelopeVersions()
    head = versions.record(_envelope("s-backwards", version=4))
    older = versions.record(_envelope("s-backwards", version=4)).with_activation(SHADOW, None)
    tampered = InMemoryEnvelopeRepository({"s-backwards": [head, _rewind(older, 3)]})
    with pytest.raises(Exception, match="goes backwards"):
        EnvelopeVersions(tampered)


def test_a_stored_active_version_with_no_surviving_approval_comes_back_in_shadow() -> None:
    """Fail-safe, in the direction the ticket names: a restart may stop a store, never start one."""
    orphaned = Envelope.from_obj(_envelope("s-orphan", activation=ACTIVE), approval=None)
    restored = EnvelopeVersions(InMemoryEnvelopeRepository({"s-orphan": [orphaned]}))
    assert restored.current("s-orphan").activation == SHADOW
    assert restored.is_live("s-orphan") is False


def test_a_failed_durable_write_does_not_leave_the_process_ahead_of_the_store() -> None:
    """persist() first, append second. A store that thinks it filed v4 over a table at v3
    is the stale writer the version rule exists to refuse."""

    class _Refusing(InMemoryEnvelopeRepository):
        def persist(self, envelope: Envelope) -> None:
            raise RuntimeError("the envelope table is unreachable")

    versions = EnvelopeVersions(_Refusing())
    with pytest.raises(RuntimeError, match="unreachable"):
        versions.record(_envelope("s-write-fails"))
    assert versions.stores() == (), "a failed durable write still landed in memory"


def test_the_postgres_repository_writes_one_row_per_version_to_sealed_envelopes() -> None:
    """The SQL is asserted, not assumed — including that it is an INSERT and not an UPSERT."""
    connection = _FakeConnection()
    repository = PostgresEnvelopeRepository(connection)
    versions = EnvelopeVersions(repository)
    versions.put("s-pg", _envelope("s-pg", max_discount_pct=12.5))

    assert len(connection.statements) == 2, connection.statements
    select_sql, _ = connection.statements[0]
    insert_sql, parameters = connection.statements[1]
    assert select_sql.startswith("select ") and "sealed.envelopes" in select_sql
    assert insert_sql.startswith("insert into sealed.envelopes ")
    assert "on conflict" not in insert_sql.lower(), (
        "an upsert would answer a racing writer with 'your version vanished'"
    )
    assert parameters[:5] == ("s-pg", 1, SHADOW, 12.5, 100.0)
    assert connection.commits == 1


def test_the_postgres_repository_rebuilds_a_history_from_rows() -> None:
    """Rows in, envelopes out — including a jsonb column the driver handed back as text."""
    rows = [
        ("s-rows", 1, SHADOW, 10.0, 100.0, "[]", "[]", "[]"),
        ("s-rows", 2, ACTIVE, 5.0, 50.0, [], ["shoes"], []),
    ]
    versions = EnvelopeVersions(PostgresEnvelopeRepository(_FakeConnection(rows)))
    assert [version.version for version in versions.history("s-rows")] == [1, 2]
    head = versions.current("s-rows")
    assert head.pursue_clusters == ("shoes",)
    # sealed.envelopes has no approval column, so v2's stored `active` cannot be honoured.
    assert head.activation == SHADOW
    assert versions.is_live("s-rows") is False


def _rewind(envelope: Envelope, version: int) -> Envelope:
    """A version-renumbered copy, built for the tamper case only."""
    document = envelope.to_dict()
    document["version"] = version
    return Envelope.from_obj(document, approval=None)


# ======================================================================================
# T-246 — the activation decision reaches the consumer that was written to read it
# ======================================================================================
def _live(versions: EnvelopeVersions, store_id: str) -> None:
    """Put a store's envelope on file and activate it against a real approval."""
    stored = versions.put(store_id, _envelope(store_id))
    versions.activate(store_id, _approval(stored))


@pytest.mark.parametrize(
    ("state", "expected_activation", "expected_may_bid"),
    [
        ("unknown", SHADOW, False),
        ("shadow", SHADOW, False),
        ("active", ACTIVE, True),
        ("killed", KILLED, False),
    ],
)
def test_the_store_agent_gate_reads_the_merchant_decision(
    state: str, expected_activation: str, expected_may_bid: bool
) -> None:
    """Driven through the REAL consumer, not a fake.

    ``store_agent.modes.runner._envelope_states`` is the activation gate T-246 says is fed by
    nobody. This test builds the context with the merchant's producer and hands it to that
    exact function, so what is proved is that the two halves of the seam agree — not that a
    mapping this test wrote has the keys this test expects.

    ``_envelope_states`` is imported inside the test rather than at module scope so a failure
    to import it is charged to this test and reads as "the seam's consumer is gone", instead
    of erroring the whole file at collection time.
    """
    from store_agent.modes.runner import _envelope_states  # noqa: PLC0415

    versions = EnvelopeVersions()
    store_id = f"s-gate-{state}"
    if state == "shadow":
        versions.put(store_id, _envelope(store_id))
    elif state == "active":
        _live(versions, store_id)
    elif state == "killed":
        _live(versions, store_id)
        versions.kill(store_id)

    context = store_agent_context(store_id, versions=versions)
    assert _envelope_states(context).value == expected_activation
    assert store_may_bid(store_id, versions=versions) is expected_may_bid
    assert context["may_bid"] is expected_may_bid


def test_the_producer_states_shadow_rather_than_relying_on_the_consumers_default() -> None:
    """An unknown store gets an explicit `shadow`, not an omitted key.

    Omitting it also fails closed today — the runner's gate defaults a missing activation to
    shadow — but it would fail closed by leaning on the CONSUMER's default. The day that
    default is loosened, a seam that granted activation by silence starts granting it.
    """
    context = store_agent_context("s-never-onboarded", versions=EnvelopeVersions())
    assert context["envelope"]["activation"] == SHADOW
    assert "no envelope" in context["reason"]


def test_the_decision_is_asked_of_is_live_and_not_re_derived() -> None:
    """A fourth activation state must not be readable as live by one of two spellings.

    ``contracts.EnvelopeActivation`` has three members, so a genuinely unknown state cannot be
    filed through the normal path. This drives the accessor directly instead: whatever
    ``is_live`` says is what the gate says, because the gate asks it rather than re-spelling
    the equality.
    """

    class _Fourth(EnvelopeVersions):
        def is_live(self, store_id: str) -> bool:
            return False

    versions = _Fourth()
    _live(versions, "s-fourth")
    assert versions.current("s-fourth").activation == ACTIVE
    assert store_may_bid("s-fourth", versions=versions) is False, (
        "the gate re-derived the decision instead of asking is_live"
    )
