"""The dedicated advocate's pitch: the thing a shop BUYS by joining (SPEC Core tenet, D55).

Everything here is graded against one claim, and it is the product's: *an in-network shop gets a
pitch written for THIS shopper, in its own voice, and a scraped shop does not.* The measurements
below were taken on the tree this file was written against, driving the real served door.

**Before:** `grep -rn 'build_llm|ROLE_STORE_AGENT' packages/store-agent/src` returned nothing.
`Bid.message` was `None` on every hosted bid — `test_runtime.py` asserted it — so `BidRequest`
carried `intent.query`, `intent.preferences` and a `BuyerProfile` to an agent that read none of
them. The sponsored side of the network had no voice.

**After, `POST /v1/bid-requests` against `fixtures/envelopes/store-alpha.approved.json`,
activation `active`, two shoppers, the SAME store and the SAME product (`prod-cap`):**

    shopper A (weights `material`)     200  "You weighted material, and here it is: merino wool.
                                             Also: ships within: 2 business days; free returns: 30 days."
    shopper B (weights `ships_within`) 200  "You weighted ships within, and here it is: 2 business
                                             days. Also: free returns: 30 days; material: merino wool."

and with a reviewed recorded model wired in (`STORE_AGENT_PITCH_RECORDINGS=store_agent_pitch`),
the same two solicitations:

    shopper A  200  "Merino wool is the whole of it here: it holds warmth on a cold ride and stays
                     wearable at the other end. It leaves us within two business days, and if it
                     turns out not to be your commute layer, returns are free for thirty days."
    shopper B  200  "It leaves us within two business days, so it can be on your doorstep before
                     the weekend. If the fit is wrong, returns are free for thirty days. Merino
                     wool, and in stock now."

    shadow store   204 envelope_not_activated   (no body, so no pitch on the wire)
    killed store   204 store_killed             (no body, so no pitch on the wire)

The difference is attributable to the shopper and not to sampling: the same intent driven twice
is byte-identical, and the fact that leads is the fact that shopper's own `preferences` weight.

**What is graded here, and what is graded by construction.** Rules A (no price), B (only what a
claim supports) and E (no buyer identity) are enforced by *filters over the material*, not by
prompt text, so each of them is tested twice: once that the honest path is clean, and once —
armed — that the filter refuses a real violation. A screen that cannot fail is the failure mode
this repo has shipped before.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from contracts import Bid, BidRequest, BuyerProfile, ProfileBuckets
from fastapi.testclient import TestClient
from llm.doubles import DeterministicLLM, RecordedLLM
from llm.prompting import wire_key
from llm.recordings import load_recording
from store_agent.hooks import ToolHooks
from store_agent.main import create_app
from store_agent.runtime import bid, is_decline
from store_agent.runtime.context import SERVED_PITCHES_KEY, assemble_context
from store_agent.runtime.pitch import (
    MAX_PITCH_CHARS,
    MIN_PITCH_WORDS,
    PITCH_CONTRACT,
    PITCHABLE_SOURCES,
    PROFILE_BUCKET_KEYS,
    compose_pitch,
    fallback_pitch,
    is_monetary,
    material_for,
    pitch_prompt,
    screen,
    screen_reasons,
)
from store_agent.solicitation import (
    DECLINE_REASON_HEADER,
    KILLED_REASON,
    NOT_ACTIVATED_REASON,
    PITCH_RECORDINGS_ENV,
    PITCH_TIMEOUT_ENV,
    PITCH_TIMEOUT_SECONDS,
    PitchClient,
    advocate,
    configure_solicitation,
    pitch_client,
    resolve_pitch_timeout,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENVELOPE_FIXTURE = REPO_ROOT / "fixtures" / "envelopes" / "store-alpha.approved.json"
PITCH_SRC = REPO_ROOT / "packages" / "store-agent" / "src" / "runtime" / "pitch.py"

STORE_ID = "store-alpha"
CLUSTER = "cluster-warm-layers"
STORE_DOMAIN = "store-alpha.example.com"
LIST_PRICE = 100.0

#: The recorded fixture this lane authored against :data:`PITCH_CONTRACT`. Its stem is what
#: ``STORE_AGENT_PITCH_RECORDINGS`` names.
PITCH_FIXTURE = "store_agent_pitch"

#: A pseudonym distinctive enough that finding it anywhere is proof, not coincidence. R5 says a
#: store receives one of these and never an identity; this file proves the pitch path receives
#: even less than that — the pseudonym does not reach the model either.
PSEUDONYM = "psn-ROTATING-7f3a"


# =============================================================================================
# The store, and two shoppers who differ only in what they are weighing
# =============================================================================================


def context(*, activation: str = "active", **overrides: Any) -> dict[str, Any]:
    """A store context in the shape the merchant service hands over, for an ACTIVATED store.

    `activation` is overlaid on the approved artifact rather than written into it, exactly as
    ``test_solicitation.py`` does and for the same reason: the shipped fixture states `shadow`,
    which is what makes it the honest source for the R7 case below.
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


#: The hard constraint both shoppers state, so both auctions land on the SAME product and the
#: pitches can only differ because the SHOPPERS differ. Without this, a difference would be
#: attributable to the catalogue instead.
MERINO = [{"field": "material", "op": "eq", "value": "merino wool"}]


