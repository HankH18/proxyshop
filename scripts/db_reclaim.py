#!/usr/bin/env python
"""Report — and only on demand, reclaim — abandoned ``proxyshop_w*`` databases (P3b).

Every worker owns its own Postgres database (D38, ``proxyshop_support/postgres.py``), and
``ensure_worker_database`` only ever **creates** one. ``grep -rn "DROP DATABASE"`` over this
repo finds nothing outside two test fixtures, so the per-worker databases of every run this
machine has ever hosted are still on the cluster; the only reclamation that exists is
``make deps-down`` (``docker compose down -v``), which destroys both volumes. This script is
the missing half of the lifecycle.

**It is a dry run unless you say otherwise, and that is the whole design.** A lane sitting
between two tests holds no connection, so its database is *droppable* at that instant —
which makes an accidental ``--apply`` against a live swarm precisely the accident worth
engineering against, not a theoretical one. So:

* ``--dry-run`` is the default and needs no flag;
* ``--apply`` must be typed, and is refused outright unless the leased-slot set could be
  read (see :func:`read_leases`) or ``--allow-no-leases`` is *also* typed;
* liveness is re-checked from ``pg_stat_activity`` **immediately before each candidate**,
  never once up front. A snapshot taken at the start is a reads-clean-when-stale shape: the
  cluster being idle when the script began says nothing about the moment of a drop;
* the drop is a plain ``DROP DATABASE``. Never ``WITH (FORCE)``, never
  ``pg_terminate_backend``. Postgres refusing to drop a database that has backends **is**
  the safety mechanism — the same reasoning that keeps a ``--force`` path out of the
  swarm-loop's worktree removal. A refusal is a finding, and is reported as one;
* anything this script cannot confidently classify is protected, and every skip prints the
  reason it was skipped.

What is never a candidate
-------------------------
``proxyshop_w0``
    The frozen ``build_succeeds`` metric runs there (``PROXYSHOP_WORKER=0 make verify``).
    Not overridable by any flag.
``$PROXYSHOP_WORKER``'s own database
    Whoever is running this is presumably about to use it.
``proxyshop_template``, ``postgres``, ``template0``, ``template1``
    Not per-worker databases at all.
``proxyshop_w<800000..899999>``
    The reserved scratch band. ``_throwaway_worker_database`` in
    ``apps/trust/tests/test_ledger_schema_state_hygiene.py`` mints
    ``SCRATCH_WORKER_BASE + worker*1000 + randbelow(1000)`` per test and drops it in a
    ``finally``. A database in that band may be seconds old and about to be used.
``proxyshop_w<N>_migrationcheck_<8 hex>``
    ``ledger_second_database`` in ``apps/trust/tests/_fixtures_ledger_schema.py`` mints one
    per test, also dropped in a ``finally``.
anything else matching ``proxyshop_w*``
    An unrecognised shape is reported and protected. A sweeper that deletes what it cannot
    name is not a sweeper.

The last two are the *race* hazard rather than a stale-data hazard: both fixtures create
and drop within a single test, so a sweep that considered them could win the gap between
another process's ``CREATE`` and its first connection — at which point the fixture's own
``DROP ... IF EXISTS`` succeeds silently and the test fails somewhere else entirely.
``--include-scratch`` exists for a genuinely idle cluster, and is not implied by anything.

Usage::

    scripts/db_reclaim.py                      # dry run: what it WOULD drop, and why not
    scripts/db_reclaim.py --json               # the same, machine-readable
    scripts/db_reclaim.py --protect 3,7,w29    # additionally spare these
    scripts/db_reclaim.py --apply --allow-no-leases     # actually drop (typed, twice)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxyshop_support.postgres import (  # noqa: E402 - after the sys.path bootstrap
    DATABASE_PREFIX,
    MAINTENANCE_DATABASE,
    TEMPLATE_DATABASE,
    maintenance_dsn,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg

#: ``proxyshop_w<N>`` and nothing else. Anchored: ``proxyshop_w3_migrationcheck_ab12cd34``
#: must NOT be read as worker 3.
WORKER_DATABASE_RE = re.compile(rf"^{re.escape(DATABASE_PREFIX)}(?P<index>[0-9]+)$")

#: ``ledger_second_database``'s shape (``_fixtures_ledger_schema.py``): a worker index, the
#: literal marker, and ``uuid4().hex[:8]``.
MIGRATIONCHECK_RE = re.compile(rf"^{re.escape(DATABASE_PREFIX)}[0-9]+_migrationcheck_[0-9a-f]{{8}}$")

#: The reserved scratch band, ``[SCRATCH_WORKER_BASE, SCRATCH_WORKER_BASE + SCRATCH_BAND_SIZE)``.
#: Kept in step with ``apps/trust/tests/test_ledger_schema_state_hygiene.SCRATCH_WORKER_BASE``
#: by ``proxyshop_support/tests/test_db_reclaim.py``, which parses that module rather than
#: importing a test package from a script.
SCRATCH_WORKER_BASE = 800_000
SCRATCH_BAND_SIZE = 100_000

#: Never a candidate, whatever the flags say.
ALWAYS_PROTECTED_INDEXES = frozenset({0})

#: Databases that are not per-worker databases at all.
NON_WORKER_DATABASES = frozenset(
    {MAINTENANCE_DATABASE, TEMPLATE_DATABASE, "template0", "template1"}
)

#: A command whose stdout is JSON describing the currently leased slots. Read, never
#: guessed: the slot pool (P1) is the only authority on which worker indexes are in use.
LEASE_COMMAND_ENV = "PROXYSHOP_SLOT_LIST_CMD"


class ReclaimError(RuntimeError):
    """The sweep could not be performed. Loud on purpose — a silent no-op reads as clean."""


@dataclass
class Candidate:
    """One ``proxyshop_w*`` database and what this run decided about it."""

    name: str
    index: int | None
    size_bytes: int
    #: ``"drop"``, ``"would-drop"`` or ``"protected"``.
    verdict: str = "protected"
    reason: str = ""
    #: Backends seen by the liveness re-check taken immediately before the verdict.
    live_backends: list[dict[str, Any]] = field(default_factory=list)

    def as_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "index": self.index,
            "size_bytes": self.size_bytes,
            "verdict": self.verdict,
            "reason": self.reason,
            "live_backends": self.live_backends,
        }


def in_scratch_band(index: int) -> bool:
    """Is ``index`` inside the reserved throwaway-database band?"""
    return SCRATCH_WORKER_BASE <= index < SCRATCH_WORKER_BASE + SCRATCH_BAND_SIZE


def parse_protect(values: list[str] | None) -> set[int]:
    """Turn ``--protect 3,w7,proxyshop_w29`` into ``{3, 7, 29}``.

    Accepts bare indexes, ``w<N>`` and full database names, because all three are what a
    human actually has in front of them when they decide to spare one.

    Raises:
        ReclaimError: an entry that names nothing. Silently ignoring an unparsable protect
            entry is the one failure mode this argument must not have.
    """
    protected: set[int] = set()
    for raw in values or []:
        for piece in raw.replace(",", " ").split():
            token = piece.strip()
            if not token:
                continue
            if token.startswith(DATABASE_PREFIX):
                token = token[len(DATABASE_PREFIX) :]
            elif token.startswith("w"):
                token = token[1:]
            if not token.isdigit():
                raise ReclaimError(
                    f"--protect {piece!r} is not a worker index. Expected e.g. `3`, `w3` "
                    f"or `{DATABASE_PREFIX}3`."
                )
            protected.add(int(token))
    return protected


def read_leases(command: str | None) -> tuple[set[int] | None, str]:
    """The set of currently leased worker indexes, or ``None`` when it cannot be read.

    Args:
        command: a shell command printing JSON. Either a list of integers, a list of
            objects carrying ``worker``/``index``/``slot``, or an object with a ``slots``
            or ``leases`` key holding one of those.

    Returns:
        ``(indexes, explanation)``. ``indexes`` is ``None`` when there is no lease source
        or it could not be read — which is not an error here, because the slot pool (P1)
        does not exist yet. It IS what makes ``--apply`` refuse without
        ``--allow-no-leases``: dropping on liveness alone means trusting that a lane between
        two tests will not come back to the database it was assigned.
    """
    if not command:
        return None, (
            f"no lease source configured (${LEASE_COMMAND_ENV} unset and no "
            f"--lease-command given), so no worker index is known to be leased"
        )
    try:
        completed = subprocess.run(  # noqa: S602 - operator-supplied command, by design
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"lease command {command!r} could not be run: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "no output"
        return None, f"lease command {command!r} exited {completed.returncode}: {tail}"
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        return None, f"lease command {command!r} did not print JSON: {exc}"

    rows: Any = payload
    if isinstance(payload, dict):
        for key in ("slots", "leases", "claimed"):
            if key in payload:
                rows = payload[key]
                break
    if not isinstance(rows, list):
        return None, f"lease command {command!r} printed JSON that is not a list of slots"

    leased: set[int] = set()
    for row in rows:
        if isinstance(row, int):
            leased.add(row)
        elif isinstance(row, dict):
            for key in ("worker", "index", "slot", "worker_index"):
                value = row.get(key)
                if isinstance(value, int):
                    leased.add(value)
                    break
                if isinstance(value, str) and value.isdigit():
                    leased.add(int(value))
                    break
    return leased, f"{len(leased)} slot(s) leased, read from {command!r}"


def live_backends(connection: psycopg.Connection, name: str) -> list[dict[str, Any]]:
    """Backends connected to ``name`` **right now**.

    Called immediately before each candidate is judged and again inside :func:`drop`, never
    once for the whole run: between the two there is a window, and the window is exactly
    where a lane starting its next test lands.
    """
    with connection.cursor() as cur:
        cur.execute(
            "select pid, usename, application_name, state, backend_start "
            "  from pg_stat_activity "
            " where datname = %s and pid <> pg_backend_pid()",
            (name,),
        )
        return [
            {
                "pid": row[0],
                "user": row[1],
                "application": row[2],
                "state": row[3],
                "since": row[4].isoformat() if row[4] is not None else None,
            }
            for row in cur.fetchall()
        ]


def list_worker_databases(connection: psycopg.Connection) -> list[tuple[str, int]]:
    """Every ``proxyshop_w*`` database with its size, largest first."""
    with connection.cursor() as cur:
        cur.execute(
            "select datname, pg_database_size(datname) "
            "  from pg_database "
            " where datname like %s and not datistemplate "
            " order by pg_database_size(datname) desc, datname",
            (f"{DATABASE_PREFIX}%",),
        )
        return [(row[0], int(row[1])) for row in cur.fetchall()]


def database_count(connection: psycopg.Connection) -> int:
    """How many databases the cluster holds. The before/after proof that a dry run wrote
    nothing."""
    with connection.cursor() as cur:
        cur.execute("select count(*) from pg_database")
        row = cur.fetchone()
        return int(row[0]) if row else 0


def classify(
    name: str,
    size_bytes: int,
    *,
    protected_indexes: set[int],
    leased: set[int] | None,
    include_scratch: bool,
) -> Candidate:
    """Decide everything about ``name`` that does not need a live-backend check.

    Liveness is deliberately NOT consulted here: it is re-read immediately before the
    verdict is acted on, and folding it into a pure function invites exactly the
    snapshot-at-the-start shape this script exists to avoid.
    """
    if name in NON_WORKER_DATABASES:
        return Candidate(name, None, size_bytes, "protected", "not a per-worker database")

    if MIGRATIONCHECK_RE.match(name):
        if not include_scratch:
            return Candidate(
                name,
                None,
                size_bytes,
                "protected",
                "per-test scratch database (ledger_second_database, "
                "apps/trust/tests/_fixtures_ledger_schema.py) — created and dropped inside "
                "one test; pass --include-scratch only on an idle cluster",
            )
        return Candidate(name, None, size_bytes, "candidate", "scratch, --include-scratch given")

    matched = WORKER_DATABASE_RE.match(name)
    if matched is None:
        return Candidate(
            name,
            None,
            size_bytes,
            "protected",
            f"unrecognised name shape for a {DATABASE_PREFIX}* database — protected because "
            f"this script cannot say what it is",
        )

    index = int(matched.group("index"))
    if index in ALWAYS_PROTECTED_INDEXES:
        return Candidate(
            name,
            index,
            size_bytes,
            "protected",
            "worker 0 runs the frozen build_succeeds metric (PROXYSHOP_WORKER=0 make "
            "verify) — never a candidate, under any flag",
        )
    if index in protected_indexes:
        return Candidate(name, index, size_bytes, "protected", "named in the protect list")
    if in_scratch_band(index):
        if not include_scratch:
            return Candidate(
                name,
                index,
                size_bytes,
                "protected",
                f"inside the reserved scratch band "
                f"[{SCRATCH_WORKER_BASE}, {SCRATCH_WORKER_BASE + SCRATCH_BAND_SIZE}) used by "
                f"_throwaway_worker_database (apps/trust/tests/"
                f"test_ledger_schema_state_hygiene.py) — created and dropped inside one test",
            )
        return Candidate(
            name, index, size_bytes, "candidate", "scratch band, --include-scratch given"
        )
    if leased is not None and index in leased:
        return Candidate(name, index, size_bytes, "protected", "slot is currently leased")
    return Candidate(name, index, size_bytes, "candidate", "no lease, no protection")


def drop(connection: psycopg.Connection, name: str) -> tuple[bool, str]:
    """``DROP DATABASE "name"`` — plain, never ``WITH (FORCE)``.

    Returns:
        ``(dropped, detail)``. A refusal because backends are connected is a **finding**,
        not an error to route around: it means something was using a database this run had
        decided was abandoned, and the protection worked.
    """
    import psycopg

    if not (WORKER_DATABASE_RE.match(name) or MIGRATIONCHECK_RE.match(name)):
        # Unreachable via classify(); belt and braces, because the name is interpolated.
        raise ReclaimError(f"refusing to drop {name!r}: it does not match a known shape")
    try:
        with connection.cursor() as cur:
            cur.execute(f'DROP DATABASE "{name}"')
    except psycopg.errors.ObjectInUse as exc:
        return False, f"REFUSED by Postgres — a backend connected in the meantime: {exc}"
    except psycopg.Error as exc:
        return False, f"failed: {exc}"
    return True, "dropped"


def sweep(
    connection: psycopg.Connection,
    *,
    protected_indexes: set[int],
    leased: set[int] | None,
    include_scratch: bool,
    apply: bool,
) -> list[Candidate]:
    """Classify every ``proxyshop_w*`` database, re-checking liveness per candidate."""
    results: list[Candidate] = []
    for name, size_bytes in list_worker_databases(connection):
        candidate = classify(
            name,
            size_bytes,
            protected_indexes=protected_indexes,
            leased=leased,
            include_scratch=include_scratch,
        )
        if candidate.verdict != "candidate":
            results.append(candidate)
            continue

        # Immediately before this candidate is judged — not once at the start of the run.
        candidate.live_backends = live_backends(connection, name)
        if candidate.live_backends:
            pids = ", ".join(str(backend["pid"]) for backend in candidate.live_backends)
            candidate.verdict = "protected"
            candidate.reason = (
                f"in use: {len(candidate.live_backends)} live backend(s) in "
                f"pg_stat_activity (pid {pids})"
            )
            results.append(candidate)
            continue

        if not apply:
            candidate.verdict = "would-drop"
            candidate.reason = "no lease, no live backend at the moment of this check"
            results.append(candidate)
            continue

        # Re-read one last time: classification and the drop are two statements, and the
        # gap between them is where a lane's next test connects.
        candidate.live_backends = live_backends(connection, name)
        if candidate.live_backends:
            pids = ", ".join(str(backend["pid"]) for backend in candidate.live_backends)
            candidate.verdict = "protected"
            candidate.reason = f"a backend appeared between the check and the drop (pid {pids})"
            results.append(candidate)
            continue

        dropped, detail = drop(connection, name)
        candidate.verdict = "dropped" if dropped else "protected"
        candidate.reason = detail
        results.append(candidate)
    return results


def _human(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - unreachable, the loop returns


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="db_reclaim.py",
        description=(
            "Report abandoned proxyshop_w* databases. Dry run unless --apply is given."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Safety: --dry-run is the default; --apply additionally requires a readable "
            "lease source or --allow-no-leases; liveness is re-read immediately before "
            "each candidate and again before each drop; the drop is a plain DROP DATABASE "
            "with no FORCE, so Postgres refusing it is the last line of defence."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="report only, writing nothing. This is the default; the flag exists so a "
        "caller can say so explicitly.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="actually DROP the candidates. Requires a lease source or --allow-no-leases.",
    )
    parser.add_argument(
        "--allow-no-leases",
        action="store_true",
        help="permit --apply with no readable lease source. Read the refusal it lifts "
        "before using it: without leases, 'idle' is the only evidence of 'abandoned'.",
    )
    parser.add_argument(
        "--protect",
        action="append",
        metavar="IDX[,IDX...]",
        help="worker indexes to spare, as `3`, `w3` or `proxyshop_w3`. Repeatable.",
    )
    parser.add_argument(
        "--include-scratch",
        action="store_true",
        help="also consider per-test scratch databases (the reserved "
        f"[{SCRATCH_WORKER_BASE}, {SCRATCH_WORKER_BASE + SCRATCH_BAND_SIZE}) band and "
        "*_migrationcheck_* names). Two fixtures create and drop these inside a single "
        "test; only use this on a cluster you know is idle.",
    )
    parser.add_argument(
        "--lease-command",
        default=os.environ.get(LEASE_COMMAND_ENV),
        help=f"shell command printing the leased slots as JSON (default: ${LEASE_COMMAND_ENV}).",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    import psycopg

    args = build_parser().parse_args(argv)

    try:
        protected_indexes = parse_protect(args.protect)
    except ReclaimError as exc:
        print(f"db_reclaim: {exc}", file=sys.stderr)
        return 2

    self_worker = os.environ.get("PROXYSHOP_WORKER")
    if self_worker and self_worker.strip().isdigit():
        protected_indexes.add(int(self_worker.strip()))

    leased, lease_note = read_leases(args.lease_command)

    if args.apply and leased is None and not args.allow_no_leases:
        print(
            "db_reclaim: REFUSING --apply.\n"
            f"  {lease_note}.\n"
            "  Without a lease set, the only evidence that a database is abandoned is that "
            "nothing is connected to it right now — and a worker sitting between two tests "
            "holds no connection, so its database looks exactly like an abandoned one.\n"
            "  Either point --lease-command (or $" + LEASE_COMMAND_ENV + ") at the slot "
            "pool, or, if you have decided the cluster is idle, pass --allow-no-leases as "
            "well.",
            file=sys.stderr,
        )
        return 3

    observed_at = datetime.now(UTC).isoformat()
    try:
        connection = psycopg.connect(maintenance_dsn(), autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        print(f"db_reclaim: cannot reach Postgres: {exc}", file=sys.stderr)
        return 4

    with connection:
        before = database_count(connection)
        results = sweep(
            connection,
            protected_indexes=protected_indexes,
            leased=leased,
            include_scratch=args.include_scratch,
            apply=args.apply,
        )
        after = database_count(connection)

    reclaimable = [c for c in results if c.verdict in {"would-drop", "dropped"}]
    payload = {
        "observed_at": observed_at,
        "mode": "apply" if args.apply else "dry-run",
        "leases": lease_note,
        "protected_indexes": sorted(protected_indexes),
        "include_scratch": args.include_scratch,
        "worker_databases": len(results),
        "reclaimable": len(reclaimable),
        "reclaimable_bytes": sum(c.size_bytes for c in reclaimable),
        "pg_database_count_before": before,
        "pg_database_count_after": after,
        "databases": [c.as_record() for c in results],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_report(payload, results, reclaimable)

    return 0


def _print_report(
    payload: dict[str, Any], results: list[Candidate], reclaimable: list[Candidate]
) -> None:
    mode = payload["mode"]
    print(f"db_reclaim — {mode}, observed at {payload['observed_at']}")
    print(f"  leases: {payload['leases']}")
    print(f"  protected indexes: {payload['protected_indexes'] or 'none beyond worker 0'}")
    print(
        f"  {payload['worker_databases']} {DATABASE_PREFIX}* database(s) on the cluster; "
        f"pg_database holds {payload['pg_database_count_before']} in total"
    )
    print()

    verb = "DROPPED" if mode == "apply" else "WOULD DROP"
    if reclaimable:
        print(f"{verb} ({len(reclaimable)}, {_human(payload['reclaimable_bytes'])}):")
        for candidate in reclaimable:
            print(f"  {candidate.name:<34} {_human(candidate.size_bytes):>9}  {candidate.reason}")
    else:
        print(f"{verb}: nothing.")
    print()

    protected = [c for c in results if c.verdict == "protected"]
    print(f"PROTECTED ({len(protected)}), every one with its reason:")
    for candidate in protected:
        print(f"  {candidate.name:<34} {_human(candidate.size_bytes):>9}  {candidate.reason}")
    print()

    before, after = payload["pg_database_count_before"], payload["pg_database_count_after"]
    if mode == "dry-run":
        print(
            f"Wrote nothing: pg_database count {before} before, {after} after "
            f"({'unchanged' if before == after else 'CHANGED — investigate'})."
        )
        if reclaimable:
            command = f"{shlex.quote(sys.argv[0])} --apply"
            print(f"To act on this, run it deliberately:  {command} --allow-no-leases")
    else:
        print(f"pg_database count {before} before, {after} after.")


if __name__ == "__main__":
    raise SystemExit(main())
