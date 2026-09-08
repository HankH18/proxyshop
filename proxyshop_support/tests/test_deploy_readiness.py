"""The deploy lane's readiness contract, pinned statically. No docker, no network.

Why this file exists
====================

``docker compose ps`` used to report eight rows of ``(healthy)`` over a stack whose
database-backed routes all answered 503. Two independent causes, both measured in this repo:

1. **The healthcheck proved the wrong thing.** Every service fragment probed
   ``/openapi.json``, which shows that the ASGI app imported and uvicorn is answering and
   nothing else. That is a *liveness* probe wearing a *readiness* probe's name. Measured:
   ``apps/trust`` reported healthy while ``GET /events/head`` answered
   ``503 {"error":"store_unavailable"}``.
2. **The credential could not be resolved inside the container.** Every fragment
   interpolated the role password into its DSN as ``${PROXYSHOP_ROLE_PASSWORD:-}``. On the
   documented ``cp .env.example .env`` path that variable is unset, so compose built
   ``postgresql://trust_rw:@postgres:5432/proxyshop_w1`` -- and an *explicitly empty*
   password is not the same thing as *no* password. libpq rejects it before it reaches the
   server::

       psycopg.OperationalError: connection failed: connection to server at "172.27.0.3",
       port 5432 failed: fe_sendauth: no password supplied

:mod:`proxyshop_support.service_launch` fixed both -- ``serve`` resolves the DSNs through
:func:`proxyshop_support.postgres.role_dsn` at container start, ``ready`` probes the
service's own datastores with the service's own credentials. Neither fix is self-enforcing:
a healthcheck can drift back to ``/openapi.json`` in one line, a fragment can grow a
``depends_on`` without growing the matching probe, and the ``command:`` that compose forces
each fragment to duplicate can silently stop matching its image's ``CMD``.

So this file is the guard, and it is deliberately made of two cheap kinds of test:

* **static** -- it parses ``docker-compose.yml``, the nine fragments it ``include:``s, the
  three Dockerfiles whose ``CMD`` is mirrored into a fragment, ``.env.example`` and the
  demo runbook. Files on disk, no daemon, no container.
* **unit** -- it calls the already-written functions in
  :mod:`proxyshop_support.service_launch` with a monkeypatched environment.

Nothing here needs the compose stack, so nothing here is marked ``docker`` and nothing here
can skip. A skipped deploy test looks exactly like a passing one in the metrics, which is
the failure mode the whole readiness effort exists to delete.
"""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml
from psycopg.conninfo import conninfo_to_dict

