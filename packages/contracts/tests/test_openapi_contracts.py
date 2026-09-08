"""Consumer contract tests for every cross-domain endpoint.

An OpenAPI document nobody checks is documentation, and documentation drifts. Three things make
these a contract instead:

* every route DESIGN §Interfaces pins is present, and nothing extra is;
* every JSON request and response body carries a checked-in example;
* every example VALIDATES against the schema its operation declares, with `$ref`s into
  `protocol.schema.json` resolved locally.

The third is the one with teeth. A schema change that breaks a consumer usually looks fine in
isolation — the model still validates its own tests — and shows up first as an example that is no
longer a legal `Bid`.
"""

from __future__ import annotations

import json

import pytest

from contracts.openapi import (
    OPENAPI_DIR,
    PINNED_ROUTES,
    documents,
    example_errors,
    iter_examples,
    routes,
)

ALL_EXAMPLES = list(iter_examples())


def test_every_pinned_route_is_declared() -> None:
    missing = sorted(set(PINNED_ROUTES) - set(routes()))
    assert missing == [], f"cross-domain endpoints with no OpenAPI contract: {missing}"


def test_no_route_is_declared_that_design_does_not_pin() -> None:
    """An undocumented extra route is a cross-domain surface nobody agreed to."""
    extra = sorted(set(routes()) - set(PINNED_ROUTES))
    assert extra == [], f"routes declared but not pinned in DESIGN §Interfaces: {extra}"


def test_every_domain_that_serves_routes_has_a_document() -> None:
    """Six, not five. `buyer` joined in the change that published `buyer.openapi.json`.

    The name of this test used to be `test_all_five_domains_have_a_document`, and the count in
    it was the only thing standing between "the buyer publishes no contract" and a green
    build: a service with no document did not fail this assertion, it satisfied it. Spelling
    the set out is what makes a seventh service's absence visible too.
    """
    assert set(documents()) == {
        "buyer",
        "exchange",
        "store-agent",
        "merchant",
        "trust",
        "ingest",
    }


def test_there_are_examples_to_check() -> None:
    """Guards the assertions below against passing vacuously on an empty document set."""
    assert len(ALL_EXAMPLES) >= 25


@pytest.mark.parametrize(
    "example", ALL_EXAMPLES, ids=[f"{e.domain}:{e.method}:{e.path}:{e.where}" for e in ALL_EXAMPLES]
)
def test_every_checked_in_example_validates_against_its_declared_schema(example) -> None:
    problems = example_errors(example)
    assert problems == [], (
        f"{example.domain} {example.method.upper()} {example.path} ({example.where}) has an "
        f"example that no longer satisfies its schema:\n  " + "\n  ".join(problems)
    )


def test_every_json_body_carries_an_example() -> None:
    """A schema with no example is a route no consumer has ever been shown a real payload for."""
    missing: list[str] = []
    for domain, document in documents().items():
        for path, operations in document["paths"].items():
            for method, operation in operations.items():
                if not isinstance(operation, dict):
                    continue
                bodies = []
                request_content = (operation.get("requestBody") or {}).get("content") or {}
                if "application/json" in request_content:
                    bodies.append(("requestBody", request_content["application/json"]))
                for status, response in (operation.get("responses") or {}).items():
                    content = (response.get("content") or {}).get("application/json")
                    if content is not None:
                        bodies.append((f"responses.{status}", content))
                for where, content in bodies:
                    if "schema" in content and "example" not in content:
                        missing.append(f"{domain} {method.upper()} {path} {where}")
    assert missing == [], f"JSON bodies with a schema but no example: {missing}"


def test_the_documents_are_openapi_3_1() -> None:
    for domain, document in documents().items():
        assert document["openapi"].startswith("3.1"), domain


def test_the_documents_are_valid_json_on_disk() -> None:
    """They are checked in and read by tooling in two languages; a stray comma is a build break."""
    paths = sorted(OPENAPI_DIR.glob("*.openapi.json"))
    assert len(paths) == 6
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            json.load(handle)


def test_no_example_reaches_for_the_network() -> None:
    """D3: every `$ref` resolves against the bundle in this repo. A remote `$ref` would make the
    contract tests depend on a host being up."""
    blob = json.dumps({domain: dict(doc) for domain, doc in documents().items()})
    assert "http://" not in blob.replace("http://json-schema.org", "")
    for ref_host in ("https://spec.openapis.org", "https://raw.githubusercontent.com"):
        assert ref_host not in blob


