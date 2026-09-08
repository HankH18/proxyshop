"""A merchant can state how long its offers stand, in a shape that survives the next auction.

**The defect, measured on the served buyer route.** Every sponsored row on the demo shortlist
was dead 3.2 seconds after it was handed to the shopper::

    gaiaherbs.com           fallback=False   expires in 3.2s
    paradiseherbs.com       fallback=False   expires in 3.2s
    oregonswildharvest.com  fallback=False   expires in 3.2s
    toniiq.com              fallback=False   expires in 3.2s

An owner shows the shortlist, talks about it, clicks a store, and gets a truthful ``409``. The
rows reaching the shopper at all was a separate repair (``exchange.ranking.filters``' expiry
race); this is whether they are worth reaching them.

**Why every offer had a zero-length life.** ``AuctionContext.offer_expires_at`` falls back to
the request's ``respond_by`` when the merchant's context states no expiry, which is deliberate
and well argued — the agent may not invent a window nobody approved. Its docstring has always
pointed at the escape hatch: *"A store that wants its offers to outlive the auction says so,
once, on its context."* That mechanism had no working form. ``offer_expires_at`` on the context
takes an ABSOLUTE INSTANT, so the only thing a merchant can state is one fixed timestamp —
right for one auction and stale by the next — and unsurprisingly not one of the four shipped
demo contexts stated one. All four fell through to the floor.

So the merchant may now state a DURATION instead, which the agent adds to ``respond_by``. That
is not the invented "+48h" the fallback rejects, and the difference is who chose the number: the
agent still invents nothing, it adds a number its merchant wrote down. No ``BidRequest`` change,
no schema edit — the statement lives where every other merchant decision already lives.

**Both directions.** A window a merchant did not state changes nothing; a window they stated
badly is a DECLINE and never a silent fall-through to the floor, because "the offer dies when
the auction closes" is precisely the wrong lifetime to hand someone who asked for a specific
one and mistyped it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from contracts.boundary import parse_timestamp, validate_bid
from store_agent.runtime import bid, is_decline
from store_agent.runtime.context import (
    OFFER_EXPIRES_AT_KEY,
    OFFER_VALID_FOR_SECONDS_KEY,
    AuctionContext,
)
from store_agent.runtime.decline import DeclineReason

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_CONTEXTS = REPO_ROOT / "deploy" / "demo" / "store-contexts"
#: The same approved envelope every other runtime test bids out of. Loaded here rather than
#: imported from ``test_runtime`` because a test module importing another test module makes the
#: two impossible to run apart, and this file is the one somebody will run alone.
ENVELOPE_FIXTURE = REPO_ROOT / "fixtures" / "envelopes" / "store-alpha.approved.json"
STORE_ID = "store-alpha"
CLUSTER = "cluster-warm-layers"
LIST_PRICE = 100.0
RESPOND_BY = "2999-01-01T00:00:00Z"
SNAPSHOT = {STORE_ID: {"store_id": STORE_ID, "score": 0.7, "blacklisted": False}}


def _fixture() -> dict[str, Any]:
    return json.loads(ENVELOPE_FIXTURE.read_text(encoding="utf-8"))


def _context(**overrides: Any) -> dict[str, Any]:
    fixture = _fixture()
    context: dict[str, Any] = {
        "store_id": STORE_ID,
        "envelope": fixture["envelope"],
        "catalog": fixture["catalog"],
        "live_state": {
            "prod-cap": {"in_stock": True, "units_left": 7},
            "prod-floor": {"in_stock": True, "units_left": 3},
        },
        "learned_policy": None,
        "network_priors": {CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }
    context.update(overrides)
    return context


def _request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "auction_id": "auc-0001",
        "intent": {
            "intent_id": "int-0001",
            "cluster_id": CLUSTER,
            "hard_constraints": [],
            "preferences": [],
            "currency": "USD",
            "created_at": "2026-01-01T00:00:00Z",
            "schema_version": "1.0.0",
        },
        "profile": {"pseudonym": "pseu-0001", "buckets": {"budget_band": "50-150"}},
        "respond_by": RESPOND_BY,
    }
    request.update(overrides)
    return request


def _roster() -> dict[str, Any]:
    """What the EXCHANGE lists this store's products at — the fixture's own prices.

    Passed on every ``validate_bid`` call because an ABSENT roster is a refusal, not an
    abstention (T-306/T-307): omitting it would make the expiry assertions below read
    ``price_unreconcilable`` and pass for the wrong reason.
    """
    return {
        ref: {"list_price": LIST_PRICE, "max_discount_pct": 100.0} for ref in _fixture()["catalog"]
    }


def _bid(request: Any = None, context: Any = None) -> Any:
    """A bid that must BE a bid: a decline here is a failure to report, never a silent skip."""
    answer = bid(request or _request(), context if context is not None else _context())
    assert not is_decline(answer), f"expected a bid, got {answer}"
    return answer


#: What the four shipped demo contexts state, and the number this file grades against.
#:
#: **Derived from the exchange's own, and then corrected.** The obvious number is 900 —
#: ``exchange.auction.collect.FALLBACK_OFFER_TTL_SECONDS``, which is
#: ``exchange.auction.state.AUCTION_TTL_SECONDS``, the lifetime the exchange gives the
#: list-price stand-in it mints for a silent store. It was 900 here for exactly that reason, and
#: an adversarial review measured why it is wrong: the auction RECORD and its shortlist live 900
#: s too, anchored at the CLOSE, while an offer's expiry is anchored at ``respond_by``. The
#: fan-out normally reaches the close a second or two BEFORE ``respond_by``, so a 900 s offer
#: outlives the record that explains it, and a shopper clicking late is answered ``404 auction …
#: is not in Redis … its 900s TTL has expired`` rather than the honest ``409
#: DiscountDoesNotApply: the offer's own expiry``. Never a 409, on any run.
#:
#: That matters because the 409 is the entire argument for admitting a merchant's offer to a
#: shortlist: a late click is told the truth about the OFFER. So the window has to end while the
#: record is still readable. 600 s is ten minutes of usable life — long enough to read a
#: shortlist, talk about it, and click — and leaves about five minutes in which a lapsed offer is
#: refused by name.
#:
#: It is stated in the CONTEXTS and not defaulted in the agent. A default would be the agent
#: choosing, which is the thing the fallback's docstring refuses; the number reaching a bid has
#: to be one a merchant wrote.
DEMO_WINDOW_SECONDS = 600

#: The exchange's record TTL, restated so the relation above is machine-checked rather than
#: argued. Restated and not imported: this package must not import ``apps/exchange`` — the
#: hosted image does not contain it — so the number is pinned here and the assertion that it is
#: still 900 lives in the exchange's own tree.
EXCHANGE_RECORD_TTL_SECONDS = 900


def _seconds_of_life(context: dict[str, Any]) -> float:
    """How long past the auction's close an offer built from this context stands."""
    answer = _bid(context=context)
    expires = parse_timestamp(answer.offer.expires_at)
    closes = parse_timestamp(RESPOND_BY)
    assert expires is not None and closes is not None
    return expires.timestamp() - closes.timestamp()


