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

    control = _check(
        make_bid(claims=[make_claim("x", "y", dict(ASSERTED_PROVENANCE))]), HOSTED_PATH
    )
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
    result = _check(
        make_bid(claims=[make_claim("free_returns", "30 days", malformed)]), HOSTED_PATH
    )

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


# =============================================================================================
# T-306 / T-307 — the two halves of the price wall are disarmed by OMITTING them
# =============================================================================================
#
# One shared generator serves both properties. It draws a bid nobody chose: every
# caller-visible field of the `Bid`/`Offer`/`Claim` triple is randomized except the ones the
# arithmetic under test is a statement about (the declared depth, the list price and the stated
# unit price), which each property fixes deliberately.
#
# **Why randomized and not parametrized over shapes.** T-233 is the identical defect one
# argument over, and its first gate was going to be a table of hand-written payload shapes.
# That was MEASURED to close exactly one gaming key: an adversarial review of that change wrote
# four patches that keep the fail-open alive for realistic bids while taking every named shape
# green — keyed on `offer.expires_at`, on a `store_id` allowlist, on `auction_id`, and on
# `nonce`. Every shape came from one fixture, so every field they shared was a viable key, and
# no enumeration of shapes closes that: adding a sixth shape moves the key. The durable form is
# the property itself — the absent argument must be INDISTINGUISHABLE from the explicit empty
# one for a bid the test author did not choose — because a fail-open keyed on any payload field
# is caught by the draws that miss its key, and one keyed on nothing is caught by all of them.


#: The six provenance sources a tool hook can mint. Drawn from rather than fixed so a repair
#: cannot key its fail-open on one spelling; all six are admitted at every claim-bearing site on
#: BOTH paths, which is what keeps the arming controls below about price and nothing else.
_HOOK_SOURCES = (
    "scraped",
    "pixel_feed",
    "owner_statement",
    "envelope_rule",
    "learned_policy",
    "network",
)


def _drawn_provenance(rng: Any) -> dict[str, Any]:
    return {
        "source": _HOOK_SOURCES[rng.randrange(len(_HOOK_SOURCES))],
        "ref": f"envelope:draw-{rng.randrange(10**9)}#commitment-{rng.randrange(10**6)}",
        "observed_at": "2026-01-01T00:00:00Z",
        "authority_rank": rng.randrange(1, 6),
    }


def _drawn_claim(rng: Any, key: Any = None, value: Any = None) -> dict[str, Any]:
    return {
        "key": f"attr-{rng.randrange(10**9)}" if key is None else key,
        "value": f"value-{rng.randrange(10**9)}" if value is None else value,
        "provenance": _drawn_provenance(rng),
    }


#: How many bids each property draws per seed.
_DRAWS_PER_SEED = 60


def _property_seeds(pinned: int) -> tuple[int, int]:
    """One PINNED seed and one drawn fresh on every run. Both properties run over both.

    The pinned seed is what makes a red run reproducible. The unpinned one closes the hole the
    pinned seed leaves, and that hole is measured rather than theoretical: with a constant seed
    the 60 "drawn" bids are a constant TABLE, and an adversarial review of this very gate keyed
    a fail-open on five of their fields in turn — `store_id`, `auction_id`, `product_ref`,
    `offer.expires_at`, `signature` — normalizing the absent roster only for the 60 values the
    table happens to contain. All five took both gates green with the money-path fail-open fully
    alive for a realistic bid. That is the same shape as the four keys that sank the T-233 gate's
    predecessor, and no enumeration of payloads closes it: the defence is a table the patch has
    not seen. Every failure message names its seed, so a red run from the unpinned half is
    reproduced by pinning the seed it printed.
    """
    import random

    return (pinned, random.SystemRandom().randrange(2**32))


