"""ONE Neo4j credential, resolved in one place, offered by every path that connects.

The defect this file pins, measured on this tree before the repair. ``NEO4J_PASSWORD`` had
three code-side defaults and they disagreed::

    apps/exchange/src/retrieval/roster.py:519   os.environ.get("NEO4J_PASSWORD", "proxyshop_dev_pw")
    services/ingest/src/graph/reembed.py:549    os.environ.get("NEO4J_PASSWORD", "proxyshop_dev_pw")
    proxyshop_support/service_launch.py:251     os.environ.get("NEO4J_PASSWORD", "")

The first two are SERVED. ``graph_sessions_from_env`` is bound by ``exchange.composition`` for
both the graph roster and the catalogue snapshots, and ``apps/exchange/compose.yaml`` defaults
``EXCHANGE_SHOP_ROSTER=graph``, so it is on the path of every roster-less ``POST /auctions``;
``graph_driver`` is under ``ingest.scheduler.catalog.graph_session``, which is what
``POST /refresh/{store_id}`` runs on.

The third is the READINESS PROBE — the check that decides whether a container is reported
healthy. That is what makes the disagreement worse than a duplicated literal: a probe offering
a different credential from the code it vouches for is not a stricter or a looser check, it is
a check of something else. Against a server with auth disabled the empty password succeeds
where the served paths' does not; against a server seeded with the dev pair it is refused while
every served read works. Either way "healthy" and "serving" come apart with nothing in the logs
connecting them.

What is pinned here, in the order the sections run:

1. the resolver is the ONLY place in product code that supplies a ``NEO4J_PASSWORD`` default —
   swept with :mod:`ast`, and the sweep's own sensitivity is asserted against a corpus of
   seven reintroductions before it is trusted;
2. all three paths offer the SAME ``(user, password)`` for the same environment AND get it
   from the one resolver — driven, not inferred, by capturing what each one hands
   ``neo4j.GraphDatabase.driver``, first for a stated environment and then for a sentinel
   credential no environment can produce. The stated half alone is vacuous: the pre-repair
   code read those same variables, so a revert leaves it green. Plus the driver POOL's key,
   which has to include the password or a rotated deployment is served its refused driver
   forever;
3. what an operator SEES when the password is wrong or unset, and that no password is in any
   of it — including one the operator embedded in ``NEO4J_URI`` or ``NEO4J_USER`` themselves.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

import pytest
from exchange.retrieval import roster
from exchange.retrieval.roster import graph_sessions_from_env

from proxyshop_support import neo4j_auth, service_launch
from proxyshop_support.neo4j_auth import (
    DEV_PASSWORD,
    ENV_PASSWORD,
    ENV_URI,
    ENV_USER,
    MAX_REMEMBERED_CONNECTIONS,
    GraphCredentials,
    graph_credentials,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The tree this sweep walks: product code and shared runtime, every file that could open a
#: bolt session on behalf of a running service.
SWEPT_ROOTS = ("apps", "services", "packages", "proxyshop_support", "e2e", "scripts")

#: The one file allowed to name a password default.
#:
#: The repo-root ``conftest.py`` used to be exempted here too, as the test harness rather than a
#: served path, and was pinned by a node of its own that required its literal to AGREE with
#: :data:`~proxyshop_support.neo4j_auth.DEV_PASSWORD`. It folded onto ``graph_credentials()``, so
#: it needs no exemption and no agreement: there is no second literal left to drift. The node
#: that pinned it said in its own failure message to delete both when this happened, and
#: :func:`test_the_test_harness_resolves_through_the_resolver_like_everything_else` replaced it.
EXEMPT = (REPO_ROOT / "proxyshop_support" / "neo4j_auth.py",)

#: A sentinel credential no environment can produce, used to prove that a connector reads the
#: RESOLVER rather than merely reading the same environment variable the resolver reads.
SENTINEL = GraphCredentials(
    uri="bolt://sentinel:1",
    user="sentinel-user",
    password="sentinel-password",
    source="a test",
    is_development_default=False,
)

#: Seven realistic ways a lane could put a second ``NEO4J_PASSWORD`` default back into the
#: tree, and the corpus :func:`_password_defaults_in` is graded against. **Six of these seven
#: survived the substring predicate this sweep used to be** — measured, by running the old
#: predicate over this corpus. It required a double-quoted literal, a ``.get(`` and a comma all
#: on one physical line, so everything except the first row below walked straight through it. A
#: gate that detects nothing is the classic false green, so the corpus is asserted against
#: rather than trusted.
REINTRODUCTIONS: tuple[tuple[str, str], ...] = (
    ("double quotes", 'import os\nvalue = os.environ.get("NEO4J_PASSWORD", "pw")\n'),
    ("single quotes", "import os\nvalue = os.environ.get('NEO4J_PASSWORD', 'pw')\n"),
    ("os.getenv", 'import os\nvalue = os.getenv("NEO4J_PASSWORD", "pw")\n'),
    ("a bare getenv", 'from os import getenv\nvalue = getenv("NEO4J_PASSWORD", "pw")\n'),
    (
        "the resolver's own constant",
        "from proxyshop_support.neo4j_auth import ENV_PASSWORD\nvalue = env.get(ENV_PASSWORD, 'pw')\n",
    ),
    (
        "a call split over three lines",
        'import os\nvalue = os.environ.get(\n    "NEO4J_PASSWORD",\n    "pw",\n)\n',
    ),
    ("an `or` fallback", 'import os\nvalue = os.environ.get("NEO4J_PASSWORD") or "pw"\n'),
)

#: Shapes that must NOT be flagged. A sweep that answers "offender" to everything is exactly
#: as useless as one that answers it to nothing, and it is the failure mode that gets a real
#: repair reverted: the next lane deletes the noisy gate rather than the credential default.
INNOCENT: tuple[tuple[str, str], ...] = (
    (
        "the resolver being called",
        "from proxyshop_support.neo4j_auth import graph_credentials\nvalue = graph_credentials()\n",
    ),
    ("a URI default", 'import os\nvalue = os.environ.get("NEO4J_URI", "bolt://localhost:7687")\n'),
    ("a lookup with no fallback", 'import os\nvalue = os.environ["NEO4J_PASSWORD"]\n'),
    ("the name in a comment", "# never os.environ.get('NEO4J_PASSWORD', 'pw')\nvalue = 1\n"),
    ("the name in a string", 'value = "set NEO4J_PASSWORD, or accept the dev default"\n'),
)


def _reads_the_password(call: ast.Call) -> bool:
    """Is ``call`` a ``.get``/``getenv`` whose FIRST argument names ``NEO4J_PASSWORD``?

    Both spellings of the name count — the literal and the ``ENV_PASSWORD`` constant
    ``neo4j_auth`` publishes — because a lane that imported the resolver's vocabulary and then
    defaulted around it is *more* likely to reach for the constant, not less.
    """
    if not call.args:
        return False
    func = call.func
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        return False
    if name not in ("get", "getenv"):
        return False
    first = call.args[0]
    if isinstance(first, ast.Constant) and first.value == ENV_PASSWORD:
        return True
    return isinstance(first, ast.Name) and first.id == "ENV_PASSWORD"


def _password_defaults_in(source: str, label: str) -> list[str]:
    """Every ``NEO4J_PASSWORD`` lookup in ``source`` that carries a fallback. **Parsed.**

    Two shapes, which between them are what "supplies a default" means in Python:

    * a lookup with a second positional argument — ``get(name, fallback)``;
    * a one-argument lookup on the left of an ``or`` — ``get(name) or fallback``.

    Parsed rather than grepped because the quoting, the spelling of the name and the line
    breaks are all free variables, and a predicate over the source TEXT has to guess every one
    of them correctly. An unparseable file is reported rather than skipped: a file the sweep
    cannot read is a hole in the sweep, not a clean one.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{label}: could not be parsed, so it could not be swept ({exc})"]
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _reads_the_password(node) and len(node.args) >= 2:
            offenders.append(f"{label}:{node.lineno}: a {ENV_PASSWORD} lookup with a fallback")
        elif (
            isinstance(node, ast.BoolOp)
            and isinstance(node.op, ast.Or)
            and node.values
            and isinstance(node.values[0], ast.Call)
            and _reads_the_password(node.values[0])
        ):
            offenders.append(
                f"{label}:{node.lineno}: a {ENV_PASSWORD} lookup with an `or` fallback"
            )
    return offenders


