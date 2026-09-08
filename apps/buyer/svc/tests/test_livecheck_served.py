"""The live-page check, driven through the SERVED routes and a real deployment document.

Nothing here reaches into a function and asserts on its return value. Every case posts to
``POST /buyer/shortlist/render`` — the route a shopper drives — and then to
``POST /buyer/livecheck/run``, with the wiring resolved by ``buyer_svc.composition`` out of
``BUYER_DEPLOYMENT_JSON``, because "built, tested, and reachable by nobody" is the defect class
this file exists to rule out. If the composition root stops binding the registries, or the
frozen entrypoint stops mounting the router, these tests fail.

**No test here opens a socket** (D3/C9). Pages come from ``tests/livepages/``, a replay corpus
whose values are recorded from ``fixtures/real-catalogs`` — see ``_corpus_build.py``.
"""

from __future__ import annotations

import json
import statistics
import time
from typing import Any

import pytest
from buyer_svc.composition import LEDGER_SINK_ATTR
from buyer_svc.main import create_app
from fastapi.testclient import TestClient

from apps.buyer.svc.tests._fixtures_livecheck import (
    CORPUS,
    STORE_DOMAIN,
    STORE_ID,
    RecordingSink,
    page_url,
    shortlist_body,
)


def _document(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "exchange_url": "http://exchange.invalid",
        "registered_domains": {STORE_ID: STORE_DOMAIN},
        "live_page_fetcher": f"recorded:{CORPUS}",
    }
    body.update(overrides)
    return body


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(_document()))
    return TestClient(create_app())


def _render(client: Any, **kwargs: Any) -> Any:
    response = client.post("/buyer/shortlist/render", json={"shortlist": shortlist_body(**kwargs)})
    assert response.status_code == 200, response.text
    return response.json()


def _run(client: Any) -> dict[str, Any]:
    response = client.post("/buyer/livecheck/run")
    assert response.status_code == 200, response.text
    return response.json()


# ==============================================================================================
# The four things the brief asked to be proved, served
# ==============================================================================================


def test_a_pitch_that_agrees_with_the_live_page_costs_the_store_nothing(client: Any) -> None:
    sink = RecordingSink()
    client.app.state.__setattr__(LEDGER_SINK_ATTR, sink)

    _render(client, fixture="agrees.html", unit_price=29.97)
    ran = _run(client)

    assert ran["checked"] == 1
    assert ran["contradicted"] == 0
    assert ran["agreed"] == 1
    assert sink.events == []
    [record] = ran["records"]
    assert record["outcome"] == "agrees"
    assert record["ledger_events"] == []


def test_a_price_that_moved_is_recorded_with_both_readings_and_costs_the_store(
    client: Any,
) -> None:
    """The disagreement, what was compared, and the penalty — all three, off the served route.

    The store asks 29.97; its own live page states 14.98. Both numbers are in the record, the
    surface each was read from is in the record, and the sentence saying what was compared is
    in the record — because a disagreement nobody can re-examine is an accusation rather than
    evidence.
    """
    sink = RecordingSink()
    client.app.state.__setattr__(LEDGER_SINK_ATTR, sink)

    _render(client, fixture="price-moved.html", unit_price=29.97)
    ran = _run(client)

    assert ran["contradicted"] == 1
    [record] = ran["records"]
    assert record["outcome"] == "contradicted"
    assert record["surface"] == "seller_live_page"

    [row] = [r for r in record["check"]["readings"] if r["status"] == "contradicted"]
    assert row["key"] == "unit_price"
    assert row["pitched_value"] == 29.97
    assert row["observed_value"] == 14.98
    assert row["surface"] == "schema.org/ld+json"
    assert "own product page" in row["compared"]
    assert row["tolerance"] == pytest.approx(0.005)

    # ...and it costs the store, on the dimension D53 published for exactly this.
    [event] = sink.events
    assert event["kind"] == "claim_verified"
    assert event["store_id"] == STORE_ID
    assert event["payload"]["status"] == "contradicted"
    assert event["payload"]["dim"] == "catalog_claim_accuracy"
    assert event["payload"]["surface"] == "seller_live_page"
    assert event["payload"]["pitched_value"] == 29.97
    assert event["payload"]["observed_value"] == 14.98
    assert event["payload"]["page_url"] == page_url("price-moved.html")


