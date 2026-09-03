"""T-035 — loss reports aggregate reason categories and leak no amounts (R9).

Ticket verify::

    PROXYSHOP_WORKER=<n> pytest apps/exchange/tests/test_loss_reports.py -q

What a loss report is for: a merchant is told *why* its bids lost, so it can fix its
catalogue. What it must never become: a price feed. The input rows carry the rival's
identity, the winning price, and this store's own offer amounts, because the exchange needs
them for other jobs — the report has to aggregate them away rather than copy them through.

The load-bearing test in this file is
``test_an_amount_the_projection_never_heard_of_does_not_reach_the_report``. Suppression by
blacklist — "strip ``unit_price``, ``winning_price``, ``discount``" — passes every test
written against a fixture whose amount fields the author already knew about, and leaks the
first field somebody adds to the auction log afterwards. So the assertion here is inverted:
the test invents money fields at call time, and asserts that **nothing the report's
projection does not explicitly read** appears anywhere in the output. A build that enumerates
forbidden names cannot pass it; one that enumerates *permitted* ones can.

``unmet_criteria`` gets its own test because it is the report's only free-text channel and
therefore the only place a leak can ride through a field that is legitimately carried.
"""

from __future__ import annotations

import types
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

# ---------------------------------------------------------------------------------------
# Walkers. Deliberately independent of the product's own guard: a test that imported the
# implementation's scanner would prove only that the scanner agrees with itself.
# ---------------------------------------------------------------------------------------
_ATOM = (str, bytes, bool, int, float, type(None))


