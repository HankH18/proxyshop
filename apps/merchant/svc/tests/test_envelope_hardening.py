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
from merchant_svc.envelope.store import EnvelopeVersions, VersionWentBackwards
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
    ["", ".model", ".store", ".digest", ".versions", ".frozen", ".repository", "._spellings"],
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
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


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
    with pytest.raises(VersionWentBackwards, match="goes backwards"):
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
    """The SQL is asserted, not assumed.

    ASSERTION CHANGED, and the ritual is recorded here because the change REVERSES what this
    test claimed. It previously read:

        assert "on conflict" not in insert_sql.lower(), (
            "an upsert would answer a racing writer with 'your version vanished'"
        )

    What it encoded: that persist is a plain INSERT, so an append-only history can never
    overwrite a version somebody else recorded.

    Would this test still be wrong if my change were reverted? YES — and that is why it is
    changed rather than worked around. An adversarial verifier drove this repository against
    the real ``proxyshop-postgres-1`` table and measured that a plain INSERT makes TWO of the
    three lifecycle transitions impossible: ``activate`` and ``kill`` deliberately do not bump
    the version, and ``envelopes_pkey PRIMARY KEY (store_id, version)`` rejects the second row
    with ``UniqueViolation``. ``POST /stores/{id}/kill`` — the control that versions.py says
    has "no approval, no version bump, no excuse" — answered 500. The old assertion encoded a
    defect as the contract.
    The right contract, asserted below: the table holds one row per (store, version) carrying
    that version's CURRENT STATE, and the in-memory history holds every transition. The
    "your write vanished" hazard the old assertion feared is real for ``do nothing``, so that
    is what is now refused by name.
    """
    connection = _FakeConnection()
    repository = PostgresEnvelopeRepository(connection)
    versions = EnvelopeVersions(repository)
    versions.put("s-pg", _envelope("s-pg", max_discount_pct=12.5))

    assert len(connection.statements) == 2, connection.statements
    select_sql, _ = connection.statements[0]
    insert_sql, parameters = connection.statements[1]
    assert select_sql.startswith("select ") and "sealed.envelopes" in select_sql
    assert insert_sql.startswith("insert into sealed.envelopes ")
    assert "on conflict (store_id, version) do update set" in insert_sql.lower(), (
        "activate and kill do not bump the version, so a plain insert makes the kill switch "
        "a UniqueViolation against envelopes_pkey"
    )
    assert "do nothing" not in insert_sql.lower(), (
        "'do nothing' would answer a writer with 'your version vanished'"
    )
    # The identifying columns must NOT be in the SET list — an upsert that rewrote store_id or
    # version would move a version rather than restate it.
    set_clause = insert_sql.lower().split("do update set", 1)[1]
    assert "store_id =" not in set_clause and "version =" not in set_clause, (
        f"the upsert rewrites the row's own identity: {set_clause}"
    )
    assert parameters[:5] == ("s-pg", 1, SHADOW, 12.5, 100.0)
    assert connection.commits == 1


def test_the_lifecycle_transitions_all_reach_the_table_at_one_version() -> None:
    """put, activate, kill and revive each produce a write, all against v1's row.

    This is the test whose absence let the plain-INSERT bug ship green: the old suite only
    ever drove ``put``, so the transitions that collide on the primary key were never
    exercised against the SQL at all. ``revive`` is a fourth one — it restates v1 a second
    time — so it lands in the same upsert and would have been the same 500 under an insert.
    """
    connection = _FakeConnection()
    versions = EnvelopeVersions(PostgresEnvelopeRepository(connection))
    stored = versions.put("s-lifecycle", _envelope("s-lifecycle"))
    versions.activate("s-lifecycle", _approval(stored))
    versions.kill("s-lifecycle")
    versions.revive("s-lifecycle")

    writes = [
        (sql, params) for sql, params in connection.statements if sql.startswith("insert into")
    ]
    assert [params[1] for _, params in writes] == [1, 1, 1, 1], (
        "activate, kill and revive must not bump the version; the approval is bound to v1's terms"
    )
    assert [params[2] for _, params in writes] == [SHADOW, ACTIVE, KILLED, SHADOW]
    # The approval columns follow the eight terms columns, in `ApprovalArtifact.FIELDS` order.
    # The kill carries the artifact forward — it is the record of what the store was running —
    # and the revive writes NULLs, because a restartable version that still shows a signature
    # would be a row asserting that this store was approved live after somebody stopped it.
    assert writes[2][1][8] is not None, "the killed row keeps the approval on file"
    assert writes[3][1][8:] == (None, None, None, None, None), (
        "a revived version must be filed unapproved"
    )
    assert connection.commits == 4