def request_body(
    auction_id: str,
    *,
    query: str,
    preferences: list[dict[str, Any]],
    buckets: dict[str, Any],
    hard_constraints: list[dict[str, Any]] | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "auction_id": auction_id,
        "intent": {
            "intent_id": f"int-{auction_id}",
            "cluster_id": CLUSTER,
            "query": query,
            "category": "outerwear",
            "hard_constraints": MERINO if hard_constraints is None else hard_constraints,
            "preferences": preferences,
            "ship_to": "US-CA",
            "currency": "USD",
            "budget_band": "50-150",
            "created_at": "2026-01-01T00:00:00Z",
            "schema_version": "1.0.0",
        },
        "profile": profile if profile is not None else {"pseudonym": PSEUDONYM, "buckets": buckets},
        "respond_by": "2999-01-01T00:00:00Z",
    }


def shopper_a() -> dict[str, Any]:
    """Weighs the fabric. A repeat buyer on the west coast."""
    return request_body(
        "auc-A",
        query="a warm merino mid-layer for cold bike commutes",
        preferences=[
            {"field": "material", "direction": "prefer", "weight": 1.0},
            {"field": "price", "direction": "minimize", "weight": 0.9},
        ],
        buckets={
            "budget_band": "50-150",
            "category_affinity": ["outerwear"],
            "frequency_tier": "occasional",
            "region": "US-CA",
            "first_time": False,
        },
    )


def shopper_b() -> dict[str, Any]:
    """Weighs dispatch and returns. A first-time buyer in a hurry."""
    return request_body(
        "auc-B",
        query="need it before the weekend and easy to send back if the fit is wrong",
        preferences=[
            {"field": "ships_within", "direction": "minimize", "weight": 1.0},
            {"field": "free_returns", "direction": "maximize", "weight": 0.8},
        ],
        buckets={
            "budget_band": "50-150",
            "category_affinity": ["gifts"],
            "frequency_tier": "frequent",
            "region": "US-NY",
            "first_time": True,
        },
    )


def answered(body: dict[str, Any], ctx: dict[str, Any] | None = None, **kwargs: Any) -> Bid:
    """A bid that must BE a bid. A decline here is a failure to report, never a silent skip."""
    answer = bid(body, context() if ctx is None else ctx, **kwargs)
    assert not is_decline(answer), f"expected a bid, got {answer}"
    return answer


def served(body: dict[str, Any], ctx: dict[str, Any] | None = None) -> Any:
    """One solicitation through the REAL door, on a freshly built app."""
    app = create_app()
    configure_solicitation(app, context=context() if ctx is None else ctx)
    with TestClient(app) as client:
        return client.post("/v1/bid-requests", json=body)


class Capturing:
    """A copywriter that records the prompt it was handed and answers with a fixed reply.

    The prompt is what rules A and E are actually about — a pitch that happens not to *mention*
    a price is not the same property as a writer that was never *shown* one — so every prompt
    assertion below reads this, not the message.
    """

    def __init__(
        self, reply: str = "Merino wool, ready to leave us within two business days."
    ) -> None:
        self.prompts: list[Any] = []
        self.reply = reply

    def complete(self, prompt: Any, **_kwargs: Any) -> str:
        self.prompts.append(prompt)
        return self.reply

    @property
    def text(self) -> str:
        """Both halves of the last prompt, exactly as they go on the wire, joined for searching."""
        system, user = wire_key(self.prompts[-1])
        return f"{system}\n{user}"