def _drawn_priced_bid(
    rng: Any,
    *,
    tag: str,
    depth: float,
    list_price: float,
    unit_price: float,
    carries_list_price: bool,
) -> dict[str, Any]:
    """A schema-valid `Bid` that states `unit_price` and declares `depth` percent off.

    Everything the price walk does NOT read is drawn: the ids, the store, the product and
    variant refs, the currency, the checkout url, the expiry, the free-text message, the agent
    version, the signature, how many claims ride along and at which of the two claim-bearing
    sites, and which hook minted each of them. What is fixed is only what the relation under
    test is about.
    """
    from packages.contracts.boundary import LIST_PRICE_CLAIM_KEY

    from packages.contracts.tests._fixtures_protocol import make_offer

    claims = [_drawn_claim(rng) for _ in range(rng.randrange(0, 4))]
    if carries_list_price:
        # The `list_price` claim `get_product_fact` mints — the ONLY list price the wall can see
        # when no roster is passed. Its position is drawn so a repair cannot key on index 0.
        claims.insert(
            rng.randrange(len(claims) + 1),
            _drawn_claim(rng, key=LIST_PRICE_CLAIM_KEY, value=list_price),
        )

    discount: Any = None
    if depth > 0.0 or rng.random() < 0.5:
        discount = {
            # All three spellings `_declared_depth` accepts, so a fix cannot key on one.
            "type": ("percentage", "percent", "pct")[rng.randrange(3)],
            "value": depth,
            "provenance": _drawn_provenance(rng),
        }

    offer = make_offer(
        # `tag` is the seed and draw index, so distinctness across a run is STRUCTURAL rather
        # than a collision probability the `len(distinct) == exercised` guard below could flake on.
        product_ref=f"prod-{tag}-{rng.randrange(10**9)}",
        variant_ref=f"var-{rng.randrange(10**9)}",
        unit_price=unit_price,
        total_price=unit_price,
        currency=("USD", "EUR", "GBP")[rng.randrange(3)],
        discount=discount,
        commitments=[_drawn_claim(rng) for _ in range(rng.randrange(0, 3))],
        expires_at=(
            f"{rng.randrange(2027, 3000)}-{rng.randrange(1, 13):02d}-"
            f"{rng.randrange(1, 29):02d}T00:00:00Z"
        ),
        checkout_url=f"https://s{rng.randrange(10**6)}.example.com/cart/{rng.randrange(10**9)}:1",
    )
    return make_bid(
        claims=claims,
        store_id=f"store-{rng.randrange(10**9)}",
        auction_id=f"auc-{rng.randrange(10**9)}",
        offer=offer,
        message=None if rng.random() < 0.5 else "m" * rng.randrange(1, 120),
        agent_version=f"agent/{rng.randrange(9)}.{rng.randrange(9)}.{rng.randrange(9)}",
        signature=f"sig-{rng.randrange(10**12)}",
    )


def _eligible_snapshot(bid: Any) -> dict[str, Any]:
    """An R12 row for exactly this bid's store, so eligibility never answers for the price wall."""
    store_id = bid["store_id"]
    return {store_id: {"store_id": store_id, "score": 0.6, "blacklisted": False}}


def _judge(bid: Any, path: str, **kwargs: Any) -> Any:
    return validate_bid(bid, path=path, trust_snapshot=_eligible_snapshot(bid), now=NOW, **kwargs)