from proxyshop_support import service_launch
from proxyshop_support.asgi_server import serve as serve_asgi
from proxyshop_support.postgres import DEV_ROLE_PASSWORD, ROLE_PASSWORD_ENV, ROLES
from proxyshop_support.service_launch import (
    NotReady,
    check_env,
    check_http,
    ready,
    resolved_dsn_env,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_COMPOSE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
STARTING_SLICE = REPO_ROOT / "docs" / "demo" / "starting-slice.md"

#: The forbidden shape: ``PROXYSHOP_ROLE_PASSWORD`` interpolated into the *password
#: position* of a URI, i.e. immediately after the ``:`` that follows the username. A bare
#: ``PROXYSHOP_ROLE_PASSWORD: "${PROXYSHOP_ROLE_PASSWORD:-}"`` environment line is a
#: different thing entirely and is FINE -- the container forwards the variable and
#: ``role_dsn`` reads it. It is only the in-DSN form that turns "unset" into "empty".
ROLE_PASSWORD_IN_DSN = ":${PROXYSHOP_ROLE_PASSWORD"

#: ``depends_on`` service name -> the ``service_launch ready`` flag that must appear in the
#: depending service's healthcheck. Declaring a dependency and not probing it is exactly the
#: healthy-but-503 defect; probing a datastore that is not depended on is the same lie in
#: the other direction, so the mapping is read in one direction only.
DATASTORE_FLAG: dict[str, str] = {
    "postgres": "--postgres",
    "redis": "--redis",
    "neo4j": "--neo4j",
}

#: Compose CLEARS the image's ``CMD`` when a fragment overrides ``command:``, so each of
#: these three fragments has to repeat its Dockerfile's uvicorn line verbatim after the
#: ``serve --`` sentinel. Duplicated configuration drifts; this is the pin that stops it.
#: service name -> (fragment, Dockerfile).
MIRRORED_COMMANDS: dict[str, tuple[str, str]] = {
    "trust": ("apps/trust/compose.yaml", "apps/trust/Dockerfile"),
    "buyer-svc": ("apps/buyer/compose.yaml", "apps/buyer/Dockerfile"),
    "exchange": ("apps/exchange/compose.yaml", "apps/exchange/Dockerfile"),
}

#: The tokens every mirrored ``command:`` must START with, so the DSN resolution actually
#: runs before the service does.
SERVE_PREFIX: list[str] = ["python", "-m", "proxyshop_support.service_launch", "serve", "--"]

#: Services allowed to keep an HTTP-only healthcheck, named explicitly rather than skipped
#: silently, each with the reason it is earned. Both reasons are re-checked by
#: :func:`test_the_http_only_healthcheck_exemptions_are_still_earned`, so an exemption that
#: stops being true fails instead of quietly widening.
HTTP_ONLY_EXEMPT: dict[str, str] = {
    # packages/store-agent declares no `depends_on` on any datastore at all -- it holds no
    # database, no redis client and no bolt session -- so "the app imported and is
    # answering" IS the whole of its readiness. There is nothing behind the probe to check.
    "store-agent": "declares no datastore dependency, so HTTP liveness is its whole surface",
    # services/shopify-stub probes `/healthz`, a real route of its own rather than the
    # framework-generated `/openapi.json`. It is a stub with no datastore; its own route
    # answering is the strongest statement available about it.
    "shopify-stub": "probes its own real /healthz route, not the generated /openapi.json",
}


# --------------------------------------------------------------------------------------
# helpers -- compose and Dockerfile parsing (yaml.safe_load, as the rest of the repo does)
# --------------------------------------------------------------------------------------


def _rel(path: Path) -> str:
    """Repo-relative POSIX path, so assertion messages name a file you can open."""
    return path.relative_to(REPO_ROOT).as_posix()


def _fragment_paths() -> list[Path]:
    """Every fragment the root stack ``include:``s, in the root file's own order.

    Derived from ``docker-compose.yml`` rather than from a glob so that a fragment added to
    the stack later is graded by these tests without an edit here, and a file that is not
    part of the stack is not graded at all.
    """
    root = yaml.safe_load(ROOT_COMPOSE.read_text(encoding="utf-8")) or {}
    return [REPO_ROOT / str(entry) for entry in (root.get("include") or [])]


def _services(path: Path) -> dict[str, dict[str, Any]]:
    """``{service name: definition}`` for one compose file; ``{}`` for a stub fragment."""
    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    services = parsed.get("services") or {}
    return {str(name): (body or {}) for name, body in services.items()}


def _all_fragment_services() -> list[tuple[Path, str, dict[str, Any]]]:
    """Every ``(fragment, service name, definition)`` in the included fragments."""
    found: list[tuple[Path, str, dict[str, Any]]] = []
    for path in _fragment_paths():
        for name, body in _services(path).items():
            found.append((path, name, body))
    return found


def _depends_on(body: dict[str, Any]) -> set[str]:
    """The service names this definition depends on, for both compose spellings.

    ``depends_on`` is either a list of names (short form) or a mapping of name ->
    ``{condition: ...}`` (long form). Reading only the mapping form would let a fragment
    disarm this whole file by switching spellings.
    """
    declared = body.get("depends_on") or {}
    if isinstance(declared, dict):
        return {str(name) for name in declared}
    return {str(name) for name in declared}


def _healthcheck_test(body: dict[str, Any]) -> str | None:
    """The healthcheck's ``test`` flattened to one string, or ``None`` if there is none.

    ``test`` is either a list (``["CMD", ...]`` / ``["CMD-SHELL", ...]``) or a bare string.
    Both are flattened, because every question these tests ask is "does this command
    mention X" and neither form should be able to hide an answer.
    """
    healthcheck = body.get("healthcheck") or {}
    test = healthcheck.get("test")
    if test is None:
        return None
    if isinstance(test, str):
        return test
    return " ".join(str(token) for token in test)


def _dockerfile_cmd(path: Path) -> list[str]:
    """The tokens of ``path``'s last exec-form ``CMD [...]``.

    Exec form only, and deliberately so: the shell form (``CMD uvicorn ...``) runs the
    command under ``/bin/sh -c``, which is what stops SIGTERM from reaching uvicorn, and no
    Dockerfile in this repo uses it. Line continuations are folded first so a ``CMD`` split
    across lines still parses rather than silently reading as "no CMD".
    """
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    matches = re.findall(r"^CMD\s+(\[.*\])\s*$", text, flags=re.MULTILINE)
    assert matches, (
        f"{_rel(path)} declares no exec-form `CMD [...]`, so there is nothing for "
        f"{_rel(path.parent)}/compose.yaml's `command:` to mirror. Give it "
        f'`CMD ["uvicorn", ...]`.'
    )
    return [str(token) for token in json.loads(matches[-1])]


# --------------------------------------------------------------------------------------
# 1-4: what compose declares
# --------------------------------------------------------------------------------------


def test_no_compose_file_interpolates_the_role_password_into_a_dsn() -> None:
    """Catches the reintroduction of ``postgresql://user:${PROXYSHOP_ROLE_PASSWORD:-}@...``.

    That interpolation is what made the documented ``cp .env.example .env`` path produce a
    stack that could not serve: ``.env.example`` says to leave the variable unset, an unset
    variable interpolates to nothing, and the resulting
    ``postgresql://trust_rw:@postgres:5432/proxyshop_w1`` carries an *explicitly empty*
    password. libpq refuses it locally with ``fe_sendauth: no password supplied`` -- it
    never reaches the server, so no amount of correct seeding on the other side helps.

    Only the password *position* of a URI is forbidden. Forwarding the variable as a plain
    environment entry (``PROXYSHOP_ROLE_PASSWORD: "${PROXYSHOP_ROLE_PASSWORD:-}"``) is the
    supported thing to do and must not be flagged: the container needs the variable so
    ``role_dsn`` can read it.
    """
    offenders: list[str] = []
    for path in [ROOT_COMPOSE, *_fragment_paths()]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ROLE_PASSWORD_IN_DSN in line:
                offenders.append(f"{_rel(path)}:{number}: {line.strip()}")
    assert not offenders, (
        "a compose file interpolates PROXYSHOP_ROLE_PASSWORD into the password position of "
        "a DSN. Unset, it expands to nothing and libpq rejects the connection with "
        "`fe_sendauth: no password supplied`. Drop the password from the DSN template and "
        "let `service_launch serve` resolve it through postgres.role_dsn:\n  "
        + "\n  ".join(offenders)
    )


def test_every_declared_datastore_dependency_has_a_readiness_check_that_names_it() -> None:
    """Catches a probe drifting back to pure liveness while the dependency stays declared.

    A ``depends_on: postgres`` says "this container cannot serve until Postgres can". A
    healthcheck that does not check Postgres says "healthy" the moment uvicorn answers.
    Holding both is the healthy-but-503 defect exactly: compose waits for the datastore to
    be *up*, then reports the service ready without ever asking whether the service can
    *reach* it with its own credentials.

    The pairing is enforced in one direction only -- declared dependency implies probe --
    because the reverse would push fragments into probing datastores they never touch,
    which is a green light with nothing behind it.
    """
    offenders: list[str] = []
    for path, name, body in _all_fragment_services():
        test = _healthcheck_test(body)
        if test is None:
            continue
        for datastore in sorted(_depends_on(body) & DATASTORE_FLAG.keys()):
            flag = DATASTORE_FLAG[datastore]
            if flag not in test:
                offenders.append(
                    f"{_rel(path)}: service {name!r} declares `depends_on: {datastore}` but "
                    f"its healthcheck never passes {flag}, so it reports healthy without "
                    f"having reached {datastore}. Either add {flag} to the "
                    f"`service_launch ready` line or drop the unused `depends_on: "
                    f"{datastore}` -- the dependency and its probe belong together."
                )
    assert not offenders, "\n".join(offenders)


def test_no_datastore_backed_service_ships_a_bare_openapi_healthcheck() -> None:
    """Catches the original defect verbatim: ``/openapi.json`` as a service's whole probe.

    ``/openapi.json`` proves the ASGI app imported and every discovered router mounted. It
    is a real check and it is kept inside ``service_launch ready`` -- but on its own, for a
    service that depends on a datastore, it is the check that reported ``(healthy)`` over
    ``503 store_unavailable``.

    Scoped to services that declare a datastore ``depends_on``, because that is the set for
    which HTTP-only is provably insufficient. The two services that legitimately have no
    such dependency are named in :data:`HTTP_ONLY_EXEMPT` rather than skipped.
    """
    offenders: list[str] = []
    for path, name, body in _all_fragment_services():
        test = _healthcheck_test(body)
        if test is None or name in HTTP_ONLY_EXEMPT:
            continue
        if not (_depends_on(body) & DATASTORE_FLAG.keys()):
            continue
        probes_a_datastore = any(flag in test for flag in DATASTORE_FLAG.values())
        if "/openapi.json" in test and not probes_a_datastore:
            offenders.append(
                f"{_rel(path)}: service {name!r} depends on a datastore but its healthcheck "
                f"is an /openapi.json fetch and nothing else, which proves only that the "
                f"ASGI app imported. Replace it with `python -m "
                f"proxyshop_support.service_launch ready --http-port <port> "
                f"--postgres <role>:<schema>.<table>` (see apps/trust/compose.yaml). "
                f"Current test: {test}"
            )
    assert not offenders, "\n".join(offenders)


def test_the_http_only_healthcheck_exemptions_are_still_earned() -> None:
    """Catches an exemption outliving its reason -- a silent hole in the test above.

    ``HTTP_ONLY_EXEMPT`` is an escape hatch, and an escape hatch nobody re-checks is how a
    service that has since grown a database keeps a liveness-only probe forever. Each entry
    states a premise; this asserts the premise is still true.
    """
    services = {name: (path, body) for path, name, body in _all_fragment_services()}
    for name, reason in HTTP_ONLY_EXEMPT.items():
        assert name in services, (
            f"{name!r} is exempted from the readiness-probe rule but no included compose "
            f"fragment declares a service by that name. Delete the stale exemption from "
            f"HTTP_ONLY_EXEMPT in {_rel(Path(__file__))}."
        )
        path, body = services[name]
        datastores = _depends_on(body) & DATASTORE_FLAG.keys()
        assert not datastores, (
            f"{_rel(path)}: service {name!r} now declares `depends_on: "
            f"{', '.join(sorted(datastores))}`, so its exemption ({reason}) is no longer "
            f"true. Give it a `service_launch ready` healthcheck naming that datastore and "
            f"remove it from HTTP_ONLY_EXEMPT."
        )
    stub = services["shopify-stub"][1]
    stub_test = _healthcheck_test(stub) or ""
    assert "/healthz" in stub_test, (
        "services/shopify-stub/compose.yaml is exempted because it probes its own real "
        f"/healthz route, and it no longer does. Its healthcheck is now: {stub_test}"
    )


@pytest.mark.parametrize("service", sorted(MIRRORED_COMMANDS))
def test_the_compose_command_mirrors_the_image_cmd(service: str) -> None:
    """Catches the duplicated uvicorn line drifting away from the Dockerfile's ``CMD``.

    Compose clears the image's ``CMD`` whenever a fragment sets ``command:``, so wrapping
    the entrypoint in ``service_launch serve`` forces every one of these fragments to repeat
    its image's uvicorn invocation. Two copies of one command, in two files, with nothing
    connecting them: change the port or the app path in the Dockerfile and the deployed
    container keeps running the stale copy -- and, because the fragment also publishes the
    old port, the symptom is a container that starts and answers nothing.

    Asserts both halves: the command *begins* with the ``serve --`` sentinel (so the DSNs
    are actually resolved before the service starts) and *ends* with exactly the
    Dockerfile's ``CMD`` tokens (so the copy cannot drift).
    """
    fragment_name, dockerfile_name = MIRRORED_COMMANDS[service]
    fragment = REPO_ROOT / fragment_name
    dockerfile = REPO_ROOT / dockerfile_name

    body = _services(fragment).get(service)
    assert body is not None, (
        f"{fragment_name} no longer declares a service named {service!r}; "
        f"MIRRORED_COMMANDS in {_rel(Path(__file__))} needs updating."
    )
    command = body.get("command")
    assert isinstance(command, list), (
        f"{fragment_name}: service {service!r} must declare `command:` as a LIST so the "
        f"exec form is preserved and SIGTERM reaches uvicorn; got {command!r}."
    )
    command = [str(token) for token in command]

    assert command[: len(SERVE_PREFIX)] == SERVE_PREFIX, (
        f"{fragment_name}: service {service!r} does not start with {SERVE_PREFIX}, so the "
        f"PROXYSHOP_PG_DSN_* variables are never resolved through postgres.role_dsn and the "
        f"container starts with a password-free DSN. Got: {command}"
    )

    cmd = _dockerfile_cmd(dockerfile)
    tail = command[-len(cmd) :] if cmd else []
    assert tail == cmd, (
        f"{fragment_name}: service {service!r}'s `command:` no longer ends with "
        f"{dockerfile_name}'s CMD. Compose CLEARS the image CMD when `command:` is set, so "
        f"the two are one command written twice and the deployed container runs the compose "
        f"copy.\n  Dockerfile CMD: {cmd}\n  compose tail:   {tail}"
    )


# --------------------------------------------------------------------------------------
# 5-6: serve -- the credential the container could not resolve
# --------------------------------------------------------------------------------------


@pytest.fixture
def isolated_dsn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hermetic environment for the ``serve`` tests: worker 3, no DSNs, no role password.

    Every ``PROXYSHOP_PG_DSN_*`` is cleared so that "this variable stayed absent" is a real
    measurement rather than an accident of whatever the developer's shell exported.
    """
    for env_var, _user, _password in ROLES.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv(ROLE_PASSWORD_ENV, raising=False)
    monkeypatch.setenv("PROXYSHOP_WORKER", "3")


@pytest.mark.usefixtures("isolated_dsn_env")
def test_resolved_dsn_env_supplies_a_password_when_the_role_password_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the exact deployed failure: a DSN template that reaches libpq password-less.

    This is the unit-level statement of the ``fe_sendauth: no password supplied`` crash.
    Compose hands the container a password-free template on purpose; ``serve`` is what turns
    it into something that can authenticate, using the one resolver on the connect side
    (``$PROXYSHOP_ROLE_PASSWORD`` > the password already in the DSN > ``DEV_ROLE_PASSWORD``).

    It also pins the D38 half: the database component is rewritten to *this worker's*
    database, so a fragment's ``proxyshop_w1`` default cannot make worker 3 share a database
    with worker 1. And it pins the restraint -- a role whose variable compose did not set
    stays unset, because inventing one would hand a service a credential its fragment
    deliberately withheld.
    """
    monkeypatch.setenv(
        "PROXYSHOP_PG_DSN_TRUST_RW",
        "postgresql://trust_rw@postgres:5432/proxyshop_w1",
    )

    resolved = resolved_dsn_env()

    assert "PROXYSHOP_PG_DSN_TRUST_RW" in resolved, (
        "resolved_dsn_env() dropped PROXYSHOP_PG_DSN_TRUST_RW, which compose had set; the "
        "container would exec with the unresolved template still in its environment."
    )
    parsed = conninfo_to_dict(resolved["PROXYSHOP_PG_DSN_TRUST_RW"])
    assert parsed.get("password") == DEV_ROLE_PASSWORD, (
        f"resolved_dsn_env() produced "
        f"{resolved['PROXYSHOP_PG_DSN_TRUST_RW']!r}, whose password is "
        f"{parsed.get('password')!r} and not the resolver's fallback "
        f"{DEV_ROLE_PASSWORD!r}. An empty or missing password here is the deployed defect: "
        f"libpq answers `fe_sendauth: no password supplied` before reaching the server."
    )
    assert parsed.get("dbname") == "proxyshop_w3", (
        f"resolved_dsn_env() left the database as {parsed.get('dbname')!r} while "
        f"PROXYSHOP_WORKER=3. D38 requires proxyshop_w3; a fragment's `proxyshop_w1` "
        f"default must not survive into another worker's container."
    )
    assert "PROXYSHOP_PG_DSN_EXCHANGE" not in resolved, (
        "resolved_dsn_env() invented PROXYSHOP_PG_DSN_EXCHANGE, which compose never set. A "
        "variable that is absent must stay absent: adding one hands the service a role its "
        "fragment deliberately did not grant it."
    )


@pytest.mark.usefixtures("isolated_dsn_env")
def test_resolved_dsn_env_honours_an_explicitly_set_role_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches ``serve`` overwriting an operator's password with the dev default.

    ``$PROXYSHOP_ROLE_PASSWORD`` is what *created* the roles (``db/init/00-roles.sql``
    seeds from it), so on the connect side nothing may be more authoritative. If ``serve``
    ignored it, setting the variable would produce a cluster the application could no longer
    reach -- the T-112 defect, one layer down, and it would only show up on a deployment
    that had bothered to change the password.
    """
    monkeypatch.setenv(
        "PROXYSHOP_PG_DSN_TRUST_RW",
        "postgresql://trust_rw@postgres:5432/proxyshop_w1",
    )
    monkeypatch.setenv(ROLE_PASSWORD_ENV, "operator-set-role-password")

    parsed = conninfo_to_dict(resolved_dsn_env()["PROXYSHOP_PG_DSN_TRUST_RW"])

    assert parsed.get("password") == "operator-set-role-password", (
        f"{ROLE_PASSWORD_ENV} was set and resolved_dsn_env() used "
        f"{parsed.get('password')!r} instead. That variable seeded the role, so it must win "
        f"over every other source."
    )
    assert parsed.get("password") != DEV_ROLE_PASSWORD, (
        "resolved_dsn_env() fell back to DEV_ROLE_PASSWORD even though "
        f"{ROLE_PASSWORD_ENV} was set, which would authenticate as the wrong credential."
    )


# --------------------------------------------------------------------------------------
# 7-8: ready -- the probe fails closed
# --------------------------------------------------------------------------------------


def test_check_env_raises_not_ready_naming_the_missing_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a probe that fails without saying which setting is missing.

    The message is not decoration: it is the entire content of ``docker inspect``'s last
    health log entry, and it is what turns "the merchant container is unhealthy" into a
    one-line fix. ``MERCHANT_ADMIN_TOKEN`` is the measured case -- unset, every
    administrative route answers ``503 admin-api-not-configured`` from a healthy container.
    """
    monkeypatch.delenv("MERCHANT_ADMIN_TOKEN", raising=False)

    with pytest.raises(NotReady) as raised:
        check_env(["MERCHANT_ADMIN_TOKEN"])

    assert "MERCHANT_ADMIN_TOKEN" in str(raised.value), (
        f"check_env raised NotReady({str(raised.value)!r}) without naming the variable that "
        f"is missing, so the operator sees an unhealthy container and no cause."
    )


def test_check_env_passes_when_the_variable_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches an env check that can never pass -- a probe stuck red is also a broken probe.

    A container that never reports healthy blocks every ``depends_on: service_healthy`` in
    the stack, so an over-strict check takes the whole deployment down rather than one
    service.
    """
    monkeypatch.setenv("MERCHANT_ADMIN_TOKEN", "dev-merchant-admin-token")

    check_env(["MERCHANT_ADMIN_TOKEN"])  # must not raise


def test_ready_propagates_a_failed_http_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches ``ready()`` swallowing a check failure and reporting healthy anyway.

    Failing closed is the whole premise. If ``ready`` caught :class:`NotReady` and returned
    normally, every fragment in the stack would be back to the ``/openapi.json`` era with
    more code -- and the failure would be invisible, because the exit status is all compose
    ever looks at.
    """

    def _refuse(port: int, path: str = "/openapi.json") -> None:
        raise NotReady(f"http://127.0.0.1:{port}{path} did not answer: injected")

    monkeypatch.setattr(service_launch, "check_http", _refuse)

    with pytest.raises(NotReady) as raised:
        ready(8084)

    assert "injected" in str(raised.value), (
        f"ready() replaced the failing check's message with {str(raised.value)!r}; the "
        f"operator needs the original cause, which is all docker inspect shows."
    )


def test_main_reports_not_ready_and_exits_nonzero_when_required_config_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catches the probe reporting success on a service that cannot serve a single request.

    This is the end-to-end shape of the healthcheck as compose runs it: an argv, an exit
    status, a line of output. A non-zero status is what makes the container ``unhealthy``;
    the ``NOT READY:`` line is what tells the operator why. Both are pinned, because a probe
    that returns 0 here is precisely the ``(healthy)``-over-503 report this file exists to
    prevent.

    ``check_http`` is stubbed to a no-op so the test measures the ``--require-env`` gate and
    nothing else -- no socket is opened, so this stays a pure unit test.
    """
    missing = "PROXYSHOP_DEPLOY_READINESS_UNSET_VAR"
    monkeypatch.delenv(missing, raising=False)
    monkeypatch.setattr(service_launch, "check_http", lambda port, path="/openapi.json": None)

    exit_code = service_launch.main(["ready", "--http-port", "8082", "--require-env", missing])

    assert exit_code == 1, (
        f"`service_launch ready --require-env {missing}` returned {exit_code} with the "
        f"variable unset. Compose reads the exit status and nothing else, so anything but a "
        f"non-zero code reports the container healthy."
    )
    captured = capsys.readouterr()
    reported = [line for line in captured.err.splitlines() if line.startswith("NOT READY:")]
    assert reported, (
        f"`service_launch ready` failed without printing a line starting with 'NOT READY:'. "
        f"That line is the whole of the operator's diagnostic in `docker inspect`.\n"
        f"  stdout: {captured.out!r}\n  stderr: {captured.err!r}"
    )
    assert missing in reported[0], (
        f"the NOT READY line ({reported[0]!r}) does not name {missing}, so it says the "
        f"container is broken without saying what to set."
    )


# --------------------------------------------------------------------------------------
# 11-13: ready over real TCP -- the probe observed passing AND observed failing
#
# Every other test in this file reads a file or calls a function with a patched
# environment. These three are the only ones that put a real socket between the probe and
# the thing it probes, and they exist because a health check that has never been WATCHED
# failing is indistinguishable from one that cannot fail -- which is precisely what
# `/openapi.json` was. `proxyshop_support.asgi_server.serve` binds port 0 and reports the
# real port (D40: no hard-coded ports in test code), and the pytest socket guard permits
# loopback, so this stays inside the offline suite: no docker, no network, no marker.
# --------------------------------------------------------------------------------------


@pytest.fixture
def live_http_port() -> Iterator[int]:
    """A real uvicorn serving a bare FastAPI app on an ephemeral loopback port.

    A bare ``FastAPI()`` is the whole fixture on purpose: FastAPI generates
    ``/openapi.json`` itself, which is the exact path :func:`check_http` fetches, so this
    reproduces what the probe meets in a deployed container without importing any service.
    """
    from fastapi import FastAPI

    with serve_asgi(FastAPI()) as base_url:
        port = urlsplit(base_url).port
        assert port is not None, f"asgi_server.serve() yielded a portless URL: {base_url!r}"
        yield port


@pytest.fixture
def dead_http_port() -> int:
    """A loopback port with nothing listening on it.

    Bound to port 0 to have the OS pick one that was free, then closed immediately, so the
    number is real and a connection to it is refused rather than hanging.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_check_http_passes_against_a_real_listening_server(live_http_port: int) -> None:
    """Catches a probe that is red against a healthy container -- the deployment-wide stall.

    Every ``depends_on: service_healthy`` in the stack waits on this check, so a probe that
    cannot go green does not fail one service, it fails the whole ``docker compose up``.
    This is the only test in the repo that answers "does check_http succeed against an
    actual HTTP server" rather than against a stub of one: a real socket, a real uvicorn, a
    real ``GET /openapi.json``, a real 200.
    """
    check_http(live_http_port)  # must not raise


def test_check_http_fails_against_a_port_nothing_is_listening_on(dead_http_port: int) -> None:
    """Catches the failure this whole module exists to prevent: a check that cannot fail.

    ``(healthy)`` over ``503`` is what a probe that always succeeds looks like. Watching
    :func:`check_http` actually raise against a refused connection is the evidence that its
    green means something -- without it, every other assertion in this file is pinning the
    shape of a check nobody has seen say no.

    The message is asserted too, because it is the entire content of ``docker inspect``'s
    health log: a ``NotReady`` that does not name the URL leaves the operator guessing which
    of the container's ports is wrong.
    """
    with pytest.raises(NotReady) as raised:
        check_http(dead_http_port)

    message = str(raised.value)
    assert f"127.0.0.1:{dead_http_port}" in message, (
        f"check_http raised NotReady({message!r}) without naming the URL it could not "
        f"reach, so `docker inspect` shows an unhealthy container and no address."
    )


def test_ready_reports_only_the_checks_it_actually_ran(live_http_port: int) -> None:
    """Catches ``ready()`` claiming credit for datastore checks it never performed.

    The return value is the ``READY: ...`` line an operator reads to see what was verified.
    If it listed checks that were not configured -- or that were configured and skipped --
    it would recreate the original defect in the report rather than in the probe: a healthy
    line naming a database nothing connected to. With no ``--postgres``/``--redis``/
    ``--neo4j``, the honest answer is exactly ``["http"]`` and nothing more.
    """
    assert ready(live_http_port) == ["http"], (
        "ready() with no datastore checks configured must report exactly ['http'] -- the "
        "one check it ran. Anything else names a check that did not happen."
    )


# --------------------------------------------------------------------------------------
# 9-10: the documented configuration
# --------------------------------------------------------------------------------------


def test_the_store_window_tokens_are_documented_forwarded_and_shipped() -> None:
    """The same hole as the merchant admin token, in a fourth feature, caught the same way.

    ``PROXYSHOP_BUYER_STORE_WINDOW_TOKENS`` opens ``GET /buyer/store-window`` — the
    store-visible, k-anonymised view of the buyer population that T-142 built. Measured before
    this gate existed: the variable appeared in exactly THREE files repo-wide (the route that
    reads it, the OpenAPI document, and the README), was forwarded by **0 of 17 services
    across 10 compose fragments**, and was named in no runbook, script or Makefile target. The
    served route answered ``503 store-window-not-configured`` in every deployment that has
    ever run, from a container reporting ``(healthy)``.

    Two links, not three, and the missing one is deliberate. Documented in ``.env.example`` so
    ``cp .env.example .env`` produces a stack where the door works, and forwarded by the
    fragment so the container can see it — but **no healthcheck clause**, because unlike the
    merchant's administrative API this window is OPTIONAL. A deployment that blanks it is
    making a legitimate choice and must stay healthy while serving nobody through it. Requiring
    it would turn "we do not expose the buyer window" into a red container.

    The third assertion is the one the merchant twin does not need: the compose default names
    a FILE, and a default naming a file that is not there is the same dead feature wearing a
    path. It must exist and it must parse as a ``{store_id: token}`` mapping.
    """
    import json as _json

    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(
        r"^PROXYSHOP_BUYER_STORE_WINDOW_TOKENS=.+$", env_example, flags=re.MULTILINE
    ), (
        f"{_rel(ENV_EXAMPLE)} does not assign PROXYSHOP_BUYER_STORE_WINDOW_TOKENS a value, so "
        f"`cp .env.example .env` produces a buyer whose store window answers 503 "
        f"store-window-not-configured to every store."
    )

    fragment = REPO_ROOT / "apps" / "buyer" / "compose.yaml"
    buyer = _services(fragment).get("buyer-svc")
    assert buyer is not None, f"{_rel(fragment)} no longer declares a `buyer-svc` service"
    environment = buyer.get("environment") or {}
    assert "PROXYSHOP_BUYER_STORE_WINDOW_TOKENS" in environment, (
        f"{_rel(fragment)}'s buyer-svc does not forward "
        f"PROXYSHOP_BUYER_STORE_WINDOW_TOKENS, so the container cannot see it however the "
        f"operator sets it — which is how this route came to be unreachable in every "
        f"deployment while its tests passed."
    )

    declared = str(environment["PROXYSHOP_BUYER_STORE_WINDOW_TOKENS"])
    _, _, default = declared.partition(":-")
    default = default.rstrip("}").strip()
    assert default, (
        f"{_rel(fragment)} forwards PROXYSHOP_BUYER_STORE_WINDOW_TOKENS with no default, so "
        f"the shipped demo opens the window for nobody."
    )
    shipped = REPO_ROOT / "deploy" / "demo" / Path(default).name
    assert shipped.is_file(), (
        f"{_rel(fragment)} defaults the store-window tokens to {default!r}, and "
        f"{_rel(shipped)} is not in the tree. A default naming a file that does not exist is "
        f"the same dead feature wearing a path."
    )
    mapping = _json.loads(shipped.read_text(encoding="utf-8"))
    assert isinstance(mapping, dict) and mapping, (
        f"{_rel(shipped)} does not parse as a non-empty store_id -> token mapping."
    )
    assert all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in mapping.items()), (
        f"{_rel(shipped)} carries an entry that is not a string store id mapped to a string "
        f"token; the route reads it as exactly that."
    )