class Exploding:
    """A copywriter that is down."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or TimeoutError("the model did not answer in time")
        self.calls = 0

    def complete(self, prompt: Any, **_kwargs: Any) -> str:
        self.calls += 1
        raise self.error


# =============================================================================================
# 1. THE CLAIM: two shoppers, one store, two pitches — served, and reproducible
# =============================================================================================


def test_two_shoppers_get_two_different_pitches_from_the_same_store_over_the_served_door() -> None:
    """The whole feature, at the boundary that proves it is reachable.

    Same store, same envelope, same product. The only thing that differs is the shopper, so the
    only thing that can explain a difference in the pitch is the shopper.
    """
    first, second = served(shopper_a()), served(shopper_b())
    assert (first.status_code, second.status_code) == (200, 200)
    a, b = first.json(), second.json()

    assert a["offer"]["product_ref"] == b["offer"]["product_ref"] == "prod-cap", (
        "both shoppers must land on the SAME product, or the difference below is attributable "
        "to the catalogue rather than to the shopper"
    )
    assert a["message"] and b["message"], "an in-network store that says nothing bought nothing"
    assert a["message"] != b["message"]

    # ...and the difference is the SHOPPER's, spelled out rather than merely observed: each pitch
    # leads with the fact that shopper's own `preferences` weight.
    assert a["message"].startswith("You weighted material"), a["message"]
    assert b["message"].startswith("You weighted ships within"), b["message"]


def test_the_same_shopper_twice_is_byte_identical() -> None:
    """S4, and the control that makes the test above mean anything.

    Two pitches that differ prove nothing if a third run would differ again. Driven through two
    freshly built apps, so no in-process state can be doing the work.
    """
    once, twice = served(shopper_a()), served(shopper_a())
    assert once.json()["message"] == twice.json()["message"]
    assert once.content == twice.content, (
        "the whole served bid is byte-identical, not just the pitch"
    )


def test_a_recorded_model_writes_the_pitch_through_the_same_served_door(monkeypatch) -> None:
    """D20/D21: a reviewed model reply, replayed offline, reaching a shopper through the real door.

    This is the "a model wrote it" half of the claim, and it runs with no key and no network.
    """
    monkeypatch.setenv(PITCH_RECORDINGS_ENV, PITCH_FIXTURE)
    a, b = served(shopper_a()).json(), served(shopper_b()).json()

    assert a["message"].startswith("Merino wool is the whole of it here"), a["message"]
    assert b["message"].startswith("It leaves us within two business days"), b["message"]
    assert a["message"] != b["message"]
    assert served(shopper_a()).json()["message"] == a["message"], "and still reproducible"


def test_the_recorded_fixture_is_reachable_through_the_real_prompt_builder() -> None:
    """The D21 guard: a change to the contract must break replay LOUDLY, not silently.

    `RecordedLLM` keys on the ``(system, prompt)`` PAIR, so this asserts that the pair
    :func:`pitch_prompt` actually composes today is a key in the reviewed fixture. Edit
    :data:`PITCH_CONTRACT` or the request block and this goes red, which is the whole point —
    the alternative is replaying an answer that was reviewed against a contract no longer in
    force, with the suite still green.
    """
    recorded = load_recording(PITCH_FIXTURE)
    assert recorded, "the fixture must carry recordings"
    assert {system for system, _ in recorded} == {PITCH_CONTRACT}, (
        "the fixture's system contract has drifted from PITCH_CONTRACT; re-review the recordings"
    )
    for body in (shopper_a(), shopper_b()):
        capture = Capturing()
        answered(body, llm=capture)
        assert wire_key(capture.prompts[-1]) in recorded, (
            f"the prompt this agent now composes for {body['auction_id']} is not in the reviewed "
            f"fixture, so the recorded pitch is unreachable and every bid silently falls back"
        )


# =============================================================================================
# 2. Rule A — the model never touches price. Twice: the honest path, and the armed control.
# =============================================================================================


@pytest.mark.parametrize("body", [shopper_a(), shopper_b()], ids=["shopper-a", "shopper-b"])
def test_no_price_the_envelope_governs_ever_reaches_the_prompt(body: dict[str, Any]) -> None:
    """Not "the pitch does not mention a price" — "the writer was never shown one"."""
    capture = Capturing()
    answer = answered(body, llm=capture)
    assert answer.offer.unit_price == LIST_PRICE, "sanity: there IS a price to have leaked"

    text = capture.text
    for forbidden in ("100.0", "100", "list_price", "max_discount_pct", "budget_cap", "min_price"):
        assert forbidden not in text, f"{forbidden!r} reached the copywriter's prompt"
    assert "$" not in text and "%" not in text.replace("percentage", "")
    assert "50-150" not in text, (
        "the buyer's own budget band is money too, and is not on the allowlist"
    )


def test_an_authorised_discount_changes_the_price_and_never_the_prompt() -> None:
    """The case the rule exists for: a bid that DOES carry a grant.

    The envelope's intro rule moves `unit_price` through the audited `authorize_discount` path,
    exactly as it did before this feature existed — and the depth, the grant and the new price
    are all still invisible to the writer, because the grant's provenance source is
    `envelope_rule` and the pitch's material is filtered by source.
    """
    discounted = context()
    discounted["envelope"] = {**discounted["envelope"], "intro_discount_pct": 15.0}

    capture = Capturing()
    answer = answered(shopper_a(), discounted, llm=capture)
    assert answer.offer.discount is not None and answer.offer.discount.value == 15.0
    assert answer.offer.unit_price == 85.0, "the envelope moved the price, as it always did"

    text = capture.text
    for forbidden in ("15.0", "85.0", "authorized_discount_pct", "envelope_rule", "learned_policy"):
        assert forbidden not in text, f"{forbidden!r} reached the copywriter's prompt"
    assert answer.message and "15" not in answer.message and "85" not in answer.message


@pytest.mark.parametrize(
    "reply",
    [
        "Merino wool, and 20% off this week only.",
        "Merino wool at just $79 while stocks last.",
        "The best price you will find on merino wool anywhere.",
        "Merino wool, and we will save you money on the returns.",
    ],
    ids=["percent-off", "currency", "price-superlative", "savings"],
)
def test_a_pitch_that_talks_about_money_is_refused_and_the_store_bids_anyway(reply: str) -> None:
    """ARMED. Rule A's output half, shown refusing rather than assumed to.

    The refusal costs the store its phrasing and nothing else: the bid, the offer and the price
    are untouched, and a deterministic pitch is served in its place (rule D).
    """
    clean = answered(shopper_a())
    dirty = answered(shopper_a(), llm=Capturing(reply))

    assert dirty.message != reply, f"a monetary pitch was served: {dirty.message!r}"
    assert dirty.message == clean.message, "and what was served instead is the deterministic pitch"
    assert dirty.offer.model_dump(mode="json") == clean.offer.model_dump(mode="json")


def test_the_monetary_key_filter_refuses_the_keys_that_matter_and_admits_the_ones_that_do_not() -> (
    None
):
    """The predicate itself, since three separate filters lean on it."""
    for key in (
        "list_price",
        "unit_price",
        "total_price",
        "discount_pct",
        "max_discount_pct",
        "msrp",
        "shipping_cost",
        "budget_band",
        "handling_fee",
        "sale_price",
    ):
        assert is_monetary(key), f"{key!r} is money and must never reach the writer"
    for key in (
        "material",
        "free_returns",
        "ships_within",
        "in_stock",
        "units_left",
        "capacity",
        "compatibility",
        "ingredients",
        "warranty",
        "care_instructions",
    ):
        assert not is_monetary(key), f"{key!r} is not money; refusing it costs a true sentence"
    # ...and by declared claim type, whatever the key is spelled.
    assert is_monetary("headline_number", "unit_price")


# =============================================================================================
# 3. Rule B — it may assert only what a claim in this same bid supports
# =============================================================================================


@pytest.mark.parametrize("body", [shopper_a(), shopper_b()], ids=["shopper-a", "shopper-b"])
def test_every_fact_offered_to_the_writer_has_a_claim_in_the_same_bid(body: dict[str, Any]) -> None:
    """R18 will check this pitch against the catalogue snapshot. This is why it can pass.

    Read off the assembled bid rather than off the material's own bookkeeping: the property is
    "a verifier reading `bid.claims` sees the evidence for everything the pitch said", and that
    is a statement about the bid.
    """
    hooks = ToolHooks(context())
    answer = bid(body, context(), hooks=hooks, llm=Capturing())
    assert not is_decline(answer)

    ctx = assemble_context(body, context())
    material = material_for(ctx, [*answer.claims, *answer.offer.commitments])
    assert material.facts, "a pitch built from no facts makes this criterion vacuous"

    evidence = {
        (str(c.key), str(c.provenance.source.value))
        for c in [*answer.claims, *answer.offer.commitments]
    }
    for fact in material.facts:
        assert any(key == fact.key for key, _ in evidence), (
            f"the pitch was offered {fact.key!r}, which no claim in this bid supports"
        )
        assert {source for key, source in evidence if key == fact.key} <= PITCHABLE_SOURCES


def test_the_two_claims_carrying_a_number_the_envelope_governs_are_never_offered() -> None:
    """`envelope_rule` and `learned_policy` are excluded BY SOURCE, not by key name.

    That is the difference between a rule and a habit: a future hook that mints a differently
    named number under either source is excluded on the day it is written.
    """
    discounted = context()
    discounted["envelope"] = {**discounted["envelope"], "intro_discount_pct": 10.0}
    answer = answered(shopper_a(), discounted, llm=Capturing())
    ctx = assemble_context(shopper_a(), discounted)

    sources = {str(c.provenance.source.value) for c in answer.claims}
    assert {"envelope_rule", "learned_policy"} <= sources, "sanity: both claims ARE in the bid"

    offered = {fact.key for fact in material_for(ctx, answer.claims).facts}
    assert "authorized_discount_pct" not in offered and "policy_action" not in offered


@pytest.mark.parametrize(
    "reply",
    [
        "The best merino mid-layer on the market, guaranteed.",
        "Hand-finished in Scotland from single-origin alpaca fibres.",
        "We always ship same day and nobody returns these.",
    ],
    ids=["superlative", "invents-with-no-supported-fact", "absolutes"],
)
def test_a_pitch_that_oversells_is_refused(reply: str) -> None:
    """ARMED. Superlatives, absolutes, and prose that names none of the supported facts."""
    material = material_for(assemble_context(shopper_a(), context()), _claims_of(shopper_a()))
    assert screen_reasons(reply, material), f"{reply!r} passed the screen"
    served_message = answered(shopper_a(), llm=Capturing(reply)).message
    assert served_message != reply, f"an unsupportable pitch was served: {served_message!r}"


# =============================================================================================
# 4. Rule C — offline determinism, and a serviceable pitch with no key
# =============================================================================================


def test_the_pitch_module_reads_no_clock_no_rng_and_no_environment() -> None:
    """The source, not the behaviour. ``test_runtime.py`` scans the whole of `runtime/`; this
    names the file, so a reader of THIS file can see the property is real rather than inherited.

    ``import os`` here is what would make a bid depend on ambient state — which is why provider
    selection lives in `solicitation/copywriter.py` and the client is injected.
    """
    tree = ast.parse(PITCH_SRC.read_text(encoding="utf-8"), filename=str(PITCH_SRC))
    banned = {"os", "random", "secrets", "socket", "time", "datetime", "uuid", "requests", "httpx"}
    offences = [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Import) and any(a.name.split(".")[0] in banned for a in node.names)
        )
        or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in banned)
    ]
    assert not offences, f"the pitch path imported ambient state at {offences}"


def test_with_no_copywriter_configured_the_store_still_pitches_and_still_reproduces() -> None:
    """Rule C's headline: no API key, no provider, no client — and a serviceable pitch anyway."""
    first = answered(shopper_a(), llm=None)
    second = answered(shopper_a(), llm=None)
    assert first.message and first.message == second.message
    assert len(first.message.split()) >= MIN_PITCH_WORDS
    assert len(first.message) <= MAX_PITCH_CHARS
    assert "merino wool" in first.message