# =====================================================================================
# 1 — the window a merchant states
# =====================================================================================
def test_a_stated_window_gives_the_offer_a_life_past_the_auction() -> None:
    """The repair, in one assertion: 900 seconds of standing instead of zero."""
    context = _context()
    context[OFFER_VALID_FOR_SECONDS_KEY] = DEMO_WINDOW_SECONDS
    assert _seconds_of_life(context) == pytest.approx(DEMO_WINDOW_SECONDS)


def test_without_one_the_conservative_floor_is_exactly_as_it_was() -> None:
    """The control, and the reason this is additive rather than a change of policy.

    Every context written before the duration key existed states none, and every one of them
    gets the same answer it always did: the offer stands for the auction it was solicited for
    and not a second longer. Nothing here relaxes that.
    """
    assert _seconds_of_life(_context()) == 0.0
    assert _bid().offer.expires_at == _request()["respond_by"]


def test_an_absolute_expiry_still_wins_over_a_window() -> None:
    """The window is a SECOND way to say it, not a replacement for the first.

    A merchant who states both has said something more specific with the instant, and the more
    specific statement is the one that binds.
    """
    context = _context()
    context[OFFER_EXPIRES_AT_KEY] = "2030-06-01T00:00:00Z"
    context[OFFER_VALID_FOR_SECONDS_KEY] = DEMO_WINDOW_SECONDS
    assert _bid(context=context).offer.expires_at == "2030-06-01T00:00:00Z"


