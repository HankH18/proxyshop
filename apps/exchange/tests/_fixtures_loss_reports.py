"""Fixtures for T-035's loss reports. Owned by T-035.

Loaded into ``apps/exchange/tests/conftest.py`` by ``proxyshop_support.fixture_loader``; that
conftest is orchestrator-owned and frozen. Every name here is prefixed ``loss_report_``
because two sibling ``_fixtures_*.py`` files defining one name poison that fixture for the
whole directory.

The record builder deliberately carries MORE than the frozen acceptance fixture does: a
nested ``offer`` with several money fields, a rival identity, and an ``extras`` mapping the
caller fills with fields the report's projection has never heard of. That last one is the
point of the gate — a suppression that works only for the amount fields somebody remembered
to blacklist is the failure mode this ticket exists to rule out, so the tests must be able to
invent a money field on the spot and watch it fail to reach the report.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

# Epoch seconds. Deliberately NOT the frozen suite's T_NOW: a builder that happened to work
# only against that one instant would look green here for the wrong reason.
LOSS_REPORT_NOW = 1_712_000_000.0


def _loss_record(
    auction_id: str,
    cluster_id: str,
    reason: str,
    ts: float | str,
    *,
    store_id: str = "store-loser",
    unmet: tuple[str, ...] = (),
    rival: str = "store-rival-alpha",
    unit_price: float = 189.95,
    winning_price: float = 142.25,
    won: bool = False,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One auction-log row as the exchange writes it: everything, amounts included."""
    record: dict[str, Any] = {
        "auction_id": auction_id,
        "cluster_id": cluster_id,
        "ts": ts,
        "store_id": store_id,
        "won": won,
        "reason": reason,
        "unmet_criteria": list(unmet),
        "offer": {
            "unit_price": unit_price,
            "total_price": unit_price,
            "discount": {"type": "percentage", "value": 12.5},
        },
        "winning_price": winning_price,
        "rival_store_id": rival,
    }
    if extras:
        record.update(extras)
    return record


@pytest.fixture
def loss_report_record() -> Callable[..., dict[str, Any]]:
    """Factory for a single auction-log row."""
    return _loss_record


@pytest.fixture
def loss_report_window() -> dict[str, float]:
    """The hour ending at :data:`LOSS_REPORT_NOW`, as epoch seconds."""
    return {"start": LOSS_REPORT_NOW - 3600.0, "end": LOSS_REPORT_NOW}


@pytest.fixture
def loss_report_log() -> list[dict[str, Any]]:
    """Two stores, three clusters, one row outside the window and one win.

    Golden counts, for the store ``store-loser``:

    ==========  ====  ===  =====  ===========  =====
    cluster     lost  fit  price  commitments  trust
    ==========  ====  ===  =====  ===========  =====
    cluster-a   3     2    1      0            0
    cluster-b   2     0    0      1            1
    ==========  ====  ===  =====  ===========  =====

    and for ``store-other``: ``cluster-a`` lost 1, all of it ``price``.
    """
    now = LOSS_REPORT_NOW
    return [
        _loss_record("a-1", "cluster-a", "fit", now - 3000.0, unmet=("capacity_l >= 30",)),
        _loss_record("a-2", "cluster-a", "fit", now - 2500.0, unmet=("capacity_l >= 30",)),
        _loss_record("a-3", "cluster-a", "price", now - 2000.0, rival="store-rival-beta"),
        _loss_record("b-1", "cluster-b", "commitments", now - 900.0, unmet=("ships_in_days <= 2",)),
        _loss_record("b-2", "cluster-b", "trust", now - 800.0),
        # A win: not a loss, must not be counted.
        _loss_record("a-4", "cluster-a", "price", now - 700.0, won=True),
        # Before the window opens: must not be counted.
        _loss_record("a-5", "cluster-a", "fit", now - 7200.0, unmet=("capacity_l >= 30",)),
        # A second store: its own report, its own counts.
        _loss_record("c-1", "cluster-a", "price", now - 600.0, store_id="store-other"),
    ]
