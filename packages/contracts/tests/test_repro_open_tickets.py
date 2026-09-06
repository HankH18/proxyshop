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


#: The exchange's own catalog for `make_offer()`'s product: `prod-1` lists at 49.00 — the price
#: the shared fixture offer charges — and the merchant approved up to 25% on it, deeper than any
#: depth these reproductions declare. An HONEST row, in other words: it refuses nothing here.
#:
#: **This roster is load-bearing and it is not decoration (T-306).** Before T-306 an absent
#: `list_prices` meant "abstain" and `_check` passed none, so the price wall was silent and the
#: only thing that could refuse a bid in this file was the defect the test was written about.
#: An absent roster is now the EMPTY roster, which refuses EVERY bid with
#: `price_unreconcilable:offer.unit_price:list_price_unavailable` — and three reproductions here
#: assert nothing stronger than `ok is False or requires_verification is True`, so all three
#: would XPASS on that unrelated refusal while the holes they name stayed wide open.
#:
#: Measured on this tree with `_check` unarmed, after the T-306 fix and with `--runxfail`:
#: `test_a_provenance_nested_in_a_claim_value...` (T-161),
#: `test_an_external_submitters_self_asserted_discount_authorisation...` (T-162) and
#: `test_the_python_door_enforces_the_date_time_format...` (T-194) all PASSED, and under
#: `xfail(strict=True)` that is an XPASS — a red suite whose only documented cure is deleting the
#: marker, which would have falsely closed three open tickets in one commit. T-337's text says
#: the T-306 fix "must also remove FIVE xfail markers"; that is wrong, and this constant is why.
#: Arming the helper silences the price wall so the defect under test is once again the only
#: thing in the tree that can refuse these bids.
_HONEST_ROSTER: dict[str, Any] = {"prod-1": {"list_price": 49.0, "max_discount_pct": 25.0}}


def _check(bid: Any, path: str) -> Any:
    return validate_bid(
        bid,
        path=path,
        trust_snapshot=make_snapshot_table(),
        now=NOW,
        list_prices=_HONEST_ROSTER,
    )


# =============================================================================================
# T-161 — a provenance block nested inside a claim's opaque `value` is never walked
# =============================================================================================


# TEST EDIT — the `xfail(strict=True)` marker that stood here was REMOVED, and nothing else in
# this node changed. No assertion was touched: the two below are byte-identical to the ones the
# marker was written around.
#
#   `assert control.ok is False` — the positive control: the same seller_asserted block written
#   at the TOP level must still be refused on the hosted path.
#   `assert result.ok is False or result.requires_verification is True` — the nested block must
#   not be admitted clean.
#
# WOULD THIS TEST STILL BE WRONG IF I REVERTED MY CHANGE? No — it would be RIGHT and RED, which
# is what it was for. Reverting `_source_verdict`'s nested walk in a scratch copy outside the
# repo returned this node to `AssertionError: ... admitted clean: ok=True reasons=[]
# requires_verification=False`, the verbatim text of the reproduction. The marker was removed
# because the defect is gone, not because the test was.
#
# MEASUREMENT that established the defect is gone: `_source_verdict` now takes the STRICTEST
# verdict over the holder's own provenance and every protocol-shaped provenance block nested in
# its opaque `value` (`_nested_provenance_sources`), so the payload below answers
# `ok=False, reasons=['hosted_non_hook_provenance:0.value.provenance:seller_asserted']`.
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


# TEST EDIT — the `xfail(strict=True)` marker that stood here was REMOVED. The single assertion
# below is untouched:
#
#   `assert result.ok is False or result.requires_verification is True` — an external submission
#   stating its own discount authorisation may not be admitted with nothing said.
#
# WOULD THIS TEST STILL BE WRONG IF I REVERTED MY CHANGE? No. Reverting the `_authorisation_verdict`
# call in `_claim_provenance_reasons` in a scratch copy outside the repo returned this node to
# `AssertionError: an external submission stating its own discount authorisation was admitted
# with nothing said: ok=True reasons=[] requires_verification=False unverified_claim_indexes=[]`.
#
# MEASUREMENT that established the defect is gone: on the external path a claim naming
# `authorized_discount_pct` or `max_discount_pct` — as its key or anywhere inside its value — is
# now routed to verification by index, so the payload below answers `ok=True,
# requires_verification=True, unverified_claim_indexes=[0]`. The test's own docstring names that
# as one of the two repairs it accepts.
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


# T-190 FIXED — and the marker is removed here by a commit whose ticket list names T-244, not
# T-190, which is worth saying out loud rather than leaving to be discovered.
#
# This node asserts that some `routes.py` declares the pinned `/bids` path. T-244's repair is
# `apps/exchange/src/external_bids/routes.py`, which declares exactly that path and calls
# `store_agent.external.door.receive_bid` behind it — so it removes the defect THIS node
# encodes as a side effect of removing its own. Under `xfail(strict=True)` that is not
# something a lane may leave alone: the node became XPASS(strict), i.e. FAILED, the moment the
# route existed. T-266's own marker text predicted this exact consequence in advance —
# "serving the bid door turns packages/contracts/tests/test_repro_open_tickets::
# test_the_pinned_external_bid_door_is_actually_served into an XPASS(strict) failure".
#
# Measured in this worktree on 2026-09-06 at worker index 10:
#   BEFORE: `1 failed` under --runxfail — "the contract pins /v1/auctions/{auction_id}/bids as
#           the signed external door and no routes.py declares any /bids path".
#   AFTER:  `1 passed`, with the declared-routes map now carrying
#           'apps/exchange/src/external_bids/routes.py': ['/v1/auctions/{auction_id}/bids'].
#   CAUSATION: deleting `apps/exchange/src/external_bids/` in a scratch copy outside the repo
#           returns this node to its original failure verbatim.
#
# The assertion itself is untouched: `validate_external_submission` now genuinely has a
# production caller, which is the thing the node was written to demand.
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


# TEST EDIT — the `xfail(strict=True)` marker that stood here was REMOVED. Both assertions are
# untouched:
#
#   `assert observed_at.get("format") == "date-time"` — the positive control: the published
#   bundle must still declare the format this test says was unenforced.
#   `assert result.ok is False` — the Python door must refuse a timestamp the bundle declares
#   invalid and the TypeScript door already refuses.
#
# WOULD THIS TEST STILL BE WRONG IF I REVERTED MY CHANGE? No. Reverting the
# `_date_time_format_reasons` call in `validate_bid` in a scratch copy outside the repo returned
# this node to `AssertionError: the Python door admitted a provenance timestamp the published
# schema declares invalid and the TypeScript door refuses: ok=True reasons=[]`.
#
# MEASUREMENT that established the defect is gone: `validate_bid` now walks the `format: date-time`
# positions the bundle declares and that a `Bid` can reach — every `provenance.observed_at`,
# `offer.expires_at`, and `issued_at` on a signed submission — with `_is_rfc3339_date_time`, which
# mirrors `ajv-formats`' own grammar rather than a stricter reading, so the two doors admit the
# same set. The payload below answers `ok=False,
# reasons=['schema_invalid:claims.0.provenance.observed_at']`.
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


# The marker below is KEPT, and its text was corrected rather than removed. The assertion is
# untouched; only the `reason` prose changed, because it named bytes that have since moved
# (T-267 replaced the `"blacklist"` example, and the schema is no longer bare). Keeping a
# strict-xfail whose stated reason is false is how a gate stops being readable evidence.
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-204: exchange.openapi.json still declares NO `enum` on the 409 denial_reason. It "
        "now carries a `description`, a `pattern` pinning the nine declared codes and an "
        "`x-vocabulary` listing them (T-267's lane), but this gate asks for `enum` "
        "specifically and an `enum` cannot be published truthfully while the served field "
        "carries the `'<code>: <prose>'` shape apps/exchange/src/accept/reasons.py builds — "
        "enumerating the bare codes would publish a constraint every real 409 violates. "
        "Closing this needs the SERVICE to publish a bare code (or a second field), which is "
        "outside packages/contracts; see the contracts-api lane's NEEDS. Remove this marker "
        "with that fix, not before"
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

    What the pinned document said about that field when this gate was written, verbatim::

        paths./auctions/{auction_id}/accept.post.responses.409
             .content.application/json.schema.properties.denial_reason  ->  {"type": "string"}
             .content.application/json.example.denial_reason            ->  "blacklist"

    A bare string, and an example ``"blacklist"`` that no producer has ever emitted —
    ``denial_code("blacklist")`` is ``None``, so ``routes._denied`` would have re-published it
    as ``unspecified: blacklist``. That was T-267 and it is FIXED: the example now reads
    ``"blacklisted: fixture:blacklisted"``, measured live off ``accept.gate._denial_reason``.

    **Why this gate is still red, and what would close it.** The schema now carries a
    ``description``, an ``x-vocabulary`` listing the nine codes of
    ``exchange.accept.DENIAL_REASONS``, and a ``pattern`` that accepts exactly what the
    service produces (proved against every code, bare and prose-suffixed). What it does NOT
    carry is ``enum``, and that omission is deliberate: the served field is
    ``"<code>: <prose>"`` — ``"checkout_refused: OrphanedOffDomainCheckout: ..."`` —
    so an ``enum`` of the bare codes would be a published constraint that essentially every
    real 409 violates, and nothing in this repo validates a live response against this schema,
    so it would go green while being false. Publishing a false contract to close a gate about
    contract/implementation divergence is the defect wearing the fix's clothes.

    Two honest closures, both outside ``packages/contracts``: have the service publish the
    bare declared code on the wire (``routes._denied`` splitting code from prose — the repair
    the paragraph below already names), or split the body into ``denial_code`` +
    ``denial_detail``. Either lets ``enum`` be both present and true.

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
# T-267 — the published denial_reason example was a value nothing has ever produced
# =============================================================================================
#
# NOT xfail. T-267 was dispatched with `verify: "false  # NO GATE YET"` and this is the gate it
# never had; the defect it names is repaired in the same change, so the honest artifact is a
# regression test that PASSES now and was RED at HEAD, not a marker pretending otherwise.
# Measured both ways under PROXYSHOP_WORKER=3: against
# `git show HEAD:packages/contracts/openapi/exchange.openapi.json` this node fails on its first
# assertion (`denial_code('blacklist') is None`); against the amended document it passes.
#
# The grader is deliberately NOT in this lane's write scope. Every expected value is read live
# out of `exchange.accept` — a package `packages/contracts` cannot edit — so the assertion
# compares the published document against the running producer rather than against a constant
# somebody could move to match. The literals below are negative controls only; they can make
# this test stricter, never greener.


