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


# xfail marker removed with the T-309 fix: `packages/store-agent/src/solicitation/routes.py`
# now exists, `create_app()` mounts it (`app.state.mounted_routers ==
# ['store_agent.solicitation.routes']`), and the served set is exactly the published one —
# `{('POST', '/v1/bid-requests')}`. The marker was `strict=True`, so leaving it in place would
# turn the repair into an XPASS failure; the assertion below is unchanged and keeps grading the
# property as either side grows.
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


# ---------------------------------------------------------------------------------------------
# TEST EDIT, JUSTIFIED — the `xfail(strict=True)` marker that stood here was REMOVED. No
# assertion below was touched, and no case was dropped from the sweep: it still generates and
# grades all 60.
#
# MARKER REMOVED, verbatim: `@pytest.mark.xfail(strict=True, reason="T-156: offer.total_price is
# reconciled against nothing at the store agent's own door. packages/store-agent/src/hooks/
# provenance.py never reads the field at all — the string appears once in the whole module, in a
# docstring at :895, because PRICE_FIELD is 'unit_price' — so an offer whose unit_price is the
# honest price for a genuinely granted depth is admitted with ANY total below it. Measured: 60 of
# 60 admitted; remove this marker with the fix")`
#
# WHAT IT CLAIMED: that the assertion below MUST fail — 60 of 60 bids stating a total under one
# already-discounted unit were admitted. Under `strict=True` the marker is itself an assertion,
# so leaving it in place once the defect is fixed turns the repair into an XPASS *failure* that
# reds `make verify`.
#
# REQUIREMENT IT ENCODES: T-156, tickets.json. This node is the ticket's recorded `verify`.
#
# REVERT CHECK — would the assertion below still pass if I reverted my change? **NO**. MEASURED in
# this lane, three runs, ONE file swapped (packages/store-agent/src/hooks/provenance.py) and
# nothing else:
#   * `git show HEAD:...provenance.py` in place, `--runxfail`: 1 failed — "60 of 60 bids stating
#     a total_price below one already-discounted unit_price were ADMITTED".
#   * the same tree, plain run: 1 xfailed, with this marker's reason printed.
#   * my `_total_price_refusal` + `ClaimMaterial.totals` restored, `--runxfail`: 2 passed.
# So the XPASS is caused by THIS lane's fix and not by unrelated drift — the failure mode a
# sibling lane hit, where three of five XPASSing markers had nothing to do with the lane's own
# change and removing them would have false-closed three open tickets.
#
# WHAT IS *NOT* CLOSED, and is deliberately left red elsewhere in this file: the companion
# finding T-175 — that neither the floor wall nor the depth reconciliation READS `total_price`,
# so a node stating a total with no unit price beside it is still collected by nothing. The fix
# below reconciles a total against the unit standing next to it and claims no more than that.
# See `test_t175_...` below, which is xfail(strict=True) and RED against this same tree.
#
# VERDICT: the marker, not the code, was the thing that had become false. Removed.
# ---------------------------------------------------------------------------------------------
def test_t156_a_total_price_below_one_unit_price_is_refused_at_the_store_agents_own_door() -> None:
    """An offer cannot cost less in total than one of the units it is pricing.

    **The name grades what it says.** This node used to be called
    ``..._is_refused_by_both_doors`` while only one door was asserted — the shared
    ``contracts.boundary`` half moved from ``escapes`` to ``contracts_silent`` (reported, not
    graded) for the reason argued at length below, and the name did not move with it. A
    reviewer selecting this node by name would have believed both doors were being graded.
    The prefix ``test_t156_`` is load-bearing and must survive any further rename: the
    ticket's recorded verify selects with ``-k t156``, and a name past that selector silently
    unhooks the gate.

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
    `packages/contracts/tests/boundary.test.ts`, whose assertion at :962 is
    `expect(priced).toEqual(expected.reasons)` — deep, order-sensitive, exact-length. So a
    reason added on the Python side is red on the TypeScript side too, and MEASURED: the
    additive variant applied to `boundary.ts` alone turns 11 corpus rows red plus the
    standalone `priceReasons` test at boundary.test.ts:1065, twelve on each side, symmetric.
    `tsc -b` and `eslint` stay clean. The coupling only bites on behaviour the corpus reaches
    — 26 of the 37 rows stayed green — so it is a tripwire, not a proof of equivalence.

    One implementation trap, measured rather than guessed: a bid can satisfy BOTH relations at
    once (`a_zero_total_behind_a_list_price_unit` — unit 100.00, total 0.00, roster cap 20), so
    the additive version must not push the reason twice. Exact-list equality makes a duplicated
    reason a failure exactly like a missing one.

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


# ---------------------------------------------------------------------------------------------
# TEST EDIT, JUSTIFIED — the `xfail(strict=True)` marker that stood here was REMOVED. No
# assertion below was touched; the marker's own `reason` said "remove this marker with the fix",
# and this is that removal.
#
# MARKER REMOVED, verbatim: `@pytest.mark.xfail(strict=True, reason="T-279: the total `except
# Exception` at door.py:404 is the only thing holding `receive_bid` never-raises up for two whole
# hazard families. A trust_snapshot whose `.get` raises anything but TypeError escapes
# contracts/boundary.py:958-964, and a keyring whose `.get` does the same escapes
# contracts/signing.py:399-406; both come back `door_failed_closed` where the readable equivalent
# is answered `trust_snapshot_unavailable:<store_id>` and `unknown_signing_key`. Every by-name
# gate inside _receive_bid could regress and the suite would stay green; remove this marker with
# the fix")`
#
# WHAT IT CLAIMED: that the assertion below MUST fail — 52 of the sweep's checks were answered by
# the outer catch-all rather than by name. Under `strict=True` the marker is itself an assertion,
# and leaving it in place once the defect is fixed turns the repair into an XPASS *failure* that
# reds `make verify`.
#
# REQUIREMENT IT ENCODES: T-279, tickets.json — "the three inner fixes need assertions that see
# BELOW the wrapper". The gate is the ticket's recorded `verify`.
#
# REVERT CHECK — would the assertion below still pass if I reverted my change? **NO**, and that is
# the whole proof. MEASURED in this lane, three runs, one file swapped and nothing else:
#   * `git show HEAD:packages/store-agent/src/external/door.py` in place, `--runxfail`:
#     1 failed — "52 hazard(s) are answered by the total wrapper rather than by name".
#   * the same tree, plain run: 1 xfailed, with this marker's reason printed.
#   * my `_readable_eligibility` + guarded `keyring_secret` restored, `--runxfail`: 2 passed.
# So the XPASS is caused by THIS lane's fix to `door.py` and not by an unrelated drift — the
# failure mode a sibling lane hit an hour ago, where three of five XPASSing markers had nothing to
# do with the lane's own change and removing them would have false-closed three open tickets.
#
# VERDICT: the marker, not the code, was the thing that had become false. Removed. The assertions
# it wrapped are unchanged and now grade the repair, which is what they were written to do.
# ---------------------------------------------------------------------------------------------
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


# =============================================================================================
# T-175 — `PRICE_FIELD == 'unit_price'`, so NEITHER price wall ever looks at `total_price`
#
# The companion to T-156 with the design citation attached, and it is a DIFFERENT hole. T-156 is
# about a relation the boundary never checked between two numbers it could both see; this is
# about a number the boundary never SEES. `_walk` records a priced node only when it reads BOTH
# `product_ref` and `unit_price` off it, so a node naming a product and stating only a
# `total_price` is collected by nothing — outside the floor wall and outside the depth
# reconciliation alike, in a module whose own comments say a dict-built bid may put a priced node
# under any key it likes.
#
# Function-local imports again, for the reason stated at the T-156 header.
# =============================================================================================

#: MEASURED on `fixtures/envelopes/store-alpha.approved.json`: `ToolHooks.price_floor` answers
#: 10.00 for `prod-cap` and 95.00 for `prod-floor`, both listing at 100.00. Restated here rather
#: than probed so a fixture that changes under the sweep turns it RED instead of shrinking it —
#: the arming test asserts every one of these against the live facade.
T175_FLOORS: dict[str, float] = {"prod-cap": 10.0, "prod-floor": 95.0}

#: Where in a dict-built bid the priced node sits. Both are reached by `_walk`, by different
#: routes, and the ticket is about a field being unread rather than a node being unreachable — so
#: a fix that taught only the `offer` branch to read a total would still leave the sweep's second
#: half admitted. `offer` is `NESTED_OBJECT_FIELDS`; `quotes.alternate` is the unrecognized-key
#: path `_sweep` exists to walk.
T175_PLACEMENTS: tuple[str, ...] = ("offer", "quotes.alternate")

T175_PINNED_SEED = 20260904
T175_DRAWS_PER_SHAPE = 4
#: 2 products x 2 placements x 4 drawn prices.
T175_CASE_COUNT = 16


def _t175_cases() -> list[dict[str, Any]]:
    """Prices strictly under the envelope's own approved floor, drawn per product.

    Under the FLOOR, deliberately, and not merely under the list price: the floor is the wall
    whose subject a bare price unambiguously is, and `_price_refusal` refuses a unit price there
    with no grant, no discount and no depth arithmetic involved. That makes the control below —
    the identical node with the number spelled `unit_price` — refused for a reason that cannot be
    confused with anything this gate is asking for.

    Half of every draw is pinned (so a failure is reproducible) and half comes from
    `SystemRandom` (so the sweep cannot be fitted to a constant table), exactly as the T-156 and
    T-279 sweeps in this file draw.
    """
    import random  # noqa: PLC0415

    pinned = random.Random(T175_PINNED_SEED)
    system = random.SystemRandom()
    cases: list[dict[str, Any]] = []
    for product in sorted(T175_FLOORS):
        floor = T175_FLOORS[product]
        for placement in T175_PLACEMENTS:
            for cent in _t156_draw(
                1, int(round(floor * 100.0)) - 1, pinned, system, T175_DRAWS_PER_SHAPE
            ):
                cases.append(
                    {
                        "product": product,
                        "placement": placement,
                        "floor": floor,
                        "price": cent / 100.0,
                    }
                )
    return cases


def _t175_label(case: dict[str, Any], field: str) -> str:
    return (
        f"{case['product']} floor={case['floor']} at .{case['placement']}.{field}={case['price']}"
    )


def _t175_bid(case: dict[str, Any], field: str) -> dict[str, Any]:
    """A dict-shaped bid stating `case['price']` for `case['product']`, spelled `field`.

    Dict-shaped rather than a `contracts.Bid`, and that is the threat model this module already
    documents rather than a convenience: `Offer` forbids extra fields and requires its own, so a
    model-built bid cannot express "a product and a total and no unit at all". A Tier-2 store
    submits a mapping, and `provenance.py`'s own comment says it "can put
    ``{"product_ref": ..., "unit_price": ...}`` under any key it likes, where nothing collected
    it and therefore neither price wall ever saw it".

    `field` is the ONLY difference between the gate's bid and its control. Everything else — the
    product, the number, the placement, the empty claims and commitments — is byte-identical, so
    a difference in verdict can only be about which field name the walls read.
    """
    priced: dict[str, Any] = {"product_ref": case["product"], field: case["price"]}
    body: dict[str, Any] = {
        "auction_id": "auction-t175",
        "store_id": _t156_fixture()["envelope"]["store_id"],
        "claims": [],
        "agent_version": "store-agent/t175-gate",
        "schema_version": "1.0.0",
    }
    if case["placement"] == "offer":
        priced.update({"currency": "USD", "discount": None, "commitments": []})
        body["offer"] = priced
    else:
        outer, inner = case["placement"].split(".")
        body["offer"] = {
            "product_ref": case["product"],
            "unit_price": float(_t156_fixture()["catalog"][case["product"]]["list_price"]),
            "currency": "USD",
            "discount": None,
            "commitments": [],
        }
        body[outer] = {inner: priced}
    return body


def _t175_refusal(bid: dict[str, Any]) -> list[str]:
    """Every reason `enforce_bid_provenance` gives for `bid`, or `[]` when it admits it."""
    from store_agent.hooks import HookProvenanceError, enforce_bid_provenance  # noqa: PLC0415

    try:
        enforce_bid_provenance(bid, _t156_hooks())
    except HookProvenanceError as exc:
        return [reason for _, reason in exc.offenders]
    return []


def test_t175_the_unread_total_price_sweep_is_armed() -> None:
    """Sixteen real cases, sixteen controls that are refused TODAY, and an honest bid still gets
    in. NOT xfail.

    Five ways the gate below could report green while the defect lived, each closed here:

    1. **The sweep goes quiet.** A loop over zero cases passes. Three sweeps in this repo were
       found doing exactly that (6->0 of 8, 70->0 of 79, 48->0 of 66). Counted and de-duplicated
       before anything is concluded.
    2. **The drawn prices stop being the floor wall's subject.** Every one is asserted to be
       strictly under the product's floor as the LIVE facade reports it, so a fixture whose
       floors moved turns this red instead of quietly making the gate about nothing.
    3. **The refusal machinery is dead.** This is the load-bearing one, and it is what makes the
       gate's red a measurement rather than an assertion about an absent apparatus: the SAME
       node, at the SAME placement, with the SAME number spelled `unit_price` instead of
       `total_price`, must be REFUSED today, naming `unit_price`. Sixteen controls, all sixteen
       red at HEAD. If the walls ever stopped refusing under-floor prices at all, these fail
       first and the gate below stops being evidence.
    4. **The walls refuse everything.** A fix that refused every dict-built bid would satisfy a
       gate that only ever looks at dishonest ones. An honest bid at each placement — the list
       price itself, which clears both floors — is required to be ADMITTED.
    5. **The two spellings differ in something other than the field name.** The control bid and
       the gate bid are asserted to be identical dicts once the one key is renamed.
    """
    from store_agent.hooks import ToolHooks  # noqa: PLC0415

    hooks: ToolHooks = _t156_hooks()
    for product, floor in T175_FLOORS.items():
        live = hooks.price_floor(product)
        assert live == floor, (
            f"the approved envelope now floors {product!r} at {live}, not {floor}; the prices "
            "this sweep draws are written against the recorded floor and are no longer under it"
        )

    cases = _t175_cases()
    assert len(cases) == T175_CASE_COUNT, (
        f"the sweep generated {len(cases)} cases, not {T175_CASE_COUNT} — it has shrunk, and a "
        "shrunken sweep proves nothing"
    )
    keys = {(case["product"], case["placement"], case["price"]) for case in cases}
    assert len(keys) == T175_CASE_COUNT, (
        f"only {len(keys)} of {len(cases)} generated cases are DISTINCT"
    )
    assert {case["placement"] for case in cases} == set(T175_PLACEMENTS)

    for case in cases:
        assert 0.0 < case["price"] < case["floor"], (
            f"{_t175_label(case, 'unit_price')}: the drawn price is not strictly under the "
            "product's floor, so the floor wall is not its subject and the control below would "
            "not be refused for the reason this gate compares against"
        )

        # 5. One key renamed, and nothing else.
        gate_bid = _t175_bid(case, "total_price")
        control_bid = _t175_bid(case, "unit_price")
        assert json.dumps(gate_bid, sort_keys=True).replace(
            '"total_price"', '"unit_price"'
        ) == json.dumps(control_bid, sort_keys=True), (
            f"{_t175_label(case, 'total_price')}: the gate's bid and its control differ in more "
            f"than the priced field's name:\n  gate:    {gate_bid}\n  control: {control_bid}"
        )

        # 3. The control is refused TODAY, by name.
        reasons = _t175_refusal(control_bid)
        assert reasons, (
            f"{_t175_label(case, 'unit_price')}: the identical node spelled `unit_price` was "
            "ADMITTED. Both price walls have stopped refusing an under-floor price, so the gate "
            "below can no longer tell 'total_price is unread' from 'nothing is read'"
        )
        assert any("unit_price" in reason for reason in reasons), (
            f"{_t175_label(case, 'unit_price')}: refused, but no reason names unit_price "
            f"({reasons}); the gate below reads offender text the same way"
        )

    # 4. An honest bid at each placement still gets in.
    for placement in T175_PLACEMENTS:
        for product in sorted(T175_FLOORS):
            listed = float(_t156_fixture()["catalog"][product]["list_price"])
            honest = _t175_bid(
                {
                    "product": product,
                    "placement": placement,
                    "floor": T175_FLOORS[product],
                    "price": listed,
                },
                "unit_price",
            )
            assert _t175_refusal(honest) == [], (
                f"{product} at .{placement}: an HONEST bid stating the list price {listed} was "
                f"refused {_t175_refusal(honest)}; a wall that refuses honest bids would satisfy "
                "the gate below without reading anything"
            )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-175: `PRICE_FIELD = 'unit_price'` at provenance.py:142, so `_walk` records a priced "
        "node only where it reads BOTH `product_ref` and `unit_price`. A dict-built node naming "
        "a product and stating only a `total_price` is collected by nothing — `prices` comes "
        "back `[]` — so neither the floor wall nor the depth reconciliation ever sees the "
        "number, and DESIGN.md:127 publishes `price_value` as a function of exactly that field "
        "while the C11 accepted-event payload records it verbatim. Measured: 16 of 16 admitted, "
        "against 16 of 16 refused for the identical node with the number spelled `unit_price`; "
        "remove this marker with the fix"
    ),
)
def test_t175_a_total_price_under_the_envelope_floor_is_refused_like_a_unit_price_is() -> None:
    """A price the store agent's own walls never read is a price the merchant never approved.

    The relation asserted is the FLOOR's, not a quantity's, and that is what keeps this gate
    closeable without answering T-156's open design question. Nothing here says what a total
    ought to be relative to a unit, or how many units a total is for. It says that a number
    stating what a buyer pays for a catalogued product must be inside the envelope the merchant
    approved, exactly as the number one field over already is — `apps/exchange/src/ranking`
    reads `("total_price", "unit_price", "price")` in that order, `total_price` FIRST, so the
    unread field is the one the published rank formula prefers.

    **Distinct from T-156, and left red on purpose by the lane that fixed T-156.** That repair
    added `_total_price_refusal`, which reconciles a total against the unit price standing
    beside it on the same node. It is silent here by construction: these nodes state no unit
    price at all, so there is nothing for that relation to compare against and
    `ClaimMaterial.totals` comes back empty. Closing this one means teaching `_walk` that a
    product plus a total is offer material — the `PRICE_FIELD` the ticket names.

    MEASURED at HEAD (and again after the T-156 repair, unchanged): `collect_claim_material`
    returns `prices=[]` and `totals=[]` for the `.offer` shape, `enforce_bid_provenance` admits
    it, and the identical node with `unit_price` in place of `total_price` is refused by BOTH
    walls — "under the envelope's approved floor of 10.0" and "99.95% off its list price".
    """
    cases = _t175_cases()
    assert len(cases) == T175_CASE_COUNT, (
        f"the sweep generated {len(cases)} cases, not {T175_CASE_COUNT}; see the arming test"
    )

    escapes: list[str] = []
    for case in cases:
        label = _t175_label(case, "total_price")
        reasons = _t175_refusal(_t175_bid(case, "total_price"))
        if not reasons:
            escapes.append(f"{label}: enforce_bid_provenance ADMITTED it")
        elif not any("total_price" in reason for reason in reasons):
            escapes.append(f"{label}: refused, but not about total_price — {reasons}")

    assert not escapes, (
        f"{len(escapes)} of {len(cases)} bids stating a total_price under the envelope's own "
        "approved floor for a catalogued product were admitted by the store agent's auditor, "
        "where the identical node spelling the same number `unit_price` is refused by both "
        "walls:\n  " + "\n  ".join(escapes)
    )


# =============================================================================================
# T-209 / T-218 — the contracts boundary was tightened and the store agent's own auditor was not
#
# One apparatus, two gates, and ONE arming control named for both — `-k t218` is T-218's own
# selector and it must select a control that passes, or a red under it could be a collection
# accident rather than a live defect.
# T-209 is the named case (`offer.commitments: null`); T-218 is the
# ticket that says the cause is generic and asks for "a property test asserting the two doors
# agree on nullability, not six more point fixes". So the family below is DISCOVERED at run time
# from the contracts door itself rather than written down: every field the pydantic model refuses
# a `null` for is in it, including the ones a future `--strict-nullable` regeneration adds. That
# is the structural half of T-218 — "every future field the generator tightens silently joins the
# family, and nothing on the store-agent side notices" — and a hard-coded list of six would
# reproduce exactly the defect it is grading.
#
# Function-local imports, for the reason stated at the T-156 header.
# =============================================================================================

#: `now` for the contracts door. Fixed, so the offer's expiry is judged against a constant.
T209_NOW = "2026-06-01T00:00:00Z"

#: The eligibility row for the base bid's store. The nullability question is about SCHEMA, so
#: every other wall on that door is satisfied deliberately — a bid refused for eligibility would
#: be refused with the flipped field too, and the comparison would be measuring nothing.
T209_SNAPSHOT_SCORE = 0.6

#: Fields the ticket says are in the family. NOT the source of truth — the family is discovered
#: from the model below — but the arming test requires the discovered set to CONTAIN these, so a
#: regeneration that quietly widened `commitments` back to optional turns this red instead of
#: shrinking the sweep to nothing. T-218 says "at least six"; nine were measured.
T209_EXPECTED_FAMILY = frozenset(
    {
        "auction_id",
        "store_id",
        "offer",
        "claims",
        "agent_version",
        "schema_version",
        "offer.commitments",
        "offer.unit_price",
        "offer.total_price",
    }
)

#: Fields that are genuinely nullable by contract. Both doors must ADMIT a null here, or the
#: property below would be asking the store agent to refuse honest traffic.
T209_NULLABLE_CONTROLS = ("pitch_ref", "message", "offer.currency", "offer.variant_ref")

#: The one field in the family both doors ALREADY agree about. It is the negative control: it
#: proves the store agent's auditor CAN refuse a null-valued field, so the escapes below are a
#: measurement of which fields it looks at rather than of an auditor that never refuses anything.
T209_AGREED_CONTROL = "offer.product_ref"


def _t209_base() -> dict[str, Any]:
    """A dict-shaped bid BOTH doors admit. Every value here is load-bearing.

    `product_ref` is on the approved envelope's catalog and `unit_price` is its list price with
    no discount declared, so the floor wall, the reconciliation and the grant ledger are all
    satisfied and cannot stand in for the schema question. `claims` and `commitments` are empty
    for the same reason. `expires_at` is in the future because the shared door fails closed on a
    missing expiry, which is a refusal that has nothing to do with nullability.
    """
    envelope = _t156_fixture()["envelope"]
    listed = float(_t156_fixture()["catalog"]["prod-cap"]["list_price"])
    return {
        "auction_id": "auction-t209",
        "store_id": envelope["store_id"],
        "offer": {
            "product_ref": "prod-cap",
            "unit_price": listed,
            "total_price": listed,
            "currency": "USD",
            "commitments": [],
            "expires_at": "2999-01-01T00:00:00Z",
        },
        "claims": [],
        "agent_version": "store-agent/t209-gate",
        "schema_version": "1.0.0",
    }


def _t209_snapshot() -> dict[str, Any]:
    store_id = _t156_fixture()["envelope"]["store_id"]
    return {
        store_id: {
            "store_id": store_id,
            "score": T209_SNAPSHOT_SCORE,
            "blacklisted": False,
        }
    }


def _t209_paths() -> list[str]:
    """Every field of `Bid`, plus every field of `Offer` as ``offer.<name>``.

    Read off the generated models rather than listed, so a field the schema gains is a field this
    sweep asks about on its next run. That is the whole of T-218's structural complaint.
    """
    from contracts.protocol import Bid, Offer  # noqa: PLC0415

    return [*Bid.model_fields, *(f"offer.{name}" for name in Offer.model_fields)]


def _t209_with_none(path: str) -> dict[str, Any]:
    """`_t209_base()` with exactly one field set to an explicit `None`."""
    import copy  # noqa: PLC0415

    body = copy.deepcopy(_t209_base())
    head, _, tail = path.partition(".")
    if tail:
        body[head][tail] = None
    else:
        body[head] = None
    return body


def _t209_contracts_verdict(body: dict[str, Any]) -> list[str]:
    """The shared door's reasons for `body`. It never raises."""
    from contracts.boundary import validate_bid  # noqa: PLC0415

    return list(
        validate_bid(body, path="hosted", trust_snapshot=_t209_snapshot(), now=T209_NOW).reasons
    )