def test_the_window_is_read_off_the_envelope_when_the_context_states_none() -> None:
    """Same two places the absolute instant is read from, in the same order."""
    context = _context()
    context["envelope"] = dict(context["envelope"], **{OFFER_VALID_FOR_SECONDS_KEY: 120})
    assert _seconds_of_life(context) == pytest.approx(120.0)


@pytest.mark.parametrize("stated", [900, 900.0, "900", " 900 "])
def test_the_number_is_read_the_way_every_other_context_number_is(stated: Any) -> None:
    """Through ``as_number``, so a merchant's JSON string is not a different statement."""
    context = _context()
    context[OFFER_VALID_FOR_SECONDS_KEY] = stated
    assert _seconds_of_life(context) == pytest.approx(900.0)


# =====================================================================================
# 2 — a window stated badly is a decline, never the floor
# =====================================================================================
@pytest.mark.parametrize(
    "stated",
    [
        0,  # "my offers stand for no time" — a statement, and an unusable one
        -1,
        -900,
        "soon",
        "",
        True,  # a bool is not a duration; `as_number` refuses it for the same reason elsewhere
        float("nan"),
        float("inf"),
        float("-inf"),
        1e300,  # so wide the sum leaves the representable calendar
        1e306,  # and wide enough that the SUM overflows before the calendar is reached
        # A 401-digit integer, which `json.loads` produces from a merchant's own context file.
        # `float()` refuses it with `OverflowError` — an `ArithmeticError`, so neither
        # `as_number`'s `except` nor `bid`'s `_UNUSABLE_INPUT` caught it, and it escaped as a
        # raw traceback rather than a decline naming the field. Found by an adversarial review;
        # it was reachable through `intro_discount_pct` long before this key existed.
        10**400,
        # POSITIVE BUT TOO SMALL TO SURVIVE THE WIRE. Instants are stated to the millisecond, so
        # a window under one renders as the deadline itself — the zero-length life this key
        # exists to end, reached SILENTLY from a bid instead of said out loud. The rows below
        # measured `expires_at` exactly equal to `respond_by` before the check existed.
        1e-9,
        0.0001,
        0.0009,
        "0.0005",
        [900],
        {"seconds": 900},
    ],
)
def test_an_unusable_window_declines_rather_than_falling_back_to_the_deadline(
    stated: Any,
) -> None:
    """THE DIRECTION A CARELESS VERSION OF THIS BREAKS.

    Falling through to ``respond_by`` here would turn a merchant's typo into "the offer dies
    when the auction closes" — silently, and with the one lifetime that cannot work. The
    merchant asked for something specific and unusable; the honest answer is to say so. It is
    the identical rule an unreadable absolute ``offer_expires_at`` has always had, and this
    parametrisation is deliberately wider than that one because a duration has more ways of
    being wrong than an instant does: zero and negative are readable numbers and still not
    windows.
    """
    context = _context()
    context[OFFER_VALID_FOR_SECONDS_KEY] = stated
    # `bid` directly, not the `_bid` helper: that helper asserts an answer IS a bid, which is
    # the opposite of what this row is about.
    answer = bid(_request(), context)

    assert is_decline(answer), f"{stated!r} was quietly accepted as a validity window"
    assert answer.reason == DeclineReason.unstatable_offer_expiry


def test_an_unreadable_respond_by_still_declines_even_with_a_good_window() -> None:
    """A duration is only a duration once there is an instant to add it to."""
    request = _request(respond_by="not-an-instant")
    context = _context()
    context[OFFER_VALID_FOR_SECONDS_KEY] = DEMO_WINDOW_SECONDS
    answer = bid(request, context)

    assert is_decline(answer) and answer.reason == DeclineReason.unstatable_offer_expiry


