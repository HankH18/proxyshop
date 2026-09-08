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


#: Every route this platform publishes a contract for. The first block is DESIGN §Interfaces
#: "Service APIs" verbatim — the cross-domain checklist; an endpoint missing from it is one no
#: consumer has an example for.
#:
#: The second block is the correction T-266/T-312/T-317 forced, and it is NOT decoration. This
#: tuple is compared against the documents in BOTH directions
#: (`test_no_route_is_declared_that_design_does_not_pin`), so for years it doubled as the answer
#: to "what may be declared" — and the answer it gave was "only what crosses a domain". Measured
#: on this branch, four services were answering SIXTEEN operations that no document declared:
#: the exchange's auction read (which `proxyshop_demo/s1.py` drives over HTTP), the trust
#: ledger's five reads plus its claim-verification producer, ingest's `er` and `extraction`
#: routers, and the merchant's OAuth install pair plus its administrative shop list. Every one
#: of them is reachable by anyone who can reach the service. "It does not cross a domain" was
#: never a reason for a served route to escape review — it was only a reason nobody had written
#: it down.
#:
#: So the rule this list now encodes is the one the served-vs-published gates grade: a route the
#: service ANSWERS is declared here. DESIGN still owns which routes are cross-domain; it does
#: not own which are reachable.
PINNED_ROUTES: tuple[Route, ...] = (
    # DESIGN §Interfaces, "Service APIs" — the cross-domain contract.
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
    Route("trust", "get", "/snapshot"),
    # T-312 UNPINNED two trust routes DESIGN §Interfaces still lists, and the reason is not
    # "nobody got to them". Each was published, served by nothing, and unservable AS PUBLISHED:
    #
    #   * `GET /stores/{store_id}/trust` declares no identity parameter of any kind, so serving
    #     it as written hands any anonymous caller any store's full per-dimension posture. The
    #     read that door describes is R9's — a merchant reading its OWN score and why. This
    #     comment used to add that R9's dashboard did not exist; it does now
    #     (`apps/merchant/app/dashboard/`: `Dashboard.tsx`, `Cards.tsx`, `api.ts`), and it reads
    #     the merchant service's own same-origin `GET /stores/{store_id}/dashboard` rather than
    #     this trust path — so the missing caller was never the reason for the unpin and the
    #     arrival of one is not a reason to reverse it. The identity parameter still is.
    #     `GET /snapshot` cannot stand in for it either: it answers every store at once, which is
    #     the one shape a shop-facing read must not have.
    #   * `POST /feedback/{order_ref}` declares `matched_pitch` / `reason` /
    #     `pseudonymous_context` and no routing evidence, so serving it as written takes R14
    #     feedback from a buyer the network never routed. The routed-buyer gate already exists,
    #     once, in `buyer_svc.feedback.submission.submit_feedback`; a second decider is the
    #     failure `apps/buyer/app/feedback/feedback.ts` names in its own header. Trust's real
    #     feedback intake is `POST /events` with `kind: "feedback"`, which `trust.feedback`
    #     folds into `feedback_match`.
    #
    # Re-pin either one in the change that serves it — with an identity parameter, and with the
    # caller that reads it. `apps/trust/tests/test_contract_surface.py` holds this ruling.
    Route("ingest", "post", "/refresh/{store_id}"),
    # Served surfaces that were reachable and undeclared until T-266 / T-312 / T-317.
    Route("exchange", "get", "/auctions/{auction_id}"),
    Route("trust", "get", "/events"),
    Route("trust", "get", "/events/head"),
    Route("trust", "get", "/events/verify"),
    Route("trust", "get", "/events/replay"),
    Route("trust", "get", "/events/{event_id}"),
    Route("trust", "post", "/claims/verifications"),
    # R4 + R12's join: where a completed purchase becomes a trust update. Unauthenticated and
    # appending to a hash-chained ledger, which is the strongest case in this service for a
    # route being written down. Served by `trust.reconcile.routes` since the S1 back half.
    Route("trust", "get", "/reconcile"),
    Route("trust", "post", "/reconcile"),
    Route("ingest", "get", "/er/config"),
    Route("ingest", "post", "/er/match"),
    Route("ingest", "post", "/er/resolve"),
    Route("ingest", "get", "/extraction/config"),
    Route("ingest", "post", "/extraction/policy-pages"),
    Route("ingest", "post", "/extraction/stores/{store_id}"),
    Route("ingest", "get", "/schedule"),
    Route("ingest", "post", "/schedule/tick"),
    Route("merchant", "get", "/install"),
    Route("merchant", "get", "/install/callback"),
    Route("merchant", "get", "/install/shops"),
    # R9's dashboard, declared in the change that serves it — which is what the note further
    # down this tuple asks for ("Re-pin either one in the change that serves it — with an
    # identity parameter, and with the caller that reads it"). Both conditions are met here:
    # the store is a path parameter on each, and the caller is `apps/merchant/app/dashboard`,
    # the SPA the merchant service now serves at `/dashboard` from its own vite build.
    #
    # The read is ONE operation rather than five because the page needs the envelope, the
    # losses, the trust snapshot, the trust payloads and the bid journal to render at all;
    # five doors would mean five partial states in a browser and five places for a refusal to
    # be swallowed into an empty chart.
    #
    # The bundle itself is NOT here and owes no contract: it is served by a `Mount`, which
    # declares no operation. If that mount ever becomes an ASGI sub-application with routes of
    # its own, those routes are served operations and belong in this tuple;
    # `apps/merchant/svc/tests/test_dashboard.py` asserts it has none.
    Route("merchant", "get", "/stores/{store_id}/dashboard"),
    Route("merchant", "post", "/stores/{store_id}/bids/solicit"),
    # R13's receiving end: the door trust pushes one store's own trust delta through.
    # Reachable by anyone who can reach a hosted agent, so it is declared here for the
    # same reason the sixteen above are.
    Route("store-agent", "post", "/v1/trust-events"),
    # The BUYER service, which until this entry published no document at all — not a partial
    # one, not one that omitted a route: `packages/contracts/openapi/` held five files and
    # `buyer.openapi.json` was not among them, while `buyer_svc.main` mounted six routers and
    # answered fourteen operations. Every gap the three sweeps above found was a service
    # publishing LESS than it served; this was a whole service outside the review process,
    # including its unauthenticated login door (`POST /buyer/auth/magic-link`), the session
    # trio behind `X-Buyer-Session`, and `POST /buyer/shortlist/accept`, which is the frame
    # that decides where a shopper's browser is sent.
    #
    # The rule this tuple encodes did not change to accommodate them — a route the service
    # ANSWERS is declared here, and these are answered. `apps/buyer/svc/tests/
    # test_openapi_contract.py` is the served-vs-published gate, the same class as T-317's for
    # the merchant, and it grades BOTH directions off `app.routes` rather than `app.openapi()`
    # so a route hidden with `include_in_schema=False` could not pass by disappearing.
    Route("buyer", "post", "/buyer/shortlist/render"),
    Route("buyer", "post", "/buyer/shortlist/accept"),
    Route("buyer", "get", "/buyer/auctions/{auction_id}"),
    Route("buyer", "post", "/buyer/auth/magic-link"),
    Route("buyer", "post", "/buyer/auth/session"),
    Route("buyer", "get", "/buyer/auth/session"),
    Route("buyer", "delete", "/buyer/auth/session"),
    Route("buyer", "get", "/buyer/profile"),
    Route("buyer", "post", "/buyer/feedback/prompt"),
    Route("buyer", "post", "/buyer/feedback"),
    Route("buyer", "post", "/buyer/intent/clarify"),
    Route("buyer", "post", "/buyer/intent/confirm"),
    Route("buyer", "post", "/buyer/livecheck/run"),
    Route("buyer", "get", "/buyer/livecheck/{auction_id}"),
    # R9's merchant-facing report door, and neither block above is the honest home for it.
    #
    # NOT the DESIGN §Interfaces block: DESIGN's exchange line pins five routes and this is not
    # one of them, so filing it there would make this tuple assert that DESIGN pins a path
    # DESIGN does not mention. (DESIGN arguably SHOULD — the caller is a shop, not the exchange
    # itself — but that is an edit to a file this entry does not own, and mis-filing the row is
    # not how to request it.)
    #
    # NOT the "served surfaces that were reachable and undeclared until T-266 / T-312 / T-317"
    # block either, and this is where the recent `POST /v1/trust-events` precedent stops
    # applying: that heading is a dated FINDING about routes four services were already
    # answering when those three tickets measured them. `GET /reports/losses` was not among
    # them and could not have been — it did not exist. It landed with R9's loss reports (a
    # producer writing rows at every auction close, `reports.log.record_losses`; a bounded log;
    # this door) and it landed RED on purpose, served and published nowhere, rather than hidden
    # behind `include_in_schema=False` or mounted only when a token file is configured. Filing
    # it under that heading would date a brand-new route to a sweep it postdates.
    #
    # What DOES fit is the rule this docstring states above: a route the service ANSWERS is
    # declared here. It is merchant-AUTHENTICATED rather than internal — the token table is
    # `{store_id: token}` and the subject store is resolved from the bearer, which is why the
    # published operation carries an `Authorization` header parameter and no `store_id` of any
    # kind — and an authenticated door is the last thing that should be reachable without a
    # contract review.
    Route("exchange", "get", "/reports/losses"),
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
