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


# =============================================================================================
# T-156 — `offer.total_price` is reconciled against nothing, on either door
#
# Every import below is function-local. This file is APPEND-ONLY by lane contract, so the
# header's import block is not mine to extend, and `E402` forbids a second module-level block
# further down. The T-309 gate above already reads this way ("measured at call time").
# =============================================================================================

#: The approved store envelope the whole T-156 sweep is drawn from. Read once and cached: the
#: honest-control loop below builds 180 bids, and re-parsing the fixture for each of them made
#: the arming test slower than the gate it arms.
T156_ENVELOPE = REPO_ROOT / "fixtures/envelopes/store-alpha.approved.json"

_T156_FIXTURE_CACHE: dict[str, Any] = {}

#: The cluster whose depth buckets the approved envelope is priced against, exactly as
#: ``packages/store-agent/tests/test_price_reconciliation.py`` builds its context.
T156_CLUSTER = "cluster-warm-layers"
T156_DEPTH_BUCKETS = [0.0, 0.05, 0.1, 0.15, 0.2]

#: MEASURED, per product, on this fixture: the declared depths `ToolHooks.authorize_discount`
#: actually GRANTS. `prod-cap` floors at 10.00 and clears every bucket up to the store-wide
#: 20% cap (25.0 is denied); `prod-floor` floors at 95.00, so 7.5% and deeper price under its
#: own floor and are denied. Hard-coding the measurement rather than probing for it is
#: deliberate — a probe that silently skipped a denied grant is precisely the quiet-sweep
#: failure this repo has already shipped three times (6->0 of 8, 70->0 of 79, 48->0 of 66).
#: The arming test asserts every one of these grants is still real, so a fixture that changes
#: under the sweep turns it RED instead of shrinking it.
T156_GRANTABLE_DEPTHS: dict[str, tuple[float, ...]] = {
    "prod-cap": (1.0, 2.5, 5.0, 7.5, 10.0, 12.5, 15.0, 17.5, 20.0),
    "prod-floor": (1.0, 2.5, 5.0),
}

#: The store-wide ceiling on `envelope.max_discount_pct`, forwarded to the contracts door so
#: its authorized depth matches the one the hook granted.
T156_MAX_DISCOUNT_PCT = 20.0

#: `contracts.boundary.PRICE_RECONCILIATION_TOLERANCE`, restated so the generator's draw space
#: is a FIXED interval rather than one the code under test gets to move. The arming test
#: asserts the two are still equal, so widening the tolerance is loud instead of silently
#: reshaping the band this gate draws from.
T156_TOLERANCE = 0.01

T156_PINNED_SEED = 20260904
T156_DRAWS_PER_SHAPE = 5
#: 9 grantable depths on `prod-cap` + 3 on `prod-floor` = 12 shapes, 5 totals each.
T156_CASE_COUNT = 60


def _t156_fixture() -> dict[str, Any]:
    if not _T156_FIXTURE_CACHE:
        _T156_FIXTURE_CACHE.update(json.loads(T156_ENVELOPE.read_text(encoding="utf-8")))
    return _T156_FIXTURE_CACHE


def _t156_roster() -> dict[str, float]:
    """The caller's own catalog, in the shape the contracts price wall reads."""
    return {ref: float(row["list_price"]) for ref, row in _t156_fixture()["catalog"].items()}


def _t156_hooks() -> Any:
    """A fresh `ToolHooks` over the approved envelope.

    Fresh per bid, never shared: `enforce_bid_provenance` SPENDS the authorization it admits,
    so a reused hooks object would refuse the second bid for a reason that has nothing to do
    with this ticket.
    """
    from store_agent.hooks import ToolHooks

    fixture = _t156_fixture()
    envelope = fixture["envelope"]
    return ToolHooks(
        {
            "store_id": envelope["store_id"],
            "envelope": envelope,
            "catalog": fixture["catalog"],
            "live_state": fixture.get("live_state", {}),
            "learned_policy": None,
            "network_priors": {T156_CLUSTER: {"depth_buckets": T156_DEPTH_BUCKETS}},
        }
    )


