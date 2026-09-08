"""T-112: the role password has ONE source of truth on the project's own fresh volume.

T-110 taught ``db/init/00-roles.sql`` to read ``$PROXYSHOP_ROLE_PASSWORD``. Its verifier
then found T-110's own objective still true where it matters, in two independent places:

* **The SEED side.** ``docker-compose.yml``'s postgres service forwarded no
  ``PROXYSHOP_ROLE_PASSWORD`` into the container, so on the volume the project actually
  creates -- ``make deps-up`` -- the initdb hook saw an unset variable and the dev default
  won no matter what the host environment said. Reproduced before the fix::

      $ PROXYSHOP_ROLE_PASSWORD=t112-not-the-default docker compose config --format json
      postgres environment keys: ['POSTGRES_DB', 'POSTGRES_PASSWORD', 'POSTGRES_USER']
      PROXYSHOP_ROLE_PASSWORD forwarded: False

* **The CONNECT side.** ``proxyshop_support.postgres.ROLES`` and ``.env.example`` carried
  four independent copies each of the literal ``'x'``, and neither consulted the variable,
  so a correctly-seeded cluster was unreachable by the app. Reproduced before the fix::

      $ PROXYSHOP_ROLE_PASSWORD=t112-not-the-default python -c '...role_dsn("exchange", 3)'
      exchange -> postgresql://exchange:x@localhost:5432/proxyshop_w3

A password that seeds but cannot connect is not one source of truth, it is a broken
deployment; the two halves are graded together here for that reason.

Why this module and not ``apps/trust/tests/test_schema_grants.py``: T-112's ``scope`` is
``docker-compose.yml, proxyshop_support/**, .env.example`` and does not include
``apps/trust/**``. The lane gate runs ``pytest proxyshop_support -q``, so these tests are
executed by the same gate.

The docker test spins a **private, throwaway** postgres container -- its own anonymous
volume, an ephemeral loopback-only published port, ``docker rm -f -v`` at teardown -- the
``-v`` is load-bearing, not decoration; see the teardown comment -- because
a fresh volume is the only state in which the initdb hook runs at all, and the roles it
creates are cluster-global: proving anything on the shared stack would mean rewriting
credentials five other live lanes are authenticating with.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from proxyshop_support.postgres import (
    DEV_ROLE_PASSWORD,
    ROLE_PASSWORD_ENV,
    ROLES,
    SEEDED_ROLES,
    role_dsn,
    with_role_password,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DB_INIT_ROLES_SQL = REPO_ROOT / "db" / "init" / "00-roles.sql"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
POSTGRES_MODULE = REPO_ROOT / "proxyshop_support" / "postgres.py"

#: Every ``PROXYSHOP_PG_DSN_*`` override, so a test can prove the *default* path.
_DSN_ENV_VARS = tuple(env_var for env_var, _, _ in ROLES.values())


def _postgres_service_block() -> str:
    """The text of docker-compose.yml's ``postgres:`` service, and nothing else.

    Grading the whole file would let a ``PROXYSHOP_ROLE_PASSWORD`` forwarded to *neo4j*
    satisfy an assertion about postgres.
    """
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    start = re.search(r"^  postgres:\s*$", text, re.MULTILINE)
    assert start is not None, "docker-compose.yml no longer declares a postgres service"
    rest = text[start.end() :]
    end = re.search(r"^  \S", rest, re.MULTILINE)
    block = rest[: end.start()] if end else rest
    # Comments out: a block comment *describing* the wiring must never be what satisfies an
    # assertion that the wiring is there. Compose values in this file carry no '#'.
    return "\n".join(line.split("#", 1)[0] for line in block.splitlines())


#: One ``KEY: value`` out of a compose mapping. Quoted forms are matched FIRST and consume to
#: their closing quote, because the unquoted branch stops at ``}`` and an interpolated value
#: contains one.
def _compose_entry(key: str) -> re.Pattern[str]:
    return re.compile(
        rf"""\b{re.escape(key)}\s*:\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s,}}]+))"""
    )


#: ``${NAME}``, ``${NAME:-default}`` or ``${NAME-default}`` — compose's own interpolation.
_INTERPOLATION = re.compile(r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::?-(?P<default>.*))?\}$")


def _compose_value(block: str, key: str) -> str | None:
    """The value compose gives ``key`` **in the shipped configuration**, or ``None``.

    "Shipped" means with nothing exported: an interpolation resolves to its ``:-`` default,
    which is the value a reader of this repository actually gets. A ``${NAME}`` with no
    default resolves to the empty string, which is what compose substitutes (with a warning).

    Written because the regex this replaces — ``([^\\s,}]+)`` — could not read an
    interpolation at all. It stopped at the first ``}``, so when ``docker-compose.yml``
    commit 6555f27 ("the datastore passwords are settable by a deployment") turned
    ``POSTGRES_PASSWORD: proxyshop_dev_pw`` into
    ``POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:-proxyshop_dev_pw}"``, the extractor returned
    the string ``"${POSTGRES_PASSWORD:-proxyshop_dev_pw`` and
    :func:`test_the_admin_password_is_pinned_to_composes_superuser_password` failed against a
    compose file that was, and is, correct. That commit changed one file and left this
    reader behind.

    **The assertion it feeds was not weakened**: it still compares the effective superuser
    password against ``ROLES['admin']`` and still fails if the two diverge — see
    :func:`test_the_compose_value_reader_resolves_what_compose_resolves`, which pins this
    resolver against the forms it has to survive so a future compose edit cannot quietly
    slip past it the way this one did.
    """
    found = _compose_entry(key).search(block)
    if found is None:
        return None
    raw = next(
        value for value in (found.group("dq"), found.group("sq"), found.group("bare")) if value
    )
    interpolated = _INTERPOLATION.match(raw)
    if interpolated is None:
        return raw
    return interpolated.group("default") or ""


# ---------------------------------------------------------------------------------------
# Acceptance 1 -- the seed side: compose forwards the variable
# ---------------------------------------------------------------------------------------


def test_compose_forwards_the_role_password_into_the_postgres_service() -> None:
    """T-112 acceptance 1, on the file that creates the project's own fresh volume.

    ``db/init/00-roles.sql`` runs *inside* the container, so it can only read variables the
    container has. Before this ticket the postgres service forwarded none, which made the
    whole of T-110 unobservable on ``make deps-up``.
    """
    block = _postgres_service_block()
    assert re.search(r"\bPROXYSHOP_ROLE_PASSWORD\s*:", block), (
        "docker-compose.yml's postgres service does not forward PROXYSHOP_ROLE_PASSWORD. "
        "db/init/00-roles.sql reads it from the container's environment, so without this "
        "line the dev default wins on every fresh volume the project itself creates -- "
        "which is T-110's defect, still true, one layer down."
    )
    assert re.search(
        r"""PROXYSHOP_ROLE_PASSWORD\s*:\s*["']?\$\{PROXYSHOP_ROLE_PASSWORD""", block
    ), (
        "the forwarded value must be an interpolation of the host's own "
        "PROXYSHOP_ROLE_PASSWORD; a fixed value here would be a THIRD source of truth."
    )