def test_a_json_null_window_is_absent_and_not_a_bad_statement() -> None:
    """``null`` is how JSON says "I am not stating this", and it takes the floor, not a decline.

    Distinguished from ``0``, which IS a statement and declines. The reader uses a private
    sentinel rather than ``or`` for exactly this: ``0 or envelope.get(...)`` would read a
    merchant's zero as silence and hand them the floor instead of the refusal they earned.
    """
    context = _context()
    context[OFFER_VALID_FOR_SECONDS_KEY] = None
    assert _seconds_of_life(context) == 0.0


# =====================================================================================
# 3 — the bid the exchange will actually see
# =====================================================================================
def test_the_offer_is_admissible_at_the_close_and_for_the_whole_stated_window() -> None:
    """Through the shared boundary, at the two instants that decide whether it was worth it.

    ``validate_bid(now=at_close)`` is the assertion that used to fail and was written down as a
    known limitation: at the instant the auction closes, the floor's expiry has already lapsed.
    With a window stated it does not, and it still has not one second before the window ends.
    """
    context = _context()
    context[OFFER_VALID_FOR_SECONDS_KEY] = DEMO_WINDOW_SECONDS
    answer = _bid(context=context)
    at_close = parse_timestamp(RESPOND_BY)
    assert at_close is not None

    for label, when in (
        ("at the close", at_close.timestamp()),
        ("one second before the window ends", at_close.timestamp() + DEMO_WINDOW_SECONDS - 1),
    ):
        checked = validate_bid(
            answer, path="hosted", trust_snapshot=SNAPSHOT, list_prices=_roster(), now=when
        )
        assert checked.ok and list(checked.reasons) == [], f"refused {label}: {checked.reasons}"


def test_and_is_over_once_the_stated_window_really_has_closed() -> None:
    """The other direction at the boundary. A longer life is not an endless one.

    Without this row the change would read as "expiry stopped being checked", which is the
    failure the exchange's own accept door exists to catch and the reason a shopper who clicks
    a genuinely lapsed offer is told so rather than sold it.
    """
    context = _context()
    context[OFFER_VALID_FOR_SECONDS_KEY] = DEMO_WINDOW_SECONDS
    answer = _bid(context=context)
    at_close = parse_timestamp(RESPOND_BY)
    assert at_close is not None

    refused = validate_bid(
        answer,
        path="hosted",
        trust_snapshot=SNAPSHOT,
        list_prices=_roster(),
        now=at_close.timestamp() + DEMO_WINDOW_SECONDS + 1,
    )
    assert list(refused.reasons) == ["offer_expired"]


def test_two_runs_on_the_same_inputs_still_produce_the_same_bytes() -> None:
    """S4. The window is arithmetic on the REQUEST, not a clock read, so replay is unchanged."""
    context = _context()
    context[OFFER_VALID_FOR_SECONDS_KEY] = DEMO_WINDOW_SECONDS
    first = _bid(context=context).offer.expires_at
    second = _bid(context=context).offer.expires_at
    assert first == second


# =====================================================================================
# 4 — the deployment, because a key nothing sets is a key that does nothing
# =====================================================================================
def test_every_shipped_demo_context_states_a_window_a_person_can_act_within() -> None:
    """The tracked deployment states it. See the test below for why that is only half a check."""
    contexts = sorted(STORE_CONTEXTS.glob("*.json"))
    assert len(contexts) == 4, [p.name for p in contexts]
    for path in contexts:
        stated = json.loads(path.read_text()).get(OFFER_VALID_FOR_SECONDS_KEY)
        assert stated == DEMO_WINDOW_SECONDS, f"{path.name} states {stated!r}"