def test_the_published_denial_reason_example_is_a_value_the_exchange_can_produce() -> None:
    """T-267: the 409 example must be a refusal the code emits, and the pattern must fit them all.

    The published example was ``"blacklist"``. No producer has ever emitted it — the
    eligibility gate spells the term ``"blacklisted"`` — and it is not merely a typo: it is not
    a declared code at all, so ``routes._denied`` would have rewritten a refusal reading
    ``"blacklist"`` as ``"unspecified: blacklist"`` before it reached a client. The one worked
    example a consumer is given of this field showed them a value the service cannot return.

    Three properties, and the second is the one that keeps the document honest as the
    vocabulary grows:

    1. the example's leading token is a code ``exchange.accept`` declares;
    2. the published ``pattern`` accepts **every** value ``denial_reason()`` can build — each
       of the nine codes bare, and each with prose behind it — so the constraint the document
       publishes is one the running service actually satisfies. This is why the field is not
       an ``enum``: see ``test_the_published_denial_reason_has_an_enumerated_vocabulary``.
    3. ``x-vocabulary`` is exactly ``DENIAL_REASONS``, so a tenth code added to the service
       without being published turns this red.
    """
    import re  # noqa: PLC0415 - kept out of this file's frozen import head

    from exchange.accept import DENIAL_REASONS  # noqa: PLC0415
    from exchange.accept.reasons import denial_code, denial_reason  # noqa: PLC0415

    assert len(DENIAL_REASONS) >= 9, (
        "the declared vocabulary shrank below what this gate was measured against; a sweep "
        f"over {len(DENIAL_REASONS)} codes is not the sweep this test claims to be"
    )

    document = json.loads((_CONTRACTS / "openapi/exchange.openapi.json").read_text())
    body = document["paths"]["/auctions/{auction_id}/accept"]["post"]["responses"]["409"][
        "content"
    ]["application/json"]
    schema = body["schema"]["properties"]["denial_reason"]
    example = body["example"]["denial_reason"]

    code = denial_code(example)
    assert code is not None and code in DENIAL_REASONS, (
        f"the published 409 example denial_reason {example!r} does not begin with any code "
        f"exchange.accept declares, so it is a value the service would itself rewrite before "
        f"returning it; the declared codes are {list(DENIAL_REASONS)}"
    )

    pattern = re.compile(schema["pattern"])
    producible = [denial_reason(c) for c in DENIAL_REASONS] + [
        denial_reason(c, "diagnosis: with a colon in it") for c in DENIAL_REASONS
    ]
    rejected = [value for value in producible if pattern.match(value) is None]
    assert rejected == [], (
        "the published pattern refuses values the service actually returns, which would make "
        f"the document false about real traffic: {rejected}"
    )

    for undeclared in ("blacklist", "OrphanedOffDomainCheckout", "denied", ""):
        assert pattern.match(undeclared) is None, (
            f"the published pattern accepts {undeclared!r}, which no declared code produces; "
            "a pattern that accepts anything pins nothing"
        )

    assert sorted(schema["x-vocabulary"]) == sorted(DENIAL_REASONS), (
        "the vocabulary the document publishes has drifted from the one the exchange emits: "
        f"published {sorted(schema['x-vocabulary'])}, emitted {sorted(DENIAL_REASONS)}"
    )


# =============================================================================================
# T-267 (part two) — the pattern must fit what the gate PASSES THROUGH, not only what
#                    `denial_reason()` BUILDS
# =============================================================================================
#
# The sweep above grades `denial_reason(code)` and `denial_reason(code, prose)`. Those are
# values the package CONSTRUCTS: `"<code>"` and `"<code>: <prose>"` by construction, which
# cannot violate a pattern written around that shape. So the check asked a narrower question
# than the surface it protects, and the surface has a third path.
#
# `apps/exchange/src/accept/gate.py::_denial_reason` is the only producer that emits a string
# it did not build:
#
#     if denial_code(reason) == code:
#         return reason            # <- verbatim, whatever the eligibility source wrote
#
# and `accept/routes.py::_denied` republishes that untouched, because `denial_code` is not
# `None`. `denial_code` splits on a BARE colon and `.strip()`s the token:
#
#     token = str(reason or "").split(DENIAL_SEPARATOR.strip(), 1)[0].strip()
#
# so it accepts a colon with NO space behind it, and whitespace in FRONT of it. Executed,
# against the `($|: )` tail this lane first published: `"blacklisted:chargeback fraud"`,
# `"blacklisted : chargeback fraud"` and `"blacklisted:"` were all served verbatim and all
# three were refused by the published pattern.
#
# That is not hypothetical. `EligibilityDecision.reason` is a free-form `str` on a public port
# (`SellerEligibility.check`) whose live lookup is explicitly not yet written, so a third-party
# source is entitled to write any of those — and the contract said the service could not
# return them. No in-repo producer emits one, which is exactly why nothing was red.
#
# The three gates below are one gate in three parts: a sweep driven through the real producer,
# the same rule as a property over every code point in Unicode, and the positive control that
# proves the first two can fail.


#: The tail this repair replaced, kept as the positive control's input. `($|: )` demanded a
#: colon AND a space; `denial_code` demands neither.
SUPERSEDED_DENIAL_REASON_TAIL = "($|: )"


def _published_denial_reason_pattern() -> str:
    """The `pattern` the checked-in contract publishes for the 409 `denial_reason`."""
    document = json.loads((_CONTRACTS / "openapi/exchange.openapi.json").read_text())
    body = document["paths"]["/auctions/{auction_id}/accept"]["post"]["responses"]["409"][
        "content"
    ]["application/json"]
    return str(body["schema"]["properties"]["denial_reason"]["pattern"])


def _reason_shapes_denial_code_accepts() -> list[tuple[str, str]]:
    """`(status, reason)` pairs an eligibility source could answer with, built from the
    PARSER rather than from a list of witnesses.

    `denial_code` is `reason.split(":", 1)[0].strip() in DENIAL_REASONS`. Three degrees of
    freedom fall straight out of that expression and nothing else does:

    * which declared code the token is — so the codes come from `DENIAL_REASONS` live;
    * what `.strip()` removes around the token — so the gaps are every ASCII code point for
      which `chr(cp).strip() == ""`, computed here, never typed;
    * what follows the first separator — free prose, including another separator.

    A hard-coded table of the four strings that were observed failing would fix four strings.
    This enumerates the shape instead, and the caller filters it with `denial_code` itself.
    """
    from exchange.accept import DENIAL_REASONS  # noqa: PLC0415
    from exchange.accept.reasons import DENIAL_SEPARATOR  # noqa: PLC0415

    separator = DENIAL_SEPARATOR.strip()
    # Written as code points so this file carries no raw control characters (T-108's habit).
    blanks = tuple(chr(cp) for cp in range(0x21) if chr(cp).strip() == "")
    gaps = ("", *blanks, chr(0x20) * 3, chr(0x09) + chr(0x20) + chr(0x0A))
    tails = (
        "",
        "prose",
        chr(0x20) + "prose",
        chr(0x20) * 2 + "prose",
        separator,
        "prose" + separator + chr(0x20) + "nested",
    )

    shapes: list[tuple[str, str]] = []
    for code in DENIAL_REASONS:
        shapes.append((code, code))
        for gap in gaps:
            shapes.append((code, code + gap))
            shapes.append((code, gap + code))
            for tail in tails:
                shapes.append((code, code + gap + separator + tail))
                shapes.append((code, gap + code + gap + separator + tail))
    return shapes


def _serve(status: str, reason: str) -> str:
    """The `denial_reason` a client receives when an eligibility source answers `(status,
    reason)` — through the REAL producer, gate then HTTP boundary, nothing re-implemented."""
    from exchange.accept.gate import _denial_reason  # noqa: PLC0415
    from exchange.accept.routes import _denied  # noqa: PLC0415
    from exchange.eligibility import EligibilityDecision  # noqa: PLC0415

    decision = EligibilityDecision(store_id="store-1", status=status, reason=reason)
    # `Response.body` is typed `bytes | memoryview[int]`; `bytes(...)` narrows it for mypy
    # without changing a byte of what the client would receive.
    return str(json.loads(bytes(_denied(_denial_reason(decision)).body))["denial_reason"])


def test_the_published_denial_reason_pattern_fits_every_reason_the_gate_passes_through() -> None:
    """T-267: drive `gate._denial_reason`'s passthrough, not just the values the package builds.

    Every shape `denial_code` accepts is pushed through the real gate and the real 409
    boundary, and the value a client would actually receive must satisfy the published
    `pattern`. The inputs are filtered by `denial_code` itself, so the day the parser widens,
    this sweep widens with it.
    """
    import re  # noqa: PLC0415 - kept out of this file's frozen import head

    from exchange.accept import DENIAL_REASONS  # noqa: PLC0415
    from exchange.accept.reasons import DENIAL_SEPARATOR, denial_code  # noqa: PLC0415

    pattern = re.compile(_published_denial_reason_pattern())
    shapes = [
        (status, reason)
        for status, reason in _reason_shapes_denial_code_accepts()
        if denial_code(reason) is not None
    ]
    assert len(shapes) >= 500, (
        f"the sweep collapsed to {len(shapes)} inputs; denial_code accepts a far larger family "
        "than that, so this is no longer the sweep this test claims to be"
    )

    served = [(reason, _serve(status, reason)) for status, reason in shapes]
    rejected = [(reason, value) for reason, value in served if pattern.match(value) is None]
    assert rejected == [], (
        "the exchange served these denial_reason values and the published pattern refuses "
        f"them, so the contract is false about responses the service really returns: "
        f"{rejected[:8]}"
    )

    # ...and the sweep must actually LEAVE the set `denial_reason()` can build, or it is the
    # narrower check again wearing a wider name. `denial_reason` emits `"<code>"` or
    # `"<code>: <prose>"` and nothing else; anything here outside those two shapes reached the
    # published surface through the passthrough.
    beyond = sorted(
        {
            value
            for _, value in served
            if value not in DENIAL_REASONS
            and not any(value.startswith(code + DENIAL_SEPARATOR) for code in DENIAL_REASONS)
        }
    )
    assert len(beyond) >= 20, (
        "this sweep never produced a value outside the two shapes denial_reason() builds, so "
        f"it re-asks the question the constructed-value sweep already answered: {beyond}"
    )


def test_the_published_denial_reason_pattern_and_denial_code_agree_on_every_code_point() -> None:
    """The rule as a PROPERTY, over all of Unicode, rather than over a list someone thought of.

    What the boundary emits verbatim is decided by two expressions and no others:
    `denial_code(reason) == code` in `gate._denial_reason`, and `str(reason).strip()` in
    `routes._denied`. So for one declared code and every code point there is, insert that code
    point between the token and the separator and require the published pattern to accept the
    result exactly when the producer would emit it. Equality in BOTH directions: a pattern that
    accepted everything would be as false as one that refuses real traffic.

    This is what makes the fix a fix for the class. The four strings that were observed failing
    are four of the thirty code points this walks; the pattern is not permitted to fit only
    those.
    """
    import re  # noqa: PLC0415 - kept out of this file's frozen import head

    from exchange.accept import DENIAL_REASONS  # noqa: PLC0415
    from exchange.accept.reasons import DENIAL_SEPARATOR, denial_code  # noqa: PLC0415

    pattern = re.compile(_published_denial_reason_pattern())
    separator = DENIAL_SEPARATOR.strip()
    code = DENIAL_REASONS[0]

    disagreements = []
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:  # surrogates are not characters
            continue
        reason = f"{code}{chr(cp)}{separator}prose"
        # The producer's own two conditions, quoted from the source, not paraphrased.
        emitted_verbatim = denial_code(reason) == code and reason == reason.strip()
        if bool(pattern.match(reason)) is not emitted_verbatim:
            disagreements.append(hex(cp))
    assert disagreements == [], (
        "the published pattern and the exchange's own denial_code parser disagree about "
        f"{len(disagreements)} code point(s); the first few are {disagreements[:12]}"
    )