def test_the_default_offline_double_is_not_served_as_a_pitch() -> None:
    """D20's default provider answers ``double:store_agent:<hex>``. That is not a pitch.

    It passes no screen, so the deterministic fallback is served instead — which is exactly what
    makes "the whole suite runs with no network" and "a shopper never sees a debug marker" the
    same statement.
    """
    double = DeterministicLLM(role="store_agent")
    marker = double.complete("anything")
    assert marker.startswith("double:store_agent:")

    material = material_for(assemble_context(shopper_a(), context()), [])
    assert screen(marker, material) is None
    assert answered(shopper_a(), llm=double).message == answered(shopper_a(), llm=None).message


def test_the_copywriter_factory_defaults_to_the_offline_double_and_never_raises() -> None:
    """The composition root, driven directly. D20: the double is the default (no key needed)."""
    default = pitch_client({})
    assert isinstance(default, PitchClient) and default.model == "double:store_agent"

    recorded = pitch_client({PITCH_RECORDINGS_ENV: PITCH_FIXTURE})
    assert isinstance(recorded, PitchClient) and isinstance(recorded.inner, RecordedLLM)

    assert pitch_client({PITCH_RECORDINGS_ENV: "no_such_fixture"}) is None, (
        "a typo'd fixture stem must cost the store its prose, never its bid"
    )
    assert pitch_client({"LLM_PROVIDER": "antropic"}) is None