def test_the_published_claim_verified_payload_satisfies_its_frozen_shape(client: Any) -> None:
    """``contracts.LEDGER_PAYLOAD_SHAPES['claim_verified']`` is ``(claim_ref, status, dim)``.

    Checked against the contract rather than against this module's own idea of the body, for
    the reason ``feedback.submission`` gives: there is a live defect elsewhere in this repo
    where one kind is emitted with two different bodies because nothing on one path validated.
    """
    from contracts import validate_ledger_payload

    sink = RecordingSink()
    client.app.state.__setattr__(LEDGER_SINK_ATTR, sink)
    _render(client, fixture="sold-out.html", unit_price=29.97)
    _run(client)

    [event] = sink.events
    assert validate_ledger_payload(event["kind"], event["payload"]) == []


def test_a_sold_out_page_contradicts_the_live_offer_it_was_serving(client: Any) -> None:
    _render(client, fixture="sold-out.html", unit_price=29.97)
    ran = _run(client)
    [record] = ran["records"]
    assert record["outcome"] == "contradicted"
    [row] = [r for r in record["check"]["readings"] if r["status"] == "contradicted"]
    assert row["key"] == "in_stock"
    assert row["pitched_value"] is True and row["observed_value"] is False


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        pytest.param("missing", "404", id="404"),
        pytest.param("timeout", "timed out", id="timeout"),
        pytest.param("robots", "robots.txt", id="robots-disallow"),
        pytest.param("no-structured-data.html", "no schema.org", id="no-structured-data"),
        pytest.param("broken-json-ld.html", "no schema.org", id="unparseable-json-ld"),
    ],
)
def test_an_unreachable_or_silent_page_yields_no_verdict_and_says_why(
    client: Any, fixture: str, expected: str
) -> None:
    """Absence is not guilt, served. A slow server is not a store that lied.

    The reason is asserted as well as the verdict: a record that said "no verdict" without
    saying WHICH of these happened would be indistinguishable from a check that never ran.
    """
    sink = RecordingSink()
    client.app.state.__setattr__(LEDGER_SINK_ATTR, sink)

    _render(client, fixture=fixture, unit_price=999.00)
    ran = _run(client)

    assert ran["no_verdict"] == 1
    assert ran["contradicted"] == 0
    assert sink.events == []
    [record] = ran["records"]
    assert record["outcome"] == "no_verdict"
    reason = f"{record['fetch_reason']} {record['check']['reason']}"
    assert expected in reason, reason


# ==============================================================================================
# The shopper never waits
# ==============================================================================================


def test_the_render_path_fetches_nothing_at_all(client: Any) -> None:
    """The structural half of "the shortlist renders at unchanged latency".

    A timing assertion on a loaded machine is a flake generator; this is the fact underneath
    the timing. The corpus fetcher records every URL it is asked for, and after a render it
    has been asked for none.
    """
    # `GET /buyer/livecheck/{id}` takes the composition hook; `/render` deliberately does not
    # (R2 — see `accept/routes.py`). Driving it first is how the fetcher gets bound at all,
    # and that asymmetry is itself the reason the two registries resolve lazily out of the
    # deployment document rather than out of `app.state`.
    assert client.get("/buyer/livecheck/none").status_code == 200
    fetcher = client.app.state.live_page_fetcher
    _render(client, fixture="agrees.html")
    assert fetcher.requested == []

    _run(client)
    assert fetcher.requested == [page_url("agrees.html")]


def test_the_render_path_is_not_measurably_slower_with_the_check_wired(client: Any) -> None:
    """The timing half, measured against the enqueue being switched off.

    Deliberately a RELATIVE comparison against the same route in the same process, not an
    absolute millisecond budget: an absolute number is a promise about the machine the suite
    happens to run on. The bound is generous (2x the unwired median plus a millisecond)
    because what it is defending against is a live fetch on this path, which is two orders of
    magnitude, not a percent.
    """
    from buyer_svc.livecheck import targets

    body = {"shortlist": shortlist_body(fixture="agrees.html")}

    def sample(reps: int = 120) -> float:
        for _ in range(20):
            client.post("/buyer/shortlist/render", json=body)
        taken = []
        for _ in range(reps):
            start = time.perf_counter()
            client.post("/buyer/shortlist/render", json=body)
            taken.append(time.perf_counter() - start)
        return statistics.median(taken)

    with_check = sample()
    targets.set_registered_domains(
        targets.NoRegisteredDomains()
    )  # nothing resolves, nothing queues
    without_check = sample()

    assert with_check <= without_check * 2.0 + 0.001, (
        f"render median {with_check * 1000:.3f}ms with the live-page check wired against "
        f"{without_check * 1000:.3f}ms with it resolving no target"
    )


