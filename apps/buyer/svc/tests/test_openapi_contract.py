"""The buyer service serves nothing that no published contract declares.

This is T-317's gate, extended to the buyer. T-317 established the rule for the merchant —
*a route the service ANSWERS is declared in `packages/contracts/openapi/`* — and repaired
three undeclared routes there. The buyer was in a worse state than the merchant ever was: it
published **no document at all**. `packages/contracts/openapi/` held five files, none of them
`buyer.openapi.json`, while `buyer_svc.main` mounted six routers and answered fourteen
operations, including an unauthenticated login door, the three session operations behind
`X-Buyer-Session`, and `POST /buyer/shortlist/accept` — the frame that decides where a
shopper's browser is sent. Nothing in the contract-review process had ever looked at any of
them, and `packages/contracts/tests/test_openapi_contracts.py` was green throughout, because
a service with no document does not fail a test that enumerates the documents that exist.

Three properties, and the first is a control that must PASS on its own:

1. the comparison machinery works in both directions and against a real app and a real
   document (:func:`test_the_buyer_route_comparison_is_armed`);
2. nothing is served that the contract does not declare;
3. nothing is declared that the service does not serve.

The routes are read off ``app.routes`` and NOT off ``app.openapi()``, for the reason T-317's
gate gives: the schema is what a service *documents*, so a route carrying
``include_in_schema=False`` is invisible to it, and this gate exists precisely to find routes
nothing reviews. Grading the schema would make one keyword argument a green button.

`test_the_published_schemas_are_the_ones_the_app_generates` is the other half, and it is what
keeps the document from being hand-written prose that drifts: every request and response body
in `buyer.openapi.json` is compared against the schema FastAPI generates from the Pydantic
model the route actually declares. A model field added, removed or retyped fails here.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: HTTP methods an OpenAPI path item can carry. `parameters` and `summary` are path-item keys
#: too and are not operations, so a plain `for method in item` over-counts.
_OPERATION_METHODS = frozenset(
    {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
)

#: Endpoints FastAPI mounts for itself. Excluded by exact path because they are the
#: framework's, not the buyer's — no contract review is owed them and no ticket is about them.
#: Listed rather than pattern-matched so a real route can never fall through by resembling one.
_FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})


def _normalize_path(path: str) -> str:
    """A route path in the contract's spelling, converter suffixes removed.

    Starlette keeps the converter in the raw path while OpenAPI spells the same parameter
    ``{name}``. No buyer route declares a converter today; the normalisation is kept anyway so
    that adding one (``{auction_id:path}``) does not report the one route both sides DO agree
    on as a mismatch, which would be a false red rather than a finding.
    """
    return re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", path)


def _served_operations() -> set[tuple[str, str]]:
    """Every ``(method, path)`` the buyer app actually answers.

    The walk follows ``original_router``, and that is not defensive coding — it is measured.
    This FastAPI version wraps each ``include_router`` in an ``_IncludedRouter`` that carries
    no ``.path`` and no ``.routes`` of its own and holds the real ``APIRouter`` as
    ``.original_router``. A flat scan of ``app.routes`` therefore sees ONLY the four framework
    endpoints and reports the buyer as serving nothing, which the armed control below is what
    catches. Both spellings are followed so this survives a FastAPI upgrade in either
    direction.
    """
    from buyer_svc.main import create_app  # noqa: PLC0415

    served: set[tuple[str, str]] = set()

    def walk(routes: Any, depth: int = 0) -> None:
        if depth > 10:  # pragma: no cover - a cycle would be a framework bug, not a finding
            raise AssertionError("the buyer route tree nests deeper than 10; refusing to walk")
        for route in routes or ():
            wrapped = getattr(route, "original_router", None)
            nested = getattr(route, "routes", None) or getattr(wrapped, "routes", None)
            if nested:
                walk(nested, depth + 1)
                continue
            raw = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if not raw or not methods or raw in _FRAMEWORK_PATHS:
                continue
            path = _normalize_path(raw)
            served.update(
                (method.lower(), path) for method in methods if method.lower() in _OPERATION_METHODS
            )

    walk(create_app().routes)
    return served


def _published_document() -> dict[str, Any]:
    """The pinned buyer contract, through the repo's own loader.

    Read through ``contracts.openapi`` rather than by re-opening the JSON: the document is
    ``packages/contracts/openapi/buyer.openapi.json``, which is outside this service's tree,
    and that is the point — the thing this gate grades the service against is not a file the
    service's own lane can edit.
    """
    from contracts.openapi import documents  # noqa: PLC0415

    return dict(documents()["buyer"])


def _published_operations() -> set[tuple[str, str]]:
    """Every ``(method, path)`` the pinned buyer contract declares."""
    paths = _published_document()["paths"]
    return {
        (method.lower(), path)
        for path, item in paths.items()
        for method in item
        if method.lower() in _OPERATION_METHODS
    }


def test_the_buyer_route_comparison_is_armed() -> None:
    """Control, and it must PASS. The comparison machinery works both ways.

    A red below has to mean "the service serves something the contract does not declare". It
    must not be able to mean "the app would not build", "the contract would not load", or "the
    two are described in different vocabularies and nothing ever matches".
    """
    served = _served_operations()
    published = _published_operations()

    assert served, "the buyer app served no operations at all"
    assert published, "the pinned buyer contract declared no operations at all"
    assert len(served & published) >= 3, (
        f"served and published overlap in only {sorted(served & published)}, which is too "
        "little to trust a diff between them"
    )


def test_the_buyer_serves_no_route_that_the_contract_does_not_declare() -> None:
    """The T-317 direction: reachable and unreviewed.

    Undeclared served routes are a security and review surface — nothing in the contract
    review process ever looks at them. When this went red the answer was to PUBLISH, not to
    stop serving: all fourteen are load-bearing, and one of them is how a shopper signs in.
    """
    undeclared = sorted(_served_operations() - _published_operations())
    assert undeclared == [], (
        "the buyer answers operations that appear in no published contract: "
        f"{undeclared}. Add them to packages/contracts/openapi/buyer.openapi.json and to "
        "contracts.openapi.PINNED_ROUTES, or stop serving them."
    )


def test_the_contract_declares_no_buyer_route_the_service_does_not_serve() -> None:
    """The other direction: a published door that answers 404.

    A contract that promises a route nothing serves is worse than no contract — a consumer
    written against it fails at runtime with nothing to read.
    """
    unserved = sorted(_published_operations() - _served_operations())
    assert unserved == [], f"the buyer contract pins routes the service does not answer: {unserved}"


# ---------------------------------------------------------------------------------------------
# The document is GENERATED, not narrated.
# ---------------------------------------------------------------------------------------------
def _dereference(schema: Any, components: dict[str, Any], depth: int = 0) -> Any:
    """A FastAPI schema with its ``#/components/schemas/...`` refs inlined.

    The published document carries no ``components`` section — the other five do not either,
    and ``contracts.openapi.example_errors`` validates each operation's schema fragment
    STANDALONE, so an intra-document ``$ref`` would not resolve. Inlining is what lets the same
    checked-in example machinery grade the buyer's bodies as it grades everyone else's.
    """
    if depth > 20:  # pragma: no cover - the buyer models are acyclic; this is a guard
        raise AssertionError("buyer schema nests deeper than 20 levels")
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            target = components[ref.rsplit("/", 1)[1]]
            rest = {key: value for key, value in schema.items() if key != "$ref"}
            return {**_dereference(target, components, depth + 1), **rest}
        return {key: _dereference(value, components, depth + 1) for key, value in schema.items()}
    if isinstance(schema, list):
        return [_dereference(item, components, depth + 1) for item in schema]
    return schema


def _generated_bodies() -> dict[tuple[str, str, str], Any]:
    """``(method, path, where) -> schema``, dereferenced, straight off the live app."""
    from buyer_svc.main import create_app  # noqa: PLC0415

    document = create_app().openapi()
    components = (document.get("components") or {}).get("schemas") or {}
    bodies: dict[tuple[str, str, str], Any] = {}
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method.lower() not in _OPERATION_METHODS:
                continue
            content = ((operation.get("requestBody") or {}).get("content") or {}).get(
                "application/json"
            )
            if content and "schema" in content:
                bodies[(method.lower(), path, "requestBody")] = _dereference(
                    content["schema"], components
                )
            for status, response in (operation.get("responses") or {}).items():
                content = ((response.get("content") or {}) or {}).get("application/json")
                if content and "schema" in content:
                    bodies[(method.lower(), path, f"responses.{status}")] = _dereference(
                        content["schema"], components
                    )
    return bodies


def test_the_published_schemas_are_the_ones_the_app_generates() -> None:
    """Every published body schema is FastAPI's, verbatim — not a hand-written retelling.

    This is what stops the document becoming documentation. A field added to ``RenderedSlot``,
    a type widened on ``AcceptBody``, a response model swapped — each changes what FastAPI
    generates, and each fails here until the document is regenerated. It is the buyer's
    equivalent of ``contracts.codegen --check``.

    Only bodies the DOCUMENT declares are graded, in both directions per operation: the
    document may document a refusal FastAPI knows nothing about (a 502 the handler raises by
    hand), and that is the repo's own house style. What it may not do is publish a body schema
    that disagrees with the model the route actually validates against.

    **422 is excluded, and the exclusion is the finding rather than a concession.** FastAPI
    generates ``HTTPValidationError`` for it — ``{"detail": [{loc, msg, type}, ...]}`` — and
    that is true only of the 422s FastAPI's own request validation raises. The handlers raise
    422 as well, with ``HTTPException(422, detail="a sentence")``, and both reach a client
    under that one status. Pinning the document to the generated shape would publish a schema
    that real served traffic violates, which is the failure mode this whole file exists to
    prevent. The published 422 is instead true of both, and
    :func:`test_the_published_422_is_true_of_both_producers` drives each of them.
    """
    generated = _generated_bodies()
    document = _published_document()

    mismatched: list[str] = []
    graded = 0
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method.lower() not in _OPERATION_METHODS:
                continue
            wheres: list[tuple[str, Any]] = []
            content = ((operation.get("requestBody") or {}).get("content") or {}).get(
                "application/json"
            )
            if content and "schema" in content:
                wheres.append(("requestBody", content["schema"]))
            for status, response in (operation.get("responses") or {}).items():
                content = ((response.get("content") or {}) or {}).get("application/json")
                if content and "schema" in content:
                    wheres.append((f"responses.{status}", content["schema"]))
            for where, published in wheres:
                if where == "responses.422":
                    continue  # two producers, one status — see this test's docstring
                expected = generated.get((method.lower(), path, where))
                if expected is None:
                    continue  # a refusal the handler raises by hand; FastAPI declares no model
                graded += 1
                if published != expected:
                    mismatched.append(
                        f"{method.upper()} {path} {where}:\n"
                        f"   published={json.dumps(published, sort_keys=True)[:400]}\n"
                        f"   generated={json.dumps(expected, sort_keys=True)[:400]}"
                    )

    assert graded >= 20, (
        f"only {graded} published bodies were compared against the app's own schemas, which is "
        "too few for this test to be evidence of anything"
    )
    assert mismatched == [], (
        "the published buyer contract disagrees with the schemas the app generates. Do not "
        "hand-edit the document — regenerate it from `create_app().openapi()`:\n"
        + "\n".join(mismatched)
    )


@pytest.mark.parametrize(
    "field, expected",
    [("openapi", "3.1"), ("x-domain", "buyer")],
)
def test_the_document_identifies_itself(field: str, expected: str) -> None:
    """``x-domain`` is how ``contracts.openapi.documents()`` keys it; a typo hides the file."""
    assert str(_published_document()[field]).startswith(expected)


def test_the_published_422_is_true_of_both_producers() -> None:
    """One status, two producers, and the published schema has to admit both.

    ``POST /buyer/intent/clarify`` reaches each of them from the same door:

    * ``{"turns": []}`` fails ``Field(min_length=1)``, so ``_BoundedBodyRoute`` answers with
      its bounded rendering of a validation error — ``detail`` is an ARRAY of
      ``{loc, msg, type}``, which is the shape FastAPI generates a model for;
    * ``{"turns": ["   "]}`` parses, reaches the handler, and raises ``EmptyDialogue``, which
      becomes ``HTTPException(422, detail="…")`` — ``detail`` is a STRING, a shape FastAPI
      knows nothing about and would never have declared.

    A document that published only the first would be green here and false on the wire for
    every handler-raised 422 in the service, which is exactly the class of defect the buyer's
    missing contract was an instance of. Both bodies are DRIVEN, not constructed.
    """
    from buyer_svc.main import create_app  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from jsonschema import Draft202012Validator  # noqa: PLC0415

    schema = _published_document()["paths"]["/buyer/intent/clarify"]["post"]["responses"]["422"][
        "content"
    ]["application/json"]["schema"]
    validator = Draft202012Validator(schema)

    with TestClient(create_app()) as client:
        validation = client.post("/buyer/intent/clarify", json={"turns": []})
        handler = client.post("/buyer/intent/clarify", json={"turns": ["   "]})

    assert validation.status_code == 422, validation.text
    assert handler.status_code == 422, handler.text
    assert isinstance(validation.json()["detail"], list), (
        f"the validation 422 stopped carrying a list: {validation.json()}"
    )
    assert isinstance(handler.json()["detail"], str), (
        f"the handler 422 stopped carrying a sentence: {handler.json()}"
    )

    for label, response in (("validation", validation), ("handler", handler)):
        problems = sorted(
            validator.iter_errors(response.json()),
            key=lambda error: list(error.absolute_path),
        )
        assert problems == [], (
            f"the published 422 refuses the {label}-produced body the service really serves: "
            f"{response.json()} -> "
            + "; ".join(
                f"{'.'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
                for e in problems
            )
        )


def test_every_published_example_is_a_body_the_service_could_have_served() -> None:
    """Sanity floor on the examples: there are many, and they are structured objects.

    ``packages/contracts/tests/test_openapi_contracts.py`` validates each one against its
    declared schema. This only guards against the document degenerating into a handful of
    trivial examples that would satisfy that check while showing a consumer nothing.
    """
    document = _published_document()
    examples = [
        content["example"]
        for item in document["paths"].values()
        for method, operation in item.items()
        if method.lower() in _OPERATION_METHODS
        for content in (
            [((operation.get("requestBody") or {}).get("content") or {}).get("application/json")]
            + [
                ((response.get("content") or {}) or {}).get("application/json")
                for response in (operation.get("responses") or {}).values()
            ]
        )
        if content and "example" in content
    ]
    assert len(examples) >= 25, f"only {len(examples)} checked-in examples for 14 operations"
