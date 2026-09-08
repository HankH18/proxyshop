"""The one place this repository decides how to authenticate to Neo4j.

Why this module exists
======================
``NEO4J_PASSWORD`` had **three disagreeing code-side defaults**, two of them on served
paths::

    apps/exchange/src/retrieval/roster.py:519      os.environ.get("NEO4J_PASSWORD", "proxyshop_dev_pw")
    services/ingest/src/graph/reembed.py:549       os.environ.get("NEO4J_PASSWORD", "proxyshop_dev_pw")
    proxyshop_support/service_launch.py:251        os.environ.get("NEO4J_PASSWORD", "")

The third one is the readiness probe — the check that decides whether a container is
reported healthy — and it is the disagreement that matters. A probe that authenticates with
a *different* credential from the code it is vouching for is a probe that can pass while the
served path is refused, or refuse while the served path is fine. It is not a stricter check
or a looser one; it is a check of something else.

The decision: an HONEST, DOCUMENTED development default, spelled ONCE
====================================================================
Not a hard failure on an unset ``NEO4J_PASSWORD``, and the reasons are specific to this
repository rather than a general preference:

1. **The repo has already made this decision, for Postgres.**
   :mod:`proxyshop_support.postgres` carries :data:`~proxyshop_support.postgres.
   DEV_ROLE_PASSWORD` and an admin literal that is this same ``proxyshop_dev_pw``, resolved
   through one function with a documented precedence. Making the graph credential the single
   thing in the tree that refuses to start without an environment variable would not be a
   stronger posture, it would be a second convention.

2. **CI cannot set it.** ``.gitlab-ci.yml`` deliberately declares no ``NEO4J_*`` name in its
   ``variables:`` block — ``scripts/ci_datastore_proof.py`` FAILS the pipeline for adding
   one, because a job-level ``NEO4J_*`` kills the neo4j service container — and
   ``.github/workflows/verify.yml`` sets no datastore credentials at all. Both rely on the
   client-side default matching ``docker-compose.yml``'s ``NEO4J_AUTH=neo4j/proxyshop_dev_pw``.
   A raise-on-unset would not make those pipelines loud; it would make the graph suite
   *skip*, which is the failure mode that looks exactly like passing.

3. **The dangerous half of a dev default is the SILENCE, not the default.** A credential
   that quietly works in development and quietly fails in production is worse than either
   alternative — so this module removes the silence rather than the default:

   * one resolution, so the readiness probe and the served path cannot disagree about what
     the credential is;
   * :attr:`GraphCredentials.is_development_default` says, in the caller's hands, that
     nothing was configured;
   * :func:`graph_credentials` LOGS a warning naming ``NEO4J_PASSWORD`` the first time a
     process falls through to the default, so the line is in a production log before the
     first refused session rather than absent from it;
   * :attr:`GraphCredentials.source` gives every failure message a true account of where the
     credential came from — which is what an operator staring at ``Neo.ClientError.Security
     .Unauthorized`` actually needs.

**Nothing here logs or formats a password.** :meth:`GraphCredentials.describe` names the
URI, the user and the *source*, and is what belongs in an exception; the value itself only
ever reaches the driver. That covers a password the OPERATOR embedded rather than only the
one this module resolved — ``NEO4J_URI=bolt://neo4j:s3cr3t@host:7687`` is the ordinary DSN
habit, and :func:`_without_userinfo` strips it before any message is built.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "DEFAULT_URI",
    "DEFAULT_USER",
    "DEV_PASSWORD",
    "ENV_PASSWORD",
    "ENV_URI",
    "ENV_USER",
    "MAX_REMEMBERED_CONNECTIONS",
    "GraphCredentials",
    "graph_credentials",
]

_log = logging.getLogger(__name__)

#: The three environment names. Spelled once here and imported everywhere else, so a
#: search for the variable finds the resolver rather than four call sites.
ENV_URI = "NEO4J_URI"
ENV_USER = "NEO4J_USER"
ENV_PASSWORD = "NEO4J_PASSWORD"

#: The compose-network default is *not* here, deliberately: ``bolt://localhost:7687`` is a
#: host-side address, and every container that needs ``bolt://neo4j:7687`` is handed
#: ``NEO4J_URI`` by its compose fragment. A container default would be a second source of
#: truth for a value the fragment already states.
DEFAULT_URI = "bolt://localhost:7687"
DEFAULT_USER = "neo4j"

#: The documented DEVELOPMENT password, and the only Neo4j password literal in the tree.
#:
#: It is the historical value — the pair ``docker-compose.yml`` seeds the server with
#: (``NEO4J_AUTH: neo4j/proxyshop_dev_pw``), the pair ``.gitlab-ci.yml`` seeds its neo4j
#: service with, and the pair ``.env.example`` documents — so a checkout that exports nothing
#: behaves exactly as it always has. Dev-only: this is not a credential to anything real, and
#: it is named ``DEV_`` so that any future reader asking "is this a secret in the repo?" gets
#: the answer from the name. The identically-spelled sibling is
#: :data:`proxyshop_support.postgres.ROLES`'s admin entry; the two are separate constants
#: because they are credentials to separate servers that happen to share a dev value.
DEV_PASSWORD = "proxyshop_dev_pw"

#: How :attr:`GraphCredentials.source` describes where the password came from.
SOURCE_ENVIRONMENT = f"the {ENV_PASSWORD} environment variable"
SOURCE_DEV_DEFAULT = f"the development default, because {ENV_PASSWORD} is unset"

#: One warning per process per connection, not one per resolution. ``graph_credentials`` is
#: called on the request path (``exchange.retrieval.roster``), so an unconditional warning
#: would be a log line per auction; a silent-after-the-first one is still in the log before
#: the first failure, which is the whole job.
#:
#: Bounded, because "remember every distinct connection this process has seen" is an
#: unbounded set keyed on a value that comes from outside this function. Nothing feeds it
#: caller-controlled input today — every caller reads ``os.environ`` — but a deduplication
#: table has no business growing without a ceiling, and past it the module simply warns every
#: time, which is the loud direction to degrade in.
MAX_REMEMBERED_CONNECTIONS = 64

#: What stands in for userinfo that could not be parsed out of a URI — see
#: :func:`_without_userinfo`'s second pass. Visible in the message on purpose: an operator
#: reading "bolt://<redacted>@host:7687" learns both that their URI carries a credential and
#: that this process did not print it.
_REDACTED_USERINFO = "<redacted>"
_WARNED: set[tuple[str, str]] = set()
_WARNING_LOCK = threading.Lock()


def _without_userinfo(uri: str) -> str:
    """``uri`` with any ``user:password@`` stripped before it can reach a log.

    Operators put credentials in a DSN — it is the normal shape for Postgres and Mongo, and
    ``bolt://neo4j:s3cr3t@host:7687`` parses. :meth:`GraphCredentials.describe` is the whole
    payload of a readiness failure and of ``ingest.graph.reembed``'s error log, and the module
    header promises nothing here formats a password; without this that promise held only for
    the password *this module resolved*, not for one the operator embedded in ``NEO4J_URI``.

    Two passes, and the second one is not belt-and-braces. :func:`~urllib.parse.urlsplit` is
    right for a LEGAL URI — a password containing ``@``, or percent-encoded, cannot walk the
    split. It is not right for an illegal one, and an operator who types a password with a raw
    ``/`` in it writes exactly that: in ``bolt://neo4j:p/ss@host:7687`` the authority ends at
    the ``/``, so ``urlsplit`` reports a netloc of ``neo4j:p`` with no ``@`` at all and the
    first pass hands the string back **verbatim, password included**. Measured, and it is the
    likelier mistake of the two, because a password with a ``/`` looks fine to the person
    typing it.

    So an ``@`` anywhere after the scheme is treated as userinfo that failed to parse and the
    whole run up to the LAST one is replaced. That can over-redact — a genuine ``@`` in a path
    would take the host with it — and over-redacting a diagnostic is the acceptable half of
    this trade, where printing a credential is not. Bolt URIs carry no path in practice.
    """
    try:
        parts = urlsplit(uri)
    except ValueError:  # pragma: no cover - urlsplit is total for str in practice
        return "<unparseable NEO4J_URI>"
    if "@" in parts.netloc:
        _, _, hostport = parts.netloc.rpartition("@")
        return urlunsplit((parts.scheme, hostport, parts.path, parts.query, parts.fragment))
    scheme, separator, rest = uri.partition("://")
    if separator:
        if "@" not in rest:
            return uri
        _, _, tail = rest.rpartition("@")
        return f"{scheme}://{_REDACTED_USERINFO}@{tail}"

    # THIRD PASS, and it is the one the first two missed. A URI with no `//` at all —
    # `neo4j:s3cr3t@host:7687` — leaks VERBATIM through both of the passes above, and an
    # adversarial reader found it after this function shipped promising the opposite.
    # `urlsplit` reads `neo4j:` as the scheme and everything after it as a PATH, so there is
    # no netloc for the first pass to find an `@` in; and the second pass partitions on
    # `://`, which is not there, so it hands the string straight back.
    #
    # Measured before the fix: `neo4j:LEAKME@host:7687` came out of `describe()` intact,
    # under a docstring that says "Never contains a password."
    #
    # Same trade as above, applied to the same shape: an `@` after the scheme is userinfo
    # that did not parse, everything up to the LAST one goes, and over-redacting a
    # diagnostic is the acceptable half.
    scheme, colon, rest = uri.partition(":")
    if not colon or "@" not in rest:
        return uri
    _, _, tail = rest.rpartition("@")
    return f"{scheme}:{_REDACTED_USERINFO}@{tail}"


@dataclass(frozen=True)
class GraphCredentials:
    """How to reach Neo4j, and an honest account of where each part came from."""

    uri: str
    user: str
    password: str
    #: Prose naming where :attr:`password` came from — one of :data:`SOURCE_ENVIRONMENT` or
    #: :data:`SOURCE_DEV_DEFAULT`. Safe to put in a log line or an exception.
    source: str
    #: ``True`` when nothing configured the password and :data:`DEV_PASSWORD` was used.
    is_development_default: bool

    @property
    def auth(self) -> tuple[str, str]:
        """The ``(user, password)`` pair ``neo4j.GraphDatabase.driver`` takes as ``auth``."""
        return (self.user, self.password)

    @property
    def cache_key(self) -> tuple[str, str, str]:
        """A key identifying this connection, for callers that pool one driver per triple."""
        return (self.uri, self.user, self.password)

    def describe(self) -> str:
        """This connection in words, for an operator. **Never contains a password.**

        What an operator sees when the password is wrong or unset is this sentence beside the
        driver's own refusal, and the two together are the whole diagnosis: the driver says
        the server rejected the credential, and this says which credential was offered and
        why — ``NEO4J_PASSWORD``, or the development default because nobody set it.

        "Never contains a password" now covers one this module did not choose. Both halves of
        the pair are scrubbed rather than only the resolved password: an operator who writes
        ``NEO4J_URI=bolt://neo4j:s3cr3t@host:7687`` — the normal DSN habit — used to have that
        secret printed verbatim into a container health log, and so did one who wrote a
        colon-bearing ``NEO4J_USER``.
        """
        user = self.user.split(":", 1)[0]
        return f"{_without_userinfo(self.uri)} as {user!r}, password from {self.source}"


def graph_credentials(env: Mapping[str, str] | None = None) -> GraphCredentials:
    """Resolve ``NEO4J_URI`` / ``NEO4J_USER`` / ``NEO4J_PASSWORD`` — the only place that does.

    Args:
        env: the environment to read; the process environment when ``None``. Present so a
            test can drive a resolution without mutating ``os.environ``, which is the same
            shape ``exchange.retrieval.roster``'s seam takes.

    Returns:
        A :class:`GraphCredentials`. Never raises, and never connects: whether the server
        accepts these is not known until a driver is built from them.

    An EMPTY ``NEO4J_PASSWORD`` is treated as unset and falls through to
    :data:`DEV_PASSWORD`. That is not leniency, it is the ``fe_sendauth: no password
    supplied`` lesson this package already learned once on the Postgres side (see
    :mod:`proxyshop_support.service_launch`'s header): compose writes
    ``${NEO4J_PASSWORD:-}`` into a container as an explicitly empty string, which is a
    *different* thing from no password and which every driver refuses with a message about
    authentication rather than about configuration. The readiness probe used to default to
    exactly that empty string.
    """
    source_env = dict(os.environ if env is None else env)
    uri = str(source_env.get(ENV_URI) or DEFAULT_URI).strip() or DEFAULT_URI
    user = str(source_env.get(ENV_USER) or DEFAULT_USER).strip() or DEFAULT_USER
    stated = str(source_env.get(ENV_PASSWORD) or "")
    if stated:
        return GraphCredentials(
            uri=uri,
            user=user,
            password=stated,
            source=SOURCE_ENVIRONMENT,
            is_development_default=False,
        )

    key = (uri, user)
    with _WARNING_LOCK:
        first = key not in _WARNED
        if len(_WARNED) < MAX_REMEMBERED_CONNECTIONS:
            _WARNED.add(key)
    if first:
        _log.warning(
            "neo4j: %s is unset, so %s at %s authenticates with the DEVELOPMENT default "
            "(proxyshop_support.neo4j_auth.DEV_PASSWORD). That is the pair docker-compose.yml "
            "seeds a local server with; against any other server it will be refused. Set %s to "
            "silence this line",
            ENV_PASSWORD,
            user.split(":", 1)[0],
            _without_userinfo(uri),
            ENV_PASSWORD,
        )
    return GraphCredentials(
        uri=uri,
        user=user,
        password=DEV_PASSWORD,
        source=SOURCE_DEV_DEFAULT,
        is_development_default=True,
    )