def test_the_generator_that_writes_those_contexts_writes_the_window_too() -> None:
    """THE producer check, and the one the test above only looks like.

    The four contexts are GENERATED — ``packages/store-agent/compose.yaml`` says so, and
    ``docs/demo/shopper-demo.md`` puts ``scripts/build_demo_deployment.py`` in the runbook. The
    first version of this change edited the tracked JSON and not the generator, so
    ``build_demo_deployment.py --check`` was RED and a documented regenerate silently deleted
    all four windows, restoring the exact defect. The test above stayed green throughout,
    because it reads the file rather than the thing that writes it — while its own docstring
    said "grepping for what SETS the value, never for what reads it". An adversarial review
    found it; this is the assertion that would have.
    """
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        from build_demo_deployment import build  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    generated = {
        path: doc for path, doc in build().items() if "store-contexts/" in path.replace("\\", "/")
    }
    assert len(generated) == 4, sorted(generated)
    for path, doc in generated.items():
        assert doc.get(OFFER_VALID_FOR_SECONDS_KEY) == DEMO_WINDOW_SECONDS, (
            f"regenerating {path} would drop the window"
        )


def test_the_window_ends_before_the_record_that_would_explain_it_is_collected() -> None:
    """The relation that makes an expired offer refusable BY NAME, asserted rather than assumed.

    The exchange keeps an auction and its shortlist for 900 s from the close; the offer's expiry
    runs from ``respond_by``, which the fan-out normally reaches a moment after. If the window
    were the same 900 s the offer would outlive the record and a late click would be answered
    "no such auction" instead of "that offer has expired" — measured live, never a 409. The gap
    is what keeps the honest refusal reachable.
    """
    assert DEMO_WINDOW_SECONDS < EXCHANGE_RECORD_TTL_SECONDS, (
        "a demo offer outlives the auction record, so a late click cannot be refused by name"
    )
    assert EXCHANGE_RECORD_TTL_SECONDS - DEMO_WINDOW_SECONDS >= 60, (
        "the band in which a lapsed offer is refused by name is too narrow to be reachable"
    )


def test_a_demo_context_produces_an_offer_that_outlives_its_auction() -> None:
    """Driven through the real assembly, so the JSON is graded and not just inspected."""
    context = json.loads((STORE_CONTEXTS / "gaiaherbs.com.json").read_text())
    built = AuctionContext(
        auction_id="a",
        store_id=context["store_id"],
        envelope=context["envelope"],
        catalog={},
        live_state={},
        learned_policy=None,
        network_priors={},
        intent={},
        profile={},
        respond_by="2026-09-08T15:00:00.000Z",
        store_currency="USD",
        stated_offer_expiry=None,
        stated_offer_window=context[OFFER_VALID_FOR_SECONDS_KEY],
    )
    assert built.offer_expires_at == "2026-09-08T15:10:00.000Z"


