"""``make demo-seed`` — provision the generated catalog into a running shopify-stub.

``Makefile:19`` is ``demo-seed: ; @./.venv/bin/python -m fixtures.seed --category "$(SEED_CATEGORY)"``,
so this package is the entry point that target runs.

The seeding rule is the stub's own: ``POST /_stub/seed`` upserts variants and answers
``{"created", "unchanged", "updated"}``, counting a variant whose stored value already
equals the incoming one as *unchanged* rather than rewriting it. Running this twice is
therefore a no-op the second time — that is what "idempotent against the stub" means, and
it is asserted over real HTTP against a real stub in ``fixtures/tests/test_seed.py``.

C9 / the ticket's non-goal: nothing here provisions a live Shopify store. ``--stub-url``
points at the local stub (``SHOPIFY_STUB_URL``, default ``http://localhost:8787``); live
dev-store provisioning belongs to the demo runbook (T-085), never to a ticket verify.
"""

from __future__ import annotations

__all__ = ["DEFAULT_STUB_URL", "SeedError", "build_payload", "seed_stub", "variant_body"]

import os
from typing import Any, Protocol

from fixtures.generator import generate
from fixtures.manifest import load_manifest

#: The repo's convention for where the local stub listens (`.env.example`).
DEFAULT_STUB_URL = os.environ.get("SHOPIFY_STUB_URL") or "http://localhost:8787"


class SeedError(Exception):
    """The seed could not be applied to the stub."""


class _Poster(Protocol):
    """The one method this module needs from an HTTP client."""

    def post(self, url: str, *, json: Any = None) -> Any: ...


def build_payload(seed_category: str | None = None, seed: int | None = None) -> dict[str, Any]:
    """The generator payload for a category, defaulting both inputs to the manifest's.

    Defaulting to the manifest rather than to a literal is the point: the seed and the
    category that reproduce the demo are ground truth a human approved, not a CLI habit.
    """
    manifest = load_manifest()
    category = seed_category or str(manifest["seed_category"])
    resolved_seed = manifest["seed"] if seed is None else seed
    return generate(category, int(resolved_seed))


def variant_body(payload: dict[str, Any]) -> dict[str, Any]:
    """The exact body ``POST /_stub/seed`` expects, built from a generator payload."""
    variants = payload.get("variants") or []
    if not variants:
        raise SeedError("the generated payload carries no variants to seed")
    return {"variants": list(variants)}


def seed_stub(client: _Poster, base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST the generated variants to a running stub and return its upsert report.

    The client is injected rather than constructed here so the same code path a human runs
    from ``make demo-seed`` is the code path the test drives against an in-process stub —
    the alternative (a test-only shortcut around the HTTP call) would prove nothing about
    the thing the Makefile actually runs.
    """
    url = f"{base_url.rstrip('/')}/_stub/seed"
    response = client.post(url, json=variant_body(payload))
    status = getattr(response, "status_code", None)
    if status != 200:
        detail = getattr(response, "text", "")
        raise SeedError(f"stub refused the seed: HTTP {status} {detail[:300]}")
    report = response.json()
    if not isinstance(report, dict) or "created" not in report:
        raise SeedError(f"stub returned an unexpected seed report: {report!r}")
    return report