def test_the_pitch_timeout_is_seconds_not_the_conversational_default() -> None:
    """Rule D's stated bound. 60s (`LLM_TIMEOUT_SECONDS`) in front of a synchronously solicited
    bid has already lost the auction; 5s is a budget a bid can absorb."""
    assert PITCH_TIMEOUT_SECONDS == 5.0
    assert resolve_pitch_timeout({}) == 5.0
    assert resolve_pitch_timeout({PITCH_TIMEOUT_ENV: "2.5"}) == 2.5
    for junk in ("", "soon", "0", "-1"):
        assert resolve_pitch_timeout({PITCH_TIMEOUT_ENV: junk}) == PITCH_TIMEOUT_SECONDS

    live = pitch_client({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-not-used"})
    assert live is not None and live.inner.timeout == PITCH_TIMEOUT_SECONDS, (
        "the live client must carry the pitch budget, not the package-wide 60s default"
    )


def test_the_pitch_client_does_not_accumulate_calls_on_a_long_lived_advocate() -> None:
    """One `KeyedLLMCall` per auction, forever, on a process-level object is a leak.

    The advocate is built once per process and answers a solicitation per auction; the doubles
    record every call and drop none. `BoundedBidLog` next door solves the same problem with a
    ring because that log is read — this record is not, so the right size for it is zero.
    """
    client = PitchClient(DeterministicLLM(role="store_agent"))
    for _ in range(50):
        client.complete("a prompt")
    assert client.inner.calls == []

    # The wrapper cleans up in a `finally` and re-raises: it is a bookkeeper, never a second
    # place where a failing copywriter is silently absorbed. `compose_pitch` owns that decision.
    exploding = PitchClient(Exploding())
    with pytest.raises(TimeoutError):
        exploding.complete("a prompt")
    assert exploding.inner.calls == 1


# =============================================================================================
# 5. Rule D — a failed or slow copywriter costs prose, never the bid
# =============================================================================================


@pytest.mark.parametrize(
    "client",
    [
        Exploding(TimeoutError("the model did not answer in time")),
        Exploding(RuntimeError("connection reset")),
        Exploding(KeyError("unrecorded prompt")),
        Capturing(""),
        Capturing("   \n\n  "),
        Capturing('{"pitch": "merino wool"}'),
        Capturing("merino"),
        Capturing("merino wool " * 200),
    ],
    ids=["timeout", "transport", "unrecorded", "empty", "blank", "json", "too-short", "too-long"],
)
def test_a_broken_copywriter_never_costs_the_store_its_bid(client: Any) -> None:
    """Eight ways for generation to fail, one outcome: the store bids, with the fallback pitch.

    The offer is compared field for field against the no-copywriter bid, because "the bid still
    went out" is not the property — "the bid went out UNCHANGED" is. A copywriter that could
    perturb a price would be worse than one that is down.
    """
    baseline = answered(shopper_a(), llm=None)
    answer = answered(shopper_a(), llm=client)

    assert answer.offer.model_dump(mode="json") == baseline.offer.model_dump(mode="json")
    assert [c.model_dump() for c in answer.claims] == [c.model_dump() for c in baseline.claims]
    assert answer.message == baseline.message


def test_a_copywriter_that_throws_does_not_become_a_decline() -> None:
    """The specific regression this is guarded against: `_assemble` converts `ValueError` and
    `TypeError` into `unusable_store_context`, so a copywriter raising either would have turned
    a perfectly good bid into a decline. `compose_pitch` cannot raise; this proves it.

    `BaseException` is deliberately NOT in the list. `compose_pitch` catches `Exception`, so a
    `KeyboardInterrupt`, a `SystemExit` or an `asyncio.CancelledError` still propagates — a bid
    path that swallowed those would make the container unkillable and a cancelled request
    un-cancellable, which is a worse failure than a bid with no pitch.
    """
    for error in (ValueError("bad"), TypeError("worse"), LookupError("missing")):
        answer = bid(shopper_a(), context(), llm=Exploding(error))
        assert not is_decline(answer), f"a copywriter raising {error!r} cost the store its bid"
    with pytest.raises(KeyboardInterrupt):
        bid(shopper_a(), context(), llm=Exploding(KeyboardInterrupt()))


def test_compose_pitch_returns_none_rather_than_raising_on_a_context_it_cannot_read() -> None:
    """Rule D at the function's own boundary, not merely at the bid's."""

    class Hostile:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError(f"no {name}")

    assert compose_pitch(Hostile(), [], offer_ref="x") is None  # type: ignore[arg-type]


# =============================================================================================
# 6. Rule E — no buyer identity in the prompt or the output
# =============================================================================================


def test_the_profile_allowlist_excludes_identity_and_money_and_names_only_real_bucket_fields() -> (
    None
):
    """The mechanism, asserted against the protocol rather than against a memory of it."""
    declared = set(ProfileBuckets.model_fields)
    assert set(PROFILE_BUCKET_KEYS) <= declared, "the allowlist names a bucket that does not exist"
    assert "pseudonym" not in PROFILE_BUCKET_KEYS
    assert "pseudonym" in BuyerProfile.model_fields, "sanity: the pseudonym IS on the profile"
    assert "budget_band" in declared and "budget_band" not in PROFILE_BUCKET_KEYS, (
        "budget_band is a bucket AND it is money; rule A keeps it out"
    )


def test_no_identifying_field_reaches_the_model_or_the_pitch() -> None:
    """R5, driven rather than asserted in a comment.

    The profile is handed over as a RAW MAPPING carrying four identity fields `BuyerProfile`
    would refuse, because `assemble_context` is deliberately shape-tolerant and a merchant
    service handing over a dict is the case the allowlist has to survive. None of them reaches
    the prompt, because nothing in the pitch path reads a key the allowlist does not name.
    """
    identities = {
        "pseudonym": PSEUDONYM,
        "email": "shopper@example.com",
        "full_name": "Dana Okonkwo",
        "buyer_id": "usr-9931",
        "device_fingerprint": "fp-cafebabe",
    }
    body = request_body(
        "auc-E",
        query="a warm merino mid-layer",
        preferences=[{"field": "material", "direction": "prefer", "weight": 1.0}],
        buckets={},
        profile={**identities, "buckets": {"region": "US-CA", "first_time": True}},
    )

    capture = Capturing()
    answer = answered(body, llm=capture)
    haystack = f"{capture.text}\n{answer.message}"
    for label, value in identities.items():
        assert value not in haystack, f"{label} ({value!r}) reached the prompt or the pitch"
    assert "US-CA" in capture.text, "sanity: an ALLOWLISTED bucket did reach the prompt"


def test_a_pitch_that_echoes_an_identifier_is_refused_even_though_it_was_never_given_one() -> None:
    """ARMED, and belt-and-braces. The writer cannot have learned the pseudonym from us — so a
    reply containing it is a reply we must not store in an append-only public ledger."""
    leaking = f"Merino wool, picked out for {PSEUDONYM} and ready to ship."
    answer = answered(shopper_a(), llm=Capturing(leaking))
    assert answer.message != leaking and PSEUDONYM not in (answer.message or "")

    material = material_for(assemble_context(shopper_a(), context()), [])
    reasons = " ".join(screen_reasons(leaking, material))
    assert "buyer identity" in reasons
    assert PSEUDONYM not in reasons, (
        "the refusal reason is logged; repeating the identifier in it would defeat the catch"
    )


def test_the_pitch_does_not_quote_the_shopper_back_at_themselves() -> None:
    """Untrusted buyer text laundered into a public artifact is the same mistake as an
    unverified claim (C10). The words aim the pitch; they are not reproduced in it."""
    body = shopper_b()
    quoted = (
        "need it before the weekend and easy to send back if the fit is wrong — merino wool, "
        "and it leaves us within two business days."
    )
    answer = answered(body, llm=Capturing(quoted))
    assert answer.message != quoted
    material = material_for(assemble_context(body, context()), [])
    assert any("quotes the shopper" in r for r in screen_reasons(quoted, material))


def test_the_shoppers_own_words_are_stripped_of_money_before_they_reach_the_writer() -> None:
    """A buyer's budget is still a number, and rule A is not a rule about the writer's discipline."""
    body = request_body(
        "auc-M",
        query="a merino mid-layer under $120, ideally 20% off",
        preferences=[{"field": "material", "direction": "prefer", "weight": 1.0}],
        buckets={"region": "US-CA"},
    )
    capture = Capturing()
    answered(body, llm=capture)
    tail = wire_key(capture.prompts[-1])[1]
    assert "merino mid-layer" in tail, "sanity: the rest of the ask survived"
    assert "$120" not in tail and "20%" not in tail and "off" not in tail.split()


# =============================================================================================
# 7. Rule F — the pitch is an artifact, not a function
# =============================================================================================


def test_a_stored_pitch_replays_verbatim_and_calls_no_model() -> None:
    """R15/S3. A pitch that regenerated on replay would reproduce something *else* — equally
    plausible, and therefore a silent divergence rather than a visible one."""
    baseline = answered(shopper_a())
    stored = "The pitch that was actually served, months ago, by a model nobody can re-run."

    replaying = context()
    replaying[SERVED_PITCHES_KEY] = {baseline.offer.bid_offer_id: stored}

    copywriter = Capturing("a fresh pitch this replay must not use")
    answer = answered(shopper_a(), replaying, llm=copywriter)

    assert answer.message == stored, "the stored artifact must win over the generator"
    assert copywriter.prompts == [], "and the model must not even be asked"
    assert answer.offer.model_dump(mode="json") == baseline.offer.model_dump(mode="json")


def test_a_stored_pitch_for_a_different_offer_does_not_leak_into_this_one() -> None:
    """Keyed on `bid_offer_id`, which is injective over (auction, store, product)."""
    replaying = context()
    replaying[SERVED_PITCHES_KEY] = {"bidoffer:some-other-auction": "not this bid's pitch"}
    answer = answered(shopper_a(), replaying)
    assert answer.message == answered(shopper_a()).message


@pytest.mark.parametrize("stored", [None, "", "   ", 7, {"text": "x"}, []])
def test_a_malformed_served_pitches_entry_is_simply_no_stored_pitch(stored: Any) -> None:
    """A context that has never been through a replay must behave exactly as it does today."""
    replaying = context()
    replaying[SERVED_PITCHES_KEY] = {"bidoffer:x": stored}
    assert answered(shopper_a(), replaying).message == answered(shopper_a()).message


def test_the_served_pitch_is_the_logged_pitch() -> None:
    """The artifact the merchant's audit log holds and the one the exchange received are the
    same bytes — otherwise "replayed as an input" has two different inputs to choose from."""
    app = create_app()
    configure_solicitation(app, context=context())
    with TestClient(app) as client:
        body = client.post("/v1/bid-requests", json=shopper_a()).json()
    hosted = advocate(app)
    assert hosted is not None and hosted.log is not None
    logged = list(hosted.log.entries)[-1]
    assert logged.answer.message == body["message"]


# =============================================================================================
# 8. A scraped shop has no advocate — shadow and killed stores leak no pitch
# =============================================================================================


@pytest.mark.parametrize(
    ("activation", "reason"),
    [("shadow", NOT_ACTIVATED_REASON), ("killed", KILLED_REASON)],
)
def test_a_store_that_may_not_bid_leaks_no_pitch(activation: str, reason: str) -> None:
    """R7 and R9. The pitch is computed in every mode — a shadow log a merchant reads to decide
    whether to activate must show what activation would actually publish — and it leaves the
    building in exactly one."""
    response = served(shopper_a(), context(activation=activation))
    assert response.status_code == 204
    assert response.headers[DECLINE_REASON_HEADER] == reason
    assert response.content == b"", "a 204 has no body, so there is no pitch on the wire"
    assert b"merino" not in response.content.lower()


def test_a_shadow_store_still_computes_the_pitch_into_its_own_audit_log() -> None:
    """The other half: shadow is "do all the work and post nothing", not "do less work"."""
    app = create_app()
    configure_solicitation(app, context=context(activation="shadow"))
    with TestClient(app) as client:
        assert client.post("/v1/bid-requests", json=shopper_a()).status_code == 204
    hosted = advocate(app)
    assert hosted is not None and hosted.log is not None
    entry = list(hosted.log.entries)[-1]
    assert entry.submitting is False
    assert entry.answer.message and "merino wool" in entry.answer.message


# =============================================================================================
# 9. Honest traffic — every bid path that worked before still works, unchanged
# =============================================================================================


def test_the_hook_call_log_is_unchanged_by_the_pitch() -> None:
    """The pitch reads the bid's OWN claims and calls no hook. If it called one, S5's audit trail
    would grow a call nothing asked for, and `test_runtime.py` pins that log exactly."""

    def log_of(**kwargs: Any) -> list[tuple[str, str]]:
        hooks = ToolHooks(context())
        bid(shopper_a(), context(), hooks=hooks, **kwargs)
        return [(call.hook, call.subject) for call in hooks.call_log]

    assert log_of(llm=None) == log_of(llm=Capturing()) == log_of(llm=Exploding())


def test_a_store_with_an_empty_catalogue_declines_exactly_as_it_did() -> None:
    """No facts, no pitch, and — the part that matters — no change to the decline."""
    empty = context()
    empty["catalog"] = {}
    answer = bid(shopper_a(), empty, llm=Capturing())
    assert is_decline(answer)
    assert answer.reason.value == "no_priced_product"
    assert not hasattr(answer, "message"), "a Decline carries no pitch field at all"


def test_a_bid_with_nothing_pitchable_carries_no_message_and_is_still_a_bid() -> None:
    """The R10 shape: a catalogue that prices a product and says nothing else about it.

    `None` is a legitimate answer — the store bids at list price with its commitments, exactly
    as it did before this feature existed — and it is what a store with nothing to say gets.
    """
    bare = context()
    bare["catalog"] = {"prod-bare": {"product_ref": "prod-bare", "list_price": LIST_PRICE}}
    bare["live_state"] = {}
    bare["envelope"] = {**bare["envelope"], "standing_commitments": []}
    body = request_body(
        "auc-bare",
        query="anything",
        preferences=[],
        buckets={},
        hard_constraints=[],
    )
    answer = bid(body, bare, llm=Capturing())
    assert not is_decline(answer)
    assert answer.message is None
    assert answer.offer.unit_price == LIST_PRICE


def test_the_bid_a_hosted_agent_produces_is_unchanged_except_for_the_message() -> None:
    """The strongest honest-traffic statement available: dump the whole bid twice, with and
    without a copywriter, and require that the ONLY field that moved is `message`."""
    without = answered(shopper_a(), llm=None).model_dump(mode="json")
    with_model = answered(
        shopper_a(), llm=Capturing("Merino wool, ready within two business days.")
    ).model_dump(mode="json")
    assert without["message"] != with_model["message"]
    without.pop("message"), with_model.pop("message")
    assert without == with_model


# =============================================================================================
# 10. The screen is load-bearing — shown failing, and shown passing a real control
# =============================================================================================


def test_the_screen_accepts_an_honest_pitch() -> None:
    """The positive control. A screen that rejects everything grades nothing, and would have
    made every test above pass for the wrong reason."""
    material = material_for(assemble_context(shopper_a(), context()), _claims_of(shopper_a()))
    honest = (
        "Merino wool, which is what you asked for, and it leaves us within two business days. "
        "Returns are free for thirty days if it is not right."
    )
    assert screen_reasons(honest, material) == ()
    assert screen(honest, material) == honest


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Merino wool at 20% off.", "monetary"),
        ("The best merino wool anywhere.", "unsupportable"),
        ("double:store_agent:0123456789abcdef", "not prose"),
        ("Merino wool <script>alert(1)</script> ships fast today", "forbidden characters"),
        ("Merino wool. " * 40, "too long"),
        ("Shipping today for everyone who orders before noon.", "ungrounded"),
    ],
    ids=["money", "superlative", "marker", "markup", "long", "ungrounded"],
)
def test_each_screen_rule_refuses_a_text_that_violates_only_it(text: str, expected: str) -> None:
    """ARMED, rule by rule, so a screen that silently stopped checking one is visible."""
    material = material_for(assemble_context(shopper_a(), context()), _claims_of(shopper_a()))
    reasons = " ".join(screen_reasons(text, material))
    assert expected in reasons, f"{text!r} was refused for {reasons!r}, not for {expected!r}"
    assert screen(text, material) is None