def _password_default_argument(source: str) -> str | None:
    """The literal ``source`` passes as the FALLBACK of its ``NEO4J_PASSWORD`` lookup.

    ``None`` when there is no such lookup, or when it supplies no literal fallback. This is
    the difference between "the value appears in this file" and "this file resolves the
    password to that value" — a comment, a docstring or an unrelated constant satisfies the
    first and none of them satisfies the second.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not _reads_the_password(node) or len(node.args) < 2:
            continue
        fallback = node.args[1]
        if isinstance(fallback, ast.Constant) and isinstance(fallback.value, str):
            return fallback.value
    return None


def _conftest_source() -> str:
    """The repo-root ``conftest.py`` as text.

    A named seam rather than an inline ``read_text``: it is what lets the mutation that proves
    this node's strictness feed it a DRIFTED conftest without editing a file in the tree.
    """
    return (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")


def _every_module_resolves_through(
    monkeypatch: pytest.MonkeyPatch, credentials: GraphCredentials
) -> None:
    """Make :func:`graph_credentials` answer ``credentials`` **wherever each caller looks**.

    Two lookup shapes, and patching only one of them proves only half the tree:

    * ``exchange.retrieval.roster`` does ``from proxyshop_support.neo4j_auth import
      graph_credentials`` at module scope, so its binding is ``roster.graph_credentials`` and
      a patch on the resolver's own module never reaches it;
    * ``ingest.graph.reembed.graph_driver`` and ``proxyshop_support.service_launch.check_neo4j``
      import it INSIDE the function, so they resolve the attribute on
      ``proxyshop_support.neo4j_auth`` at call time and that is the binding to replace.
    """
    monkeypatch.setattr(neo4j_auth, "graph_credentials", lambda env=None: credentials)
    monkeypatch.setattr(roster, "graph_credentials", lambda env=None: credentials)


class _CapturedDriver:
    """Stands in for ``neo4j.Driver``. Records the auth it was built with and connects to nothing."""

    def __init__(self, uri: str, *, auth: Any, **_: Any) -> None:
        self.uri = uri
        self.auth = auth
        self.closed = False

    def verify_connectivity(self) -> None:
        return None

    def session(self, **_: Any) -> Any:
        return object()

    def close(self) -> None:
        self.closed = True


class _RefusingDriver(_CapturedDriver):
    """A server that refuses the credential it was handed, which is what a wrong password is."""

    def verify_connectivity(self) -> None:
        raise RuntimeError("Neo.ClientError.Security.Unauthorized: the client is unauthorized")


def _capture(monkeypatch: pytest.MonkeyPatch, driver_cls: type[_CapturedDriver]) -> list[Any]:
    """Point every ``GraphDatabase.driver`` call at ``driver_cls`` and collect what was built."""
    built: list[Any] = []

    def factory(uri: str, *, auth: Any = None, **kwargs: Any) -> Any:
        driver = driver_cls(uri, auth=auth, **kwargs)
        built.append(driver)
        return driver

    import neo4j

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", staticmethod(factory))
    return built


@pytest.fixture(autouse=True)
def _forget_the_warning() -> Any:
    """The resolver warns once per process per connection; each test wants its own first time."""
    neo4j_auth._WARNED.clear()
    yield
    neo4j_auth._WARNED.clear()


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may read the developer's own exported ``NEO4J_*``.

    Every test states the environment it is about. Without this the suite passes on a machine
    with ``NEO4J_PASSWORD`` exported and fails on CI, which is the opposite of what a gate over
    a credential default is for.
    """
    for name in (ENV_URI, ENV_USER, ENV_PASSWORD):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _one_driver_per_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """``roster`` pools one driver per credential triple, process-wide. Give each test its own.

    It is also why ``test_two_environments_differing_only_in_the_password_...`` exists: this
    fixture means no node here resolves two different credentials in one pool unless it goes
    out of its way to, and the cache KEY is only visible when one does.
    """
    monkeypatch.setattr(roster, "_DRIVERS", {})


