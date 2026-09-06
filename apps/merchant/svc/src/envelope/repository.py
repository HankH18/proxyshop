"""The persistence seam behind :class:`~merchant_svc.envelope.store.EnvelopeVersions` (T-239).

Two implementations of one protocol, exactly as ``buyer_svc.vault.store`` does it for the
pseudonym history:

* :class:`InMemoryEnvelopeRepository` — the default. Process-local, and the only thing a
  merchant service booted without a database ever gets. Behaviour is byte-for-byte what the
  store did before this module existed.
* :class:`PostgresEnvelopeRepository` — reads and writes ``sealed.envelopes``, the table
  ``db/migrations/0003_sealed_vault_app_tables.sql`` already ships and where DESIGN puts the
  real history. It takes a **live connection rather than a DSN**, so the caller decides which
  role it authenticates as; ``sealed.*`` is seller strategy and has no grant for the exchange
  role (S7), and a service handed the wrong connection gets ``InsufficientPrivilege`` on the
  first statement rather than a quietly degraded envelope store.

Why the seam is shaped ``load`` + ``persist`` and nothing else
--------------------------------------------------------------
The three invariants the envelope history exists for — **append-only**, **a version never
goes backwards**, **activation is bound to the head** — are properties of the *history*, not
of any one row. They are therefore enforced in exactly one place,
:class:`~merchant_svc.envelope.store.EnvelopeVersions`, over whatever this seam hands back.
A repository that could also *edit* or *delete* would be a second place the append-only rule
had to be re-stated, and the second statement of a rule is where it drifts.

``persist`` is called **before** the in-memory append, inside the store's lock. A write that
fails therefore leaves the process's history unchanged rather than one version ahead of the
durable one — a store that believes it filed v4 while the table stops at v3 is the exact
"stale writer reinstates replaced limits" failure the version rule exists to prevent.

The approval crosses the boundary too (T-350)
----------------------------------------------
``sealed.envelopes`` carries five ``approval_*`` columns mirroring
:class:`~merchant_svc.envelope.model.ApprovalArtifact` field for field, and a CHECK,
``envelopes_active_requires_approval``, that makes the three mandatory ones NOT NULL for any
row asserting ``active``. That is the same rule
``merchant_svc.envelope.store._refuse_unapproved_activation`` enforces in memory (R6, T-248),
now stated in the one place a bug in this module cannot get around.

It used to be the other way: the table had nowhere to put the artifact, so every row read back
as ``active`` was downgraded to ``shadow`` and an approved envelope silently stopped bidding
across a restart. :func:`restorable` still makes exactly that downgrade — it is the fail-safe
for a row written before the migration, or by something that is not this module — but it is no
longer the *only* thing that can happen to a live row. A row that carries its approval comes
back live — and only if the artifact still *covers* the terms it came back with, which
:func:`restorable` re-checks, so a hash that survived as text while the terms changed
underneath it lands in ``shadow`` rather than being believed.

Reading a row that predates the migration is deliberately still possible: the approval columns
are treated as absent-means-no-approval rather than as a shape error, so the fail-safe applies
to it instead of an exception. The eight columns that identify and describe a version are still
required, because a row missing one of *those* is not a ``sealed.envelopes`` row at all.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol, runtime_checkable

from .digest import approval_covers, approval_digest
from .model import ACTIVE, SHADOW, ApprovalArtifact, Envelope, EnvelopeInvalid

__all__ = [
    "ENVELOPES_TABLE",
    "EnvelopeRepository",
    "InMemoryEnvelopeRepository",
    "PostgresEnvelopeRepository",
    "restorable",
]

_log = logging.getLogger(__name__)

#: The table DESIGN puts the real history in. Named once, so the two statements below and any
#: future reader cannot drift on which table this is.
ENVELOPES_TABLE = "sealed.envelopes"

#: The eight columns that identify a version and describe its terms. A row missing one of
#: these is not a ``sealed.envelopes`` row and is refused as such.
_CORE_COLUMNS: tuple[str, ...] = (
    "store_id",
    "version",
    "activation",
    "max_discount_pct",
    "budget_cap",
    "floors",
    "pursue_clusters",
    "standing_commitments",
)

#: The recorded written approval, column per :class:`ApprovalArtifact` field, in that class's
#: own :data:`~merchant_svc.envelope.model.ApprovalArtifact.FIELDS` order. Appended AFTER the
#: core eight and never interleaved: the parameter tuple's leading positions are asserted
#: verbatim by the existing hardening suite, and reordering them would move a version's terms
#: into the approval's columns.
_APPROVAL_COLUMNS: tuple[str, ...] = tuple(f"approval_{field}" for field in ApprovalArtifact.FIELDS)

#: The row shape, in the order every statement below uses it.
_COLUMNS: tuple[str, ...] = _CORE_COLUMNS + _APPROVAL_COLUMNS

_SELECT_HISTORY = f"select {', '.join(_COLUMNS)} from {ENVELOPES_TABLE} order by store_id, version"  # noqa: S608

#: Columns that carry the version's STATE, as opposed to the two that identify it. The
#: approval belongs here: ``activate`` files the artifact onto the version it approves and
#: ``kill`` carries it forward, both at the same version number, so both restate this row.
_STATE_COLUMNS: tuple[str, ...] = _COLUMNS[2:]

#: ``on conflict (store_id, version) do update`` is not a shortcut, it is what the table means.
#:
#: The first draft was a plain INSERT, on the reasoning that an append-only history should
#: never overwrite. Measured against the real table, that made TWO of the three lifecycle
#: transitions impossible: ``activate`` and ``kill`` deliberately do NOT bump the version
#: (versions.py — an approval is bound to a digest of *this* version's terms, so minting a new
#: version at activation time would produce a live document its own approval no longer covers),
#: and ``envelopes_pkey PRIMARY KEY (store_id, version)`` then rejected the second row.
#: ``POST /stores/{id}/kill`` raised psycopg's UniqueViolation, which ``onboarding/routes.py``
#: does not catch, so the kill switch answered 500 — the one control that must always work.
#:
#: So the table stores **the current state of each version**, and the in-memory history stores
#: **every transition**. That is a real difference and it is the schema's, not this module's:
#: ``sealed.envelopes`` has one row per (store, version) and no ordering column, so a
#: transition log cannot be expressed in it. The terms columns are in the SET list too, but
#: they cannot actually change for a fixed version — ``edit_envelope`` always bumps — so the
#: update rewrites a version's LIFECYCLE and never its approved terms.
_UPSERT_VERSION = (
    f"insert into {ENVELOPES_TABLE} ({', '.join(_COLUMNS)}) "  # noqa: S608
    f"values ({', '.join(['%s'] * len(_COLUMNS))}) "
    "on conflict (store_id, version) do update set "
    + ", ".join(f"{column} = excluded.{column}" for column in _STATE_COLUMNS)
)

#: What ``numeric(6,3)`` and ``numeric(14,2)`` can hold exactly. A merchant's approved terms
#: must not change by being written down: the approval digest is taken over these numbers, so
#: a value the column silently rounds comes back as terms no approval on file covers, and R9
#: says old versions never change. Refused loudly at the boundary instead.
_NUMERIC_SCALE: dict[str, int] = {"max_discount_pct": 3, "budget_cap": 2}


@runtime_checkable
class EnvelopeRepository(Protocol):
    """Where an envelope history outlives the process that recorded it."""

    def load(self) -> Mapping[str, Sequence[Envelope]]:
        """Every store's history, oldest version first. ``{}`` when nothing is on file."""
        ...  # pragma: no cover - protocol

    def persist(self, envelope: Envelope) -> None:
        """File one version durably. Called before the in-memory append, under the lock."""
        ...  # pragma: no cover - protocol