def test_the_fallback_pitch_survives_the_screen_it_is_the_fallback_for() -> None:
    """Otherwise the failure path serves nothing, and rule D's promise is half a promise."""
    for body in (shopper_a(), shopper_b()):
        material = material_for(assemble_context(body, context()), _claims_of(body))
        text = fallback_pitch(material)
        assert text and screen_reasons(text, material) == (), text


def test_the_screen_is_not_a_verifier_and_this_file_does_not_pretend_it_is() -> None:
    """The honest limit of the emitting-side screen, measured rather than glossed.

    A reply that invents an attribute *while also* naming a supported one — "single-origin
    merino, hand-finished in Scotland" — is NOT refused here. The grounding check asks whether
    the pitch argues from this store's material at all; it is not a claim-by-claim verifier, and
    building one on the emitting side would be building the thing the exchange must not trust an
    emitter to build. R18 does that job adversarially, against the catalogue snapshot, and D53
    routes a `contradicted` verdict into `catalog_claim_accuracy` — so the store that let this
    through pays for it in trust.

    What this screen IS: the emitting side declining to send the things it can tell are
    unsupportable by inspection — money, superlatives, absolutes, buyer identity, markup, and
    prose that argues from nothing. Stating the boundary in a test means a later reader cannot
    mistake it for verification.
    """
    material = material_for(assemble_context(shopper_a(), context()), _claims_of(shopper_a()))
    invented = "Single-origin merino, hand-finished in Scotland by people we know."
    assert screen_reasons(invented, material) == (), (
        "if this now has a reason, the screen has grown a capability and this test should be "
        "rewritten to describe the new boundary rather than deleted"
    )
    assert answered(shopper_a(), llm=Capturing(invented)).message == invented