# =====================================================================================
# 1. One default, in one file
# =====================================================================================
def test_the_sweep_catches_every_realistic_way_a_password_default_could_come_back(
    tmp_path: Path,
) -> None:
    """The POSITIVE CONTROL for the sweep below, and the reason it is worth running.

    A source-text gate that detects nothing passes forever, and it passes loudest on the day
    the defect returns. This node writes each shape in :data:`REINTRODUCTIONS` into a real file
    and requires the detector to flag it — run against the substring predicate this sweep used
    to be, six of the seven walked straight through: single quotes, ``os.getenv``, a bare
    ``getenv``, the ``ENV_PASSWORD`` constant, a call wrapped over three lines, and
    ``... or "pw"``. Only the double-quoted one-liner was ever caught.

    :data:`INNOCENT` is the other half. A sweep that flags the resolver being called, or a
    ``NEO4J_URI`` default, is a sweep the next lane deletes rather than obeys.
    """
    for name, source in REINTRODUCTIONS:
        path = tmp_path / f"{name.replace(' ', '_').replace('`', '')}.py"
        path.write_text(source, encoding="utf-8")
        flagged = _password_defaults_in(path.read_text(encoding="utf-8"), name)
        assert len(flagged) == 1, (
            f"the sweep flagged {flagged} for a {name} reintroduction of the password default:"
            f"\n{source}\nA gate that misses this shape reports a clean tree on the day a "
            f"second credential default comes back, which is worse than having no gate: the "
            f"green is read as evidence"
        )

    for name, source in INNOCENT:
        path = tmp_path / f"innocent_{name.replace(' ', '_')}.py"
        path.write_text(source, encoding="utf-8")
        flagged = _password_defaults_in(path.read_text(encoding="utf-8"), name)
        assert flagged == [], (
            f"the sweep flagged {name} as a password default: {flagged}\n{source}\nA sweep "
            f"that answers 'offender' to correct code gets deleted instead of obeyed"
        )


