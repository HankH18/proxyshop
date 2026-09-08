"""The prose must not push the bid past the deadline: the exchange's `respond_by` IS the store's.

WHAT IS AND IS NOT CLAIMED BY THAT SENTENCE
---------------------------------------------
This bid path is synchronous end to end. The offer is computed first and the request then WAITS
on the copywriter for as long as the budget allows — default-window auctions measure 2.9–3.7 s
wall for exactly that reason, and nothing here runs the two concurrently. What the budget buys
is narrower and is the whole repair: the offer is finished in ~12.7 ms and is never *lost* to a
slow copywriter, it is merely held until the budget expires and then shipped with the
deterministic fallback pitch — instead of shipping late to an exchange that stopped listening.

WHAT WAS MEASURED, AND WHY NONE OF IT WAS VISIBLE
--------------------------------------------------
Against the live docker stack with a real key and ``LLM_PROVIDER=anthropic``, in two different
populations. They are quoted separately because each answers a question the other cannot, and
reading one as a bound on the other is what makes them look contradictory:

* **In process, one store (gaiaherbs), driven through ``AgentRunner.run``.** The OFFER —
  `unit_price`, `discount`, `total_price`, `commitments`, `expiry` — is computed
  deterministically with **no model call**: 12.7 ms median (n=20, ``llm=None``). The PITCH is
  one synchronous ``llm.complete(...)``: **3.35–5.04 s** (n=5). Those two are the same
  population measured with and without the copywriter, which is why **99.67%** is computed from
  them and from nothing else: ``(3.79 − 0.0127) / 3.79``.
* **Over the wire, all four hosted containers.** ``POST /v1/bid-requests`` end to end:
  **1.97–4.73 s** (n=24) — gaiaherbs 3.12–4.73, toniiq 2.05–2.76, paradiseherbs 2.16–2.51,
  oregonswildharvest 1.97–2.39. The floor is below the in-process floor because the in-process
  range is gaiaherbs alone and gaiaherbs is the slow store; the other three pull it down.
* And the exchange abandons the bid at a hard deadline. Against a 3 s window every hosted store
  missed on either measurement, and a market of four bidding agents silently became 100%
  list-price fallback (R10) — the branch R10 exists to cover for a store that is **not
  answering at all**.

Every green test in this repository stayed green through all of it, and that is the interesting
part rather than an aside. The bid path had no notion of elapsed time, the copywriter was bounded
by a constant nobody had reconciled with the auction window, and the one operator log line
carried no timing whatsoever — so "the store answered" and "the store answered in time" were the
same observation, and only one of them was true. This file grades the second one.

A SECOND DEFECT, INDEPENDENT AND WORSE
---------------------------------------
``packages/llm`` built ``anthropic.Anthropic(api_key=..., timeout=...)`` and never set
``max_retries``. The pinned SDK is ``anthropic==1.2.0``, whose ``DEFAULT_MAX_RETRIES`` is 2, and
which **retries** ``APITimeoutError``. Measured directly against the pinned SDK with a per-call
timeout of 0.8s::

    max_retries=2: APITimeoutError after 3.92s
    max_retries=0: APITimeoutError after 0.84s

So the documented "5-second pitch budget" was really a fifteen-second-plus budget, and no
per-call timeout on this path meant anything until the retries were pinned.
``test_the_live_pitch_client_pins_the_sdks_retries_to_zero`` is the one test here that grades the
mechanism rather than the arithmetic: without it every other assertion in this file passes
against a client that would still take three times its budget in production.

WHAT IS *NOT* CLAIMED
----------------------
Nothing here interrupts the bid path — there is no watchdog thread, and adding one is not the
design. The budget is handed to the provider as its own per-request timeout, which is where the
latency actually is, and the stand-in copywriters below honour it exactly as the SDK does. A
double that merely slept and ignored its ``timeout`` would be modelling a provider this code has
never claimed to be able to bound.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from llm.client import build_llm
from llm.config import ROLE_BUYER, ROLE_STORE_AGENT
from store_agent.main import create_app
from store_agent.solicitation import (
    DECLINE_REASON_HEADER,
    PITCH_MAX_RETRIES,
    PITCH_OUTCOMES,
    PITCH_RESERVE_ENV,
    PITCH_RESERVE_SECONDS,
    PITCH_TIMEOUT_ENV,
    PITCH_TIMEOUT_SECONDS,
    PitchAttempt,
    PitchBudgetExhausted,
    PitchClient,
    advocate,
    configure_advocate,
    configure_solicitation,
    is_deadline_miss,
    pitch_budget_seconds,
    pitch_client,
    resolve_pitch_reserve,
)
from store_agent.trust_intake import configure_trust_intake

#: The advocate MODULE, fetched by name. ``import store_agent.solicitation.advocate as m`` binds
#: the package attribute ``advocate``, which ``solicitation/__init__.py`` has already rebound to
#: the *function* of that name — so the plain import hands back a function with no
#: ``pitch_client`` on it. ``import_module`` returns the module from ``sys.modules``.
advocate_module = import_module("store_agent.solicitation.advocate")

REPO_ROOT = Path(__file__).resolve().parents[3]
ENVELOPE_FIXTURE = REPO_ROOT / "fixtures" / "envelopes" / "store-alpha.approved.json"

STORE_ID = "store-alpha"
CLUSTER = "cluster-warm-layers"
STORE_DOMAIN = "store-alpha.example.com"

#: The route's own logger, so a WARNING assertion cannot be satisfied by the copywriter's line.
ROUTE_LOGGER = "store_agent.solicitation.routes"

#: The advocate's own logger, for the one line a composition root gets when its runner's
#: copywriter cannot be budgeted. Named separately for the same reason as above.
ADVOCATE_LOGGER = "store_agent.solicitation.advocate"

#: A reply distinctive enough that finding it in a served `Bid.message` is proof the MODEL wrote
#: that message, and not finding it is proof the deterministic fallback did. It is deliberately
#: ordinary prose about facts the fixture catalogue supports, because
#: `store_agent.runtime.pitch.screen` refuses superlatives, money and anything unsupported — a
#: marker string would be refused and every "the model wrote this" case would be vacuous.
MODEL_REPLY = "Merino wool, and it leaves us within two business days."

#: A fixed instant for the arithmetic tests, so nothing here reads a clock to check a clock.
NOW = 1_800_000_000.0


def at(offset: float, *, base: float = NOW) -> str:
    """``base + offset`` seconds as the RFC-3339 string the exchange puts on `respond_by`."""
    return datetime.fromtimestamp(base + offset, tz=UTC).isoformat().replace("+00:00", "Z")


def soon(seconds: float) -> str:
    """``seconds`` from now, for the tests that go through the real door against a real clock."""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def context(*, activation: str = "active", **overrides: Any) -> dict[str, Any]:
    """A store context in the shape the merchant service hands over, for an ACTIVATED store.

    `activation` is overlaid on the approved artifact rather than written into it, exactly as
    ``test_solicitation.py`` and ``test_pitch.py`` do: the shipped fixture states `shadow`, which
    is what makes it the honest source for R7's un-activated case elsewhere.
    """
    fixture = json.loads(ENVELOPE_FIXTURE.read_text(encoding="utf-8"))
    built: dict[str, Any] = {
        "store_id": STORE_ID,
        "envelope": {**fixture["envelope"], "activation": activation},
        "catalog": fixture["catalog"],
        "live_state": {
            "prod-cap": {"in_stock": True, "units_left": 7},
            "prod-floor": {"in_stock": True, "units_left": 3},
        },
        "learned_policy": None,
        "network_priors": {CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1]}},
        "store_domain": STORE_DOMAIN,
    }
    built.update(overrides)
    return built


def request_body(respond_by: str, *, auction_id: str = "auc-budget") -> dict[str, Any]:
    """A `BidRequest` that lands on the fixture's merino product, with a stated deadline.

    The auction id is fixed on purpose. ``AgentRunner._select_arm`` seeds its draw with
    ``(state, cluster, auction)`` and nothing else, so two freshly built apps answering this same
    body play the same arm — which is what lets the comparisons below attribute a difference to
    the copywriter rather than to the learning loop.
    """
    return {
        "auction_id": auction_id,
        "intent": {
            "intent_id": "int-budget",
            "cluster_id": CLUSTER,
            "query": "a warm merino mid-layer for cold bike commutes",
            "category": "outerwear",
            "hard_constraints": [{"field": "material", "op": "eq", "value": "merino wool"}],
            "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
            "ship_to": "US-CA",
            "currency": "USD",
            "budget_band": "50-150",
            "created_at": "2026-01-01T00:00:00Z",
            "schema_version": "1.0.0",
        },
        "profile": {
            "pseudonym": "psn-0001",
            "buckets": {
                "budget_band": "50-150",
                "category_affinity": ["outerwear"],
                "frequency_tier": "occasional",
                "region": "US-CA",
                "first_time": False,
            },
        },
        "respond_by": respond_by,
    }


# =============================================================================================
# The stand-in copywriters. Each one models a real provider behaviour and nothing more.
# =============================================================================================


class Recording:
    """Answers instantly and remembers the ``timeout`` it was handed on every call.

    The ``timeout`` list is the point: it is how "the store passed its remaining time down to
    the provider" is graded, and an empty list is how "the model was never called" is graded.
    """

    def __init__(self, reply: str = MODEL_REPLY) -> None:
        self.reply = reply
        self.timeouts: list[Any] = []

    def complete(self, prompt: Any, *, timeout: Any = None, **_kwargs: Any) -> str:
        del prompt
        self.timeouts.append(timeout)
        return self.reply


class Slow:
    """A provider that takes ``latency`` seconds AND honours its per-request timeout.

    Both halves are load-bearing. Sleeping models the measured in-process pitch (3.35–5.04 s,
    n=5); raising at ``timeout`` models what ``anthropic`` actually does, which is the ONLY
    mechanism bounding this path — nothing in the store interrupts anything. A double that slept
    past its timeout and returned anyway would be asserting a watchdog this design deliberately
    does not have.

    It raises the BUILTIN `TimeoutError`, and that is what `is_deadline_miss` classifies as a
    missed deadline offline. The SDK's own `anthropic.APITimeoutError` is not a subclass of it
    and is recognised separately — see the classification tests below, which install a stand-in
    ``anthropic`` module rather than requiring the SDK to be installed at all (D20).
    """

    def __init__(self, latency: float = 3.0, reply: str = MODEL_REPLY) -> None:
        self.latency = latency
        self.reply = reply
        self.timeouts: list[Any] = []

    def complete(self, prompt: Any, *, timeout: Any = None, **_kwargs: Any) -> str:
        del prompt
        self.timeouts.append(timeout)
        if timeout is None:
            time.sleep(self.latency)
            return self.reply
        time.sleep(min(self.latency, float(timeout)))
        if self.latency > float(timeout):
            raise TimeoutError("the provider gave up at its per-request timeout")
        return self.reply


class Forbidden:
    """A copywriter whose being called at all is the failure. Used for the zero-budget cases.

    It COUNTS as well as raising, and both halves are needed. Raising makes the failure loud
    where the exception can escape; counting makes it visible through the served door, where it
    cannot — `compose_pitch` catches every `Exception` on purpose (rule D), so a call that
    reached the model and blew up here would still serve a perfectly good bid with the fallback
    pitch, and the test would pass while the store burned its whole budget on a closed auction.
    ``calls == 0`` is the assertion that cannot be satisfied that way.
    """

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: Any, **_kwargs: Any) -> str:
        del prompt
        self.calls += 1
        raise AssertionError(
            "the model was called for a solicitation whose deadline had already passed"
        )


def serve(
    body: dict[str, Any],
    inner: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ctx: dict[str, Any] | None = None,
) -> tuple[Any, float, Any]:
    """One solicitation through the REAL door, on a fresh app whose copywriter is ``inner``.

    ``pitch_client`` is patched where ``advocate._advocate_for`` looks it up, so everything else
    on the path is the shipped wiring: the runner is built the same way, the client is put on the
    `Advocate` the same way, and the route arms it from this request's own `respond_by`.

    Returns the response, the wall seconds the whole HTTP call took, and the app.
    """
    client = None if inner is None else PitchClient(inner)
    # The MODULE, by object rather than by dotted string: `store_agent.solicitation.advocate`
    # resolves to the re-exported `advocate` FUNCTION on the package, not to the module, and
    # monkeypatch would set the attribute on that function instead.
    monkeypatch.setattr(advocate_module, "pitch_client", lambda *_a, **_k: client)
    app = create_app()
    configure_solicitation(app, context=context() if ctx is None else ctx)
    with TestClient(app) as http:
        started = time.monotonic()
        response = http.post("/v1/bid-requests", json=body)
        elapsed = time.monotonic() - started
    return response, elapsed, app


# =============================================================================================
# 1. The arithmetic: min(ceiling, respond_by - now - reserve), floored at zero
# =============================================================================================


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        pytest.param(60.0, 5.0, id="deadline-far-away-clamped-to-the-ceiling"),
        pytest.param(5.35, 5.0, id="deadline-exactly-at-the-ceiling-plus-reserve"),
        pytest.param(3.0, 2.65, id="a-three-second-auction-window"),
        pytest.param(1.0, 0.65, id="one-second-left"),
        pytest.param(0.35, 0.0, id="the-whole-remainder-is-the-reserve"),
        pytest.param(0.2, 0.0, id="less-left-than-the-reserve"),
        pytest.param(0.0, 0.0, id="the-deadline-is-now"),
        pytest.param(-30.0, 0.0, id="the-auction-already-closed"),
    ],
)
def test_the_budget_is_the_exchanges_deadline_minus_the_reserve_under_the_ceiling(
    offset: float, expected: float
) -> None:
    """The whole formula, at the defaults, including both directions it is clamped in.

    The negative case is the one that was costing the most: a solicitation whose `respond_by` has
    passed used to spend a full five seconds writing prose for an auction the exchange had
    already abandoned, on a threadpool a live process answers every auction with.
    """
    budget = pitch_budget_seconds(
        at(offset), now=NOW, ceiling=PITCH_TIMEOUT_SECONDS, reserve=PITCH_RESERVE_SECONDS
    )
    assert budget == pytest.approx(expected, abs=1e-6)
    assert budget >= 0.0, "a negative budget would be handed to the SDK as a timeout"


def test_the_ceiling_and_the_reserve_are_both_honoured_as_given() -> None:
    """Neither is hard-coded: a deployment moves them, and the arithmetic follows."""
    assert pitch_budget_seconds(at(10.0), now=NOW, ceiling=2.0, reserve=0.1) == 2.0
    assert pitch_budget_seconds(at(1.5), now=NOW, ceiling=2.0, reserve=0.1) == pytest.approx(1.4)
    assert pitch_budget_seconds(at(1.5), now=NOW, ceiling=2.0, reserve=1.6) == 0.0


@pytest.mark.parametrize(
    "respond_by",
    [
        "",
        "   ",
        "soon",
        "2026-13-45T99:99:99Z",
        "tomorrow please",
        None,
        {"at": "2026-01-01T00:00:00Z"},
        [],
        True,
    ],
)
def test_a_missing_or_unreadable_deadline_leaves_the_store_its_configured_ceiling(
    respond_by: Any,
) -> None:
    """A store told no deadline keeps exactly the budget it had before any of this existed.

    Falling back rather than refusing, for the same reason `compose_pitch` cannot raise: an
    exchange that sends a `respond_by` this store cannot parse is a bug on the other side of the
    wire, and answering it with no pitch — or with an exception — would be this store punishing
    itself for someone else's field. `True` is in the list because `parse_timestamp` treats a
    bool as unreadable rather than as the epoch second `1`, and this must agree with it.
    """
    budget = pitch_budget_seconds(respond_by, now=NOW, ceiling=4.0, reserve=0.25)
    assert budget == 4.0


def test_the_budget_reads_the_deadline_with_the_boundarys_own_parser() -> None:
    """The SAME `parse_timestamp` the offer expiry uses, so the two cannot read it differently.

    `runtime/context.py` already parses this field to decide the offer's expiry. A second,
    private parser here would let a store take a deadline as readable for one purpose and
    unreadable for the other — offering something that expires when the auction closes while
    budgeting as though it had been told nothing.
    """
    naive = datetime.fromtimestamp(NOW + 2.0, tz=UTC).replace(tzinfo=None).isoformat()
    assert pitch_budget_seconds(naive, now=NOW, ceiling=5.0, reserve=0.35) == pytest.approx(1.65)
    assert pitch_budget_seconds(NOW + 2.0, now=NOW, ceiling=5.0, reserve=0.35) == pytest.approx(
        1.65
    )
    offset = "2026-01-01T00:00:00+02:00"
    utc_same = "2025-12-31T22:00:00Z"
    assert pitch_budget_seconds(offset, now=NOW, ceiling=9.0, reserve=0.0) == pitch_budget_seconds(
        utc_same, now=NOW, ceiling=9.0, reserve=0.0
    )


def test_the_reserve_resolves_like_the_timeout_does_and_says_so_when_it_cannot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """0.35s by default; malformed or non-positive falls back, loudly, and never raises."""
    assert PITCH_RESERVE_SECONDS == 0.35
    assert PITCH_RESERVE_ENV == "STORE_AGENT_PITCH_RESERVE_SECONDS"
    assert resolve_pitch_reserve({}) == 0.35
    assert resolve_pitch_reserve({PITCH_RESERVE_ENV: "1.25"}) == 1.25
    assert resolve_pitch_reserve({PITCH_RESERVE_ENV: "  0.5  "}) == 0.5

    for junk in ("", "   ", "later", "0", "-1"):
        with caplog.at_level(logging.WARNING):
            assert resolve_pitch_reserve({PITCH_RESERVE_ENV: junk}) == PITCH_RESERVE_SECONDS
    warned = [r for r in caplog.records if PITCH_RESERVE_ENV in r.getMessage()]
    assert len(warned) == 3, (
        "an unset reserve is not a misconfiguration and must not warn; a non-number and a "
        f"non-positive one are, and must — got {[r.getMessage() for r in warned]}"
    )


def test_the_budget_defaults_come_from_the_two_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``ceiling``/``reserve`` given, the deployment's own configuration decides.

    This is the pairing that was missing end to end: ``STORE_AGENT_PITCH_TIMEOUT_SECONDS`` was
    read by the code and set by no compose file and no ``.env.example``, so the only way to move
    the number was to edit the source.
    """
    monkeypatch.delenv(PITCH_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(PITCH_RESERVE_ENV, raising=False)
    assert pitch_budget_seconds("", now=NOW) == PITCH_TIMEOUT_SECONDS
    assert pitch_budget_seconds(at(1.0), now=NOW) == pytest.approx(0.65)

    monkeypatch.setenv(PITCH_TIMEOUT_ENV, "2.0")
    monkeypatch.setenv(PITCH_RESERVE_ENV, "0.5")
    assert pitch_budget_seconds("", now=NOW) == 2.0
    assert pitch_budget_seconds(at(1.0), now=NOW) == pytest.approx(0.5)
    assert pitch_budget_seconds(at(30.0), now=NOW) == 2.0


# =============================================================================================
# 2. A budget of zero means the model is not called AT ALL
# =============================================================================================


def test_an_expired_deadline_calls_no_model_at_all() -> None:
    """The client refuses before any I/O, and the copywriter that would have run is untouched.

    `Forbidden.complete` raises `AssertionError` if it is reached, which is a failure this test
    can see; a sleeping double would only make a broken build slow.
    """
    client = PitchClient(Forbidden())
    client.arm(0.0)
    with pytest.raises(PitchBudgetExhausted):
        client.complete("a prompt")
    attempt = client.disarm()
    assert attempt.outcome == "skipped"
    assert attempt.budget == 0.0
    assert attempt.elapsed == 0.0
    assert attempt.missed is True


def test_the_zero_budget_refusal_is_what_compose_pitch_already_knows_how_to_absorb() -> None:
    """`PitchBudgetExhausted` is an ordinary `Exception`, so rule D's existing catch covers it.

    Stated as a test rather than as a comment because the whole design rests on it: the new
    failure mode reaches `compose_pitch` through the same `except Exception` that a provider
    timeout does, and a class that had escaped it — a `BaseException`, say — would turn a
    late auction into a 500 instead of into a fallback pitch.
    """
    assert issubclass(PitchBudgetExhausted, Exception)
    recorder = Recording()
    client = PitchClient(recorder)
    client.arm(-2.5)
    with pytest.raises(PitchBudgetExhausted):
        client.complete("a prompt")
    assert recorder.timeouts == [], "a negative budget must not reach the provider as a timeout"


def test_a_solicitation_whose_auction_already_closed_still_serves_a_real_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real door: the deadline has passed, no model runs, and a bid still ships.

    This is the case the store used to handle worst. It computed an offer in ~13 ms, then spent
    five seconds writing a pitch for an auction the exchange had abandoned — and served a 200
    anyway. Now it answers immediately with the deterministic fallback pitch, and the bid it
    serves is the SAME bid, offer for offer, as one written with no copywriter at all.
    """
    # ONE body for both solicitations. `respond_by` is also the offer's expiry fallback, so two
    # bodies built a millisecond apart produce two different `expires_at` values and the offers
    # would differ for a reason that has nothing to do with the copywriter.
    body = request_body(soon(-30.0))
    forbidden = Forbidden()
    late, elapsed, _ = serve(body, forbidden, monkeypatch)
    assert late.status_code == 200, late.headers.get(DECLINE_REASON_HEADER)
    assert forbidden.calls == 0, (
        "the model was called for an auction the exchange had already abandoned; the bid still "
        "looks fine because rule D absorbs the failure, which is exactly why this is asserted "
        "on the copywriter and not on the response"
    )
    served = late.json()

    offer = served["offer"]
    assert offer["unit_price"] > 0.0
    assert offer["checkout_url"] and STORE_DOMAIN in offer["checkout_url"]
    assert offer["expires_at"]
    assert served["message"], "a store with no time for prose still owes the shopper a pitch"

    reference, _, _ = serve(body, None, monkeypatch)
    assert reference.json()["offer"] == offer
    assert reference.json()["message"] == served["message"], (
        "with no time to write, the served pitch must be exactly the deterministic fallback"
    )
    assert elapsed < 2.0, f"the bid took {elapsed:.2f}s for an auction that had already closed"


# =============================================================================================
# 3. A copywriter slower than the deadline costs the store its prose, never its bid
# =============================================================================================


def test_a_copywriter_slower_than_the_deadline_still_ships_a_bid_on_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured defect, inverted into a property.

    A copywriter with the measured live latency (3 s) against a 1-second auction window: the
    store must answer inside the window with a real, discounted offer and the deterministic
    fallback pitch, rather than answering after the exchange has stopped listening.

    The offer is compared field for field against the same auction answered with NO copywriter,
    which is the only way to say "a real offer, not a degraded one" without hard-coding a price
    that the envelope, the catalogue or the learning loop could legitimately move.
    """
    slow = Slow(latency=3.0)
    body = request_body(soon(1.0))  # one body for both runs; see the comment above
    response, elapsed, _ = serve(body, slow, monkeypatch)
    assert response.status_code == 200, response.headers.get(DECLINE_REASON_HEADER)
    served = response.json()

    assert slow.timeouts and slow.timeouts[0] is not None, (
        "the store must hand the provider its remaining time; without a per-request timeout "
        "the provider's own default is the only bound and it is far past respond_by"
    )
    assert 0.0 < float(slow.timeouts[0]) <= 0.7, (
        f"a 1s auction window less the 0.35s reserve is ~0.65s, got {slow.timeouts[0]}"
    )
    assert elapsed < 2.0, (
        f"the whole solicitation took {elapsed:.2f}s against a 1s window and a 3s copywriter; "
        f"the bid waited on the prose"
    )

    reference, _, _ = serve(body, None, monkeypatch)
    assert served["offer"] == reference.json()["offer"]
    assert served["offer"]["unit_price"] > 0.0
    assert served["message"] == reference.json()["message"]
    assert MODEL_REPLY not in (served["message"] or "")


def test_a_copywriter_inside_the_budget_still_writes_the_bids_pitch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control. If the budget refused everything, every assertion above would be vacuous.

    Same door, same store, same auction — a copywriter that answers in time, and the served
    `Bid.message` is the model's sentence rather than the deterministic fallback. D55 is the
    whole product here: the pitch is what an in-network shop is buying.
    """
    quick = Recording()
    body = request_body(soon(4.0))  # one body for both runs; see the comment above
    response, _, _ = serve(body, quick, monkeypatch)
    assert response.status_code == 200
    served = response.json()
    assert quick.timeouts, "the client was armed, so a timeout must have been passed down"
    assert served["message"] == MODEL_REPLY

    reference, _, _ = serve(body, None, monkeypatch)
    assert served["offer"] == reference.json()["offer"], (
        "the offer is model-independent by construction; a difference here means the copywriter "
        "reached the price, which is what rule A forbids"
    )
    assert served["message"] != reference.json()["message"]


def test_the_bid_still_ships_when_respond_by_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exchange that sends a deadline this store cannot parse gets a normal, pitched bid.

    ``respond_by`` is required by the contract with ``min_length=1``, so "missing" reaches this
    door as garbage rather than as an absent key — an empty string is a 422 from the schema and
    never gets here. The store falls back to its configured ceiling and behaves exactly as it did
    before budgets existed: the model is called, and its prose is served.

    The context states its own ``offer_expires_at``, and that is not scenery. ``respond_by`` has
    a SECOND, pre-existing job — it is the fallback the offer's ``expires_at`` falls back to —
    so a store handed an unreadable one and nothing else declines ``unstatable_offer_expiry``
    before the copywriter is ever reached. That decline is correct and unrelated to the budget
    (an offer nobody can price the risk of is not an offer), but it would hide the property
    under test, so the merchant states an expiry of its own here. The second half of this test
    pins that behaviour down rather than leaving it as an assumption.
    """
    quick = Recording()
    expiring = context(offer_expires_at="2099-01-01T00:00:00Z")
    response, _, _ = serve(request_body("not-a-timestamp"), quick, monkeypatch, ctx=expiring)
    assert response.status_code == 200, response.headers.get(DECLINE_REASON_HEADER)
    assert response.json()["message"] == MODEL_REPLY
    assert quick.timeouts == [pytest.approx(PITCH_TIMEOUT_SECONDS)], (
        "an unreadable deadline must leave the ceiling in place, not zero the budget"
    )
    assert response.json()["offer"]["unit_price"] > 0.0

    # And the pre-existing rule, stated so a future reader does not mistake it for the budget's
    # doing: with no stated expiry either, the same body is declined by the OFFER, not by the
    # pitch — and the copywriter was still given its full ceiling before that decline.
    also_quick = Recording()
    bare, _, _ = serve(request_body("not-a-timestamp"), also_quick, monkeypatch)
    assert bare.status_code == 204
    assert bare.headers[DECLINE_REASON_HEADER] == "unstatable_offer_expiry"


def test_an_unarmed_client_behaves_exactly_as_it_did_before_budgets_existed() -> None:
    """Every in-process caller and every offline test takes this path; none of them may change.

    Unarmed means: no ``timeout`` injected, nothing refused, and an all-``None`` attempt record.
    """
    recorder = Recording()
    client = PitchClient(recorder)
    assert client.budget is None
    assert client.complete("a prompt") == MODEL_REPLY
    assert recorder.timeouts == [None]
    assert client.disarm() == PitchAttempt(budget=None, elapsed=None, outcome="not_attempted")


def test_a_caller_that_names_its_own_timeout_keeps_it() -> None:
    """The armed budget is a default for the request, not an override of an explicit decision."""
    recorder = Recording()
    client = PitchClient(recorder)
    client.arm(2.0)
    client.complete("a prompt", timeout=0.25)
    assert recorder.timeouts == [0.25]
    assert client.disarm().outcome == "ok"


def test_the_budget_is_thread_local_so_two_auctions_cannot_take_each_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``answer_bid_request`` is a plain ``def``, so FastAPI runs it on the threadpool.

    Two solicitations are therefore in flight at once against ONE process-cached `PitchClient`.
    A budget on ``self`` would mean a 4-second auction handing its budget to a 0.2-second one —
    or taking it — and the resulting bid would be wrong in a way no single-threaded test could
    see. `ResponseChannel` next door solved the identical problem the identical way.
    """
    import threading  # noqa: PLC0415 - the concurrency is the subject, not a module-wide need

    del monkeypatch
    client = PitchClient(Recording())
    seen: dict[str, Any] = {}
    started = threading.Barrier(2)

    def arm_and_read(name: str, budget: float) -> None:
        client.arm(budget)
        started.wait(timeout=5.0)
        time.sleep(0.05)
        seen[name] = client.budget
        seen[f"{name}-attempt"] = client.disarm()

    threads = [
        threading.Thread(target=arm_and_read, args=("short", 0.2)),
        threading.Thread(target=arm_and_read, args=("long", 4.0)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert seen["short"] == 0.2
    assert seen["long"] == 4.0
    assert seen["short-attempt"].budget == 0.2
    assert seen["long-attempt"].budget == 4.0
    assert client.budget is None, "the main thread armed nothing and must have nothing"


# =============================================================================================
# 4. The mechanism that makes the budget honest: max_retries=0 on the live client
# =============================================================================================


def _fake_sdk() -> tuple[Any, list[dict[str, Any]]]:
    """A stand-in ``anthropic`` module that records the constructor keywords it is handed."""
    seen: list[dict[str, Any]] = []

    def anthropic_client(**kwargs: Any) -> Any:
        seen.append(dict(kwargs))
        return SimpleNamespace(messages=SimpleNamespace())

    return SimpleNamespace(Anthropic=anthropic_client), seen


def test_the_live_pitch_client_pins_the_sdks_retries_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured 3.92s-vs-0.84s defect, graded at the SDK constructor.

    ``anthropic==1.2.0`` defaults ``max_retries`` to 2 and RETRIES ``APITimeoutError``, so a
    0.8s per-call timeout was measured raising after 3.92s. A budget a retry can multiply is not
    a budget, and every other assertion in this file would pass against a client that still took
    three times what it was given.

    The assertion is at the constructor and not on the wrapper's attribute on purpose: storing
    the number and never passing it is exactly the shape of the defect being repaired.
    """
    live = pitch_client({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-not-used"})
    assert live is not None
    assert live.inner.max_retries == PITCH_MAX_RETRIES == 0
    assert live.inner.timeout == PITCH_TIMEOUT_SECONDS

    fake, seen = _fake_sdk()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    live.inner._ensure_client()  # noqa: SLF001 - the SDK build is the private thing under test
    assert seen == [{"api_key": "sk-not-used", "timeout": PITCH_TIMEOUT_SECONDS, "max_retries": 0}]


def test_pinning_the_retries_is_additive_and_changes_no_other_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Buyer, interview and extract must build byte-identically to before the kwarg existed.

    ``max_retries=None`` is NOT "no retries" — it is "do not say", and the SDK then applies its
    own ``DEFAULT_MAX_RETRIES``. A client built unconditionally with ``max_retries=None`` would
    have silently disabled retries for every conversational role in the repository, which is a
    much worse change than the one being made.
    """
    fake, seen = _fake_sdk()
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    buyer = build_llm(ROLE_BUYER, provider="anthropic", env={"ANTHROPIC_API_KEY": "sk-x"})
    assert buyer.max_retries is None
    buyer._ensure_client()  # noqa: SLF001 - as above
    assert seen[-1] == {"api_key": "sk-x", "timeout": buyer.timeout}
    assert "max_retries" not in seen[-1]

    asked = build_llm(
        ROLE_STORE_AGENT, provider="anthropic", env={"ANTHROPIC_API_KEY": "sk-x"}, max_retries=0
    )
    asked._ensure_client()  # noqa: SLF001 - as above
    assert seen[-1]["max_retries"] == 0


# =============================================================================================
# 5. The store says so when it misses its own deadline
# =============================================================================================


def test_every_copywriter_outcome_has_a_word_for_what_the_call_did() -> None:
    """A new outcome with no entry in the route's table would log ``pitch_call=unknown`` in prod.

    The mapping and the vocabulary live in different modules on purpose — the copywriter owns
    what happened, the route owns how it is said — so this is the seam where they can drift
    apart silently, and a log line an operator cannot interpret is the failure being repaired.

    **The words are about the CALL, and this test is where that is pinned.** They used to be
    about the shipped prose (`source=model` for the outcome `ok`), which the route cannot know:
    `compose_pitch` screens the model's reply and ships the deterministic template when it
    fails the content rule, in a pure function whose only return value is the string. Measured
    on the deployed demo, three of four agents served the byte-identical template while logging
    `outcome=ok source=model`, and the one agent that served real prose logged the same line.
    """
    from store_agent.solicitation.routes import _PITCH_CALL  # noqa: PLC0415

    assert set(_PITCH_CALL) == set(PITCH_OUTCOMES)
    assert set(_PITCH_CALL.values()) == {
        "answered",
        "budget_missed",
        "errored",
        "not_called",
        "unarmed",
    }
    # ...and no word here may name a PROVENANCE, which is the claim that was wrong.
    assert not {"model", "fallback"} & set(_PITCH_CALL.values()), (
        "a word in this table names where the shipped pitch came from, which this route cannot "
        "know — see the table's own comment for the measurement"
    )


def test_the_store_logs_a_warning_when_the_pitch_misses_its_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without this line the defect is invisible, which is how it survived every green suite.

    The old line said "answered a solicitation in mode active (submitting=True)" and nothing
    else — the same words whether the pitch took 40 ms or 5 s, and whether the served
    `Bid.message` was the merchant's own voice or the platform's template. An operator could not
    tell a market of four advocating stores from four stores serving fallback prose after the
    exchange had given up on them.

    The word is ``timed_out`` and not ``failed``: this copywriter gave up on the clock, which is
    what a wider window or a faster model repairs. ``failed`` is reserved for a copywriter that
    is broken — see ``test_a_broken_copywriter_is_not_reported_as_a_missed_deadline``.
    """
    with caplog.at_level(logging.INFO, logger=ROUTE_LOGGER):
        serve(request_body(soon(1.0)), Slow(latency=3.0), monkeypatch)
    lines = [r for r in caplog.records if r.name == ROUTE_LOGGER]
    assert len(lines) == 1
    (line,) = lines
    assert line.levelno == logging.WARNING
    message = line.getMessage()
    assert "outcome=timed_out" in message
    assert "pitch_call=budget_missed" in message
    assert "budget=0." in message, f"the budget it was given must be in the line: {message}"
    assert "elapsed=0." in message, f"what the copywriter spent must be in the line: {message}"
    assert STORE_ID in message


def test_the_store_logs_a_warning_when_it_had_no_budget_to_spend(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Skipped for lack of time is a WARNING too: the bid is fine, the product is not delivered."""
    with caplog.at_level(logging.INFO, logger=ROUTE_LOGGER):
        serve(request_body(soon(-5.0)), Forbidden(), monkeypatch)
    (line,) = [r for r in caplog.records if r.name == ROUTE_LOGGER]
    assert line.levelno == logging.WARNING
    assert "outcome=skipped" in line.getMessage()
    assert "pitch_call=not_called" in line.getMessage()


def test_the_store_logs_at_info_when_the_copywriter_makes_its_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half, and the one that keeps the WARNING worth reading.

    A line that warned on every solicitation would be filtered out within a day, and the defect
    would be exactly as invisible as it was before.
    """
    with caplog.at_level(logging.INFO, logger=ROUTE_LOGGER):
        serve(request_body(soon(4.0)), Recording(), monkeypatch)
    (line,) = [r for r in caplog.records if r.name == ROUTE_LOGGER]
    assert line.levelno == logging.INFO
    assert "outcome=ok" in line.getMessage()
    # `answered`, not `model`: the model replied inside its budget, which is what this line
    # can attest. Whether its words were used is decided later, by `screen`, out of sight.
    assert "pitch_call=answered" in line.getMessage()


def test_the_line_still_carries_what_it_always_carried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The store, the mode, whether it submitted, and the bid-log depth. Nothing was dropped."""
    with caplog.at_level(logging.INFO, logger=ROUTE_LOGGER):
        _, _, app = serve(request_body(soon(4.0)), Recording(), monkeypatch)
    (line,) = [r for r in caplog.records if r.name == ROUTE_LOGGER]
    message = line.getMessage()
    assert STORE_ID in message
    assert "submitting=True" in message
    assert "1 entr(y|ies) in the bid log" in message
    hosted = advocate(app)
    assert hosted is not None and hosted.pitch is not None
    assert hosted.pitch.budget is None, "the route must disarm the client before it returns"


def test_a_store_with_no_copywriter_logs_at_info_and_claims_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No model configured is not a missed deadline, and must not be reported as one.

    D20 keeps the offline default clientless-or-doubled, so this is the shape most deployments
    and every offline run take. Reporting it at WARNING would bury the real misses.
    """
    with caplog.at_level(logging.INFO, logger=ROUTE_LOGGER):
        response, _, app = serve(request_body(soon(4.0)), None, monkeypatch)
    assert response.status_code == 200
    (line,) = [r for r in caplog.records if r.name == ROUTE_LOGGER]
    assert line.levelno == logging.INFO
    assert "outcome=not_attempted" in line.getMessage()
    assert "pitch_call=unarmed" in line.getMessage()
    assert "budget=-" in line.getMessage() and "elapsed=-" in line.getMessage()
    hosted = advocate(app)
    assert hosted is not None and hosted.pitch is None


# =============================================================================================
# 6. A BROKEN copywriter is not a SLOW one, and the log must not say it is
# =============================================================================================


class Broken:
    """A copywriter that fails for a reason no auction window would have fixed.

    A 401 is the case that matters: a container with a wrong key raises on every solicitation
    for the life of the image, and the whole point of separating the two words is that this must
    not read as latency. `PermissionError` is used rather than a stand-in `anthropic` class
    because the classification must hold with no SDK in the image at all (D20); the SDK's own
    class is graded separately below.
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = PermissionError("401 invalid x-api-key") if error is None else error
        self.calls = 0

    def complete(self, prompt: Any, **_kwargs: Any) -> str:
        del prompt
        self.calls += 1
        raise self.error


def test_the_two_ways_a_copywriter_can_lose_are_recorded_as_different_facts() -> None:
    """``missed`` is a TIME fact again, and it stopped being one the moment it counted faults.

    ``PitchAttempt.missed`` was documented as "the fallback because of TIME, not because of
    taste" while returning True for ``failed`` — which `PitchClient.complete` set for ANY
    exception. An auth failure, a reset connection and a provider 500 therefore all reported a
    missed deadline, which is precisely the misdiagnosis (a broken key read as a latency
    problem) that the pitch budget exists to end. The two are separate outcomes now.
    """
    timed = PitchClient(Slow(latency=3.0))
    timed.arm(0.05)
    with pytest.raises(TimeoutError):
        timed.complete("a prompt")
    ran_out = timed.disarm()
    assert ran_out.outcome == "timed_out"
    assert (ran_out.missed, ran_out.faulted, ran_out.degraded) == (True, False, True)

    broke = PitchClient(Broken())
    broke.arm(4.0)
    with pytest.raises(PermissionError):
        broke.complete("a prompt")
    fault = broke.disarm()
    assert fault.outcome == "failed"
    assert (fault.missed, fault.faulted, fault.degraded) == (False, True, True)
    assert fault.budget == 4.0, "a fault must still report the budget it had; it was not short"


def test_a_broken_copywriter_is_not_reported_as_a_missed_deadline(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Through the real door, with four seconds of budget and a copywriter that cannot work.

    Still a WARNING — the merchant is not getting the prose it joined to buy — and still a
    served bid with the fallback pitch. The word is what changed: an operator reading
    ``outcome=failed`` against ``budget=3.65s elapsed=0.00s`` can see this is not the clock.
    """
    with caplog.at_level(logging.INFO, logger=ROUTE_LOGGER):
        response, _, _ = serve(request_body(soon(4.0)), Broken(), monkeypatch)
    assert response.status_code == 200, response.headers.get(DECLINE_REASON_HEADER)
    assert response.json()["offer"]["unit_price"] > 0.0
    (line,) = [r for r in caplog.records if r.name == ROUTE_LOGGER]
    assert line.levelno == logging.WARNING, "a broken copywriter is still an operator's problem"
    message = line.getMessage()
    assert "outcome=failed" in message
    assert "pitch_call=errored" in message
    assert "outcome=timed_out" not in message


def test_the_copywriters_own_line_says_which_of_the_two_happened(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The route's line is per solicitation; this one names the exception type and the budget.

    Both at WARNING, and neither carries the provider's message — an auth failure's error text
    is the string on this path most likely to contain a credential.
    """
    copywriter_logger = "store_agent.solicitation.copywriter"
    with caplog.at_level(logging.WARNING, logger=copywriter_logger):
        slow = PitchClient(Slow(latency=3.0))
        slow.arm(0.05)
        with pytest.raises(TimeoutError):
            slow.complete("a prompt")
        broke = PitchClient(Broken())
        broke.arm(4.0)
        with pytest.raises(PermissionError):
            broke.complete("a prompt")

    missed, faulted = [r for r in caplog.records if r.name == copywriter_logger]
    assert missed.levelno == faulted.levelno == logging.WARNING
    assert "RAN OUT OF TIME" in missed.getMessage()
    assert "TimeoutError" in missed.getMessage()
    assert "IS BROKEN" in faulted.getMessage()
    assert "NOT a missed deadline" in faulted.getMessage()
    assert "PermissionError" in faulted.getMessage()
    assert "invalid x-api-key" not in faulted.getMessage(), "the provider's text may carry a key"


def test_the_sdks_own_timeout_counts_without_this_module_importing_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`anthropic.APITimeoutError` is found in ``sys.modules``, never imported.

    ``packages/llm`` keeps ``import anthropic`` inside ``AnthropicLLM._ensure_client`` so an
    offline image needs no SDK (D20), and an import in the classifier would undo that for every
    offline run. The lookup is sufficient as well as cheap: an `APITimeoutError` instance cannot
    exist unless the module is already imported.
    """

    class APITimeoutError(Exception):
        """Shaped like the real one, which is NOT a subclass of the builtin `TimeoutError`."""

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(APITimeoutError=APITimeoutError))
    assert is_deadline_miss(APITimeoutError("Request timed out.")) is True
    assert is_deadline_miss(TimeoutError()) is True
    assert is_deadline_miss(PermissionError("401 invalid x-api-key")) is False

    # A stubbed module without the attribute — several tests here install one — must not raise,
    # and must fall to "fault". A fault miscalled as a miss is the failure being repaired; a
    # miss miscalled as a fault only over-reports a broken copywriter, which is the safe way.
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace())
    assert is_deadline_miss(RuntimeError("boom")) is False


def test_the_classification_holds_with_no_anthropic_installed_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default image ships no SDK, and every offline suite runs in it."""
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    assert is_deadline_miss(TimeoutError("the provider gave up")) is True
    assert is_deadline_miss(RuntimeError("boom")) is False
    assert is_deadline_miss(PitchBudgetExhausted("no time left")) is False


def test_the_real_sdks_timeout_class_is_the_one_that_is_recognised() -> None:
    """Graded against the installed SDK, because a fake cannot say the real class still matches.

    ``anthropic==1.2.0`` derives `APITimeoutError` from `APIConnectionError`, NOT from the
    builtin `TimeoutError` — so the `isinstance(error, TimeoutError)` half does not cover it and
    the ``sys.modules`` lookup is load-bearing. Skipped rather than asserted away where the SDK
    is absent: this repository's default image does not ship it.
    """
    anthropic = pytest.importorskip("anthropic")
    assert not issubclass(anthropic.APITimeoutError, TimeoutError), (
        "if the SDK ever derives it from the builtin, this test is the place that notices"
    )
    timed_out = anthropic.APITimeoutError.__new__(anthropic.APITimeoutError)
    assert is_deadline_miss(timed_out) is True
    assert is_deadline_miss(anthropic.AnthropicError("a provider fault")) is False


# =============================================================================================
# 7. A composition root that wires its OWN runner still gets a budget
# =============================================================================================


def test_configure_advocate_recovers_the_copywriter_from_the_runner_it_was_handed() -> None:
    """The foot-gun: ``configure_advocate(app, runner=...)`` with no ``pitch=`` armed nothing.

    ``store_agent.trust_intake.runner.configure_trust_intake`` makes exactly that call, so a
    process wired through the trust door had ``Advocate.pitch is None``, threw away the budget
    the route computed from the exchange's `respond_by`, and reverted to the fixed ceiling —
    this ticket's defect, silently reintroduced by a composition root. The client and the
    runner's copywriter are the same object by construction, so it is recovered rather than
    required.
    """
    client = PitchClient(Recording())
    app = create_app()
    configure_advocate(app, runner=SimpleNamespace(_llm=client, store_id=STORE_ID))
    hosted = advocate(app)
    assert hosted is not None
    assert hosted.pitch is client


def test_an_explicit_pitch_still_wins_over_the_one_the_runner_holds() -> None:
    """Recovery is a default, not an override: a caller that names one has decided."""
    explicit = PitchClient(Recording())
    app = create_app()
    configure_advocate(app, runner=SimpleNamespace(_llm=PitchClient(Recording())), pitch=explicit)
    hosted = advocate(app)
    assert hosted is not None and hosted.pitch is explicit


def test_an_advocate_that_cannot_recover_a_budget_says_so_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A copywriter that genuinely cannot be budgeted is allowed; failing silently is not.

    A runner writing with a bare client rather than a `PitchClient` has a model and no way to
    hold it to the exchange's deadline — the served bid can then land after the auction closes
    exactly as it did before any of this existed. One line, where the advocate is built, rather
    than one per solicitation.
    """
    app = create_app()
    with caplog.at_level(logging.WARNING, logger=ADVOCATE_LOGGER):
        configure_advocate(app, runner=SimpleNamespace(_llm=Recording(), store_id=STORE_ID))
    hosted = advocate(app)
    assert hosted is not None and hosted.pitch is None
    (line,) = [r for r in caplog.records if r.name == ADVOCATE_LOGGER]
    assert "Recording" in line.getMessage()
    assert "PitchClient" in line.getMessage()


def test_a_runner_with_no_copywriter_at_all_is_not_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D20's default, and the shape most deployments take: nothing to bound, nothing to say.

    Warning here would put the line in front of every offline run and every test, which is how
    a warning stops being read — and the store loses nothing, because a bid with no copywriter
    already carries the deterministic fallback pitch.
    """
    app = create_app()
    with caplog.at_level(logging.WARNING, logger=ADVOCATE_LOGGER):
        configure_advocate(app, runner=SimpleNamespace(_llm=None, store_id=STORE_ID))
    hosted = advocate(app)
    assert hosted is not None and hosted.pitch is None
    assert [r for r in caplog.records if r.name == ADVOCATE_LOGGER] == []


def test_the_trust_doors_own_wiring_answers_inside_the_exchanges_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the REAL bid door on an app wired by ``configure_trust_intake``.

    This is the caller that hits the foot-gun in the shipped tree, so it is graded through the
    door rather than on the dataclass: a 3-second copywriter, a 1-second auction window, and a
    bid that has to be on the wire before the exchange gives up. Before the recovery it armed
    nothing, handed the provider no per-request timeout, and took the copywriter's full latency.
    """
    slow = Slow(latency=3.0)
    client = PitchClient(slow)
    monkeypatch.setattr(advocate_module, "pitch_client", lambda *_a, **_k: client)

    donor = create_app()
    configure_solicitation(donor, context=context())
    hosted = advocate(donor)
    assert hosted is not None and hosted.pitch is client

    app = create_app()
    configure_solicitation(app, context=context())
    configure_trust_intake(app, runner=hosted.runner)  # the real call, and it names no pitch=

    with TestClient(app) as http:
        started = time.monotonic()
        response = http.post("/v1/bid-requests", json=request_body(soon(1.0)))
        elapsed = time.monotonic() - started

    assert response.status_code == 200, response.headers.get(DECLINE_REASON_HEADER)
    assert slow.timeouts and slow.timeouts[-1] is not None, (
        "the wired advocate armed no budget, so the copywriter kept whatever fixed bound it was "
        "built with — the exact defect the budget closes"
    )
    assert 0.0 < float(slow.timeouts[-1]) <= 0.7, (
        f"a 1s window less the 0.35s reserve is ~0.65s, got {slow.timeouts[-1]}"
    )
    assert elapsed < 2.0, f"the bid took {elapsed:.2f}s against a 1s window"
    assert MODEL_REPLY not in (response.json()["message"] or "")
