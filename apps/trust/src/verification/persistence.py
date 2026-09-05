"""Where a verification outcome stops being a value and becomes rows (T-065, T-256).

T-065's objective has two halves. The first — decide a claim — lives in
``packages/verification`` and is reached through this package's :func:`verify` shim. The
second — *persist the result, and emit the observation the scorer folds* — had no code at
all: ``db/migrations/0002_ledger_tables.sql`` reserves five tables for it, and until this
module the only ``INSERT`` into ``ledger.*`` anywhere in the product tree targeted
``ledger.commerce_events``. That is what T-256 measured, and it is why T-065's acceptance
item 2 ("re-running the same (claim, snapshot, verifier version) writes nothing") was being
graded as pure-function idempotency: a function that writes nothing satisfies it for free.

The five tables, and what one call puts in each
-----------------------------------------------
======================================  =============================================
``ledger.claims``                       the claim itself, upserted on its
                                        ``(store_id, claim_ref)`` identity
``ledger.claim_verifications``          this verifier's outcome for it, arbitrated by
                                        ``claim_verifications_idempotency_key``
``ledger.verification_evidence_refs``   what the verifier looked at
``ledger.trust_observations``           the observation :func:`trust.scoring.score`
                                        folds, routed through the approved
                                        ``claim_type -> dimension`` table
``ledger.trust_scores``                 the store's score after folding it in
======================================  =============================================

Idempotency is the DATABASE's, not this module's
------------------------------------------------
There is no memo, no ``seen`` set, no process-local state anywhere below — deliberately. A
replay is detected by asking Postgres: the ``claim_verifications`` insert carries
``ON CONFLICT ON CONSTRAINT claim_verifications_idempotency_key DO NOTHING ... RETURNING
verification_id``, so it yields a row the first time that ``(claim_id,
catalog_snapshot_id, verifier_version)`` arrives and **nothing** afterwards. That empty
answer is the signal, and everything downstream of it is skipped.

This matters most for ``ledger.trust_observations``, which is the one table here with no
arbiter of its own: its only unique index is the primary key on ``observation_id``, declared
``uuid ... default gen_random_uuid()`` and therefore fresh on every INSERT. A bare
``ON CONFLICT DO NOTHING`` there would be decoration — measured on a live database, three
identical calls leave three rows with the clause in place. So the defence for that table is
not to emit the second INSERT at all, which is the only defence that actually holds. A
``UNIQUE (verification_id, dim, observation_type)`` would let the database arbitrate it
instead; until one exists this writer's discipline is what stands between a redelivered
queue message and a silently double-counted trust observation.

What this module does NOT do, stated rather than hidden
--------------------------------------------------------
It does not re-read a store's observation history. ``score`` is computed over the
observations the CALLER hands it (``history``) plus the one this call just recorded, so a
caller that passes no history gets the score implied by this verification alone. That is the
honest shape for a seam with one verification in hand; the whole-history recomputation is
:func:`trust.snapshot.build_snapshot`'s job, and it writes ``low_data`` and ``blacklisted``,
which is why this module names neither column and leaves both to their DDL defaults on
insert and to the snapshot builder on update.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_PROVENANCE_SOURCE",
    "DEFAULT_SNAPSHOT_VERSION",
    "PersistedVerification",
    "persist_claim_verification",
]

#: What a claim's provenance is when the caller does not say. ``seller_asserted`` is the
#: weakest member of ``claims_provenance_source_check`` — a claim of unstated origin is a
#: claim the store made about itself, which is the assumption that cannot over-credit it.
DEFAULT_PROVENANCE_SOURCE = "seller_asserted"

#: ``ledger.trust_scores`` is keyed ``(store_id, snapshot_version)``. One is the live row.
DEFAULT_SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class PersistedVerification:
    """What one call wrote, and whether it wrote anything at all.

    ``written`` is False exactly when the database refused the verification as a replay, in
    which case ``verification_id`` is None and nothing downstream of it was emitted. A caller
    can therefore tell "this was already recorded" from "this is new" without asking again.
    """

    claim_id: Any
    verification_id: Any
    dim: str
    observation_type: str
    written: bool
    score: float | None = None
    confidence: float | None = None


_CLAIM_UPSERT = """
insert into ledger.claims (
    store_id, claim_ref, claim_type, key, value,
    provenance_source, provenance_ref, authority_rank, observed_at
)
values (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
on conflict on constraint claims_store_claim_ref_key
do update set claim_type = excluded.claim_type,
              key = excluded.key,
              value = excluded.value,
              provenance_source = excluded.provenance_source,
              provenance_ref = excluded.provenance_ref,
              authority_rank = excluded.authority_rank,
              observed_at = excluded.observed_at
returning claim_id
"""

# DO NOTHING rather than DO UPDATE, and RETURNING is load-bearing: the empty answer on a
# replay is the ONLY signal this module uses to know it has seen this outcome before.
_VERIFICATION_INSERT = """
insert into ledger.claim_verifications (
    claim_id, catalog_snapshot_id, verifier_version, status,
    observed_value, confidence, dim, verified_at
)
values (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
on conflict on constraint claim_verifications_idempotency_key do nothing
returning verification_id
"""

_EVIDENCE_INSERT = """
insert into ledger.verification_evidence_refs (
    verification_id, evidence_ref, source_class, observed_at
)
values (%s, %s, %s, %s)
on conflict on constraint verification_evidence_refs_unique do nothing
"""

# No ON CONFLICT clause on purpose. This table has no unique index over its real columns for
# one to arbitrate with, so the clause would be decoration; the guard is that this statement
# is only reached when the verification insert above proved the outcome was new.
_OBSERVATION_INSERT = """
insert into ledger.trust_observations (
    verification_id, store_id, dim, observation_type, weight, observed_at
)
values (%s, %s, %s, %s, %s, %s)
"""

# `low_data` and `blacklisted` are deliberately absent from both the column list and the
# update set: they are the snapshot builder's answers, derived from clean-episode counts and
# the blacklist registry, neither of which a single verification can see. On insert they take
# their DDL defaults (true / false), which is the conservative reading for a store this seam
# has just met; on update they keep whatever the builder last wrote.
_SCORE_UPSERT = """
insert into ledger.trust_scores (
    store_id, snapshot_version, score, confidence,
    effective_sample_size, score_version, dims, as_of
)
values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
on conflict on constraint trust_scores_pkey
do update set score = excluded.score,
              confidence = excluded.confidence,
              effective_sample_size = excluded.effective_sample_size,
              score_version = excluded.score_version,
              dims = excluded.dims,
              as_of = excluded.as_of,
              computed_at = now()
"""


def _one(cursor: Any) -> Any:
    """The single value a ``RETURNING`` clause read back, or None when it returned no row."""
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    if isinstance(row, (list, tuple)):
        return row[0] if row else None
    return row


def persist_claim_verification(
    *,
    connection: Any,
    store_id: str,
    claim_ref: str,
    claim_type: str,
    key: str,
    status: str,
    confidence: float,
    catalog_snapshot_id: Any,
    verifier_version: str,
    observed_at: Any,
    evidence_refs: Sequence[str] = (),
    value: Any = None,
    observed_value: Any = None,
    provenance_source: str = DEFAULT_PROVENANCE_SOURCE,
    provenance_ref: str | None = None,
    authority_rank: int = 0,
    weight: float | None = None,
    source_class: str | None = None,
    history: Iterable[Mapping[str, Any]] = (),
    snapshot_version: int = DEFAULT_SNAPSHOT_VERSION,
) -> PersistedVerification:
    """Persist one verification outcome across the five tables it was specified for.

    Args:
        connection: a DB-API connection — anything with ``cursor()`` and ``commit()``.
            Injected rather than resolved here so the caller owns the transaction boundary.
        store_id: the store the claim was made by.
        claim_ref: the claim's identity within that store. ``(store_id, claim_ref)`` is what
            ``claims_store_claim_ref_key`` upserts on.
        claim_type: a member of ``claims_claim_type_check``; routed to a trust dimension
            through the human-approved table, and raising if unmapped.
        key: the claim's key, e.g. ``price.value``.
        status: one of ``claim_verification.VERIFICATION_STATUSES``. It is both the
            verification's status and the observation's type — the same word in both
            vocabularies, which is why the migration's two CHECK constraints overlap.
        confidence: the verifier's confidence, in ``[0, 1]``.
        catalog_snapshot_id: the catalog snapshot verified against. A uuid: with anything
            else live Postgres answers ``invalid input syntax for type uuid``.
        verifier_version: which verifier decided it. Part of the idempotency key, so a
            verifier bump legitimately re-verifies.
        observed_at: when. Explicit, never a clock — the scorer decays against it and a
            replay has to reproduce the same number.
        evidence_refs: what the verifier looked at. Each becomes one evidence row.
        value: the claim's asserted value (jsonb).
        observed_value: what the catalog actually said (jsonb).
        provenance_source: where the claim came from; see :data:`DEFAULT_PROVENANCE_SOURCE`.
        provenance_ref: an optional pointer to that source.
        authority_rank: how much this provenance outranks another for the same key.
        weight: the per-observation weight in ``[0, 1]`` (R14). None means exactly 1.0.
        source_class: the provenance class of the evidence rows.
        history: observations already known for this store, folded in alongside the new one
            before the score is written. Empty means "score this verification alone" — see
            the module docstring on what that does and does not claim.
        snapshot_version: which ``ledger.trust_scores`` row this updates.

    Returns:
        :class:`PersistedVerification`. ``written`` is False when the database recognised the
        call as a replay, and in that case nothing after the verification insert ran.

    Raises:
        UnmappedClaimType: ``claim_type`` is absent from the approved table. Loud on purpose
            — a silent default would score the store on a dimension no human approved.
    """
    import json

    from ..scoring import SCORE_VERSION, claim_dimension, score

    dim = claim_dimension(claim_type)
    observation: dict[str, Any] = {
        "store_id": str(store_id),
        "dim": dim,
        "type": str(status),
        "observed_at": observed_at,
    }
    if weight is not None:
        observation["weight"] = float(weight)

    with connection.cursor() as cursor:
        cursor.execute(
            _CLAIM_UPSERT,
            (
                str(store_id),
                str(claim_ref),
                str(claim_type),
                str(key),
                json.dumps(value),
                str(provenance_source),
                provenance_ref,
                int(authority_rank),
                observed_at,
            ),
        )
        claim_id = _one(cursor)

        cursor.execute(
            _VERIFICATION_INSERT,
            (
                claim_id,
                str(catalog_snapshot_id),
                str(verifier_version),
                str(status),
                json.dumps(observed_value),
                float(confidence),
                dim,
                observed_at,
            ),
        )
        verification_id = _one(cursor)
        if verification_id is None:
            # The database refused it: this exact (claim, snapshot, verifier version) is
            # already recorded. T-065 acceptance 2 is about ROWS, so we write no more.
            connection.commit()
            return PersistedVerification(
                claim_id=claim_id,
                verification_id=None,
                dim=dim,
                observation_type=str(status),
                written=False,
            )

        for evidence_ref in evidence_refs or ():
            cursor.execute(
                _EVIDENCE_INSERT,
                (verification_id, str(evidence_ref), source_class, observed_at),
            )

        cursor.execute(
            _OBSERVATION_INSERT,
            (
                verification_id,
                str(store_id),
                dim,
                str(status),
                None if weight is None else float(weight),
                observed_at,
            ),
        )

        folded = score([*history, observation], as_of=observed_at)
        cursor.execute(
            _SCORE_UPSERT,
            (
                str(store_id),
                int(snapshot_version),
                float(folded["score"]),
                float(folded["confidence"]),
                float(folded.get("effective_evidence") or 0.0),
                str(folded.get("score_version") or SCORE_VERSION),
                json.dumps(folded["dims"]),
                observed_at,
            ),
        )

    connection.commit()
    return PersistedVerification(
        claim_id=claim_id,
        verification_id=verification_id,
        dim=dim,
        observation_type=str(status),
        written=True,
        score=float(folded["score"]),
        confidence=float(folded["confidence"]),
    )
