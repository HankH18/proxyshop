"""Durable scheduler state — the half of T-024 that has to survive the process (D41).

"Durable" is the load-bearing word in the ticket and it is not decoration. A refresh clock held
only in a Python dict resets the moment the container restarts, and the observable consequence is
specific and bad in both directions:

* **Every store becomes due at once.** A field with no remembered ``last_refreshed`` is overdue by
  definition, so the first tick after a redeploy re-crawls every configured store simultaneously,
  at whatever moment the deploy happened, forever. A weekly policy crawl becomes a per-deploy
  policy crawl, and the merchant sees the traffic.
* **The differential guarantee stops being a guarantee.** ``CatalogRefreshRunner.hashes`` is what
  makes "unchanged content re-extracts nothing" true; it lives in the runner and dies with the
  process, so the first refresh after every restart re-extracts a catalog nobody touched. That is
  the exact property the acceptance criterion measures "across a full cycle", and a cycle that
  straddles a redeploy would have failed it.

So both things — the per-field refresh clock and the per-store hash ledger — are written here, and
:class:`~ingest.scheduler.cycle.RefreshScheduler` reloads them on construction.

The substrate is a JSON file, chosen over the two alternatives on purpose:

* **Neo4j** is this service's only datastore (``services/ingest/compose.yaml`` hands it no Postgres
  DSN, and ``git grep -l psycopg -- services/ingest/src`` is empty). Putting the refresh clock
  there makes the scheduler unable to start when the graph is down — precisely when you most want
  it to know it has not crawled in six hours — and makes every test of it need a live database,
  which under C9/D19 means a *skipped* test, which is indistinguishable from a passing one.
* **A new datastore** for one small document per store is a deployment cost with no payer.

A file is durable in exactly the sense the ticket asks for: it survives the process. It is durable
across a *redeploy* only if it is written somewhere that outlives the container, which is a
deployment fact, so the path is configuration and nothing here guesses one. When no path is
configured the store is explicitly and loudly non-durable (:class:`MemoryScheduleStore`) rather
than quietly writing to a path inside an ephemeral filesystem and calling that persistence.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "STATE_ENV",
    "STATE_VERSION",
    "JsonFileScheduleStore",
    "MemoryScheduleStore",
    "ScheduleState",
    "ScheduleStore",
    "StoreSchedule",
    "build_schedule_store",
    "parse_instant",
]

_log = logging.getLogger(__name__)

#: Environment variable naming the JSON file the scheduler persists into. Unset means the
#: scheduler runs with in-memory state and says so at startup.
STATE_ENV = "PROXYSHOP_INGEST_SCHEDULER_STATE"

#: Written into every document. A future reader that finds a version it does not understand
#: starts empty rather than misreading a shape it was not written for; re-crawling a store is
#: recoverable, silently mis-scheduling every store is not.
STATE_VERSION = 1


def parse_instant(value: object) -> datetime | None:
    """Read one ISO-8601 instant out of persisted state, or ``None`` if it is unreadable.

    Never raises. A single corrupt timestamp makes one field due early; a scheduler that refused
    to start over one makes every store stale.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@dataclass
class StoreSchedule:
    """What one store has already had done to it.

    Attributes:
        last_refreshed: field name -> the instant that field was last made fresh. A field absent
            from this mapping has never been refreshed and is therefore due.
        hashes: the store's differential ledger, ``CatalogRefreshRunner.hashes`` for this store —
            product ref -> content hash. Persisted so an unchanged catalog is still unchanged
            after a restart.
    """

    last_refreshed: dict[str, datetime] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """The JSON-safe form written to disk."""
        return {
            "last_refreshed": {
                name: moment.astimezone(UTC).isoformat()
                for name, moment in sorted(self.last_refreshed.items())
            },
            "hashes": dict(sorted(self.hashes.items())),
        }

    @classmethod
    def from_payload(cls, payload: object) -> StoreSchedule:
        """Read one store's entry, dropping anything unreadable rather than raising."""
        if not isinstance(payload, dict):
            return cls()
        raw_times = payload.get("last_refreshed")
        times: dict[str, datetime] = {}
        if isinstance(raw_times, dict):
            for name, value in raw_times.items():
                moment = parse_instant(value)
                if moment is not None and isinstance(name, str):
                    times[name] = moment
        raw_hashes = payload.get("hashes")
        hashes = (
            {str(k): str(v) for k, v in raw_hashes.items()} if isinstance(raw_hashes, dict) else {}
        )
        return cls(last_refreshed=times, hashes=hashes)