def test_a_term_the_column_would_round_is_refused_rather_than_silently_changed() -> None:
    """An approved figure that changes by being written down is no longer what was approved.

    Measured against the real table by an adversarial verifier: max_discount_pct is
    numeric(6,3) and budget_cap is numeric(14,2), so 10.0005 and 100.005 came back as 10.001
    and 100.01 — and the restored version's approval digest no longer matched the recorded
    one. R9 says old versions never change; rounding them in the storage layer changes them.
    """
    from merchant_svc.envelope.model import EnvelopeInvalid  # noqa: PLC0415

    versions = EnvelopeVersions(PostgresEnvelopeRepository(_FakeConnection()))
    with pytest.raises(EnvelopeInvalid, match="max_discount_pct"):
        versions.put("s-round", _envelope("s-round", max_discount_pct=10.0005))
    with pytest.raises(EnvelopeInvalid, match="budget_cap"):
        versions.put("s-round", _envelope("s-round", budget_cap=100.005))

    # Control: a value the column CAN hold exactly is stored without complaint, so the guard
    # is about representability and not about refusing decimals.
    exact = EnvelopeVersions(PostgresEnvelopeRepository(_FakeConnection()))
    assert exact.put("s-exact", _envelope("s-exact", max_discount_pct=10.125)).max_discount_pct == (
        10.125
    )


def test_a_failed_write_rolls_back_so_the_connection_survives() -> None:
    """One integrity error must not brick the store — including the kill switch.

    Measured: without a rollback PostgreSQL aborts the transaction and answers every later
    statement InFailedSqlTransaction, taking the reload and the kill switch down with the
    write that failed.
    """

    class _Exploding(_FakeConnection):
        def cursor(self) -> _FakeCursor:
            if self.statements:
                raise RuntimeError("the envelope table is unreachable")
            return _FakeCursor(self)

    connection = _Exploding()
    versions = EnvelopeVersions(PostgresEnvelopeRepository(connection))
    before = connection.rollbacks
    with pytest.raises(RuntimeError, match="unreachable"):
        versions.record(_envelope("s-boom"))
    assert connection.rollbacks > before, "a failed write left the transaction open"


def test_a_read_does_not_leave_the_connection_idle_in_transaction() -> None:
    """A boot that loads and never writes must not pin a snapshot for the process's life."""
    connection = _FakeConnection()
    EnvelopeVersions(PostgresEnvelopeRepository(connection))
    assert connection.rollbacks == 1, (
        "load() left its SELECT's transaction open; a service that never writes would sit "
        "idle in transaction forever"
    )


def test_a_dict_row_connection_is_read_correctly_and_not_silently_scrambled() -> None:
    """psycopg's dict_row factory is common, and zip(strict=True) does NOT catch it.

    Zipping a mapping iterates its KEYS and the lengths match, so every field silently becomes
    its own column name. Measured before the fix as an unrelated float-conversion error.
    """
    rows = [
        {
            "store_id": "s-dict",
            "version": 1,
            "activation": SHADOW,
            "max_discount_pct": 10.0,
            "budget_cap": 100.0,
            "floors": [],
            "pursue_clusters": ["hats"],
            "standing_commitments": [],
        }
    ]
    versions = EnvelopeVersions(PostgresEnvelopeRepository(_FakeConnection(rows)))
    head = versions.current("s-dict")
    assert head.store_id == "s-dict"
    assert head.pursue_clusters == ("hats",)
    assert head.max_discount_pct == 10.0


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


