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
        "T-156: offer.total_price is reconciled against nothing at the store agent's own "
        "door. packages/store-agent/src/hooks/provenance.py never reads the field at all — the "
        "string appears once in the whole module, in a docstring at :895, because PRICE_FIELD "
        "is 'unit_price' — so an offer whose unit_price is the honest price for a genuinely "
        "granted depth is admitted with ANY total below it. Measured: 60 of 60 admitted; "
        "remove this marker with the fix"
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

    MEASURED at HEAD, over exactly the band this draws from: 60 of 60 cases ADMITTED by
    `enforce_bid_provenance`, and `price_reasons` returned `[]` for every one of them, with and
    without a roster. The ticket's own example (unit 80.00, total 1.00 behind a genuine 20%
    grant on a 100.00 list) is one point of it.

    **Why only the store agent's door is GRADED here, though both are measured.** The shared
    `contracts.boundary` door is silent on all 60 of these bids too, and its relation at
    :909-911 double-discounts an already-discounted unit price. But that door is PINNED by an
    existing test this gate has no standing to overrule:
    `packages/contracts/tests/test_boundary_dual_path.py::test_the_total_may_not_undercut_the_depth_the_offer_declares`
    requires `priced_offer(100.0, 15.0)` — unit 100, total 15, declared 20%, and NO list-price
    claim — to be refused naming `offer.total_price`, so the relation must work with no catalog
    at all, against `unit_price`; and in the same test requires `priced_offer(100.0, 80.0)` —
    unit 100, total 80 — to be ADMITTED, so `total >= unit` must NOT hold there. Together those
    pin that door to exactly `total >= unit * (100 - depth) / 100`. Its `unit_price` is the
    price BEFORE the discount; `_price_reconciliation_refusal`'s is the price AFTER it
    (`unit_price >= list * (100 - declared) / 100`). The shared `make_offer` fixture ships
    `unit_price 49.0 / total_price 44.1 / 10%`, which is the first reading.

    Two doors reading one field two ways is what the ticket means by "the relation is not
    decidable from a bid alone", and reconciling them is the design decision it says closing
    this requires. MEASURED, so it is a finding and not a guess — three ways to correct that
    door, and what each one costs:

    * `total >= unit` there: **56** tests fail. The shared `make_offer` fixture alone
      (`unit 49.0 / total 44.1 / 10%`) sits inside the band it would refuse.
    * the list-price form, `total >= list * (100 - depth) / 100`: **15** fail, and three of
      them are genuine verdict flips rather than stale expectations — both params of the
      no-catalog pinning test above stop refusing `priced_offer(100.0, 15.0)` at all (with no
      roster and no list-price claim `listed is None`, so the relation abstains and nothing
      else objects to a total of 15.00 under a stated unit of 100.00), and one corpus row goes
      from refused to admitted.
    * **additive** — keep the existing unit relation and ADD the list-price one: **12** fail,
      every one of them a stale exact-reason list on a bid that was already refused, and ZERO
      verdicts flip. The pinning test stays green. This is the cheapest correct version, and
      it still needs those 12 expectations updated.

    What binds all three is `packages/contracts/tests/price_parity_corpus.json`: 37 wire
    payloads asserting EXACT reason lists, loaded both by
    `test_the_price_verdicts_match_the_typescript_peer` and by
    `packages/contracts/tests/boundary.test.ts` — so a reason added on the Python side is red
    on the TypeScript side too.

    Fixing only the store agent's door — the location this ticket names — breaks NOTHING:
    1202 passed, 0 new failures, re-measured on two independent copies.

    So the second door's silence is printed in the failure message and graded nowhere. A gate
    that demanded an existing test be rewritten would be a gate no repair lane could close.
    """
    from contracts.boundary import OFFER_TOTAL_PRICE_SITE, price_reasons
    from store_agent.hooks import HookProvenanceError, enforce_bid_provenance

    cases = _t156_cases()
    assert len(cases) == T156_CASE_COUNT, (
        f"the sweep generated {len(cases)} cases, not {T156_CASE_COUNT}; see the arming test"
    )

    roster = _t156_roster()
    escapes: list[str] = []
    contracts_silent: list[str] = []
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
        # REPORTED, NOT ASSERTED — and the difference is the whole reason this gate is
        # closeable. See the docstring: the shared contracts door is pinned to the
        # double-discounting relation by an existing test that this gate has no standing to
        # overrule. The count is printed so the second door's silence stays visible in the
        # failure output instead of being quietly dropped.
        reasons = price_reasons(bid, list_prices=roster, max_discount_pct=T156_MAX_DISCOUNT_PCT)
        if not any(OFFER_TOTAL_PRICE_SITE in reason for reason in reasons):
            contracts_silent.append(label)

    assert not escapes, (
        f"{len(escapes)} of {len(cases)} bids stating a total_price below one "
        "already-discounted unit_price were ADMITTED by enforce_bid_provenance, behind a "
        "genuine grant, with every other wall satisfied:\n  "
        + "\n  ".join(escapes[:20])
        + (f"\n  ... and {len(escapes) - 20} more" if len(escapes) > 20 else "")
        + f"\n  [for information, not graded here: contracts.boundary.price_reasons was also "
        f"silent about {OFFER_TOTAL_PRICE_SITE} on {len(contracts_silent)} of {len(cases)} of "
        "the same bids]"
    )


# =============================================================================================
# T-279 — the total catch-all is the ONLY thing holding "never raises" up
#
# Function-local imports again: this file is append-only by lane contract and `E402` forbids a
# second module-level import block.
# =============================================================================================

#: Sixteen words with sixteen distinct first letters. The head word of every drawn id is chosen
#: by INDEX rather than by draw, so no two bases can share a literal prefix — the exact hole
#: that let a `store_id`-prefix sabotage go undetected in this package's sibling gate. The rest
#: of each id is drawn, so the ids are varied as well as distinct.
T279_WORDS = (
    "alpaca",
    "birch",
    "cinder",
    "dune",
    "ember",
    "fern",
    "glacier",
    "harbor",
    "indigo",
    "juniper",
    "kelp",
    "lumen",
    "meadow",
    "nimbus",
    "opal",
    "quartz",
)
T279_SEPARATORS = ("-", "_", ".", "")
T279_PINNED_SEED = 20260904
T279_BASE_COUNT = 12
T279_FRESHNESS_WINDOW_SECONDS = 300.0
#: 30 hazards x 2 distinct base bids each.
T279_CASE_COUNT = 60

#: The reason the outer wrapper returns when something escaped `_receive_bid`. It is a REAL
#: refusal — the door failing closed is correct — but it is also the door saying "I do not know
#: what went wrong", and it must not be the answer to an input the door has a name for.
T279_FAILED_CLOSED = "door_failed_closed"


def _t279_token(rng: Any, index: int, offset: int) -> str:
    head = T279_WORDS[(index + offset) % len(T279_WORDS)]
    sep = rng.choice(T279_SEPARATORS)
    tail = rng.choice(T279_WORDS)
    return f"{head}{sep}{tail}{sep}{rng.randrange(1000, 9999)}"


def _t279_sink(item: Any) -> None:  # a queue that takes everything and says nothing
    return None


def _t279_bases() -> list[dict[str, Any]]:
    """Twelve fully valid, correctly signed submissions this door ACCEPTS.

    Every hostile case below is one of these with exactly ONE caller-supplied argument
    corrupted, which is the only way the sweep can attribute a refusal to the corruption. That
    makes the acceptance of these twelve the load-bearing precondition of the whole gate, and
    the arming test asserts it rather than assuming it.

    This is the lesson of ``test_repro_external_door.py`` restated: its generator drew
    ``expires_at`` in 2027-2999 against ``now=2026``, so no draw ever resembled a realistic bid,
    two live sabotages went undetected and a 4,959-test differential came back an EMPTY DIFF.
    So the shapes here are drawn, not constant, and drawn NEAR the instant they are judged
    against: half the bases expire within a day of ``now``, by index rather than by luck.

    Half the bases are drawn from a pinned seed (reproducible) and half from ``SystemRandom``
    (different every run, so the sweep cannot be fitted to a constant table).
    """
    import random
    from datetime import UTC, datetime, timedelta

    from store_agent.external import sign_bid

    pinned = random.Random(T279_PINNED_SEED)
    system = random.SystemRandom()
    now = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)
    bases: list[dict[str, Any]] = []
    for index in range(T279_BASE_COUNT):
        rng: Any = pinned if index % 2 == 0 else system
        store_id = _t279_token(rng, index, 0)
        signer_id = _t279_token(rng, index, 5)
        key_id = _t279_token(rng, index, 9)
        secret = _t279_token(rng, index, 13)
        nonce = _t279_token(rng, index, 3)
        # Near-term for even indexes, far for odd — by index, so "half the draws look like a
        # real bid" is true by construction and not a coin flip the assertion has to tolerate.
        #
        # BUCKETED BY INDEX, and that is a bug fix rather than a flourish. Drawn flat, the
        # `issued_at` offset had 241 possible values and twelve draws collide about one run in
        # four — measured, and it made the distinctness assertion below fail ~1 run in 8. A
        # gate that is flaky about its own arming is worse than no arming at all, because the
        # red it produces is noise and gets ignored. Each index draws from its OWN disjoint
        # window, so distinctness is structural and the value is still drawn.
        bucket = index // 2
        if index % 2 == 0:
            expires_at = now + timedelta(minutes=30 + bucket * 230 + rng.randrange(0, 200))
        else:
            expires_at = now + timedelta(
                minutes=1500 + bucket * 400_000 + rng.randrange(0, 300_000)
            )
        issued_at = now + timedelta(seconds=-115 + index * 20 + rng.randrange(-4, 5))
        deadline = now + timedelta(seconds=rng.randrange(60, 3600))
        unit_price = rng.randrange(500, 50000) / 100.0
        payload = {
            "auction_id": _t279_token(rng, index, 7),
            "store_id": store_id,
            "offer": {
                "product_ref": _t279_token(rng, index, 11),
                "unit_price": unit_price,
                "total_price": round(unit_price * rng.randrange(1, 4), 2),
                "discount": None,
                "commitments": [],
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            },
            "claims": [],
            "message": f"{rng.choice(T279_WORDS)} {rng.choice(T279_WORDS)}",
            "agent_version": f"ext-{rng.randrange(1, 9)}.{rng.randrange(0, 9)}.0",
            "schema_version": "1",
            "signer_id": signer_id,
            "key_id": key_id,
            "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
            "nonce": nonce,
        }
        bases.append(
            {
                "index": index,
                "stream": "pinned" if index % 2 == 0 else "drawn",
                "payload": payload,
                "signature": sign_bid(payload, secret),
                "keyring": {signer_id: {key_id: secret}},
                "store_id": store_id,
                "signer_id": signer_id,
                "key_id": key_id,
                "nonce": nonce,
                "now": now.isoformat().replace("+00:00", "Z"),
                "auction_deadline": deadline.isoformat().replace("+00:00", "Z"),
                "expires_at": payload["offer"]["expires_at"],
                "issued_at": payload["issued_at"],
                "trust_snapshot": {
                    store_id: {"store_id": store_id, "score": 0.9, "blacklisted": False}
                },
            }
        )
    return bases


def _t279_invoke(entry: Any, base: dict[str, Any], overrides: dict[str, Any]) -> Any:
    """Call ``entry`` (``receive_bid`` or ``_receive_bid``) on ``base`` with one field changed.

    A fresh ``NonceStore`` per call, deliberately: an accepted submission consumes its nonce,
    and a shared store would make the second call on the same base a ``replayed_nonce`` refusal
    — a green-looking refusal that says nothing about the hazard under test.
    """
    from store_agent.external import NonceStore

    kwargs: dict[str, Any] = {
        "queue": _t279_sink,
        "nonce_store": NonceStore(),
        "now": base["now"],
        "auction_deadline": base["auction_deadline"],
        "blacklist": None,
        "freshness_window_seconds": T279_FRESHNESS_WINDOW_SECONDS,
        "trust_snapshot": base["trust_snapshot"],
        "list_prices": None,
        "max_discount_pct": None,
    }
    positional = ("payload", "signature", "keyring")
    kwargs.update({name: value for name, value in overrides.items() if name not in positional})
    return entry(
        overrides.get("payload", base["payload"]),
        overrides.get("signature", base["signature"]),
        overrides.get("keyring", base["keyring"]),
        **kwargs,
    )


def _t279_heads(reasons: Any) -> set[str]:
    """The reason TOKENS, with their ``:<detail>`` suffixes trimmed.

    ``trust_snapshot_unavailable`` alone is emitted in three different spellings — bare, with a
    plain ``:<store_id>``, and with a ``:{store_id!r}`` — and the door's own
    ``store_blacklisted`` carries no suffix where the boundary's does. Grading the whole string
    would make this gate fail on a spelling; grading the token is what makes it about the
    door's ANSWER.
    """
    return {str(reason).split(":", 1)[0] for reason in reasons}


def _t279_hazards() -> list[dict[str, Any]]:
    """Every hazard, with the READABLE input the door already has a name for beside it.

    The expected reason is never written down here. It is MEASURED at run time, from the benign
    equivalent, on the same base bid — "a store whose eligibility row cannot be read must be
    refused the way a store with no row is", "a keyring that cannot be searched must be refused
    the way an empty one is". That is what makes the assertion impossible to satisfy with an
    invented token: nine families expect eight DISTINCT reason tokens, and one generic catch-all
    can only ever return one of them. A catch-all that dispatched on the hazard to pick the
    right token would BE the by-name handling this ticket asks for.

    Six of the nine families are green at HEAD. They are not filler — they are the canary. If
    the harness below stopped being able to see a refusal at all, they would go red with it,
    so a red result on the other three is a measurement rather than a broken assertion.
    """
    from collections.abc import Mapping

    class _HostileMapping(Mapping):  # type: ignore[type-arg]
        """A real ``Mapping`` whose reads raise. The type gate passes; the read is the hazard."""

        def __init__(self, error: BaseException) -> None:
            self._error = error

        def get(self, key: Any, default: Any = None) -> Any:
            raise self._error

        def __getitem__(self, key: Any) -> Any:
            raise self._error

        def __iter__(self) -> Any:
            return iter(())

        def __len__(self) -> int:
            return 0

    class _ExplodingPayload(Mapping):  # type: ignore[type-arg]
        """A submission that answers ``after`` reads honestly and then starts raising."""

        def __init__(self, body: dict[str, Any], after: int, error: BaseException) -> None:
            self._body = body
            self._after = after
            self._error = error
            self.reads = 0

        def _tick(self) -> None:
            self.reads += 1
            if self.reads > self._after:
                raise self._error

        def get(self, key: Any, default: Any = None) -> Any:
            self._tick()
            return self._body.get(key, default)

        def __getitem__(self, key: Any) -> Any:
            self._tick()
            return self._body[key]

        def __iter__(self) -> Any:
            return iter(self._body)

        def __len__(self) -> int:
            return len(self._body)

    class _UnreadableBlacklist:
        def __iter__(self) -> Any:
            raise ValueError("the block list feed answered with half-decoded JSON")

    class _ExplodingId(str):
        """A str SUBCLASS, so ``isinstance(entry, str)`` passes and the comparison is reached.

        A plain object would be refused by ``_blacklisted``'s own "not a string" guard before
        any ``__eq__`` ran, and the hazard would never be exercised.
        """

        def __eq__(self, other: Any) -> bool:
            raise RuntimeError("this blocked id cannot be compared")

        def __hash__(self) -> int:
            return 0

    class _UnaskableQueue:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("this transport cannot be asked what it can do")

    class _RaisingQueue:
        def __call__(self, item: Any) -> None:
            raise RuntimeError("this transport took nothing")

    class _UnreadableDeadline:
        def __str__(self) -> str:
            raise RuntimeError("this deadline cannot be rendered")

        def __repr__(self) -> str:
            return "<unreadable deadline>"

    def snapshot_benign(base: dict[str, Any]) -> dict[str, Any]:
        return {"trust_snapshot": {}}

    def keyring_benign(base: dict[str, Any]) -> dict[str, Any]:
        return {"keyring": {}}

    def keyring_inner_benign(base: dict[str, Any]) -> dict[str, Any]:
        return {"keyring": {base["signer_id"]: {}}}

    def blacklist_benign(base: dict[str, Any]) -> dict[str, Any]:
        return {"blacklist": [base["store_id"]]}

    def window_benign(base: dict[str, Any]) -> dict[str, Any]:
        return {"freshness_window_seconds": -1.0}

    def deadline_benign(base: dict[str, Any]) -> dict[str, Any]:
        return {"auction_deadline": "not-an-instant"}

    def payload_benign(base: dict[str, Any]) -> dict[str, Any]:
        body = dict(base["payload"])
        body.pop("signer_id")
        return {"payload": body}

    def queue_benign(base: dict[str, Any]) -> dict[str, Any]:
        return {"queue": None}

    def prices_benign(base: dict[str, Any]) -> dict[str, Any]:
        return {"list_prices": {}}

    hazards: list[dict[str, Any]] = []

    for error in (
        ValueError("eligibility feed is half-decoded"),
        RuntimeError("eligibility store is not connected"),
        KeyError("eligibility partition"),
        OverflowError("eligibility index overflowed"),
        ArithmeticError("eligibility score is not a number"),
        ZeroDivisionError("eligibility ratio divided by zero"),
        LookupError("eligibility shard is missing"),
    ):
        hazards.append(
            {
                "family": "trust_snapshot",
                "label": f"trust_snapshot.get raises {type(error).__name__}",
                "make": lambda base, error=error: {"trust_snapshot": _HostileMapping(error)},
                "benign": snapshot_benign,
            }
        )

    for error in (
        RuntimeError("the key store is not connected"),
        ValueError("the key store answered with half-decoded JSON"),
        OverflowError("the key index overflowed"),
    ):
        hazards.append(
            {
                "family": "keyring",
                "label": f"keyring.get raises {type(error).__name__}",
                "make": lambda base, error=error: {"keyring": _HostileMapping(error)},
                "benign": keyring_benign,
            }
        )
        hazards.append(
            {
                "family": "keyring_inner",
                "label": f"keyring[signer].get raises {type(error).__name__}",
                "make": lambda base, error=error: {
                    "keyring": {base["signer_id"]: _HostileMapping(error)}
                },
                "benign": keyring_inner_benign,
            }
        )

    hazards.append(
        {
            "family": "blacklist",
            "label": "blacklist.__iter__ raises ValueError",
            "make": lambda base: {"blacklist": _UnreadableBlacklist()},
            "benign": blacklist_benign,
        }
    )
    hazards.append(
        {
            "family": "blacklist",
            "label": "a blocked id whose __eq__ raises RuntimeError",
            "make": lambda base: {"blacklist": [_ExplodingId(base["store_id"])]},
            "benign": blacklist_benign,
        }
    )

    for window, label in (
        (10**400, "freshness_window_seconds=10**400 (float() raises OverflowError)"),
        (float("nan"), "freshness_window_seconds=nan (loses every comparison)"),
        (float("inf"), "freshness_window_seconds=inf (says never stale)"),
        (None, "freshness_window_seconds=None"),
    ):
        hazards.append(
            {
                "family": "freshness_window",
                "label": label,
                "make": lambda base, window=window: {"freshness_window_seconds": window},
                "benign": window_benign,
            }
        )

    hazards.append(
        {
            "family": "auction_deadline",
            "label": "an auction_deadline whose __str__ raises",
            "make": lambda base: {"auction_deadline": _UnreadableDeadline()},
            "benign": deadline_benign,
        }
    )
    hazards.append(
        {
            "family": "auction_deadline",
            "label": "an auction_deadline that is a list",
            "make": lambda base: {"auction_deadline": ["2026-01-01T00:05:00Z"]},
            "benign": deadline_benign,
        }
    )

    for after in (0, 1, 3, 6, 10):
        hazards.append(
            {
                "family": "payload",
                "label": f"payload.get raises after {after} read(s)",
                "make": lambda base, after=after: {
                    "payload": _ExplodingPayload(
                        base["payload"], after, RuntimeError("the submission stopped answering")
                    )
                },
                "benign": payload_benign,
            }
        )

    hazards.append(
        {
            "family": "queue",
            "label": "a queue whose attribute access raises",
            "make": lambda base: {"queue": _UnaskableQueue()},
            "benign": queue_benign,
        }
    )
    hazards.append(
        {
            "family": "queue",
            "label": "a queue whose __call__ raises",
            "make": lambda base: {"queue": _RaisingQueue()},
            "benign": queue_benign,
        }
    )

    for error in (
        RuntimeError("the catalog is not connected"),
        ValueError("the catalog answered with half-decoded JSON"),
    ):
        hazards.append(
            {
                "family": "list_prices",
                "label": f"list_prices.get raises {type(error).__name__}",
                "make": lambda base, error=error: {"list_prices": _HostileMapping(error)},
                "benign": prices_benign,
            }
        )

    return hazards


def _t279_cases() -> list[dict[str, Any]]:
    """Every hazard against two DIFFERENT drawn bids, so no case rests on one lucky shape."""
    bases = _t279_bases()
    hazards = _t279_hazards()
    cases: list[dict[str, Any]] = []
    for position, hazard in enumerate(hazards):
        for repeat in range(2):
            base = bases[(2 * position + repeat) % len(bases)]
            cases.append({"hazard": hazard, "base": base})
    return cases


def test_t279_the_hostile_input_sweep_is_armed() -> None:
    """Sixty real cases, twelve bids the door really accepts, nine benign answers to compare
    against, and the wrapper split still in place. NOT xfail.

    Each block below closes one way the gate could report green while the erosion lived:

    1. **The sweep goes quiet.** Counted and de-duplicated before anything is concluded.
    2. **The base bids stop being accepted.** This is the failure this package has already
       shipped: a generator whose draws did not resemble a bid, so nothing it did could be
       detected. If a base were refused, every hostile variant of it would refuse too — for the
       base's reason, not the hazard's — and the comparison below would be measuring nothing.
    3. **The draws collapse onto a constant table.** Distinct ids, distinct instants, no shared
       literal prefix, and half the offers expiring within a day of the instant they are judged
       against.
    4. **The benign answers are empty.** The gate asserts "answer the unreadable input the way
       you answer the readable one". If a benign equivalent were ACCEPTED, or refused with no
       reasons, that comparison would be satisfiable by anything.
    5. **The wrapper split is gone.** The gate's below-the-wrapper probe is a direct call to
       ``_receive_bid``; if that name stopped being a separate function the probe would be
       grading the wrapper again.
    """
    from store_agent.external import receive_bid
    from store_agent.external.door import _receive_bid

    assert _receive_bid is not receive_bid, (
        "receive_bid and _receive_bid are the same object; the total wrapper and the gates it "
        "wraps can no longer be told apart, so nothing below can see under the catch-all"
    )

    cases = _t279_cases()
    assert len(cases) == T279_CASE_COUNT, (
        f"the sweep generated {len(cases)} cases, not {T279_CASE_COUNT} — it has shrunk"
    )
    keys = {(case["hazard"]["label"], case["base"]["index"]) for case in cases}
    assert len(keys) == T279_CASE_COUNT, (
        f"only {len(keys)} of {len(cases)} cases are DISTINCT (hazard, bid) pairs"
    )
    families: dict[str, int] = {}
    for case in cases:
        families[case["hazard"]["family"]] = families.get(case["hazard"]["family"], 0) + 1
    assert families == {
        "trust_snapshot": 14,
        "keyring": 6,
        "keyring_inner": 6,
        "blacklist": 4,
        "freshness_window": 8,
        "auction_deadline": 4,
        "payload": 10,
        "queue": 4,
        "list_prices": 4,
    }, f"the hazard families no longer have the counts this gate was measured against: {families}"

    # 2. The bases are real bids this door admits.
    bases = _t279_bases()
    assert len(bases) == T279_BASE_COUNT
    for base in bases:
        receipt = _t279_invoke(receive_bid, base, {})
        assert receipt.accepted is True, (
            f"base bid {base['index']} ({base['stream']} stream) was REFUSED "
            f"{receipt.reasons}; every hostile variant of it would then be refused for the "
            "base's reason and the sweep would be blind — this is exactly how this package's "
            "sibling generator drew 4,959 cases that detected nothing"
        )
        assert receipt.reasons == (), f"base bid {base['index']} carried reasons {receipt.reasons}"

    # 3. The draws are varied, not a constant table wearing different numbers.
    for field in ("store_id", "signer_id", "key_id", "nonce", "issued_at", "expires_at"):
        drawn = [base[field] for base in bases]
        assert len(set(drawn)) == T279_BASE_COUNT, (
            f"only {len(set(drawn))} of {T279_BASE_COUNT} drawn {field} values are distinct"
        )
    for field in ("store_id", "signer_id", "key_id", "nonce"):
        drawn = [base[field] for base in bases]
        shared = 0
        while all(len(value) > shared and value[shared] == drawn[0][shared] for value in drawn):
            shared += 1
        assert shared <= 1, (
            f"all {T279_BASE_COUNT} drawn {field} values share the literal prefix "
            f"{drawn[0][:shared]!r}; a sabotage keyed on that prefix would go undetected, which "
            "is what happened to this package's sibling gate"
        )
    near_term = sum(1 for base in bases if base["expires_at"] < "2026-01-02")
    assert near_term >= T279_BASE_COUNT // 2, (
        f"only {near_term} of {T279_BASE_COUNT} offers expire within a day of the instant they "
        "are judged against; the sibling generator drew 2027-2999 against now=2026 and no draw "
        "ever resembled a realistic bid"
    )

    # 4. Every family has a readable equivalent the door already refuses BY NAME.
    seen_tokens: dict[str, set[str]] = {}
    for hazard in _t279_hazards():
        base = bases[0]
        benign = _t279_invoke(receive_bid, base, hazard["benign"](base))
        assert benign.accepted is not True, (
            f"{hazard['family']}: the readable equivalent was ADMITTED "
            f"({hazard['benign'](base)}); there is nothing for the hostile case to be compared "
            "against"
        )
        assert benign.reasons, f"{hazard['family']}: the readable equivalent carried no reasons"
        heads = _t279_heads(benign.reasons)
        assert T279_FAILED_CLOSED not in heads, (
            f"{hazard['family']}: even the READABLE input now answers {T279_FAILED_CLOSED}; the "
            "gate can no longer distinguish a named refusal from an internal fault"
        )
        seen_tokens[hazard["family"]] = heads
    distinct = {frozenset(heads) for heads in seen_tokens.values()}
    assert len(distinct) >= 8, (
        f"the nine hazard families expect only {len(distinct)} distinct reason token sets "
        f"({seen_tokens}); the gate's whole defence against an invented catch-all token is that "
        "one token cannot answer for all of them"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-279: the total `except Exception` at door.py:404 is the only thing holding "
        "`receive_bid` never-raises up for two whole hazard families. A trust_snapshot whose "
        "`.get` raises anything but TypeError escapes contracts/boundary.py:958-964, and a "
        "keyring whose `.get` does the same escapes contracts/signing.py:399-406; both come "
        "back `door_failed_closed` where the readable equivalent is answered "
        "`trust_snapshot_unavailable:<store_id>` and `unknown_signing_key`. Every by-name gate "
        "inside _receive_bid could regress and the suite would stay green; remove this marker "
        "with the fix"
    ),
)
def test_t279_every_hostile_input_is_refused_by_name_not_by_the_catch_all() -> None:
    """The door must answer an input it cannot READ the way it answers one it can.

    The wrapper at ``door.py:386-407`` is right and this gate does not ask for its removal: an
    anonymous ``POST`` must never be handed a 500, and an error nobody anticipated must be a
    refusal. What it asks is that the wrapper be the belt and not the braces. Today it is the
    only thing holding the property up for two families, which is the erosion the ticket names:
    the grading test at ``test_repro_external_door.py:494`` asserts only ``accepted is not
    True``, so reverting any by-name guard inside ``_receive_bid`` leaves it green — measured,
    by reverting ``_blacklisted`` and ``_freshness_window`` to their pre-fix forms.

    Two assertions per case, and neither can be satisfied by moving the catch-all:

    * **The refusal carries the hazard's OWN reason token**, measured from the readable
      equivalent on the same bid rather than written down here. Nine families, eight distinct
      tokens — ``trust_snapshot_unavailable``, ``unknown_signing_key``, ``store_blacklisted``,
      ``freshness_window_invalid``, ``auction_deadline_unparseable``,
      ``signing_envelope_uncanonicalizable``, ``verification_queue_unavailable``,
      ``price_unreconcilable``. A generic handler one frame down returns ONE token and fails
      eight of the nine; a handler that returned the right token for each hazard would be the
      by-name handling this ticket asks for. This is deliberately a POSITIVE check: "not
      ``door_failed_closed``" would be satisfied by any freshly invented string, which is how
      the first draft of this gate could have certified a rename of the catch-all.
    * **``_receive_bid`` itself does not raise**, and answers the same token. That is the
      structural half: the wrapper adds nothing, because there is nothing left for it to catch.

    Six of the nine families pass today. They are the canary, not padding — they prove the
    comparison can see a refusal, so the three that fail are a measurement.

    MEASURED at HEAD: 60/60 hostile calls refused by the outer door (the existing assertion is
    satisfied throughout), while ``_receive_bid`` RAISES for every trust_snapshot and keyring
    case and the receipt says ``door_failed_closed``.
    """
    from store_agent.external import receive_bid
    from store_agent.external.door import _receive_bid

    cases = _t279_cases()
    assert len(cases) == T279_CASE_COUNT, (
        f"the sweep generated {len(cases)} cases, not {T279_CASE_COUNT}; see the arming test"
    )

    escapes: list[str] = []
    for case in cases:
        hazard, base = case["hazard"], case["base"]
        label = f"bid {base['index']} + {hazard['label']}"
        overrides = hazard["make"](base)
        expected = _t279_heads(_t279_invoke(receive_bid, base, hazard["benign"](base)).reasons)

        try:
            receipt = _t279_invoke(receive_bid, base, overrides)
        except Exception as exc:
            escapes.append(f"{label}: receive_bid RAISED {type(exc).__name__}: {exc}")
            continue
        if receipt.accepted is True:
            escapes.append(f"{label}: ADMITTED")
            continue
        actual = _t279_heads(receipt.reasons)
        if actual != expected:
            escapes.append(
                f"{label}: refused {sorted(actual)}, but the readable equivalent is refused "
                f"{sorted(expected)}"
            )

        try:
            below = _t279_invoke(_receive_bid, base, overrides)
        except Exception as exc:
            escapes.append(
                f"{label}: _receive_bid RAISED {type(exc).__name__}: {exc} — only the outer "
                "catch-all is holding the never-raises property up here"
            )
            continue
        under = _t279_heads(below.reasons)
        if under != expected:
            escapes.append(
                f"{label}: below the wrapper the refusal is {sorted(under)}, not {sorted(expected)}"
            )

    assert not escapes, (
        f"{len(escapes)} hazard(s) are answered by the total wrapper rather than by name:\n  "
        + "\n  ".join(escapes[:24])
        + (f"\n  ... and {len(escapes) - 24} more" if len(escapes) > 24 else "")
    )
