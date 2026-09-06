"""The gate on :mod:`proxyshop_support.trust_ledger` — the shared trust-ledger writer.

This file grades the MECHANISM: endpoint resolution, delivery accounting, what is reported
when the trust service stops taking events, and the property that makes the module worth
sharing at all — that importing it drags in no service package, so merchant and buyer can
adopt it without either depending on the exchange.

What it deliberately does NOT grade: that the exchange uses it. That belongs to
``apps/exchange/tests/test_composition_root.py``, which asserts the served app's sink IS one
of these and drives real auction traffic through it. A mechanism gate that also claimed the
product was wired would be the "a module nothing imports" failure that this repository has
already paid for once.
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest

from proxyshop_support.trust_ledger import (
    DEFAULT_TRUST_URL,
    MAX_UNDELIVERED_LEDGER_EVENTS,
    SOURCE_DEFAULT,
    SOURCE_ENVIRONMENT,
    SOURCE_STATED,
    TRUST_EVENTS_PATH,
    TrustLedgerPublisher,
    trust_endpoint,
    trust_events_url,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class _Transport:
    """A stand-in ``httpx.Client``: answers whatever the test told it to, and records posts."""

    def __init__(self, status: int = 201) -> None:
        self.status = status
        self.raises: BaseException | None = None
        self.posted: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.posted.append((url, dict(kwargs.get("json") or {})))
        if self.raises is not None:
            raise self.raises
        return _Response(self.status)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _publisher(transport: _Transport, **kwargs: Any) -> TrustLedgerPublisher:
    publisher = TrustLedgerPublisher("http://trust.invalid/events", **kwargs)
    publisher._client = transport
    return publisher


def test_the_append_door_is_resolved_in_one_place_and_says_where_it_came_from() -> None:
    """Stated, then ``TRUST_URL``, then the built-in default — and the path is appended once.

    ``source`` is not decoration. A wiring-time line that reports only the URL cannot tell an
    operator the difference between "this deployment chose to write its audit trail here" and
    "nobody said anything and a constant in the source decided", which is exactly the question
    a person reading a start-up log about a required dependency is asking.
    """
    assert trust_endpoint("http://ledger:9", {}) == ("http://ledger:9/events", SOURCE_STATED)
    # One trailing slash in a document must not become `//events`.
    assert trust_endpoint("http://ledger:9/", {})[0] == "http://ledger:9/events"
    assert trust_endpoint("  ", {"TRUST_URL": "http://from-env:9"}) == (
        "http://from-env:9/events",
        SOURCE_ENVIRONMENT,
    )
    assert trust_endpoint(None, {}) == (f"{DEFAULT_TRUST_URL}{TRUST_EVENTS_PATH}", SOURCE_DEFAULT)
    assert trust_endpoint(None, {"TRUST_URL": "   "})[1] == SOURCE_DEFAULT
    # The URL-only spelling is the same function, not a second implementation of the rule.
    assert trust_events_url("http://ledger:9/", {}) == "http://ledger:9/events"


def test_an_event_that_lands_is_counted_and_one_that_does_not_is_named() -> None:
    """Delivery accounting, including the ``409`` that is once-only landing working.

    A ``409`` is the trust door saying "I already hold this id". That is the ledger's
    idempotency doing its job, and it is still not a delivery — an operator reading
    ``delivered`` wants the number of events the chained ledger actually took, so the two stay
    distinguishable rather than being rounded to "fine".
    """
    transport = _Transport(status=201)
    publisher = _publisher(transport)

    assert publisher.publish({"event_id": "e-1", "kind": "auction_opened"}) is True
    assert publisher.delivered == 1 and publisher.lost == 0 and not publisher.undelivered
    assert transport.posted == [
        ("http://trust.invalid/events", {"event_id": "e-1", "kind": "auction_opened"})
    ]

    for status in (409, 422, 503):
        refusing = _publisher(_Transport(status=status))
        assert refusing.publish({"event_id": f"e-{status}", "kind": "auction_opened"}) is False
        assert refusing.delivered == 0, status
        assert [reason for _, reason in refusing.undelivered] == [f"trust answered HTTP {status}"]

    dead = _Transport()
    dead.raises = ConnectionError("nodename nor servname provided, or not known")
    down = _publisher(dead)
    assert down.publish({"event_id": "e-x", "kind": "auction_closed"}) is False
    assert list(down.undelivered) == [
        ("e-x", "ConnectionError: nodename nor servname provided, or not known")
    ]


def test_the_record_of_what_did_not_land_is_a_ring_and_never_holds_a_body() -> None:
    """Bounded, because this runs on unauthenticated request paths — and ids only.

    An unbounded list of failures against a trust service that is down is a memory leak
    anybody can fill by posting in a loop. The second property is the one a log line cannot
    give back once it is written: nothing a client sent is kept here, so an operator reading
    the ring cannot be reading a buyer's body.
    """
    dead = _Transport()
    dead.raises = ConnectionError("down")
    publisher = _publisher(dead)

    for index in range(MAX_UNDELIVERED_LEDGER_EVENTS + 10):
        publisher.publish(
            {
                "event_id": f"e-{index}",
                "kind": "auction_opened",
                "payload": {"secret": "PSX-NEVER-LOG-THIS"},
            }
        )

    assert publisher.undelivered.maxlen == MAX_UNDELIVERED_LEDGER_EVENTS
    assert len(publisher.undelivered) == MAX_UNDELIVERED_LEDGER_EVENTS
    assert publisher.lost == MAX_UNDELIVERED_LEDGER_EVENTS + 10
    rendered = repr(list(publisher.undelivered)) + repr(publisher.status())
    assert "PSX-NEVER-LOG-THIS" not in rendered, rendered


def test_a_trust_service_that_stops_taking_events_is_reported_once_per_outage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The posture, and the whole of what replaced T-150's per-sink ``WARNING``.

    Four properties, and the last two are what the old shape could not do:

    * a run in which everything lands says **nothing**;
    * the moment events stop landing is one ``ERROR`` — not a ``WARNING``, because the trust
      ledger is required architecture and a required write that is not happening is a fault,
      and not per-event, because the second failure is the same fact as the first;
    * the moment they land again is one ``INFO``, naming how many were lost. The old sink
      reported the start of an outage once per PROCESS and its end never, so an operator who
      saw the line had no way to learn the condition had cleared;
    * a SECOND outage is reported again. Under the old shape it was silent forever, which is
      the failure mode "one warning and then counters" quietly has.
    """
    transport = _Transport()
    transport.raises = ConnectionError("down")
    log = logging.getLogger("test.trust_ledger.posture")
    publisher = _publisher(transport, log=log, subject="a service")

    with caplog.at_level(logging.DEBUG, logger=log.name):
        for index in range(5):
            publisher.publish({"event_id": f"e-{index}", "kind": "auction_opened"})
        outage = [record for record in caplog.records if record.name == log.name]
        assert [record.levelname for record in outage] == ["ERROR"], (
            f"five failed publishes produced {[r.levelname for r in outage]}; the fault is one "
            f"state change, not five events"
        )
        assert not [record for record in outage if record.levelname == "WARNING"]
        assert publisher.delivering is False
        assert "NOT in the chained ledger" in outage[0].getMessage(), outage[0].getMessage()

        caplog.clear()
        transport.raises = None
        publisher.publish({"event_id": "e-back", "kind": "auction_closed"})
        recovery = [record for record in caplog.records if record.name == log.name]
        assert [record.levelname for record in recovery] == ["INFO"], recovery
        assert "5 event(s)" in recovery[0].getMessage(), recovery[0].getMessage()
        assert publisher.delivering is True

        caplog.clear()
        publisher.publish({"event_id": "e-quiet", "kind": "auction_closed"})
        assert [record for record in caplog.records if record.name == log.name] == []

        caplog.clear()
        transport.raises = ConnectionError("down again")
        publisher.publish({"event_id": "e-again", "kind": "auction_closed"})
        second = [record for record in caplog.records if record.name == log.name]
        assert [record.levelname for record in second] == ["ERROR"], (
            "a trust service that failed, recovered and failed again reported nothing the "
            "second time — which is the hole a one-shot report leaves"
        )

    assert publisher.status() == {
        "url": "http://trust.invalid/events",
        "delivering": False,
        "delivered": 2,
        "lost": 6,
        "last_failure": "ConnectionError: down again",
    }