def restorable(envelope: Envelope) -> Envelope:
    """``envelope`` as it may be brought back into a fresh process.

    An ``active`` version whose approval artifact did not survive the round trip comes back in
    ``shadow``. This is the one place that decision is made, so both repositories agree about
    what a restart means and a test against the fast implementation is evidence about the slow
    one.

    Since T-350 both implementations *can* keep the artifact, so this fires for one case
    rather than for every restored row: a row written before ``sealed.envelopes`` grew its
    ``approval_*`` columns, or by something that is not this module. The table now refuses to
    accept such a row (``envelopes_active_requires_approval``) and the migration downgraded the
    ones already on file — this is the belt to that pair of braces, and it stays because
    reinstating "live" on the strength of a row that lost its paperwork is exactly the
    caller-asserted activation T-248 closed.

    The second branch is the one the columns made necessary, and it is a **downgrade rather
    than a refusal on purpose.** A stored artifact that does not cover the terms it came back
    with means the row's approval and the row's terms disagree — someone changed a live
    envelope's terms in the table without re-approving them. ``record`` refuses that outright
    (T-248), because there is a caller to tell. On the way *in* there is nobody to tell and
    raising takes the whole ``EnvelopeVersions`` down with it, which means the merchant service
    cannot boot at all: no routes, and no kill switch, over ONE bad row for ONE store.
    Measured against ``proxyshop-postgres-1`` with a hand-written row, before this branch
    existed: ``ApprovalRejected`` out of ``EnvelopeVersions.__init__``. So it lands in
    ``shadow`` like the other unjustified row, loudly — the safe direction this module has
    always taken, and the only one that keeps stopping a store possible.
    """
    if envelope.activation != ACTIVE:
        return envelope
    approval = envelope.approval
    if approval is None:
        _log.warning(
            "store %r envelope v%d was stored active but its approval artifact did not survive "
            "the persistence boundary; restoring it in %s — re-approve to make it live again",
            envelope.store_id,
            envelope.version,
            SHADOW,
        )
        return envelope.with_activation(SHADOW, None)
    if not approval_covers(envelope, approval.envelope_hash):
        _log.warning(
            "store %r envelope v%d was stored active under an approval by %r bound to %s, "
            "which is not this version's terms (%s); the row's terms and its approval "
            "disagree, so nothing on file authorizes it being live — restoring it in %s",
            envelope.store_id,
            envelope.version,
            approval.approver,
            approval.envelope_hash,
            approval_digest(envelope),
            SHADOW,
        )
        return envelope.with_activation(SHADOW, None)
    return envelope


