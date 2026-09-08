"""A verdict an OLDER fold already sealed must not take ``POST /reconcile`` down for ever.

MEASURED, on this tree::

    >>> append(store, reconciled_v1)          # inserted
    >>> append(store, reconciled_v2)          # same event_id, one extra payload key
    IdempotencyConflict: event_id 'reconciled:s-1:o-1' is already in the chain with DIFFERENT
    content, so accepting this append would silently lose it.

``event_id`` here is minted BY THIS SERVICE from the order (``reconciled:{store}:{order}``),
not supplied by a caller, so that conflict cannot mean "a producer contradicted itself". It
means the fold changed — which it does whenever the engine learns to carry another field, and
did the moment the auction's ``cluster_id`` started riding along so a completed purchase could
be routed to a bandit arm.

Left to raise, ``_append_all`` turned that into ``500 reconciliation_not_recorded`` with
NOTHING in the fold landing: no verdict, no observation, no relational row, no notification —
for every order in the chain, not just the one with the old row. And the ledger is append-only
with an ``ENABLE ALWAYS`` trigger, so the offending row cannot be removed: the door would have
stayed dead for the life of that database.

The two things this file pins are the two halves of the fix. The door keeps working, and it
does not go quiet about it: the stored verdict stands, this run's recomputation of it is
discarded, and the response says so under ``superseded`` rather than folding the number into
``already_present`` where nobody could tell the two apart.

THE THIRD THING, and it is the one that bites every existing database
---------------------------------------------------------------------
Keeping the door open is not the same as keeping the fold converging. ``_append_all`` says of
itself that ``persist_observations`` "wants EVERY landed event … an event that was already in
the chain but whose row never made it is exactly the case that must be retried" — and the
supersession branch used to ``continue`` *before* the event reached ``landed``, so a superseded
observation never reached the relational write at all::

    POST /reconcile -> 200
      appended        : {'reconciled': 0, 'offer_integrity': 0}
      superseded      : {'reconciled': 1, 'offer_integrity': 2}
      landed handed to persist_observations: []
      observations_written: 0

``ledger.trust_observations`` is the postgres table ``GET /snapshot`` reads and the exchange
ranks on, so that is the trust update not arriving. And because carrying the auction's
``cluster_id`` changed the body of every ``reconciled:*`` and ``offer_integrity:*`` payload
while their ``event_id``s stayed deterministic, EVERY order reconciled before that deploy takes
this branch — the un-converged row is the normal case on any database with history, not an edge
one. The last test in this file drives it over the served door and counts the rows in postgres.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from trust.feedback import ENV_EXCHANGE_OUTCOMES_URL, ENV_STORE_AGENT_ENDPOINTS, TRUST_EVENT_PATH

from proxyshop_support.asgi_server import serve
from proxyshop_support.postgres import role_dsn

STORE = "store-superseded"
ORDER = "ord-superseded"
TOKEN = "tok-superseded"
CLUSTER = "cluster-superseded"


def _purchase() -> list[dict[str, Any]]:
    """One whole checkout: the promise, the payment, the delivery."""
    return [
        {
            "event_id": f"accepted:{STORE}:{ORDER}",
            "ts": "2026-09-01T00:00:00Z",
            "kind": "accepted",
            "store_id": STORE,
            "auction_id": "auc-superseded",
            "payload": {
                "bid_ref": f"auc-superseded:{STORE}",
                "checkout_token": TOKEN,
                "state": "accepted",
                "intent_id": "int-superseded",
                "cluster_id": CLUSTER,
                "offer": {
                    "unit_price": 40.0,
                    "total_price": 40.0,
                    "product_ref": "p-1",
                    "delivery_estimate_days": 3,
                },
            },
        },
        {
            "event_id": f"order_paid:{STORE}:{ORDER}",
            "ts": "2026-09-02T00:00:00Z",
            "kind": "order_paid",
            "store_id": STORE,
            "order_ref": ORDER,
            "payload": {"checkout_token": TOKEN, "order_ref": ORDER, "total_price": 55.0},
        },
        {
            "event_id": f"order_fulfilled:{STORE}:{ORDER}",
            "ts": "2026-09-25T00:00:00Z",
            "kind": "order_fulfilled",
            "store_id": STORE,
            "order_ref": ORDER,
            "payload": {"order_ref": ORDER, "fulfilled_at": "2026-09-25T00:00:00Z"},
        },
    ]


#: What an OLDER version of the fold would have sealed for this order: the published keys and
#: nothing else. Byte-different from what the fold composes today, which is the whole point.
_OLDER_VERDICT = {
    "event_id": f"reconciled:{STORE}:{ORDER}",
    "ts": "2026-09-02T00:00:00Z",
    "kind": "reconciled",
    "store_id": STORE,
    "order_ref": ORDER,
    "payload": {
        "price_honored": True,
        "discount_honored": True,
        "pixel_missing": True,
        "order_ref": ORDER,
    },
}


@pytest.mark.docker("postgres")
def test_a_verdict_an_older_fold_sealed_is_reported_not_fatal(
    ledger_clean: Any, worker_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    received: list[dict[str, Any]] = []
    agent = FastAPI()

    @agent.post(TRUST_EVENT_PATH)
    def _door(body: dict[str, Any]) -> dict[str, str]:
        received.append(body)
        return {"ok": "true"}

    dsn = role_dsn("trust_rw", database=worker_database)
    with contextlib.ExitStack() as stack:
        agent_url = stack.enter_context(serve(agent))
        for variable in (
            "PROXYSHOP_LEDGER_DSN",
            "PROXYSHOP_PG_DSN_TRUST_RW",
            "PROXYSHOP_PG_DSN_APP",
        ):
            monkeypatch.setenv(variable, dsn)
        monkeypatch.setenv(ENV_STORE_AGENT_ENDPOINTS, json.dumps({STORE: agent_url}))
        monkeypatch.delenv(ENV_EXCHANGE_OUTCOMES_URL, raising=False)

        from trust.main import create_app

        app = create_app()
        stack.callback(lambda: getattr(getattr(app.state, "event_store", None), "close", bool)())
        client = stack.enter_context(TestClient(app))

        for event in (*_purchase(), _OLDER_VERDICT):
            seeded = client.post("/events", json=event)
            assert seeded.status_code == 201, seeded.text

        folded = client.post("/reconcile")

        assert folded.status_code == 200, (
            f"the fold answered {folded.status_code}; one row an older fold wrote has taken "
            f"the whole door down: {folded.text}"
        )
        body = folded.json()
        assert body["superseded"]["reconciled"] == 1, (
            f"the discarded recomputation is not reported, so 'already present' and 'present "
            f"and not what this code would write' are indistinguishable: {body['superseded']}"
        )
        assert body["appended"]["reconciled"] == 0
        # The rest of the fold still landed: the observations are what the store learns from,
        # and they must not be collateral damage from a verdict row that predates them.
        assert body["appended"]["offer_integrity"] == 2, body["appended"]
        assert body["notifications"] == {"attempted": 2, "pushed": 2, "not_pushed": 0}

    assert {hit["dim"] for hit in received} == {"price_honored", "shipped_on_time"}


@pytest.mark.docker("postgres")
def test_an_unsuperseded_fold_reports_zero_so_the_count_is_not_always_on(
    ledger_clean: Any, worker_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control. Without it, ``superseded >= 1`` would pass on a door that always says one."""
    from fastapi.testclient import TestClient

    dsn = role_dsn("trust_rw", database=worker_database)
    with contextlib.ExitStack() as stack:
        for variable in (
            "PROXYSHOP_LEDGER_DSN",
            "PROXYSHOP_PG_DSN_TRUST_RW",
            "PROXYSHOP_PG_DSN_APP",
        ):
            monkeypatch.setenv(variable, dsn)
        monkeypatch.setenv(ENV_STORE_AGENT_ENDPOINTS, "{}")
        monkeypatch.delenv(ENV_EXCHANGE_OUTCOMES_URL, raising=False)

        from trust.main import create_app

        app = create_app()
        stack.callback(lambda: getattr(getattr(app.state, "event_store", None), "close", bool)())
        client = stack.enter_context(TestClient(app))

        for event in _purchase():
            assert client.post("/events", json=event).status_code == 201

        first = client.post("/reconcile").json()
        assert first["superseded"] == {"reconciled": 0, "offer_integrity": 0}
        assert first["appended"] == {"reconciled": 1, "offer_integrity": 2}

        # And a re-run is still a plain no-op rather than a supersession, which is the
        # difference between "this fold is idempotent" and "this fold cannot restate itself".
        second = client.post("/reconcile").json()
        assert second["superseded"] == {"reconciled": 0, "offer_integrity": 0}
        assert second["appended"] == {"reconciled": 0, "offer_integrity": 0}
        assert second["already_present"] == {"reconciled": 1, "offer_integrity": 2}


