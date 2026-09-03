"""Fixtures for T-042 — the store-agent learning loop.

Owned by T-042. Loaded into ``packages/store-agent/tests/conftest.py`` automatically by
``proxyshop_support.fixture_loader``, which is why this file is named ``_fixtures_learning.py``
and why every fixture here is prefixed ``learning_``: seven tickets share this directory and a
bare name like ``outcome_records`` is a collision waiting to happen.

Everything here is plain data and plain stdlib. No network, no clock, no unseeded randomness —
the module under test is a pure function of its inputs and these fixtures keep it that way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

#: The cluster every fixture record belongs to.
LEARNING_CLUSTER = "cluster-warm-layers"

#: The store whose own outcomes are being learned from.
LEARNING_STORE_ID = "store-alpha"

#: A *different* store, used to prove own-outcome filtering actually filters.
LEARNING_OTHER_STORE_ID = "store-beta"

LEARNING_LIST_PRICE = 100.0


class KeyAccessSpy(Mapping):
    """A read-only mapping that records every key anybody asks it for.

    This is the instrument behind the blindness proof. Equality of two prior builds
    (with and without discount fields) shows the discount fields did not *change* the
    answer; this shows they were never *read*, which is the stronger claim and the one
    R17 actually makes.

    Iteration is logged too, under the sentinel :data:`ITER`. ``dict(record)``,
    ``record.items()`` and ``**record`` all go through ``keys()``/``__iter__``, so a
    builder that slurps the whole record instead of naming the fields it wants is caught
    here even though it never mentions a discount key by name.
    """

    #: Logged when the mapping is enumerated rather than asked for a named key.
    ITER = "<iterated>"

    def __init__(self, data: Mapping[str, Any], log: list[str]) -> None:
        self._data = dict(data)
        self._log = log

    def __getitem__(self, key: Any) -> Any:
        self._log.append(str(key))
        return self._data[key]

    def __contains__(self, key: Any) -> bool:
        self._log.append(str(key))
        return key in self._data

    def __iter__(self):
        self._log.append(self.ITER)
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"KeyAccessSpy({self._data!r})"


def _prior_record(index: int) -> dict[str, Any]:
    """One cross-store pitch outcome, carrying discount fields the prior must not read.

    Deliberately the same shape the frozen acceptance fixture uses, so this gate and the
    frozen goal are grading the same builder against the same evidence.
    """
    won = index % 3 != 0
    return {
        "cluster_id": LEARNING_CLUSTER,
        "store_id": f"store-{index % 4}",
        "value_prop": "durability" if won else "price",
        "pitch_claims": ["free_returns", "ships_within"],
        "commitments": ["free_returns"],
        "won": won,
        # Everything below is discount evidence. The network prior must be blind to all of it.
        "discount_depth": 0.2 if won else 0.05,
        "discount_pct": 20.0 if won else 5.0,
        "offer": {
            "unit_price": LEARNING_LIST_PRICE,
            "discount": {"type": "percentage", "value": 20.0 if won else 5.0},
        },
    }


def _strip_discounts(node: Any) -> Any:
    """Recursively drop every key whose name mentions a discount."""
    if isinstance(node, Mapping):
        return {k: _strip_discounts(v) for k, v in node.items() if "discount" not in str(k).lower()}
    if isinstance(node, list):
        return [_strip_discounts(v) for v in node]
    return node


def _outcomes(
    win_depth: float,
    loss_depth: float,
    n: int = 40,
    store_id: str = LEARNING_STORE_ID,
) -> list[dict[str, Any]]:
    """Own-store outcomes: `win_depth` converted, `loss_depth` did not."""
    records: list[dict[str, Any]] = []
    for i in range(n):
        records.append(
            {
                "cluster_id": LEARNING_CLUSTER,
                "store_id": store_id,
                "discount_depth": win_depth,
                "commitments": ["free_returns"],
                "won": True,
                "order_ref": f"ord-win-{i:03d}",
            }
        )
        records.append(
            {
                "cluster_id": LEARNING_CLUSTER,
                "store_id": store_id,
                "discount_depth": loss_depth,
                "commitments": ["free_returns"],
                "won": False,
                "order_ref": f"ord-loss-{i:03d}",
            }
        )
    return records


@pytest.fixture
def learning_cluster() -> str:
    return LEARNING_CLUSTER


@pytest.fixture
def learning_store_id() -> str:
    return LEARNING_STORE_ID


@pytest.fixture
def learning_other_store_id() -> str:
    return LEARNING_OTHER_STORE_ID


@pytest.fixture
def learning_prior_records() -> list[dict[str, Any]]:
    """24 cross-store pitch outcomes, each carrying three separate discount fields."""
    return [_prior_record(i) for i in range(24)]


@pytest.fixture
def learning_strip_discounts():
    """The recursive discount-field stripper, as a callable."""
    return _strip_discounts


@pytest.fixture
def learning_outcomes():
    """Factory: ``learning_outcomes(win_depth, loss_depth, n=40, store_id=...)``."""
    return _outcomes


@pytest.fixture
def learning_key_spy():
    """Factory: ``learning_key_spy(records) -> (spied_records, access_log)``."""

    def build(records):
        log: list[str] = []
        return [KeyAccessSpy(r, log) for r in records], log

    return build


@pytest.fixture
def learning_spy_class():
    """The :class:`KeyAccessSpy` type itself, for its ``ITER`` sentinel."""
    return KeyAccessSpy
