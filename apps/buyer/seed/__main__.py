"""``python -m apps.buyer.seed`` — produce, verify, load and audit the store-window corpus.

Five subcommands, and the **exit status is the finding**, not decoration:

* ``run``     — draw the population, verify its chain, write the corpus. ``0`` when the bytes
                on disk load back clean; ``1`` when they do not.
* ``verify``  — load the committed corpus, recompute the chain from the rows alone and
                compare it to the recorded head. ``0`` when it holds; ``1`` when it does not.
                Opens no socket and needs no database.
* ``load``    — upsert the corpus into ``app.buyer_accounts``. ``0`` when every row is present
                and marked ``'seed'`` afterwards, read back from the database; ``1`` otherwise.
* ``census``  — what the corpus holds, by cohort, and where it says it came from. Always ``0``
                if the corpus loads.
* ``audit``   — compare the TABLE's seeded rows against the committed corpus. ``0`` when every
                row marked ``'seed'`` is one this repository committed to and carries the
                buckets it committed; ``1`` when any is unpinned, altered or missing. This is
                the one check the database's constraint cannot make — see :func:`_cmd_audit`.

``2`` from any subcommand means it could not start: no corpus, no driver, an unusable DSN.

Two guards on ``load``, and both are about the same hazard — this command writes manufactured
buyers into a real table:

* the DSN must **name a host, and every host it names must be loopback**, unless
  ``--allow-remote`` is passed **on the command line**. There is no environment variable and no
  config file that can move that default; the same rule, for the same reason, as
  ``services/sim/seed/targets.py``. Naming none is refused too — libpq would then read
  ``$PGHOST``, and "wherever the environment points" is exactly the target this guard exists to
  refuse. See :func:`hosts_in` for the three DSN grammars it has to read to say that.
* it writes only rows whose pseudonym carries :data:`~apps.buyer.seed.chain.SEED_PSEUDONYM_PREFIX`,
  checked in this process before the statement is sent — so a corpus that somehow held an
  unmarked row fails the run rather than quietly putting an unlabelled synthetic buyer into a
  window a store reads. The database's CHECK would refuse it too; this refusal is the one that
  names the corpus.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .chain import SEED_PSEUDONYM_PREFIX, ChainBroken, is_seeded_pseudonym, verify_chain
from .population import DEFAULT_MEMBERS_PER_COHORT, DEFAULT_SEED, CohortCollapsed, census, draw
from .store import COLLECTION_FILE, SeedArtifactError, default_root, load, write

__all__ = ["hosts_in", "main"]

#: Hostnames a corpus may be loaded into without saying so explicitly. An absent host is NOT
#: among them: "this DSN names no host" and "this DSN names localhost" are different facts, and
#: only the second one is permission. ``services/sim/seed/targets.py`` draws the same line for
#: the same reason.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Statement the loader sends. Names ``provenance`` explicitly rather than leaning on the
#: column default: the default is ``'live'``, so a loader that omitted the column would write
#: forty unmarked synthetic buyers — except that the CHECK constraint refuses them, which is
#: precisely the belt this brace exists to make unnecessary.
UPSERT = (
    "insert into app.buyer_accounts (pseudonym, buckets, provenance) "
    "values (%s, %s::jsonb, 'seed') "
    "on conflict (pseudonym) do update set buckets = excluded.buckets, provenance = 'seed'"
)


def _producer(seed: int, members: int) -> str:
    """The exact command that reproduces the corpus, with every value resolved.

    Spelled out rather than echoing what was typed: "the default" stops being a reproducible
    instruction the moment the default moves, and the whole point of the field is that someone
    holding only the corpus can run the line and compare digests.
    """
    return f"python -m apps.buyer.seed run --seed {seed} --members {members}"


def _root_of(args: argparse.Namespace) -> Path:
    return Path(args.source) if getattr(args, "source", None) else default_root()


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        rows, head = draw(seed=args.seed, members=args.members)
    except (CohortCollapsed, ValueError) as exc:
        print(f"the population could not be drawn: {exc}", file=sys.stderr)
        return 2

    destination = write(
        rows,
        chain_head=head,
        producer=_producer(args.seed, args.members),
        seed=args.seed,
        members=args.members,
        root=_root_of(args) if args.source else None,
    )

    # Read it back through the same door every other caller uses. A producer that trusted its
    # own in-memory rows would report success over a corpus that its next `load` refuses.
    try:
        corpus = load(destination)
    except (SeedArtifactError, ChainBroken) as exc:
        print(f"the corpus was written and does not load back: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {len(corpus.rows)} seeded row(s) to {destination}")
    print(f"  marker      {SEED_PSEUDONYM_PREFIX}<24 hex of this row's chain digest>")
    print(f"  chain head  {corpus.chain_head}")
    print(f"  accounts    sha256:{corpus.collection['file_sha256']['accounts.jsonl']}")
    print(f"  reproduce   {corpus.collection['determinism']['reproduce']}")
    print("  next        python -m apps.buyer.seed load --dsn $PROXYSHOP_PG_DSN_APP")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        corpus = load(_root_of(args))
    except SeedArtifactError as exc:
        print(f"the corpus could not be read: {exc}", file=sys.stderr)
        return 2
    except ChainBroken as exc:
        print(f"CHAIN BROKEN: {exc}", file=sys.stderr)
        return 1

    # `load` already verified it. Recomputed here from the rows alone, with the head taken
    # from the provenance rather than from the loader, so this subcommand is a check somebody
    # can read rather than a report of a check that happened somewhere else.
    try:
        recomputed = verify_chain(corpus.rows, expected_head=corpus.chain_head)
    except ChainBroken as exc:
        print(f"CHAIN BROKEN: {exc}", file=sys.stderr)
        return 1

    unmarked = [
        row["pseudonym"] for row in corpus.rows if not is_seeded_pseudonym(row["pseudonym"])
    ]
    if unmarked:
        print(f"{len(unmarked)} row(s) carry no seed marker: {unmarked[:5]}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"rows": len(corpus.rows), "chain_head": recomputed}, sort_keys=True))
    else:
        print(f"{len(corpus.rows)} row(s) verify against chain head {recomputed}")
    return 0


def _cmd_census(args: argparse.Namespace) -> int:
    try:
        corpus = load(_root_of(args))
    except (SeedArtifactError, ChainBroken) as exc:
        print(f"the corpus could not be read: {exc}", file=sys.stderr)
        return 2
    counted = census(corpus.rows)
    if args.json:
        print(json.dumps({"rows_by_cohort": counted}, sort_keys=True))
        return 0
    print(f"{corpus.root / COLLECTION_FILE}")
    print(f"  produced by  {corpus.collection['producer']}")
    print(f"  at revision  {corpus.collection['source'].get('commit') or '(unknown)'}")
    print(f"  chain head   {corpus.chain_head}")
    for name, count in counted.items():
        print(f"  {count:>3}  {name}")
    return 0


def hosts_in(dsn: str) -> set[str]:
    """Every host ``dsn`` could connect to. Empty when it names none.

    libpq accepts a connection string in **three** shapes and psycopg takes all of them, so a
    guard that reads only one is not a guard. MEASURED against the version this repo pins, each
    of these reaches ``prod.example.com`` and only the first has a URL ``hostname``::

        postgresql://app@prod.example.com:5432/proxyshop      -> urlsplit().hostname
        postgresql:///proxyshop?host=prod.example.com         -> the URL's query
        host=prod.example.com port=5432 dbname=proxyshop      -> keyword/value form

    The second and third both returned ``None`` from ``urlsplit(...).hostname``, which the
    earlier version of this check read as "loopback" and admitted. Every shape is parsed here
    and the UNION is what gets judged, so a DSN naming several hosts (libpq accepts a
    comma-separated list for failover) is refused unless *all* of them are loopback.
    """
    found: set[str] = set()
    split = urlsplit(dsn)
    if split.hostname:
        found.update(part.strip() for part in split.hostname.split(",") if part.strip())
    # `host=` in a URL query, and `host=` in keyword/value form, are the same token in two
    # grammars; both are whitespace- or ampersand-separated `key=value` pairs, so one scan
    # finds them wherever they sit.
    for token in re.split(r"[\s&?]+", dsn):
        key, sep, value = token.partition("=")
        if sep and key.strip().lower() in {"host", "hostaddr"}:
            found.update(part.strip() for part in value.split(",") if part.strip())
    return found


def _refuse_remote(dsn: str, *, allow_remote: bool) -> str | None:
    """The reason this DSN may not be written to, or ``None``.

    Refuses a DSN that names NO host as firmly as one that names a remote. An absent host means
    libpq falls back to ``PGHOST``, then to a local socket — so "no host" is not "localhost",
    it is "whatever the environment says", and this command exists to stop manufactured buyers
    reaching a database nobody chose. There is no environment variable that can grant the
    exception, which is the whole point.
    """
    if allow_remote:
        return None
    hosts = hosts_in(dsn)
    if hosts and hosts <= LOOPBACK_HOSTS:
        return None
    named = f"{sorted(hosts)}" if hosts else "no host at all (libpq would read $PGHOST)"
    return (
        f"refusing to write manufactured buyers into a database this DSN names {named}: not "
        "every target is a loopback address. Pass --allow-remote on the command line if that "
        "is genuinely what you mean; there is no environment variable that can grant it."
    )


def _cmd_load(args: argparse.Namespace) -> int:
    try:
        corpus = load(_root_of(args))
    except (SeedArtifactError, ChainBroken) as exc:
        print(f"the corpus could not be read: {exc}", file=sys.stderr)
        return 2

    unmarked = [
        row["pseudonym"] for row in corpus.rows if not is_seeded_pseudonym(row["pseudonym"])
    ]
    if unmarked:
        print(
            f"refusing to load: {len(unmarked)} row(s) carry no {SEED_PSEUDONYM_PREFIX!r} "
            f"marker ({unmarked[:5]}). An unlabelled synthetic buyer in a window a store "
            "reads is the one outcome this package exists to prevent.",
            file=sys.stderr,
        )
        return 1

    refusal = _refuse_remote(args.dsn, allow_remote=args.allow_remote)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg is a root dependency
        print("psycopg is not importable, so no corpus can be loaded", file=sys.stderr)
        return 2

    try:
        connection = psycopg.connect(args.dsn)
    except Exception as exc:  # noqa: BLE001 - every connect failure is "could not start"
        print(f"could not connect: {exc}", file=sys.stderr)
        return 2

    try:
        with connection, connection.cursor() as cur:
            for row in corpus.rows:
                cur.execute(
                    UPSERT,
                    (row["pseudonym"], json.dumps(row["buckets"], sort_keys=True)),
                )
            # Read back from the database rather than counting the statements sent. A loader
            # that reported its own INSERT count would report success against a transaction
            # that rolled back, and against a table whose CHECK silently relabelled nothing.
            cur.execute(
                "select count(*) from app.buyer_accounts "
                "where provenance = 'seed' and pseudonym = any(%s)",
                ([row["pseudonym"] for row in corpus.rows],),
            )
            present = int((cur.fetchone() or (0,))[0])
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        print(f"the load failed: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    if present != len(corpus.rows):
        print(
            f"only {present} of {len(corpus.rows)} seeded row(s) are present and marked "
            "'seed' after the load",
            file=sys.stderr,
        )
        return 1
    print(f"{present} seeded row(s) present in app.buyer_accounts, marked 'seed'")
    print(f"  chain head  {corpus.chain_head}")
    print("  read them   GET /buyer/store-window")
    print("  check them  python -m apps.buyer.seed audit --dsn <the same DSN>")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    """Compare the table's seeded rows against the committed corpus.

    **This is the check that the rest of the marker cannot make, and the reason it exists is
    worth stating plainly rather than leaving implied.**

    The database guarantees CONSISTENCY: ``buyer_accounts_provenance_is_the_key`` makes it
    impossible for a row's ``provenance`` and its pseudonym to disagree, so no caller of any
    served route — a real buyer logging in included — can produce a row that reads as seeded.
    What the database does NOT guarantee is MEMBERSHIP. Anything holding the ``app`` role can
    insert ``psn-seed-<any 24 characters>`` with ``provenance = 'seed'``, and that row is
    perfectly legal, is served by the window as seeded, and matches no link in any chain. The
    table stores no signature, so nothing at read time can tell it from a real corpus row.

    The corpus is what closes that, and only if somebody compares. So: this. It reports, and
    exits non-zero for, three findings the constraint cannot see —

    * **unpinned** — a row marked ``'seed'`` whose pseudonym is in no committed corpus. Either
      somebody wrote synthetic buyers by hand, or a corpus was loaded and then changed here.
    * **altered** — a pinned pseudonym whose buckets in the table are not the buckets the
      corpus (and therefore the chain digest that names it) commits to.
    * **missing** — a corpus row that is not in the table, so the window is serving less than
      the corpus claims.

    A clean audit is the statement "every manufactured buyer in this database is one this
    repository committed to, and no real buyer is labelled as one". Nothing weaker is available
    without giving the table a signature, and nothing stronger is needed for a demo.
    """
    try:
        corpus = load(_root_of(args))
    except (SeedArtifactError, ChainBroken) as exc:
        print(f"the corpus could not be read: {exc}", file=sys.stderr)
        return 2

    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg is a root dependency
        print("psycopg is not importable, so no table can be audited", file=sys.stderr)
        return 2
    try:
        connection = psycopg.connect(args.dsn)
    except Exception as exc:  # noqa: BLE001 - every connect failure is "could not start"
        print(f"could not connect: {exc}", file=sys.stderr)
        return 2

    try:
        with connection, connection.cursor() as cur:
            cur.execute(
                "select pseudonym, buckets from app.buyer_accounts where provenance = 'seed'"
            )
            in_table = {
                pseudonym: (buckets if isinstance(buckets, dict) else json.loads(buckets or "{}"))
                for pseudonym, buckets in cur.fetchall()
            }
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        print(f"the audit could not read the table: {exc}", file=sys.stderr)
        return 2
    finally:
        connection.close()

    pinned = {row["pseudonym"]: row["buckets"] for row in corpus.rows}
    unpinned = sorted(set(in_table) - set(pinned))
    missing = sorted(set(pinned) - set(in_table))
    altered = sorted(key for key in set(pinned) & set(in_table) if pinned[key] != in_table[key])

    if args.json:
        print(
            json.dumps(
                {
                    "altered": altered,
                    "chain_head": corpus.chain_head,
                    "missing": missing,
                    "seeded_in_table": len(in_table),
                    "pinned_in_corpus": len(pinned),
                    "unpinned": unpinned,
                },
                sort_keys=True,
            )
        )
    else:
        print(f"{len(in_table)} row(s) marked 'seed' in app.buyer_accounts")
        print(f"  pinned by the committed corpus  {len(pinned)} (head {corpus.chain_head})")
        for label, offenders, why in (
            ("unpinned", unpinned, "marked 'seed' and in no committed corpus"),
            ("altered", altered, "pinned, but the table's buckets are not the corpus's"),
            ("missing", missing, "in the corpus and not in the table"),
        ):
            if offenders:
                print(f"  {label:<8} {len(offenders):>4}  {why}: {offenders[:5]}")
    if unpinned or altered or missing:
        return 1
    print("  every manufactured buyer in this database is one this repository committed to")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.buyer.seed", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="draw the population and write the corpus")
    run.add_argument("--seed", type=int, default=DEFAULT_SEED)
    run.add_argument("--members", type=int, default=DEFAULT_MEMBERS_PER_COHORT)
    run.add_argument("--out", dest="source", default=None, help="write here instead")
    run.set_defaults(handler=_cmd_run)

    verify = sub.add_parser("verify", help="recompute the chain over the committed corpus")
    verify.add_argument("--from", dest="source", default=None)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(handler=_cmd_verify)

    show = sub.add_parser("census", help="what the corpus holds, by cohort")
    show.add_argument("--from", dest="source", default=None)
    show.add_argument("--json", action="store_true")
    show.set_defaults(handler=_cmd_census)

    apply_ = sub.add_parser("load", help="upsert the corpus into app.buyer_accounts")
    apply_.add_argument("--dsn", required=True, help="an app-role DSN; it must be able to write")
    apply_.add_argument("--from", dest="source", default=None)
    apply_.add_argument(
        "--allow-remote",
        action="store_true",
        help="permit a non-loopback host. Command line only, by design.",
    )
    apply_.set_defaults(handler=_cmd_load)

    # No --allow-remote. This one only reads, so pointing it at a remote database risks
    # nothing, and a flag that has to be typed before an audit is a flag that makes the audit
    # skipped.
    audit = sub.add_parser("audit", help="is every seeded row in the table one we committed?")
    audit.add_argument("--dsn", required=True, help="any DSN that can SELECT app.buyer_accounts")
    audit.add_argument("--from", dest="source", default=None)
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(handler=_cmd_audit)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    handler: Any = args.handler
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main())