def test_no_module_outside_the_resolver_supplies_a_neo4j_password_default() -> None:
    """The sweep that would have been red before, over the whole product tree.

    It looks for the SHAPE of the defect — ``NEO4J_PASSWORD`` and a fallback in the same
    expression — rather than for the value, so a lane that reintroduces the pattern with a
    *different* literal is caught too. That is the failure mode worth catching: a second
    default nobody notices because it is spelled differently from the first.

    Its sensitivity is not asserted here; it is asserted in the positive control above, over a
    corpus of seven shapes. This node is the sweep itself.
    """
    offenders: list[str] = []
    for root in SWEPT_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if path in EXEMPT or ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            # Test modules are out of scope, and the exclusion is narrow rather than
            # convenient: the defect is PRODUCT code inventing a credential default for a
            # running service. A test that names the old shape — this file's own header quotes
            # all three pre-repair lines verbatim — is documenting it, not shipping it.
            if "tests" in path.parts or path.name.startswith(("test_", "conftest")):
                continue
            offenders += _password_defaults_in(
                path.read_text(encoding="utf-8"), str(path.relative_to(REPO_ROOT))
            )

    assert offenders == [], (
        "a second Neo4j password default is back in the tree:\n  "
        + "\n  ".join(offenders)
        + "\nEvery caller must go through proxyshop_support.neo4j_auth.graph_credentials. Two "
        "defaults is how the readiness probe ends up vouching for a credential the served "
        "path does not use"
    )


def test_the_test_harness_resolves_through_the_resolver_like_everything_else() -> None:
    """The repo-root ``conftest.py`` reads the resolver, not a fourth copy of its default.

    This node replaced one that required conftest's own ``proxyshop_dev_pw`` literal to EQUAL
    :data:`~proxyshop_support.neo4j_auth.DEV_PASSWORD`. That was the right assertion while a
    literal was there, and its failure message said to delete it the moment conftest started
    calling ``graph_credentials()`` — which it now does. Asking the old question of the new code
    can only fail, and asking nothing would drop the property entirely, so the question moved:
    not "do the two literals still agree" but "is there still only one".

    Why it is graded at all, rather than trusted: ``_neo4j_connection`` turns an authentication
    refusal into ``pytest.skip``. So a conftest that resolved the password differently from the
    served paths would not turn the graph suite RED — it would turn it SKIPPED, and a silently
    skipped datastore suite reads exactly like a passing one. That is the failure mode this file
    exists for, and it is the reason the harness is worth a node even though it ships nothing.
    """
    source = _conftest_source()

    assert "graph_credentials" in source, (
        "conftest.py no longer resolves the Neo4j credential through "
        "proxyshop_support.neo4j_auth.graph_credentials. If it has gone back to reading "
        f"{ENV_PASSWORD} with its own fallback, that is a second default again — and the graph "
        "suite will SKIP rather than fail when it drifts"
    )
    assert _password_default_argument(source) is None, (
        f"conftest.py has a literal fallback on its own {ENV_PASSWORD} lookup again, beside the "
        "resolver call. Two readings of one credential is the defect this file grades; the "
        "harness is not exempt from it just because it ships nothing"
    )
    # Deliberately NOT asserted: that the string never appears in conftest.py at all. This
    # file's own header quotes all three pre-repair lines verbatim, and the sweep above skips
    # test modules for the stated reason — naming the old shape is documenting it, not
    # shipping it. A comment recording what was removed is the convention here, not a leak.
    # The two assertions above ask the question that matters: is there a second RESOLUTION.