def _t209_schema_refused(path: str) -> bool:
    """Whether the CONTRACTS door refuses a null at `path` as a schema violation.

    `schema_invalid:<dotted path>` specifically, not any refusal: `offer.expires_at` is genuinely
    nullable at the pydantic door and is refused `offer_expiry_missing` by a different wall, and
    counting that as a nullability divergence would put a field in the family that the generator
    never tightened.
    """
    for reason in _t209_contracts_verdict(_t209_with_none(path)):
        if str(reason).startswith("schema_invalid") and path in str(reason):
            return True
    return False


def _t209_store_agent_admits(body: dict[str, Any]) -> bool:
    """Whether the STORE AGENT's own auditor lets `body` through."""
    from store_agent.hooks import HookProvenanceError, enforce_bid_provenance  # noqa: PLC0415

    try:
        enforce_bid_provenance(body, _t156_hooks())
    except HookProvenanceError:
        return False
    return True


def _t209_family() -> list[str]:
    """Every field the contracts door refuses a null for, discovered rather than listed."""
    return [path for path in _t209_paths() if _t209_schema_refused(path)]


def test_t209_t218_the_two_doors_nullability_sweep_is_armed() -> None:
    """A base both doors admit, a family discovered from the model, and an auditor that can
    still refuse. NOT xfail.

    Five ways the gates below could report green while the divergence lived:

    1. **The family is empty.** If the discovery probe stopped seeing `schema_invalid` reasons —
       a regeneration without `--strict-nullable`, a reason string respelled — the property
       below would iterate nothing and pass. The discovered family is required to contain the
       nine measured fields by name.
    2. **The base bid is not admitted.** Every case is the base with ONE field flipped, so if the
       base were refused, every case would be refused for the base's reason and the sweep would
       be blind. Both doors are required to admit it.
    3. **The auditor refuses everything.** A store agent that refused any dict-built bid would
       satisfy the property without reading a field. The genuinely NULLABLE fields are required
       to be admitted by both doors.
    4. **The auditor refuses nothing.** The mirror image, and the one that matters: if
       `enforce_bid_provenance` could not refuse a null-valued field at all, the escapes below
       would be an artefact. `offer.product_ref` is the negative control — a field in the family
       that BOTH doors already refuse today — so the apparatus is proven able to produce the
       verdict the gate is asking for.
    5. **The two doors are not being asked about the same document.** The same dict object is
       handed to both, built once per case.
    """
    base = _t209_base()
    assert _t209_contracts_verdict(base) == [], (
        f"the base bid is refused by the contracts door {_t209_contracts_verdict(base)}; every "
        "case below is this bid with one field flipped, so the sweep would be measuring the "
        "base's refusal rather than the flip"
    )
    assert _t209_store_agent_admits(base), (
        "the base bid is refused by enforce_bid_provenance; every case below would be refused "
        "for the base's reason and the property would pass without reading a field"
    )

    paths = _t209_paths()
    assert len(paths) == len(set(paths)) and len(paths) >= 15, (
        f"the model fields this sweep asks about are {paths}; that is not the shape of Bid+Offer"
    )

    family = _t209_family()
    missing = sorted(T209_EXPECTED_FAMILY - set(family))
    assert not missing, (
        f"the contracts door no longer refuses a null at {missing}. Either --strict-nullable was "
        "dropped from the generator (T-195's fix, packages/contracts/src/codegen.py) or the "
        "reason spelling moved; the family this property sweeps is discovered from that door, so "
        "it has just silently shrunk"
    )

    # 3. Genuinely nullable fields, admitted by BOTH.
    for path in T209_NULLABLE_CONTROLS:
        assert path not in family, (
            f"{path} is documented nullable but the contracts door now refuses a null there; the "
            "control has become a case"
        )
        body = _t209_with_none(path)
        assert _t209_contracts_verdict(body) == [], (
            f"{path}: a null in a NULLABLE field was refused by the contracts door "
            f"{_t209_contracts_verdict(body)}"
        )
        assert _t209_store_agent_admits(body), (
            f"{path}: a null in a NULLABLE field was refused by enforce_bid_provenance; the "
            "property below would be asking the auditor to refuse honest traffic"
        )

    # 4. The negative control: one field in the family that BOTH doors already refuse.
    assert T209_AGREED_CONTROL in family, (
        f"{T209_AGREED_CONTROL} is no longer refused by the contracts door, so it can no longer "
        "serve as the control proving the two doors CAN agree"
    )
    assert not _t209_store_agent_admits(_t209_with_none(T209_AGREED_CONTROL)), (
        f"enforce_bid_provenance now ADMITS a null {T209_AGREED_CONTROL}. That was the one field "
        "in the family the store agent refused, and it is what proved the auditor is able to "
        "refuse a null-valued field at all — without it the gates below measure nothing"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-209: T-195 regenerated the model with --strict-nullable, so `offer.commitments: null` "
        "is now correctly refused `schema_invalid:offer.commitments` at the contracts door. The "
        "store agent walks `commitments` with no pydantic gate at all: `_walk`'s first statement "
        "is `if node is None: return`, so the null is skipped SILENTLY — not recorded in "
        "`unwalkable`, not refused — and a dict-built bid carrying it is admitted by "
        "enforce_bid_provenance. The two doors disagree about the same shape; remove this marker "
        "with the fix"
    ),
)
def test_t209_a_null_commitments_list_is_refused_by_the_store_agents_own_door_too() -> None:
    """The named case. `offer.commitments: null` must not be admitted by the auditor that walks it.

    `commitments` is in `CLAIM_BEARING_FIELDS`, so the walk goes looking for claims under it and
    finds a `None`. Returning on that is the difference between "there are no claims here" and
    "I could not look", and the boundary's own comment elsewhere insists that difference be said
    out loud: the `unwalkable` list exists precisely so that "we did not look" is an answer the
    boundary gives rather than swallows. A null where a list belongs is the same silence with a
    tighter door one package over now refusing it.
    """
    body = _t209_with_none("offer.commitments")
    assert any(
        str(reason).startswith("schema_invalid") for reason in _t209_contracts_verdict(body)
    ), (
        "the contracts door no longer refuses this shape either, so there is no disagreement "
        "left to grade; see the arming test"
    )
    assert not _t209_store_agent_admits(body), (
        "enforce_bid_provenance ADMITTED a bid whose offer.commitments is an explicit null, "
        "which contracts.validate_bid refuses as "
        f"{_t209_contracts_verdict(body)}. The walk short-circuits on the None and records "
        "nothing, so the auditor cannot even report that it did not look"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-218: the cause is generic, not specific to commitments. `_walk` short-circuits on ANY "
        "None while --strict-nullable made a growing set of fields non-nullable at the pydantic "
        "door, so the family is whatever the generator has tightened so far. MEASURED: 9 of the "
        "10 fields the contracts door refuses a null for are ADMITTED by enforce_bid_provenance, "
        "with offer.product_ref the only one the two doors agree about. Every future field the "
        "generator tightens joins the family and nothing on the store-agent side notices; remove "
        "this marker with the fix"
    ),
)
def test_t218_the_two_doors_agree_about_every_field_that_may_not_be_null() -> None:
    """The property, not the six point fixes: whatever the contracts door refuses a null for,
    the store agent's own auditor must refuse too.

    The family is DISCOVERED from the contracts door on every run, which is the part that makes
    this a property rather than a longer list. T-218's structural complaint is that a field the
    generator tightens tomorrow joins the divergence silently; a sweep that enumerated today's
    six would go on passing through exactly that. This one asks the model.

    It does not require the same REASON from both doors, and deliberately so. The store agent
    does not speak `schema_invalid` and should not learn to — it is not a schema validator, it is
    a provenance auditor, and the honest repair is for the walk to record a null where it expected
    a container instead of returning on it silently. All this asks is that the bid not be
    ADMITTED.

    MEASURED at HEAD: 9 in the family, 8 of them admitted by `enforce_bid_provenance`, the ninth
    (`offer.product_ref`) refused by a price wall that happens to need the name.
    """
    family = _t209_family()
    assert family, "the discovered family is empty; see the arming test"

    escapes: list[str] = []
    for path in family:
        body = _t209_with_none(path)
        if _t209_store_agent_admits(body):
            escapes.append(
                f"{path}: refused {_t209_contracts_verdict(body)} by the contracts "
                "door, ADMITTED by enforce_bid_provenance"
            )

    assert not escapes, (
        f"{len(escapes)} of {len(family)} fields that may not be null at the contracts door are "
        "admitted with an explicit null by the store agent's own auditor:\n  "
        + "\n  ".join(escapes)
    )


