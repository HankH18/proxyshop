"""Deterministic clocks. Orchestrator-owned (T-000), frozen.

Two shapes, deliberately different:

``ManualClock``
    An *injectable* clock. Nothing global is patched; code under test takes a ``clock``
    argument and calls ``clock.now()`` / ``clock.monotonic()``. Preferred for new code,
    because it composes with concurrency and leaves the real clock alone.
``time_machine`` (wired up by the ``frozen_clock`` fixture in the root ``conftest.py``)
    A *global* freeze, for code that cannot be given a clock — third-party libraries,
    database ``now()`` comparisons in Python, ``datetime.now()`` buried in a helper.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: The instant every ProxyShop test starts from unless it says otherwise. Chosen to be
#: unambiguous in logs and safely inside every certificate/JWT validity window in fixtures.
EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class ManualClock:
    """A clock that only moves when you move it.

    Args:
        start: the instant the clock reports first. Defaults to :data:`EPOCH`. A naive
            datetime is interpreted as UTC.

    Example:
        >>> clock = ManualClock()
        >>> clock.now().isoformat()
        '2026-01-01T00:00:00+00:00'
        >>> clock.advance(90)
        >>> clock.now().isoformat()
        '2026-01-01T00:01:30+00:00'
    """

    def __init__(self, start: datetime | None = None) -> None:
        moment = start or EPOCH
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        self._start = moment
        self._now = moment

    def now(self) -> datetime:
        """The current instant, always timezone-aware UTC."""
        return self._now

    def timestamp(self) -> float:
        """The current instant as a POSIX timestamp."""
        return self._now.timestamp()

    def monotonic(self) -> float:
        """Seconds elapsed since the clock was created. Never goes backwards."""
        return (self._now - self._start).total_seconds()

    def advance(self, seconds: float) -> datetime:
        """Move the clock forward. Returns the new instant.

        Raises:
            ValueError: ``seconds`` is negative — a monotonic clock never rewinds. Use
                :meth:`set` if you genuinely need to jump backwards.
        """
        if seconds < 0:
            raise ValueError("ManualClock.advance() does not accept negative seconds")
        self._now += timedelta(seconds=seconds)
        return self._now

    def set(self, moment: datetime) -> datetime:
        """Jump to an absolute instant (may be earlier than the current one)."""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        self._now = moment
        return self._now

    def __repr__(self) -> str:
        return f"ManualClock({self._now.isoformat()})"