# =====================================================================================
# 2. Every path offers the same credential
# =====================================================================================
def test_the_served_roster_the_ingest_cli_and_the_readiness_probe_offer_one_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline: three connectors, one environment, one ``(user, password)`` — and one
    resolver.

    Two phases, and the second is what stops the first being an accident.

    **Phase one, the environment.** Each path is actually run and what it hands
    ``GraphDatabase.driver`` is captured, so this is driven rather than asserted about the
    source. It is also, on its own, VACUOUS: setting ``NEO4J_PASSWORD`` explicitly is a case
    the pre-repair code got right, because all three call sites read that same variable. A
    complete revert of every Fix B call site leaves phase one green — measured — so it grades
    the environment and not the repair.

    **Phase two, the sentinel.** ``graph_credentials`` is replaced with a function returning a
    credential no environment can produce, patched at BOTH bindings (see
    :func:`_every_module_resolves_through`), and all three drivers must be built from it. That
    is false the moment any of the three resolves its own — which is precisely what all three
    used to do, with three defaults that disagreed.
    """
    from ingest.graph.reembed import graph_driver

    monkeypatch.setenv(ENV_URI, "bolt://graph.example:7687")
    monkeypatch.setenv(ENV_USER, "reader")
    monkeypatch.setenv(ENV_PASSWORD, "the-operators-password")
    built = _capture(monkeypatch, _CapturedDriver)

    graph_sessions_from_env()()
    with graph_driver():
        pass
    service_launch.check_neo4j()

    assert len(built) == 3, f"expected three drivers, got {len(built)}"
    assert {driver.auth for driver in built} == {("reader", "the-operators-password")}, (
        f"the three paths offered different credentials: {[d.auth for d in built]}. A readiness "
        f"probe that authenticates differently from the code it vouches for reports healthy on "
        f"a container whose every graph read is refused"
    )
    assert {driver.uri for driver in built} == {"bolt://graph.example:7687"}

    through_the_resolver = _capture(monkeypatch, _CapturedDriver)
    _every_module_resolves_through(monkeypatch, SENTINEL)

    graph_sessions_from_env()()
    with graph_driver():
        pass
    service_launch.check_neo4j()

    assert len(through_the_resolver) == 3, (
        f"three connectors were driven and {len(through_the_resolver)} drivers were built"
    )
    offered = [(driver.uri, driver.auth) for driver in through_the_resolver]
    assert set(offered) == {(SENTINEL.uri, SENTINEL.auth)}, (
        f"a connector built its driver from something other than the one resolver: {offered}. "
        f"The environment still says {ENV_URI}=bolt://graph.example:7687 / "
        f"{ENV_PASSWORD}=the-operators-password, so a path that reads those variables for "
        f"itself answers with them here and the sentinel never reaches it. That is the "
        f"pre-repair tree: three call sites, three defaults, and a readiness probe vouching "
        f"for a credential no served path uses"
    )


def test_with_nothing_configured_all_three_paths_fall_back_to_the_same_development_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case the old code got wrong: unset. The probe used ``""``; the served paths did not.

    This is the node that is red on the pre-repair tree — ``check_neo4j`` built its driver with
    ``("neo4j", "")`` while ``graph_sessions_from_env`` built one with
    ``("neo4j", "proxyshop_dev_pw")``.
    """
    from ingest.graph.reembed import graph_driver

    built = _capture(monkeypatch, _CapturedDriver)

    graph_sessions_from_env()()
    with graph_driver():
        pass
    service_launch.check_neo4j()

    assert {driver.auth for driver in built} == {("neo4j", DEV_PASSWORD)}, (
        f"with NEO4J_PASSWORD unset the three paths offered {[d.auth for d in built]}; the "
        f"readiness probe used to send the empty string here, so it authenticated against a "
        f"server the served paths could not reach, and vice versa"
    )