# =====================================================================================
# 5 — the hand-rolled renderer, graded against the standard library
# =====================================================================================
def test_the_instant_renderer_agrees_with_the_standard_library_everywhere_it_matters() -> None:
    """``_instant`` is civil-from-days by hand, so it is graded against ``datetime`` here.

    The runtime may not IMPORT ``datetime`` — ``test_runtime``'s determinism gate refuses it on
    the whole bid path, deliberately, because it reads the source and cannot tell a pure
    conversion from a clock read. A test is not on the bid path, so this is where the two are
    compared, and comparing them is the price of hand-rolling: an arithmetic renderer nobody
    checked against a real calendar is a leap-year bug waiting for February.

    The sample is deliberately nasty — leap days, century and 400-year boundaries, the epoch
    itself, pre-epoch instants, second and millisecond boundaries, and the far future the
    fixtures actually use.
    """
    from datetime import UTC, datetime

    from store_agent.runtime.context import _instant

    epochs = [
        0.0,  # 1970-01-01
        -1.0,  # the day before, which the negative branch of the era shift must get right
        -86_400.5,
        951_782_400.0,  # 2000-02-29, a leap day in a 400-year leap century
        4_107_542_400.0,  # 2100-03-01, the day after a century that is NOT a leap year
        1_788_876_293.595,  # the live reproduction's own deadline
        253_370_764_800.0,  # 9999-01-01, the last year RFC-3339 can spell
        1_700_000_000.999,
        1_700_000_000.001,
    ]
    for epoch in epochs:
        expected = (
            datetime.fromtimestamp(epoch, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        assert _instant(epoch) == expected, f"epoch {epoch!r}"


def test_the_renderer_agrees_with_the_standard_library_on_random_instants() -> None:
    """A seeded RANDOM sweep, and the only thing that found the bug this function shipped with.

    The two sweeps below walk exact day boundaries, where the fractional second is always zero —
    and the defect lived entirely in the fraction. ``_instant`` computed
    ``int(epoch * 1000.0 // 1.0)``, and at large epochs that product is itself rounded to the
    nearest double, which can cross a millisecond boundary the true instant never reached.
    Measured over this sample: **2,349 disagreements in 300,000**, every one an off-by-one
    millisecond, the first at ``176048524741.948`` (year 7548) rendering ``.948`` where the
    calendar says ``.947``.

    Nothing on the bid path could reach it — a parsed ``respond_by`` is a millisecond multiple
    around 1.8e9, where the arithmetic is exact — which is precisely why only a random sweep
    across the whole spellable calendar caught it, and why the chosen samples elsewhere in this
    file all passed. Seeded, so a failure is reproducible rather than a rumour.

    10,000 rather than 300,000 so the suite stays fast; the seed makes the sample fixed, and the
    defect it found reproduces at this size.
    """
    import random
    from datetime import UTC, datetime

    from store_agent.runtime.context import (
        _EPOCH_AT_YEAR_1,
        _EPOCH_AT_YEAR_10000,
        _instant,
    )

    rng = random.Random(20260908)
    for _ in range(10_000):
        epoch = rng.uniform(_EPOCH_AT_YEAR_1, _EPOCH_AT_YEAR_10000 - 1)
        expected = (
            datetime.fromtimestamp(epoch, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        assert _instant(epoch) == expected, f"epoch {epoch!r}"


def test_the_renderer_agrees_with_the_standard_library_across_four_centuries() -> None:
    """A sweep, because the sample above is chosen and a chosen sample proves what was chosen.

    One reading a day for 400 years walks every leap rule the Gregorian calendar has, including
    the 100-year exception and the 400-year exception to the exception.
    """
    from datetime import UTC, datetime

    from store_agent.runtime.context import _instant

    day = 86_400.0
    start = 946_684_800.0  # 2000-01-01
    for step in range(0, 146_097):  # one full 400-year era, in days
        epoch = start + step * day
        expected = (
            datetime.fromtimestamp(epoch, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        assert _instant(epoch) == expected, f"day {step}"


@pytest.mark.parametrize("epoch", [1e18, -1e18, 1e300, 1e306, -1e306, float("inf"), float("-inf")])
def test_an_instant_outside_the_spellable_calendar_is_refused_rather_than_clamped(
    epoch: float,
) -> None:
    """A window so wide it names no instant is refused, like every other unusable window.

    ``1e306`` is the row an adversarial review added and it is not a curiosity: it is finite and
    positive, so ``as_number`` reads it and the sum with the deadline is still finite — and then
    ``epoch * 1000.0`` overflows to ``inf``, whose floor is ``nan``, which ``int()`` refuses.
    Measured before the epoch bounds were checked first, on a real bid with a real context::

        decline(unusable_store_context): ValueError: cannot convert float NaN to integer

    A decline only because an outer handler swallowed it, and the wrong one: it condemned the
    whole store context and put a Python exception string where a merchant-facing reason goes.
    """
    from store_agent.runtime.context import _instant

    assert _instant(epoch) is None


def test_the_epoch_bounds_really_are_the_calendar_they_claim_to_be() -> None:
    """Two hardcoded epochs, graded against ``datetime`` — otherwise they are wrong by a day.

    A constant nobody checked is the cheapest kind of prose-that-outlived-its-subject, and
    these two decide whether an instant is renderable at all.
    """
    from datetime import UTC, datetime

    from store_agent.runtime.context import _EPOCH_AT_YEAR_1, _EPOCH_AT_YEAR_10000

    assert (
        datetime.fromtimestamp(_EPOCH_AT_YEAR_1, tz=UTC).isoformat() == "0001-01-01T00:00:00+00:00"
    )
    # Year 10000 is past what `datetime` can hold, so the bound is checked one second below it.
    assert (
        datetime.fromtimestamp(_EPOCH_AT_YEAR_10000 - 1, tz=UTC).isoformat()
        == "9999-12-31T23:59:59+00:00"
    )