def _t156_bid(hooks: Any, product: str, depth: float, unit: float, total: float) -> Any:
    """One bid whose ONLY dishonest number is `total_price`.

    The grant is real (`hooks.authorize_discount`), the declared depth matches it, and
    `unit_price` is exactly the honest price for that depth — so the discount wall, the floor
    wall, the cap and the unit-price reconciliation are all satisfied and cannot stand in for
    the wall this gate is looking for.
    """
    from contracts import Bid, Discount, Offer
    from store_agent.hooks import Denied

    grant = hooks.authorize_discount(product, depth)
    assert not isinstance(grant, Denied), (
        f"the fixture no longer grants {depth}% on {product!r} ({grant!r}); the sweep would be "
        "measuring denied grants rather than dishonest totals — see the arming test"
    )
    return Bid(
        auction_id="auction-t156",
        store_id=_t156_fixture()["envelope"]["store_id"],
        offer=Offer(
            product_ref=product,
            unit_price=unit,
            total_price=total,
            currency="USD",
            discount=Discount(type="percentage", value=depth),
            commitments=[],
        ),
        claims=[grant],
        agent_version="store-agent/t156-gate",
        schema_version="1.0.0",
    )


def _t156_draw(lo_cents: int, hi_cents: int, pinned: Any, system: Any, want: int) -> list[int]:
    """`want` DISTINCT totals, in whole cents, from two streams.

    A pinned seed alone is a parametrized probe in a costume: it draws the same `want` numbers
    forever, so it can be satisfied by a fix that happens to cover those numbers. Half of every
    draw therefore comes from `SystemRandom`, which is different on every run and cannot be
    fitted. The pinned half is what makes a failure reproducible.

    Sampling without replacement rather than re-drawing on a collision: the narrowest band this
    is asked for is 98 cents wide, where five independent uniform draws collide about one run
    in ten, and a gate that is flaky about its own case count cannot arm itself. The top-up
    walks the band in order, so the count is exact by construction.
    """
    picked: list[int] = []
    seen: set[int] = set()
    span = range(lo_cents, hi_cents + 1)
    for rng, take in ((pinned, want - want // 2), (system, want // 2)):
        for cent in rng.sample(span, take):
            if cent not in seen:
                seen.add(cent)
                picked.append(cent)
    for cent in span:
        if len(picked) >= want:
            break
        if cent not in seen:
            seen.add(cent)
            picked.append(cent)
    return picked


def _t156_cases() -> list[dict[str, Any]]:
    """The sweep. Each case is an honest bid with one dishonest number in it.

    The drawn `total_price` is narrowed on FOUR sides, and every one of them closes a specific
    way this gate could be greened while the defect lived:

    * ``total < unit_price`` — strictly below the price of ONE already-discounted unit. Every
      quantity is at least one, so this is unreachable under every quantity semantics. That is
      what makes the gate quantity-safe: it does not pre-decide the ticket's open design
      question about where quantity enters the bid, it only refuses the half-line no quantity
      can reach.
    * ``total <= unit_price - 2 * TOLERANCE`` — below it by more than the wall's own tolerance,
      so a correct fix written with the repo's usual ``+ TOLERANCE <`` slack still refuses it.
    * ``total >= unit_price * (100 - depth) / 100`` — AT OR ABOVE the relation
      ``contracts.boundary`` already enforces at :909-911. That relation double-discounts (it
      re-applies the depth to an already-discounted unit price), and lifting it verbatim into
      `provenance.py` is the cheapest way to make a naive version of this gate green. Every
      case here survives that lift: at depth 20 on a 100.00 list the existing wall fires below
      64.00 and this band starts AT 64.00.
    * ``total > price_floor(list_price)`` — above the roster's own floor, which is
      ``list * 0.001`` = 0.10 here. The floor wall reports
      ``price_unreconcilable:offer.total_price:roster_price_below_floor``, which NAMES
      ``offer.total_price`` and would satisfy the assertion below for a reason that has nothing
      to do with the missing unit/total relation.

    MEASURED at HEAD over exactly this band: 24 of 24 spot cases ADMITTED by
    `enforce_bid_provenance` and `[]` from `price_reasons`, with and without a roster.
    """
    import math
    import random

    catalog = _t156_fixture()["catalog"]
    pinned = random.Random(T156_PINNED_SEED)
    system = random.SystemRandom()
    cases: list[dict[str, Any]] = []
    for product in sorted(T156_GRANTABLE_DEPTHS):
        listed = float(catalog[product]["list_price"])
        for depth in T156_GRANTABLE_DEPTHS[product]:
            unit = round(listed * (100.0 - depth) / 100.0, 2)
            existing_wall = unit * (100.0 - depth) / 100.0
            lo_cents = math.ceil(existing_wall * 100.0 - 1e-6)
            hi_cents = math.floor((unit - 2.0 * T156_TOLERANCE) * 100.0 + 1e-6)
            for cent in _t156_draw(lo_cents, hi_cents, pinned, system, T156_DRAWS_PER_SHAPE):
                cases.append(
                    {
                        "product": product,
                        "list_price": listed,
                        "depth": depth,
                        "unit": unit,
                        "total": cent / 100.0,
                        "existing_wall": existing_wall,
                        "band_cents": (lo_cents, hi_cents),
                    }
                )
    return cases


def _t156_label(case: dict[str, Any]) -> str:
    return (
        f"{case['product']} list={case['list_price']} depth={case['depth']}% "
        f"unit={case['unit']} total={case['total']}"
    )


def test_t156_the_dishonest_total_sweep_is_armed() -> None:
    """The sweep below draws 60 real cases, and an honest bid still gets through. NOT xfail.

    Five ways the T-156 gate could report green while the defect lived, each closed by an
    assertion here rather than by inspection:

    1. **The sweep goes quiet.** A loop that iterates zero cases passes. Three sweeps in this
       repo were found doing exactly that (6->0 of 8, 70->0 of 79, 48->0 of 66), and one of
       them shrank because unpriced products were silently skipped. The count and the
       distinctness are asserted before anything else.
    2. **The generator drifts out of its own band.** The four narrowing constraints in
       :func:`_t156_cases` are what make the gate quantity-safe and cheap-fix-proof; they are
       re-asserted per case here so a change to the draw space is loud.
    3. **The fixture stops granting.** If `authorize_discount` began denying these depths, every
       bid would be refused for a reason that has nothing to do with `total_price` and the gate
       would go green on a denial. Every drawn grant is checked to be real.
    4. **The wall refuses everything.** A fix that simply refused every priced bid would satisfy
       a gate that only ever looks at dishonest ones. The honest control below runs the SAME 60
       shapes at `total_price = unit_price * k` for k in 1, 2 and 3 — quantity one, two and
       three — and requires all 180 to be ADMITTED by both doors. This is also what keeps the
       gate from pre-deciding the ticket's open design question: it never asserts an upper
       bound on a total.
    5. **The refusal machinery is dead.** If `enforce_bid_provenance` had stopped raising, or
       `price_reasons` had stopped reporting, the gate's `except` clause would never run and
       its assertion would be vacuous. The last block drives a bid whose UNIT price is
       dishonest and requires both doors to refuse it today, naming `unit_price`.
    """
    from contracts.boundary import (
        OFFER_UNIT_PRICE_SITE,
        PRICE_RECONCILIATION_TOLERANCE,
        price_floor,
        price_reasons,
    )
    from store_agent.hooks import Denied, HookProvenanceError, enforce_bid_provenance

    assert T156_TOLERANCE == PRICE_RECONCILIATION_TOLERANCE, (
        f"the price wall's tolerance moved ({PRICE_RECONCILIATION_TOLERANCE}); the band this "
        f"gate draws from is written against {T156_TOLERANCE} and is no longer the band it "
        "documents"
    )
    catalog = _t156_fixture()["catalog"]
    assert set(catalog) == set(T156_GRANTABLE_DEPTHS), (
        f"the approved envelope's catalog is now {sorted(catalog)}, not "
        f"{sorted(T156_GRANTABLE_DEPTHS)}; the measured grantable depths no longer describe it"
    )

    cases = _t156_cases()
    assert len(cases) == T156_CASE_COUNT, (
        f"the sweep generated {len(cases)} cases, not {T156_CASE_COUNT} — it has shrunk, and a "
        "shrunken sweep proves nothing"
    )
    keys = {(case["product"], case["depth"], case["total"]) for case in cases}
    assert len(keys) == T156_CASE_COUNT, (
        f"only {len(keys)} of {len(cases)} generated cases are DISTINCT; the sweep is counting "
        "the same bid several times"
    )
    shapes = {(case["product"], case["depth"]) for case in cases}
    assert len(shapes) == sum(len(d) for d in T156_GRANTABLE_DEPTHS.values())

    for case in cases:
        unit, total = case["unit"], case["total"]
        label = _t156_label(case)
        lo_cents, hi_cents = case["band_cents"]
        assert hi_cents - lo_cents + 1 >= T156_DRAWS_PER_SHAPE, (
            f"{label}: the band is only {hi_cents - lo_cents + 1} cents wide, too narrow to "
            f"draw {T156_DRAWS_PER_SHAPE} distinct totals from"
        )
        assert total < unit, f"{label}: the drawn total is not below one unit; it is honest"
        assert total <= unit - 2.0 * T156_TOLERANCE + 1e-9, (
            f"{label}: the drawn total is within the wall's own tolerance of one unit, so a "
            "correct fix written with the repo's usual slack would not refuse it"
        )
        assert total + 1e-9 >= case["existing_wall"], (
            f"{label}: the drawn total is BELOW the relation contracts.boundary already "
            f"enforces ({case['existing_wall']}), so lifting that relation verbatim into "
            "provenance.py would green this case while the defect lived"
        )
        assert total > price_floor(case["list_price"]), (
            f"{label}: the drawn total is under the roster price floor "
            f"({price_floor(case['list_price'])}), where the floor wall already reports a "
            "reason naming offer.total_price for a reason unrelated to this ticket"
        )

    # 3 + 4. Real grants, and an honest bid still gets in — at three different quantities.
    for case in cases:
        label = _t156_label(case)
        for quantity in (1, 2, 3):
            hooks = _t156_hooks()
            grant = hooks.authorize_discount(case["product"], case["depth"])
            assert not isinstance(grant, Denied), (
                f"{label}: the fixture no longer grants this depth ({grant!r}); every case in "
                "the sweep would be refused on the grant rather than on the total"
            )
            hooks = _t156_hooks()
            honest = _t156_bid(
                hooks,
                case["product"],
                case["depth"],
                case["unit"],
                round(case["unit"] * quantity, 2),
            )
            enforce_bid_provenance(honest, hooks)  # must not raise
            reasons = price_reasons(
                honest, list_prices=_t156_roster(), max_discount_pct=T156_MAX_DISCOUNT_PCT
            )
            assert reasons == [], (
                f"{label}: an HONEST bid at quantity {quantity} "
                f"(total={round(case['unit'] * quantity, 2)}) was refused {reasons}; a wall that "
                "refuses honest bids would satisfy the gate below without reconciling anything"
            )

    # 5. The refusal machinery is live: a dishonest UNIT price is refused TODAY, by both doors.
    hooks = _t156_hooks()
    dishonest_unit = _t156_bid(hooks, "prod-cap", 20.0, 75.0, 150.0)
    with pytest.raises(HookProvenanceError) as raised:
        enforce_bid_provenance(dishonest_unit, hooks)
    text = "; ".join(reason for _, reason in raised.value.offenders)
    assert "unit_price" in text, (
        "enforce_bid_provenance refused a 25%-off price behind a 20% grant without naming "
        f"unit_price ({text}); the gate below reads offender text the same way, so it can no "
        "longer tell a real refusal from a silent one"
    )
    control = price_reasons(
        dishonest_unit, list_prices=_t156_roster(), max_discount_pct=T156_MAX_DISCOUNT_PCT
    )
    assert any(OFFER_UNIT_PRICE_SITE in reason for reason in control), (
        f"contracts.boundary.price_reasons reported {control} for a price 25% under list behind "
        "a 20% grant; the gate below reads this list the same way and would be vacuous"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-156: offer.total_price is reconciled against nothing. "
        "packages/store-agent/src/hooks/provenance.py never reads the field at all (the string "
        "appears once in the whole module, in a docstring at :895; PRICE_FIELD is 'unit_price'), "
        "and contracts.boundary's relation at :909-911 double-discounts an already-discounted "
        "unit price, so a total between unit*(100-depth)/100 and unit is admitted by both doors "
        "behind a genuine grant; remove this marker with the fix"
    ),
)
def test_t156_a_total_price_below_one_unit_price_is_refused_by_both_doors() -> None:
    """An offer cannot cost less in total than one of the units it is pricing.

    The bid this sweeps is honest in every other respect: the product is on the approved
    envelope's catalog, the declared depth is backed by a real `hooks.authorize_discount` grant,
    the depth is inside the store-wide cap, and `unit_price` is exactly the honest price for
    that depth — so the floor wall, the cap, the grant ledger and the unit-price reconciliation
    are all satisfied. The only dishonest number is `total_price`, and both doors admit it.

    The relation asserted is the ONLY one decidable from a bid alone, and it is deliberately
    one-sided. Quantity is not on the protocol object — the ticket's own open design question
    is where it should enter — so nothing here says what a total OUGHT to be. It says a total
    strictly under the price of one already-discounted unit is unreachable under every quantity
    semantics, because every quantity is at least one. A fix that reconciles a total against a
    quantity the bid learns to carry passes this gate unchanged; so does one that simply
    refuses the impossible half-line. What is refused is the field going unread.

    It matters downstream and not just at the wall: `apps/exchange/src/ranking/__init__.py:115`
    reads `("total_price", "unit_price", "price")` in that order — `total_price` FIRST — and
    DESIGN.md:127 publishes `price_value = clamp((list_price - total_price)/list_price, 0, 1)`.
    So the number no wall checks is the number the published rank formula reads.

    MEASURED at HEAD, over exactly the band this draws from: 24 of 24 spot cases ADMITTED by
    `enforce_bid_provenance`, and `price_reasons` returned `[]` for every one of them, with and
    without a roster. The ticket's own example (unit 80.00, total 1.00 behind a genuine 20%
    grant on a 100.00 list) is one point of it.
    """
    from contracts.boundary import OFFER_TOTAL_PRICE_SITE, price_reasons
    from store_agent.hooks import HookProvenanceError, enforce_bid_provenance

    cases = _t156_cases()
    assert len(cases) == T156_CASE_COUNT, (
        f"the sweep generated {len(cases)} cases, not {T156_CASE_COUNT}; see the arming test"
    )

    roster = _t156_roster()
    escapes: list[str] = []
    for case in cases:
        label = _t156_label(case)
        hooks = _t156_hooks()
        bid = _t156_bid(hooks, case["product"], case["depth"], case["unit"], case["total"])
        try:
            enforce_bid_provenance(bid, hooks)
        except HookProvenanceError as exc:
            text = "; ".join(reason for _, reason in exc.offenders)
            if "total_price" not in text:
                escapes.append(f"{label}: provenance refused, but not about total_price — {text}")
        else:
            escapes.append(f"{label}: enforce_bid_provenance ADMITTED it")
        reasons = price_reasons(bid, list_prices=roster, max_discount_pct=T156_MAX_DISCOUNT_PCT)
        if not any(OFFER_TOTAL_PRICE_SITE in reason for reason in reasons):
            escapes.append(
                f"{label}: contracts.boundary.price_reasons reported {reasons or '[]'}, which "
                f"names nothing about {OFFER_TOTAL_PRICE_SITE}"
            )

    assert not escapes, (
        f"{len(escapes)} of {2 * len(cases)} door verdicts admitted a total_price below one "
        "already-discounted unit_price, behind a genuine grant, with every other wall "
        "satisfied:\n  "
        + "\n  ".join(escapes[:20])
        + (f"\n  ... and {len(escapes) - 20} more" if len(escapes) > 20 else "")
    )