def test_the_superseded_denial_reason_tail_would_have_failed_both_gates_above() -> None:
    """The positive control. A gate that passed for the OLD pattern too would prove nothing.

    `($|: )` is re-attached to the same code alternation the document publishes, and the two
    gates above are re-run against it: the sweep must reject real served values, and the
    code-point property must find exactly the thirty points where a bare-colon parser and a
    colon-plus-space pattern part company — the twenty-nine `str.strip()` removes, plus the
    separator itself (`"blacklisted::x"`, which `denial_code` accepts and `: ` does not).
    """
    import re  # noqa: PLC0415 - kept out of this file's frozen import head

    from exchange.accept import DENIAL_REASONS  # noqa: PLC0415
    from exchange.accept.reasons import DENIAL_SEPARATOR, denial_code  # noqa: PLC0415

    published = _published_denial_reason_pattern()
    # The alternation is everything up to the tail group; the old pattern is rebuilt from the
    # document's own code list so the control cannot drift away from what is published.
    alternation = published[: published.rindex("($|")]
    superseded = re.compile(alternation + SUPERSEDED_DENIAL_REASON_TAIL)

    served = [
        _serve(status, reason)
        for status, reason in _reason_shapes_denial_code_accepts()
        if denial_code(reason) is not None
    ]
    refused = [value for value in served if superseded.match(value) is None]
    assert refused, (
        "the superseded tail accepts every value this sweep serves, so the sweep above cannot "
        "have been what caught the defect"
    )

    separator = DENIAL_SEPARATOR.strip()
    code = DENIAL_REASONS[0]
    divergent = []
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        reason = f"{code}{chr(cp)}{separator}prose"
        emitted_verbatim = denial_code(reason) == code and reason == reason.strip()
        if bool(superseded.match(reason)) is not emitted_verbatim:
            divergent.append(cp)
    expected = sorted(
        [cp for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF and chr(cp).strip() == ""]
        + [ord(separator)]
    )
    assert divergent == expected, (
        "the superseded tail no longer diverges where it was measured to diverge, so this "
        f"control has stopped controlling anything: {len(divergent)} vs {len(expected)}"
    )


# =============================================================================================
# T-240 — the pinned merchant contract has nowhere to put an envelope approval artifact
# =============================================================================================


