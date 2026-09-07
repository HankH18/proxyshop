"""The trust service answers exactly the operations its contract publishes (T-312).

The property is asserted in BOTH directions on purpose, because each direction is a
different lie and only one of them was ever the famous one.

* **Published and not served** is a promise to a consumer that 404s. A client generated
  from the document compiles, ships, and fails in a deployment.
* **Served and not published** is a reachable surface nobody agreed to. Nothing on
  ``apps/trust`` is authenticated — ``trust.reconcile.routes`` says so in its own header,
  "There is not one ``Depends`` in ``apps/trust``" — so an undeclared route is not a
  documentation gap, it is an unreviewed door.

This file is the trust-side owner of that sweep. An equivalent assertion lives in
``apps/exchange/tests/test_repro_open_tickets.py`` under a ``test_t312_…`` name; that one
reaches ``trust.main`` from the exchange's file only because it *can*, and it carries a
strict ``xfail`` this lane cannot remove (``apps/exchange/**`` is held by another lane).
Nothing about the property is exchange-specific, and the service that owns the surface
should own the gate on it, so the assertion is repeated here rather than referenced.

WHAT THIS MEASURES. ``create_app()`` is the same factory ``uvicorn`` reaches through the
module-level ``app`` in ``trust.main``, and ``app.openapi()['paths']`` is the route table
FastAPI built from the routers it actually mounted — not a grep over decorators, which
cannot see a ``routes.py`` that failed to export ``router`` and is silently skipped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
TRUST_OPENAPI = REPO_ROOT / "packages" / "contracts" / "openapi" / "trust.openapi.json"

#: The OpenAPI keys under a path item that are operations. `parameters`, `summary`,
#: `description` and `$ref` are path-item metadata and are not routes.
_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


def _operations(paths: Any) -> set[tuple[str, str]]:
    """``{(METHOD, path)}`` for one OpenAPI ``paths`` mapping, from either side."""
    found: set[tuple[str, str]] = set()
    for path, item in (paths or {}).items():
        for method in _HTTP_METHODS:
            if method in item:
                found.add((method.upper(), str(path)))
    return found


def served_operations() -> set[tuple[str, str]]:
    """What a started ``proxyshop-trust`` process actually answers."""
    from trust.main import create_app

    return _operations(create_app().openapi()["paths"])


def published_operations() -> set[tuple[str, str]]:
    """What ``packages/contracts/openapi/trust.openapi.json`` declares."""
    with TRUST_OPENAPI.open(encoding="utf-8") as handle:
        return _operations(json.load(handle).get("paths"))


def _divergence(served: set[tuple[str, str]], published: set[tuple[str, str]]) -> str:
    unserved = sorted(f"{method} {path}" for method, path in published - served)
    undeclared = sorted(f"{method} {path}" for method, path in served - published)
    return (
        f"published and served by nothing: {unserved or 'none'}; "
        f"served and declared nowhere: {undeclared or 'none'}"
    )


def test_the_sweep_is_armed() -> None:
    """Both sides are non-empty, so `served == published` cannot pass by both being `set()`."""
    served, published = served_operations(), published_operations()
    assert served, "trust.main mounted no operations; the sweep would pass vacuously"
    assert published, "the trust contract declares nothing; the sweep would pass vacuously"


def test_the_trust_service_serves_exactly_the_operations_its_contract_publishes() -> None:
    served, published = served_operations(), published_operations()
    assert served == published, (
        f"the trust service's served surface diverges from its published contract — "
        f"{_divergence(served, published)}"
    )


def test_the_reconciliation_doors_are_declared() -> None:
    """The named half of the sweep above, so a regression says *which* door went undeclared.

    ``GET``/``POST /reconcile`` are where a completed purchase becomes a trust update (R4 +
    R12, "one trust system, not two"). They are unauthenticated and they append to a
    hash-chained ledger, which is the strongest case in this service for a route being
    written down.
    """
    published = published_operations()
    for operation in (("GET", "/reconcile"), ("POST", "/reconcile")):
        assert operation in published, (
            f"{operation[0]} {operation[1]} is served by trust.reconcile.routes and declared "
            f"in no document; published: {sorted(published)}"
        )


def test_no_per_store_trust_read_or_second_feedback_intake_is_published() -> None:
    """T-312's ruling, kept from being silently reverted.

    ``GET /stores/{store_id}/trust`` and ``POST /feedback/{order_ref}`` were declared and
    served by nothing. Re-publishing either without serving it re-opens the same gap, and
    serving either as published is worse than the gap:

    * the per-store read carries no identity parameter of any kind, so as declared it hands
      any anonymous caller any store's full per-dimension posture; and
    * the feedback intake carries ``matched_pitch``/``reason``/``pseudonymous_context`` and
      no routing evidence, so it would take R14 feedback from a caller the network never
      routed — while ``buyer_svc.feedback.submission.submit_feedback`` is already the one
      place that decides who may leave feedback.

    Trust's real feedback intake is ``POST /events`` with ``kind: "feedback"``, which is
    served, declared, and what ``trust.feedback`` folds into ``feedback_match``.
    """
    published = published_operations()
    assert ("GET", "/stores/{store_id}/trust") not in published
    assert ("POST", "/feedback/{order_ref}") not in published
