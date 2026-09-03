"""Shared fixtures for the E6 trust lane (T-061, T-062, T-063, T-064, T-065).

Every fixture name here is prefixed ``e6_``. ``apps/trust/tests/conftest.py`` merges every
sibling ``_fixtures_*.py`` into ONE flat namespace with no reserved-name registry, and seven
tickets share this directory — ``events_*`` belongs to T-060 and ``ledger_*`` to T-011, so
this lane takes ``e6_`` and nothing else. A collision here does not fail only the offending
test; it poisons a name for the whole directory (see ``proxyshop_support.fixture_loader``).

Everything in this file is pure data. Nothing here touches Postgres, Redis, Neo4j, a socket
or a clock: the E6 product surface is deliberately I/O-free, and a fixture that opened a
connection would make these tests slower and less deterministic than the code they grade.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

#: The fixed instant every E6 computation is pinned to. Decay is a function of ``as_of`` and
#: never of the wall clock, so a test that used ``now()`` would grade a different scorer on
#: every run and could never assert an exact alpha or beta.
E6_AS_OF = "2026-01-01T00:00:00Z"

#: The six trust dimensions, restated here rather than imported so that a test comparing the
#: engine's published vocabulary against this list is comparing against something the engine
#: did not author. Importing ``TRUST_DIMENSIONS`` and asserting it equals itself is vacuous.
E6_DIMENSIONS = (
    "price_honored",
    "discount_honored",
    "shipped_on_time",
    "not_returned",
    "feedback_match",
    "catalog_claim_accuracy",
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def e6_observation(store_id: str, dim: str, otype: str, observed_at: str = E6_AS_OF) -> dict:
    """One trust observation, in the shape ``score`` and ``replay`` both use."""
    return {"store_id": store_id, "dim": dim, "type": otype, "observed_at": observed_at}


def e6_event(event_id: str, kind: str, **fields: Any) -> dict:
    """A plain ledger event record (DESIGN §Interfaces shape, minus the chain fields)."""
    event = {
        "event_id": event_id,
        "ts": fields.pop("ts", E6_AS_OF),
        "kind": kind,
        "payload": dict(fields.pop("payload", {}) or {}),
    }
    event.update({key: value for key, value in fields.items() if value is not None})
    return event


@pytest.fixture
def e6_as_of() -> str:
    """The pinned reference instant."""
    return E6_AS_OF


@pytest.fixture
def e6_dims() -> tuple[str, ...]:
    """The six trust dimensions, independently spelled out."""
    return E6_DIMENSIONS


@pytest.fixture
def e6_obs():
    """A factory for one trust observation."""
    return e6_observation


@pytest.fixture
def e6_make_event():
    """A factory for one plain ledger event."""
    return e6_event


@pytest.fixture(scope="session")
def e6_manifest() -> dict:
    """The human-approved manifest, read straight off disk.

    Read here rather than through ``trust.scoring.manifest`` on purpose: a test that asked
    the engine for the manifest and then compared the engine against it would pass no matter
    what the engine did. Ground truth has to come from outside the thing being graded.
    """
    path = _REPO_ROOT / "fixtures" / "manifest.json"
    assert path.is_file(), f"missing human-approved manifest at {path} (T-080)"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def e6_golden_set() -> dict:
    """The approved golden pitch set, read straight off disk. Same reasoning as above."""
    path = _REPO_ROOT / "fixtures" / "golden" / "golden_set.json"
    assert path.is_file(), f"missing approved golden set at {path} (T-080)"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def e6_catalog():
    """A one-product catalog snapshot with an attribute of every comparator family."""

    def _build(snapshot_id: str = "e6-snap", **overrides: Any) -> dict:
        attributes: dict[str, Any] = {
            "weight": {"value": 500, "unit": "g"},
            "vegan": {"value": True},
            "material": {"value": "Cotton"},
            "ingredients": {"value": ["water", "glycerin", "aloe"]},
            "compatible_with": {"value": ["p-2"]},
            "voltage": {"value": ["120 V", "230 V"]},
        }
        attributes.update(overrides)
        return {
            "snapshot_id": snapshot_id,
            "products": [
                {
                    "product_ref": "p-1",
                    "canonical_name": "Test Product",
                    "attributes": attributes,
                    "offer": {"unit_price": 19.99, "currency": "USD", "availability": "in_stock"},
                }
            ],
        }

    return _build


@pytest.fixture
def e6_pitch():
    """A pitch carrying pre-decomposed atomic claims with seller_asserted provenance."""

    def _build(pitch_id: str, claims: list[dict], text: str = "A pitch.") -> dict:
        return {
            "pitch_id": pitch_id,
            "store_id": "s-1",
            "product_ref": "p-1",
            "text": text,
            "claims": [
                dict(
                    claim,
                    provenance={
                        "source": "seller_asserted",
                        "ref": f"{pitch_id}#{claim.get('claim_ref', claim.get('key'))}",
                        "observed_at": E6_AS_OF,
                        "authority_rank": 0,
                    },
                )
                for claim in claims
            ],
        }

    return _build
