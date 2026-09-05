"""ProxyShop's runnable demos — the product, made visible to a person in a room.

    .venv/bin/python -m proxyshop_demo

Deliberately import-free at package scope. ``docs/tests/test_runbook_executability.py`` and
the frozen ``.swarm-loop/acceptance/test_e8_proofs.py`` both resolve the runbook's commands by
running ``importlib.util.find_spec`` in an isolated subprocess, and a package whose ``__init__``
dragged in FastAPI, uvicorn and httpx would make "does this command name something real" cost a
second and depend on the whole application graph importing cleanly.

The driver itself is :mod:`proxyshop_demo.s1`.
"""

from __future__ import annotations

__all__: list[str] = []