# TEST EDIT — the `xfail(strict=True)` marker that stood here was REMOVED. Nothing below this
# line changed; the assertion is untouched and now passes on its own terms.
#
# TEST:       packages/contracts/tests/test_repro_open_tickets.py::
#             test_the_pinned_merchant_contract_can_express_an_envelope_approval
# ASSERTION:  `assert expressible, (...)` — where `expressible = bool(headers) or
#             bool(approval_fields) or bool(responses & {"403"})`. Unchanged, verbatim, below.
# REMOVED:    only the marker, whose own reason string reads "remove this marker with the fix".
#             `strict=True` makes a repair an XPASS *failure* precisely so the marker cannot
#             outlive the defect; leaving it would redden `make verify`.
# REVERT CHECK: NO — it does not still fail if the change is reverted, and that is the proof
#             rather than an excuse. MEASURED both ways under PROXYSHOP_WORKER=3: with
#             `packages/contracts/openapi/merchant.openapi.json` restored to HEAD
#             (`git show HEAD:...`) this node reports `1 xfailed`; with the amended document
#             it reports XPASS(strict). The marker's disappearance is caused by the contract
#             edit and by nothing else.
# ORIGIN:     0e9734a "test(repro): strict-xfail gates for 11 core-package tickets that had
#             none" — the gate for T-240, whose requirement is R6/E5: an envelope is activated
#             by a recorded written approval, never by a body asserting `activation: "active"`.
# VERDICT:    NEITHER wrong — the defect was real and is now fixed. `merchant.openapi.json`
#             now declares the `X-Envelope-Approval` header parameter the served route reads
#             (apps/merchant/svc/src/onboarding/routes.py:50, :79-93) AND the `403
#             approval-required` body it returns without one (:147-162), so the document has
#             somewhere to put an approval artifact and names the refusal it was silent about.
# EVIDENCE:   independent of the assertion — `sorted(p["name"] for p in put["parameters"])`
#             now contains "X-Envelope-Approval", and `sorted(put["responses"])` contains
#             "403"; both were absent at HEAD. The published 403 example is the exact body
#             `_problem(403, "approval-required", store_id=..., version=..., activation=...,
#             detail=str(exc))` builds, read off the source rather than off the test.
# BLAST RADIUS: no operation was added or removed, so `PINNED_ROUTES` and every route-set
#             comparison (test_openapi_contracts.py:34-42, apps/merchant/svc/tests/
#             test_merchant_hardening.py:411, the T-317 gates) are untouched. The new response
#             bodies are checked by test_every_checked_in_example_validates_against_its_
#             declared_schema and its TypeScript twin, both green.
def test_the_pinned_merchant_contract_can_express_an_envelope_approval() -> None:
    """The published document must not invite the request its own implementation refuses.

    ``PUT /stores/{store_id}/envelope`` is pinned with a body of ``$defs/Envelope`` — eight
    required fields, ``additionalProperties: false`` — and its request **example** carries
    ``"activation": "active"``. Read literally *and with nothing else in the document*, that
    says a store activates its envelope by writing the word into the body, which is the
    self-activation E5 exists to prevent.

    The served route does not do that. ``apps/merchant/svc/src/onboarding/routes.py`` reads the
    approval artifact from an ``X-Envelope-Approval`` header, stores an unapproved ``active``
    body in SHADOW, and answers ``403 approval-required``. That behaviour is right; the defect
    was that it was invisible.

    **AS MEASURED AT 0e9734a, when this gate was written** — kept in the past tense because a
    docstring describing a repaired defect in the present tense is the next reader's wrong
    turn: the string ``X-Envelope-Approval`` occurred nowhere in
    ``packages/contracts/openapi/merchant.openapi.json``, the pinned PUT declared exactly one
    parameter (the ``store_id`` path parameter) and exactly one response (``200``), and the
    ``Envelope`` schema had no ``approver``, ``approved_at`` or ``envelope_hash`` field to
    carry the artifact in the body instead. A client implementing the published contract
    faithfully got a 403 the contract never mentioned.

    **Repaired** by the first of the three repairs below *and* the third: the PUT now declares
    the ``X-Envelope-Approval`` header parameter, with the artifact's own shape and the
    binding rule (``envelope_hash``) written down, and a ``403`` response whose example is the
    exact body ``_problem(403, "approval-required", ...)`` builds. The ``Envelope`` schema is
    untouched — it lives in ``packages/contracts/schemas/``, and the eight-field wire document
    ``test_onboarding_envelope.py:565`` pins is deliberately not widened.

    Three repairs satisfy this test and it does not choose between them: document the header
    parameter, give the body a place to carry the approval, or document the 403 refusal. A
    fourth — deleting the self-activating example — is deliberately NOT enough on its own,
    because the served route would still refuse an undocumented way. (The example still reads
    ``"activation": "active"``, and that is now correct rather than an invitation: it is a
    legal request, and the document says on the same operation what must accompany it.)
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


#: THE SHAPE IS DRAWN TOO, NOT ONLY THE VALUE — and this is the part that took the longest to
#: get right. A generator that randomizes values inside a FIXED TEMPLATE is separable from real
#: traffic by a predicate on the template, and a fresh seed closes none of those: it redraws the
#: values and leaves the template exactly where it was. Measured on an earlier cut of these
#: gates, 22 such predicates took BOTH properties green while leaving the money-path fail-open
#: fully alive — `product_ref.startswith("prod-")`, `store_id.startswith("store-")`,
#: `signature.startswith("sig-")`, `agent_version.startswith("agent/")`, every claim key
#: matching `attr-*`, every provenance ref matching `envelope:draw-*`, `total_price ==
#: unit_price`, `currency in {USD,EUR,GBP}`, a `message` of all-`m`, expiries always at
#: midnight, `len(claims) <= 4`, the snapshot row's `score == 0.6`, and so on. So every one of
#: those is now drawn from a POOL OF SHAPES, and the pools deliberately include this repo's own
#: fixture spellings (`_fixtures_protocol` uses `variant_ref="44352913"`,
#: `agent_version="store-agent/1.0.0"`, claim key `"material"`), which is what makes "looks like
#: the test generator" stop being a decidable question rather than merely a harder one.
def _drawn_id(rng: Any, *aliases: str) -> str:
    """An identifier of a drawn SHAPE and a drawn prefix.

    The prefix pool is passed in by the caller and always contains this repo's own spelling
    (`auc-1`, `store-1`, `prod-1`, `sig-deadbeef`), because a generator that only ever emits
    `auction-<n>` is separable from real traffic by a regex on the prefix alone — measured, that
    exact key took both gates green with the money path open. The last two shapes are free-form
    over a drawn alphabet and length, so the emitted set is not enumerable by a pattern.
    """
    kind = aliases[rng.randrange(len(aliases))]
    n = rng.randrange(10 ** rng.randrange(1, 12))
    alphabet = (
        "0123456789abcdef",
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "0123456789",
        "abcdefghijklmnopqrstuvwxyz-_.",
    )[rng.randrange(4)]
    junk = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 33)))
    return (
        f"{kind}-{n}",
        f"{n}",
        f"{kind}_{n:08d}",
        f"gid://shopify/{kind.capitalize()}/{n}",
        f"{kind[:2].upper()}{n:04X}",
        junk,
        f"{kind}.{n}.v{rng.randrange(9)}",
        f"{kind}-{junk}",
    )[rng.randrange(8)]


_WORDS = (
    "free",
    "returns",
    "merino",
    "wool",
    "ships",
    "within",
    "two",
    "days",
    "organic",
    "cotton",
    "made",
    "in",
    "portugal",
    "limited",
    "run",
    "fragrance",
    "gentle",
    "restock",
)

_CURRENCIES = ("USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "SEK", "NZD", None)

#: Claim keys a real bid carries, alongside drawn ones. `list_price` is deliberately absent —
#: it is inserted explicitly and only when the caller asks for it.
_CLAIM_KEYS = ("material", "origin", "care", "free_returns", "ships_within", "warranty")


#: Strings this repo's own fixtures actually carry, drawn alongside prose and free-form junk.
_REAL_STRINGS = (
    "30 days",
    "merino wool",
    "New Zealand",
    "machine washable",
    "60 days, free",
    "gentle vitamin C serum for sensitive skin",
)


def _drawn_text(rng: Any) -> str:
    """Prose, free-form junk, or one of this repo's own fixture strings.

    A vocabulary of 18 words is itself a key — "every string in this bid is built from these
    words" separated 100% of drawn bids from real traffic, where `"30 days"` is undrawable.
    """
    roll = rng.random()
    if roll < 0.35:
        alphabet = " abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,/-'"
        return "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 60)))
    if roll < 0.5:
        return _REAL_STRINGS[rng.randrange(len(_REAL_STRINGS))]
    return " ".join(_WORDS[rng.randrange(len(_WORDS))] for _ in range(rng.randrange(1, 12)))


def _drawn_timestamp(rng: Any, *, future: bool) -> str:
    """A timestamp. `future` means strictly after `NOW`.

    Two measured keys live here and they point in OPPOSITE directions, so both windows have to
    be wide rather than merely different from the last one:

    * the non-future window reaches past `NOW`'s year and OVERLAPS the future one. A past window
      stopping short of the current year gave "this bid carries no provenance observed in 2026 or
      later", which separated 100% of the generated population from 100% of real traffic.
    * the time of day is midnight a fifth of the time. Drawing it uniformly closed "every expiry
      is at midnight" and opened "NO timestamp in this bid is at midnight" — the repo's own
      `NOT_EXPIRED` and `HOOK_PROVENANCE` are both midnight, and a uniform draw hits it once in
      86,400. Same for the top of the hour and the top of the minute.
    """
    if future:
        year = rng.randrange(2026, 3000)
        month = rng.randrange(7, 13) if year == 2026 else rng.randrange(1, 13)
    else:
        year = rng.randrange(2000, 2028)
        month = rng.randrange(1, 13)
    roll = rng.random()
    if roll < 0.2:
        hh, mm, ss = 0, 0, 0
    elif roll < 0.3:
        hh, mm, ss = rng.randrange(24), 0, 0
    elif roll < 0.4:
        hh, mm, ss = rng.randrange(24), rng.randrange(60), 0
    else:
        hh, mm, ss = rng.randrange(24), rng.randrange(60), rng.randrange(60)
    # The real length of the real month. Stopping at 28 was a key of its own — every bid stamped
    # on the 29th, 30th or 31st was outside the drawn population — and a wrong length is worse
    # than a narrow one: 2026-02-29 is unparseable, which fails the expiry arming rather than the
    # property.
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]
    return f"{year:04d}-{month:02d}-{rng.randrange(1, days + 1):02d}T{hh:02d}:{mm:02d}:{ss:02d}Z"


def _drawn_provenance(rng: Any) -> dict[str, Any]:
    ref = (
        f"envelope:{_drawn_id(rng, 'store')}:v{rng.randrange(9)}#commitment-{rng.randrange(99)}",
        f"snapshot://{_drawn_id(rng, 'host')}.example.com/p@sha256:{rng.randrange(16**8):08x}",
        f"pitch:{_drawn_id(rng, 'p')}#span-{rng.randrange(99)}",
        _drawn_id(rng, "ref"),
    )[rng.randrange(4)]
    return {
        "source": _HOOK_SOURCES[rng.randrange(len(_HOOK_SOURCES))],
        "ref": ref,
        "observed_at": _drawn_timestamp(rng, future=False),
        "authority_rank": rng.randrange(1, 10),
    }


def _drawn_claim(
    rng: Any, key: Any = None, value: Any = None, provenance: Any = None
) -> dict[str, Any]:
    if key is None:
        key = (
            _CLAIM_KEYS[rng.randrange(len(_CLAIM_KEYS))]
            if rng.random() < 0.5
            else _drawn_id(rng, "attr", "fact", "k")
        )
    if value is None:
        value = (
            _drawn_text(rng),
            rng.randrange(1000),
            round(rng.uniform(0.0, 999.0), 2),
            rng.random() < 0.5,
            None,
        )[rng.randrange(5)]
    # `provenance` is passed in when the bid reuses ONE block across its claims, as a real
    # hook-minted bid does. Drawing every block independently is a key: "no two provenance
    # blocks in this bid are byte-identical" is true of every drawn bid and of no fixture one.
    return {
        "key": key,
        "value": value,
        "provenance": _drawn_provenance(rng) if provenance is None else dict(provenance),
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
    depth: float,
    list_price: float,
    unit_price: float,
    carries_list_price: bool,
) -> dict[str, Any]:
    """A schema-valid `Bid` that states `unit_price` and declares `depth` percent off.

    Everything the price walk does NOT read is drawn, SHAPE INCLUDED — see `_drawn_id`. What is
    fixed is only what the relation under test is a statement about: the declared depth, the
    list price, and the stated unit price. `total_price` is drawn at or above `unit_price`
    rather than pinned equal to it, because any quantity is at least one and a total pinned
    equal to a unit price is itself a template a fail-open can key on.
    """
    from packages.contracts.boundary import LIST_PRICE_CLAIM_KEY

    from packages.contracts.tests._fixtures_protocol import make_offer

    # A COHERENT FIXTURE-SHAPED BID, one draw in six. Drawing each id independently closed the
    # per-field prefix keys but left their CONJUNCTION unreachable: each id is repo-shaped ~5-12%
    # of the time, so "all four ids look like our fixtures" holds for ~1e-4 of draws and never
    # once in 120 — measured ALIVE at 20/20 as a fail-open key. Closing a per-field key by
    # widening a field does not close the whole-payload key over those fields; the population has
    # to actually contain the fixture-shaped bid.
    fixture_shaped = rng.random() < 0.17

    # Sometimes ONE provenance block is reused across every claim, the way a bid minted by a
    # single hook call is. Always drawing independent blocks is a key on its own.
    shared = _drawn_provenance(rng) if rng.random() < 0.4 else None

    claims = [_drawn_claim(rng, provenance=shared) for _ in range(rng.randrange(0, 8))]
    if carries_list_price:
        # The `list_price` claim `get_product_fact` mints — the ONLY list price the wall can see
        # when no roster is passed. Its position is drawn so a repair cannot key on index 0.
        claims.insert(
            rng.randrange(len(claims) + 1),
            _drawn_claim(rng, key=LIST_PRICE_CLAIM_KEY, value=list_price, provenance=shared),
        )

    discount: Any = None
    if depth > 0.0 or rng.random() < 0.5:
        discount = {
            # All three spellings `_declared_depth` accepts, so a fix cannot key on one.
            "type": ("percentage", "percent", "pct")[rng.randrange(3)],
            "value": depth,
            # A real hook-minted grant carries `HOOK_PROVENANCE` verbatim. Never emitting it made
            # "the discount's provenance is that exact block" a live key at 20/20.
            "provenance": (
                dict(HOOK_PROVENANCE)
                if fixture_shaped
                else (_drawn_provenance(rng) if shared is None else dict(shared))
            ),
        }

    # Any quantity is at least one, so a total can only ever be LARGER than one discounted unit.
    # A third of draws are an exact INTEGER multiple, because that is what a real quantity is and
    # a continuous multiplier never lands on one: `total_price == 2 * unit_price` was measured
    # ALIVE at 40/40 as a fail-open key, and it holds for every quantity-2 bid in production.
    roll = rng.random()
    if roll < 0.3:
        total_price = unit_price
    elif roll < 0.6:
        total_price = round(unit_price * rng.randrange(1, 9), 2)
    else:
        total_price = round(unit_price * rng.uniform(1.0, 5.0), 2)

    store_id = (
        ("store-1", "store-one", "store-external-1")[rng.randrange(3)]
        if fixture_shaped
        else _drawn_id(rng, "store", "store-one", "shop", "s")
    )
    variant_ref = (
        "44352913"
        if fixture_shaped
        else (None if rng.random() < 0.2 else _drawn_id(rng, "var", "variant", "v"))
    )
    # A real cart permalink is VARIANT-SCOPED (D25), so its cart id IS the offer's variant_ref.
    # Drawing the two independently meant no drawn bid ever had them agree, and "the url's cart
    # id equals variant_ref" — true of every real bid — ran ALIVE at 20/20 as a fail-open key.
    cart_id = variant_ref if variant_ref is not None else str(rng.randrange(10**9))
    offer = make_offer(
        product_ref=(
            ("prod-1", "prod-cap", "gate-prod-1")[rng.randrange(3)]
            if fixture_shaped
            else _drawn_id(rng, "prod", "product", "sku", "p")
        ),
        variant_ref=variant_ref,
        unit_price=unit_price,
        total_price=total_price,
        currency="USD" if fixture_shaped else _CURRENCIES[rng.randrange(len(_CURRENCIES))],
        discount=discount,
        commitments=[_drawn_claim(rng, provenance=shared) for _ in range(rng.randrange(0, 6))],
        expires_at=_drawn_timestamp(rng, future=True),
        # The host is drawn INDEPENDENTLY of `store_id` most of the time. Tying every url's host
        # to the drawn store id was itself a key, and one this generator INTRODUCED while closing
        # a different one: this repo's own fixture pairs `store_id="store-1"` with the host
        # `store-one.example.com`, so no realistic bid matched the shapes drawn here and
        # "checkout_url is one of my three shapes" ran ALIVE at 60/60.
        checkout_url=(
            None
            if rng.random() < 0.2
            else (
                f"https://{store_id}.example.com/cart/{cart_id}:1",
                f"https://store-one.example.com/cart/{cart_id}:1",
                f"https://example.test/{_drawn_id(rng, 'checkout', 'c')}",
                f"https://{_drawn_id(rng, 'shop', 'store-one')}.myshopify.com/{rng.randrange(10**9)}",
                f"https://{_drawn_id(rng, 'www', 'shop', 'cdn')}.example.org/p/{rng.randrange(10**6)}",
            )[rng.randrange(5)]
        ),
    )
    return make_bid(
        claims=claims,
        store_id=store_id,
        auction_id=(
            ("auc-1", "auc-0100", "auc-gate-1")[rng.randrange(3)]
            if fixture_shaped
            else _drawn_id(rng, "auc", "auction", "a")
        ),
        offer=offer,
        message=(None, _drawn_text(rng), "m" * rng.randrange(1, 120))[rng.randrange(3)],
        agent_version=(
            "store-agent/1.0.0"
            if fixture_shaped
            else (
                f"store-agent/{rng.randrange(9)}.{rng.randrange(9)}.{rng.randrange(9)}",
                f"{rng.randrange(9)}.{rng.randrange(99)}.{rng.randrange(99)}",
                _drawn_id(rng, "agent", "store-agent"),
            )[rng.randrange(3)]
        ),
        signature=(
            "sig-deadbeef" if fixture_shaped else _drawn_id(rng, "sig", "sig-deadbeef", "signature")
        ),
        schema_version=(
            "1.0.0" if fixture_shaped else ("1", "1.0.0", "1.1.0", "2.0.0")[rng.randrange(4)]
        ),
    )


def _eligible_snapshot(bid: Any, rng: Any = None) -> dict[str, Any]:
    """An R12 row for exactly this bid's store, so eligibility never answers for the price wall.

    Drawn too when an `rng` is given: a fixed `{"score": 0.6, "blacklisted": False}` row is one
    more template a fail-open can key on, and it was one of the 22 measured. `blacklisted` has
    several falsy spellings this door treats alike — `[]` is deliberately NOT among them,
    because `_js_truthy` reads JavaScript truthiness, where an empty array is TRUE.
    """
    store_id = bid["store_id"]
    if rng is None:
        return {store_id: {"store_id": store_id, "score": 0.6, "blacklisted": False}}

    row: dict[str, Any] = {"store_id": store_id, "score": round(rng.uniform(0.0, 1.0), 3)}
    if rng.random() < 0.8:
        # Sometimes omitted entirely: an absent flag defaults to not-blacklisted.
        row["blacklisted"] = (False, 0, "", 0.0)[rng.randrange(4)]

    table = {store_id: row}
    for _ in range(rng.randrange(0, 3)):
        # Unrelated rows, as a real snapshot carries. Their verdicts are irrelevant to this bid.
        other = _drawn_id(rng, "store", "store-one", "shop", "s")
        if other != store_id:
            table[other] = {
                "store_id": other,
                "score": round(rng.uniform(0.0, 1.0), 3),
                "blacklisted": rng.random() < 0.5,
            }
    return table


def _documented_specimen(
    total_price: float = 15.0,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """THE bid both tickets describe, in this repo's own fixture spelling.

    Returns `(bid, trust_snapshot, roster)`. It charges 15.00 for a product the exchange prices
    at 100.00 while declaring 20% — 85% off behind a 20% authorization — and carries no
    `list_price` claim of its own. `total_price` is a parameter because pinning it equal to the
    unit price made `total_price != unit_price` a one-field escape from this whole case.

    It is asserted alongside the drawn population because it can never BE drawn: every drawn bid
    is priced honestly for its declared depth so that its `supplied.ok is True` arming can pass,
    and this one is by construction priced dishonestly. That gap was measured to be a live
    gaming key — `NOT(unit_price == 15.0 and discount.value == 20.0)` took both gates green on
    20 consecutive fresh seeds while keeping the EXACT documented specimen admitted, unmodified.
    Any specific point in a two-decimal continuous draw has measure zero, so the answer is not a
    wider draw; it is to assert the point itself, with the arming inverted to suit a bid that is
    supposed to be refused.
    """
    from packages.contracts.tests._fixtures_protocol import make_offer

    bid = make_bid(
        offer=make_offer(
            unit_price=15.0,
            total_price=total_price,
            discount={"type": "percentage", "value": 20.0, "provenance": dict(HOOK_PROVENANCE)},
        )
    )
    snapshot = {"store-1": {"store_id": "store-1", "score": 0.6, "blacklisted": False}}
    roster = {"prod-1": {"list_price": 100.0, "max_discount_pct": 20.0}}
    return bid, snapshot, roster


def _judge(bid: Any, path: str, **kwargs: Any) -> Any:
    snapshot = kwargs.pop("snapshot", None)
    if snapshot is None:
        snapshot = _eligible_snapshot(bid)
    return validate_bid(bid, path=path, trust_snapshot=snapshot, now=NOW, **kwargs)


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


# T-306 — FIXED, marker removed here. JUSTIFY-TEST-EDIT for the deletion, which is a test edit
# like any other. The marker read:
#
#     @pytest.mark.xfail(
#         strict=True,
#         reason=(
#             "T-306: boundary.py's `if list_prices is None: return None, []` (in "
#             "_authorized_depth and _roster_list_price) makes the ABSENT roster more permissive "
#             "than an explicit empty one on the money path — measured, a bid charging 15.00 for "
#             "a 100.00 product is ok=True reasons=[] with list_prices omitted and ok=False "
#             "['price_unreconcilable:offer.discount:authorized_depth_unavailable', "
#             "'price_unreconcilable:offer.unit_price:list_price_unavailable'] with "
#             "list_prices={}; remove this marker with the fix"
#         ),
#     )
#
# What it claimed: this door does not yet hold the property below. That is no longer true, and
# under `strict=True` leaving it would turn a repaired defect into a RED suite — the marker
# cannot outlive the bug by design, and its own text says to delete it with the fix.
#
# CAUSATION PROVEN BEFORE DELETING, because "it passes now" is not the same claim as "my change
# is why". Measured in this worktree: with the three carve-outs re-introduced verbatim in
# `boundary.py` (`if list_prices is None: return None, []` in `_authorized_depth` and
# `_roster_list_price`, and `authorized = depth if list_prices is None else 0.0` plus the
# `list_prices is not None` guard in `_price_reasons`), this test and T-307's returned to
# `2 xfailed`, and 14 tests across this package went red. Restoring the fix returned both to
# passing. Nothing else in the tree moved between those two runs.
#
# THREE OTHER MARKERS IN THIS FILE ALSO XPASSED UNDER THE FIX AND ARE DELIBERATELY STILL HERE —
# T-161, T-162 and T-194. They XPASSed coincidentally, on the price wall's unrelated refusal of
# a bid `_check` used to send with no roster; see `_HONEST_ROSTER` at the top of this file. They
# are not fixed and their markers stay.
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
    (`test_boundary_dual_path.py::test_the_wall_answers_one_identical_refusal_to_every_spelling_of_no_roster`,
    `test_boundary_price_roster.py::test_the_cap_is_never_consulted_without_a_roster`). So this
    gate is a claim that the OPT-IN ITSELF is the defect on the money path, and closing it is a
    deliberate behaviour change with a measured blast radius — not the correction of an
    oversight. Whoever fixes it has to re-baseline those tests, and the TypeScript peer carries
    the identical asymmetry at `src/ts/boundary.ts:545` and `:580`.

    The property is about the ARGUMENT and therefore holds for every bid, which is why it is
    drawn rather than written — see the module comment above for the measured gaming keys a
    table of named shapes leaves open.

    Each draw arms itself before the comparison. With a roster that really prices the product the
    same bid must be ADMITTED, so the refusals below are the roster argument answering rather
    than the bid being unbuildable or blacklisted or expired; and the explicit empty roster must
    REFUSE, so the equality cannot be satisfied by collapsing both sides to `ok=True` — an
    "empty roster means no roster" repair points the fail-open the other way and is refused here
    by the `omitted.ok is False` assertion.

    Every draw is then run a SECOND time as an UNDER-PRICED variant of the same bid, armed the
    other way up (with the roster the wall must refuse it). Without that, the whole drawn
    population is honestly priced — the admission arming forces it — and the gate's only
    coverage of the case the ticket is actually about is the one hard-coded specimen below.
    """
    import random

    def _draw_once(rng: Any, seed: int, draw: int) -> str:
        """Build one case, arm it twice, then assert the property. Returns its `product_ref` so
        the caller can prove afterwards that the draws were genuinely distinct."""
        # Round numbers are drawn as often as ragged ones, and the margin over the honest price
        # is sometimes exactly zero, so the ticket's own documented specimen (100.00 listed,
        # 20% declared) is inside the population rather than the one bid a fail-open can spare.
        list_price = (
            round(rng.uniform(5.0, 5000.0), 2)
            if rng.random() < 0.7
            else (49.0, 100.0, 250.0, 1000.0)[rng.randrange(4)]
        )
        row_cap = (
            round(rng.uniform(5.0, 60.0), 1)
            if rng.random() < 0.7
            else (10.0, 20.0, 25.0)[rng.randrange(3)]
        )
        depth = (
            round(rng.uniform(0.0, row_cap), 1)
            if rng.random() < 0.7
            else min(row_cap, (0.0, 10.0, 20.0)[rng.randrange(3)])
        )
        # THE SPECIMEN'S PRICE LINE, DRAWN rather than pinned. One draw in five uses the
        # ticket's own numbers — a 100.00 list at a declared 20% — so `unit_price == 15.00` at
        # `depth == 20%` is INSIDE the population rather than three hard-coded points beside it.
        # Measured: with only those points, `total_price != 45.00` — the identical bid at
        # quantity three — took both gates green on 20 consecutive fresh seeds with the money
        # path open. Three points are not a population, and a fourth point would not have been
        # one either; the line itself has to be drawable.
        on_specimen_line = rng.random() < 0.2
        if on_specimen_line:
            list_price, row_cap, depth = 100.0, 20.0, 20.0

        # The HONEST price for that depth. The margin is drawn: at zero the tolerance still
        # clears the wall, so the price can land exactly on the arithmetic rather than a cent above.
        margin = (0.0, 0.0, 0.01, round(rng.uniform(0.0, 40.0), 2))[rng.randrange(4)]
        unit_price = round(list_price * (100.0 - depth) / 100.0 + margin, 2)
        path = (HOSTED_PATH, EXTERNAL_PATH)[rng.randrange(2)]

        bid = _drawn_priced_bid(
            rng,
            depth=depth,
            list_price=list_price,
            unit_price=unit_price,
            carries_list_price=rng.random() < 0.5,
        )
        product_ref = bid["offer"]["product_ref"]
        # ONE drawn snapshot, reused across all three calls: the comparison below is about the
        # roster argument, so every other input has to be held identical between them.
        snap = _eligible_snapshot(bid, rng)
        roster = {product_ref: {"list_price": list_price, "max_discount_pct": row_cap}}

        supplied = _judge(bid, path, snapshot=snap, list_prices=roster)
        assert supplied.ok is True, (
            f"seed {seed} draw {draw}: arming — with a roster that prices this product at "
            f"{list_price} and authorizes {row_cap}%, an honest bid declaring {depth}% at "
            f"{unit_price} must be admitted, or the refusals below say nothing about the roster "
            f"ARGUMENT. path={path} bid={bid!r} result={supplied!r}"
        )

        empty = _judge(bid, path, snapshot=snap, list_prices={})
        assert empty.ok is False, (
            f"seed {seed} draw {draw}: control — a roster that cannot price this product is an "
            f"unavailable read and must be refused, never degraded back to the abstention. "
            f"path={path} bid={bid!r} result={empty!r}"
        )

        omitted = _judge(bid, path, snapshot=snap)
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

        # AN UNDER-PRICED VARIANT OF THE SAME BID. Every draw above is priced HONESTLY for its
        # declared depth — forced by the `supplied.ok is True` arming, which is what makes the
        # comparison mean anything — so the drawn population contained no under-priced bid at
        # all, and this gate's entire coverage of the case the ticket is ABOUT rested on one
        # hard-coded specimen. Measured: a fail-open that spares that single point takes both
        # gates green at 40/40 with the money path open. A point is not a population, and the
        # answer is a drawn population of them, armed the other way up: with the roster the wall
        # must REFUSE this bid, which is what proves it reached the wall at all.
        cheat = (
            15.0
            if on_specimen_line
            else round(max(list_price * 0.01, list_price * (100.0 - depth) / 100.0 * 0.4), 2)
        )
        underpriced = dict(bid)
        underpriced["offer"] = dict(bid["offer"])
        underpriced["offer"]["unit_price"] = cheat
        # A REAL QUANTITY, not always one. Every dishonestly-priced bid this gate judged used to
        # set `total_price == unit_price`, and so did the hard-coded specimen — so the entire
        # class "any trait the drawn population never emits AND total_price != unit_price" walked
        # straight through, measured ALIVE at 20/20 on three independent traits. The escape was
        # one field wide.
        quantity_roll = rng.random()
        if quantity_roll < 0.3:
            underpriced["offer"]["total_price"] = cheat
        elif quantity_roll < 0.65:
            underpriced["offer"]["total_price"] = round(cheat * rng.randrange(2, 9), 2)
        else:
            # CONTINUOUS as well as integral, so no finite set of totals can be spared.
            underpriced["offer"]["total_price"] = round(cheat * rng.uniform(1.0, 9.0), 2)

        cheat_rostered = _judge(underpriced, path, snapshot=snap, list_prices=roster)
        assert cheat_rostered.ok is False and "price_under_declared_depth:offer.unit_price" in list(
            cheat_rostered.reasons
        ), (
            f"seed {seed} draw {draw}: arming the under-priced variant — charging {cheat} for a "
            f"product the roster prices at {list_price} while declaring {depth}% must be refused "
            f"by the price wall: path={path} result={cheat_rostered!r}"
        )

        cheat_empty = _judge(underpriced, path, snapshot=snap, list_prices={})
        assert cheat_empty.ok is False, (
            f"seed {seed} draw {draw}: control — an empty roster must refuse the under-priced "
            f"variant: path={path} result={cheat_empty!r}"
        )

        cheat_omitted = _judge(underpriced, path, snapshot=snap)
        assert _verdict(cheat_omitted) == _verdict(cheat_empty), (
            f"seed {seed} draw {draw}: for an UNDER-PRICED bid, omitting list_prices gave "
            f"{_verdict(cheat_omitted)} where an explicit empty roster gave "
            f"{_verdict(cheat_empty)}. This is the shape the ticket is about — a bid charging "
            f"{cheat} for a {list_price} product — and forgetting the roster is what admits it. "
            f"path={path} bid={underpriced!r}"
        )
        return repr(bid)

    # THE DOCUMENTED SPECIMEN — the one bid the ticket names, which the drawn population cannot
    # contain. Armed inversely: a roster that prices the product must REFUSE it (proving it
    # reaches the price wall), and the empty roster must refuse it too.
    for spec_total in (15.0, 30.0, 105.0):
        spec, spec_snap, spec_roster = _documented_specimen(spec_total)
        spec_rostered = _judge(spec, HOSTED_PATH, snapshot=spec_snap, list_prices=spec_roster)
        assert spec_rostered.ok is False and "price_under_declared_depth:offer.unit_price" in list(
            spec_rostered.reasons
        ), (
            f"arming the documented specimen at total={spec_total}: 15.00 for a product the "
            f"roster prices at 100.00 under a 20% authorization must be refused by the price "
            f"wall, or this case proves nothing: {spec_rostered!r}"
        )
        spec_empty = _judge(spec, HOSTED_PATH, snapshot=spec_snap, list_prices={})
        assert spec_empty.ok is False, (
            f"control at total={spec_total}: an empty roster must refuse: {spec_empty!r}"
        )
        spec_omitted = _judge(spec, HOSTED_PATH, snapshot=spec_snap)
        assert _verdict(spec_omitted) == _verdict(spec_empty), (
            f"THE DOCUMENTED SPECIMEN at total={spec_total}: omitting list_prices gave "
            f"{_verdict(spec_omitted)} where an explicit empty roster gave "
            f"{_verdict(spec_empty)}. This is the exact bid the ticket describes — 85% off "
            f"behind a 20% authorization — and forgetting the roster admitted it"
        )

    exercised = 0
    distinct: set[str] = set()
    for seed in _property_seeds(20260904):
        rng = random.Random(seed)
        for draw in range(_DRAWS_PER_SEED):
            distinct.add(_draw_once(rng, seed, draw))
            exercised += 1

    # A LITERAL 120, never `2 * _DRAWS_PER_SEED`. Measured: setting that constant to 1 left the
    # ordinary run, the canary AND the positive control all green while each property measured
    # two draws instead of 120 — the guard was comparing the constant against itself, which is
    # the same tautology as a loop that iterates zero cases and reports success.
    assert _DRAWS_PER_SEED >= 60, (
        f"_DRAWS_PER_SEED shrank to {_DRAWS_PER_SEED}; these properties are sized in the ticket "
        f"gate at 60 draws per seed and the guards below are written against a literal 120"
    )
    expected = 120
    assert exercised == expected, (
        f"only {exercised} of {expected} cases were built, armed and compared. A loop that "
        f"silently iterates fewer cases than it claims is how three sweeps in this repo went "
        f"QUIET rather than red (6->0 of 8, 70->0 of 79, 48->0 of 66)"
    )
    assert len(distinct) == expected, (
        f"the generator produced {len(distinct)} distinct bids across {expected} draws; a "
        f"property asserted over one repeated bid is a single-payload probe wearing a loop"
    )