def test_the_merchant_admin_token_is_documented_forwarded_and_required() -> None:
    """Catches the setting that existed only in the source, making the admin API dead.

    ``MERCHANT_ADMIN_TOKEN`` appeared in NO compose fragment and NO ``.env.example``, while
    ``install/routes.py`` reads it with no default and no fallback -- deliberately, so an
    unset value refuses every caller. The measured result was that discount-code minting and
    every ``/install/shops``, ``/stores/{id}/envelope`` and ``/stores/{id}/kill`` call was
    unreachable in every deployment::

        $ curl -s localhost:8082/install/shops
        {"error":"admin-api-not-configured","missing":["MERCHANT_ADMIN_TOKEN"], ...}   503

    ...answered by a container reporting ``(healthy)``.

    All three links are checked, because any one of them alone leaves the hole open:
    documented in ``.env.example`` (so ``cp .env.example .env`` produces a working stack),
    forwarded by the fragment (so the container can see it), and named by the healthcheck
    (so a deployment that loses it goes unhealthy instead of silently 503-ing).
    """
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(r"^MERCHANT_ADMIN_TOKEN=.+$", env_example, flags=re.MULTILINE), (
        f"{_rel(ENV_EXAMPLE)} does not assign MERCHANT_ADMIN_TOKEN a value, so "
        f"`cp .env.example .env` produces a merchant whose administrative routes answer "
        f"503 admin-api-not-configured to every caller."
    )

    fragment = REPO_ROOT / "apps" / "merchant" / "compose.yaml"
    merchant = _services(fragment).get("merchant-svc")
    assert merchant is not None, f"{_rel(fragment)} no longer declares a `merchant-svc` service"

    environment = merchant.get("environment") or {}
    names = (
        set(environment)
        if isinstance(environment, dict)
        else {str(entry).split("=", 1)[0] for entry in environment}
    )
    assert "MERCHANT_ADMIN_TOKEN" in names, (
        f"{_rel(fragment)}: merchant-svc does not forward MERCHANT_ADMIN_TOKEN into the "
        f"container, so install/routes.py reads nothing however the host is configured."
    )

    test = _healthcheck_test(merchant) or ""
    assert "--require-env" in test and "MERCHANT_ADMIN_TOKEN" in test, (
        f"{_rel(fragment)}: merchant-svc's healthcheck does not require "
        f"MERCHANT_ADMIN_TOKEN, so a deployment that loses the token reports healthy while "
        f"its whole admin surface answers 503. Current test: {test}"
    )