# =============================================================================================
# T-280 — a refusal receipt with no identity
# =============================================================================================

#: Nesting depths past `door._SNAPSHOT_MAX_DEPTH` (32). `_snapshot` raises `ValueError` on these
#: and the door answers `malformed_submission` — the one refusal site in the file that passes no
#: `payload=`. Restated rather than imported so a bound that moves makes the arming test say so.
T280_DEPTHS: tuple[int, ...] = (33, 34, 36, 40, 48, 64)

T280_SIGNER = "store-external-t280"
T280_KEY_ID = "key-2026-01"
T280_SECRET = "gate-secret-t280"
T280_NOW = "2026-01-01T00:00:05Z"
T280_DEADLINE = "2026-01-01T00:05:00Z"


def _t280_payload(depth: int) -> dict[str, Any]:
    """A fully valid, correctly signable submission carrying one absurdly nested extra key.

    A PLAIN dict, and that is the whole point of the shape: every read `_refuse` would make is
    an ordinary dict lookup that cannot fail, so the identity is sitting right there and the
    receipt's silence about it is the omission and nothing else. A mapping whose reads raise
    would be refused anonymously by `_refuse`'s own guard no matter what this site passed, and
    would therefore grade nothing.
    """
    node: Any = {"leaf": "kettle"}
    for level in range(depth):
        node = {f"layer{level}": node}
    return {
        "auction_id": f"auction-t280-{depth}",
        "store_id": "store-t280",
        "offer": {
            "product_ref": "prod-t280",
            "unit_price": 10.0,
            "total_price": 10.0,
            "discount": None,
            "commitments": [],
            "expires_at": "2999-01-01T00:00:00Z",
        },
        "claims": [],
        "message": "a submission the door cannot copy",
        "agent_version": "ext-1.0.0",
        "schema_version": "1",
        "signer_id": T280_SIGNER,
        "key_id": T280_KEY_ID,
        "issued_at": "2026-01-01T00:00:00Z",
        "nonce": f"nonce-t280-{depth}",
        "deep": node,
    }


