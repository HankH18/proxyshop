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
``neo4j_lock``
    D37's cross-worker ``flock`` on ``/tmp/proxyshop-neo4j.lock``.
``reachability``
    Non-blocking TCP probes used to *skip* ``@pytest.mark.docker`` tests with an explicit
    message when the compose stack is down. Never hangs, and answers **per service** (T-109)
    so one datastore's outage cannot empty another datastore's gate.
``service_markers``
    The rule that turns one collected item into the set of compose services it needs — an
    explicit ``@pytest.mark.docker("postgres")`` argument, else the datastore fixtures it
    requests, else the whole stack.
"""

__all__ = [
    "clock",
    "embedding",
    "fixture_loader",
    "llm_double",
    "neo4j_lock",
    "reachability",
    "redis_client",
    "service_markers",
    "worker",
]
