"""One implementation of the served-versus-published contract sweep (T-321).

Three gate files — ``apps/exchange/tests/test_repro_open_tickets.py``,
``services/ingest/tests/test_repro_open_tickets.py`` and
``packages/store-agent/tests/test_repro_open_tickets.py`` — each ask the same question of a
different service: *does the surface this app actually answers agree, in both directions, with
the OpenAPI document the repo publishes for it?* Every helper that question needs used to be
written out once per file, under two different spellings, with nothing comparing the copies.

**Why that mattered, measured rather than asserted.** The copies drifted once already: the
path normaliser was a regex in ``packages/store-agent`` and a ``str.partition`` loop in the
other two, and the two spellings disagreed on ``/a/{b/{c}`` (``/a/{b/{}`` against ``/a/{}``).
Nothing noticed, because nothing compared them — two services agreed a surface was fine while
the third was measuring something slightly different. Three implementations of one rule is
three things to keep in step and the failure mode is silent, so the rule now lives here once
and the three gates import it.

**Where the copies differed when they were folded together**, so the union is on the record:

* the path normaliser, the operation extractor, the published/served extractors and the
  divergence message were behaviourally identical in all three — measured across all five
  contracts in ``packages/contracts/openapi`` and over ten malformed path shapes, every answer
  equal. ``packages/store-agent`` expressed the raw variant as a second function
  (``published_raw``) where the other two used a ``raw=True`` branch; same rule, one spelling
  kept.
* the probe app's title genuinely differed: ``probe`` in ``packages/store-agent`` against
  ``probe:<contract file name>`` in the other two. The naming one is kept, because a failure
  message that names the contract it built the probe from is strictly more informative and
  nothing asserts the title.

**This module ships.** ``COPY proxyshop_support/ /app/proxyshop_support/`` appears in seven of
this repo's Dockerfiles, so a file added here lands inside the deployable images and is walked
by ``proxyshop_support/tests/test_artifact_copyset.py``. Nothing at module scope may therefore
import anything an image might not install: only the standard library is imported here, and
``fastapi`` is reached lazily inside :func:`contract_probe_app`, which no shipped code path
calls. Keep it that way — an import that works in the tree can be absent from the image.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "HTTP_METHODS",
    "contract_probe_app",
    "normalise_route",
    "operation_divergence",
    "operations",
    "published_operations",
    "served_operations",
]

#: The methods an OpenAPI path item may carry. Everything else under a path item
#: (``parameters``, ``summary``, ``$ref``, ``servers``) is not an operation, and counting it as
#: one would inflate the very non-zero checks that arm the sweeps.
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def normalise_route(path: str) -> str:
    """``/stores/{store_id}/trust`` -> ``/stores/{}/trust``.

    The comparison is about the *wire shape* of a route, not about what a service names its
    path parameter: a service answering ``/stores/{sid}/trust`` genuinely satisfies the
    contract's ``/stores/{store_id}/trust``, and failing it for the spelling would make these
    gates red for a reason none of their tickets is about. Every failure message still prints
    the raw spellings, so a real naming divergence stays visible without being fatal.

    Written with ``str.partition`` rather than a regex, which is not a style preference: the
    regex spelling this replaced DISAGREED with it on malformed input, and that divergence is
    the whole reason this module exists.

    Behaviour on malformed input, stated exactly, because an earlier draft of this docstring
    claimed something else and was wrong: a ``{`` with no ``}`` anywhere after it is passed
    through unchanged, but ``/a/{b/{c}`` collapses ``b/{c`` into a single ``{}`` — the first
    ``{`` pairs with the only ``}``. Nothing in the five contracts is shaped like that today,
    and the injectivity checks in the callers' arming tests are what keep a future one from
    collapsing two distinct paths onto one string unnoticed.
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


def operations(paths: dict[str, Any], *, raw: bool = False) -> set[tuple[str, str]]:
    """``{(METHOD, path)}`` from an OpenAPI ``paths`` object, normalised unless ``raw``."""
    return {
        (method.upper(), path if raw else normalise_route(path))
        for path, item in paths.items()
        for method in item
        if method.lower() in HTTP_METHODS
    }


def published_operations(contract: Path, *, raw: bool = False) -> set[tuple[str, str]]:
    """What an OpenAPI document on disk declares.

    ``raw=True`` leaves paths exactly as the contract spells them, which is what
    :func:`contract_probe_app` needs to mount real routes.
    """
    document = json.loads(contract.read_text(encoding="utf-8"))
    return operations(document.get("paths", {}), raw=raw)


def served_operations(app: Any) -> set[tuple[str, str]]:
    """What a built FastAPI application actually answers, read off its published schema."""
    return operations(app.openapi().get("paths", {}))


def operation_divergence(served: set[tuple[str, str]], published: set[tuple[str, str]]) -> str:
    """A message naming BOTH differences, and the counts each side actually iterated."""
    unserved = sorted(f"{method} {path}" for method, path in published - served)
    unpublished = sorted(f"{method} {path}" for method, path in served - published)
    return (
        f"served {len(served)} operation(s), contract publishes {len(published)}; "
        f"published but NOT served: {unserved or 'none'}; "
        f"served but NOT published: {unpublished or 'none'}"
    )


def contract_probe_app(contract: Path) -> Any:
    """A synthetic app serving exactly what ``contract`` publishes — the sweeps' arming device.

    Three sweeps in this repo were found going QUIET rather than red (T-229 6->0 of 8, T-281
    70->0 of 79, T-241 48->0 of 66): a loop that iterates zero cases and passes. A
    served-vs-published comparison has the same hazard in a nastier form, because
    ``set() == set()`` is a *pass*. Pointing :func:`served_operations` at an app whose served
    set is known — built out of the very paths under test — is what makes an empty ``served``
    mean "this service serves nothing" rather than "this probe can no longer see routes".

    ``fastapi`` is imported inside the function on purpose: this module ships inside seven
    container images and nothing at its module scope may need a wheel an image might not
    install.
    """
    from fastapi import FastAPI  # noqa: PLC0415 - see the module docstring: this module ships

    def _probe() -> dict[str, Any]:  # pragma: no cover - mounted, never called
        return {}

    app = FastAPI(title=f"probe:{contract.name}")
    for method, path in sorted(published_operations(contract, raw=True)):
        app.add_api_route(path, _probe, methods=[method])
    return app