def _t280_receipt(depth: int, *, signature: Any = None) -> Any:
    from store_agent.external import NonceStore, receive_bid, sign_bid  # noqa: PLC0415

    payload = _t280_payload(depth)
    return receive_bid(
        payload,
        sign_bid(payload, T280_SECRET) if signature is None else signature,
        {T280_SIGNER: {T280_KEY_ID: T280_SECRET}},
        queue=lambda item: None,
        nonce_store=NonceStore(),
        now=T280_NOW,
        auction_deadline=T280_DEADLINE,
        trust_snapshot={
            "store-t280": {"store_id": "store-t280", "score": 0.9, "blacklisted": False}
        },
    )


def test_t280_the_anonymous_receipt_sweep_is_armed() -> None:
    """Six submissions the door really cannot copy, and a neighbouring refusal that DOES carry
    identity on the identical payload. NOT xfail.

    Four ways the gate below could report green while the omission lived:

    1. **The sweep reaches a different refusal.** `malformed_submission` is emitted at exactly
       two sites, and only one of them — the `_snapshot` failure — is this ticket's. Every case
       is required to come back with that reason and no other, so a submission refused earlier
       (an incomplete envelope, an unknown key, a stale `issued_at`) cannot be mistaken for it.
    2. **The identity was never readable.** The payload is a plain dict; its `signer_id` and
       `nonce` are asserted to be present, non-empty strings, so "the receipt has no identity"
       cannot be explained by there being none to carry.
    3. **The receipt cannot carry identity at all.** This is the control that makes the gate a
       measurement. The SAME payload presented with an empty signature is refused
       `signature_missing` at a site that DOES pass `payload=`, and that receipt is required to
       carry both fields today. If `ExternalBidReceipt` ever stopped reporting them, this fails
       first.
    4. **The nesting bound moved.** If `_SNAPSHOT_MAX_DEPTH` were raised past these depths the
       cases would sail through and be ACCEPTED, and a sweep of accepted submissions grades
       nothing. Requirement 1 catches that.
    """
    for depth in T280_DEPTHS:
        payload = _t280_payload(depth)
        assert isinstance(payload["signer_id"], str) and payload["signer_id"], (
            f"depth {depth}: the submission carries no readable signer_id"
        )
        assert isinstance(payload["nonce"], str) and payload["nonce"], (
            f"depth {depth}: the submission carries no readable nonce"
        )

        receipt = _t280_receipt(depth)
        assert receipt.accepted is False, f"depth {depth}: the door ADMITTED it: {receipt!r}"
        assert receipt.reasons == ("malformed_submission",), (
            f"depth {depth}: refused {receipt.reasons}, not the single malformed_submission this "
            "gate is about. The case is no longer reaching the _snapshot failure — check whether "
            "the door's copy bound moved, or whether an earlier gate now refuses this shape"
        )

    # 3. The neighbouring refusal, on the identical payload, DOES carry identity.
    control = _t280_receipt(T280_DEPTHS[0], signature="")
    assert control.reasons == ("signature_missing",), (
        f"the control was refused {control.reasons}, not signature_missing; it can no longer "
        "stand for 'a refusal that passes payload='"
    )
    assert control.signer_id == T280_SIGNER and control.nonce, (
        f"the control receipt carries signer_id={control.signer_id!r} nonce={control.nonce!r}. "
        "A refusal that DOES pass payload= has stopped reporting identity, so the gate below "
        "would be measuring the receipt rather than the omission"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-280: the `malformed_submission` refusal for a mapping whose reads fail during "
        "`_snapshot` is built by `_refuse(REASON_MALFORMED_SUBMISSION)` with no `payload=`, so "
        "the receipt carries no signer_id and no nonce and cannot be tied to the submission it "
        "refused. The same file argues the opposite case ninety lines earlier for "
        "door_failed_closed — 'without it a genuine internal fault produced a receipt with no "
        "identity at all, indistinguishable in a rejection log from an ordinary policy refusal' "
        "— and every neighbouring refusal passes payload=. Measured: 6 of 6 anonymous; remove "
        "this marker with the fix"
    ),
)
def test_t280_a_refused_submission_the_door_could_read_is_named_in_its_own_receipt() -> None:
    """A rejection log entry that cannot be tied to a submission is not a rejection log entry.

    The submissions swept here are plain dicts whose `signer_id` and `nonce` are ordinary
    strings — the door read them to get this far — and `_refuse`'s own reads are guarded, so
    handing it the caller's object at this site cannot itself raise. That guarantee is the
    reason the file gives for passing `payload=` at the `door_failed_closed` site, and it holds
    here unchanged.

    The sibling site — the not-a-`Mapping` refusal — is deliberately NOT swept. There the object
    provably is not a mapping, so there is nothing to read and the omission is defensible. This
    is the one where a submission with a perfectly readable identity is refused anonymously.
    """
    escapes: list[str] = []
    for depth in T280_DEPTHS:
        receipt = _t280_receipt(depth)
        if receipt.signer_id != T280_SIGNER or not receipt.nonce:
            escapes.append(
                f"depth {depth}: refused {receipt.reasons} with signer_id="
                f"{receipt.signer_id!r} nonce={receipt.nonce!r}, where the submission states "
                f"signer_id={T280_SIGNER!r} nonce={_t280_payload(depth)['nonce']!r}"
            )

    assert not escapes, (
        f"{len(escapes)} of {len(T280_DEPTHS)} malformed_submission receipts carry no identity, "
        "so nothing in a rejection log can tie them to the submission they refused:\n  "
        + "\n  ".join(escapes)
    )