class InMemoryEnvelopeRepository:
    """The default backing store: a dict, and nothing outlives the process.

    It is a real implementation of the protocol rather than a ``None`` special case, so the
    store has exactly one code path and the seam is exercised by every existing test.
    """

    def __init__(self, history: Mapping[str, Iterable[Envelope]] | None = None) -> None:
        self._history: dict[str, list[Envelope]] = {
            str(store_id): list(versions) for store_id, versions in (history or {}).items()
        }

    def load(self) -> Mapping[str, Sequence[Envelope]]:
        return {store_id: tuple(versions) for store_id, versions in self._history.items()}

    def persist(self, envelope: Envelope) -> None:
        self._history.setdefault(envelope.store_id, []).append(envelope)


class PostgresEnvelopeRepository:
    """``sealed.envelopes``, reached through a caller-supplied connection.

    The connection carries the identity: ``sealed.*`` is seller strategy with no grant for the
    exchange role (S7/D5), and this class deliberately does nothing to soften what happens to
    a caller holding the wrong one.
    """

    def __init__(self, connection: Any, *, autocommit: bool = False) -> None:
        self._connection = connection
        self._autocommit = autocommit

    @property
    def connection(self) -> Any:
        return self._connection

    def _commit(self) -> None:
        if not self._autocommit:
            self._connection.commit()

    def _finish_read(self) -> None:
        """End the read's transaction.

        A ``SELECT`` on a non-autocommit connection opens a transaction and holds a snapshot.
        A merchant service that boots, loads the history and never writes would otherwise sit
        ``idle in transaction`` for the life of the process — measured — pinning the xmin
        horizon and blocking vacuum on a table nobody is using.
        """
        if not self._autocommit:
            self._connection.rollback()

    def _abandon_failed_write(self) -> None:
        """Roll a failed write back so the connection stays usable.

        Without this a single integrity error turns the envelope store into a brick:
        PostgreSQL aborts the transaction and answers every later statement with
        ``InFailedSqlTransaction`` — measured, and it took the *reload* and the kill switch
        down with it, not just the write that failed.
        """
        try:
            self._connection.rollback()
        except Exception:  # noqa: BLE001 - the original failure is the one worth raising
            _log.exception("could not roll back a failed envelope write")

    def load(self) -> Mapping[str, Sequence[Envelope]]:
        """Every version on file, oldest first per store, activation downgraded if unbacked."""
        history: dict[str, list[Envelope]] = {}
        try:
            with self._connection.cursor() as cur:
                cur.execute(_SELECT_HISTORY)
                rows = cur.fetchall()
        except Exception:
            self._abandon_failed_write()
            raise
        else:
            self._finish_read()
        for row in rows:
            document = _row_to_document(row)
            document["max_discount_pct"] = float(document["max_discount_pct"] or 0.0)
            document["budget_cap"] = float(document["budget_cap"] or 0.0)
            for jsonb in ("floors", "pursue_clusters", "standing_commitments"):
                document[jsonb] = _as_list(document[jsonb])
            # Explicit, never KEEP: the artifact is rebuilt from the row's own approval_*
            # columns, so "no approval columns" reads as no approval rather than as whatever
            # happened to be lying around in the document.
            envelope = Envelope.from_obj(document, approval=_recorded_approval(document))
            history.setdefault(envelope.store_id, []).append(restorable(envelope))
        return history

    def persist(self, envelope: Envelope) -> None:
        """File one version's current state, inserting it or updating the state it is in.

        See :data:`_UPSERT_VERSION` for why this is an upsert rather than the plain insert an
        append-only history suggests: ``activate`` and ``kill`` do not bump the version, and
        ``envelopes_pkey`` is ``(store_id, version)``, so an insert made the kill switch a 500.

        Raises:
            EnvelopeInvalid: a term the column cannot hold exactly, which would change the
                merchant's approved figures by writing them down.
        """
        document = envelope.to_dict()
        _refuse_terms_the_columns_would_round(document)
        # `or {}` and not a branch: an envelope with no approval writes five NULLs, which is
        # what `envelopes_approval_is_whole` means by "absent". Writing a partial artifact is
        # not reachable from here — `ApprovalArtifact` cannot be built without its three
        # mandatory fields — and the CHECK refuses it anyway.
        approval: Mapping[str, Any] = document["approval"] or {}
        values = (
            document["store_id"],
            document["version"],
            document["activation"],
            document["max_discount_pct"],
            document["budget_cap"],
            json.dumps(document["floors"]),
            json.dumps(document["pursue_clusters"]),
            json.dumps(document["standing_commitments"]),
            *(approval.get(field) for field in ApprovalArtifact.FIELDS),
        )
        try:
            with self._connection.cursor() as cur:
                cur.execute(_UPSERT_VERSION, values)
        except Exception:
            self._abandon_failed_write()
            raise
        self._commit()