def test_compose_does_not_re_declare_the_dev_default_password() -> None:
    """T-112 acceptance 4, for the file added in acceptance 1.

    The obvious way to write acceptance 1 is ``${PROXYSHOP_ROLE_PASSWORD:-x}``, which
    preserves the dev default by *copying the literal into a second file* -- the exact
    shape this ticket exists to remove. An empty compose-side default reaches
    ``coalesce(nullif(current_setting(...), ''), 'x')`` in db/init/00-roles.sql, which is
    the one place the literal is allowed to live, so the default is preserved without
    being duplicated.
    """
    block = _postgres_service_block()
    default = re.search(r"\$\{PROXYSHOP_ROLE_PASSWORD:?-(?P<value>[^}]*)\}", block)
    assert default is not None, (
        "docker-compose.yml interpolates PROXYSHOP_ROLE_PASSWORD without a `:-` default. "
        "Compose then warns and substitutes an empty string anyway, so say so explicitly."
    )
    assert default.group("value") == "", (
        f"docker-compose.yml gives PROXYSHOP_ROLE_PASSWORD the compose-side default "
        f"{default.group('value')!r}. That is a second copy of the dev password; the default "
        f"belongs only in db/init/00-roles.sql's coalesce, which an empty value falls "
        f"through to via nullif(..., '')."
    )


def test_the_connect_side_and_the_seed_side_name_the_same_variable() -> None:
    """One variable, spelled the same in the SQL that seeds and the Python that connects.

    Read out of the files rather than asserted as a constant on both sides: two halves
    that agree only because the same string was typed twice is the failure mode T-110's
    verifier found, and it is invisible to an equality check against a literal.
    """
    init_sql = DB_INIT_ROLES_SQL.read_text(encoding="utf-8")
    assert f"\\getenv proxyshop_role_password {ROLE_PASSWORD_ENV}" in init_sql, (
        f"db/init/00-roles.sql does not read ${ROLE_PASSWORD_ENV} -- the name the connect "
        f"side in proxyshop_support.postgres consults. The seed side and the connect side "
        f"reading two different variables is two sources of truth wearing one name."
    )
    assert re.search(r"\bPROXYSHOP_ROLE_PASSWORD\s*:", _postgres_service_block()), (
        "compose must forward the same variable both other halves read"
    )


