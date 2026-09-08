"""Container startup and readiness for the deployed services (the deploy lane).

Two subcommands, one module, because both have to be present in every service image and
every service image already copies ``proxyshop_support/`` wholesale::

    python -m proxyshop_support.service_launch serve -- uvicorn trust.main:app ...
    python -m proxyshop_support.service_launch ready --http-port 8084 \
        --postgres trust_rw:ledger.commerce_events

Why this file exists
====================

**``serve`` — the credential the container could not resolve.** Every service fragment used
to interpolate the role password *into* its DSN as ``${PROXYSHOP_ROLE_PASSWORD:-}``, with a
deliberately empty default so no compose file would carry a second copy of the dev literal.
The intent was right and the result was a broken deployment: an unset variable produced
``postgresql://trust_rw:@postgres:5432/proxyshop_w1``, and an *explicitly empty* password is
not the same thing as *no* password. libpq rejects it before it reaches the server. Measured,
in this repo, from ``docker compose logs buyer-svc``::

    psycopg.OperationalError: connection failed: connection to server at "172.27.0.3",
    port 5432 failed: fe_sendauth: no password supplied

The connect side already had exactly one place that knows how to resolve that credential --
:func:`proxyshop_support.postgres.role_dsn`, which walks ``$PROXYSHOP_ROLE_PASSWORD`` ->
the password inside ``$PROXYSHOP_PG_DSN_*`` -> :data:`~proxyshop_support.postgres.
DEV_ROLE_PASSWORD`, and which the T-112 tests pin against ``db/init/00-roles.sql``'s
``coalesce()``. The deployed containers were the one caller that did not go through it:
``git grep role_dsn`` outside the tests returns ``conftest.py``, ``scripts/db_init.py`` and
nothing else. Services read ``$PROXYSHOP_PG_DSN_*`` raw.

``serve`` closes that gap **without adding a literal anywhere**: at container start it
rewrites each ``PROXYSHOP_PG_DSN_*`` that is set through ``role_dsn``, then ``execvp``s the
real command, so the service inherits the same DSN the test suite would have built. Compose
keeps a password-free template; the resolver stays the single source of truth; and setting
``PROXYSHOP_ROLE_PASSWORD`` still overrides everything, exactly as documented.

**``ready`` — a health signal that means something.** Every service healthcheck used to
probe ``/openapi.json``, so "healthy" meant "the ASGI app imported and uvicorn is answering".
That is a liveness probe wearing a readiness probe's name, and the failure it produced is the
one this project keeps paying for: ``docker compose ps`` reporting eight rows of ``(healthy)``
over a stack whose database-backed routes all answered 503. ``docs/deploy.md`` named the gap
and left it owed.

``ready`` keeps the ``/openapi.json`` check -- it is still the cheapest proof that the app
imported and every discovered router mounted -- and adds the part that was missing: the
service's **own** datastore dependencies, reached with the service's **own** credentials, and
where a schema is named, the relation the service actually reads. A container reports healthy
only when it could serve.

Two rules kept this from becoming decoration:

* **Probe only what the service uses.** The dependency set per service is measured
  (``git grep -l psycopg`` / ``worker_redis`` / ``neo4j`` under each service's source), not
  copied between fragments. ``merchant-svc`` touches no datastore, so its probe checks no
  datastore -- a postgres check there would be a green light with nothing behind it, which is
  the defect, not the fix.
* **Name the relation, not just the server.** ``make deps-up`` creates ``proxyshop_w<N>``
  and applies no migrations, so a probe that stopped at "postgres answers" would go green
  against a database holding nothing but ``public``. That is measured too: on a stack brought
  up exactly as the runbook says, ``\\dn`` in ``proxyshop_w1`` listed one schema, and the
  trust ledger answered ``503 store_unavailable``. ``--postgres trust_rw:ledger.commerce_events``
  is what makes the probe fail in that state instead of passing.

Every check is bounded. A healthcheck that hangs is worse than one that fails, because compose
reports the container as still ``starting`` and the operator waits.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from proxyshop_support.postgres import ROLES, role_dsn

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

#: Per-check network timeout, seconds. The compose fragments give the healthcheck
#: ``timeout: 5s``; every check has to fit inside that *together*, so each one is given a
#: budget well under it rather than the default (``None`` -- i.e. block forever).
CHECK_TIMEOUT = 3.0

#: What ``serve`` rewrites: the ``PROXYSHOP_PG_DSN_*`` variable of every role in
#: :data:`~proxyshop_support.postgres.ROLES`, and only when compose actually set it. A
#: variable that is absent stays absent -- adding one would hand a service a credential its
#: fragment deliberately did not give it.
DSN_ENV_BY_ROLE: dict[str, str] = {role: spec[0] for role, spec in ROLES.items()}


class NotReady(Exception):
    """A readiness check failed. The message is what the operator sees in ``docker inspect``."""


# --------------------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------------------


def resolved_dsn_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The ``PROXYSHOP_PG_DSN_*`` values this container should run with.

    Only variables already present in ``environ`` are returned, each rewritten through
    :func:`~proxyshop_support.postgres.role_dsn` -- which supplies the password from the one
    place that resolves it and rewrites the database component to this worker's database.

    Args:
        environ: the environment to read. Defaults to ``os.environ``.

    Returns:
        ``{variable: dsn}`` for every role variable that was set. Empty when compose gave
        this service no DSN at all, which is the correct answer for a service that talks to
        no database.
    """
    env = os.environ if environ is None else environ
    resolved: dict[str, str] = {}
    for role, env_var in DSN_ENV_BY_ROLE.items():
        if env.get(env_var) is None:
            continue
        resolved[env_var] = role_dsn(role)
    return resolved