def _row_to_document(row: Any) -> dict[str, Any]:
    """One result row as a column-keyed mapping, whatever row factory the caller wired.

    The connection is supplied by the caller by design, and ``dict_row`` is the common modern
    psycopg factory. Zipping a mapping against :data:`_COLUMNS` iterates its KEYS, and the
    lengths match, so ``strict=True`` does not catch it — every field silently becomes its own
    column name and the failure surfaces later as an unrelated float conversion error.

    The :data:`_CORE_COLUMNS` are required and the :data:`_APPROVAL_COLUMNS` are not, because
    those two absences mean different things. A row missing ``budget_cap`` did not come from
    this table. A row missing ``approval_approver`` came from this table **before T-350 added
    the column**, and the answer to it is the fail-safe that has always been there:
    :func:`restorable` restores it in ``shadow``. Refusing it instead would turn a rolling
    deploy — new code, old schema — into a merchant service that cannot boot.
    """
    if isinstance(row, Mapping):
        missing = [column for column in _CORE_COLUMNS if column not in row]
        if missing:
            raise EnvelopeInvalid(
                f"a {ENVELOPES_TABLE} row is missing column(s) {missing}; the table does not "
                "have the shape this repository was written against"
            )
        return {column: row.get(column) for column in _COLUMNS}
    values = tuple(row)
    if len(values) == len(_CORE_COLUMNS):
        document = dict(zip(_CORE_COLUMNS, values, strict=True))
        document.update(dict.fromkeys(_APPROVAL_COLUMNS))
        return document
    return dict(zip(_COLUMNS, values, strict=True))