# =============================================================================================
# T-220 — cycle detection is correct, and its depth ceiling is invisible
#
# The product code is RIGHT here and this gate does not ask for it to change. `MAX_SWEEP_DEPTH`
# is gone, the cycle guard replaced it, and a priced node at depth 200 is refused today. What is
# wrong is that nothing would NOTICE the bound coming back: the two tests grading "there is no
# depth at which a bid stops being checked" parametrize layers either side of the OLD bound of
# 12 and stop at 64, so a bound re-introduced at 65 — or 100, or 400 — is invisible to the whole
# suite. The ticket's own reproduction is "re-introduce a depth bound of 65 in _walk and run the
# suite: green".
#
# So this gate is about the coverage, and it grades the two parametrizations directly. That does
# mean a test reading a test file — see the note in the gate's docstring, which is honest about
# the limit rather than hiding it.
# =============================================================================================

#: The dotted name pytest gives the sibling module. The hyphen in `store-agent` is why this is
#: `importlib.import_module` and not an `import` statement — `packages.store-agent...` is not
#: valid syntax. Under `--import-mode=importlib` (pyproject) with
#: `consider_namespace_packages = true`, this is the exact key the module is registered under
#: during collection, so the object read here is the one pytest ran.
T220_SIBLING = "packages.store-agent.tests.test_price_reconciliation"