def test_env_example_does_not_prescribe_the_reserved_worker_zero() -> None:
    """Catches the shipped default pointing every deployment at the measurement worker.

    Worker 0 is reserved for this project's scored ``build_succeeds`` run (see
    ``scripts/db_reclaim.py``). Every worker owns its own Postgres database, Redis logical
    DB and key prefix (D38), so a demo or a deployment that takes worker 0 writes into the
    database the project is graded on. ``.env.example`` is copied verbatim by the documented
    setup path, which makes its value the default for everyone who follows the runbook.
    """
    offenders = [
        f"{_rel(ENV_EXAMPLE)}:{number}: {line}"
        for number, line in enumerate(ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(), 1)
        if re.match(r"^PROXYSHOP_WORKER=0\s*$", line)
    ]
    assert not offenders, (
        "`.env.example` assigns the RESERVED worker 0, which `cp .env.example .env` then "
        "makes every operator's default. Worker 0 runs the frozen build_succeeds metric; a "
        "deployment that takes it corrupts the measurement. Use PROXYSHOP_WORKER=1.\n  "
        + "\n  ".join(offenders)
    )


def test_the_starting_slice_runbook_does_not_prescribe_the_reserved_worker_zero() -> None:
    """Catches the runbook telling an operator to run the demo on the measurement worker.

    ``.env.example`` states that an earlier revision of this runbook said
    ``export PROXYSHOP_WORKER=0`` and that "the runbook was the side that was wrong". That
    claim is only true while the runbook's own commands say something else -- and the
    runbook is the copy a human pastes into a shell, so it is the copy that decides which
    database gets written. Anyone following it lands on worker 0 and overwrites the frozen
    ``build_succeeds`` measurement's database, whatever ``.env.example`` says.

    Only **fenced code blocks** are read, because only those are pasted. The surrounding
    prose is allowed -- indeed required -- to quote the old value while explaining why it
    changed, and a matcher that could not tell an instruction from a description of one
    would fail on the very edit that fixes the defect.
    """
    offenders: list[str] = []
    in_code_block = False
    for number, line in enumerate(STARTING_SLICE.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block and line.strip().startswith("export PROXYSHOP_WORKER=0"):
            offenders.append(f"{_rel(STARTING_SLICE)}:{number}: {line.strip()}")
    assert not offenders, (
        "docs/demo/starting-slice.md tells the reader to `export PROXYSHOP_WORKER=0`, the "
        "worker reserved for the scored build_succeeds run, and .env.example already claims "
        "this runbook was corrected. Change the export to PROXYSHOP_WORKER=1 so the two "
        "agree and the demo stops writing into the measurement worker's database.\n  "
        + "\n  ".join(offenders)
    )