def test_an_explicitly_empty_password_is_the_development_default_and_not_an_empty_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NEO4J_PASSWORD: "${NEO4J_PASSWORD:-}"`` in a compose file is UNSET, not empty.

    This repository has already paid for the distinction once on the Postgres side —
    ``postgresql://trust_rw:@postgres:5432/...`` produced ``fe_sendauth: no password
    supplied`` in a running container — and compose writes exactly that shape whenever a
    fragment forwards a host variable that is not exported.
    """
    monkeypatch.setenv(ENV_PASSWORD, "")

    resolved = graph_credentials()

    assert resolved.password == DEV_PASSWORD
    assert resolved.is_development_default is True


def test_a_stated_password_is_used_verbatim_and_is_not_flagged_as_the_default() -> None:
    """A configured deployment must not be told it is running on a development credential."""
    resolved = graph_credentials(
        {ENV_URI: "bolt://prod:7687", ENV_USER: "svc", ENV_PASSWORD: "  spaces are legal  "}
    )

    assert resolved.password == "  spaces are legal  ", (
        "the password was stripped or otherwise rewritten; a password is an opaque byte "
        "string and trimming it turns a correct credential into a refused one"
    )
    assert resolved.is_development_default is False
    assert resolved.auth == ("svc", "  spaces are legal  ")


def test_two_environments_differing_only_in_the_password_do_not_share_a_pooled_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``roster`` pools one driver per credential TRIPLE, and the password is the third part.

    Nothing else in this suite can see that. ``_one_driver_per_test`` hands every node a fresh
    ``_DRIVERS``, so no other test resolves two different credentials in one process — which is
    exactly the situation a PASSWORD ROTATION puts a long-lived exchange in. A pool keyed on
    ``(uri, user)`` alone would answer every request after the rotation with the driver holding
    the password the server has just stopped accepting, for the life of the process, with the
    new password sitting correctly in the environment and nothing in the log connecting the
    two. ``graph_sessions_from_env`` is bound by ``exchange.composition`` for the graph roster,
    so that process is the one serving ``POST /auctions``.
    """
    built = _capture(monkeypatch, _CapturedDriver)
    environments = [
        {ENV_URI: "bolt://graph:7687", ENV_USER: "svc", ENV_PASSWORD: password}
        for password in ("the-old-password", "the-rotated-password", "the-one-after-that")
    ]

    for environment in environments:
        graph_sessions_from_env(environment)()

    assert len(built) == len(environments), (
        f"{len(environments)} environments differing only in {ENV_PASSWORD} produced "
        f"{len(built)} drivers: {[driver.auth for driver in built]}. Two of them shared a "
        f"pooled connection, so a rotated deployment is served by a driver holding a password "
        f"the server refuses — permanently, because nothing ever evicts the entry"
    )
    assert {driver.auth[1] for driver in built} == {
        environment[ENV_PASSWORD] for environment in environments
    }, f"a driver was built with a password no environment stated: {[d.auth for d in built]}"

    graph_sessions_from_env(environments[0])()
    graph_sessions_from_env(environments[0])()

    assert len(built) == len(environments), (
        f"repeating an environment already resolved built {len(built) - len(environments)} "
        f"more driver(s); a `neo4j.Driver` owns a connection pool and this factory is on the "
        f"served roster path, so one per request is a pool leak per auction"
    )
    assert len(roster._DRIVERS) == len(environments), (
        f"the pool holds {len(roster._DRIVERS)} entries for {len(environments)} distinct "
        f"credentials"
    )


