"""Orchestrator-owned (T-000) shared runtime helpers for the ProxyShop monorepo.

This package is **frozen after T-000 closes**: no feature ticket edits it, and it sits
outside every ticket ``scope`` glob in ``tickets.json`` on purpose, so that parallel
workers can never race on it.

It exists because a handful of cross-cutting rulings need a single implementation that
belongs to no feature package:

``redis_client``
    D39's Redis client wrapper: per-worker logical DB index plus the central ``w{N}:``
    key prefix. ``FLUSHDB`` is the permitted reset; the repo-wide banned reset command is
    rejected at runtime as well as by the lint gate.
``clock``
    ``ManualClock`` — an injectable, wall-clock-independent clock for deterministic tests.
``llm_double``
    D19's ``LLM_PROVIDER=double`` deterministic offline LLM double.
``embedding``
    D18's ``EMBEDDING_PROVIDER=hash`` deterministic 1024-dimension embedding (D6).
``worker``
    ``PROXYSHOP_WORKER`` resolution (D38).
``fixture_loader``
    The ``tests/_fixtures_*.py`` auto-discovery used by every per-directory ``conftest.py``,
    so a worker adds fixtures in a file it owns and never edits a shared conftest.
``logging_config``
    T-308's single place application logging is configured. Nothing in the repo configured
    it before, so a started service had ``root.handlers == []`` at level ``WARNING`` and
    every INFO call site in the product was dropped before a record was built. Importing
    this module changes nothing; a service calls ``configure_logging()`` on its startup
    path. Also carries the per-request correlation id (``RequestIdMiddleware``).
``neo4j_auth``
    The one resolution of ``NEO4J_URI``/``NEO4J_USER``/``NEO4J_PASSWORD``. It is here rather
    than in a feature package because the READINESS PROBE (``service_launch``) and the two
    served readers (``exchange.retrieval.roster``, ``ingest.graph.reembed``) all need it, and
    while they each spelled their own defaults the probe authenticated with ``""`` and the
    served paths with ``proxyshop_dev_pw`` — a health signal vouching for a credential the
    service does not use.
``neo4j_lock``
    D37's cross-worker ``flock`` on ``/tmp/proxyshop-neo4j.lock``.
``reachability``
    Non-blocking TCP probes used to *skip* ``@pytest.mark.docker`` tests with an explicit
    message when the compose stack is down. Never hangs, and answers **per service** (T-109)
    so one datastore's outage cannot empty another datastore's gate.
``service_markers``
    The rule that turns one collected item into the set of compose services it needs — an
    explicit ``@pytest.mark.docker("postgres")`` argument, else the datastore fixtures it
    requests. An item that declares neither is **refused**, not widened to the whole stack
    (T-172): silence about a dependency must not read as depending on everything.
"""

__all__ = [
    "clock",
    "embedding",
    "fixture_loader",
    "llm_double",
    "logging_config",
    "neo4j_auth",
    "neo4j_lock",
    "reachability",
    "redis_client",
    "service_markers",
    "worker",
]