def test_the_external_door_documents_the_required_envelope() -> None:
    """D52: the one route that carries a SigningEnvelope must say so in its contract."""
    operation = documents()["exchange"]["paths"]["/v1/auctions/{auction_id}/bids"]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("#/$defs/SignedBidSubmission")
    example = operation["requestBody"]["content"]["application/json"]["example"]
    for field in ("signer_id", "key_id", "issued_at", "nonce", "schema_version"):
        assert field in example


def test_the_solicitation_route_does_not_carry_an_envelope() -> None:
    """A hosted Tier-1 agent never crosses the external boundary and holds no key to sign with."""
    operation = documents()["store-agent"]["paths"]["/v1/bid-requests"]["post"]
    example = operation["responses"]["200"]["content"]["application/json"]["example"]
    assert set(example).isdisjoint({"signer_id", "key_id", "issued_at", "nonce"})


# ---------------------------------------------------------------------------------------------
# Negative controls.
#
# Everything above asserts `problems == []`. On its own that is unfalsifiable: an `example_errors`
# that returned `[]` unconditionally would satisfy every one of those ~29 cases, and the file that
# calls itself "the one with teeth" would have none. These prove the checker can say no.
# ---------------------------------------------------------------------------------------------


def _one_example(domain: str, path: str, where: str):
    for example in ALL_EXAMPLES:
        if example.domain == domain and example.path == path and example.where == where:
            return example
    raise AssertionError(f"no example for {domain} {path} {where}")


def test_the_example_checker_rejects_a_broken_example() -> None:
    """Corrupt a real example and the checker must object, naming the field."""
    good = _one_example("store-agent", "/v1/bid-requests", "responses.200")
    broken = good._replace(value={**good.value, "store_id": 12345})
    problems = example_errors(broken)
    assert problems, "a Bid whose store_id is a number must not validate as a Bid"
    assert any("store_id" in problem for problem in problems)


def test_the_example_checker_rejects_a_smuggled_field() -> None:
    good = _one_example("store-agent", "/v1/bid-requests", "responses.200")
    broken = good._replace(value={**good.value, "network_fee": 0.0})
    assert example_errors(broken), "the protocol objects close their property sets"


def test_the_example_checker_rejects_a_missing_signing_envelope() -> None:
    """D52 at the contract level: the documented external submission cannot lose a field."""
    good = _one_example("exchange", "/v1/auctions/{auction_id}/bids", "requestBody")
    for field in ("signer_id", "key_id", "issued_at", "nonce", "schema_version"):
        stripped = {k: v for k, v in good.value.items() if k != field}
        assert example_errors(good._replace(value=stripped)), (
            f"an external submission missing {field!r} must fail its own documented schema"
        )


# The two controls below used to corrupt `trust GET /stores/{store_id}/trust responses.200`,
# whose schema was a bare `$ref` to `TrustSnapshot`. T-312 unpinned that operation — it was
# published, served by nothing, and, declaring no identity parameter, unservable as written —
# so they are repointed at `trust GET /snapshot responses.200`.
#
# That is a STRICTER anchor, not a weaker substitute. `/snapshot`'s schema reaches the same
# definition through `additionalProperties: {"$ref": ...TrustSnapshot}`, so the second control
# now proves the resolver follows a `$ref` nested one level inside a subschema rather than only
# at the root. A registry that resolved bare `$ref`s and silently dropped nested ones would have
# passed the old test and fails this one.
def test_the_example_checker_rejects_an_empty_payload() -> None:
    good = _one_example("trust", "/snapshot", "responses.200")
    assert example_errors(good._replace(value={"store-1": {}}))


def test_the_example_checker_resolves_protocol_refs_rather_than_ignoring_them() -> None:
    """If the `$ref` silently failed to resolve, every example would validate against `true` and
    the whole file would be green for the wrong reason."""
    good = _one_example("trust", "/snapshot", "responses.200")
    store_id, snapshot = next(iter(good.value.items()))
    five_dims = {k: v for k, v in snapshot["dims"].items() if k != "catalog_claim_accuracy"}
    broken = {**good.value, store_id: {**snapshot, "dims": five_dims}}
    assert example_errors(good._replace(value=broken)), (
        "a five-dimension TrustSnapshot must fail — if it passes, the $ref is not resolving"
    )