# =====================================================================================
# 3. What an operator sees
# =====================================================================================
def test_falling_back_to_the_development_default_says_so_once_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default is honest, not silent — and it is not one line per served auction either."""
    with caplog.at_level(logging.WARNING, logger=neo4j_auth.__name__):
        graph_credentials()
        graph_credentials()
        graph_credentials()

    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1, (
        f"{len(warnings)} warnings for three resolutions; graph_credentials is called on the "
        f"request path, so an unconditional warning is a log line per auction"
    )
    message = warnings[0].getMessage()
    assert ENV_PASSWORD in message, "the warning does not name the variable an operator must set"
    assert DEV_PASSWORD not in message, "the warning printed the password itself"


def test_the_readiness_probe_refuses_and_names_where_the_credential_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong password: the container stays unhealthy and the log says which credential.

    ``Neo.ClientError.Security.Unauthorized`` on its own tells an operator that the server said
    no. It does not tell them what was offered, which is the half that distinguishes "I set
    NEO4J_PASSWORD to the wrong thing" from "I never set NEO4J_PASSWORD and this container is
    authenticating with the development default".
    """
    monkeypatch.setenv(ENV_URI, "bolt://prod:7687")
    _capture(monkeypatch, _RefusingDriver)

    with pytest.raises(service_launch.NotReady) as refused:
        service_launch.check_neo4j()

    message = str(refused.value)
    assert "bolt://prod:7687" in message
    assert ENV_PASSWORD in message, (
        f"the readiness failure does not name {ENV_PASSWORD}: {message!r}. This is the only "
        f"line an operator gets out of a container stuck in `starting`"
    )
    assert "development default" in message, (
        f"the readiness failure does not say the credential was the unconfigured default: "
        f"{message!r}"
    )
    assert DEV_PASSWORD not in message, f"the readiness failure printed the password: {message!r}"


def test_a_stated_password_is_never_echoed_into_a_readiness_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction: a real deployment's secret must not reach a health log either."""
    monkeypatch.setenv(ENV_PASSWORD, "s3cret-do-not-log")
    _capture(monkeypatch, _RefusingDriver)

    with pytest.raises(service_launch.NotReady) as refused:
        service_launch.check_neo4j()

    message = str(refused.value)
    assert "s3cret-do-not-log" not in message, f"the password rode into the log: {message!r}"
    assert ENV_PASSWORD in message, "the failure does not say which variable supplied it"


def test_describe_names_the_connection_and_never_the_password() -> None:
    """``describe()`` is what every message uses, so its contract is pinned on its own."""
    described = graph_credentials(
        {ENV_URI: "bolt://prod:7687", ENV_USER: "svc", ENV_PASSWORD: "hunter2"}
    ).describe()

    assert "bolt://prod:7687" in described
    assert "svc" in described
    assert ENV_PASSWORD in described
    assert "hunter2" not in described, f"describe() leaked the password: {described!r}"