#: The two tests whose parametrization is the subject.
T220_GRADED = (
    "test_a_priced_node_is_collected_however_deep_the_bid_buries_it",
    "test_a_claim_is_checked_however_deep_the_bid_buries_it",
)

#: The depth the parametrizations must reach. FOUR TIMES the deepest value they carry today, so
#: it is past any bound a lane could plausibly re-introduce while staying far under the only
#: ceiling that really exists — the interpreter's own recursion limit, which `_walk` meets at
#: roughly 450-500 layers inside a pytest process. The arming test DRIVES a bid at exactly this
#: depth and requires it to be refused by the price wall, so this number can never quietly become
#: one no repair could satisfy: if the stack ever shrank under it, the arming test goes red and
#: says which depth stopped being reachable, instead of the gate becoming unclosable in silence.
T220_REQUIRED_DEPTH = 256

#: Depths the arming test drives directly, to show the PROPERTY holds today well past 64 — which
#: is what makes this a coverage ticket rather than a product one.
T220_PROBE_DEPTHS = (65, 128, T220_REQUIRED_DEPTH)


def _t220_sibling() -> Any:
    import importlib  # noqa: PLC0415

    return importlib.import_module(T220_SIBLING)


def _t220_depths(func: Any) -> list[int]:
    """The `layers` argvalues a parametrized test actually runs, read off its own marks."""
    found: list[int] = []
    for mark in getattr(func, "pytestmark", []):
        if getattr(mark, "name", None) != "parametrize":
            continue
        names, values = mark.args[0], mark.args[1]
        if "layers" not in str(names).replace(" ", "").split(","):
            continue
        found.extend(int(value) for value in values)
    return found


def _t220_buried_bid(module: Any, layers: int) -> dict[str, Any]:
    """The sibling's own `_buried` helper, so this grades the same construction it does."""
    return {
        "offer": {"product_ref": "prod-cap", "discount": None},
        "claims": [],
        **module._buried(  # noqa: SLF001 - the helper under discussion is module-private
            {"product_ref": "prod-cap", "unit_price": 1.0, "total_price": 1.0}, layers
        ),
    }