def _recorded_approval(document: Mapping[str, Any]) -> dict[str, Any] | None:
    """The written approval this row records, or ``None`` when it records none.

    ``None`` means *nothing at all was written down* — every approval column NULL, which is a
    shadow row, a killed row that was never approved, or a row that predates the columns.
    Anything else is handed to :meth:`ApprovalArtifact.parse` as-is, so a row carrying half an
    artifact is refused by the model, loudly and by name, rather than quietly restored as an
    approval with a missing approver. ``envelopes_approval_is_whole`` makes that unreachable
    through the table; a hand-edited database is not the table's problem to catch twice.
    """
    recorded = {
        field: _approval_field(document.get(column))
        for field, column in zip(ApprovalArtifact.FIELDS, _APPROVAL_COLUMNS, strict=True)
    }
    if all(value is None for value in recorded.values()):
        return None
    return {field: value for field, value in recorded.items() if value is not None}


def _approval_field(value: Any) -> Any:
    """One ``approval_*`` column as the text :class:`ApprovalArtifact` expects.

    ``approval_approved_at`` is ``timestamptz``, which is the honest type for an instant and
    is what lets the database order and compare it — but psycopg hands it back as a
    ``datetime``, and the artifact's ``approved_at`` is ISO-8601 text. Rendering it here in
    UTC produces exactly what :func:`~merchant_svc.envelope.model._utc_timestamp` produced on
    the way in, so the round trip is the identity for every instant the model can hold (it
    truncates to microseconds at parse time, which is also ``timestamptz``'s resolution).
    Nothing in the approval digest covers ``approved_at``, so this cannot move a hash — but an
    approval record whose date changed by being written down is still not the record.
    """
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def _refuse_terms_the_columns_would_round(document: Mapping[str, Any]) -> None:
    """Refuse a term the numeric column cannot hold exactly.

    ``max_discount_pct numeric(6,3)`` and ``budget_cap numeric(14,2)`` round on the way in.
    Measured: 10.0005 and 100.005 came back as 10.001 and 100.01, and the restored version's
    approval digest no longer matched the recorded one — so the merchant's approved terms had
    been changed by the act of storing them, which is precisely what R9 forbids and what an
    approval artifact exists to make impossible.

    Silently rounding is the one option that is not available. Refusing is loud, recoverable,
    and names the column; the alternative is a live envelope whose terms nobody approved.
    """
    for field, scale in _NUMERIC_SCALE.items():
        value = document.get(field)
        if value is None:
            continue
        quantized = Decimal(str(value)).quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
        if quantized != Decimal(str(value)):
            raise EnvelopeInvalid(
                f"{ENVELOPES_TABLE}.{field} holds {scale} decimal places and would store "
                f"{value!r} as {quantized}; an approved term that changes by being written "
                "down is no longer the term the merchant approved (R9), so it is refused "
                "rather than rounded"
            )


def _as_list(value: Any) -> list[Any]:
    """A jsonb column as a list, whether the driver already decoded it or handed back text."""
    if value is None:
        return []
    if isinstance(value, str | bytes | bytearray):
        decoded = json.loads(value)
        return list(decoded) if isinstance(decoded, list) else []
    return list(value)