# T-307 — FIXED, marker removed here. JUSTIFY-TEST-EDIT for the deletion. The marker read:
#
#     @pytest.mark.xfail(
#         strict=True,
#         reason=(
#             "T-307: `max_discount_pct` supplied WITHOUT `list_prices` is never read — "
#             "boundary.py's `if list_prices is None: return None, []` in _authorized_depth "
#             "returns before the ceiling is consulted, so the authorization ceiling is inert "
#             "whenever the roster is absent. Measured: a bid declaring 85% and carrying its own "
#             "list_price claim is ok=True reasons=[] under max_discount_pct=20.0 with no "
#             "roster, and ok=False with list_prices={} and the same ceiling; remove this marker "
#             "with the fix"
#         ),
#     )
#
# Same grounds as T-306's above, same causation proof (both returned to `xfailed` together when
# the carve-outs were re-introduced, and to passing when the fix was restored), and the same
# `strict=True` reason why leaving it is not the safe option. The ceiling is now read whether or
# not a roster is passed; `test_boundary_price_roster.py::
# test_the_cap_is_consulted_even_when_no_roster_is_passed` and the two `a_call_wide_ceiling_...`
# rows in the shared parity corpus assert that property outside this file, in both languages, so
# deleting this marker does not leave the requirement resting on a gate nobody runs.
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
        list_price = (
            round(rng.uniform(5.0, 5000.0), 2)
            if rng.random() < 0.7
            else (49.0, 100.0, 250.0, 1000.0)[rng.randrange(4)]
        )
        ceiling = (
            round(rng.uniform(0.0, 30.0), 1)
            if rng.random() < 0.7
            else (0.0, 10.0, 20.0, 25.0)[rng.randrange(4)]
        )
        depth = (
            round(rng.uniform(ceiling + 10.0, 95.0), 1)
            if rng.random() < 0.7
            else max(ceiling + 10.0, (50.0, 75.0, 85.0, 90.0)[rng.randrange(4)])
        )
        margin = (0.0, 0.0, 0.01, round(rng.uniform(0.0, 5.0), 2))[rng.randrange(4)]
        unit_price = round(list_price * (100.0 - depth) / 100.0 + margin, 2)
        path = (HOSTED_PATH, EXTERNAL_PATH)[rng.randrange(2)]

        bid = _drawn_priced_bid(
            rng,
            depth=depth,
            list_price=list_price,
            unit_price=unit_price,
            carries_list_price=True,
        )
        product_ref = bid["offer"]["product_ref"]
        # ONE drawn snapshot across all four calls — see the T-306 draw for why.
        snap = _eligible_snapshot(bid, rng)

        # ARM 1 — the bid is otherwise clean. A roster that prices the product AND authorizes
        # exactly the depth it declares admits it, so a refusal below is the ceiling and not the
        # schema, R8, the expiry, R12 or the arithmetic. This is stated with a roster PRESENT on
        # purpose; see the docstring for why arming on the no-roster call would put this gate in
        # direct contradiction with T-306's.
        authorizing = {product_ref: {"list_price": list_price, "max_discount_pct": depth}}
        clean = _judge(bid, path, snapshot=snap, list_prices=authorizing)
        assert clean.ok is True, (
            f"seed {seed} draw {draw}: arming — a bid declaring {depth}% at {unit_price} for a "
            f"product the roster prices at {list_price} and authorizes {depth}% on must be "
            f"admitted, or the refusals below say nothing about the CEILING. path={path} "
            f"bid={bid!r} result={clean!r}"
        )

        # ARM 2 — the ceiling is a number this door reads, and this bid exceeds it. The roster
        # prices the product and authorizes nothing of its own, so the CALLER-WIDE ceiling is
        # what `_authorized_depth` has to fall through to.
        bounded = _judge(
            bid,
            path,
            snapshot=snap,
            list_prices={product_ref: list_price},
            max_discount_pct=ceiling,
        )
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
        empty = _judge(bid, path, snapshot=snap, list_prices={}, max_discount_pct=ceiling)
        assert empty.ok is False, (
            f"seed {seed} draw {draw}: control — an explicitly empty roster with a ceiling of "
            f"{ceiling}% must refuse. path={path} bid={bid!r} result={empty!r}"
        )

        omitted = _judge(bid, path, snapshot=snap, max_discount_pct=ceiling)
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
        return repr(bid)

    # THE DOCUMENTED SPECIMEN under a ceiling of zero — "I authorize no discount at all", and
    # the roster forgotten. Armed inversely, as in T-306.
    for spec_total in (15.0, 30.0, 105.0):
        spec, spec_snap, _ = _documented_specimen(spec_total)
        spec_bounded = _judge(
            spec,
            HOSTED_PATH,
            snapshot=spec_snap,
            list_prices={"prod-1": 100.0},
            max_discount_pct=0.0,
        )
        assert spec_bounded.ok is False and any(
            reason.startswith("discount_over_authorized_depth:") for reason in spec_bounded.reasons
        ), (
            f"arming the documented specimen at total={spec_total}: a 20% depth under a 0% "
            f"caller ceiling must be refused for exceeding the authorized depth once a roster "
            f"is present: {spec_bounded!r}"
        )
        spec_empty = _judge(
            spec, HOSTED_PATH, snapshot=spec_snap, list_prices={}, max_discount_pct=0.0
        )
        assert spec_empty.ok is False, (
            f"control at total={spec_total}: empty roster plus a 0% ceiling must refuse: "
            f"{spec_empty!r}"
        )
        spec_omitted = _judge(spec, HOSTED_PATH, snapshot=spec_snap, max_discount_pct=0.0)
        assert _verdict(spec_omitted) == _verdict(spec_empty), (
            f"THE DOCUMENTED SPECIMEN at total={spec_total}: a 0% ceiling with the roster "
            f"OMITTED gave {_verdict(spec_omitted)} where the same ceiling with an explicit "
            f"empty roster gave {_verdict(spec_empty)}. The caller authorized nothing and was "
            f"ignored"
        )

    exercised = 0
    distinct: set[str] = set()
    for seed in _property_seeds(20260905):
        rng = random.Random(seed)
        for draw in range(_DRAWS_PER_SEED):
            distinct.add(_draw_once(rng, seed, draw))
            exercised += 1

    # A LITERAL 120, never `2 * _DRAWS_PER_SEED`. Measured: setting that constant to 1 left the
    # ordinary run, the canary AND the positive control all green while each property measured
    # two draws instead of 120 — the guard was comparing the constant against itself, which is
    # the same tautology as a loop that iterates zero cases and reports success.
    assert _DRAWS_PER_SEED >= 60, (
        f"_DRAWS_PER_SEED shrank to {_DRAWS_PER_SEED}; these properties are sized in the ticket "
        f"gate at 60 draws per seed and the guards below are written against a literal 120"
    )
    expected = 120
    assert exercised == expected, (
        f"only {exercised} of {expected} cases were built, armed and compared. A loop that "
        f"silently iterates fewer cases than it claims is how three sweeps in this repo went "
        f"QUIET rather than red (6->0 of 8, 70->0 of 79, 48->0 of 66)"
    )
    assert len(distinct) == expected, (
        f"the generator produced {len(distinct)} distinct bids across {expected} draws; a "
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

    **It covers every configuration the two gates actually draw**, which the first version of
    this canary did not. It ran one point only (hosted, `carries_list_price=True`), and that was
    measured to be worth nothing at the edges: a generator sabotaged to raise on
    ``EXTERNAL_PATH``, and one sabotaged to raise when ``carries_list_price=False``, BOTH left an
    ordinary run reporting ``1 passed, 8 xfailed`` — byte-identical to health, with the two gates
    quietly scoring errors as reproductions. A canary that only walks the happy corner is the
    failure it exists to detect, one level further up again.
    """
    import random

    assert _DRAWS_PER_SEED >= 60, (
        f"_DRAWS_PER_SEED is {_DRAWS_PER_SEED}. Nothing outside this file pins it, and shrinking "
        f"it silently narrows both properties — measured, setting it to 1 left the ordinary run "
        f"green while each gate measured two draws instead of 120"
    )

    rng = random.Random(20260906)
    seen = 0

    # Both paths x both claim configurations x five numeric points spanning what the gates
    # ACTUALLY draw (depth to 95, list price to 5000). The numeric axis matters and its RANGE
    # matters: against a version of this canary that stopped at 62.5, a generator sabotaged to
    # raise for `depth > 62.5` left an ordinary run reporting `1 passed, 8 xfailed` while T-307
    # scored a total runtime error as a reproduction.
    for path in (HOSTED_PATH, EXTERNAL_PATH):
        for carries in (True, False):
            for list_price, depth in (
                (100.0, 20.0),
                (49.0, 0.0),
                (3499.99, 62.5),
                (5000.0, 95.0),
                (100.0, 85.0),
            ):
                unit_price = round(list_price * (100.0 - depth) / 100.0, 2)
                bid = _drawn_priced_bid(
                    rng,
                    depth=depth,
                    list_price=list_price,
                    unit_price=unit_price,
                    carries_list_price=carries,
                )
                product_ref = bid["offer"]["product_ref"]
                snap = _eligible_snapshot(bid, rng)
                where = f"path={path} carries={carries} {depth}% off {list_price}"

                admitted = _judge(
                    bid,
                    path,
                    snapshot=snap,
                    list_prices={
                        product_ref: {"list_price": list_price, "max_discount_pct": depth}
                    },
                )
                assert admitted.ok is True, (
                    f"{where}: the T-306/T-307 generator can no longer build a bid this door "
                    f"admits, so both gates are scoring errors as reproductions: {admitted!r}"
                )

                refused = _judge(bid, path, snapshot=snap, list_prices={})
                assert refused.ok is False, (
                    f"{where}: an explicitly empty roster must refuse — it is the right-hand "
                    f"side both gates compare the absent argument against: {refused!r}"
                )
                seen += 1

    assert seen == 20, f"the canary walked {seen} of 20 configurations"

    # GENERATOR SPREAD — the assertion that actually guards the gaming analysis.
    #
    # Everything the two gates claim about being un-gameable rests on the generator EMITTING the
    # shapes it advertises. A generator quietly NARROWED — never emitting a dashed id, never
    # stamping a provenance in the current year, never landing on midnight — stays green
    # everywhere, canary and positive control included, while silently resurrecting fail-open
    # keys that were measured dead. Two such narrowings were demonstrated and both were invisible
    # to every other check in this file. So the spread is measured rather than assumed, on a
    # pinned seed so it cannot flake. Each threshold sits far below its expectation.
    stats = {
        k: 0
        for k in (
            "dashed",
            "current_year",
            "midnight",
            "reused",
            "foreign_host",
            "integer_total",
            "late_day",
            "fixture_shaped",
            "other_host",
            "usd",
            "schema_1_0_0",
            "agent_fixture",
            "message_none",
            "variant_cart_url",
            "hook_provenance",
            "no_claims",
            "no_commitments",
            "no_variant",
            "no_url",
            "pct_spelling",
        )
    }
    currencies: set[Any] = set()
    for sample in range(250):
        # BOTH claim configurations. Sampling only `carries_list_price=True` always inserts a
        # `list_price` claim, so `claims` was never empty and the floor below could not see it.
        b = _drawn_priced_bid(
            rng,
            depth=20.0,
            list_price=100.0,
            unit_price=80.0,
            carries_list_price=sample % 2 == 0,
        )
        offer = b["offer"]
        currencies.add(offer["currency"])
        if "-" in str(offer["product_ref"]) and str(offer["product_ref"])[-1].isdigit():
            stats["dashed"] += 1
        stamps = [c["provenance"]["observed_at"] for c in b["claims"]]
        stamps += [c["provenance"]["observed_at"] for c in offer["commitments"]]
        if offer.get("discount"):
            stamps.append(offer["discount"]["provenance"]["observed_at"])
        if any(t[:4] >= "2026" for t in stamps):
            stats["current_year"] += 1
        # DRAWN stamps only. `HOOK_PROVENANCE["observed_at"]` is itself midnight, so once the
        # fixture-shaped draw started carrying that block verbatim this floor was counting a
        # CONSTANT: measured 50/250 midnights, all of them the fixture's, and zero from a drawn
        # stamp — so forcing every drawn timestamp off midnight left the floor satisfied and the
        # narrowing invisible. A floor that can be satisfied by something the generator does not
        # draw is not a floor.
        drawn_stamps = [t for t in stamps if t != HOOK_PROVENANCE["observed_at"]]
        if any(t.endswith("T00:00:00Z") for t in drawn_stamps + [offer["expires_at"]]):
            stats["midnight"] += 1
        blocks = [repr(c["provenance"]) for c in b["claims"]]
        if len(blocks) > 1 and len(set(blocks)) < len(blocks):
            stats["reused"] += 1
        url = offer.get("checkout_url")
        if url and str(b["store_id"]) not in url:
            stats["foreign_host"] += 1
        if url and ("example.test" in url or "myshopify" in url or "example.org" in url):
            # The url pool is more than "my own host" plus one alternative — cutting it back to
            # two shapes resurrected the checkout_url key while `foreign_host` stayed satisfied.
            stats["other_host"] += 1
        unit, total = offer["unit_price"], offer["total_price"]
        if unit and total >= unit * 2 and abs(total / unit - round(total / unit)) < 1e-9:
            # A real quantity is an integer. A continuous multiplier never lands on one.
            stats["integer_total"] += 1
        if int(str(offer["expires_at"])[8:10]) >= 29:
            stats["late_day"] += 1
        # The COHERENT fixture-shaped bid, checked across ALL SIX fields. A floor over two of
        # them stays satisfied while `store_id`/`auction_id`/`product_ref` are redrawn off the
        # fixture values, and the six-field conjunction key comes straight back to life — that
        # exact narrowing was measured invisible to a two-field floor.
        if (
            str(b["signature"]) == "sig-deadbeef"
            and str(offer["variant_ref"]) == "44352913"
            and str(b["store_id"]) in {"store-1", "store-one", "store-external-1"}
            and str(b["auction_id"]) in {"auc-1", "auc-0100", "auc-gate-1"}
            and str(offer["product_ref"]) in {"prod-1", "prod-cap", "gate-prod-1"}
            and str(b["agent_version"]) == "store-agent/1.0.0"
        ):
            stats["fixture_shaped"] += 1
        # Individually unremarkable values that a real bid always carries. Each was measured as a
        # live key at 20/20 once the generator stopped emitting it, and no other floor saw it.
        if offer["currency"] == "USD":
            stats["usd"] += 1
        if b["schema_version"] == "1.0.0":
            stats["schema_1_0_0"] += 1
        if b["agent_version"] == "store-agent/1.0.0":
            stats["agent_fixture"] += 1
        if b["message"] is None:
            stats["message_none"] += 1
        if url and offer["variant_ref"] and f"/cart/{offer['variant_ref']}:" in url:
            # A cart permalink is variant-scoped, so a real url's cart id IS the variant_ref.
            stats["variant_cart_url"] += 1
        if offer.get("discount") and offer["discount"]["provenance"] == dict(HOOK_PROVENANCE):
            stats["hook_provenance"] += 1
        # Axes nothing was watching. Each is a shape a real bid has and a narrowed generator
        # could silently stop producing, which is exactly how the midnight floor was defanged.
        if not b["claims"]:
            stats["no_claims"] += 1
        if not offer["commitments"]:
            stats["no_commitments"] += 1
        if offer["variant_ref"] is None:
            stats["no_variant"] += 1
        if offer.get("checkout_url") is None:
            stats["no_url"] += 1
        if offer.get("discount") and offer["discount"]["type"] in ("percent", "pct"):
            stats["pct_spelling"] += 1

    floors = {
        "dashed": 10,
        "current_year": 10,
        "midnight": 20,
        "reused": 10,
        "foreign_host": 20,
        "integer_total": 10,
        "late_day": 5,
        "fixture_shaped": 10,
        "other_host": 10,
        "usd": 10,
        "schema_1_0_0": 10,
        "agent_fixture": 10,
        "message_none": 20,
        "variant_cart_url": 10,
        "hook_provenance": 10,
        "no_claims": 5,
        "no_commitments": 20,
        "no_variant": 10,
        "no_url": 20,
        "pct_spelling": 40,
    }
    thin = {k: v for k, v in stats.items() if v < floors[k]}
    assert not thin, (
        f"the bid generator has been NARROWED: {thin} out of 250 draws, against floors {floors}. "
        f"Each of these shapes closes a fail-open key that was measured ALIVE without it — a "
        f"dashed id (B02/B03/B07), a provenance observed in the current year (B20), a midnight "
        f"timestamp, a provenance block reused within one bid (B21/B22), a checkout host "
        f"unrelated to the store id and a url pool wider than two shapes (B04), an integer "
        f"quantity in total_price, a day-of-month past the 28th, and the COHERENT "
        f"fixture-shaped bid (B05/B05b, whose conjunction no per-field floor can see). A "
        f"generator that stops emitting one of them leaves "
        f"every test in this file green while the gate it protects becomes satisfiable without "
        f"the fix. Full spread: {stats}"
    )
    assert len(currencies) >= 6, (
        f"the generator emitted only {len(currencies)} distinct currencies across 250 draws: "
        f"{sorted(currencies, key=str)}"
    )

    # THE JS-TRUTHINESS TRAP, pinned rather than left to a comment. `_eligible_snapshot` spells
    # `blacklisted` several FALSY ways, and `[]` must never join them: `_js_truthy` reads
    # JavaScript truthiness, where an empty array is TRUE, so a row spelling the flag `[]` is a
    # BLACKLISTED store. A generator that quietly added it would refuse every armed draw in both
    # gates, and the failure would read as the fix being wrong rather than the fixture.
    trapped = {bid["store_id"]: {"store_id": bid["store_id"], "score": 0.5, "blacklisted": []}}
    blocked = _judge(bid, HOSTED_PATH, snapshot=trapped)
    assert blocked.ok is False and any(
        reason.startswith("store_blacklisted:") for reason in blocked.reasons
    ), (
        f"an empty list is TRUTHY in JavaScript, so `blacklisted: []` is a blacklisted store and "
        f"must never be used as a falsy spelling in a fixture: {blocked!r}"
    )

    # The seeds themselves. `isinstance(drawn, int)` alone proves nothing — it is satisfied by a
    # `_property_seeds` de-randomized to a constant pair, which is the one property its docstring
    # calls load-bearing. Two calls must disagree on the drawn half; collision odds are 2**-32.
    first_pinned, first_drawn = _property_seeds(20260906)
    second_pinned, second_drawn = _property_seeds(20260906)
    assert first_pinned == second_pinned == 20260906, (
        f"_property_seeds must return the pinned seed it was given, got "
        f"{(first_pinned, second_pinned)!r}"
    )
    assert first_drawn != second_drawn, (
        f"_property_seeds returned the same 'drawn' seed twice ({first_drawn}), so the two "
        f"properties are running over a constant table after all — which is precisely the hole "
        f"the unpinned seed exists to close, and 22 measured fail-open keys walk through it"
    )


# =============================================================================================
# T-162 (part two) — the discount-authorisation walk's OWN bound fails open
# =============================================================================================


# NO `xfail` MARKER, deliberately: this gate lands in the same change as the repair it names, so
# it is RED on the parent commit and GREEN here. A `strict` marker on a test its own commit
# fixes is an XPASS failure by construction. The BEFORE measurement is quoted verbatim in the
# docstring below, and reverting `_mentions_discount_authorisation` reproduces it exactly.
def test_an_exhausted_discount_authorisation_walk_is_not_reported_as_a_clean_pass() -> None:
    """A bounded walk that ran out of budget has not said "there is nothing here".

    ``_mentions_discount_authorisation`` (packages/contracts/src/boundary.py) is the walk that
    finds a self-granted ``authorized_discount_pct`` / ``max_discount_pct`` buried anywhere
    inside a claim's opaque ``value``. It is bounded — ``NESTED_PROVENANCE_MAX_DEPTH`` and
    ``NESTED_PROVENANCE_MAX_NODES`` — and it used to answer exhaustion with the same word it
    uses for "I looked everywhere and found nothing"::

        if depth > NESTED_PROVENANCE_MAX_DEPTH or budget <= 0:
            return False

    So no caller could tell a finished look from an abandoned one, and padding is free to
    whoever writes the value. Its sibling ``_nested_provenance_sources`` gets this right: it
    returns ``(found, truncated)`` and ``_source_verdict`` refuses on ``truncated``.

    The padding does not even have to be visible to that sibling, which is why its
    ``claim_value_unwalkable`` did not already cover this: the sibling deliberately does NOT
    descend into a *recognised* provenance block (it has already judged it), so a block wearing
    a valid hook ``source`` is a free hiding place — the sibling spends one node on it and
    answers ``truncated=False`` while the authorisation walk spends its whole budget inside it.
    Measured on this tree, external path, with the honest roster::

        grant = {"authorized_discount_pct": 25.0}          # six {"w": …} wrappers around it
        value = {"provenance": {**HOOK_PROVENANCE, "pad": <the wrapped grant>}}
        validate_bid(make_bid(claims=[make_claim("policy", value, HOOK_PROVENANCE)]),
                     path="external", ...)
        -> ok=True  reasons=[]  requires_verification=False  unverified_claim_indexes=[]

    while the identical grant written plainly answers ``ok=True requires_verification=True
    unverified_claim_indexes=[0]``. Same grant, same door, same bid; only the padding changed,
    and the padding is what turned a flagged claim into a silent one. The node-budget spelling
    of the same move — 600 sibling containers written inside that provenance block — measured
    identically, which is why both are pinned below.

    What this test refuses is only that third outcome: a walk that did not finish, reported as a
    clean pass. Every defensible repair satisfies it — refuse the bid (``claim_value_unwalkable``,
    which is what the sibling's truncation already does), or route the claim to verification the
    way a *found* authorisation is routed. It does not choose between them.
    """

    def buried(wrappers: int) -> dict[str, Any]:
        grant: Any = {"authorized_discount_pct": 25.0}
        for _ in range(wrappers):
            grant = {"w": grant}
        block = dict(HOOK_PROVENANCE)
        block["pad"] = grant
        return {"provenance": block}

    # POSITIVE CONTROL. The same grant, unpadded, must still be seen — without this the gate
    # would also pass on a boundary that stopped looking for discount authorisations at all.
    plain = make_claim("policy", {"authorized_discount_pct": 25.0}, dict(HOOK_PROVENANCE))
    control = _check(make_bid(claims=[plain]), EXTERNAL_PATH)
    assert control.ok is False or control.requires_verification is True, (
        "positive control: an unpadded self-asserted discount authorisation must still be "
        f"caught on the external path, got ok={control.ok} reasons={control.reasons} "
        f"requires_verification={control.requires_verification}"
    )

    # SECOND POSITIVE CONTROL. The same shape carrying NO authorisation must still be admitted —
    # without it, "refuse anything with a provenance-shaped value in it" would satisfy the
    # assertions below while refusing honest bids.
    innocuous = dict(HOOK_PROVENANCE)
    innocuous["pad"] = {"w": {"w": {"w": {"colour": "blue"}}}}
    harmless = _check(
        make_bid(claims=[make_claim("policy", {"provenance": innocuous}, dict(HOOK_PROVENANCE))]),
        EXTERNAL_PATH,
    )
    assert harmless.ok is True and harmless.requires_verification is False, (
        "second positive control: a nested value carrying no discount authorisation and no "
        f"laundered provenance must still be admitted clean, got ok={harmless.ok} "
        f"reasons={harmless.reasons} requires_verification={harmless.requires_verification}"
    )

    # THE DEFECT, in its depth spelling.
    deep = _check(
        make_bid(claims=[make_claim("policy", buried(6), dict(HOOK_PROVENANCE))]), EXTERNAL_PATH
    )
    assert deep.ok is False or deep.requires_verification is True, (
        "a self-asserted discount authorisation buried past NESTED_PROVENANCE_MAX_DEPTH was "
        f"admitted with nothing said: ok={deep.ok} reasons={deep.reasons} "
        f"requires_verification={deep.requires_verification} "
        f"unverified_claim_indexes={deep.unverified_claim_indexes}"
    )

    # ...and in its node-budget spelling, the same fail-open reached by width rather than by
    # depth. Pinned separately because a repair that only re-checked the depth guard would leave
    # this one wide open.
    wide = dict(HOOK_PROVENANCE)
    for index in range(600):
        wide[f"pad_{index:04d}"] = {"x": index}
    wide["zzz_grant"] = {"authorized_discount_pct": 25.0}
    result = _check(
        make_bid(claims=[make_claim("policy", {"provenance": wide}, dict(HOOK_PROVENANCE))]),
        EXTERNAL_PATH,
    )
    assert result.ok is False or result.requires_verification is True, (
        "a self-asserted discount authorisation written behind NESTED_PROVENANCE_MAX_NODES "
        f"worth of padding was admitted with nothing said: ok={result.ok} "
        f"reasons={result.reasons} requires_verification={result.requires_verification} "
        f"unverified_claim_indexes={result.unverified_claim_indexes}"
    )


def test_one_unwalkable_claim_value_is_reported_once_not_once_per_walk() -> None:
    """Two bounded walks over one value are two looks at one thing, not two findings.

    ``_source_verdict`` walks a claim's ``value`` for laundered provenance and
    ``_authorisation_verdict`` walks the same value for a self-granted discount depth. Both are
    bounded by the same two constants, so one padded value exhausts both — and once the second
    walk is allowed to report its exhaustion, the byte-identical ``claim_value_unwalkable:0``
    would be logged twice for one claim. That is the mislabelling this module already refuses
    for ``not_positive``/``below_price_floor`` and for a field the schema model has already
    complained about: one finding, reported once, however many walks ran into it.
    """
    padded: Any = {"grant": {"authorized_discount_pct": 25.0}}
    for _ in range(8):
        padded = {"w": padded}

    result = _check(
        make_bid(claims=[make_claim("policy", padded, dict(HOOK_PROVENANCE))]), EXTERNAL_PATH
    )

    assert result.ok is False or result.requires_verification is True, (
        "a value too deep for either walk to finish was admitted clean: "
        f"ok={result.ok} reasons={result.reasons}"
    )
    assert len(result.reasons) == len(set(result.reasons)), (
        f"one claim, one unfinished value, and the same reason logged twice: {result.reasons}"
    )


# =============================================================================================
# T-276 — a product the roster lists at one cent could not be bid on at all
# =============================================================================================


def test_t276_the_absolute_half_of_the_price_floor_cannot_eat_a_cheap_products_whole_range() -> (
    None
):
    """The floor's absolute half was a flat cent, so on a cheap product it WAS the price.

    ``price_floor`` takes the larger of a proportional half (0.1% of list) and an absolute half
    (one minor currency unit), and drops the absolute half only where the roster lists the
    product BELOW one minor unit. That drop was written precisely so a catalog pricing something
    at 0.005 is not refused every bid on it — but it stopped one hundredth of a cent too early.
    At exactly ``0.01`` the drop does not apply, ``max(1e-05, 0.01)`` IS the list price, and every
    price under list is under the floor. Measured on this door before the repair::

        price_reasons(bid 0.009, roster {list 0.01, max_discount_pct 100})
          -> ['price_unreconcilable:offer.unit_price:below_price_floor',
              'price_unreconcilable:offer.total_price:below_price_floor']

    Closed rather than fail-closed — the direction this wall's positive controls exist to catch,
    and the same "refuses everything" failure the drop was written to avoid.

    THIS GATE IS BEHAVIOURAL AND ACCEPTS EVERY DEFENSIBLE REPAIR. It does not name a formula or a
    constant: it asks that a cheap product keep a usable range, and — in the same breath, because
    a floor is only a floor if it still refuses something — that every price the wall refuses
    today stay refused. The controls are not decoration. A repair that simply deleted the
    absolute half, or lowered it globally, satisfies the first assertion and fails the rest:
    0.005 on a 5.00 product and the half cent on a 0.50 one are exactly the prices
    ``MINIMUM_PAYABLE_AMOUNT`` exists to refuse, and they are what stops "give the cheap band a
    range" from becoming "open the floor everywhere".

    The 0.50 row is the sharpest of them. The shared price-parity corpus pins that listing in
    BOTH directions — ``a_half_cent_is_below_the_absolute_floor_on_a_cheap_product`` refuses
    0.005 there and ``a_price_exactly_at_the_absolute_floor_is_admitted`` admits 0.01 — so the
    floor on a 0.50 product is pinned to exactly one cent by two standing cases, and a repair
    that lowers it is not a repair, it is a second defect wearing this ticket's number.
    """
    from contracts import boundary  # noqa: PLC0415

    def refused(listed: float, priced: float) -> bool:
        bid = {
            "auction_id": "auc-1",
            "store_id": "store-1",
            "offer": {"product_ref": "prod-1", "unit_price": priced, "total_price": priced},
        }
        reasons = boundary.price_reasons(
            bid,
            list_prices={"prod-1": {"list_price": listed, "max_discount_pct": 100.0}},
        )
        below = [r for r in reasons if r.endswith(boundary.ROSTER_PRICE_BELOW_FLOOR)]
        assert below == reasons, (listed, priced, reasons)
        return bool(below)

    # THE DEFECT. A product listed at one cent, and a bid a cent's worth of authorized depth
    # below it. Nothing but the floor can refuse this: the roster authorizes 100%.
    assert not refused(0.01, 0.009), (
        "a product the roster lists at 0.01 cannot be bid on at all — every price under list is "
        f"under the floor, which is {boundary.price_floor(0.01)}"
    )
    # ...and one cent higher up, where the flat half was still half the product's price.
    assert not refused(0.02, 0.008), (
        "a bid declaring an authorized 60% off a 0.02 listing was refused by the floor alone; "
        f"the floor there is {boundary.price_floor(0.02)}"
    )

    # THE CONTROLS — every price the wall refused before this ticket, still refused. Without
    # these the assertions above are satisfied by deleting the floor.
    assert refused(100.0, 0.001), "T-223's measured attack: a tenth of a cent on a 100.00 product"
    assert refused(1_000_000.0, 999.0), "the proportional half, on a listing where it dominates"
    assert refused(5.0, 0.005), "T-271: half a cent on a five-dollar product"
    assert refused(0.5, 0.005), "the corpus's own half cent on a 0.50 listing"

    # ...and their positive controls, so the wall is not merely refusing everything.
    assert not refused(100.0, 1.0), "R10 pins an undeclared 1.00 on an uncapped 100.00 as ADMITTED"
    assert not refused(1_000_000.0, 1001.0), "T-271's upper pair"
    assert not refused(5.0, 0.02), "T-271's upper pair"
    assert not refused(0.5, 0.01), "the corpus admits exactly one cent on a 0.50 listing"
    assert not refused(0.005, 0.004), "the sub-cent listing keeps its dropped absolute half"

    # The 0.50 crossover, stated as the equality the corpus and `test_boundary_dual_path.py`
    # already pin, because it is the assertion any clamp on the absolute half must not move.
    assert boundary.price_floor(0.5) == boundary.MINIMUM_PAYABLE_AMOUNT
    assert boundary.price_floor(100.0) == 0.1
    assert boundary.price_floor(0.005) < boundary.MINIMUM_PAYABLE_AMOUNT

    # The floor never rises above the list price it guards — the invariant whose violation IS
    # this ticket, asserted across the whole cheap band rather than at the two points above.
    listed = 0.001
    while listed <= 1_000_000.0:
        assert boundary.price_floor(listed) < listed, (
            f"the floor on a product listed at {listed} is {boundary.price_floor(listed)}, at or "
            "above its own list price, so no bid under list can ever be admitted"
        )
        listed *= 1.05


def test_t276_the_typescript_door_carries_the_same_floor_arithmetic() -> None:
    """T-278's lesson, applied to this ticket: a floor fixed in one language is not fixed.

    ``price_floor`` and ``priceFloor`` are peers, and the T-250 threshold they both implement
    shipped to Python first and sat unguarded in TypeScript until the shared parity corpus was
    built. The corpus drives whole bids and so covers the VERDICTS; this asserts the narrower
    thing the corpus cannot see, which is that the two files agree on the constants the floor is
    made of. A repair applied to ``boundary.py`` alone leaves this red.
    """
    from contracts import boundary  # noqa: PLC0415

    source = (_CONTRACTS / "src" / "ts" / "boundary.ts").read_text(encoding="utf-8")

    for name, value in (
        ("MINIMUM_PAYABLE_AMOUNT", boundary.MINIMUM_PAYABLE_AMOUNT),
        ("PRICE_FLOOR_FRACTION", boundary.PRICE_FLOOR_FRACTION),
        ("ABSOLUTE_FLOOR_MAX_FRACTION", boundary.ABSOLUTE_FLOOR_MAX_FRACTION),
    ):
        declared = [
            line.strip()
            for line in source.splitlines()
            if line.startswith(f"export const {name} =")
        ]
        assert len(declared) == 1, f"boundary.ts does not declare {name} exactly once: {declared}"
        assert declared[0] == f"export const {name} = {value};", (
            f"the two doors disagree about {name}: python has {value!r}, boundary.ts has "
            f"{declared[0]!r}"
        )