def serve(command: Sequence[str]) -> int:
    """Resolve the DSNs into the environment, then become ``command``.

    ``execvp`` rather than ``subprocess``: the service must be PID 1's direct successor so
    that ``docker stop``'s SIGTERM reaches uvicorn itself. A wrapper process in between
    would swallow it and every shutdown would become a 10-second SIGKILL.

    Returns:
        Never, on success. ``2`` when ``command`` is empty, which is a compose-file error.
    """
    if not command:
        print("service_launch serve: no command to exec", file=sys.stderr)
        return 2
    for env_var, dsn in resolved_dsn_env().items():
        os.environ[env_var] = dsn
    os.execvp(command[0], list(command))  # noqa: S606 - the command is the compose file's


# --------------------------------------------------------------------------------------
# ready
# --------------------------------------------------------------------------------------


def check_http(port: int, path: str = "/openapi.json") -> None:
    """The ASGI app imported, every router mounted, uvicorn is answering on ``port``.

    Kept from the probe this replaces. It is a real check -- it is simply not a *readiness*
    check on its own, which is the whole point of the module.
    """
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=CHECK_TIMEOUT) as response:  # noqa: S310
            if response.status != 200:
                raise NotReady(f"{url} answered {response.status}, expected 200")
    except NotReady:
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise NotReady(f"{url} did not answer: {exc}") from exc


