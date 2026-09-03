"""Fixtures for T-043 — shadow mode, activation and trust intake (``store_agent.modes``).

Owned by T-043. Every name here is prefixed ``modes_`` so it cannot collide with a sibling
``_fixtures_*.py`` in this directory (see :mod:`proxyshop_support.fixture_loader`).

The store context and the bid request below are the SAME plain-dict shapes the frozen E4
acceptance file builds (`.swarm-loop/acceptance/test_e4_store_agent.py`,
``_store_context`` / ``_bid_request`` / ``_trust_event_payload``). They are restated here
rather than imported because the frozen file is not importable product support and must
never be edited; keeping the shapes identical is what makes this gate a real proxy for the
frozen goal instead of a friendlier restatement of it.

Offline and clock-free, like the code it grades: no network, no database, no wall clock,
no unseeded randomness.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

MODES_CLUSTER = "cluster-warm-layers"
MODES_STORE_ID = "store-alpha"
MODES_LIST_PRICE = 100.0


def _prov(source: str, ref: str) -> dict:
    return {
        "source": source,
        "ref": ref,
        "observed_at": "2026-01-01T00:00:00Z",
        "authority_rank": 1,
    }


class _Recorder:
    """A recording double for an injected sink or submitter.

    Callable, and aliased under every plausible method name, exactly like the frozen
    suite's own ``_Recorder``: the gate must turn on *whether* the runner logged or
    submitted, never on the spelling of the method it used.
    """

    def __init__(self, name: str = "recorder") -> None:
        self.name = name
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        return None

    write = append = submit = send = log = record = put = enqueue = __call__

    @property
    def count(self) -> int:
        return len(self.calls)

    def payloads(self) -> list:
        out: list = []
        for args, kwargs in self.calls:
            out.extend(args)
            out.extend(kwargs.values())
        return out

    def only(self) -> Any:
        assert len(self.calls) == 1, f"{self.name} was called {len(self.calls)} time(s), wanted 1"
        return self.payloads()[0]


class _Tripwire:
    """A double that refuses to be *touched at all*.

    A submitter that merely counts calls proves only that a call was not counted — a
    disconnected wire and a correctly-suppressed submission are the same zero. This one
    raises on invocation AND on any attribute lookup, so "shadow mode submitted nothing"
    becomes "shadow mode never so much as looked at the submitter", and the positive
    control (the same object in ``active`` mode) proves the tripwire really does fire.
    """

    class Touched(Exception):
        """Raised the instant anything reaches the tripwire."""

    def __init__(self, name: str = "tripwire") -> None:
        # Set through the base implementation: __getattr__ below only fires for names that
        # are *not* found normally, so a real instance attribute is safe to read.
        object.__setattr__(self, "name", name)

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        raise _Tripwire.Touched(f"{self.name} was CALLED with {args!r} {kwargs!r}")

    def __getattr__(self, item: str) -> Any:
        raise _Tripwire.Touched(f"{self.name} had attribute {item!r} read off it")


def _plain(obj: Any, _depth: int = 0) -> Any:
    """Normalize a product object to plain Python — the frozen suite's ``_plain``, trimmed."""
    if _depth > 40:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_plain(v, _depth + 1) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _plain(v, _depth + 1) for k, v in obj.items()}
    for attr in ("model_dump", "_asdict", "to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _plain(fn(), _depth + 1)
            except Exception:  # pragma: no cover - fall through to the next shape
                pass
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields:
        return {str(f): _plain(getattr(obj, f, None), _depth + 1) for f in fields}
    namespace = getattr(obj, "__dict__", None)
    if isinstance(namespace, dict) and namespace:
        return {
            str(k): _plain(v, _depth + 1) for k, v in namespace.items() if not str(k).startswith("_")
        }
    return str(obj)


def _walk(plain: Any):
    stack = [plain]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


@pytest.fixture
def modes_plain():
    """``(obj) -> plain Python`` — the frozen suite's normalizer, so assertions match its view."""
    return _plain


@pytest.fixture
def modes_canon():
    """``(obj) -> str`` — a stable canonical string, the byte-identity workhorse."""

    def canon(obj: Any) -> str:
        return json.dumps(_plain(obj), sort_keys=True, default=str)

    return canon


@pytest.fixture
def modes_first_value():
    """``(obj, key) -> value`` — the frozen suite's ``_first_value``, so this gate finds a
    logged ``offer``/``rationale`` exactly the way the frozen goal will."""

    def first_value(obj: Any, key: str, default: Any = None) -> Any:
        found = [n[key] for n in _walk(_plain(obj)) if key in n]
        return found[0] if found else default

    return first_value


@pytest.fixture
def modes_envelope():
    """``() -> dict`` — the approved envelope: 20% cap, one walled product, shadow activation."""

    def envelope(**overrides: Any) -> dict:
        env = {
            "store_id": MODES_STORE_ID,
            "version": 3,
            "floors": [
                {"product_ref": "prod-floor", "min_price": 95.0},
                {"product_ref": "prod-cap", "min_price": 10.0},
            ],
            "max_discount_pct": 20.0,
            "budget_cap": 500.0,
            "pursue_clusters": [MODES_CLUSTER],
            "standing_commitments": [
                {
                    "key": "free_returns",
                    "value": "30 days",
                    "provenance": _prov("owner_statement", "envelope:store-alpha:v3#free_returns"),
                },
                {
                    "key": "ships_within",
                    "value": "2 business days",
                    "provenance": _prov("owner_statement", "envelope:store-alpha:v3#ships_within"),
                },
            ],
            "activation": "shadow",
        }
        env.update(overrides)
        return env

    return envelope


@pytest.fixture
def modes_context(modes_envelope):
    """``(**overrides) -> dict`` — the store context the runner is constructed around."""

    def context(**overrides: Any) -> dict:
        ctx = {
            "store_id": MODES_STORE_ID,
            "envelope": modes_envelope(),
            "catalog": {
                "prod-cap": {
                    "product_ref": "prod-cap",
                    "list_price": MODES_LIST_PRICE,
                    "material": "merino wool",
                    "gtin": "00000000000017",
                },
                "prod-floor": {
                    "product_ref": "prod-floor",
                    "list_price": MODES_LIST_PRICE,
                    "material": "alpaca",
                    "gtin": "00000000000024",
                },
            },
            "live_state": {
                "prod-cap": {"in_stock": True, "units_left": 7},
                "prod-floor": {"in_stock": True, "units_left": 3},
            },
            # Deliberately None: R10's cold agent. This gate never needs the learning module.
            "learned_policy": None,
            "network_priors": {MODES_CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
        }
        ctx.update(overrides)
        return ctx

    return context


@pytest.fixture
def modes_request():
    """``(auction_id) -> dict`` — one BidRequest the cold path answers with a Bid."""

    def request(auction_id: str = "auc-0001") -> dict:
        return {
            "auction_id": auction_id,
            "intent": {
                "intent_id": "int-0001",
                "cluster_id": MODES_CLUSTER,
                "query": "a warm mid-layer for cold commutes",
                "category": "outerwear",
                "hard_constraints": [{"field": "material", "op": "eq", "value": "merino wool"}],
                "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
                "ship_to": "US-CA",
                "currency": "USD",
                "budget_band": "50-150",
                "created_at": "2026-01-01T00:00:00Z",
                "schema_version": "1",
            },
            "profile": {
                "pseudonym": "pseu-0001",
                "buckets": {
                    "budget_band": "50-150",
                    "category_affinity": ["outerwear"],
                    "frequency_tier": "occasional",
                    "region": "US-W",
                    "first_time": True,
                },
            },
            "respond_by": "2999-01-01T00:00:00Z",
        }

    return request


@pytest.fixture
def modes_trust_event():
    """``(**overrides) -> dict`` — the frozen suite's TrustEventPayload, shape for shape."""

    def event(**overrides: Any) -> dict:
        payload = {
            "store_id": MODES_STORE_ID,
            "event": {
                "event_id": "evt-0001",
                "ts": "2026-01-02T00:00:00Z",
                "kind": "feedback",
                "auction_id": "auc-0000",
                "store_id": MODES_STORE_ID,
                "order_ref": "ord-0001",
                "payload": {"matched_pitch": False, "reason": "ships_within missed"},
            },
            "dim": "shipped_on_time",
            "delta": -0.35,
            "pseudonymous_context": {"cluster_id": MODES_CLUSTER, "pseudonym": "pseu-0002"},
        }
        payload.update(overrides)
        return payload

    return event


@pytest.fixture
def modes_recorder():
    """``(name) -> _Recorder`` — a call-recording sink/submitter double."""
    return _Recorder


@pytest.fixture
def modes_tripwire():
    """``(name) -> _Tripwire`` — a double that raises the moment anything touches it."""
    return _Tripwire