def _scalars(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Yield ``(path, scalar)`` for every scalar reachable from ``node``."""
    if isinstance(node, _ATOM):
        yield (path, node)
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _scalars(value, f"{path}.{key}" if path else str(key))
        return
    if isinstance(node, (list, tuple, set, frozenset)):
        for index, value in enumerate(node):
            yield from _scalars(value, f"{path}[{index}]")
        return
    if hasattr(node, "__dict__"):
        for key, value in vars(node).items():
            yield from _scalars(value, f"{path}.{key}" if path else str(key))
        return
    yield (path, node)


def _texts(node: Any) -> list[str]:
    return [str(value) for _, value in _scalars(node) if isinstance(value, str)]


def _numbers(node: Any) -> list[float]:
    return [
        float(value)
        for _, value in _scalars(node)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]


def _assert_absent(report: Any, needle: Any) -> None:
    """``needle`` appears nowhere in ``report`` — as a value, or inside any string."""
    if isinstance(needle, str):
        for text in _texts(report):
            assert needle not in text, f"report leaks {needle!r} inside {text!r}"
        return
    assert float(needle) not in _numbers(report), f"report leaks the number {needle!r}"
    for text in _texts(report):
        for form in {str(needle), f"{float(needle):g}", repr(needle)}:
            assert form not in text, f"report leaks {needle!r} as text inside {text!r}"


def _by_cluster(report: Any) -> dict[str, Any]:
    return {entry.cluster_id: entry for entry in report.by_cluster}


def _counts(entry: Any) -> dict[str, int]:
    return {name: getattr(entry.reasons, name) for name in type(entry.reasons).model_fields}


def _only(reports: Any, store_id: str) -> Any:
    matches = [r for r in reports if r.store_id == store_id]
    assert len(matches) == 1, f"expected exactly one report for {store_id}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------------------
# 1. Shape: the published contract objects, not lookalikes.
# ---------------------------------------------------------------------------------------
def test_build_loss_report_returns_the_published_contract_types(
    loss_report_log: list[dict[str, Any]], loss_report_window: dict[str, float]
) -> None:
    from contracts import ClusterLoss, LossReasons, LossReport, LossWindow

    from apps.exchange.src.reports import build_loss_report

    reports = build_loss_report(loss_report_log, loss_report_window)
    assert isinstance(reports, list), f"expected a list of reports, got {type(reports)!r}"
    assert all(isinstance(report, LossReport) for report in reports), (
        "reports must be the contract's LossReport, not a local lookalike"
    )
    report = _only(reports, "store-loser")
    assert isinstance(report.window, LossWindow)
    assert report.window.start == loss_report_window["start"]
    assert report.window.end == loss_report_window["end"]
    assert report.by_cluster and all(isinstance(e, ClusterLoss) for e in report.by_cluster)
    assert all(isinstance(e.reasons, LossReasons) for e in report.by_cluster)


# ---------------------------------------------------------------------------------------
# 2. Aggregation: one report per store, golden counts, window and wins honoured.
# ---------------------------------------------------------------------------------------
def test_one_report_per_store_with_golden_reason_counts(
    loss_report_log: list[dict[str, Any]], loss_report_window: dict[str, float]
) -> None:
    from apps.exchange.src.reports import build_loss_report

    reports = build_loss_report(loss_report_log, loss_report_window)
    assert [r.store_id for r in reports] == ["store-loser", "store-other"], (
        "one report per losing store, ordered deterministically"
    )

    loser = _by_cluster(_only(reports, "store-loser"))
    assert set(loser) == {"cluster-a", "cluster-b"}
    assert _counts(loser["cluster-a"]) == {"fit": 2, "price": 1, "commitments": 0, "trust": 0}
    assert _counts(loser["cluster-b"]) == {"fit": 0, "price": 0, "commitments": 1, "trust": 1}
    assert loser["cluster-a"].lost == 3
    assert loser["cluster-b"].lost == 2

    other = _by_cluster(_only(reports, "store-other"))
    assert set(other) == {"cluster-a"}
    assert _counts(other["cluster-a"]) == {"fit": 0, "price": 1, "commitments": 0, "trust": 0}


def test_a_win_is_not_a_loss(loss_report_record: Any, loss_report_window: dict[str, float]) -> None:
    from apps.exchange.src.reports import build_loss_report

    now = loss_report_window["end"]
    log = [
        loss_report_record("w-1", "cluster-a", "price", now - 10.0, won=True),
        loss_report_record("w-2", "cluster-a", "price", now - 20.0, won=True),
    ]
    assert build_loss_report(log, loss_report_window) == [], (
        "a log with nothing but wins produces no loss report"
    )


def test_window_bounds_are_inclusive_and_everything_outside_is_dropped(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    from apps.exchange.src.reports import build_loss_report

    start, end = loss_report_window["start"], loss_report_window["end"]
    log = [
        loss_report_record("edge-1", "cluster-a", "fit", start),
        loss_report_record("edge-2", "cluster-a", "fit", end),
        loss_report_record("out-1", "cluster-a", "fit", start - 0.001),
        loss_report_record("out-2", "cluster-a", "fit", end + 0.001),
    ]
    entry = _by_cluster(_only(build_loss_report(log, loss_report_window), "store-loser"))
    assert entry["cluster-a"].lost == 2, "both bounds are inclusive; both outside rows are dropped"


def test_rfc3339_instants_are_understood_on_both_the_rows_and_the_window() -> None:
    from apps.exchange.src.reports import build_loss_report
    from apps.exchange.tests._fixtures_loss_reports import _loss_record

    window = {"start": "2024-04-01T00:00:00Z", "end": "2024-04-01T23:59:59Z"}
    log = [
        _loss_record("i-1", "cluster-a", "fit", "2024-04-01T09:00:00Z"),
        _loss_record("i-2", "cluster-a", "price", "2024-04-01T10:30:00+00:00"),
        _loss_record("i-3", "cluster-a", "trust", "2024-03-31T23:59:00Z"),
    ]
    report = _only(build_loss_report(log, window), "store-loser")
    entry = _by_cluster(report)["cluster-a"]
    assert _counts(entry) == {"fit": 1, "price": 1, "commitments": 0, "trust": 0}
    assert report.window.start == window["start"], "the window is echoed in the caller's own form"


# ---------------------------------------------------------------------------------------
# 3. The load-bearing one: suppression by projection, not by blacklist.
# ---------------------------------------------------------------------------------------
def test_an_amount_the_projection_never_heard_of_does_not_reach_the_report(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    from apps.exchange.src.reports import PROJECTED_FIELDS, build_loss_report

    now = loss_report_window["end"]
    invented = {
        "shipping_surcharge": 41.75,
        "rebate_pct": 7.25,
        "settlement": {"clearing_amount": 1234.56, "beaten_by": "store-rival-gamma"},
        "bid_ladder": [{"step": 311.5}, {"step": 288.25}],
    }
    log = [
        loss_report_record("x-1", "cluster-a", "fit", now - 100.0, extras=invented),
        loss_report_record("x-2", "cluster-a", "price", now - 90.0, extras=invented),
    ]
    reports = build_loss_report(log, loss_report_window)

    # Concrete: the specific inventions are gone.
    for needle in (41.75, 7.25, 1234.56, 311.5, 288.25, "store-rival-gamma"):
        _assert_absent(reports, needle)

    # Structural: NOTHING the projection does not read survives anywhere in the output.
    # `PROJECTED_FIELDS` is the module's own statement of what it reads, so this assertion
    # tightens automatically if that list is ever widened.
    read: set[Any] = set()
    for record in log:
        for name in PROJECTED_FIELDS:
            for _, value in _scalars(record.get(name)):
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    read.add(value)
    residue = {
        value
        for record in log
        for _, value in _scalars(record)
        if isinstance(value, (str, float)) and not isinstance(value, bool) and value not in read
    }
    assert residue, "the probe is vacuous unless the rows carry something unprojected"
    for value in residue:
        _assert_absent(reports, value)


def test_the_projection_reads_only_what_the_report_is_defined_to_carry() -> None:
    from apps.exchange.src.reports import PROJECTED_FIELDS

    assert set(PROJECTED_FIELDS) == {
        "store_id",
        "cluster_id",
        "reason",
        "ts",
        "won",
        "unmet_criteria",
    }, (
        "widening the projection widens what a report can leak; if a field is added here, "
        "the amount-suppression argument has to be re-made for it"
    )


def test_a_criterion_that_embeds_an_amount_or_a_rival_is_dropped_not_copied(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    from apps.exchange.src.reports import build_loss_report

    now = loss_report_window["end"]
    log = [
        loss_report_record(
            "c-1",
            "cluster-a",
            "price",
            now - 100.0,
            unmet=(
                "capacity_l >= 30",
                "undercut by 142.25",
                "store-rival-alpha shipped faster",
            ),
        )
    ]
    entry = _by_cluster(_only(build_loss_report(log, loss_report_window), "store-loser"))
    criteria = list(entry["cluster-a"].unmet_criteria)
    assert criteria == ["capacity_l >= 30"], (
        "the clean criterion survives; the two that carry a rival's price or name do not"
    )
    assert entry["cluster-a"].lost == 1, "redacting a criterion does not drop the loss itself"


def test_unmet_criteria_are_deduplicated_and_ordered(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    from apps.exchange.src.reports import build_loss_report

    now = loss_report_window["end"]
    log = [
        loss_report_record(
            "d-1", "cluster-a", "fit", now - 50.0, unmet=("weight_kg <= 1", "capacity_l >= 30")
        ),
        loss_report_record("d-2", "cluster-a", "fit", now - 40.0, unmet=("capacity_l >= 30",)),
    ]
    entry = _by_cluster(_only(build_loss_report(log, loss_report_window), "store-loser"))
    assert list(entry["cluster-a"].unmet_criteria) == ["capacity_l >= 30", "weight_kg <= 1"]


# ---------------------------------------------------------------------------------------
# 4. The counting invariant — what proves the integers are counts rather than copies.
# ---------------------------------------------------------------------------------------
def test_lost_equals_the_sum_of_its_reason_counts_and_the_rows_counted(
    loss_report_log: list[dict[str, Any]], loss_report_window: dict[str, float]
) -> None:
    from apps.exchange.src.reports import build_loss_report

    reports = build_loss_report(loss_report_log, loss_report_window)
    start, end = loss_report_window["start"], loss_report_window["end"]
    for report in reports:
        expected_rows = sum(
            1
            for row in loss_report_log
            if row["store_id"] == report.store_id and not row["won"] and start <= row["ts"] <= end
        )
        assert sum(e.lost for e in report.by_cluster) == expected_rows
        for entry in report.by_cluster:
            assert entry.lost == sum(_counts(entry).values()), entry.cluster_id


# ---------------------------------------------------------------------------------------
# 5. Fail-closed edges.
# ---------------------------------------------------------------------------------------
def test_every_reason_the_contract_publishes_is_accepted(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    from contracts import LossReasons

    from apps.exchange.src.reports import build_loss_report

    now = loss_report_window["end"]
    for reason in LossReasons.model_fields:
        log = [loss_report_record("r-1", "cluster-a", reason, now - 10.0)]
        entry = _by_cluster(_only(build_loss_report(log, loss_report_window), "store-loser"))
        assert _counts(entry["cluster-a"])[reason] == 1, reason


def test_an_unknown_reason_is_refused_rather_than_silently_dropped(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    from apps.exchange.src.reports import build_loss_report

    log = [loss_report_record("u-1", "cluster-a", "vibes", loss_report_window["end"] - 10.0)]
    with pytest.raises(ValueError, match="vibes"):
        build_loss_report(log, loss_report_window)


def test_a_malformed_row_is_refused_without_echoing_its_amounts(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    from apps.exchange.src.reports import build_loss_report

    row = loss_report_record("m-1", "cluster-a", "fit", loss_report_window["end"] - 10.0)
    del row["cluster_id"]
    with pytest.raises(ValueError) as excinfo:
        build_loss_report([row], loss_report_window)
    message = str(excinfo.value)
    assert "cluster_id" in message
    for leak in ("189.95", "142.25", "store-rival-alpha"):
        assert leak not in message, f"the refusal itself leaks {leak!r}: {message!r}"


def test_an_empty_or_fully_filtered_log_yields_no_report(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    from apps.exchange.src.reports import build_loss_report

    assert build_loss_report([], loss_report_window) == []
    stale = [loss_report_record("s-1", "cluster-a", "fit", loss_report_window["start"] - 10_000.0)]
    assert build_loss_report(stale, loss_report_window) == []


def test_the_builder_neither_mutates_its_input_nor_varies_between_calls(
    loss_report_log: list[dict[str, Any]], loss_report_window: dict[str, float]
) -> None:
    import copy

    from apps.exchange.src.reports import build_loss_report

    before = copy.deepcopy(loss_report_log)
    first = build_loss_report(loss_report_log, loss_report_window)
    second = build_loss_report(loss_report_log, loss_report_window)
    assert loss_report_log == before, "the auction log is read-only input"
    assert [r.model_dump() for r in first] == [r.model_dump() for r in second]


def test_rows_may_be_objects_rather_than_mappings(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    from apps.exchange.src.reports import build_loss_report

    now = loss_report_window["end"]
    rows = [
        types.SimpleNamespace(**loss_report_record("o-1", "cluster-a", "fit", now - 10.0)),
        types.SimpleNamespace(**loss_report_record("o-2", "cluster-a", "trust", now - 20.0)),
    ]
    entry = _by_cluster(_only(build_loss_report(rows, loss_report_window), "store-loser"))
    assert _counts(entry["cluster-a"]) == {"fit": 1, "price": 0, "commitments": 0, "trust": 1}
    for needle in (189.95, 142.25, "store-rival-alpha"):
        _assert_absent(build_loss_report(rows, loss_report_window), needle)


# ---------------------------------------------------------------------------------------
# 6. Three log shapes the first build got wrong, each one measured before it was fixed.
# ---------------------------------------------------------------------------------------
def test_an_amount_smuggled_inside_a_structured_criterion_is_still_redacted(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    """A row that writes the winning price twice must not thereby launder it.

    ``unmet_criteria`` is projected, so anything reachable inside it once looked "read" — and
    a row carrying the same number under ``winning_price`` and inside a structured criterion
    made ``winning_price`` look read too, so neither copy was residue and the criterion sailed
    through. Measured: the report came back holding
    ``"{'field': 'price', 'beat_by': 142.25}"``.
    """
    from apps.exchange.src.reports import build_loss_report

    row = loss_report_record(
        "s-1",
        "cluster-a",
        "price",
        loss_report_window["end"] - 10.0,
        unmet=({"field": "price", "beat_by": 142.25},),
        winning_price=142.25,
    )
    reports = build_loss_report([row], loss_report_window)
    entry = _by_cluster(_only(reports, "store-loser"))["cluster-a"]
    assert list(entry.unmet_criteria) == [], "the structured criterion carried the winning price"
    assert entry.lost == 1
    _assert_absent(reports, 142.25)


def test_a_rival_that_also_loses_does_not_refuse_the_whole_file(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    """In a real auction log every rival is somebody's losing store.

    Measured on the first build: scanning a report against each row's *own* unread scalars
    flagged ``store-a``'s id inside ``store-a``'s own report — because a different row named
    it as the rival — and raised ``LossReportLeak`` for every report in the log. A privacy
    guard that refuses all output is not a stricter guard, it is a broken one.
    """
    from apps.exchange.src.reports import build_loss_report

    now = loss_report_window["end"]
    log = [
        loss_report_record(
            "v-1", "cluster-a", "price", now - 10.0, store_id="store-a", rival="store-b"
        ),
        loss_report_record(
            "v-2", "cluster-a", "price", now - 20.0, store_id="store-b", rival="store-a"
        ),
    ]
    reports = build_loss_report(log, loss_report_window)
    assert [r.store_id for r in reports] == ["store-a", "store-b"]
    assert all(r.by_cluster[0].lost == 1 for r in reports)


def test_a_store_id_that_contains_a_rivals_id_is_not_mistaken_for_a_leak(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    """``store-b-north`` losing to ``store-b`` is a substring, not a disclosure."""
    from apps.exchange.src.reports import build_loss_report

    log = [
        loss_report_record(
            "p-1",
            "cluster-a",
            "trust",
            loss_report_window["end"] - 10.0,
            store_id="store-b-north",
            rival="store-b",
        )
    ]
    report = _only(build_loss_report(log, loss_report_window), "store-b-north")
    assert report.by_cluster[0].lost == 1


def test_the_log_may_be_any_iterable_and_is_read_once(
    loss_report_record: Any, loss_report_window: dict[str, float]
) -> None:
    from apps.exchange.src.reports import build_loss_report

    now = loss_report_window["end"]
    rows = [
        loss_report_record("g-1", "cluster-a", "fit", now - 10.0),
        loss_report_record("g-2", "cluster-b", "trust", now - 20.0),
    ]
    from_generator = build_loss_report((row for row in rows), loss_report_window)
    from_tuple = build_loss_report(tuple(rows), loss_report_window)
    assert [r.model_dump() for r in from_generator] == [r.model_dump() for r in from_tuple]
    assert len(from_generator[0].by_cluster) == 2
