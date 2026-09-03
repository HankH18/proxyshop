"""Fixtures for the T-034 exposure-bandit gate (``test_bandit.py``).

Owned by T-034. Auto-loaded into ``apps/exchange/tests/conftest.py`` by
``proxyshop_support.fixture_loader``, so every fixture here is prefixed ``bandit_`` —
seven tickets share this directory and the loader poisons a duplicated name.

The trust-snapshot shape built here is deliberately the same shape the frozen acceptance
suite builds (``.swarm-loop/acceptance/test_e3_exchange.py::_trust_snapshot``): a mapping
of ``store_id`` to a record carrying ``score``, ``confidence``, per-dimension Beta
parameters, ``blacklisted`` and ``low_data``. Keeping the two in step is the point — a
gate that feeds a friendlier shape than the grader does is a gate that proves nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

#: Fixed clock value. Never ``time.time()`` — see the root conftest (D38).
T_PAST = 1_600_000_000.0

#: The six R12 trust dimensions, in the order the snapshot publishes them.
TRUST_DIMENSIONS = (
    "price_honored",
    "discount_honored",
    "shipped_on_time",
    "not_returned",
    "feedback_match",
    "catalog_claim_accuracy",
)


def build_trust_snapshot(
    store_ids,
    *,
    scores: dict[str, float] | None = None,
    blacklisted=(),
    low_data=(),
    drop_keys: dict[str, tuple[str, ...]] | None = None,
    none_keys: dict[str, tuple[str, ...]] | None = None,
    omit_stores=(),
) -> dict[str, Any]:
    """Build a trust snapshot for ``store_ids``.

    Args:
        store_ids: every store the snapshot should describe.
        scores: per-store trust score in [0, 1]; anything unnamed gets a neutral 0.5.
        blacklisted: store ids whose ``blacklisted`` flag is set.
        low_data: store ids whose ``low_data`` flag is set.
        drop_keys: per-store keys to delete from the record entirely — the
            fail-closed probe for a snapshot that never answered the question.
        none_keys: per-store keys to set to ``None`` — the fail-closed probe for a
            snapshot whose answer was "unknown".
        omit_stores: store ids left out of the snapshot altogether.
    """
    scores = scores or {}
    drop_keys = drop_keys or {}
    none_keys = none_keys or {}
    snapshot: dict[str, Any] = {}
    for sid in store_ids:
        if sid in omit_stores:
            continue
        record: dict[str, Any] = {
            "store_id": sid,
            "score": float(scores.get(sid, 0.5)),
            "confidence": 0.4,
            "dims": {
                dim: {"alpha": 2.0, "beta": 2.0, "decayed_at": T_PAST}
                for dim in TRUST_DIMENSIONS
            },
            "blacklisted": sid in blacklisted,
            "low_data": sid in low_data,
        }
        for key in drop_keys.get(sid, ()):
            record.pop(key, None)
        for key in none_keys.get(sid, ()):
            record[key] = None
        snapshot[sid] = record
    return snapshot


def build_outcomes(rounds: int, winners, losers=(), cluster_id: str = "cluster-1"):
    """``rounds`` repetitions of one conversion for each winner and one loss per loser."""
    outcomes = []
    for _ in range(rounds):
        for sid in winners:
            outcomes.append({"store_id": sid, "cluster_id": cluster_id, "converted": True})
        for sid in losers:
            outcomes.append({"store_id": sid, "cluster_id": cluster_id, "converted": False})
    return outcomes


@pytest.fixture
def bandit_trust_snapshot():
    """Factory for a trust snapshot in the frozen suite's shape."""
    return build_trust_snapshot


@pytest.fixture
def bandit_outcomes():
    """Factory for a list of conversion outcomes."""
    return build_outcomes
