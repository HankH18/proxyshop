"""The pinned cross-domain routes, and the checked-in examples that make them a contract.

An OpenAPI document that nobody checks is documentation, and documentation drifts. What makes
`packages/contracts/openapi/*.json` a contract instead is:

1. every route DESIGN §Interfaces pins appears in exactly one document — `PINNED_ROUTES` below is
   read straight off DESIGN and the consumer contract tests compare it to what is on disk, so a
   route that quietly disappears fails the build;
2. every JSON request and response body carries a checked-in `example`;
3. every example is VALIDATED against the schema its operation declares, in both languages, with
   `$ref`s into `protocol.schema.json` resolved. An example that stops being a legal `Bid` is the
   earliest possible warning that a schema change broke a consumer, and it costs nothing to have.

`resolver()` is what makes (3) work: it hands `jsonschema` a registry in which
`https://proxyshop.dev/schemas/protocol.schema.json` resolves to the local bundle, so no network
lookup ever happens (D3) and the examples validate against the exact bytes in this repo.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from contracts.registry import PROTOCOL_SCHEMA_ID, protocol_schema

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_DIR = PACKAGE_ROOT / "openapi"


class Route(NamedTuple):
    """One pinned cross-domain endpoint."""

    domain: str
    method: str
    path: str


#: Every route DESIGN §Interfaces pins under "Service APIs". Read it as the checklist it is: a
#: cross-domain endpoint that is not here is one no consumer has an example for.
PINNED_ROUTES: tuple[Route, ...] = (
    Route("exchange", "post", "/auctions"),
    Route("exchange", "get", "/auctions/{auction_id}/shortlist"),
    Route("exchange", "post", "/auctions/{auction_id}/accept"),
    Route("exchange", "post", "/v1/auctions/{auction_id}/bids"),
    Route("exchange", "post", "/internal/outcomes"),
    Route("store-agent", "post", "/v1/bid-requests"),
    Route("merchant", "post", "/codes"),
    Route("merchant", "post", "/webhooks/shopify/{topic}"),
    Route("merchant", "post", "/pixel/collect"),
    Route("merchant", "get", "/stores/{store_id}/envelope"),
    Route("merchant", "put", "/stores/{store_id}/envelope"),
    Route("merchant", "post", "/stores/{store_id}/kill"),
    Route("trust", "post", "/events"),
    Route("trust", "get", "/stores/{store_id}/trust"),
    Route("trust", "get", "/snapshot"),
    Route("trust", "post", "/feedback/{order_ref}"),
    Route("ingest", "post", "/refresh/{store_id}"),
)

_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


@lru_cache(maxsize=1)
def documents() -> Mapping[str, Mapping[str, Any]]:
    """Every OpenAPI document in this package, keyed by its `x-domain`."""
    loaded: dict[str, Mapping[str, Any]] = {}
    for path in sorted(OPENAPI_DIR.glob("*.openapi.json")):
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
        domain = document.get("x-domain") or path.name.split(".", 1)[0]
        loaded[str(domain)] = document
    return loaded


def routes() -> tuple[Route, ...]:
    """Every route actually declared across the checked-in documents."""
    found: list[Route] = []
    for domain, document in documents().items():
        for path, operations in (document.get("paths") or {}).items():
            for method in _HTTP_METHODS:
                if method in operations:
                    found.append(Route(domain, method, path))
    return tuple(sorted(found))


class Example(NamedTuple):
    """One checked-in example and the schema it must satisfy."""

    domain: str
    method: str
    path: str
    where: str
    schema: Mapping[str, Any]
    value: Any


def iter_examples() -> Iterator[Example]:
    """Every `application/json` body that carries both a schema and an example."""
    for domain, document in documents().items():
        for path, operations in (document.get("paths") or {}).items():
            for method in _HTTP_METHODS:
                operation = operations.get(method)
                if not isinstance(operation, Mapping):
                    continue

                body = operation.get("requestBody") or {}
                content = (body.get("content") or {}).get("application/json")
                if isinstance(content, Mapping) and "schema" in content and "example" in content:
                    yield Example(
                        domain, method, path, "requestBody", content["schema"], content["example"]
                    )

                for status, response in (operation.get("responses") or {}).items():
                    content = ((response.get("content") or {}) or {}).get("application/json")
                    if (
                        isinstance(content, Mapping)
                        and "schema" in content
                        and "example" in content
                    ):
                        yield Example(
                            domain,
                            method,
                            path,
                            f"responses.{status}",
                            content["schema"],
                            content["example"],
                        )


@lru_cache(maxsize=1)
def resolver() -> Any:
    """A `referencing` registry in which the protocol bundle resolves LOCALLY, never over HTTP."""
    from referencing import Registry, Resource

    resource = Resource.from_contents(
        dict(protocol_schema()), default_specification=_DEFAULT_SPECIFICATION
    )
    return Registry().with_resource(PROTOCOL_SCHEMA_ID, resource)


def _default_specification() -> Any:
    from referencing.jsonschema import DRAFT202012

    return DRAFT202012


_DEFAULT_SPECIFICATION = _default_specification()


def example_errors(example: Example) -> list[str]:
    """Every reason one checked-in example does not satisfy its declared schema."""
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(dict(example.schema), registry=resolver())
    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(
            validator.iter_errors(example.value), key=lambda e: list(e.absolute_path)
        )
    ]


__all__ = [
    "OPENAPI_DIR",
    "PINNED_ROUTES",
    "Example",
    "Route",
    "documents",
    "example_errors",
    "iter_examples",
    "resolver",
    "routes",
]