def test_the_publisher_is_adoptable_by_a_service_that_drains_a_ring() -> None:
    """Merchant's shape, driven for real: a ring buffer emptied through ``publish``.

    ``apps/merchant/svc/src/install/webhooks.py``'s ``HANDOFF`` is a ring whose own
    ``default_sink`` docstring says it exists because "E6 is not deployed yet". This is the
    whole of what adopting this module would cost it — build one publisher, drain the ring
    through it, keep the events that did not land. No exchange import, no second HTTP client,
    and the same ``delivered``/``undelivered`` reading the exchange's operator already has.
    """
    transport = _Transport()
    publisher = _publisher(transport, subject="merchant handoff")
    ring = [
        {"event_id": "m-1", "kind": "order_paid", "order_ref": "o-1"},
        {"event_id": "m-2", "kind": "code_created", "order_ref": "o-2"},
    ]

    undrained = [event for event in ring if not publisher.publish(event)]

    assert undrained == []
    assert publisher.delivered == 2
    assert [url for url, _ in transport.posted] == ["http://trust.invalid/events"] * 2
    assert [body["event_id"] for _, body in transport.posted] == ["m-1", "m-2"]


def test_importing_the_shared_writer_pulls_in_no_service_package() -> None:
    """The property that makes it SHARED rather than the exchange's code moved sideways.

    Measured in a fresh interpreter, because this one has already imported the exchange: a
    module can only be adopted by merchant and buyer if importing it does not drag the
    exchange (or either of them) in behind it. ``httpx`` is checked in the same breath — the
    client is built on first publish, so composing a service that never writes an event opens
    no socket and imports no transport.
    """
    roots = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / ".pkgroot")])
    inherited = os.environ.get("PYTHONPATH")
    env = dict(
        os.environ,
        PYTHONPATH=f"{roots}{os.pathsep}{inherited}" if inherited else roots,
    )
    probe = (
        "import sys\n"
        "import proxyshop_support.trust_ledger as m\n"
        "print('MODULE', m.__file__)\n"
        "roots = {name.split('.')[0] for name in sys.modules}\n"
        "print('SERVICES', sorted(roots & {'exchange', 'merchant_svc', 'buyer_svc', 'trust'}))\n"
        "print('HTTPX', 'httpx' in roots)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = dict(
        line.split(" ", 1)
        for line in completed.stdout.splitlines()
        if line.startswith(("MODULE ", "SERVICES ", "HTTPX "))
    )
    assert pathlib.Path(lines["MODULE"]).is_relative_to(REPO_ROOT), lines["MODULE"]
    assert lines["SERVICES"] == "[]", (
        f"importing the shared trust-ledger writer imported {lines['SERVICES']}. A service "
        f"that adopts it would inherit that dependency, which is the thing this module exists "
        f"to prevent"
    )
    assert lines["HTTPX"] == "False", "the transport was imported before anything published"