def test_t220_the_depth_coverage_reader_is_armed() -> None:
    """The reader really reads marks, the two tests still exist, and the depth this gate demands
    is one a repair could actually reach. NOT xfail.

    Five ways the gate below could report green while the ceiling stayed invisible:

    1. **The module does not import**, or the two tests were renamed. Either would make
       `_t220_depths` return `[]` for a name that no longer exists, and `max([])` would raise
       rather than assert — but a gate that errors is a gate nobody reads. Both names are
       resolved here first.
    2. **The mark reader reads nothing.** A locally-decorated function with KNOWN argvalues is
       fed to the same reader, so "no depths found" cannot be confused with "no coverage".
    3. **The reader matches any parametrize.** A second local function parametrized on a
       different argument is required to yield NO depths, so the reader is selecting on `layers`
       rather than on "has a parametrize".
    4. **The demanded depth is unreachable.** This is the one that keeps the gate closeable. The
       property is DRIVEN at 65, 128 and 256 against the live boundary and required to hold —
       refused, with the price wall naming `unit_price`. If the interpreter's stack ever stopped
       accommodating 256 layers, this fails and names the depth, rather than the gate silently
       becoming impossible to satisfy.
    5. **The construction drifted.** `_buried` is taken from the sibling module rather than
       reimplemented, so this arming test and the tests it grades bury a node the same way.
    """
    from store_agent.hooks import HookProvenanceError, enforce_bid_provenance  # noqa: PLC0415

    module = _t220_sibling()
    for name in T220_GRADED:
        assert hasattr(module, name), (
            f"{T220_SIBLING} no longer defines {name!r}. The gate below grades that test's depth "
            "parametrization by name, so a rename silently unhooks it — which is the same class "
            "of failure the ticket is about"
        )
        assert _t220_depths(getattr(module, name)), (
            f"{name} no longer parametrizes `layers`; the reader found nothing, and a gate that "
            "reads nothing passes"
        )

    # 2 + 3. The reader is proven on functions whose argvalues are known here.
    @pytest.mark.parametrize("layers", [7, 9000])
    def _control(layers: int) -> None:  # pragma: no cover - never executed, only inspected
        pass

    @pytest.mark.parametrize("width", [1, 2])
    def _decoy(width: int) -> None:  # pragma: no cover - never executed, only inspected
        pass

    assert _t220_depths(_control) == [7, 9000], (
        "the mark reader cannot see argvalues it is pointed straight at, so an empty result from "
        "the two real tests would mean nothing"
    )
    assert _t220_depths(_decoy) == [], (
        "the mark reader answers for a parametrize on a different argument, so it is not reading "
        "`layers` at all"
    )

    # 5. The construction is the sibling's own.
    assert module._buried({"x": 1}, 3) == {"layer2": {"layer1": {"layer0": {"x": 1}}}}  # noqa: SLF001

    # 4. The property holds today at every depth this gate demands.
    for depth in T220_PROBE_DEPTHS:
        bid = _t220_buried_bid(module, depth)
        with pytest.raises(HookProvenanceError) as raised:
            enforce_bid_provenance(bid, _t156_hooks(), product_ref="prod-cap")
        reasons = " ".join(reason for _, reason in raised.value.offenders)
        assert "unit_price" in reasons, (
            f"at depth {depth} the boundary no longer refuses a 1.00 price on a 100.00 product "
            f"by naming unit_price ({reasons[:200]}). If this is the recursion ceiling, then "
            f"T220_REQUIRED_DEPTH={T220_REQUIRED_DEPTH} is no longer a depth a repair could add "
            "to the parametrizations, and the number in this file has to come down before the "
            "gate below can be closed"
        )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-220: the two tests grading 'there is no depth at which a bid stops being checked' "
        "parametrize layers as [1, 11, 13, 14, 64] and [1, 13, 14, 64] — values chosen either "
        "side of the OLD bound of 12 — so a depth bound re-introduced at 65 or beyond is "
        "invisible to the entire suite. The product code is correct; the coverage pins the fix "
        "that was made rather than the property that was promised. MEASURED: max depth graded "
        "anywhere in the package is 64, while the boundary is demonstrably still checking at "
        "256; remove this marker with the fix"
    ),
)
def test_t220_the_suite_grades_depths_a_reintroduced_bound_could_hide_behind() -> None:
    """A bound at 65 must not be able to hide from the tests that exist to forbid it.

    The two tests this reads are the only ones in the repo that grade depth through the
    provenance walk, and both stop at 64 — one layer above the old bound of 12, chosen when 12
    was the number that mattered. Every claim they make is therefore true only up to 64, while
    the property they document ("there is no depth at which a bid stops being checked") is
    unbounded. That gap is the ticket.

    **This gate reads a test file, and that is a real limit worth stating rather than hiding.**
    The lane rule is that a test may never grade a file inside its own write scope, because an
    assertion you can satisfy by editing the thing it measures measures nothing. There is no way
    to honour it here: T-220's recorded location IS
    `packages/store-agent/tests/test_price_reconciliation.py`, and the defect IS that file's
    coverage. What keeps this honest is that the repair the gate asks for cannot be faked into
    existence — the arming test DRIVES the boundary at the demanded depth, so a parametrization
    extended to 256 has to actually run and actually refuse. Adding the number without the
    property working would turn the sibling tests red, not this one green.

    One trap for whoever closes it, measured rather than guessed: past roughly 450-500 layers
    `_walk` meets the interpreter's own recursion limit, `collect_claim_material` catches the
    `RecursionError`, and the bid is still REFUSED — but with the `unwalkable` reason ("this bid
    nests deeper than the boundary can walk") instead of the price wall's, so
    `assert "unit_price" in reasons` goes red there for a correct behaviour. A parametrization
    pushed to 1000 or 5000 must accept EITHER refusal path. 256 is chosen to sit well inside the
    price-wall regime, which the arming test re-establishes on every run.
    """
    module = _t220_sibling()
    shortfalls: list[str] = []
    for name in T220_GRADED:
        depths = _t220_depths(getattr(module, name))
        if max(depths) < T220_REQUIRED_DEPTH:
            shortfalls.append(
                f"{name} parametrizes layers={depths}, deepest {max(depths)} — a bound "
                f"re-introduced anywhere above {max(depths)} is invisible to it"
            )

    assert not shortfalls, (
        f"{len(shortfalls)} of {len(T220_GRADED)} depth properties stop short of "
        f"{T220_REQUIRED_DEPTH}, which the arming test just proved the boundary still checks "
        "at:\n  " + "\n  ".join(shortfalls)
    )


# =============================================================================================
# T-321 — six contract/served sweep helpers, one implementation per package, nothing enforcing
# that they agree
#
# They agree TODAY — the arming test measures that rather than assuming it. The defect is that
# nothing makes them: three implementations of one rule, each free to drift, and they HAVE
# drifted once already (a regex path-normaliser here disagreed with the other two on malformed
# input). So the gate is about the number of implementations, which is the thing that can be
# fixed; a gate asserting they currently agree would be green and would grade nothing.
# =============================================================================================

#: Every gate file that could hold a copy. Eleven files share this name across the repo, not the
#: three the ticket counts, and the arming test reports the real number — a sweep scoped to
#: three files would miss a fourth copy appearing tomorrow, which is the same drift the ticket is
#: about.
T321_GATE_FILE = "test_repro_open_tickets.py"
T321_ROOTS = ("apps", "packages", "services", "proxyshop_support")