def test_describe_strips_a_credential_the_operator_embedded_in_the_uri_or_the_user(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ "Never contains a password" has to cover one this module did not choose.

    ``NEO4J_URI=bolt://neo4j:s3cr3t@host:7687`` is the ordinary DSN habit — it is how Postgres
    and Mongo are configured all over this tree — and it parses, so the driver accepts it and
    the operator has no reason to think twice. ``describe()`` is the whole payload of a
    readiness failure and of ``ingest.graph.reembed``'s error log, both of which land in a
    container's health output, so before ``_without_userinfo`` existed that secret was printed
    verbatim into a log an operator pastes into a ticket. Same for a colon-bearing
    ``NEO4J_USER``.

    **The illegal-URI case is asserted too, and it is the likelier mistake.** ``urlsplit``
    alone only reaches userinfo the URI grammar admits. A password whose ``/`` is
    percent-encoded (``p%2Fss``) parses and is stripped; a RAW ``/`` —
    ``bolt://neo4j:p/ss@host:7687``, not a legal URI because ``/`` is outside the userinfo
    character set — makes ``urlsplit`` read the netloc as ``neo4j:p`` with no ``@`` in it, and
    a single-pass strip hands the whole string back with the password in it. It is the
    likelier of the two because a password with a ``/`` in it looks perfectly fine to the
    person typing it. ``_without_userinfo`` has a second pass for exactly that, which
    over-redacts rather than under-redacts, and both shapes are pinned below.
    """
    embedded_in_the_uri = graph_credentials(
        {ENV_URI: "bolt://neo4j:s3cr3t@host:7687", ENV_PASSWORD: "stated"}
    ).describe()

    assert "s3cr3t" not in embedded_in_the_uri, (
        f"describe() printed the password the operator embedded in {ENV_URI}: "
        f"{embedded_in_the_uri!r}. This sentence is what a stuck container's health log carries"
    )
    assert "host:7687" in embedded_in_the_uri, (
        f"the userinfo was stripped and took the SERVER with it: {embedded_in_the_uri!r}. An "
        f"operator who cannot see which host was dialled has lost the other half of the "
        f"diagnosis"
    )

    embedded_in_the_user = graph_credentials(
        {ENV_USER: "neo4j:s3cr3t", ENV_PASSWORD: "stated"}
    ).describe()

    assert "s3cr3t" not in embedded_in_the_user, (
        f"describe() printed a secret written into {ENV_USER}: {embedded_in_the_user!r}"
    )
    assert "neo4j" in embedded_in_the_user, (
        f"the user is no longer named at all: {embedded_in_the_user!r}"
    )

    # A password carrying `@` and a percent-encoded `/`: the split has to be taken at the LAST
    # `@` of the authority, which is why this is `urlsplit` and `rpartition` rather than a
    # regex stopping at the first one.
    awkward = graph_credentials(
        {ENV_URI: "bolt://neo4j:p%2Fss@w0rd@host:7687", ENV_PASSWORD: "stated"}
    ).describe()

    assert "p%2Fss" not in awkward and "w0rd" not in awkward, (
        f"a password containing `@` and an encoded `/` walked the split: {awkward!r}. A regex "
        f"that stops at the first `@` leaves the tail of the password in the message"
    )
    assert "host:7687" in awkward, f"the host was lost with the credential: {awkward!r}"

    # THE ILLEGAL URI, and the one a single `urlsplit` pass hands back verbatim. `/` is outside
    # the userinfo character set, so the authority ends at it and `urlsplit` sees no `@` to
    # strip — a password an operator typed with a slash in it is not exotic, it is a password.
    raw_slash = graph_credentials(
        {ENV_URI: "bolt://neo4j:p/ss@host:7687", ENV_PASSWORD: "stated"}
    ).describe()

    assert "p/ss" not in raw_slash and "ss@host" not in raw_slash, (
        f"a password containing a RAW `/` was printed verbatim: {raw_slash!r}. `urlsplit` reads "
        f"this netloc as 'neo4j:p' with no `@` in it, so a single-pass strip returns the whole "
        f"string; the fallback pass is what has to catch it"
    )
    assert "host:7687" in raw_slash, (
        f"the fallback pass took the server with the credential: {raw_slash!r}. It is allowed "
        f"to over-redact, but an operator still has to be able to see which host was dialled"
    )

    with caplog.at_level(logging.WARNING, logger=neo4j_auth.__name__):
        graph_credentials({ENV_URI: "bolt://neo4j:s3cr3t@host:7687"})

    warned = " ".join(record.getMessage() for record in caplog.records)
    assert "s3cr3t" not in warned, (
        f"the unset-password WARNING printed the credential embedded in {ENV_URI}: {warned!r}. "
        f"This line is emitted on the first resolution of every process, so it reaches the log "
        f"of a container that never fails at all"
    )
    assert "host:7687" in warned, f"the warning names no server: {warned!r}"


def test_the_table_of_connections_already_warned_about_is_bounded() -> None:
    """ "Remember every connection this process has seen" is an unbounded set.

    Nothing feeds it caller-controlled input today — every caller reads ``os.environ`` — so
    this is a ceiling on a table rather than a repair of a live leak, and it is worth having
    for the same reason the auction store's cap is: a deduplication table keyed on a value from
    outside the function has no business growing without one. Past the ceiling the module warns
    every time instead of remembering, which is the loud direction to degrade in.
    """
    for index in range(200):
        graph_credentials({ENV_URI: f"bolt://graph-{index}:7687"})

    assert len(neo4j_auth._WARNED) <= MAX_REMEMBERED_CONNECTIONS, (
        f"200 distinct connections left {len(neo4j_auth._WARNED)} remembered against a ceiling "
        f"of {MAX_REMEMBERED_CONNECTIONS}; the table that exists to keep one warning out of the "
        f"log per auction must not itself grow per auction"
    )