def _claims_of(body: dict[str, Any]) -> list[Any]:
    """The claim material of the bid this request actually produces."""
    answer = bid(body, context())
    assert not is_decline(answer)
    return [*answer.claims, *answer.offer.commitments]


# =============================================================================================
# 11. The prompt is assembled cache-first (C4)
# =============================================================================================


def test_the_static_contract_is_the_cacheable_prefix_and_the_shopper_is_the_tail() -> None:
    """Reverse the two and every request has a unique prefix, so nothing is ever a cache hit —
    a failure that is invisible in output and shows up only as latency and spend."""
    a = pitch_prompt(
        material_for(assemble_context(shopper_a(), context()), _claims_of(shopper_a()))
    )
    b = pitch_prompt(
        material_for(assemble_context(shopper_b(), context()), _claims_of(shopper_b()))
    )

    assert a.static_context == b.static_context == PITCH_CONTRACT
    assert a.prefix_digest == b.prefix_digest, "two shoppers must share one cacheable prefix"
    assert a.dynamic_tail != b.dynamic_tail
    assert a.text.startswith(PITCH_CONTRACT)
    assert a.dynamic_tail.split("\n", 1)[0].isupper(), (
        "a recorded user turn must open with a section label (D21 fixture validation)"
    )


def test_the_request_carries_the_shopper_and_the_supported_facts_and_nothing_else() -> None:
    """A reader can see exactly what the writer was told. This is the whole of rules A and E,
    read off the artifact rather than inferred."""
    capture = Capturing()
    answered(shopper_b(), llm=capture)
    tail = wire_key(capture.prompts[-1])[1]

    assert "store: store-alpha" in tail
    assert "they are weighing: ships_within, free_returns" in tail
    assert "coarse buckets (no identity is known)" in tail
    assert "- commitment | ships_within = 2 business days" in tail
    assert "- catalogue | material = merino wool" in tail
    assert "- availability | in_stock = yes" in tail
    assert "price" not in tail and "discount" not in tail
    assert PSEUDONYM not in tail


def test_a_bid_request_built_from_the_protocol_model_and_from_a_dict_pitch_identically() -> None:
    """The runtime is shape-tolerant; the pitch must not be the one place that is not."""
    body = shopper_a()
    from_dict = answered(body, llm=None)
    from_model = answered(BidRequest.model_validate(body), llm=None)  # type: ignore[arg-type]
    assert from_dict.message == from_model.message