# ==============================================================================================
# Who gets checked, and who does not
# ==============================================================================================


def test_a_scraped_shop_with_no_pitch_of_its_own_is_never_checked(client: Any) -> None:
    """D55: the sponsored side carries the motive and is the side checked adversarially.

    A scraped shop's case was written by the PLATFORM out of its own snapshot. Fetching that
    store's page to check the platform's own prose would be grading the wrong party.
    """
    body = shortlist_body(fixture="price-moved.html", unit_price=29.97)
    body["slots"][0]["message"] = None
    response = client.post("/buyer/shortlist/render", json={"shortlist": body})
    assert response.status_code == 200
    ran = _run(client)
    assert ran["checked"] == 0
    assert client.app.state.live_page_fetcher.requested == []


def test_a_store_the_platform_has_not_registered_is_refused_and_the_refusal_is_served(
    client: Any,
) -> None:
    _render(client, store_id="store-nobody-registered", fixture="price-moved.html")
    ran = _run(client)
    assert ran["checked"] == 0

    served = client.get("/buyer/livecheck/auc-live-1")
    assert served.status_code == 200, served.text
    body = served.json()
    assert body["records"] == []
    [refusal] = body["refused"]
    assert refusal["store_id"] == "store-nobody-registered"
    assert "registered domain" in refusal["reason"]


def test_a_service_with_no_deployment_document_checks_nothing_and_does_not_break_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fail-closed default, served: no document, no registry, no fetch, and a 200."""
    monkeypatch.delenv("BUYER_DEPLOYMENT_JSON", raising=False)
    monkeypatch.delenv("BUYER_DEPLOYMENT", raising=False)
    monkeypatch.delenv("EXCHANGE_URL", raising=False)
    bare = TestClient(create_app())

    response = bare.post(
        "/buyer/shortlist/render", json={"shortlist": shortlist_body(fixture="price-moved.html")}
    )
    assert response.status_code == 200
    ran = bare.post("/buyer/livecheck/run")
    assert ran.status_code == 200
    assert ran.json()["checked"] == 0
    assert ran.json()["fetcher"] == "NoPageFetcher"


def test_the_recorded_verdicts_are_served_per_auction(client: Any) -> None:
    _render(client, fixture="price-moved.html", auction_id="auc-A")
    _render(client, fixture="agrees.html", auction_id="auc-B")
    _run(client)

    a = client.get("/buyer/livecheck/auc-A").json()
    b = client.get("/buyer/livecheck/auc-B").json()
    assert [row["outcome"] for row in a["records"]] == ["contradicted"]
    assert [row["outcome"] for row in b["records"]] == ["agrees"]


def test_the_shortlist_body_is_unchanged_by_the_check_being_wired(client: Any) -> None:
    """R2's contract: the render route serves the same slots it served before.

    The check is evidence gathered about a store; it is not a filter, a re-rank or a badge on
    this response, and a shopper's shortlist must not move because a queue was appended to.
    """
    rendered = _render(client, fixture="price-moved.html")
    assert [slot["slot"] for slot in rendered["slots"]] == ["headline"]
    assert rendered["slots"][0]["price"]["unit_price"] == 29.97
    assert set(rendered["slots"][0]) == {
        "slot",
        "bid_ref",
        "auction_id",
        "fit_score",
        "provenance_labels",
        "labels_source",
        "trust_summary",
        "store_domain",
        "product",
        "price",
        "commitments",
        # D55: whose price this is, and why the exchange stood in if it did.
        "fallback",
        "fallback_reason",
        "pitch",
    }


def test_draining_twice_does_not_check_anything_twice(client: Any) -> None:
    _render(client, fixture="price-moved.html")
    first = _run(client)
    second = _run(client)
    assert first["checked"] == 1
    assert second["checked"] == 0
    assert second["pending"] == 0