@dataclass
class ScheduleState:
    """Every store's schedule, keyed by ``store_id``."""

    stores: dict[str, StoreSchedule] = field(default_factory=dict)

    def for_store(self, store_id: str) -> StoreSchedule:
        """The entry for ``store_id``, created empty on first use."""
        return self.stores.setdefault(str(store_id), StoreSchedule())

    def to_payload(self) -> dict[str, Any]:
        """The whole document, JSON-safe and key-sorted so two identical states compare equal."""
        return {
            "version": STATE_VERSION,
            "stores": {
                store_id: schedule.to_payload()
                for store_id, schedule in sorted(self.stores.items())
            },
        }

    @classmethod
    def from_payload(cls, payload: object) -> ScheduleState:
        """Read a whole document, answering an empty state for anything unrecognisable."""
        if not isinstance(payload, dict):
            return cls()
        if payload.get("version") != STATE_VERSION:
            _log.warning(
                "scheduler state version %r is not %r; starting from an empty schedule",
                payload.get("version"),
                STATE_VERSION,
            )
            return cls()
        raw = payload.get("stores")
        if not isinstance(raw, dict):
            return cls()
        return cls(
            stores={
                str(store_id): StoreSchedule.from_payload(entry) for store_id, entry in raw.items()
            }
        )


class ScheduleStore(Protocol):
    """Where a :class:`ScheduleState` is kept between ticks."""

    #: Whether this store survives the process. Read by the scheduler so a non-durable
    #: deployment is reported rather than assumed.
    durable: bool

    def load(self) -> ScheduleState:
        """The persisted state, or an empty one when there is nothing to read."""

    def save(self, state: ScheduleState) -> None:
        """Persist ``state``, replacing whatever was there."""


class MemoryScheduleStore:
    """A schedule store that lives and dies with the process.

    Not a stub: it is the honest answer when no state path is configured, and it says so —
    :attr:`durable` is ``False`` and the scheduler logs that its refresh clock resets on restart.
    A default file path would be worse, because a path inside a container's writable layer *looks*
    persistent right up until the redeploy that proves it is not.
    """

    durable = False

    def __init__(self, state: ScheduleState | None = None) -> None:
        self._state = state if state is not None else ScheduleState()

    def load(self) -> ScheduleState:
        return self._state

    def save(self, state: ScheduleState) -> None:
        self._state = state

    def __repr__(self) -> str:
        return "MemoryScheduleStore(durable=False)"


class JsonFileScheduleStore:
    """A schedule store backed by one JSON file, replaced atomically.

    The write is ``write temp -> fsync -> os.replace``, so a process killed mid-save leaves either
    the previous document or the new one and never a truncated file. A truncated file would be read
    back as "no store has ever been refreshed", which is the failure this class exists to prevent —
    it would re-crawl every configured merchant on the next tick.

    Args:
        path: the file to keep state in. Parent directories are created on first save.
    """

    durable = True

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        #: Two ticks are never meant to overlap, but the tick route is `def` and Starlette runs
        #: those in a worker thread, so "never meant to" is not a guarantee. The lock makes a
        #: concurrent save a queue rather than two writers racing for one temp name.
        self._lock = threading.Lock()

    def load(self) -> ScheduleState:
        """Read the document, answering an empty state when there is nothing usable to read.

        A file that exists but cannot be parsed is *moved aside* to ``<name>.corrupt`` rather than
        silently overwritten by the next save: the reason the scheduler forgot everything is worth
        keeping, and it is the only evidence that would exist.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ScheduleState()
        except OSError as exc:
            _log.warning(
                "scheduler state %s could not be read (%s); starting empty", self.path, exc
            )
            return ScheduleState()
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            _log.warning(
                "scheduler state %s is not valid JSON (%s); moved aside and starting empty",
                self.path,
                exc,
            )
            self._quarantine()
            return ScheduleState()
        return ScheduleState.from_payload(payload)

    def save(self, state: ScheduleState) -> None:
        """Replace the document with ``state``, atomically."""
        payload = json.dumps(state.to_payload(), indent=2, sort_keys=True) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)

    def _quarantine(self) -> None:
        try:
            self.path.replace(self.path.with_name(f"{self.path.name}.corrupt"))
        except OSError as exc:  # pragma: no cover - the read succeeded, so this is near-impossible
            _log.warning("could not move aside corrupt scheduler state %s: %s", self.path, exc)

    def __repr__(self) -> str:
        return f"JsonFileScheduleStore({str(self.path)!r})"


def build_schedule_store(
    environ: Mapping[str, str] | None = None, *, warnings: list[str] | None = None
) -> ScheduleStore:
    """The schedule store :data:`STATE_ENV` asks for, or a named non-durable one.

    Returns:
        A :class:`JsonFileScheduleStore` when ``PROXYSHOP_INGEST_SCHEDULER_STATE`` names a path,
        otherwise a :class:`MemoryScheduleStore` with a warning saying what that costs.
    """
    env = os.environ if environ is None else environ
    note = warnings if warnings is not None else []
    raw = str(env.get(STATE_ENV, "") or "").strip()
    if not raw:
        note.append(
            f"{STATE_ENV} is unset: the refresh schedule is held in memory and resets on "
            f"restart, so the first tick after a redeploy re-crawls every configured store"
        )
        return MemoryScheduleStore()
    return JsonFileScheduleStore(raw)
