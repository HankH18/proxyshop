"""Fixtures for the closed trust loop: a served trust app over the real ledger database.

Every name here is prefixed ``loop_`` because ``apps/trust/tests/`` binds one fixture name
across the whole directory (``proxyshop_support.fixture_loader``), and this file shares it
with T-011's, T-060's and T-062's.

What these build that nothing else here does: **one application serving BOTH doors** —
``POST /events`` (where a producer writes) and ``GET /snapshot`` (where the exchange reads)
— against one real database, as ``trust_rw``. That pairing is the whole subject: the defect
these tests exist for was invisible to any fixture that exercised one door at a time, because
each door worked perfectly and they were not connected to each other.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

#: The pinned reference instant every snapshot and replay in these tests is taken at.
#: Fixed, because decay is a function of ``as_of`` and a wall clock would make an ``==``
#: between a served score and a replayed one meaningless.
LOOP_AS_OF = "2026-03-01T00:00:00Z"

#: When the events themselves happen. A month before ``LOOP_AS_OF``, so decay is real (an
#: observation that had not decayed at all would hide a whole class of arithmetic bug) and
#: identical for every observation in one test.
LOOP_TS = "2026-02-01T00:00:00.000Z"


@pytest.fixture
def loop_as_of() -> str:
    """The pinned instant every score in these tests is computed against."""
    return LOOP_AS_OF


@pytest.fixture
def loop_ts() -> str:
    """The instant the events in these tests are stamped with."""
    return LOOP_TS


@pytest.fixture
def loop_seed_sellers(pg_admin: Any) -> Callable[..., None]:
    """Put stores on the platform roster.

    ``GET /snapshot`` reads ``app.sellers LEFT JOIN ledger.trust_observations``, so a store
    with no roster row is served no entry at all — and a test asserting "the posture moved"
    would then fail for a reason that has nothing to do with the observation.
    """

    def seed(*store_ids: str) -> None:
        with pg_admin.cursor() as cursor:
            for store_id in store_ids:
                cursor.execute(
                    "insert into app.sellers (store_id, domain, business_identity, tier) "
                    "values (%s, %s, %s, 'hosted') on conflict do nothing",
                    (store_id, f"{store_id}.example", f"co-{store_id}"),
                )

    return seed


@pytest.fixture
def loop_client(
    ledger_clean: Any, worker_database: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Any]:
    """A ``TestClient`` over the WHOLE trust app, pointed at this worker's real database.

    Nothing is injected on ``app.state``: the event store, the snapshot's tables and the
    projection's connection all resolve the way a deployment resolves them, from
    ``PROXYSHOP_LEDGER_DSN``. That is the point — an injected store would prove the
    projection can write to a double, which is what the defect these tests grade already
    did.

    ``trust_rw`` and not ``admin``, so the grant set D5 gives this service is exercised too:
    a projection that needed a privilege ``trust_rw`` does not hold would pass under a
    superuser and fail in every deployment.
    """
    from fastapi.testclient import TestClient

    from proxyshop_support.postgres import role_dsn

    dsn = role_dsn("trust_rw", database=worker_database)
    # Every spelling `PostgresEventStore.DEFAULT_DSN_ENV` consults, set to the SAME role:
    # a developer's `.env` naming `PROXYSHOP_PG_DSN_APP` would otherwise let a run that
    # forgot the first variable grade a different role's grant set and say nothing.
    monkeypatch.setenv("PROXYSHOP_LEDGER_DSN", dsn)
    monkeypatch.setenv("PROXYSHOP_PG_DSN_TRUST_RW", dsn)
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", dsn)

    # R13's push is not the subject of these tests, and a configured endpoint would make it
    # one: `POST /events` notifies the affected store whenever a sink can be built from the
    # environment, which would put a bounded HTTP call on every append here — through the
    # pytest socket guard, and against whatever answers that address.
    from trust.feedback.notify import ENV_STORE_AGENT_ENDPOINTS

    monkeypatch.delenv(ENV_STORE_AGENT_ENDPOINTS, raising=False)

    from trust.main import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client
    store = getattr(app.state, "event_store", None)
    closer = getattr(store, "close", None)
    if callable(closer):
        closer()


@pytest.fixture
def loop_rows(pg_admin: Any) -> Callable[[], list[tuple]]:
    """Every trust observation row, as ``(store_id, dim, observation_type, weight)``.

    Read with the admin connection rather than through the app, so what is asserted is what
    is IN the table and not what a route says about it.
    """

    def rows() -> list[tuple]:
        with pg_admin.cursor() as cursor:
            cursor.execute(
                "select store_id, dim, observation_type, weight "
                "from ledger.trust_observations order by store_id, dim, observation_type"
            )
            return [tuple(row) for row in cursor.fetchall()]

    return rows


@pytest.fixture
def loop_event() -> Callable[..., dict[str, Any]]:
    """A ledger event in the published shape, stamped at :data:`LOOP_TS` by default."""

    def build(event_id: str, kind: str, **fields: Any) -> dict[str, Any]:
        event: dict[str, Any] = {
            "event_id": event_id,
            "ts": fields.pop("ts", LOOP_TS),
            "kind": kind,
            "payload": dict(fields.pop("payload", {}) or {}),
        }
        event.update({key: value for key, value in fields.items() if value is not None})
        return event

    return build


@pytest.fixture
def loop_purchase(loop_event: Callable[..., dict[str, Any]]) -> Callable[..., list[dict]]:
    """One whole checkout as ledger events: the promise, the payment, and the delivery.

    ``delivery_estimate_days=None`` omits the delivery promise entirely (an offer that made
    none), and ``fulfilled_at=None`` omits the fulfilment (an order that has not shipped).
    Both omissions are states this repo really produces, and both are graded.
    """

    def build(
        store_id: str,
        *,
        order_ref: str,
        checkout_token: str,
        total_price: float = 100.0,
        paid_price: float | None = None,
        delivery_estimate_days: float | None = None,
        fulfilled_at: str | None = None,
        ordered_at: str = LOOP_TS,
    ) -> list[dict[str, Any]]:
        offer: dict[str, Any] = {
            "product_ref": f"p-{store_id}",
            "unit_price": total_price,
            "total_price": total_price,
        }
        if delivery_estimate_days is not None:
            offer["delivery_estimate_days"] = delivery_estimate_days
        events = [
            loop_event(
                f"accepted:{store_id}:{order_ref}",
                "accepted",
                ts=ordered_at,
                store_id=store_id,
                payload={
                    "bid_ref": f"bid-{store_id}",
                    "checkout_token": checkout_token,
                    "offer": offer,
                },
            ),
            loop_event(
                f"order_paid:{store_id}:{order_ref}",
                "order_paid",
                ts=ordered_at,
                store_id=store_id,
                order_ref=order_ref,
                payload={
                    "checkout_token": checkout_token,
                    "order_ref": order_ref,
                    "total_price": total_price if paid_price is None else paid_price,
                },
            ),
        ]
        if fulfilled_at is not None:
            events.append(
                loop_event(
                    f"order_fulfilled:{store_id}:{order_ref}",
                    "order_fulfilled",
                    ts=fulfilled_at,
                    store_id=store_id,
                    order_ref=order_ref,
                    payload={"order_ref": order_ref, "fulfilled_at": fulfilled_at},
                )
            )
        return events

    return build


@pytest.fixture
def loop_post(loop_client: Any) -> Callable[..., Any]:
    """POST one event through the served door and insist it landed."""

    def post(event: dict[str, Any], *, expect: int = 201) -> Any:
        response = loop_client.post("/events", json=event)
        assert response.status_code == expect, (
            f"POST /events {event['kind']} answered {response.status_code}, not {expect}: "
            f"{response.text}"
        )
        return response

    return post