def test_the_connect_sides_dev_default_is_the_one_the_seed_side_falls_back_to() -> None:
    """The seed default lives in SQL; the connect default has to live in Python. Pin them.

    ``proxyshop_support`` cannot read ``db/init/00-roles.sql`` at import time (it is a
    library, and the SQL is not on any deployment path it controls), so the historical
    literal genuinely exists twice: once in that file's ``coalesce`` and once as
    :data:`~proxyshop_support.postgres.DEV_ROLE_PASSWORD`. Two copies that cannot drift are
    one source of truth; two copies that can are the defect. This is the assertion that
    makes the difference, and ``apps/trust/tests/test_schema_grants.py`` already pins
    ``db/migrations/0001``'s copy to the same expression, which closes the triangle.
    """
    init_sql = DB_INIT_ROLES_SQL.read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in init_sql.splitlines())
    fallback = re.search(
        r"coalesce\s*\(\s*nullif\s*\(\s*current_setting\s*\([^)]*\)\s*,\s*''\s*\)\s*,\s*'"
        r"(?P<default>[^']*)'\s*\)",
        code,
        re.IGNORECASE | re.DOTALL,
    )
    assert fallback is not None, (
        "db/init/00-roles.sql no longer resolves the role password through "
        "coalesce(nullif(current_setting(...), ''), '<default>') -- the shape the connect "
        "side's default is pinned to"
    )
    assert fallback.group("default") == DEV_ROLE_PASSWORD, (
        f"db/init/00-roles.sql seeds roles with {fallback.group('default')!r} when the "
        f"environment says nothing, but proxyshop_support.postgres connects with "
        f"{DEV_ROLE_PASSWORD!r}. A checkout that sets no environment would seed a cluster "
        f"it cannot log in to -- the T-112 defect with the two halves swapped."
    )


# ---------------------------------------------------------------------------------------
# Acceptance 2 / 4 -- the connect side reads the variable, and holds one literal
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", sorted(SEEDED_ROLES))
def test_role_dsn_takes_the_password_from_the_environment(role: str, monkeypatch) -> None:
    """T-112 acceptance 2. Before the fix every one of these returned ``:x@``."""
    for env_var in _DSN_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setenv("PG_PORT", "5432")
    monkeypatch.setenv(ROLE_PASSWORD_ENV, "t112-not-the-default")

    assert (
        role_dsn(role, 3) == f"postgresql://{role}:t112-not-the-default@localhost:5432/proxyshop_w3"
    )


@pytest.mark.parametrize("role", sorted(SEEDED_ROLES))
def test_role_dsn_keeps_the_dev_default_when_the_variable_is_unset(role: str, monkeypatch) -> None:
    """The other half of acceptance 1: a checkout with no environment behaves as before.

    ``db/init/00-roles.sql``'s coalesce falls back to the same literal, so an unset
    variable must produce the historical DSN exactly -- otherwise fixing the non-default
    case would break every developer who sets nothing.
    """
    for env_var in _DSN_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv(ROLE_PASSWORD_ENV, raising=False)
    monkeypatch.setenv("PG_PORT", "5432")

    assert (
        role_dsn(role, 3) == f"postgresql://{role}:{DEV_ROLE_PASSWORD}@localhost:5432/proxyshop_w3"
    )