def test_the_published_activation_and_the_verdict_can_never_disagree() -> None:
    """The unsafe direction, closed by construction: one read decides both halves.

    An earlier draft of ``_decide`` read the history TWICE — ``current()`` for the activation
    string it publishes and ``is_live()`` for the verdict — and published the first read
    beside the second's answer. An adversarial verifier measured the consequence on the plain
    class under concurrent writes: 3 contexts in 863,004 reads carried ``activation='active'``
    next to ``may_bid=False``, and the real consumer reads the activation, not the verdict.

    This drives the divergence deterministically instead of racing for it. ``_Divergent``
    makes the store-level accessor disagree with the envelope it hands out — exactly the
    split a badly-timed write produced — and the context must still be internally consistent.
    A context that says ``active`` while refusing to bid is a killed store bidding.
    """

    class _Divergent(EnvelopeVersions):
        def is_live(self, store_id: str) -> bool:
            return not super().is_live(store_id)

    versions = _Divergent()
    _live(versions, "s-divergent")
    assert versions.current("s-divergent").activation == ACTIVE

    context = store_agent_context("s-divergent", versions=versions)
    published_live = context["envelope"]["activation"] == ACTIVE
    assert published_live is context["may_bid"], (
        f"the producer published activation={context['envelope']['activation']!r} beside "
        f"may_bid={context['may_bid']}; the consumer reads the activation, so a context that "
        "disagrees with itself is a store bidding on the strength of the half nobody checked"
    )
    assert store_may_bid("s-divergent", versions=versions) is context["may_bid"], (
        "store_may_bid and the published context disagree about the same store"
    )


def test_a_killed_store_is_never_published_as_active_under_concurrent_writes() -> None:
    """The property under real interleaving, not a double: no context ever says active-and-not.

    A store is flipped live/killed on one thread while another reads the context. Every
    context observed must be internally consistent. This is the test that would have caught
    the two-read bug without needing anyone to think of it.
    """
    import threading  # noqa: PLC0415

    versions = EnvelopeVersions()
    _live(versions, "s-race")
    stop = threading.Event()
    unsafe: list[dict[str, Any]] = []

    def flip() -> None:
        while not stop.is_set():
            versions.kill("s-race")
            head = versions.current("s-race")
            versions.record(head.with_activation(SHADOW, None))
            refreshed = versions.put("s-race", _envelope("s-race"))
            versions.activate("s-race", _approval(refreshed))

    writer = threading.Thread(target=flip, daemon=True)
    writer.start()
    try:
        for _ in range(20000):
            context = store_agent_context("s-race", versions=versions)
            if (context["envelope"]["activation"] == ACTIVE) is not context["may_bid"]:
                unsafe.append(context)
                break
    finally:
        stop.set()
        writer.join(timeout=10)

    assert unsafe == [], f"a context disagreed with itself under concurrent writes: {unsafe}"


# ======================================================================================
# T-247 — the webhook sink cannot be left unwired by a caller putting back what it took
# ======================================================================================
def test_set_webhook_sink_none_restores_the_boot_default_rather_than_clearing() -> None:
    """`None` means "put it back", because that is what every caller passing it means.

    Restored explicitly at the end rather than through a fixture: this test writes a
    process-global, and leaving it changed is the exact defect the ticket is about.
    """
    from merchant_svc.install import webhooks  # noqa: PLC0415

    borrowed = webhooks.webhook_sink()
    try:
        webhooks.set_webhook_sink(lambda event: None)
        assert webhooks.webhook_sink() is not webhooks.default_sink

        webhooks.set_webhook_sink(None)
        assert webhooks.webhook_sink() is webhooks.default_sink, (
            "set_webhook_sink(None) left the service unwired instead of restoring the default"
        )

        # Handing deliveries to nobody is still reachable — it just has to be asked for.
        webhooks.set_webhook_sink(webhooks.inbox_only_sink)
        assert webhooks.webhook_sink() is webhooks.inbox_only_sink
    finally:
        webhooks.set_webhook_sink(borrowed)


def test_a_first_put_starts_at_version_one_whatever_the_body_claims() -> None:
    """`put` derives the version from what is on file — including when nothing is.

    ``contracts.Envelope`` puts no lower bound on ``version``, so before this a client could
    open a store at v0 (which sealed.envelopes' ``envelopes_version_positive`` CHECK rejects,
    turning a request body into a 500) or at v9999, permanently poisoning the monotonic rule
    for that store because nothing may go backwards from it again.
    """
    versions = EnvelopeVersions()
    assert versions.put("s-zero", _envelope("s-zero", version=0)).version == 1
    assert versions.put("s-huge", _envelope("s-huge", version=9999)).version == 1
    # Control: the derivation still climbs normally from there.
    assert versions.put("s-huge", _envelope("s-huge", max_discount_pct=5.0)).version == 2
