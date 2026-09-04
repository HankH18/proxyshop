"""Reproduction gates for the open `packages/contracts` findings.

Every test here asserts the behaviour that SHOULD hold and therefore fails against the tree as
it stands. Each carries ``xfail(strict=True)`` so an ordinary run reports ``xfailed`` and the
repo-wide build gate stays green, while the ticket's own gate
(``pytest <file> -q --runxfail -k <name>``) reports a real failure with the test SELECTED. When
the defect is repaired the test XPASSes, which ``strict=True`` turns into a failure — so the
marker cannot outlive the bug and whoever fixes it has to delete it.

Each assertion is deliberately written to accept EVERY defensible repair and to refuse only the
outcome that is actually wrong, because a gate that pins one implementation is a gate that has
to be argued with rather than satisfied.
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Any

import pytest

from packages.contracts import EXTERNAL_PATH, HOSTED_PATH, validate_bid
from packages.contracts.tests._fixtures_protocol import (
    ASSERTED_PROVENANCE,
    HOOK_PROVENANCE,
    make_bid,
    make_claim,
    make_snapshot_table,
)

NOW = "2026-06-01T00:00:00Z"

_CONTRACTS = pathlib.Path(__file__).resolve().parents[1]
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _check(bid: Any, path: str) -> Any:
    return validate_bid(bid, path=path, trust_snapshot=make_snapshot_table(), now=NOW)


# =============================================================================================
# T-161 — a provenance block nested inside a claim's opaque `value` is never walked
# =============================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-161: _source_verdict reads only holder['provenance'], so a seller_asserted block "
        "nested inside a hook-provenanced claim's opaque `value` is neither refused on the "
        "hosted path nor flagged for verification — measured ok=True, reasons=[], "
        "requires_verification=False; remove this marker with the fix"
    ),
)
def test_a_provenance_nested_in_a_claim_value_is_not_invisible_to_the_hosted_door() -> None:
    """Wrapping the evidence one level down must not launder it.

    ``_source_verdict`` (packages/contracts/src/boundary.py) judges exactly one thing: the
    ``provenance`` mapping hanging directly off the holder it was handed. ``Claim.value`` is
    typed ``Any``, so a claim may carry a whole second provenance block *inside* its value, and
    nothing descends into it. Measured on this tree::

        claim = {"key": "x",
                 "value": {"provenance": ASSERTED_PROVENANCE},   # seller_asserted, rank 5
                 "provenance": HOOK_PROVENANCE}                  # owner_statement, rank 1
        validate_bid(make_bid(claims=[claim]), path="hosted", ...)
        -> ok=True  reasons=[]  requires_verification=False  unverified_claim_indexes=[]

    The positive control is the same seller-asserted block written at the top level, which the
    hosted door refuses out of hand with ``hosted_non_hook_provenance:0:seller_asserted``. So
    the identical evidence is refused at one depth and invisible at the next, and the only
    difference is which key it was written under.

    Both defensible repairs satisfy this test and it does not choose between them: refuse the
    bid (R8 applied to the nested source), or admit it and flag it for verification (R18's
    admit-and-flag). A schema decision that forbids provenance-shaped values in ``Claim.value``
    outright also satisfies it, because that path refuses the bid too. What is refused here is
    only the third outcome — a clean ``ok=True`` with nothing said.
    """
    nested = {
        "key": "x",
        "value": {"provenance": dict(ASSERTED_PROVENANCE)},
        "provenance": dict(HOOK_PROVENANCE),
    }

    control = _check(make_bid(claims=[make_claim("x", "y", dict(ASSERTED_PROVENANCE))]), HOSTED_PATH)
    assert control.ok is False, (
        "positive control: the same seller_asserted block at the top level must still be "
        f"refused on the hosted path, got {control.reasons}"
    )

    result = _check(make_bid(claims=[nested]), HOSTED_PATH)

    assert result.ok is False or result.requires_verification is True, (
        "a seller_asserted provenance smuggled into a hook-provenanced claim's value was "
        f"admitted clean: ok={result.ok} reasons={result.reasons} "
        f"requires_verification={result.requires_verification}"
    )


# =============================================================================================
# T-162 — the external door checks provenance SOURCE but never discount AUTHORISATION
# =============================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-162: an external submitter's own claim of authorized_discount_pct is admitted "
        "clean by validate_bid on the external path (ok=True, reasons=[], "
        "requires_verification=False) — the store-agent's hook-ledger guard has no equivalent "
        "on this door; remove this marker with the fix"
    ),
)
def test_an_external_submitters_self_asserted_discount_authorisation_is_not_taken_on_trust() -> (
    None
):
    """A Tier-2 store writing its own permission slip is not evidence of the permission.

    ``authorized_discount_pct`` is the number that decides how far below list a bid may price.
    On the hosted path a grant of that depth has to survive
    ``store_agent/hooks/provenance.py``, which holds the hook ledger and the envelope and
    refuses a depth the envelope never granted. The external path meets no such wall: the
    boundary holds no ledger, so the *only* thing it judges about the claim is the shape of the
    ``provenance`` block sitting next to it — and an external submitter writes that block too.
    Measured on this tree::

        claim = make_claim("policy", {"authorized_discount_pct": 25.0}, HOOK_PROVENANCE)
        validate_bid(make_bid(claims=[claim]), path="external", ...)
        -> ok=True  reasons=[]  requires_verification=False  unverified_claim_indexes=[]

    So a submitted document asserting its own discount authority is admitted as settled fact,
    indistinguishable from one the exchange actually authorised, and nothing downstream is told
    to look at it.

    The property asserted is the weakest one that closes the hole, and it does not conflict with
    R18's existing rule that a hook-shaped provenance need not be verified in general: a claim
    that asserts *its own authorisation* is a claim about the exchange's permissions rather than
    about the product, and on the door that holds no ledger it must be either refused or routed
    to verification. Either repair passes.
    """
    authorising = make_claim("policy", {"authorized_discount_pct": 25.0}, dict(HOOK_PROVENANCE))

    result = _check(make_bid(claims=[authorising]), EXTERNAL_PATH)

    assert result.ok is False or result.requires_verification is True, (
        "an external submission stating its own discount authorisation was admitted with "
        f"nothing said: ok={result.ok} reasons={result.reasons} "
        f"requires_verification={result.requires_verification} "
        f"unverified_claim_indexes={result.unverified_claim_indexes}"
    )


# =============================================================================================
# T-190 — the pinned external bid door has no server, so the boundary has no production caller
# =============================================================================================


def _declared_route_paths() -> dict[str, list[str]]:
    """Every literal path each `routes.py` in the repo declares, by file.

    Read from the source rather than from a built app: importing five services into one test
    to ask whether a route exists costs more than reading the decorators, and this question is
    about what is written down.
    """
    declared: dict[str, list[str]] = {}
    for path in sorted(_REPO_ROOT.glob("*/*/**/routes.py")):
        # `.pkgroot` is a directory of symlinks back into these same trees, and pathlib's `*`
        # matches dot-directories, so every route file would otherwise be counted twice.
        if any(part.startswith(".") or part == "node_modules" for part in path.parts):
            continue
        paths: list[str] = []
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not decorator.args:
                    continue
                first = decorator.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    paths.append(first.value)
        if paths:
            declared[str(path.relative_to(_REPO_ROOT))] = paths
    return declared


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-190: exchange.openapi.json pins POST /v1/auctions/{auction_id}/bids "
        "(operationId submitExternalBid) as the signed external door, and no routes.py in the "
        "repo declares it — so validate_external_submission has no production caller and the "
        "tightened bid boundary is unreachable from outside; remove this marker with the fix"
    ),
)
def test_the_pinned_external_bid_door_is_actually_served() -> None:
    """A published door with no building behind it.

    The exchange's OpenAPI document pins ``POST /v1/auctions/{auction_id}/bids`` with the
    summary "The signed external door: a Tier-2 seller submits a Bid", and
    ``test_openapi_contracts.py`` checks that every DESIGN-pinned route is *declared in the
    document*. Nothing checks that anything serves it. Scanning every ``routes.py`` in the
    repo, the exchange declares exactly ``POST /auctions``, ``GET /auctions/{auction_id}``,
    ``POST /accept`` and ``POST /confirm``; there is no ``/bids`` path anywhere.

    That is why the R8/R18 wall this package spent several tickets tightening has no production
    caller: ``validate_external_submission``'s only non-test call site in the repo is
    ``store_agent.external.door.receive_bid``, and ``receive_bid`` itself is called by nothing
    outside tests. A Tier-2 seller has no way to reach the boundary, and the boundary has no way
    to refuse anything a real client sent — which also means every measurement taken of it so
    far has been taken through a test harness rather than through the door.

    Two repairs pass: serve the route, or stop pinning it. The test asks the question the
    document raises — if the contract publishes this door, something must answer it.
    """
    document = json.loads((_CONTRACTS / "openapi/exchange.openapi.json").read_text())
    pinned = "/v1/auctions/{auction_id}/bids"
    if pinned not in document["paths"]:
        pytest.skip("the external bid door is no longer pinned; nothing to serve")

    declared = _declared_route_paths()
    assert declared, "no routes.py was found at all; the scan is wrong"

    serving = {
        file: [route for route in routes if route.rstrip("/").endswith("/bids")]
        for file, routes in declared.items()
    }
    served = {file: routes for file, routes in serving.items() if routes}

    assert served, (
        f"the contract pins {pinned} as the signed external door and no routes.py declares "
        f"any /bids path; declared routes are {declared}"
    )


# =============================================================================================
# T-194 — the two doors do not run the same schema check (`format: date-time`)
# =============================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-194: the TypeScript door validates through ajv WITH ajv-formats, so format: "
        "date-time is enforced on all eight timestamp fields; the Python door validates "
        "through generated pydantic where they are bare str, so it admits provenance."
        "observed_at='not-a-date' with ok=True, reasons=[] where the TS door answers "
        "schema_invalid; remove this marker with the fix"
    ),
)
def test_the_python_door_enforces_the_date_time_format_the_typescript_door_enforces() -> None:
    """One bid, two doors, two answers — and the wire format is the thing they disagree about.

    ``packages/contracts/src/ts/schemas.ts`` builds its validator as
    ``addFormats(new Ajv2020(...))``, so every ``format: date-time`` in the bundle is checked.
    The Python door's first step is ``model.model_validate(...)`` against
    ``packages/contracts/generated/python/protocol.py``, where all eight of those fields are
    typed as a bare ``str`` — pydantic never sees the ``format`` keyword, because
    datamodel-code-generator does not carry it into the annotation. Measured on the same
    payload::

        claim.provenance.observed_at = "not-a-date"
        python: validate_bid(..., path="hosted") -> ok=True  reasons=[]
        node:   validateBid(...,  {path:"hosted"}) ->
                ok=false reasons=["schema_invalid:claims.0.provenance.observed_at: must match
                                  format \\"date-time\\""]

    ``observed_at`` is the field to test this on rather than ``offer.expires_at``: the expiry is
    read a second time by the expiry wall, which refuses an unparseable value on its own, so the
    two doors happen to agree there for an unrelated reason. Nothing else reads ``observed_at``,
    so it shows the schema check itself.

    Two doors that admit different documents are not one contract, and the half that admits more
    is the half a Tier-2 submitter picks. The assertion is that the Python door refuses what the
    published bundle declares invalid; how it comes to refuse — a typed annotation in the
    generated model, an explicit format check at the boundary — is not this test's business.
    """
    schema = json.loads((_CONTRACTS / "schemas/protocol.schema.json").read_text())
    observed_at = schema["$defs"]["Provenance"]["properties"]["observed_at"]
    assert observed_at.get("format") == "date-time", (
        "positive control: the published bundle must still declare the format this test says "
        f"is unenforced, got {observed_at}"
    )

    malformed = dict(HOOK_PROVENANCE, observed_at="not-a-date")
    result = _check(make_bid(claims=[make_claim("free_returns", "30 days", malformed)]), HOSTED_PATH)

    assert result.ok is False, (
        "the Python door admitted a provenance timestamp the published schema declares "
        f"invalid and the TypeScript door refuses: ok={result.ok} reasons={result.reasons}"
    )


# =============================================================================================
# T-204 — `denial_reason` is a published field whose vocabulary is an exception class name
# =============================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-204: exchange.openapi.json types the 409 denial_reason as a bare {'type': 'string'} "
        "with no enum, while apps/exchange/src/accept/offer.py formats type(exc).__name__ into "
        "it and persists it into a policy_event — a client-visible vocabulary nothing pins; "
        "remove this marker with the fix"
    ),
)
def test_the_published_denial_reason_has_an_enumerated_vocabulary() -> None:
    """A persisted, client-visible field whose values are exception class names.

    ``accept()`` builds its refusal through ``_refused(..., reason=...)``, and the reason it is
    handed on the exception paths is ``type(exc).__name__``. That string lands in two places
    that outlive the request: ``AcceptResult.denial_reason``, which the 409 body publishes, and
    the ``policy_event`` payload the refusal writes. Renaming an exception class is therefore a
    breaking change to a published contract, and three reason values changed in a single branch
    (``OrphanedOffDomainCheckout``, ``UnusableDiscount``, and a doubled
    ``"OrphanedCheckoutCode: <Original>: …"``) without anything noticing.

    What the pinned document says about that field today, verbatim::

        paths./auctions/{auction_id}/accept.post.responses.409
             .content.application/json.schema.properties.denial_reason  ->  {"type": "string"}
             .content.application/json.example.denial_reason            ->  "blacklist"

    A bare string. Note that even the document's own example is from the *other* producer —
    ``accept.gate._denial_reason``, which spells eligibility refusals — so the two vocabularies
    that share this field have never been written down together.

    The property asserted is the minimum that turns an accident into a contract: the field is
    enumerated, and the document's own example is a member of the enumeration. It does not say
    what belongs in the vocabulary, so any repair that names the values passes — including one
    that stops formatting exception class names into the field and publishes stable codes
    instead.
    """
    document = json.loads((_CONTRACTS / "openapi/exchange.openapi.json").read_text())
    response = document["paths"]["/auctions/{auction_id}/accept"]["post"]["responses"]["409"]
    body = response["content"]["application/json"]
    schema = body["schema"]["properties"]["denial_reason"]

    assert schema.get("enum"), (
        "denial_reason is persisted and client-visible but its published vocabulary is "
        f"open-ended: {schema}"
    )
    example = body.get("example", {}).get("denial_reason")
    if example is not None:
        assert example in schema["enum"], (
            f"the document's own example denial_reason {example!r} is not in the vocabulary it "
            f"publishes: {schema['enum']}"
        )


# =============================================================================================
# T-240 — the pinned merchant contract has nowhere to put an envelope approval artifact
# =============================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-240: merchant.openapi.json's PUT envelope example asks for activation: 'active' "
        "while the document declares no approval affordance at all — no header parameter, no "
        "approval field on Envelope (additionalProperties: false), and no 403 response — yet "
        "the served route refuses exactly that request with 403 approval-required; remove this "
        "marker with the fix"
    ),
)
def test_the_pinned_merchant_contract_can_express_an_envelope_approval() -> None:
    """The published document invites the request its own implementation refuses.

    ``PUT /stores/{store_id}/envelope`` is pinned with a body of ``$defs/Envelope`` — eight
    required fields, ``additionalProperties: false`` — and its request **example** carries
    ``"activation": "active"``. Read literally, the contract says a store activates its
    envelope by writing the word into the body, which is the self-activation E5 exists to
    prevent.

    The served route does not do that. ``apps/merchant/svc/src/onboarding/routes.py`` reads the
    approval artifact from an ``X-Envelope-Approval`` header, stores an unapproved ``active``
    body in SHADOW, and answers ``403 approval-required``. That behaviour is right; the problem
    is that it is invisible. The string ``X-Envelope-Approval`` does not occur anywhere in
    ``packages/contracts/openapi/merchant.openapi.json``, the pinned PUT declares exactly one
    parameter (the ``store_id`` path parameter) and exactly one response (``200``), and the
    ``Envelope`` schema has no ``approver``, ``approved_at`` or ``envelope_hash`` field to carry
    the artifact in the body instead. So a client that implements the published contract
    faithfully gets a 403 the contract never mentions.

    Three repairs satisfy this test and it does not choose between them: document the header
    parameter, give the body a place to carry the approval, or document the 403 refusal. A
    fourth — deleting the self-activating example — is deliberately NOT enough on its own,
    because the served route would still refuse an undocumented way.
    """
    document = json.loads((_CONTRACTS / "openapi/merchant.openapi.json").read_text())
    put = document["paths"]["/stores/{store_id}/envelope"]["put"]

    headers = [p for p in put.get("parameters", []) if p.get("in") == "header"]
    responses = set(put.get("responses", {}))
    envelope = json.loads((_CONTRACTS / "schemas/protocol.schema.json").read_text())
    fields = set(envelope["$defs"]["Envelope"]["properties"])
    approval_fields = fields & {"approver", "approved_at", "approval", "envelope_hash"}

    expressible = bool(headers) or bool(approval_fields) or bool(responses & {"403"})

    assert expressible, (
        "the pinned PUT envelope operation has nowhere to put an approval artifact and never "
        "mentions the refusal the served route returns without one: header parameters="
        f"{headers}, Envelope approval fields={sorted(approval_fields)}, responses="
        f"{sorted(responses)}"
    )