def test_the_role_password_does_not_reach_the_cluster_superuser(monkeypatch) -> None:
    """``admin`` is compose's ``POSTGRES_PASSWORD``, not one of 00-roles.sql's four roles.

    Letting ``$PROXYSHOP_ROLE_PASSWORD`` rewrite it would make every admin connection fail
    the moment somebody set the variable -- a different credential, seeded by a different
    mechanism, on a fresh volume.
    """
    monkeypatch.setenv(ROLE_PASSWORD_ENV, "t112-not-the-default")
    monkeypatch.delenv("PROXYSHOP_PG_DSN_ADMIN", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setenv("PG_PORT", "5432")

    assert "admin" not in SEEDED_ROLES
    assert (
        role_dsn("admin", 3)
        == "postgresql://proxyshop:proxyshop_dev_pw@localhost:5432/proxyshop_w3"
    )


def test_an_explicit_dsn_password_still_wins_over_the_dev_default(monkeypatch) -> None:
    """The ``PROXYSHOP_PG_DSN_*`` escape hatch survives, and is not double-encoded.

    ``.env`` keeps its documented meaning for host, port and credentials; only the *seeded*
    variable outranks it, because that variable is what actually created the role.
    """
    monkeypatch.delenv(ROLE_PASSWORD_ENV, raising=False)
    monkeypatch.setenv(
        "PROXYSHOP_PG_DSN_EXCHANGE", "postgresql://exchange:p%40ss@db.example:6000/proxyshop_w1"
    )
    assert role_dsn("exchange", 4) == "postgresql://exchange:p%40ss@db.example:6000/proxyshop_w4"

    monkeypatch.setenv(ROLE_PASSWORD_ENV, "t112-wins")
    assert role_dsn("exchange", 4) == "postgresql://exchange:t112-wins@db.example:6000/proxyshop_w4"


def test_a_password_needing_percent_encoding_survives_the_round_trip(monkeypatch) -> None:
    """A DSN is a URI: an unescaped ``@`` or ``/`` in the password silently re-points it.

    Without quoting, ``PROXYSHOP_ROLE_PASSWORD='p@ss'`` would produce
    ``postgresql://exchange:p@ss@host/db`` and libpq would read the host as ``ss``.
    """
    import psycopg.conninfo

    for env_var in _DSN_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setenv("PG_PORT", "5432")
    monkeypatch.setenv(ROLE_PASSWORD_ENV, "p@ss/w:rd#1")

    parsed = psycopg.conninfo.conninfo_to_dict(role_dsn("exchange", 3))
    assert parsed["password"] == "p@ss/w:rd#1"
    assert parsed["host"] == "localhost"
    assert parsed["dbname"] == "proxyshop_w3"


def test_only_one_dev_password_literal_survives_on_the_connect_side() -> None:
    """T-112 acceptance 4, on the two files that carried four copies each.

    ``.env.example`` may not spell the role password at all -- a file every worktree copies
    is the worst possible second home for it -- and ``proxyshop_support.postgres`` must
    reach it through the single :data:`DEV_ROLE_PASSWORD` constant.
    """
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    stray = [
        line
        for line in env_example.splitlines()
        if line.startswith("PROXYSHOP_PG_DSN_")
        and re.search(rf"://[^:@\s]+:{re.escape(DEV_ROLE_PASSWORD)}@", line)
    ]
    assert stray == [], (
        f".env.example still hard-codes the role password in {stray}. Every worktree copies "
        f"this file, so each copy is an independent source of truth that stops agreeing "
        f"with db/init/00-roles.sql the moment PROXYSHOP_ROLE_PASSWORD is set."
    )

    source = POSTGRES_MODULE.read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    literals = re.findall(rf"""['"]{re.escape(DEV_ROLE_PASSWORD)}['"]""", code)
    assert len(literals) == 1, (
        f"expected exactly one {DEV_ROLE_PASSWORD!r} literal in proxyshop_support/postgres.py "
        f"(the DEV_ROLE_PASSWORD constant), found {len(literals)}. Four independent copies "
        f"in the ROLES table is the defect T-112 exists to close."
    )


def test_env_example_carries_no_postgres_password_at_all() -> None:
    """T-112 acceptance 4, stated as the file-level property it actually is.

    ``.env.example`` is copied into every worktree, so any credential spelled here becomes
    as many independent sources of truth as there are checkouts. All five ``PROXYSHOP_PG_DSN_*``
    values therefore carry a user and no password: the four seeded roles get theirs from
    ``$PROXYSHOP_ROLE_PASSWORD``, and ``admin`` from :data:`ROLES`, which the test below pins
    to compose's own ``POSTGRES_PASSWORD``.
    """
    offenders = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("PROXYSHOP_PG_DSN_"):
            continue
        _, _, dsn = line.partition("=")
        if urlsplit(dsn).password:
            offenders.append(line)
    assert offenders == [], (
        f".env.example spells a Postgres password in {offenders}. proxyshop_support.postgres "
        f"supplies every one of them from a single place; a copy here only creates something "
        f"for that place to disagree with."
    )


def test_the_admin_password_is_pinned_to_composes_superuser_password() -> None:
    """The cluster superuser is a *different* credential, and it also has two copies.

    ``admin`` is not one of ``db/init/00-roles.sql``'s four roles: it is created by initdb
    from compose's ``POSTGRES_PASSWORD``, so ``$PROXYSHOP_ROLE_PASSWORD`` deliberately does
    not govern it. That leaves the literal in exactly two places — the compose file that
    seeds it and the ``ROLES`` table that connects with it — for the same reason the dev role
    password is in two: a library cannot read the deployment. Two copies that cannot drift
    are one source of truth, so pin them, rather than leave a changed compose password to be
    discovered as every admin connection failing at once.

    The password is now an interpolation with a default rather than a bare literal, so the
    comparison is against the value compose resolves with nothing exported (see
    :func:`_compose_value`). That is the same claim, read correctly: a deployment that
    exports its own ``POSTGRES_PASSWORD`` moves the seed side and the connect side together
    only if it also hands ``ROLES`` an override DSN, and *that* is what
    ``PROXYSHOP_PG_DSN_ADMIN`` is for.
    """
    block = _postgres_service_block()
    user = _compose_value(block, "POSTGRES_USER")
    password = _compose_value(block, "POSTGRES_PASSWORD")
    assert user is not None and password is not None, (
        "docker-compose.yml's postgres service no longer declares POSTGRES_USER/PASSWORD"
    )
    assert (ROLES["admin"][1], ROLES["admin"][2]) == (user, password), (
        f"proxyshop_support.postgres connects as "
        f"{ROLES['admin'][1]}/{ROLES['admin'][2]} but docker-compose.yml creates the "
        f"superuser as {user}/{password}. Every admin connection in the "
        f"repo — including the one that CREATEs each worker database — goes through ROLES."
    )


def test_the_compose_value_reader_resolves_what_compose_resolves() -> None:
    """The extractor above, pinned against the forms a compose file actually carries.

    Added with the fix, because the defect it repairs was invisible: the old regex did not
    raise or return ``None`` on an interpolation, it returned a **truncated string**, and the
    only symptom was an assertion about Postgres failing with a confusing message. A reader
    that can be silently wrong needs its own test, or the next compose edit reopens this.

    The last two cases are the ones that matter: a resolver that returned the raw text would
    pass every other case here and still fail the real file.
    """
    for block, key, expected in [
        ("environment: { POSTGRES_USER: proxyshop, POSTGRES_DB: x }", "POSTGRES_USER", "proxyshop"),
        ('{ A: "plain-quoted" }', "A", "plain-quoted"),
        ("{ A: 'single-quoted' }", "A", "single-quoted"),
        ('{ A: "${SOME_VAR:-the-default}" }', "A", "the-default"),
        ('{ A: "${SOME_VAR-the-default}" }', "A", "the-default"),
        ('{ A: "${SOME_VAR}" }', "A", ""),
        ('{ A: "${SOME_VAR:-}" }', "A", ""),
        (
            '{ POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:-proxyshop_dev_pw}" }',
            "POSTGRES_PASSWORD",
            "proxyshop_dev_pw",
        ),
        ("{ A: x }", "MISSING", None),
    ]:
        assert _compose_value(block, key) == expected, (
            f"_compose_value({block!r}, {key!r}) did not resolve to {expected!r}; the "
            f"reader that feeds the admin-password pin is wrong, so that pin is grading "
            f"a value docker compose would never produce."
        )

    # The pin must still BITE. A compose file whose effective default really has drifted
    # from ROLES has to be caught, which is the whole point of the assertion above.
    drifted = '{ POSTGRES_USER: proxyshop, POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:-changed}" }'
    assert _compose_value(drifted, "POSTGRES_PASSWORD") != ROLES["admin"][2], (
        "a compose file whose superuser password no longer matches ROLES['admin'] resolved "
        "to the ROLES value anyway; the pin has stopped being able to fail."
    )


# ---------------------------------------------------------------------------------------
# Acceptance 3 -- a non-default value SEEDS and CONNECTS, end to end, on a fresh volume
# ---------------------------------------------------------------------------------------


def _docker(*argv: str, timeout: int = 180, env: dict[str, str] | None = None):
    # T-122 sweep: deliberately no PYTHONPATH. The child is the `docker` CLI, not a Python
    # interpreter; what it needs is PROXYSHOP_ROLE_PASSWORD, which callers pass in `env`.
    return subprocess.run(
        ["docker", *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=REPO_ROOT,
        env=env,
    )


def _require_docker_cli() -> None:
    """Skip only when there is no docker daemon to talk to -- never for a failing container."""
    if shutil.which("docker") is None:
        pytest.skip("the docker CLI is not on PATH")
    probe = _docker("info", "--format", "{{.ServerVersion}}", timeout=60)
    if probe.returncode != 0:
        pytest.skip(f"the docker daemon is not reachable: {probe.stderr.strip()[:200]}")


def _resolved_postgres_service(role_password: str | None) -> dict:
    """What compose ACTUALLY hands the postgres container, with ``role_password`` on the host.

    This is the difference between grading a line of YAML and grading the deployment: the
    interpolation, the ``include:`` fragments and any ``.env`` in the project directory all
    take part, exactly as they do under ``make deps-up``.
    """
    environment = {k: v for k, v in os.environ.items() if k != ROLE_PASSWORD_ENV}
    if role_password is not None:
        environment[ROLE_PASSWORD_ENV] = role_password
    resolved = _docker(
        "compose", "-f", str(COMPOSE_FILE), "config", "--format", "json", env=environment
    )
    assert resolved.returncode == 0, f"docker compose config failed: {resolved.stderr}"
    return json.loads(resolved.stdout)["services"]["postgres"]


@contextlib.contextmanager
def _fresh_volume_from_compose(
    worker_index: int, role_password: str | None
) -> Iterator[tuple[str, str, dict[str, str]]]:
    """A throwaway postgres seeded with *compose's own* resolved environment.

    Yields ``(container_name, host_port, environment)``. Nothing is shared with the live
    stack: a per-worker name with a random suffix, an anonymous volume, and a port
    published on 127.0.0.1 with an **ephemeral** number so it cannot land on the stack's.
    """
    _require_docker_cli()
    service = _resolved_postgres_service(role_password)
    environment = {k: v for k, v in service["environment"].items() if v is not None}
    name = f"proxyshop_w{worker_index}_t112_{uuid.uuid4().hex[:8]}"

    argv = ["run", "-d", "--name", name, "--memory", "512m", "-p", "127.0.0.1::5432"]
    for key, value in sorted(environment.items()):
        argv += ["-e", f"{key}={value}"]
    argv += [
        "-v",
        f"{REPO_ROOT / 'db' / 'init'}:/docker-entrypoint-initdb.d:ro",
        service["image"],
    ]

    created = _docker(*argv)
    assert created.returncode == 0, f"could not start the throwaway postgres: {created.stderr}"
    try:
        deadline = time.monotonic() + 120
        while True:
            ready = _docker(
                "exec", name, "pg_isready", "-h", "127.0.0.1",
                "-U", environment["POSTGRES_USER"], "-d", environment["POSTGRES_DB"], timeout=60,
            )  # fmt: skip
            if ready.returncode == 0:
                break
            state = _docker("inspect", "-f", "{{.State.Running}}", name, timeout=60)
            assert state.stdout.strip() == "true", (
                f"the throwaway postgres exited during init:\n{_docker('logs', name).stderr}"
            )
            assert time.monotonic() < deadline, (
                f"the throwaway postgres never accepted TCP:\n{_docker('logs', name).stderr}"
            )
            time.sleep(0.5)

        logs = _docker("logs", name)
        assert "/docker-entrypoint-initdb.d/00-roles.sql" in logs.stdout + logs.stderr, (
            "the initdb hook never ran 00-roles.sql -- the db/init mount did not take"
        )

        published = _docker("port", name, "5432/tcp", timeout=60)
        assert published.returncode == 0, f"the container published no port: {published.stderr}"
        host_port = published.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        yield name, host_port, environment
    finally:
        # `-v`, for the same reason as apps/trust/tests/test_schema_grants.py:1579 -- the
        # postgres image declares a VOLUME on its data directory, so `docker run` without an
        # explicit mount creates an ANONYMOUS volume, and `docker rm -f` does NOT remove it.
        # This fixture's sibling was fixed; this one was missed, so it kept leaking ~200MB
        # per use. That is the leak that filled this host's Docker VM disk (254 volumes
        # reclaimed by hand once, 23 more pruned since). `-v` removes only the anonymous
        # volumes the container owns -- the `db/init` bind mount is a host path, not a
        # volume, so it is untouched.
        _docker("rm", "-f", "-v", name, timeout=120)


def _point_the_connect_side_at(monkeypatch, host_port: str, role_password: str | None) -> None:
    """Make ``role_dsn`` describe the throwaway container and nothing else."""
    for env_var in _DSN_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    monkeypatch.setenv("PG_PORT", host_port)
    if role_password is None:
        monkeypatch.delenv(ROLE_PASSWORD_ENV, raising=False)
    else:
        monkeypatch.setenv(ROLE_PASSWORD_ENV, role_password)


@pytest.mark.docker("postgres")  # T-172: declares Postgres; a Redis/Neo4j outage must not skip it
def test_a_non_default_role_password_seeds_and_connects_end_to_end(
    worker_index: int, monkeypatch
) -> None:
    """T-112 acceptance 3, both halves, in one run.

    The seed half goes through ``docker compose config`` -- so the container's environment
    is the one ``make deps-up`` would build -- and the connect half goes through
    :func:`proxyshop_support.postgres.role_dsn`, the function every fixture and service in
    the repo uses. Nothing in between is simulated: a real fresh volume runs
    ``db/init/00-roles.sql``, and a real psycopg connection authenticates over a published
    TCP port, which lands on the image's ``scram-sha-256`` pg_hba line rather than the
    ``trust`` loopback lines.

    The negative direction is what makes it evidence: with the variable *unset* on the
    connect side the same code must be REFUSED, because a connect side that ignored the
    environment entirely would otherwise pass the positive half by accident.
    """
    import psycopg

    password = f"t112-{uuid.uuid4().hex}"
    assert password != DEV_ROLE_PASSWORD

    with _fresh_volume_from_compose(worker_index, password) as (_, host_port, environment):
        assert environment.get(ROLE_PASSWORD_ENV) == password, (
            "docker compose did not forward PROXYSHOP_ROLE_PASSWORD into the postgres "
            "service, so the container never saw it -- T-112's seed half"
        )
        database = environment["POSTGRES_DB"]

        _point_the_connect_side_at(monkeypatch, host_port, password)
        for role in sorted(SEEDED_ROLES):
            with psycopg.connect(role_dsn(role, worker_index, database=database),
                                 connect_timeout=10) as conn:  # fmt: skip
                with conn.cursor() as cur:
                    cur.execute("select current_user")
                    assert cur.fetchone() == (role,)

        _point_the_connect_side_at(monkeypatch, host_port, None)
        for role in sorted(SEEDED_ROLES):
            with pytest.raises(psycopg.OperationalError) as refused:
                psycopg.connect(role_dsn(role, worker_index, database=database), connect_timeout=10)
            assert "password authentication failed" in str(refused.value), (
                f"{role} was refused for the wrong reason -- this is not evidence that the "
                f"connect side read ${ROLE_PASSWORD_ENV} at all: {refused.value}"
            )


@pytest.mark.docker("postgres")  # T-172: declares Postgres; a Redis/Neo4j outage must not skip it
def test_a_fresh_volume_with_no_variable_still_connects_on_the_dev_default(
    worker_index: int, monkeypatch
) -> None:
    """The compose-side default is empty, so the *SQL's* coalesce has to carry the default.

    This is the test that would catch ``${PROXYSHOP_ROLE_PASSWORD:-}`` being forwarded as a
    literal empty password instead of falling through to ``'x'`` -- a fresh clone with no
    environment must keep working exactly as it always has.
    """
    import psycopg

    with _fresh_volume_from_compose(worker_index, None) as (_, host_port, environment):
        assert environment.get(ROLE_PASSWORD_ENV, "") == "", (
            "with nothing set on the host, compose must forward an EMPTY value; a "
            "compose-side default would be a second copy of the dev password"
        )
        database = environment["POSTGRES_DB"]

        _point_the_connect_side_at(monkeypatch, host_port, None)
        for role in sorted(SEEDED_ROLES):
            with psycopg.connect(role_dsn(role, worker_index, database=database),
                                 connect_timeout=10) as conn:  # fmt: skip
                with conn.cursor() as cur:
                    cur.execute("select current_user")
                    assert cur.fetchone() == (role,)


# =====================================================================================
# The THIRD side: the callers that read a PROXYSHOP_PG_DSN_* variable directly
#
# The two sides above -- seed and connect -- are both about `role_dsn`. A handful of callers
# never go through it, because for them an UNSET variable has to keep meaning "no database at
# all", which `role_dsn` cannot express: it always answers with the compose default. Those
# callers read the raw variable and handed it straight to `psycopg.connect`.
#
# Every DSN this repository ships is PASSWORD-FREE by design (that is what T-112 decided), so
# a raw read reached libpq as an EXPLICITLY EMPTY password and was refused with `fe_sendauth:
# no password supplied` before the request left the process. MEASURED against a live cluster,
# before `with_role_password` existed:
#
#     PROXYSHOP_PG_DSN_VAULT=postgresql://buyer_vault@localhost:5432/proxyshop_w5
#     buyer_vault  RAW      -> OperationalError: fe_sendauth: no password supplied
#     buyer_vault  RESOLVED -> OK
#
# so the login vault, the profile publisher, the store-window read and the trust ledger writer
# could not open a connection in ANY deployment using the configuration this repository ships
# -- the "wired but switched off in every deployment" shape, four times over.
#
# It is graded here, beside the other two sides, because `with_role_password` is
# `proxyshop_support`'s and this file is what the lane gate runs.
# =====================================================================================

#: The four (role, env var) pairs a service reads directly, with the exact DSN shape the
#: compose fragments forward. Spelled out rather than derived from ``ROLES`` so that a change
#: to either side has to be made here too.
SHIPPED_DIRECT_READS = (
    ("buyer_vault", "PROXYSHOP_PG_DSN_VAULT", "postgresql://buyer_vault@{host}:{port}/{db}"),
    ("app", "PROXYSHOP_PG_DSN_APP", "postgresql://app@{host}:{port}/{db}"),
    ("trust_rw", "PROXYSHOP_PG_DSN_TRUST_RW", "postgresql://trust_rw@{host}:{port}/{db}"),
)


@pytest.mark.parametrize("role,_env,shape", SHIPPED_DIRECT_READS)
def test_the_dsn_shapes_this_repo_ships_carry_no_password_of_their_own(
    role: str, _env: str, shape: str
) -> None:
    """The premise of everything below: if these ever gain a password, the gate is vacuous."""
    dsn = shape.format(host="postgres", port=5432, db="proxyshop_w1")
    assert urlsplit(dsn).password is None
    assert with_role_password(role, dsn) != dsn, (
        f"{role}'s shipped DSN carries no password and with_role_password supplied none "
        f"either, so a direct reader still cannot connect: {dsn}"
    )


@pytest.mark.parametrize("role,_env,shape", SHIPPED_DIRECT_READS)
def test_a_dsn_that_states_its_own_password_is_returned_byte_for_byte(
    role: str, _env: str, shape: str
) -> None:
    """The honest-operator direction. An explicit credential must never be overwritten."""
    stated = shape.format(host="db.example", port=5432, db="proxyshop").replace(
        "@", ":deliberately-chosen@", 1
    )
    assert with_role_password(role, stated) == stated


def test_with_role_password_refuses_a_role_it_does_not_know() -> None:
    """A typo must not silently authenticate as some other principal."""
    with pytest.raises(KeyError):
        with_role_password("postgres", "postgresql://postgres@localhost:5432/proxyshop_w1")


@pytest.mark.docker("postgres")  # T-172: declares Postgres; a Redis/Neo4j outage must not skip it
@pytest.mark.parametrize("role,env_var,shape", SHIPPED_DIRECT_READS)
def test_the_shipped_password_free_dsn_opens_a_connection_after_resolution(
    role: str, env_var: str, shape: str, worker_index: int
) -> None:
    """Against the shared cluster, both directions: raw is refused, resolved connects.

    The raw half is what makes this a gate rather than a smoke test. A connection that opens
    for some other reason -- a ``.pgpass``, a ``PGPASSWORD`` in the environment, ``trust``
    auth -- would let the resolution be deleted and this test stay green, so the refusal is
    asserted first and its MESSAGE is checked: ``fe_sendauth`` and nothing else.
    """
    import psycopg

    dsn = shape.format(host="localhost", port=5432, db=f"proxyshop_w{worker_index}")

    with pytest.raises(psycopg.OperationalError) as refused:
        psycopg.connect(dsn, connect_timeout=10)
    assert "fe_sendauth" in str(refused.value), (
        f"{role} was refused for some reason other than the missing password, so this test "
        f"is not evidence that the resolution below is doing anything: {refused.value}"
    )

    with psycopg.connect(with_role_password(role, dsn), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("select current_user")
            assert cur.fetchone() == (role,)


@pytest.mark.docker("postgres")  # T-172: declares Postgres; a Redis/Neo4j outage must not skip it
def test_every_direct_reader_seam_opens_the_connection_it_promises(
    worker_index: int, monkeypatch
) -> None:
    """The four SEAMS, driven -- not the helper they happen to call.

    Each of these reads its own variable and connects; a fix applied to three of them and
    missed on the fourth is exactly the shape this repository keeps producing, so all four are
    driven here rather than trusting a grep.
    """
    database = f"proxyshop_w{worker_index}"
    monkeypatch.setenv(
        "PROXYSHOP_PG_DSN_VAULT", f"postgresql://buyer_vault@localhost:5432/{database}"
    )
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", f"postgresql://app@localhost:5432/{database}")
    monkeypatch.setenv(
        "PROXYSHOP_PG_DSN_TRUST_RW", f"postgresql://trust_rw@localhost:5432/{database}"
    )
    monkeypatch.delenv("PROXYSHOP_LEDGER_DSN", raising=False)

    from buyer_svc.auth.routes import _app_connection_from_env, _vault_from_env
    from buyer_svc.window import routes as window_routes
    from trust.events.pg import PostgresEventStore

    vault = _vault_from_env()
    assert vault is not None, "the vault seam returned None with its variable set"

    app_connection = _app_connection_from_env()
    assert app_connection is not None
    app_connection.close()

    window_routes.set_window_connection(None)
    window_connection = window_routes._connection()
    assert window_connection is not None
    window_routes.set_window_connection(None)

    import psycopg

    ledger_dsn = PostgresEventStore()._resolve_dsn()
    assert urlsplit(ledger_dsn).password, (
        "the trust ledger writer resolved a DSN with no password, so it would be refused "
        f"with fe_sendauth before reaching the server: {ledger_dsn}"
    )
    with psycopg.connect(ledger_dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("select current_user")
            assert cur.fetchone() == ("trust_rw",)
