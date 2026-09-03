"""Aggregate an auction log into per-store loss reports (R9).

A losing store is told *why* it lost — how many bids in each cluster fell to fit, price,
commitments or trust, and which buyer criteria it failed to meet. It is told nothing else.
The rows this reads carry the rival's identity, the winning price and the store's own offer
amounts, because the exchange needs those for jobs that are not this one; a report that
copied them through would turn a seller feedback loop into a price-intelligence feed.

How the amounts are kept out
============================

Not by a blacklist. A list of forbidden field names is only ever as complete as the memory
of whoever wrote it, and it is silently wrong the day a new money field is added to the
auction log. The suppression here is the other direction, and it has three layers:

1. **A total projection.** :data:`PROJECTED_FIELDS` is the complete set of row fields this
   module reads. Every row is converted to a :class:`_Loss` — six values, no reference back
   to the row — before any aggregation happens, so after that line there is no live object
   an amount could be copied out of. Adding a field to the report means widening
   ``PROJECTED_FIELDS``, which is a visible edit with a test standing on it.

2. **Redaction of the one free-text channel.** ``unmet_criteria`` is carried verbatim, so it
   is the only place a value can ride through a field that is itself permitted. Every
   criterion is checked against the row's *residue* — the scalars of that row the projection
   did not keep — and dropped if it contains one. A criterion naming the price that beat you
   is not a criterion, it is the winning price with a sentence around it.

3. **An egress check on the finished report.** Every string and number in the built report is
   scanned against the log's residue, and a hit raises :class:`LossReportLeak` rather than
   returning. Layer 1 is what makes leaking hard; this is what makes a mistake in layer 1
   loud — it is what would catch a future field copied straight off a row. Integers equal to
   a tally this build computed are passed as proven-derived rather than scanned; every other
   integer is scanned, so a new integer field cannot quietly become a money field.

The two residues in :func:`_residues` are not redundant. Redaction uses the strict, per-row
set, because a criterion naming a rival has to go even when that rival loses an auction of
its own further down the log. The egress scan uses the pooled set, because in a real auction
log every rival is also somebody's losing store — scanning with the strict set flags the
store's own id in its own report and refuses every report in the file.

Because the residue is derived from the input rather than from a list of names, a money field
nobody here has ever heard of is suppressed on the day it is added.

.. code-block:: python

    from apps.exchange.src.reports import build_loss_report

    reports = build_loss_report(auction_log, {"start": t0, "end": t1})
    for report in reports:              # one per losing store, ordered by store_id
        for entry in report.by_cluster:  # ordered by cluster_id
            entry.lost, entry.reasons.fit, entry.unmet_criteria

Window bounds are inclusive at both ends and may be epoch seconds or RFC-3339 instants, in
either the rows or the window; the window is echoed back in whatever form the caller passed.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from contracts import ClusterLoss, LossReasons, LossReport, LossWindow
from contracts.boundary import parse_timestamp

__all__ = [
    "PROJECTED_FIELDS",
    "REASON_CATEGORIES",
    "LossReportLeak",
    "build_loss_report",
]

#: Every field of an auction-log row this module reads. Nothing else in a row is in scope
#: after :func:`_project`, which is the whole amount-suppression argument: see the module
#: docstring. Widening this widens what a report can carry, so it is asserted by the gate.
PROJECTED_FIELDS: tuple[str, ...] = (
    "store_id",
    "cluster_id",
    "reason",
    "ts",
    "won",
    "unmet_criteria",
)

#: The reason categories, taken from the contract rather than restated. ``LossReasons``
#: closes its property set on purpose — a new reason is a schema change — so deriving the
#: accepted set from it means this module cannot drift from what a report can express.
REASON_CATEGORIES: tuple[str, ...] = tuple(LossReasons.model_fields)

# A one- or two-character needle matches everywhere and means nothing: "30" would redact
# "capacity_l >= 30" the moment any unread field happened to hold 30. Three is the shortest
# needle that carries information.
_MIN_NEEDLE = 3

_MISSING = object()


class LossReportLeak(RuntimeError):
    """The finished report carried a value the projection never read.

    Raised instead of returning. A report is a privacy boundary, and a boundary that reports
    its own breach by handing the caller the breached object is not one.
    """


# ---------------------------------------------------------------------------------------
# Tolerant field access. Rows arrive as mappings from the ledger and as objects from the
# in-process auction, and the difference is not interesting to a report.
# ---------------------------------------------------------------------------------------
def _get(row: Any, name: str, default: Any = _MISSING) -> Any:
    if isinstance(row, Mapping):
        value = row.get(name, default)
    else:
        value = getattr(row, name, default)
    return value


def _walk(node: Any, seen: set[int] | None = None) -> Iterator[Any]:
    """Yield every scalar reachable from ``node``, through mappings, sequences and objects."""
    if node is None or isinstance(node, (str, bytes, bool, int, float)):
        yield node
        return
    if seen is None:
        seen = set()
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, Mapping):
        for value in node.values():
            yield from _walk(value, seen)
        return
    if isinstance(node, (list, tuple, set, frozenset)):
        for value in node:
            yield from _walk(value, seen)
        return
    attributes = getattr(node, "__dict__", None)
    if isinstance(attributes, Mapping):
        for value in attributes.values():
            yield from _walk(value, seen)
        return
    yield node


def _numeral_forms(value: float) -> set[str]:
    """The text spellings a number could hide inside a string as."""
    forms = {str(value), repr(value), f"{float(value)}", f"{float(value):g}"}
    return {form for form in forms if len(form) >= _MIN_NEEDLE}


@dataclass(frozen=True)
class _Residue:
    """What the projection did not read, in every form it could be recognised by."""

    texts: frozenset[str] = frozenset()
    numbers: frozenset[float] = frozenset()
    numerals: frozenset[str] = frozenset()

    def hides_in(self, text: str) -> str | None:
        low = text.casefold()
        for needle in self.texts:
            if needle in low:
                return needle
        for numeral in self.numerals:
            if numeral in low:
                return numeral
        return None


def _read_of(row: Any, loss: _Loss) -> set[Any]:
    """The scalars the projection actually kept out of ``row``.

    Kept, not *reachable*: the criteria are the strings :func:`_project` produced, and this
    deliberately does not descend into whatever they were built from. Walking inside
    ``unmet_criteria`` would let a row smuggle an amount past the residue check by writing it
    both under a money field and inside a structured criterion — the criterion copy would
    make the money field look "read", and both would then ride through.
    """
    read: set[Any] = set(loss.criteria) | {loss.store_id, loss.cluster_id, loss.reason}
    for name in ("store_id", "cluster_id", "reason", "ts"):
        raw = _get(row, name, None)
        if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
            read.add(raw)
    return read


def _residues(pairs: list[tuple[Any, _Loss]]) -> tuple[_Residue, _Residue, frozenset[Any]]:
    """Two residues over the same log, because they answer two different questions.

    ``strict`` pools each row's *own* unread scalars, and is what a criterion is redacted
    against: a criterion naming a rival must go even when that rival happens to lose an
    auction of its own further down the log.

    ``egress`` subtracts everything the projection read *anywhere* in the log, and is what
    the finished report is scanned against. It has to be the looser of the two, because in a
    real auction log every rival is also somebody's losing store — scanning a report with
    the strict set would flag the store's own id in its own report and refuse every report
    in the file. The third return value is that pooled read set: a value the projection read
    somewhere is a value the report is entitled to carry.
    """
    strict_texts: set[str] = set()
    strict_numbers: set[float] = set()
    strict_numerals: set[str] = set()
    seen_texts: set[str] = set()
    seen_numbers: set[float] = set()
    read_global: set[Any] = set()

    for row, loss in pairs:
        read = _read_of(row, loss)
        read_global |= read
        for value in _walk(row):
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, str):
                if len(value.strip()) < _MIN_NEEDLE:
                    continue
                seen_texts.add(value.casefold())
                if value not in read:
                    strict_texts.add(value.casefold())
            elif isinstance(value, (int, float)):
                seen_numbers.add(float(value))
                if value not in read:
                    strict_numbers.add(float(value))
                    strict_numerals.update(_numeral_forms(value))

    read_texts = {v.casefold() for v in read_global if isinstance(v, str)}
    read_numbers = {float(v) for v in read_global if isinstance(v, (int, float))}
    egress_numbers = seen_numbers - read_numbers
    egress = _Residue(
        frozenset(seen_texts - read_texts),
        frozenset(egress_numbers),
        frozenset().union(*(_numeral_forms(n) for n in egress_numbers))
        if egress_numbers
        else frozenset(),
    )
    strict = _Residue(
        frozenset(strict_texts), frozenset(strict_numbers), frozenset(strict_numerals)
    )
    return strict, egress, frozenset(read_global)


# ---------------------------------------------------------------------------------------
# Projection: a row becomes six values, and the row itself goes out of scope.
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Loss:
    store_id: str
    cluster_id: str
    reason: str
    at: datetime
    won: bool
    criteria: tuple[str, ...]


def _required_text(row: Any, name: str, index: int) -> str:
    value = _get(row, name)
    if value is _MISSING or value is None or not str(value).strip():
        raise ValueError(f"auction log row {index}: {name!r} is missing or blank")
    return str(value).strip()


def _project(row: Any, index: int) -> _Loss:
    """One row, reduced to what a report is allowed to know about it.

    Refusal messages name the field and the row index and never the row's contents: an
    exception that quotes the offending record to explain itself is one more way for an
    amount to reach a log line, a bug report, or a merchant-facing error.
    """
    store_id = _required_text(row, "store_id", index)
    cluster_id = _required_text(row, "cluster_id", index)

    reason = _required_text(row, "reason", index).casefold()
    if reason not in REASON_CATEGORIES:
        raise ValueError(
            f"auction log row {index}: unknown loss reason {reason[:40]!r}; "
            f"the contract publishes {list(REASON_CATEGORIES)}"
        )

    raw_ts = _get(row, "ts")
    if raw_ts is _MISSING:
        raise ValueError(f"auction log row {index}: 'ts' is missing or blank")
    at = parse_timestamp(raw_ts)
    if at is None:
        raise ValueError(f"auction log row {index}: 'ts' is not a readable instant")

    raw_criteria = _get(row, "unmet_criteria", ())
    if raw_criteria is _MISSING or raw_criteria is None:
        raw_criteria = ()
    if isinstance(raw_criteria, (str, bytes)) or isinstance(raw_criteria, Mapping):
        raw_criteria = [raw_criteria]
    criteria = tuple(str(item).strip() for item in raw_criteria if str(item).strip())

    # A row that does not say whether it won is a loss: an auction log of losses is the
    # common shape, and defaulting the other way would silently empty every report.
    return _Loss(store_id, cluster_id, reason, at, bool(_get(row, "won", False)), criteria)


# ---------------------------------------------------------------------------------------
# Aggregation.
# ---------------------------------------------------------------------------------------
@dataclass
class _Tally:
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(REASON_CATEGORIES, 0))
    criteria: set[str] = field(default_factory=set)

    @property
    def lost(self) -> int:
        return sum(self.counts.values())


def _window_bounds(window: Any) -> tuple[datetime, datetime, LossWindow]:
    raw_start, raw_end = _get(window, "start"), _get(window, "end")
    if raw_start is _MISSING or raw_end is _MISSING:
        raise ValueError("window must carry both 'start' and 'end'")
    start, end = parse_timestamp(raw_start), parse_timestamp(raw_end)
    if start is None or end is None:
        raise ValueError("window bounds are not readable instants")
    if end < start:
        raise ValueError("window 'end' precedes 'start'")
    if not isinstance(raw_start, (str, int, float)) or isinstance(raw_start, bool):
        raise ValueError("window 'start' must be epoch seconds or an RFC-3339 instant")
    if not isinstance(raw_end, (str, int, float)) or isinstance(raw_end, bool):
        raise ValueError("window 'end' must be epoch seconds or an RFC-3339 instant")
    return start, end, LossWindow(start=raw_start, end=raw_end)


def _assert_no_residue(
    report: LossReport,
    residue: _Residue,
    counts: frozenset[int],
    carried: frozenset[Any],
) -> None:
    """Refuse to hand back a report containing anything the projection did not read.

    Three kinds of value are passed without scanning, and each one is passed for a reason
    that survives a new field being added to the schema:

    ``carried``   a value the projection read somewhere in this log. It is published by
                  definition — the store's own id, a cluster id, a criterion that survived
                  redaction — and matching residue *inside* it says only that some rival's
                  id is a prefix of the store's own.
    ``counts``    the tallies this build computed. An integer equal to one of them is proven
                  derived rather than copied; any other integer is scanned like everything
                  else, so a future integer field cannot quietly become a money field.
    window bounds the caller's own argument, echoed back to the caller.
    """
    bounds = {report.window.start, report.window.end}
    for path, value in _dumped(report.model_dump()):
        if value is None or isinstance(value, bool) or value in bounds:
            continue
        if isinstance(value, str):
            if value in carried:
                continue
            needle = residue.hides_in(value)
            if needle is not None:
                raise LossReportLeak(
                    f"loss report for {report.store_id!r} carries an unprojected value at "
                    f"{path}: matched {needle!r}"
                )
        elif isinstance(value, int) and value in counts:
            continue
        elif isinstance(value, (int, float)) and value not in carried:
            if float(value) in residue.numbers:
                raise LossReportLeak(
                    f"loss report for {report.store_id!r} carries an unprojected number at {path}"
                )


def _dumped(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _dumped(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _dumped(value, f"{path}[{index}]")
    else:
        yield (path, node)


def build_loss_report(auction_log: Any, window: Any) -> list[LossReport]:
    """Aggregate ``auction_log`` into one :class:`~contracts.LossReport` per losing store.

    ``auction_log`` is any iterable of rows, each a mapping or an object carrying at least
    ``store_id``, ``cluster_id``, ``reason`` and ``ts``; ``window`` carries ``start`` and
    ``end`` as epoch seconds or RFC-3339 instants. Rows that won, and rows whose instant
    falls outside the inclusive window, are not counted.

    Returns a list ordered by ``store_id``, each report's clusters ordered by ``cluster_id``
    and each cluster's criteria deduplicated and sorted — a report that reordered itself
    between two runs over the same log would make every diff of it unreadable.

    Raises :class:`ValueError` for a malformed row or window and :class:`LossReportLeak` if
    the finished report would carry a value the projection did not read.
    """
    rows = list(auction_log)
    start, end, echoed = _window_bounds(window)
    # Every row is projected before anything is aggregated, and the residue is measured
    # against what the projection kept — so a malformed row two thousand lines in refuses the
    # whole report rather than half-building one.
    pairs = [(row, _project(row, index)) for index, row in enumerate(rows)]
    strict, egress, carried = _residues(pairs)

    tallies: dict[str, dict[str, _Tally]] = {}
    for _row, loss in pairs:
        if loss.won or not (start <= loss.at <= end):
            continue
        tally = tallies.setdefault(loss.store_id, {}).setdefault(loss.cluster_id, _Tally())
        tally.counts[loss.reason] += 1
        # The one channel carried as free text, so the one that needs redacting.
        tally.criteria.update(c for c in loss.criteria if strict.hides_in(c) is None)

    reports: list[LossReport] = []
    for store_id in sorted(tallies):
        by_cluster = [
            ClusterLoss(
                cluster_id=cluster_id,
                lost=tally.lost,
                reasons=LossReasons(**tally.counts),
                unmet_criteria=sorted(tally.criteria),
            )
            for cluster_id, tally in sorted(tallies[store_id].items())
        ]
        report = LossReport(store_id=store_id, window=echoed, by_cluster=by_cluster)
        counts = frozenset(
            {entry.lost for entry in by_cluster}
            | {count for tally in tallies[store_id].values() for count in tally.counts.values()}
        )
        _assert_no_residue(report, egress, counts, carried)
        reports.append(report)
    return reports
