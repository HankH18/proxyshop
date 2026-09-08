"""``ingest.graph.graph_driver`` resolves its credential in one place and says where from.

``graph_driver`` is not only the CLI's connector. ``ingest.scheduler.catalog.graph_session``
calls it, and that is the default ``session_factory`` of ``CatalogRefreshRunner`` — the module
state behind ``POST /refresh/{store_id}`` and ``POST /schedule/tick``. So the three lines it
used to hold::

    os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    os.environ.get("NEO4J_USER", "neo4j")
    os.environ.get("NEO4J_PASSWORD", "proxyshop_dev_pw")

were a served path inventing a credential, and its docstring's claim that "the fallbacks here
match ``.env.example``" was true of this function and false of the tree: the readiness probe
that decides whether the ingest container is healthy defaulted the same password to ``""``.

Both facts are pinned here — the resolution goes through
:func:`proxyshop_support.neo4j_auth.graph_credentials`, and a refused credential leaves an
operator a line naming where it came from and never the password itself.

Nothing here opens a bolt session; ``neo4j.GraphDatabase.driver`` is replaced.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from ingest.graph.reembed import graph_driver

from proxyshop_support import neo4j_auth
from proxyshop_support.neo4j_auth import (
    DEV_PASSWORD,
    ENV_PASSWORD,
    ENV_URI,
    ENV_USER,
    GraphCredentials,
)

#: A credential no environment can produce. It is what tells "this function calls the shared
#: resolver" apart from "this function happens to read the same three variables the resolver
#: reads" — and the second is what the pre-repair code did, with a password default of its own.
SENTINEL = GraphCredentials(
    uri="bolt://sentinel:1",
    user="sentinel-user",
    password="sentinel-password",
    source="a test",
    is_development_default=False,
)


class _Driver:
    """Records what it was constructed with; connects to nothing."""

    def __init__(self, uri: str, *, auth: Any = None, refuse: bool = False, **_: Any) -> None:
        self.uri = uri
        self.auth = auth
        self.refuse = refuse
        self.closed = False

    def verify_connectivity(self) -> None:
        if self.refuse:
            raise RuntimeError("Neo.ClientError.Security.Unauthorized: the client is unauthorized")

    def close(self) -> None:
        self.closed = True


def _drivers(monkeypatch: pytest.MonkeyPatch, *, refuse: bool = False) -> list[_Driver]:
    built: list[_Driver] = []

    def factory(uri: str, *, auth: Any = None, **kwargs: Any) -> _Driver:
        driver = _Driver(uri, auth=auth, refuse=refuse, **kwargs)
        built.append(driver)
        return driver

    import neo4j

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", staticmethod(factory))
    return built


@pytest.fixture(autouse=True)
def _stated_environment_only(monkeypatch: pytest.MonkeyPatch) -> Any:
    """No ambient ``NEO4J_*``, and each test gets the resolver's first-warning back."""
    for name in (ENV_URI, ENV_USER, ENV_PASSWORD):
        monkeypatch.delenv(name, raising=False)
    neo4j_auth._WARNED.clear()
    yield
    neo4j_auth._WARNED.clear()


def test_the_driver_is_built_from_the_shared_resolver_and_not_from_its_own_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stated environment reaches the driver verbatim, **and it goes through the resolver.**

    Two phases, because the first one alone grades nothing. A stated ``NEO4J_PASSWORD`` is the
    case the pre-repair code got right — it read that same variable, with its own default
    behind it — so the environment half of this node survives a complete revert of the repair.
    Measured.

    Phase two replaces :func:`proxyshop_support.neo4j_auth.graph_credentials` with a function
    returning a credential no environment can produce. ``graph_driver`` imports the resolver
    INSIDE its body, so that is the binding it resolves at call time and the one to patch; a
    function that read the three variables for itself would answer with the environment set
    above and never see the sentinel at all.
    """
    monkeypatch.setenv(ENV_URI, "bolt://ingest-graph:7687")
    monkeypatch.setenv(ENV_USER, "loader")
    monkeypatch.setenv(ENV_PASSWORD, "stated-by-the-operator")
    built = _drivers(monkeypatch)

    with graph_driver() as driver:
        assert driver is built[0]

    assert built[0].uri == "bolt://ingest-graph:7687"
    assert built[0].auth == ("loader", "stated-by-the-operator")
    assert built[0].closed is True, "the context manager did not close the driver"

    monkeypatch.setattr(neo4j_auth, "graph_credentials", lambda env=None: SENTINEL)

    with graph_driver():
        pass

    assert len(built) == 2, f"the second call built {len(built) - 1} driver(s)"
    assert (built[1].uri, built[1].auth) == (SENTINEL.uri, SENTINEL.auth), (
        f"graph_driver built its driver with {(built[1].uri, built[1].auth)} while the one "
        f"resolver was answering {(SENTINEL.uri, SENTINEL.auth)}. It is resolving its own "
        f"credential — the environment above is what it answered with, which is exactly the "
        f"pre-repair shape: three call sites reading the same three variables and defaulting "
        f"three different ways behind them, one of which was the readiness probe"
    )


def test_with_nothing_set_it_uses_the_one_documented_development_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The dev default survives — it has to, CI sets no ``NEO4J_*`` — but it is not silent."""
    built = _drivers(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=neo4j_auth.__name__), graph_driver():
        pass

    assert built[0].auth == ("neo4j", DEV_PASSWORD), (
        "the CLI and the scheduler's refresh path no longer authenticate with the pair "
        "docker-compose.yml seeds the local server with; the graph suite will start SKIPPING"
    )
    warned = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert any(ENV_PASSWORD in message for message in warned), (
        f"nothing warned that an unconfigured development credential was used: {warned}"
    )
    assert not any(DEV_PASSWORD in message for message in warned), "the password was logged"


def test_a_refused_credential_leaves_an_operator_the_source_and_never_the_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """What an operator sees when the password is WRONG.

    ``Neo.ClientError.Security.Unauthorized`` says the server refused; it does not say what was
    offered. The added line does, and it distinguishes the two mistakes an operator can have
    made — set the variable to the wrong value, or never set it and inherit the development
    default in production.
    """
    monkeypatch.setenv(ENV_URI, "bolt://prod-graph:7687")
    _drivers(monkeypatch, refuse=True)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as refused:
        with graph_driver():
            pass

    assert "Unauthorized" in str(refused.value), (
        "the driver's own refusal was swallowed or its type replaced; a caller catching "
        "neo4j.exceptions.AuthError must still catch it"
    )
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "bolt://prod-graph:7687" in logged
    assert ENV_PASSWORD in logged, f"the failure log names no credential source: {logged!r}"
    assert "development default" in logged, (
        f"the failure log does not say the credential was the unconfigured default: {logged!r}"
    )
    assert DEV_PASSWORD not in logged, f"the failure log printed the password: {logged!r}"


def test_the_driver_is_closed_even_when_the_credential_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused connection must not leak a connection pool per failed refresh."""
    built = _drivers(monkeypatch, refuse=True)

    with pytest.raises(RuntimeError), graph_driver():
        pass

    assert built[0].closed is True, (
        "a driver whose credential was refused was left open; POST /refresh/{store_id} runs "
        "this on every scheduled tick"
    )


def test_a_stated_password_never_reaches_the_failure_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The other direction: a real deployment's secret is not printed when it is rejected."""
    monkeypatch.setenv(ENV_PASSWORD, "prod-secret-value")
    _drivers(monkeypatch, refuse=True)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError), graph_driver():
        pass

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "prod-secret-value" not in logged, f"the password rode into the log: {logged!r}"
    assert ENV_PASSWORD in logged
