"""Reproduction gates for the open ``packages/store-agent`` findings.

Same mechanism as the sibling files in ``apps/exchange/tests`` and ``services/ingest/tests``:
every test asserts the behaviour that SHOULD hold, carries ``xfail(strict=True)`` so an
ordinary run reports ``xfailed`` and ``make verify`` stays green, and the ticket's own gate
(``pytest <file> -q --runxfail -k <name>``) reports a real failure with the test SELECTED.
``strict=True`` turns the eventual repair into an XPASS *failure*, so the marker cannot
outlive the bug.

Covered here: **T-309** — the store agent serves no HTTP path at all.

The gate is a *property*, not a probe over one route name: build the app, read
``app.openapi()['paths']``, load ``packages/contracts/openapi/store-agent.openapi.json``, and
require the two operation sets to agree **in both directions**. A hand-written
``client.post('/v1/bid-requests')`` blocks exactly one way of being wrong; this one keeps
holding as routes are added on either side, and it fails loudly rather than quietly if the
contract ever declares a second door.

Nothing here touches product source. A lane that repairs the defect it was asked to reproduce
destroys the gate that would have graded the repair.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The published store-agent contract. It is the *specification* of the served surface, which
#: is what makes a path it declares and the app does not serve a defect rather than a taste.
STORE_AGENT_OPENAPI = REPO_ROOT / "packages/contracts/openapi/store-agent.openapi.json"

#: The methods an OpenAPI path item may carry. Everything else under a path item
#: (``parameters``, ``summary``, ``$ref``, ``servers``) is not an operation and must not be
#: counted as one — a sweep that counted them would inflate its own non-zero check.
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def normalise(path: str) -> str:
    """``/stores/{store_id}/trust`` -> ``/stores/{}/trust``.

    The comparison is about the *wire shape* of the route, not about what the service happens
    to name its path parameter. A service that serves ``/stores/{sid}/trust`` genuinely answers
    the contract's ``/stores/{store_id}/trust``; grading the parameter's spelling would make
    this gate fail for a reason the ticket is not about. Both raw spellings are still printed
    in every failure message, so a genuine naming divergence is visible without being fatal.

    Byte-for-byte the same implementation as the sibling gates in ``apps/exchange/tests`` and
    ``services/ingest/tests``. It was a regex here first, and the two spellings DISAGREED on
    malformed input (``/a/{b/{c}`` gave ``/a/{b/{}`` under the regex and ``/a/{}`` here), which
    is exactly the kind of quiet divergence that makes three copies of a helper worse than one.
    They are copies rather than a shared import because this lane owns three files in three
    packages and no place to put a shared one; see the lane report.

    Behaviour on malformed input, stated exactly: a ``{`` with no ``}`` anywhere after it is
    passed through unchanged, but ``/a/{b/{c}`` collapses ``b/{c`` into a single ``{}`` — the
    first ``{`` pairs with the only ``}``. The injectivity check in the arming test is what
    keeps a future contract from collapsing two distinct paths onto one string unnoticed.
    """
    out: list[str] = []
    rest = path
    while "{" in rest:
        head, _, tail = rest.partition("{")
        _param, closed, rest = tail.partition("}")
        if not closed:
            return "".join(out) + head + "{" + tail
        out.append(head + "{}")
    return "".join(out) + rest


def published_operations(contract: Path) -> set[tuple[str, str]]:
    """``{(METHOD, normalised path)}`` declared by an OpenAPI document on disk."""
    document = json.loads(contract.read_text(encoding="utf-8"))
    return {
        (method.upper(), normalise(path))
        for path, item in document.get("paths", {}).items()
        for method in item
        if method.lower() in HTTP_METHODS
    }


def published_raw(contract: Path) -> set[tuple[str, str]]:
    """The same set with paths left exactly as the contract spells them."""
    document = json.loads(contract.read_text(encoding="utf-8"))
    return {
        (method.upper(), path)
        for path, item in document.get("paths", {}).items()
        for method in item
        if method.lower() in HTTP_METHODS
    }


def served_operations(app: FastAPI) -> set[tuple[str, str]]:
    """``{(METHOD, normalised path)}`` the built application actually answers."""
    return {
        (method.upper(), normalise(path))
        for path, item in app.openapi().get("paths", {}).items()
        for method in item
        if method.lower() in HTTP_METHODS
    }


def divergence(served: set[tuple[str, str]], published: set[tuple[str, str]]) -> str:
    """A message naming BOTH differences, with the counts each side actually iterated."""
    unserved = sorted(f"{method} {path}" for method, path in published - served)
    unpublished = sorted(f"{method} {path}" for method, path in served - published)
    return (
        f"served {len(served)} operation(s), contract publishes {len(published)}; "
        f"published but NOT served: {unserved or 'none'}; "
        f"served but NOT published: {unpublished or 'none'}"
    )


def _probe_endpoint() -> dict[str, Any]:  # pragma: no cover - never called, only mounted
    return {}


def probe_app_for(contract: Path) -> FastAPI:
    """A synthetic app that serves exactly what ``contract`` publishes.

    This is the sweep's arming device and it is not optional. Three sweeps in this repo were
    found going QUIET rather than red — a loop that iterates zero cases and passes — so the
    extractor is pointed at an app whose served set is *known*, built out of the very paths
    under test. If :func:`served_operations` ever stops seeing routes, or the two sides ever
    normalise differently, the control below fails instead of the real gate silently agreeing
    that ``set() == set()``.
    """
    app = FastAPI(title="probe")
    for method, path in sorted(published_raw(contract)):
        app.add_api_route(path, _probe_endpoint, methods=[method])
    return app


# =============================================================================================
# Arming — NOT xfail. This one must be green, and stay green, for the gate below to mean
# anything at all.
# =============================================================================================


def test_the_served_versus_published_sweep_is_armed() -> None:
    """The contract is non-empty and the extractor can see routes when there are routes.

    Two failure modes this closes, both observed elsewhere in this repo:

    * a contract that parses to zero operations, so ``published - served`` is empty and the
      real gate below XPASSes for the wrong reason;
    * an extractor that returns nothing for structural reasons (a changed FastAPI, a
      swallowed exception in ``app.openapi()``), so ``served`` is empty for every app and the
      gate can never distinguish "serves nothing" from "cannot be measured".

    The probe is built from the contract's own raw paths, so it also proves the two sides
    normalise identically — the one way a set comparison can be wrong without being empty.
    """
    raw = published_raw(STORE_AGENT_OPENAPI)
    published = published_operations(STORE_AGENT_OPENAPI)
    assert published, f"{STORE_AGENT_OPENAPI} declares no operations; the sweep would be blind"

    # Normalisation must be INJECTIVE, or the comparison silently shrinks. Two distinct
    # published paths that normalise to one string collapse identically on BOTH sides, so the
    # probe check below still passes while an app serving only one of them satisfies
    # ``served == published`` with the other door 404ing.
    assert len(published) == len(raw), (
        f"normalising path parameters collapsed {len(raw)} published operations onto "
        f"{len(published)} — two distinct contract paths differ only in the NAME of a path "
        f"parameter, so the comparison can no longer tell them apart. Raw: "
        f"{sorted(f'{m} {p}' for m, p in raw)}"
    )

    probe = served_operations(probe_app_for(STORE_AGENT_OPENAPI))
    assert probe, "the served-path extractor returned nothing for an app built with routes"
    assert probe == published, (
        "the extractor and the contract reader disagree on an app built from the contract "
        f"itself: {divergence(probe, published)}"
    )


# =============================================================================================
# T-309 — the store agent serves ZERO HTTP paths
# =============================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-309: no <feature>/routes.py exists anywhere under packages/store-agent/src, so "
        "create_app() mounts nothing and app.openapi()['paths'] is empty — the contract's "
        "POST /v1/bid-requests, the door the exchange solicits stores through, is answered by "
        "no server at all and receive_bid is reachable only as a library call; remove this "
        "marker with the fix"
    ),
)
def test_t309_the_store_agent_serves_every_path_its_contract_publishes() -> None:
    """A fully built, fully tested door with no transport in front of it.

    ``packages/store-agent/src/external/door.py`` implements ``receive_bid`` — six gates,
    signature verification, nonce replay, price reconciliation — and the frozen acceptance
    suite exercises it directly at ``.swarm-loop/acceptance/test_e4_store_agent.py``. None of
    that is in question. What is missing is the *server*: ``main.create_app()`` mounts every
    ``<feature>/routes.py`` beside it, and there is no such file under
    ``packages/store-agent/src`` at all, so the app it builds has an empty route table.

    Measured at HEAD::

        >>> store_agent.main.create_app().state.mounted_routers
        []
        >>> sorted(store_agent.main.create_app().openapi()['paths'])
        []

    against a contract that publishes ``POST /v1/bid-requests``. That is the door the exchange
    solicits stores through, so the auction's whole solicitation leg is a library call between
    two processes that have no way to reach each other. This is distinct from ``receive_bid``
    being unwired in production: the function is not merely unwired, there is no route that
    could wire it.

    The assertion is the general property — served set and published set agree, in both
    directions — so it keeps grading the surface as it grows instead of grading one frozen
    name. Any repair passes: a ``routes.py`` under any feature directory that answers the
    published operations. What is refused is a published door nothing answers, and equally a
    served door no contract declares.
    """
    from store_agent.main import create_app  # noqa: PLC0415 - measured at call time, not import

    app = create_app()
    served = served_operations(app)
    published = published_operations(STORE_AGENT_OPENAPI)

    assert published, "the contract declares nothing; the sweep is unarmed (see the control)"

    assert served == published, (
        "the store agent's served surface and its published contract do not agree — "
        f"{divergence(served, published)}; mounted routers: "
        f"{getattr(app.state, 'mounted_routers', 'unknown')}"
    )