def _verdict(result: Any) -> tuple:
    """Everything the caller can observe. Reasons are compared, not just `ok`: two refusals for
    different reasons are two different behaviours, and "absent" collapsing to some OTHER
    refusal would be a new defect wearing this one's passing grade."""
    return (
        result.ok,
        tuple(result.reasons or ()),
        result.requires_verification,
        tuple(result.unverified_claim_indexes or ()),
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-306: boundary.py's `if list_prices is None: return None, []` (in _authorized_depth "
        "and _roster_list_price) makes the ABSENT roster more permissive than an explicit "
        "empty one on the money path — measured, a bid charging 15.00 for a 100.00 product is "
        "ok=True reasons=[] with list_prices omitted and ok=False "
        "['price_unreconcilable:offer.discount:authorized_depth_unavailable', "
        "'price_unreconcilable:offer.unit_price:list_price_unavailable'] with list_prices={}; "
        "remove this marker with the fix"
    ),
)
def test_t306_an_absent_list_prices_roster_is_indistinguishable_from_an_empty_one() -> None:
    """Omission is the case that happens by accident, so it must not be the permissive one.

    This is T-233 one argument over, and this time on the money path. Measured on this tree,
    hosted path, a store with a clean R12 row, a hook-minted 20% grant, an offer charging 15.00
    for a product the exchange prices at 100.00 — 85% off behind a 20% authorization::

        validate_bid(bid, ..., )                  -> ok=True   reasons=[]
        validate_bid(bid, ..., list_prices={})    -> ok=False   reasons=[
            'price_unreconcilable:offer.discount:authorized_depth_unavailable',
            'price_unreconcilable:offer.unit_price:list_price_unavailable']

    A caller who said nothing about its catalog got a MORE permissive answer than one who said
    "I hold no catalog". `_roster_list_price` is explicit that a supplied roster is evidence and
    that every way of failing to read it is a refusal; the only thing that is not evidence is
    the argument nobody passed, which is precisely the call a caller makes by forgetting.

    **What this gate does NOT rest on**, because it was checked and it is not what it looked
    like: the phrase "the door it matters most on" (boundary.py:1136) is in
    `validate_external_submission`'s docstring, and read in full it is an argument for PASSING a
    roster at the Tier-2 door — "Pass the roster here or that choice is the only list price
    anybody checks" — not the door confessing to this asymmetry. The no-roster abstention is
    documented as a deliberate opt-in, and existing tests pin it
    (`test_boundary_dual_path.py::test_the_wall_abstains_deliberately_when_the_bid_carries_no_list_price`,
    `test_boundary_price_roster.py::test_the_cap_is_never_consulted_without_a_roster`). So this
    gate is a claim that the OPT-IN ITSELF is the defect on the money path, and closing it is a
    deliberate behaviour change with a measured blast radius — not the correction of an
    oversight. Whoever fixes it has to re-baseline those tests, and the TypeScript peer carries
    the identical asymmetry at `src/ts/boundary.ts:545` and `:580`.

    The property is about the ARGUMENT and therefore holds for every bid, which is why it is
    drawn rather than written — see the module comment above for the measured gaming keys a
    table of named shapes leaves open.

    Each draw arms itself twice before the comparison. With a roster that really prices the
    product the same bid must be ADMITTED, so the refusals below are the roster argument
    answering rather than the bid being unbuildable or blacklisted or expired; and the explicit
    empty roster must REFUSE, so the equality cannot be satisfied by collapsing both sides to
    `ok=True` — an "empty roster means no roster" repair points the fail-open the other way and
    is refused here by the `omitted.ok is False` assertion.
    """
    import random

    def _draw_once(rng: Any, seed: int, draw: int) -> str:
        """Build one case, arm it twice, then assert the property. Returns its `product_ref` so
        the caller can prove afterwards that the draws were genuinely distinct."""
        list_price = round(rng.uniform(5.0, 5000.0), 2)
        row_cap = round(rng.uniform(5.0, 60.0), 1)
        depth = round(rng.uniform(0.0, row_cap), 1)
        # The HONEST price for that depth, plus a cent so no rounding can put it under the wall.
        unit_price = round(list_price * (100.0 - depth) / 100.0 + 0.01, 2)
        path = (HOSTED_PATH, EXTERNAL_PATH)[rng.randrange(2)]

        bid = _drawn_priced_bid(
            rng,
            tag=f"{seed}-{draw}",
            depth=depth,
            list_price=list_price,
            unit_price=unit_price,
            carries_list_price=rng.random() < 0.5,
        )
        product_ref = bid["offer"]["product_ref"]
        roster = {product_ref: {"list_price": list_price, "max_discount_pct": row_cap}}

        supplied = _judge(bid, path, list_prices=roster)
        assert supplied.ok is True, (
            f"seed {seed} draw {draw}: arming — with a roster that prices this product at "
            f"{list_price} and authorizes {row_cap}%, an honest bid declaring {depth}% at "
            f"{unit_price} must be admitted, or the refusals below say nothing about the roster "
            f"ARGUMENT. path={path} bid={bid!r} result={supplied!r}"
        )

        empty = _judge(bid, path, list_prices={})
        assert empty.ok is False, (
            f"seed {seed} draw {draw}: control — a roster that cannot price this product is an "
            f"unavailable read and must be refused, never degraded back to the abstention. "
            f"path={path} bid={bid!r} result={empty!r}"
        )

        omitted = _judge(bid, path)
        assert _verdict(omitted) == _verdict(empty), (
            f"seed {seed} draw {draw}: omitting list_prices gave {_verdict(omitted)} where "
            f"passing an explicit empty roster gave {_verdict(empty)}. The absent argument must "
            f"be the empty one for EVERY bid, not for the ones this file happens to name — a "
            f"default that reads any part of the submission to decide how permissive to be is "
            f"the T-233 defect with a different key, on the money path. path={path} bid={bid!r}"
        )
        assert omitted.ok is False, (
            f"seed {seed} draw {draw}: a bid judged with NO catalog argument at all was admitted "
            f"while the same bid judged with an explicitly empty catalog was refused "
            f"{list(empty.reasons)}. Omission is the case that happens by accident, and it is "
            f"the more permissive of the two. path={path} bid={bid!r} result={omitted!r}"
        )
        return product_ref

    exercised = 0
    distinct: set[str] = set()
    for seed in _property_seeds(20260904):
        rng = random.Random(seed)
        for draw in range(_DRAWS_PER_SEED):
            distinct.add(_draw_once(rng, seed, draw))
            exercised += 1

    expected = 2 * _DRAWS_PER_SEED
    assert exercised == expected, (
        f"only {exercised} of {expected} cases were built, armed and compared. A loop that "
        f"silently iterates fewer cases than it claims is how three sweeps in this repo went "
        f"QUIET rather than red (6->0 of 8, 70->0 of 79, 48->0 of 66)"
    )
    assert len(distinct) == expected, (
        f"the generator produced {len(distinct)} distinct products across {expected} draws; a "
        f"property asserted over one repeated bid is a single-payload probe wearing a loop"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-307: `max_discount_pct` supplied WITHOUT `list_prices` is never read — "
        "boundary.py's `if list_prices is None: return None, []` in _authorized_depth returns "
        "before the ceiling is consulted, so the authorization ceiling is inert whenever the "
        "roster is absent. Measured: a bid declaring 85% and carrying its own list_price claim "
        "is ok=True reasons=[] under max_discount_pct=20.0 with no roster, and ok=False with "
        "list_prices={} and the same ceiling; remove this marker with the fix"
    ),
)
def test_t307_a_supplied_discount_ceiling_is_not_inert_when_the_roster_is_absent() -> None:
    """A ceiling the door does not read is not a ceiling.

    `max_discount_pct` is documented as "a caller-wide ceiling, in percentage points, for a
    caller holding one approved number rather than a per-product column". A caller that sets it
    to 0 — "I authorize no discount at all" — and omits the roster is currently told nothing:
    measured on this tree, a bid carrying its own `list_price: 100.0` claim, declaring 85% and
    charging 15.00::

        validate_bid(bid, ..., max_discount_pct=20.0)                   -> ok=True  reasons=[]
        validate_bid(bid, ..., max_discount_pct=20.0, list_prices={})   -> ok=False  reasons=[
            'discount_over_authorized_depth:offer.discount',
            'price_unreconcilable:offer.unit_price:list_price_unavailable',
            'price_under_declared_depth:offer.unit_price']

    The two arguments together are the price wall, and omitting ONE of them disarms BOTH rather
    than failing closed on the missing input. That is worse than the roster half alone, because
    the caller here did not stay silent — it stated a ceiling and was ignored, and nothing in
    the verdict says the number it supplied was dropped on the floor.

    Every draw declares a depth strictly deeper than the ceiling it is judged under and prices
    itself honestly against its OWN carried list price, so the only thing that can refuse it is
    the ceiling. Three controls arm each draw before the property is asserted:

    * with a roster that prices the product AND authorizes exactly the depth the bid declares,
      the bid is ADMITTED — so every wall other than the ceiling passes on this bid: schema, R8
      provenance at all three claim-bearing sites, expiry, R12 eligibility, and the arithmetic
      itself. A refusal below is therefore the ceiling answering and not something else;
    * with a roster that prices the product but authorizes nothing of its own, the same ceiling
      REFUSES it with `discount_over_authorized_depth:offer.discount` — proving the ceiling is a
      number this door knows how to read and that this bid genuinely exceeds it;
    * with an explicitly empty roster and the same ceiling, it is refused too — so the equality
      below cannot be satisfied by collapsing both sides to `ok=True`.

    **The first control is deliberately NOT "with no roster and no ceiling this bid is
    admitted", which is the obvious way to write it.** That call is precisely T-306's fail-open,
    and arming on it would make these two gates CONTRADICT each other: the repair that closes
    T-306 makes that same call refuse, so T-307 would go red for the fix that is supposed to
    close it, and the only patches taking both green would be ones keyed on the drawn payloads.
    Measured — a gate armed that way, run against the candidate T-306 fix, failed on its own
    precondition rather than on its property. A gate that can only be satisfied by cheating is
    worse than no gate.

    Any repair that makes the supplied ceiling bind satisfies this: reading it against the
    carried list price, or treating the absent roster as an empty one. RAISING on a ceiling
    supplied without a roster does not, and deliberately so — `validate_bid` is documented to
    never raise, because "a boundary that threw would make reject and crash indistinguishable
    to the caller", so an exception is a different defect rather than a repair. What is refused
    here is the outcome that is actually wrong: a clean `ok=True` for a bid that took 85% off
    under a ceiling of 20.
    """
    import random

    def _draw_once(rng: Any, seed: int, draw: int) -> str:
        """Build one case, arm it three times, then assert the property. Returns its
        `product_ref` so the caller can prove afterwards that the draws were genuinely
        distinct."""
        list_price = round(rng.uniform(5.0, 5000.0), 2)
        ceiling = round(rng.uniform(0.0, 30.0), 1)
        depth = round(rng.uniform(ceiling + 10.0, 95.0), 1)
        unit_price = round(list_price * (100.0 - depth) / 100.0 + 0.01, 2)
        path = (HOSTED_PATH, EXTERNAL_PATH)[rng.randrange(2)]

        bid = _drawn_priced_bid(
            rng,
            tag=f"{seed}-{draw}",
            depth=depth,
            list_price=list_price,
            unit_price=unit_price,
            carries_list_price=True,
        )
        product_ref = bid["offer"]["product_ref"]

        # ARM 1 — the bid is otherwise clean. A roster that prices the product AND authorizes
        # exactly the depth it declares admits it, so a refusal below is the ceiling and not the
        # schema, R8, the expiry, R12 or the arithmetic. This is stated with a roster PRESENT on
        # purpose; see the docstring for why arming on the no-roster call would put this gate in
        # direct contradiction with T-306's.
        authorizing = {product_ref: {"list_price": list_price, "max_discount_pct": depth}}
        clean = _judge(bid, path, list_prices=authorizing)
        assert clean.ok is True, (
            f"seed {seed} draw {draw}: arming — a bid declaring {depth}% at {unit_price} for a "
            f"product the roster prices at {list_price} and authorizes {depth}% on must be "
            f"admitted, or the refusals below say nothing about the CEILING. path={path} "
            f"bid={bid!r} result={clean!r}"
        )

        # ARM 2 — the ceiling is a number this door reads, and this bid exceeds it. The roster
        # prices the product and authorizes nothing of its own, so the CALLER-WIDE ceiling is
        # what `_authorized_depth` has to fall through to.
        bounded = _judge(bid, path, list_prices={product_ref: list_price}, max_discount_pct=ceiling)
        assert bounded.ok is False and any(
            reason.startswith("discount_over_authorized_depth:") for reason in bounded.reasons
        ), (
            f"seed {seed} draw {draw}: arming — a bid declaring {depth}% under a caller ceiling "
            f"of {ceiling}% must be refused for exceeding the authorized depth once a roster is "
            f"present, or this draw does not exercise the ceiling at all. path={path} "
            f"bid={bid!r} result={bounded!r}"
        )

        # ARM 3 — the explicit empty roster with the same ceiling refuses, so the equality below
        # cannot be satisfied by collapsing both sides to `ok=True`.
        empty = _judge(bid, path, list_prices={}, max_discount_pct=ceiling)
        assert empty.ok is False, (
            f"seed {seed} draw {draw}: control — an explicitly empty roster with a ceiling of "
            f"{ceiling}% must refuse. path={path} bid={bid!r} result={empty!r}"
        )

        omitted = _judge(bid, path, max_discount_pct=ceiling)
        assert _verdict(omitted) == _verdict(empty), (
            f"seed {seed} draw {draw}: supplying max_discount_pct={ceiling} with the roster "
            f"OMITTED gave {_verdict(omitted)} where supplying it with an explicit empty roster "
            f"gave {_verdict(empty)}. The ceiling is the caller's authorization, and which of "
            f"the two arguments the caller forgot must not decide whether the other one is "
            f"read. path={path} bid={bid!r}"
        )
        assert omitted.ok is False, (
            f"seed {seed} draw {draw}: a bid declaring {depth}% off was ADMITTED under a caller "
            f"ceiling of {ceiling}% because the roster was absent. The ceiling was supplied and "
            f"never read: an authorization the door drops on the floor is worse than one that "
            f"was never given, because the caller believes it is protected. path={path} "
            f"bid={bid!r} result={omitted!r}"
        )
        return product_ref

    exercised = 0
    distinct: set[str] = set()
    for seed in _property_seeds(20260905):
        rng = random.Random(seed)
        for draw in range(_DRAWS_PER_SEED):
            distinct.add(_draw_once(rng, seed, draw))
            exercised += 1

    expected = 2 * _DRAWS_PER_SEED
    assert exercised == expected, (
        f"only {exercised} of {expected} cases were built, armed and compared. A loop that "
        f"silently iterates fewer cases than it claims is how three sweeps in this repo went "
        f"QUIET rather than red (6->0 of 8, 70->0 of 79, 48->0 of 66)"
    )
    assert len(distinct) == expected, (
        f"the generator produced {len(distinct)} distinct products across {expected} draws; a "
        f"property asserted over one repeated bid is a single-payload probe wearing a loop"
    )