def check_postgres(role: str, relation: str | None = None) -> None:
    """``role`` can authenticate against this worker's database, and ``relation`` exists.

    The DSN comes from :func:`~proxyshop_support.postgres.role_dsn`, so the probe
    authenticates as exactly the principal the service does -- a probe connecting as the
    superuser would stay green through a missing GRANT, which is a failure mode D5 makes
    likely rather than exotic.

    ``relation`` is checked with ``to_regclass``, which answers ``NULL`` for "no such
    relation" instead of raising, and which respects ``search_path``-free qualified names.
    Without it the probe passes against an empty database: ``make deps-up`` creates
    ``proxyshop_w<N>`` and applies no migrations at all.
    """
    import psycopg

    dsn = role_dsn(role)
    try:
        with psycopg.connect(dsn, connect_timeout=int(CHECK_TIMEOUT), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                if cur.fetchone() != (1,):
                    raise NotReady(f"postgres as {role!r} did not answer `select 1`")
                if relation is not None:
                    cur.execute("select to_regclass(%s)", (relation,))
                    row = cur.fetchone()
                    if row is None or row[0] is None:
                        raise NotReady(
                            f"postgres as {role!r}: relation {relation!r} does not exist or is "
                            f"not visible to this role -- the migrations have not been applied "
                            f"to this database (see `scripts/db_migrate.py`)"
                        )
    except NotReady:
        raise
    except psycopg.Error as exc:
        raise NotReady(f"postgres as {role!r} refused the connection: {exc}") from exc


def check_redis() -> None:
    """Redis answers PING, and this worker's index is one the server can isolate (D39).

    Built through :func:`~proxyshop_support.redis_client.worker_redis` and never as a raw
    ``redis.Redis``. That is D39, enforced by ``scripts/check_verify_contracts.py`` — which
    caught the first draft of this function doing exactly that. The rule is about unprefixed
    keys and a shared logical DB, and a bare ``PING`` writes no keys at all, so the first
    draft was harmless *and* the checker was still right to refuse it: a carve-out for "this
    particular client only reads" is precisely how the next one starts writing.

    Using the required client is also the better check. ``worker_redis`` refuses, at
    construction, a worker index the running server cannot isolate — a state in which no
    route touching auction state can serve — so that condition is covered here for free.

    Bounded without needing its own timeouts: ``worker_redis`` builds its client with
    ``socket_connect_timeout=2.0`` and ``socket_timeout=5.0``, and the compose healthcheck's
    own ``timeout: 5s`` is the backstop that makes a pathological hang a failure rather than
    a container stuck in ``starting``.
    """
    from proxyshop_support.redis_client import worker_redis

    url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        worker_redis(url).ping()
    except Exception as exc:  # noqa: BLE001 - every redis failure is "not ready"
        raise NotReady(f"redis at {url} did not answer PING: {exc}") from exc


def check_neo4j() -> None:
    """The bolt endpoint answers and accepts THE CREDENTIAL THE SERVED CODE WILL OFFER.

    ``verify_connectivity`` opens a real session against the configured URI rather than
    TCP-probing the port: a Neo4j that is listening but still recovering its store answers a
    socket connect and refuses a session, and the difference is exactly the window in which
    an ingest container would report healthy and fail every write.

    **The credential comes from** :func:`proxyshop_support.neo4j_auth.graph_credentials`,
    which is the one resolver in the tree, and that is the substance of this function rather
    than a tidier import. It used to read the three variables itself and default the password
    to ``""``, against ``proxyshop_dev_pw`` in ``exchange.retrieval.roster`` and in
    ``ingest.graph.reembed`` — the two SERVED readers this probe exists to vouch for. Three
    defaults for one credential means this check was not a weaker or stronger version of the
    served path's authentication, it was authentication of something else: against a server
    with auth disabled the empty password succeeds and the served path's does not, and
    against a server seeded with the dev pair the empty one is refused while every served
    read works. Either way the health signal and the service disagree, silently, and the
    container's own logs contain no line connecting them.

    ``.env.example``, ``docker-compose.yml``'s ``NEO4J_AUTH`` and both compose fragments all
    carry the same dev pair, so on the shipped stack this changes nothing; what it changes is
    a deployment that sets ``NEO4J_PASSWORD`` for the service and not for the probe, or the
    reverse. Neither is now expressible.

    Both ``services/ingest`` and ``apps/exchange`` declare this check. The old note here said
    only ingest could, because only the ingest image installed the ``neo4j`` driver — both
    halves have since moved: ``apps/exchange/Dockerfile`` installs ``neo4j==5.28.5`` and
    ``EXCHANGE_SHOP_ROSTER`` defaults to ``graph``, so the catalogue graph is on the served
    path of every roster-less auction.
    """
    from neo4j import GraphDatabase

    from proxyshop_support.neo4j_auth import graph_credentials

    credentials = graph_credentials()
    driver = None
    try:
        driver = GraphDatabase.driver(
            credentials.uri,
            auth=credentials.auth,
            connection_timeout=CHECK_TIMEOUT,
            connection_acquisition_timeout=CHECK_TIMEOUT,
        )
        driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001 - every neo4j failure is "not ready"
        # `describe()` and never the password: an operator reading a stuck container's health
        # log needs to know WHICH credential was offered and where it came from — the two
        # answers are "NEO4J_PASSWORD" and "the development default, because NEO4J_PASSWORD is
        # unset", and the second is the one that explains an `Unauthorized` in production.
        raise NotReady(
            f"neo4j at {credentials.describe()} did not accept a session: {exc}"
        ) from exc
    finally:
        if driver is not None:
            with contextlib.suppress(Exception):
                driver.close()


def check_env(names: Sequence[str]) -> None:
    """Every variable in ``names`` is set and non-empty.

    For settings whose absence makes a route refuse every caller rather than fail loudly at
    startup -- ``MERCHANT_ADMIN_TOKEN`` is the measured one: without it
    ``GET /install/shops`` answers ``503 {"error":"admin-api-not-configured"}`` while the
    container reports healthy.
    """
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise NotReady(f"required configuration is unset: {', '.join(missing)}")


def ready(
    http_port: int,
    *,
    postgres: Sequence[str] = (),
    redis: bool = False,
    neo4j: bool = False,
    require_env: Sequence[str] = (),
) -> list[str]:
    """Run every configured check. Returns the checks that passed; raises :class:`NotReady`.

    Order is deliberate: cheapest and most-likely-first. The HTTP check runs before any
    datastore check so that a container that has not finished importing reports *that*,
    rather than a confusing datastore message from a process that is not listening yet.
    """
    passed = ["http"]
    check_http(http_port)
    if require_env:
        check_env(require_env)
        passed.append(f"env({','.join(require_env)})")
    for spec in postgres:
        role, _, relation = spec.partition(":")
        check_postgres(role, relation or None)
        passed.append(f"postgres:{spec}")
    if redis:
        check_redis()
        passed.append("redis")
    if neo4j:
        check_neo4j()
        passed.append("neo4j")
    return passed


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The CLI. ``serve`` takes a REMAINDER so the service's own flags reach it untouched."""
    parser = argparse.ArgumentParser(prog="proxyshop_support.service_launch")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    serve_parser = sub.add_parser("serve", help="resolve DSNs, then exec the service")
    serve_parser.add_argument("command", nargs=argparse.REMAINDER)

    ready_parser = sub.add_parser("ready", help="the readiness probe (compose healthcheck)")
    ready_parser.add_argument("--http-port", type=int, required=True)
    ready_parser.add_argument(
        "--postgres",
        action="append",
        default=[],
        metavar="ROLE[:RELATION]",
        help="authenticate as ROLE; when RELATION is given, require it to exist",
    )
    ready_parser.add_argument("--redis", action="store_true")
    ready_parser.add_argument("--neo4j", action="store_true")
    ready_parser.add_argument("--require-env", action="append", default=[], metavar="NAME")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.subcommand == "serve":
        # Strip only the LEADING `--` separator argparse leaves on a REMAINDER. Dropping
        # every `--` would eat one the service itself meant to pass through to its own
        # argument parser.
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        return serve(command)

    try:
        passed = ready(
            args.http_port,
            postgres=args.postgres,
            redis=args.redis,
            neo4j=args.neo4j,
            require_env=args.require_env,
        )
    except NotReady as exc:
        print(f"NOT READY: {exc}", file=sys.stderr)
        return 1
    print(f"READY: {' '.join(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