#: The six helpers, grouped by ROLE rather than by name, because the copies do not even share
#: their names: this package spells the normaliser `normalise` and the other two spell it
#: `_normalise_route`. Grouping by role is what lets the sweep see that they are the same helper
#: wearing different labels — a sweep keyed on the literal name would report one definition each
#: and conclude, wrongly, that nothing is duplicated.
T321_ROLES: dict[str, tuple[str, ...]] = {
    "path normaliser": ("normalise", "_normalise_route"),
    "published operations": ("published_operations", "_published_operations"),
    "raw published operations": ("published_raw",),
    "served operations": ("served_operations", "_served_operations"),
    "divergence message": ("divergence", "_operation_divergence"),
    "contract probe app": ("probe_app_for", "_contract_probe_app"),
    "operation extractor": ("_operations",),
}

#: Malformed paths the three copies must agree about. Every one of them is a shape the ORIGINAL
#: divergence turned on: a brace with no closing brace, a nested brace, an empty parameter. The
#: regex spelling and the partition spelling gave `/a/{b/{}` and `/a/{}` for the second.
T321_MALFORMED = (
    "/a/{b/{c}",
    "/a/{b",
    "/a/{}",
    "/{}/{}",
    "/a/{b}/{c}",
    "/stores/{store_id}/trust",
    "/",
    "",
    "/a/}b{/c",
    "/{a{b}c}",
)


def _t321_gate_files() -> list[Any]:
    """Every `test_repro_open_tickets.py` under the source roots, sorted."""
    found: list[Any] = []
    for root in T321_ROOTS:
        found.extend(sorted((REPO_ROOT / root).rglob(T321_GATE_FILE)))
    return [path for path in found if ".venv" not in path.parts and ".pkgroot" not in path.parts]


def _t321_definitions() -> dict[str, list[str]]:
    """`{role: [<repo-relative file>::<name>, ...]}` — where each role is DEFINED.

    AST rather than a text search: a name inside a docstring, a comment or a triple-quoted
    subprocess script is a mention and not a definition, and this repo already has one gate whose
    call sites live inside a string literal. Only a real `def` at module level counts.
    """
    import ast  # noqa: PLC0415

    aliases = {name: role for role, names in T321_ROLES.items() for name in names}
    found: dict[str, list[str]] = {role: [] for role in T321_ROLES}
    for path in _t321_gate_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in aliases:
                found[aliases[node.name]].append(f"{path.relative_to(REPO_ROOT)}::{node.name}")
    return found


def _t321_normalisers() -> dict[str, Any]:
    """`{module name: the path normaliser it defines}`, for every gate file that has one."""
    import importlib  # noqa: PLC0415

    resolved: dict[str, Any] = {}
    for path in _t321_gate_files():
        parts = path.relative_to(REPO_ROOT).with_suffix("").parts
        name = ".".join(parts)
        try:
            module = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - a module that will not import has no helper to compare
            continue
        for alias in T321_ROLES["path normaliser"]:
            helper = getattr(module, alias, None)
            if callable(helper):
                resolved[f"{name}.{alias}"] = helper
    return resolved


def test_t321_the_duplicated_helper_scan_is_armed() -> None:
    """The scanner finds definitions it is pointed at, the copies really exist, and they agree
    TODAY. NOT xfail.

    Four ways the gate below could report green while the duplication lived:

    1. **The scan finds nothing.** A path root that moved, a file renamed, an AST walk that
       silently returned `[]` — every one of them makes a "no duplicates" verdict vacuous. The
       file count and the definition count are asserted first, and this very module is required
       to be among the files found, defining the normaliser it is pointed straight at.
    2. **The scan matches everything.** A role whose names appear nowhere is required to resolve
       to zero definitions, so the scanner is selecting rather than sweeping.
    3. **A mention is counted as a definition.** The names occur in docstrings and inside a
       triple-quoted subprocess script elsewhere in the repo. The scan is AST-based and the count
       it reports is asserted against the module-level `def`s, not against `grep`.
    4. **The copies have already diverged**, in which case this ticket would be a live bug rather
       than a missing constraint. Every normaliser found is run over the same corpus of malformed
       paths and required to AGREE today — which is exactly why nothing has noticed there are
       three of them.
    """
    files = _t321_gate_files()
    assert len(files) >= 3, (
        f"only {len(files)} gate file(s) named {T321_GATE_FILE} were found under {T321_ROOTS}; "
        "the scan has stopped seeing the copies it exists to count"
    )
    assert any(path.name == T321_GATE_FILE and "store-agent" in str(path) for path in files), (
        f"the scan did not find this very file among {[str(p) for p in files]}"
    )

    definitions = _t321_definitions()
    mine = [entry for entry in definitions["path normaliser"] if "store-agent" in entry]
    assert mine == ["packages/store-agent/tests/test_repro_open_tickets.py::normalise"], (
        f"the scanner cannot see the definition it is pointed straight at: {mine}"
    )

    # 2. A role that is not there resolves to nothing.
    assert _t321_definitions().get("raw published operations") is not None
    assert all(isinstance(entries, list) for entries in definitions.values())

    # 4. The copies AGREE today — which is the whole reason nothing has noticed.
    normalisers = _t321_normalisers()
    assert len(normalisers) >= 3, (
        f"only {len(normalisers)} path normaliser(s) could be imported and compared "
        f"({sorted(normalisers)}); the agreement check below is not covering the copies"
    )
    for path in T321_MALFORMED:
        answers = {name: helper(path) for name, helper in normalisers.items()}
        assert len(set(answers.values())) == 1, (
            f"the copies of the path normaliser already DISAGREE on {path!r}: {answers}. That "
            "makes T-321 a live divergence rather than a missing constraint, and this arming "
            "test is the thing that noticed — which is the ticket's point exactly"
        )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-321: the contract/served sweep helpers exist once per package with nothing enforcing "
        "that they agree. They agree today — the arming test measures it — but they have drifted "
        "once already (a regex path-normaliser in this package disagreed with the other two on "
        "`/a/{b/{c}`), and three implementations of one rule is three things to keep in step. "
        "MEASURED: the path normaliser, the published-operations extractor, the served-operations "
        "extractor, the divergence message and the contract probe app are each defined in three "
        "separate gate files, under two different spellings; remove this marker with the fix"
    ),
)
def test_t321_each_contract_sweep_helper_has_exactly_one_implementation() -> None:
    """One rule, one implementation. Three copies is three chances to drift.

    What this asks for is a single definition site the three gate files import from — not that
    the three files be merged, and not any particular home. `proxyshop_support` is already on
    `sys.path` for every one of them and is already imported by at least one; `fixtures` is
    another importable package. Where it goes is the repairing lane's call. What the gate refuses
    is the arrangement where the same rule is written down N times and nothing compares them.

    A role, not a name: this package spells the normaliser `normalise` and the other two spell it
    `_normalise_route`, so a sweep keyed on the literal name would find one definition each and
    conclude nothing is duplicated. That near-miss is recorded here because it is how this gate
    could have been written green.

    MEASURED at HEAD, by AST over every `test_repro_open_tickets.py` in the repo rather than the
    three the ticket names — there are eleven files with that name, and a fourth copy appearing
    tomorrow is the same defect.
    """
    definitions = _t321_definitions()
    duplicated = {role: entries for role, entries in definitions.items() if len(entries) > 1}
    assert not duplicated, (
        f"{len(duplicated)} of {len(T321_ROLES)} contract-sweep helper roles are implemented more "
        "than once, with nothing comparing the copies:\n  "
        + "\n  ".join(
            f"{role}: {len(entries)} definitions — {entries}"
            for role, entries in sorted(duplicated.items())
        )
    )