def test_the_t306_t307_generator_can_still_build_and_exercise_a_case() -> None:
    """A canary for the two gates above, and it is deliberately NOT xfail-marked.

    ``xfail(strict=True)`` scores a runtime ERROR exactly like a reproduction. A gate whose
    generator raises — an ImportError, a schema change that makes the drawn bid unbuildable —
    still reports ``xfailed``, the ordinary suite stays green, and the gate silently measures
    nothing. That is the same "went quiet rather than red" failure three sweeps in this repo
    have already had, one level up: not a loop iterating zero cases, but a whole gate scoring
    zero assertions and being congratulated for it.

    So this drives the same generator down the same arming path outside any xfail. It asserts
    only what is true BOTH before and after T-306/T-307 are fixed — a roster that prices the
    product admits an honest bid, an empty roster refuses it — so it never has to be edited
    when the defect is closed, and a broken generator fails an ordinary ``make verify``.
    """
    import random

    rng = random.Random(20260906)
    bid = _drawn_priced_bid(
        rng,
        tag="canary",
        depth=20.0,
        list_price=100.0,
        unit_price=80.01,
        carries_list_price=True,
    )
    product_ref = bid["offer"]["product_ref"]

    admitted = _judge(
        bid, HOSTED_PATH, list_prices={product_ref: {"list_price": 100.0, "max_discount_pct": 20.0}}
    )
    assert admitted.ok is True, (
        f"the T-306/T-307 generator can no longer build a bid this door admits, so both gates "
        f"are scoring errors as reproductions: {admitted!r}"
    )

    refused = _judge(bid, HOSTED_PATH, list_prices={})
    assert refused.ok is False, (
        f"an explicitly empty roster must refuse — it is the right-hand side both gates above "
        f"compare the absent argument against: {refused!r}"
    )

    pinned, drawn = _property_seeds(20260906)
    assert pinned == 20260906 and isinstance(drawn, int), (
        f"_property_seeds must return the pinned seed plus one drawn per run, got "
        f"{(pinned, drawn)!r}"
    )
