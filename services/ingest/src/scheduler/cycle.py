"""The durable, cadence-driven refresh scheduler (T-024).

This is the thing ``catalog.py``'s docstring said was not built here. It is the middle of three
pieces and holds none of the other two's jobs: :mod:`ingest.scheduler.cadence` says *how stale each
field may get*, :mod:`ingest.scheduler.state` says *what is remembered across a restart*, and this
module answers one question on demand —

    given the clock, which stores are overdue, and which adapters have to run to fix that?

and then runs exactly those adapters, through the same code path a hand-triggered
``POST /refresh/{store_id}`` uses.

**A tick is driven, not looped.** ``POST /schedule/tick`` runs one, from cron, a Kubernetes
CronJob, or an operator with ``curl``. There is deliberately no background thread: a timer inside
the process would make the schedule depend on process uptime — the exact coupling the durable state
exists to break — and a service that had been up for four minutes would have a four-minute-old
schedule. Because the decision is recomputed from persisted timestamps every time, a tick that is
missed, duplicated, or delivered to a freshly restarted process still produces the right answer.
That is what makes the cadence a property of the *data* rather than of the runtime.

**The clock is a seam.** Everything here reads ``clock.now()``; nothing calls ``datetime.now()``
except :class:`SystemClock`, which is the production implementation of that one method. Tests pass
``proxyshop_support.clock.ManualClock`` — the repo's existing injectable clock — and advance it,
so a test of a weekly cadence costs no wall-clock time and has no tolerance window to be flaky in.

**A failed refresh does not advance the clock.** If the crawl raises, the store's fields keep their
old ``last_refreshed`` and the store is still due on the next tick. The alternative — stamping the
fields fresh because a refresh was *attempted* — makes an unreachable store look current forever,
which is worse than not scheduling it at all.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .cadence import CadenceConfig, FieldCadence, sections_of
from .catalog import StoreRegistry
from .state import MemoryScheduleStore, ScheduleState, ScheduleStore, StoreSchedule

__all__ = [
    "FieldStatus",
    "RefreshScheduler",
    "StoreFailure",
    "StorePlan",
    "StoreRun",
    "StoreSkip",
    "SystemClock",
    "TickReport",
    "build_scheduler",
]

_log = logging.getLogger(__name__)


class Clock(Protocol):
    """The one method this module needs from a clock. ``ManualClock`` satisfies it."""

    def now(self) -> datetime:
        """The current instant, timezone-aware."""


class SystemClock:
    """The wall clock, as the same one-method seam a :class:`ManualClock` presents."""

    def now(self) -> datetime:
        """The current UTC instant."""
        return datetime.now(UTC)

    def __repr__(self) -> str:
        return "SystemClock()"


@dataclass(frozen=True)
class FieldStatus:
    """One field of one store, and whether it has gone stale.

    Attributes:
        field: the cadence entry's field name.
        section: the surface that refreshes it.
        max_age_seconds: how old it may get.
        last_refreshed: when it was last made fresh, or ``None`` if never.
        due_at: when it goes stale, or ``None`` when it never has been refreshed and is
            therefore already due.
        due: whether it is stale as of the instant this status was computed.
    """

    field: str
    section: str
    max_age_seconds: int
    last_refreshed: datetime | None
    due_at: datetime | None
    due: bool

    def as_dict(self) -> dict[str, Any]:
        """The JSON-safe form the ``GET /schedule`` route publishes."""
        return {
            "field": self.field,
            "section": self.section,
            "max_age_seconds": self.max_age_seconds,
            "last_refreshed": _iso(self.last_refreshed),
            "due_at": _iso(self.due_at),
            "due": self.due,
        }


@dataclass(frozen=True)
class StorePlan:
    """What one store needs doing at a given instant."""

    store_id: str
    at: datetime
    fields: tuple[FieldStatus, ...]

    @property
    def due_fields(self) -> tuple[str, ...]:
        """The names of the fields that have gone stale."""
        return tuple(status.field for status in self.fields if status.due)

    @property
    def due_sections(self) -> tuple[str, ...]:
        """Which adapters have to run — the union of the overdue fields' sections.

        This is the sentence the ticket asks for: *cadence config drives which adapters run*.
        An empty tuple means the store is current and the tick touches neither adapter.
        """
        return sections_of([_entry(status) for status in self.fields if status.due])

    @property
    def refreshed_by(self) -> tuple[str, ...]:
        """Every field a run of :attr:`due_sections` would make fresh.

        A superset of :attr:`due_fields`, and deliberately: one catalog crawl re-reads every
        catalog field, so a run triggered by an overdue ``offer.price`` also refreshes
        ``product.title``. Stamping only the fields that happened to be *due* would leave the
        slow ones claiming a staleness the crawl already fixed, and re-trigger a redundant run.
        """
        sections = set(self.due_sections)
        return tuple(status.field for status in self.fields if status.section in sections)

    @property
    def next_due_at(self) -> datetime | None:
        """The earliest instant this store next needs a run, or ``None`` if it never does."""
        candidates = [status.due_at for status in self.fields if status.due_at is not None]
        if any(status.due_at is None for status in self.fields):
            return self.at
        return min(candidates) if candidates else None

    def as_dict(self) -> dict[str, Any]:
        """The JSON-safe form the ``GET /schedule`` route publishes."""
        return {
            "store_id": self.store_id,
            "due": bool(self.due_sections),
            "due_sections": list(self.due_sections),
            "due_fields": list(self.due_fields),
            "next_due_at": _iso(self.next_due_at),
            "fields": [status.as_dict() for status in self.fields],
        }


@dataclass(frozen=True)
class StoreRun:
    """One store that a tick actually refreshed."""

    store_id: str
    sections: tuple[str, ...]
    fields: tuple[str, ...]
    job_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "sections": list(self.sections),
            "fields": list(self.fields),
            "job_id": self.job_id,
        }


@dataclass(frozen=True)
class StoreSkip:
    """One store a tick left alone because nothing it holds had gone stale."""

    store_id: str
    next_due_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {"store_id": self.store_id, "next_due_at": _iso(self.next_due_at)}


@dataclass(frozen=True)
class StoreFailure:
    """One store whose refresh raised. Its clock is deliberately not advanced."""

    store_id: str
    sections: tuple[str, ...]
    error: str

    def as_dict(self) -> dict[str, Any]:
        return {"store_id": self.store_id, "sections": list(self.sections), "error": self.error}


@dataclass(frozen=True)
class TickReport:
    """What one tick did."""

    at: datetime
    ran: tuple[StoreRun, ...] = ()
    skipped: tuple[StoreSkip, ...] = ()
    failed: tuple[StoreFailure, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """The JSON-safe form the ``POST /schedule/tick`` route publishes."""
        return {
            "at": _iso(self.at),
            "ran": [run.as_dict() for run in self.ran],
            "skipped": [skip.as_dict() for skip in self.skipped],
            "failed": [failure.as_dict() for failure in self.failed],
        }


#: What a tick calls to actually refresh a store: ``perform(store_id, sections, force=...)``.
#: ``ingest.scheduler.routes.perform_refresh`` is the production one, which is the *same*
#: function ``POST /refresh/{store_id}`` calls — the scheduler is another caller of the manual
#: path, never a parallel implementation of it.
PerformRefresh = Callable[..., Any]


class RefreshScheduler:
    """Decides which stores are overdue and runs the adapters that make them current.

    Args:
        registry: the stores this process may refresh. The same object the route holds.
        perform: how a store is refreshed — see :data:`PerformRefresh`.
        cadence: the field cadence table. Defaults to :meth:`CadenceConfig.from_env`.
        store: where the schedule is persisted. Defaults to a non-durable in-memory store, and
            says so in the log, because a default file path is a persistence claim this module
            has no standing to make.
        runner: the catalog runner whose differential ledger is persisted alongside the refresh
            clock. Optional only so a planning-only scheduler can be built without one.
        clock: the instant source. Inject ``ManualClock`` in tests.
    """

    def __init__(
        self,
        *,
        registry: StoreRegistry,
        perform: PerformRefresh,
        cadence: CadenceConfig | None = None,
        store: ScheduleStore | None = None,
        runner: Any = None,
        clock: Clock | None = None,
    ) -> None:
        self.registry = registry
        self.perform = perform
        self.cadence = cadence if cadence is not None else CadenceConfig.from_env()
        self.store: ScheduleStore = store if store is not None else MemoryScheduleStore()
        self.runner = runner
        self.clock: Clock = clock if clock is not None else SystemClock()
        self._state: ScheduleState = self.store.load()
        self.hydrate()

    # -- durability --------------------------------------------------------------------------

    @property
    def state(self) -> ScheduleState:
        """The in-memory schedule. Mutating it does not persist until a tick saves."""
        return self._state

    @property
    def durable(self) -> bool:
        """Whether this scheduler's state survives the process."""
        return bool(getattr(self.store, "durable", False))

    def reload(self) -> ScheduleState:
        """Re-read the persisted state, discarding anything unsaved. Returns it."""
        self._state = self.store.load()
        self.hydrate()
        return self._state

    def hydrate(self) -> None:
        """Push the persisted differential ledgers back into the runner.

        This is what makes the hash gating survive a restart, and therefore what makes "no
        re-extraction without a hash change" true *across a full cycle* rather than only within
        one process's lifetime. Without it the first refresh after every redeploy re-extracts a
        catalog nobody has touched.
        """
        if self.runner is None:
            return
        for store_id, schedule in self._state.stores.items():
            if schedule.hashes:
                self.runner.hashes[store_id] = dict(schedule.hashes)

    def persist(self) -> None:
        """Write the in-memory schedule out."""
        self.store.save(self._state)

    def forget(self, store_id: str) -> None:
        """Drop everything remembered about one store, so its next tick refreshes it."""
        self._state.stores.pop(str(store_id), None)

    # -- planning ----------------------------------------------------------------------------

    def status_for(self, entry: FieldCadence, schedule: StoreSchedule, at: datetime) -> FieldStatus:
        """Whether one field of one store has gone stale as of ``at``."""
        last = schedule.last_refreshed.get(entry.field)
        due_at = last + timedelta(seconds=entry.max_age_seconds) if last is not None else None
        return FieldStatus(
            field=entry.field,
            section=entry.section,
            max_age_seconds=entry.max_age_seconds,
            last_refreshed=last,
            due_at=due_at,
            # A field that has never been refreshed is due; one whose deadline has arrived is
            # due. `>=` rather than `>` so a cadence of N seconds fires exactly N seconds later
            # rather than on the following tick.
            due=due_at is None or at >= due_at,
        )

    def plan_for(self, store_id: str, *, now: datetime | None = None) -> StorePlan:
        """What ``store_id`` needs doing, as of ``now`` (default: the clock)."""
        at = now if now is not None else self.clock.now()
        schedule = self._state.stores.get(str(store_id), StoreSchedule())
        return StorePlan(
            store_id=str(store_id),
            at=at,
            fields=tuple(self.status_for(entry, schedule, at) for entry in self.cadence),
        )

    def plan(self, *, now: datetime | None = None) -> tuple[StorePlan, ...]:
        """The plan for every registered store, in store-id order."""
        at = now if now is not None else self.clock.now()
        return tuple(self.plan_for(store_id, now=at) for store_id in self.registry.store_ids)

    # -- the tick ----------------------------------------------------------------------------

    def tick(self, *, now: datetime | None = None, force: bool = False) -> TickReport:
        """Refresh every store that has gone stale, and nothing else.

        Args:
            now: the instant to decide against. Defaults to the clock.
            force: treat every field as due, so every store is refreshed. This overrides the
                *cadence clock* only — the differential hash ledger is untouched, so a forced
                tick of an unchanged store still performs no graph work. Dropping the ledger too
                is what ``POST /refresh/{store_id}`` with ``{"force": true}`` is for.

        Returns:
            A :class:`TickReport`. The schedule is persisted before it returns, so a process that
            dies immediately afterwards has still recorded what it did.
        """
        at = now if now is not None else self.clock.now()
        ran: list[StoreRun] = []
        skipped: list[StoreSkip] = []
        failed: list[StoreFailure] = []

        for store_id in self.registry.store_ids:
            plan = self.plan_for(store_id, now=at)
            sections = self.cadence.sections if force else plan.due_sections
            if not sections:
                skipped.append(StoreSkip(store_id=store_id, next_due_at=plan.next_due_at))
                continue
            fields = (
                self.cadence.fields
                if force
                else plan.refreshed_by  # every field the run makes fresh, not only the overdue
            )
            try:
                report = self.perform(store_id, sections, force=False)
            except Exception as exc:  # noqa: BLE001 - a crawl's failures are not a closed set
                # The clock is NOT advanced. A store that could not be read is still stale, and
                # the next tick must try it again.
                _log.warning(
                    "scheduled refresh store=%s sections=%s failed: %s: %s",
                    store_id,
                    list(sections),
                    type(exc).__name__,
                    exc,
                )
                failed.append(
                    StoreFailure(
                        store_id=store_id,
                        sections=tuple(sections),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            schedule = self._state.for_store(store_id)
            for name in fields:
                schedule.last_refreshed[name] = at
            if self.runner is not None:
                schedule.hashes = dict(self.runner.hashes.get(store_id, {}))
            ran.append(
                StoreRun(
                    store_id=store_id,
                    sections=tuple(sections),
                    fields=tuple(fields),
                    job_id=getattr(report, "job_id", None),
                )
            )

        # Saved unconditionally, including a tick where every store was skipped: the point of a
        # durable schedule is that the file is the truth, and a save that only happened when work
        # was done would leave a brand-new deployment with no file at all until its first crawl.
        self.persist()
        return TickReport(at=at, ran=tuple(ran), skipped=tuple(skipped), failed=tuple(failed))


def _entry(status: FieldStatus) -> FieldCadence:
    """The cadence entry a status came from — enough of one for :func:`sections_of`."""
    return FieldCadence(
        field=status.field, section=status.section, max_age_seconds=status.max_age_seconds
    )


def _iso(moment: datetime | None) -> str | None:
    """An instant as ISO-8601 UTC, or ``None``."""
    return moment.astimezone(UTC).isoformat() if moment is not None else None


def build_scheduler(
    *,
    registry: StoreRegistry,
    perform: PerformRefresh,
    runner: Any = None,
    environ: Any = None,
    warnings: list[str] | None = None,
) -> RefreshScheduler:
    """The scheduler this process's environment configures.

    Both halves come from configuration and neither is guessed: the cadence table from
    ``PROXYSHOP_INGEST_CADENCE`` and the durable state path from
    ``PROXYSHOP_INGEST_SCHEDULER_STATE``. Everything either of them could not read is appended to
    ``warnings`` for the caller to log, exactly as :meth:`StoreRegistry.from_env` does — a
    misconfigured cadence must not take the service down at import time.
    """
    from .state import build_schedule_store  # noqa: PLC0415 - keeps the import graph shallow

    note = warnings if warnings is not None else []
    return RefreshScheduler(
        registry=registry,
        perform=perform,
        cadence=CadenceConfig.from_env(environ, warnings=note),
        store=build_schedule_store(environ, warnings=note),
        runner=runner,
    )