def _observation_rows(dsn: str) -> list[tuple[Any, ...]]:
    """``(event_seq, dim, observation_type)`` for every row in ``ledger.trust_observations``.

    Read out of postgres rather than off the response, because "the fold says it wrote two"
    and "two rows exist in the table ``GET /snapshot`` reads" are different claims and only
    the second one is the trust update arriving.
    """
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select event_seq, dim, observation_type from ledger.trust_observations order by dim"
        )
        return [tuple(row) for row in cursor.fetchall()]


def _without_cluster(event: dict[str, Any]) -> dict[str, Any]:
    """``event`` as the fold composed it BEFORE the auction's ``cluster_id`` rode along.

    Derived from what this build's own ``GET /reconcile`` composes rather than hand-written,
    so the "older" body is the real one minus exactly the field whose arrival made every
    pre-existing row a superseding one. Hand-writing it would let the two drift and quietly
    stop reproducing anything.
    """
    payload = {key: value for key, value in dict(event["payload"]).items() if key != "cluster_id"}
    return {**event, "payload": payload}


@pytest.mark.docker("postgres")
def test_a_superseded_observation_still_converges_its_relational_row(
    ledger_clean: Any, worker_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row an older fold sealed and never persisted is exactly the one that must retry."""
    from fastapi.testclient import TestClient

    received: list[dict[str, Any]] = []
    agent = FastAPI()

    @agent.post(TRUST_EVENT_PATH)
    def _door(body: dict[str, Any]) -> dict[str, str]:
        received.append(body)
        return {"ok": "true"}

    dsn = role_dsn("trust_rw", database=worker_database)
    with contextlib.ExitStack() as stack:
        agent_url = stack.enter_context(serve(agent))
        for variable in (
            "PROXYSHOP_LEDGER_DSN",
            "PROXYSHOP_PG_DSN_TRUST_RW",
            "PROXYSHOP_PG_DSN_APP",
        ):
            monkeypatch.setenv(variable, dsn)
        monkeypatch.setenv(ENV_STORE_AGENT_ENDPOINTS, json.dumps({STORE: agent_url}))
        monkeypatch.delenv(ENV_EXCHANGE_OUTCOMES_URL, raising=False)

        from trust.events.store import append as ledger_append
        from trust.main import create_app

        app = create_app()
        stack.callback(lambda: getattr(getattr(app.state, "event_store", None), "close", bool)())
        stack.callback(
            lambda: getattr(getattr(app.state, "trust_event_sink", None), "close", bool)()
        )
        client = stack.enter_context(TestClient(app))

        for event in _purchase():
            assert client.post("/events", json=event).status_code == 201

        # What an EARLIER deploy of this fold would have sealed: the same deterministic ids,
        # the same everything, minus the cluster carry.
        diagnosis = client.get("/reconcile")
        assert diagnosis.status_code == 200, diagnosis.text
        older = [
            _without_cluster(event)
            for event in (*diagnosis.json()["events"], *diagnosis.json()["observation_events"])
        ]
        assert len(older) == 3, older
        assert all("cluster_id" in event["payload"] for event in diagnosis.json()["events"])

        # Appended straight to the chain rather than through `POST /events`, and that is the
        # scenario rather than a shortcut: `POST /events` writes the relational row itself, so
        # going through it would seed the very row this test is about. What is being
        # reproduced is the case `_append_all`'s own docstring names — an event that IS in the
        # chain and whose `ledger.trust_observations` row never made it (the earlier run folded
        # with `observations_persisted: false`, or its write failed). That row can only ever be
        # written by a later fold, and the supersession branch is what stops one.
        store = app.state.event_store
        stored_seqs = {
            str(event["event_id"]): int(ledger_append(store, event).seq) for event in older
        }
        assert _observation_rows(dsn) == [], "the seeding must not write the row itself"
        seeded_hits = len(received)

        folded = client.post("/reconcile")
        assert folded.status_code == 200, folded.text
        body = folded.json()

        assert body["superseded"] == {"reconciled": 1, "offer_integrity": 2}, body["superseded"]
        assert body["appended"] == {"reconciled": 0, "offer_integrity": 0}, body["appended"]

        # THE POINT. A superseded event is still a landed event, so its relational row
        # converges on the next fold like every other one.
        assert body["observations_written"] == 2, (
            f"the fold wrote {body['observations_written']} observation row(s); every order "
            f"reconciled before the cluster carry takes this branch, so on any existing "
            f"database the relational convergence is off: {body}"
        )
        rows = _observation_rows(dsn)
        # `contradicted` on both, because this purchase is a broken promise on both counts:
        # 40.0 promised against 55.0 paid, and a 3-day delivery estimate against 23 days.
        assert [(row[1], row[2]) for row in rows] == [
            ("price_honored", "contradicted"),
            ("shipped_on_time", "contradicted"),
        ], rows
        # Against the STORED rows' seqs, not this run's recomposition: `event_seq` is
        # `bigint REFERENCES ledger.commerce_events (seq)` and the chain's row is the one that
        # stands, so a guessed seq would be a foreign key onto the wrong event.
        assert {int(row[0]) for row in rows} == {
            seq for event_id, seq in stored_seqs.items() if event_id.startswith("offer_integrity:")
        }, (rows, stored_seqs)
        # Nothing was guessed: a stored row this fold could not read back is counted rather
        # than invented, and on a healthy chain that count is zero.
        assert body["superseded_unreadable"] == {"reconciled": 0, "offer_integrity": 0}, body

        # And it is NOT announced. A supersession means the chain already held this
        # observation, and telling the store again charges it twice for one purchase.
        assert body["notifications"]["attempted"] == 0, body["notifications"]
        assert len(received) == seeded_hits, [hit["event"]["event_id"] for hit in received]

        # Converged, so the second run has nothing left to write and still nothing to push.
        again = client.post("/reconcile").json()
        assert again["superseded"] == {"reconciled": 1, "offer_integrity": 2}, again["superseded"]
        assert again["observations_written"] == 0, again
        assert again["notifications"]["attempted"] == 0, again["notifications"]
        assert len(_observation_rows(dsn)) == 2
