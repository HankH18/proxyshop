"""S1, end to end: one scripted run through the starting path (T-082).

SPEC S1 — *intent → ≤3 clarifications → parallel bids → shortlist → accepted offer → code
created & validated → checkout completes through the CheckoutProvider port → webhook + pixel
reconciled → ledger event → trust update* — run once, offline, against the real components.
The run itself lives in ``e2e/support/s1/flow.py`` and is driven by the session-scoped
``s1_run`` fixture; this file only asserts.

**One run, many assertions, on purpose.** "One scripted run proves the full S1 flow" is not
twenty tests each standing up its own world: twenty runs prove twenty different things and
none of them is the chain. Every test below reads the same pass, so a break anywhere in the
chain is visible from every angle at once.

What a green here does and does not mean
----------------------------------------
It means the stages listed in ``flow.py``'s docstring ran, in order, as the product's own
code, and that the ledger they produced is exactly the multiset D34 pins — no missing event,
and no extra one. Every stage is the real entry point; the only doubles in the run are the
offline LLM double the buyer's clarifier is *designed* to take (D19), and a code-creator
recorder that the redirect path never calls and which the run asserts was never called.

It does **not** mean every event in the ledger came from a production emitter — but the four
that used to be written by the driver do now.
``test_the_run_records_the_three_auction_kinds_from_the_exchange_itself`` is the record of
``bid_placed``, ``shown`` and ``claim_verified`` becoming the served auction's own output, and
``test_the_pixel_row_was_written_by_the_served_collector_and_not_by_the_run`` is the record of
``checkout_pixel`` becoming the served merchant collector's. Both are what stop this driver
quietly writing them again. Nor does it mean the checkout seam is whole: the exchange's
``checkout_token`` and the merchant's are unrelated values, and
``test_the_checkout_token_seam_has_no_production_binding`` pins that defect where a reader
will find it.

Markers: none. Only ``docker``/``graph``/``slow``/``needs_model`` are registered
(``pyproject.toml``) and this run needs no datastore, so it must never be skippable — a
skipped e2e is indistinguishable from a passing one in the metrics.
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The kinds the run produces, and the count of each. Every one of the eighteen frozen
#: LedgerEvent kinds appears — a kind the run must NOT produce is written here as 0 rather
#: than left out, because "not in the dict" and "happened zero times" are different claims
#: and only the second one is checkable.
EXPECTED_ZERO_KINDS = (
    "refund",
    "order_fulfilled",
    "feedback",
    "offer_integrity",
    "blacklisted",
    "blacklist_expired",
    "policy_event",
)


# --------------------------------------------------------------------------------------
# 0 — the run is THIS worktree's code
# --------------------------------------------------------------------------------------
def test_every_component_under_test_is_this_worktrees_code(s1_run) -> None:
    """The `.pth` guard, and it is not paranoia.

    ``site-packages/_proxyshop.pth`` puts a *different* checkout's source tree and its
    ``.pkgroot`` on ``sys.path`` for every process that uses this venv, whatever
    ``PYTHONPATH`` says. An e2e that imported the other tree's code would grade the wrong
    repository and report a confident green about a change it never executed. Both sides are
    resolved before comparison, so a symlinked worktree cannot slip past on spelling.
    """
    assert s1_run.module_files, "the run recorded no module paths, so nothing was checked"
    root = str(REPO_ROOT.resolve())
    strays = {
        name: path for name, path in s1_run.module_files.items() if not path.startswith(root + "/")
    }
    assert not strays, (
        "the S1 run drove code from OUTSIDE this worktree — its result says nothing about "
        f"this branch. Expected everything under {root}; got: {strays}"
    )


# --------------------------------------------------------------------------------------
# 1 — acceptance 1: the exact per-kind multiset over the complete LedgerEvent enum (D34)
# --------------------------------------------------------------------------------------
def test_the_run_covers_every_frozen_ledger_kind_with_an_exact_count(s1_run, s1_fixture) -> None:
    """T-082 acceptance 1 — an exact multiset over all eighteen kinds, not a subset.

    Each count below is derived from the run rather than hard-coded, and then cross-checked
    against the run fixture. That order matters: a count taken only from the run would agree
    with itself no matter what the exchange did, and a count taken only from the fixture
    would not notice the chain producing the right *number* of the wrong events.
    """
    from contracts.ledger import LEDGER_EVENT_KINDS

    expected = s1_fixture["expected"]
    counts = s1_run.kind_counts
    shortlist_slots = len(s1_run.shortlist["slots"])

    # `bid_placed == n_stores_solicited`, and the run's solicitation is what the fixture said
    # it would be — so a silently shrunken roster cannot satisfy this by agreeing with itself.
    assert s1_run.solicited == expected["solicited"], (
        f"the exchange solicited {s1_run.solicited}, not {expected['solicited']}"
    )
    assert shortlist_slots == expected["shortlist_slots"]
    assert len(s1_run.asserted_claims) == expected["asserted_claims"]

    want = {
        "bid_placed": len(s1_run.solicited),
        "shown": shortlist_slots,
        "claim_verified": len(s1_run.asserted_claims),
        "auction_opened": 1,
        "auction_closed": 1,
        "accepted": 1,
        "code_created": 1,
        "checkout_redirect": 1,
        "checkout_pixel": 1,
        "order_paid": 1,
        "reconciled": 1,
    }
    want.update({kind: 0 for kind in EXPECTED_ZERO_KINDS})

    assert set(want) == set(LEDGER_EVENT_KINDS), (
        "this assertion must cover the COMPLETE frozen enum; it is out of step with "
        f"contracts.ledger.LEDGER_EVENT_KINDS. Missing: {sorted(set(LEDGER_EVENT_KINDS) - set(want))}; "
        f"unknown: {sorted(set(want) - set(LEDGER_EVENT_KINDS))}"
    )

    got = {kind: counts.get(kind, 0) for kind in want}
    assert got == want, (
        "the S1 run's ledger is not the exact multiset S1 pins.\n"
        f"  too few:  { {k: (want[k], got[k]) for k in want if got[k] < want[k]} }\n"
        f"  too many: { {k: (want[k], got[k]) for k in want if got[k] > want[k]} }"
    )


def test_no_event_outside_the_frozen_vocabulary_reached_the_ledger(s1_run) -> None:
    """A kind nobody published is a protocol split, not a new feature."""
    from contracts.ledger import LEDGER_EVENT_KINDS

    unknown = sorted(set(s1_run.kind_counts) - set(LEDGER_EVENT_KINDS))
    assert not unknown, f"the run wrote kinds outside the frozen D34 vocabulary: {unknown}"


def test_the_runs_ledger_is_a_verifiable_hash_chain(s1_run) -> None:
    """Every event of the run appends to the chained store, and the chain verifies.

    ``InMemoryEventStore`` refuses an unknown kind and an unknown top-level field before it
    will chain anything, so this is also the assertion that each event the run produced is a
    well-formed ``LedgerEvent`` and not merely a dict with a plausible shape.

    It is NOT the assertion that the chain is tamper-evident. Measured: this test stays green
    with ``verify_chain`` replaced by one that always answers ``ok``. Tamper-evidence is held
    by ``test_a_tampered_ledger_event_breaks_the_chain_it_is_in``, which edits a payload and
    requires the verifier to name the break.
    """
    report = s1_run.chain
    assert report.get("ok") is True, f"the run's ledger chain does not verify: {report}"
    assert int(report.get("length", 0)) == len(s1_run.events)


# --------------------------------------------------------------------------------------
# 2 — acceptance 2: the shortlist carried a fallback bid AND a hosted bid
# --------------------------------------------------------------------------------------
def test_the_shortlist_carried_both_a_fallback_and_a_hosted_bid(s1_run, s1_fixture) -> None:
    """T-082 acceptance 2 — R10's two ways for a store to be represented, both shortlisted.

    "fallback" and "hosted" are an *exchange-side* distinction, not a store-agent setting:
    ``BidEntry.fallback`` is True when the exchange had to quote a store's catalogue list
    price because the store's agent said nothing, and False when a real agent answered inside
    the bid window. The shortlist slot itself carries neither flag, so each slot is joined
    back to the entry the exchange collected.
    """
    expected = s1_fixture["expected"]
    slots = s1_run.shortlist["slots"]
    assert slots, "the ranker produced an empty shortlist"

    by_kind: dict[bool, list[str]] = {True: [], False: []}
    for slot in slots:
        entry = s1_run.entry_for_bid_ref(slot["bid_ref"])
        by_kind[bool(entry["fallback"])].append(entry["store_id"])

    assert sorted(by_kind[True]) == sorted(expected["fallback_stores"]), (
        f"shortlisted fallback bids were {sorted(by_kind[True])}, expected "
        f"{sorted(expected['fallback_stores'])}"
    )
    assert sorted(by_kind[False]) == sorted(expected["hosted_stores"]), (
        f"shortlisted hosted bids were {sorted(by_kind[False])}, expected "
        f"{sorted(expected['hosted_stores'])}"
    )

    # The fallback is the exchange's own construction, not something the run handed it: the
    # silent store never answered, so its reason must be the exchange's.
    (silent,) = expected["fallback_stores"]
    assert s1_run.entry(silent)["fallback_reason"] == "no_response", (
        "the fallback must be the exchange representing a store that stayed silent, not a "
        f"bid the run wrote for it: {s1_run.entry(silent)}"
    )
    assert silent not in s1_run.bids, "the silent store must not have produced a bid at all"

    # …and the hosted bids came from the real store agent, priced off its own catalogue.
    for store_id in expected["hosted_stores"]:
        bid = s1_run.bids[store_id]
        row = next(s for s in s1_fixture["stores"] if s["store_id"] == store_id)
        assert bid["store_id"] == store_id
        assert bid["offer"]["unit_price"] == pytest.approx(row["list_price"])
        assert bid["claims"], "a hosted bid with no claims is not a pitch"


# --------------------------------------------------------------------------------------
# 3 — acceptance 3: the run completes offline
# --------------------------------------------------------------------------------------
def test_the_whole_run_completed_offline(s1_run) -> None:
    """T-082 acceptance 3 — no datastore, no network beyond loopback, no model.

    The socket guard the root conftest arms session-wide (``--disable-socket``, allow-hosts
    ``127.0.0.1,localhost,::1``) is what enforces this: the run could not have reached
    anything off-loopback and still be here. What this test adds is that the run really did
    reach the *merchant*, in-process — an offline run that skipped the checkout entirely
    would also be offline, and would prove nothing.
    """
    import socket

    from pytest_socket import SocketConnectBlockedError

    with pytest.raises(SocketConnectBlockedError):
        socket.create_connection(("10.255.255.1", 80), timeout=0.5)

    assert s1_run.completion.get("order_id"), "no order was created at the merchant"
    assert s1_run.completion.get("pixel_event_posted") is True, "the web pixel never fired"
    assert s1_run.pixel_observation is not None, "the merchant collector parsed no beacon"
    assert s1_run.webhook_decision.accepted, "the paid webhook was not accepted"


def test_the_paid_webhook_was_authenticated_rather_than_assumed(s1_run) -> None:
    """The run's own `orders/paid` delivery was accepted by the real verifier.

    **This test does not prove the signature was checked, and its docstring used to claim it
    did.** "``handle_delivery`` answers 401 on a bad signature, so an accepted decision is
    proof the stub signed the exact bytes" is exactly backwards: accepting a VALID signature
    is the one thing an always-true verifier also does. Measured, not reasoned — this test
    stays green with ``merchant_svc.install.webhooks.verify`` replaced by ``lambda *_: True``
    and with ``handle_delivery`` replaced by one that always answers 200.

    What it is still worth: it pins that the honest path reaches 200 with an event, which is
    the positive control the adversarial test needs in order to mean anything.
    ``test_a_tampered_unsigned_or_reserialised_paid_webhook_is_refused`` is what actually
    holds the property, by driving deliveries that must be REFUSED.
    """
    decision = s1_run.webhook_decision
    assert decision.status_code == 200, f"{decision.status_code}: {decision.reason}"
    assert decision.event is not None


# --------------------------------------------------------------------------------------
# 4 — acceptance 4: the two release blockers
# --------------------------------------------------------------------------------------
def test_no_blacklisted_seller_was_eligible_anywhere_in_the_run(s1_run, s1_fixture) -> None:
    """T-082 acceptance 4, first blocker — checked at every gate the store could pass.

    One absence assertion per gate, because they fail independently: the solicitation gate
    can hold while the ranker's does not, and a store excluded from the shortlist can still
    be accepted if ``accept`` never asks.
    """
    from exchange.eligibility import BLACKLISTED, StaticSellerEligibility, read_eligibility

    (blocked,) = s1_fixture["expected"]["denied"]

    source = StaticSellerEligibility(
        {store["store_id"]: store["eligibility"] for store in s1_fixture["stores"]}
    )
    assert read_eligibility(source, blocked).status == BLACKLISTED

    assert blocked not in s1_run.solicited, "a blacklisted store was solicited for bids"
    assert blocked not in [entry["store_id"] for entry in s1_run.entries], (
        "a blacklisted store reached the collected entries, so it could be ranked"
    )
    assert blocked not in s1_run.bids, "a blacklisted store's agent was asked to bid"
    denied = {denial["store_id"]: denial for denial in s1_run.denied}
    assert blocked in denied and denied[blocked]["status"] == BLACKLISTED, (
        f"the blacklisted store must be recorded as denied, with a reason: {s1_run.denied}"
    )

    assert blocked not in [candidate["store_id"] for candidate in s1_run.candidates]
    shortlisted = {
        s1_run.entry_for_bid_ref(slot["bid_ref"])["store_id"] for slot in s1_run.shortlist["slots"]
    }
    assert blocked not in shortlisted, "a blacklisted store was shown to the buyer"

    accepted_store = next(event for event in s1_run.events if event["kind"] == "accepted")[
        "store_id"
    ]
    assert accepted_store != blocked, "the buyer accepted a blacklisted store's offer"

    for event in s1_run.events:
        assert event.get("store_id") != blocked, (
            f"a blacklisted store reached the ledger through a {event['kind']} event"
        )


def test_no_off_domain_checkout_url_was_returned(s1_run, s1_fixture) -> None:
    """T-082 acceptance 4, second blocker — the permalink the buyer got is on the seller's
    REGISTERED domain, and the guard is exercised against hosts that defeat weaker checks.

    The positive half alone would be satisfied by a guard that never runs, so the hostile
    corpus is the load-bearing part: a plain rival domain, two suffix spoofs that defeat
    ``endswith``/``in``, a userinfo spoof that defeats ``startswith``, and a subdomain. Each
    must be refused *before* a single-use discount code exists — a code minted for an
    off-domain checkout is already a loss even if the permalink is then withheld.
    """
    from exchange.accept import accept
    from exchange.checkout import OffDomainCheckout
    from exchange.checkout.sellers import StaticRegisteredDomains

    from e2e.support.s1.flow import _auction_record, _RecordingCodeCreator, store_domain

    winner = s1_fixture["expected"]["winning_store"]
    seller = store_domain(winner)

    assert s1_run.accept_result.domain_verified is True, (
        "the accept ran without a platform-verified registered domain, so its host check "
        "compared the store's own claim against itself"
    )
    assert urlsplit(s1_run.permalink_url).hostname == seller, (
        f"the buyer was handed {s1_run.permalink_url!r}, which is not on {seller!r}"
    )
    for event in s1_run.events:
        url = (event.get("payload") or {}).get("permalink_url")
        if url:
            assert urlsplit(str(url)).hostname == seller, (
                f"a {event['kind']} event carried an off-domain checkout URL: {url!r}"
            )

    winning_bid_ref = s1_run.shortlist["slots"][0]["bid_ref"]
    domains = StaticRegisteredDomains(
        {store["store_id"]: store_domain(store["store_id"]) for store in s1_fixture["stores"]}
    )
    hostile = {
        "plain other domain": "https://attacker.tld/cart/1:1?discount=PSX-ABCDEFGH",
        "suffix spoof": f"https://evil-{seller}.attacker.tld/cart/1:1?discount=PSX-ABCDEFGH",
        "glued suffix spoof": f"https://evil-{seller}/cart/1:1?discount=PSX-ABCDEFGH",
        "userinfo spoof": f"https://{seller}@attacker.tld/cart/1:1?discount=PSX-ABCDEFGH",
        "subdomain": f"https://checkout.{seller}/cart/1:1?discount=PSX-ABCDEFGH",
    }
    for label, url in hostile.items():
        record = copy.deepcopy(_auction_record(s1_run, winning_bid_ref))
        for bid in record["bids"]:
            if bid["bid_id"] == winning_bid_ref:
                bid["offer"]["checkout_url"] = url
        creator = _RecordingCodeCreator()
        try:
            result = accept(
                record, winning_bid_ref, creator, "redirect", registered_domains=domains
            )
        except OffDomainCheckout:
            assert not creator.calls, (
                f"{label}: accept refused, but had already created a discount code"
            )
            continue
        permalink = getattr(result, "permalink_url", None)
        # The message says "returned at all", not "returned an attacker URL", because that is
        # what a disabled guard actually produces here. Measured with
        # `exchange.checkout.domain.assert_on_domain` neutered: `accept` does NOT hand back
        # the attacker host — the provider rebuilds the permalink from the REGISTERED domain.
        # The loss is that a live single-use code was minted for an off-domain offer. So the
        # thing to refuse is the acceptance itself, and `creator.calls` below is the half that
        # names the harm.
        assert not permalink, (
            f"{label}: accept SUCCEEDED on a bid whose checkout URL is outside the registered "
            f"domain {seller!r} ({url}); it returned {permalink!r}. A code minted for an "
            f"off-domain offer is already a loss even when the permalink is rebuilt on-domain"
        )
        assert not creator.calls, (
            f"{label}: the domain must be validated before a single-use code is created"
        )


# --------------------------------------------------------------------------------------
# 5 — the chain, stage by stage: each link really is the previous link's output
# --------------------------------------------------------------------------------------
def test_the_buyer_clarified_within_the_published_ceiling(s1_run) -> None:
    """S1's "≤3 clarifications". A clarifier that asked nothing is also a failure here."""
    outcome = s1_run.clarification
    questions = list(outcome.questions)
    assert 1 <= len(questions) <= 3, f"S1 caps clarification at 3 questions; got {questions}"
    assert outcome.confirmed is False, (
        "clarify() must never self-confirm — confirmation is the buyer's separate act"
    )


def test_the_auction_the_buyer_confirmed_is_the_auction_that_ran(s1_run) -> None:
    """The buyer's confirmation created the auction every later stage is keyed to."""
    assert s1_run.auction_id, "no auction was created"
    assert s1_run.auction_response["state"] == "closed", (
        f"the auction did not close: {s1_run.auction_response['state']}"
    )
    keyed = {event["auction_id"] for event in s1_run.events if event.get("auction_id") is not None}
    assert keyed == {s1_run.auction_id}, (
        f"the run's ledger spans more than one auction: {sorted(keyed)}"
    )


def test_every_shortlist_slot_was_shown_exactly_once(s1_run) -> None:
    """`shown` is per slot, and the bid refs must be the shortlist's own."""
    slots = {slot["bid_ref"]: slot["slot"] for slot in s1_run.shortlist["slots"]}
    shown = [event for event in s1_run.events if event["kind"] == "shown"]
    assert Counter(event["payload"]["bid_ref"] for event in shown) == Counter(slots.keys()), (
        "the shown events do not correspond one-for-one to the shortlist slots: "
        f"shown {[event['payload']['bid_ref'] for event in shown]} vs slots {sorted(slots)}"
    )
    for event in shown:
        assert event["payload"]["slot"] == slots[event["payload"]["bid_ref"]]


def test_every_shortlist_slot_shows_the_product_and_the_price_it_is_offering(
    s1_run, s1_fixture
) -> None:
    """R2's slot: PRODUCT and PRICE, on the object the buyer is actually served.

    **This test exists because deleting the code that builds those fields was SILENT.** Driven
    as a sabotage: ``ranking/serving.py``'s ``_with_offer_fields`` — the join that puts the
    candidate's offer onto the rank row's slot — was removed, ``POST /auctions`` answered three
    slots carrying ``product: null`` and ``price: null``, and the whole S1 suite stayed green.
    A shortlist slot with no product and no price is not a shortlist; a buyer cannot be shown
    it, and R2 names both fields.

    Both doors are checked, because the published contract says they serve the same object:
    the inline ``shortlist`` on the 201, and ``GET /auctions/{auction_id}/shortlist``, which
    re-validates through the pinned ``Shortlist`` model.

    Values, not merely presence. Each slot's price is joined back to the entry the exchange
    collected for that store, so a slot showing *a* price rather than *this bid's* price fails
    here — including the R10 fallback, whose price is the roster's list price because the
    exchange manufactured the offer.
    """
    slots = s1_run.shortlist["slots"]
    assert slots, "the ranker produced an empty shortlist"
    assert s1_run.served_shortlist == s1_run.shortlist, (
        "GET /auctions/{auction_id}/shortlist and the POST body disagree about the same "
        "auction's shortlist"
    )

    by_store = {store["store_id"]: store for store in s1_fixture["stores"]}
    for slot in slots:
        entry = s1_run.entry_for_bid_ref(slot["bid_ref"])
        store_id = entry["store_id"]
        assert slot["product"] is not None, (
            f"slot {slot['slot']!r} for {store_id} shows no product; R2 wants the buyer to see "
            "WHICH catalogue thing is being offered"
        )
        assert slot["product"]["product_ref"] == by_store[store_id]["product_ref"]
        assert slot["price"] is not None, f"slot {slot['slot']!r} for {store_id} shows no price"
        assert slot["price"]["unit_price"] == pytest.approx(entry["unit_price"])
        assert slot["price"]["total_price"] == pytest.approx(entry["total_price"])
        assert slot["price"]["currency"] == "USD"

    # …and the fallback's price really is the roster's list price, which is what makes the
    # join above a check on the exchange's own construction rather than on the store's reply.
    (silent,) = s1_fixture["expected"]["fallback_stores"]
    silent_slot = next(
        slot for slot in slots if s1_run.entry_for_bid_ref(slot["bid_ref"])["store_id"] == silent
    )
    assert silent_slot["price"]["unit_price"] == pytest.approx(by_store[silent]["list_price"])


def test_the_ranking_moved_on_the_features_the_exchange_computed(s1_run, s1_fixture) -> None:
    """The published five-term formula ran with real inputs, not with five neutrals.

    **Also written because deleting the producer was SILENT.** Driven as a sabotage:
    ``ranking/serving.py``'s ``attach_features`` call was removed, every feature became absent
    on every served candidate, each took its published ``when_absent`` neutral, ``rank_score``
    collapsed to the one-term ``0.4 + 0.2*trust`` — identical for all three stores — and the
    S1 suite stayed green, because nothing here read a score or a component.

    The discriminator this run actually has is ``verified_claim_ratio``: two hosted stores made
    claims the exchange checked against its own catalogue and verified, and the silent store's
    R10 fallback carries no claims at all, so it takes the neutral. If the exchange's own
    verdicts are not reaching the formula, those two numbers are equal — which is precisely
    what the sabotage produced.

    ``components`` and not just ``rank_score``, because a score can coincide; the components
    are the published per-term breakdown and they say WHICH term did the work.
    """
    components = {row["store_id"]: row["components"] for row in s1_run.ranked}
    scores = {row["store_id"]: row["rank_score"] for row in s1_run.ranked}
    (silent,) = s1_fixture["expected"]["fallback_stores"]

    assert set(components) == {entry["store_id"] for entry in s1_run.entries}
    for store_id in s1_fixture["expected"]["hosted_stores"]:
        assert (
            components[store_id]["verified_claim_ratio"]
            > components[silent]["verified_claim_ratio"]
        ), (
            f"{store_id} presented claims this exchange verified and scored no better on "
            f"verified_claim_ratio than {silent}, which presented none: "
            f"{components[store_id]} vs {components[silent]}"
        )
        assert scores[store_id] > scores[silent], (
            f"the store that answered with a verified pitch ranked no higher than the store "
            f"that never answered: {scores}"
        )
        # And it is not charged for saying MORE than the exchange can check. Each hosted
        # agent publishes a `policy_action` claim — its own record of the discount decision,
        # which `trust.scoring.claim_dimension` refuses to route to any trust dimension and
        # which no catalogue was ever going to carry a reading for. Graded as a failed
        # product claim it took the ratio to 3/4; a store publishing three such records
        # beside three true ones would have landed on exactly the 0.5 neutral the SILENT
        # store gets for free, and a fourth would have put an honest store below silence.
        #
        # **The number this pins moved with `RANKING_FEATURES_VERSION` 2.0.0 and the reason is
        # in the published definition, not here.** `verified_claim_ratio` is no longer the
        # share of decided claims verified; it is `1 - Π(1 - gain_i)` over the verified claims
        # whose key lands on something THIS buyer asked about (`contracts.ranking
        # .EVIDENCE_GAIN_BY_RELEVANCE`). A diminishing-returns aggregation never reaches 1.0
        # by construction, so "twice the neutral" is not a value any store can hold under
        # 2.0.0 and the old assertion pinned an arithmetic that no longer exists.
        #
        # What is pinned instead is STRICTLY TIGHTER: the exact published value of this run,
        # which is one relevant verified claim and nothing else. S1's buyer states one ask
        # this exchange can name — a `price` preference — and of the four claims each hosted
        # agent makes (`list_price`, `in_stock`, `units_left`, `policy_action`) exactly one,
        # `list_price`, is on that axis. So the term is one `term_scored_ask` gain: anything
        # counting `policy_action` against the store, or dropping `list_price`, or minting a
        # penalty, lands somewhere else and this fails. The `> silent` assertion above is the
        # control that the term still separates a store that answered from one that did not.
        from contracts.ranking import DEFAULT_RANKING_WEIGHTS, EVIDENCE_GAIN_BY_RELEVANCE

        w_e = DEFAULT_RANKING_WEIGHTS.feature_weights["verified_claim_ratio"]
        assert components[store_id]["verified_claim_ratio"] == pytest.approx(
            w_e * EVIDENCE_GAIN_BY_RELEVANCE["term_scored_ask"]
        ), (
            "a hosted store's verified_claim_ratio is not the published value of the one "
            "buyer-relevant claim it proved, so something it said that this exchange could "
            f"not check was counted against it: {components[store_id]} vs {components[silent]}"
        )
        assert components[store_id]["verified_claim_ratio"] > w_e * float(
            DEFAULT_RANKING_WEIGHTS.normalization.verified_claim_ratio_when_absent
        ), "a store that proved a buyer-relevant fact scored no better than the published neutral"
    # The exchange's OWN verdicts are what moved it — one `claim_verified` per counted claim,
    # and none for the store that made none.
    verdicts = Counter(
        str(event["store_id"])
        for event in s1_run.events
        if event["kind"] == "claim_verified" and event["payload"]["status"] == "verified"
    )
    for store_id in s1_fixture["expected"]["hosted_stores"]:
        assert verdicts[store_id] > 0
    assert verdicts[silent] == 0


def test_the_accepted_offer_is_the_top_shortlist_slot(s1_run, s1_fixture) -> None:
    """Acceptance follows the shortlist rather than reaching past it."""
    top = s1_run.shortlist["slots"][0]
    accepted = next(event for event in s1_run.events if event["kind"] == "accepted")
    assert accepted["payload"]["bid_ref"] == top["bid_ref"]
    assert accepted["store_id"] == s1_fixture["expected"]["winning_store"]


def test_the_redirect_path_minted_its_own_code_without_calling_the_merchant(s1_run) -> None:
    """D22/D45: ``SimulatedRedirectProvider`` mints locally; the code creator stays untouched.

    Worth asserting rather than assuming, because a provider that quietly fell through to the
    merchant client would still produce a green checkout — and would need a merchant the
    starting path is supposed to work without.
    """
    assert s1_run.minted_code, "no discount code was minted"
    assert s1_run.minted_code in s1_run.permalink_url, (
        "the permalink does not carry the code that was minted for it"
    )
    assert s1_run.completion["discount_code"] == s1_run.minted_code, (
        "the order the merchant closed redeemed a different code than the exchange minted"
    )


def test_the_order_the_merchant_closed_honoured_the_offer_that_was_promised(s1_run) -> None:
    """The reconciliation verdict, which is the whole point of the pixel and the webhook."""
    (reconciled,) = [event for event in s1_run.events if event["kind"] == "reconciled"]
    payload = reconciled["payload"]
    assert payload["price_honored"] is True, f"the promised price was not honoured: {payload}"
    assert payload["discount_honored"] is True
    assert payload["pixel_missing"] is False, (
        "reconciliation recorded the beacon as missing, but the stub really posted one"
    )
    assert reconciled["store_id"] == s1_run.fixture["expected"]["winning_store"]
    assert str(payload["order_ref"]) == str(reconciled["order_ref"])
    assert str(s1_run.completion["order_id"]) in str(payload["order_ref"]), (
        "the reconciled verdict is not about the order the merchant actually created"
    )


def test_the_trust_projection_moved_on_the_evidence_the_run_produced(s1_run, s1_fixture) -> None:
    """S1's last link: the reconciled outcome and the verified claims reach trust.

    A snapshot that came back empty, or one whose store carries no dimension the run wrote
    to, would mean the ledger and the trust engine are two systems rather than one.
    """
    winner = s1_fixture["expected"]["winning_store"]
    stores = s1_run.trust_after.get("stores", s1_run.trust_after)
    assert stores, f"the trust projection produced nothing: {s1_run.trust_after}"

    entry = stores[winner] if isinstance(stores, dict) else None
    if entry is None:
        entry = next(row for row in stores if row["store_id"] == winner)
    assert entry["blacklisted"] is False
    dims = entry["dims"]
    for dim in ("price_honored", "discount_honored", "catalog_claim_accuracy"):
        assert dim in dims, f"the run wrote {dim} observations that trust did not project"


def test_the_blacklisted_store_stays_blacklisted_in_the_projection(s1_run, s1_fixture) -> None:
    """The seeded blacklist survives the projection — otherwise the ranker's filter is blind."""
    (blocked,) = s1_fixture["expected"]["denied"]
    assert s1_run.trust_before[blocked]["blacklisted"] is True


# --------------------------------------------------------------------------------------
# 6 — payload conformance, and the two places the product does not conform
# --------------------------------------------------------------------------------------
#: Kinds whose emitter writes a payload that does not satisfy the published D34 shape. These
#: are product defects, measured on this tree and reported by this ticket, NOT a licence for
#: the run's own events to be sloppy — every other kind below is held to the published shape.
#:
#: * ``checkout_pixel`` — the SHIPPED web pixel does not send the order total, so
#:   ``merchant_svc.composition.pixel_ledger_payload`` projects ``total_price: None`` and the
#:   published shape ``("checkout_token","client_id","total_price")`` is one key short.
#:   ``pixel/src/beacon.ts``'s ``COLLECTOR_BODY_KEYS`` is ``clientId, checkoutToken, orderId,
#:   discountApplications`` and the stub's ``shopify_stub.telemetry.collector_payload`` mirrors
#:   exactly those four, so the beacon this run drives carries no total either. The gap is in
#:   the EMITTER: the collector's door already accepts ``totalPrice``
#:   (``merchant_svc.collector.CARRIED_FIELDS``) and the projection already carries it, so a
#:   beacon that sends one conforms with no further change. This entry appeared the moment the
#:   run stopped hand-building the row — the hand-built one put the ORDER's total on it, which
#:   is the webhook's number and not the beacon's, so the driver was papering over a real
#:   defect in the emitter it was standing in for.
#:
#: Three kinds have left this set, each because an emitter was fixed and the test below went
#: red demanding the deletion — which is the whole point of that test existing:
#:
#: * ``code_created`` — the checkout port used to write ``{checkout_token, discount_code}``
#:   against a published ``("code","permalink_url","expires_at")``;
#: * ``auction_opened`` / ``auction_closed`` — ``AuctionStateMachine._transition`` used to
#:   write ``{state, intent_id, cluster_id, reason}`` against
#:   ``("intent_id","cluster_id","roster_size")`` and ``("shortlist_size","reason")``.
#:   Measured on this tree: ``auction_opened`` now carries ``roster_size`` (and a ``roster``
#:   beside it) and ``auction_closed`` carries ``shortlist_size``, so both validate clean.
KNOWN_NONCONFORMING_KINDS = frozenset({"checkout_pixel"})


def test_every_event_the_run_produced_satisfies_its_published_payload_shape(s1_run) -> None:
    """D34: a kind's payload keys are published, and an emitter that omits one is a defect."""
    from contracts.ledger import validate_ledger_payload

    problems: dict[str, list[str]] = {}
    for event in s1_run.events:
        kind = str(event["kind"])
        if kind in KNOWN_NONCONFORMING_KINDS:
            continue
        found = validate_ledger_payload(kind, event.get("payload") or {})
        if found:
            problems.setdefault(kind, []).extend(found)
    assert not problems, f"events whose payload does not match the published shape: {problems}"


def test_the_known_nonconforming_emitters_are_still_exactly_the_ones_reported(s1_run) -> None:
    """The exemption above is a live measurement, not a permanent excuse.

    If an emitter is fixed, this test goes red and the exemption is deleted. If ANOTHER kind
    starts violating its published shape, the test above catches it. Between them the list
    cannot quietly grow.
    """
    from contracts.ledger import validate_ledger_payload

    still_broken = {
        str(event["kind"])
        for event in s1_run.events
        if str(event["kind"]) in KNOWN_NONCONFORMING_KINDS
        and validate_ledger_payload(str(event["kind"]), event.get("payload") or {})
    }
    assert still_broken == set(KNOWN_NONCONFORMING_KINDS), (
        "the exempted-emitter list is out of date. Fixed since it was written: "
        f"{sorted(set(KNOWN_NONCONFORMING_KINDS) - still_broken)} — delete them from "
        "KNOWN_NONCONFORMING_KINDS so the conformance test starts holding them."
    )


# --------------------------------------------------------------------------------------
# 7 — what this run had to supply itself, stated as tests so it cannot be forgotten
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _KindProducers:
    """What the parse below found about one kind — in THREE values, never two.

    ``decided`` is the answer; ``undecided`` is the reason an empty ``decided`` may not be
    read as "there is no producer". Every source in ``undecided`` builds something
    ``LedgerEvent``-shaped whose ``kind`` this parse cannot resolve, so it could be a producer
    of any of the eighteen and this function cannot say which.

    **The two-valued version of this is how the bug survived twice.** A search that answers
    ``[]`` for "I could not tell" is indistinguishable from one that answers ``[]`` for "there
    is nothing there", and both times the difference was the whole ticket.
    """

    kind: str
    decided: tuple[str, ...]
    undecided: tuple[str, ...]

    def summary(self) -> str:
        """One line for an assertion message, saying which of the three answers this is."""
        found = ", ".join(self.decided) or "nowhere in the tree"
        if not self.undecided:
            return found
        return f"{found} (and this parse could not decide about: {', '.join(self.undecided)})"


def _ledger_emitters(kind: str) -> _KindProducers:
    r"""Every product source that emits ``kind`` — by AST, not by grepping for a literal.

    **This function is the repair of a gate that was measured VACUOUS**, and the measurement is
    written here because the failure is the interesting part. Its predecessor was a regex,
    ``(?:\.record|build_event)\(\s*["']<kind>["']``, and it was blind in two directions at once:

    * ``apps/trust/src/claims/routes.py`` has served ``POST /claims/verifications`` — which
      persists a verification and then appends a ``claim_verified`` event — for as long as this
      test has existed, and the regex never saw it, because that route names the kind in a
      module constant and appends through ``trust.events.append`` rather than ``.record``. So
      the test asserted "``claim_verified`` has no producer anywhere in the tree" while a
      producer sat in the tree, served, on a published route;
    * when ``apps/exchange`` gained its own emitters for ``bid_placed``, ``shown`` and
      ``claim_verified``, every one of them named its kind in a module constant
      (``BID_PLACED_KIND``, ``SHOWN_KIND``, ``CLAIM_VERIFIED_KIND``) — house style in this
      repo — and the regex stayed green through all three. A gate that a variable name can
      switch off is not a gate.

    **And it was measured vacuous a THIRD time, by this ticket.** The rule below used to be
    "an ``append(...)`` of a dict literal carrying ``"kind": <kind>``" — the call was part of
    the pattern. ``merchant_svc.composition.pixel_ledger_event`` *returns* such a dict and a
    separate line publishes it (``trust_publisher().publish(event)``), so when
    ``collect_pixel_event`` started calling ``publish_pixel_observation`` — a served
    ``POST /pixel/collect``, writing a real ``checkout_pixel`` row to the chained ledger — this
    search still answered "no producer anywhere in the tree", and
    ``KINDS_THE_RUN_STILL_EMITS_ITSELF`` stayed green while naming a kind that had one.
    Measured on this tree with the emitter in place: ``_ledger_emitters("checkout_pixel")``
    returned ``[]``.

    So the dict rule no longer requires a particular enclosing call. Any dict literal in a
    product module that maps ``"kind"`` to this kind counts, whether it is appended, returned,
    published or handed to something else — a producer builds the event either way, and which
    verb carries it away is not the question this function is asking. The call rule for
    ``recorder.record(<kind>, ...)`` / ``build_event(<kind>, ...)`` is unchanged.

    **And a FOURTH time, which is why this returns three values rather than a list.** The
    first blind spot the paragraph above names — "a kind assembled from a non-constant
    expression" — was not hypothetical. ``merchant_svc.composition.ledger_event`` builds
    ``"kind": str(record.get("kind") or "")``, and the value comes from a mapping
    ``merchant_svc.install.webhooks.ledger_record`` builds in ANOTHER module out of
    ``LEDGER_KIND_FOR_TOPIC.get(event.topic, ...)``. No per-file parse can resolve that, and
    no amount of constant folding will: it is cross-module dataflow off a runtime value. So
    this search answered ``[]`` for ``order_paid`` while ``POST /webhooks/shopify/orders/paid``
    sat served, one hop from the run, publishing that exact row — and
    ``KINDS_THE_RUN_STILL_EMITS_ITSELF`` could not have caught it either, because putting
    ``order_paid`` in that tuple asserts *no producer exists* and this parse would have
    agreed.

    The repair is not a cleverer resolver, it is an honest third answer. A source whose
    ``LedgerEvent``-shaped construction names a kind this parse cannot resolve is reported as
    :attr:`_KindProducers.undecided` rather than being silently skipped, so ``decided == ()``
    means "nothing found AND nothing ambiguous" and nothing else. What counts as
    ``LedgerEvent``-shaped is narrow on purpose, because a list of "could be anything" is a
    list nobody reads: a dict literal that maps ``kind`` alongside at least one of
    ``event_id`` / ``ts`` / ``payload``, or a non-constant kind handed to ``record`` /
    ``build_event`` / ``build_published_event`` in a call that also names a ``LedgerEvent``
    field as a keyword. That last qualifier is what keeps ``record`` usable at all — it is a
    method name a dozen unrelated classes in this tree use (a robots cache, a lint visitor, an
    envelope store), and none of those calls passes ``auction_id=`` or ``payload=``, while
    ``AuctionStateMachine``'s ``self.ledger.record(_TRANSITION_KIND[target], auction_id=…,
    payload=…)`` — the only producer ``auction_opened`` and ``auction_closed`` have — does.
    Measured on this tree: thirteen sources, one of them the merchant composition root this
    ticket is about.

    The blind spots that remain are named rather than left to be discovered: a kind read out
    of a config file, and one emitted by a service that is not under ``apps/``, ``packages/``
    or ``services/``. The direct measurement of a producer is a served request, which is what
    ``test_the_run_records_the_three_auction_kinds_from_the_exchange_itself``,
    ``test_the_pixel_row_was_written_by_the_served_collector_and_not_by_the_run`` and
    ``test_the_order_paid_row_was_written_by_the_served_webhook_route_and_not_by_the_run``
    below do for the five kinds this run no longer writes for itself.
    """
    roots = [REPO_ROOT / "apps", REPO_ROOT / "packages", REPO_ROOT / "services"]
    sources = {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for root in roots
        for path in root.rglob("*.py")
        if path.is_file() and "tests" not in path.relative_to(REPO_ROOT).parts
    }
    assert len(sources) > 50, (
        f"only {len(sources)} product sources found; the search would pass vacuously"
    )
    return _emitters_in(sources, kind)


#: The driver itself, parsed the same way the product is. The run may READ any kind back —
#: that is the whole point of it now — but it must not WRITE one a served route produces, or
#: the exact per-kind multiset double-counts.
HARNESS_PATH = "e2e/support/s1/flow.py"


def _harness_emitters(kind: str) -> _KindProducers:
    """What the driver itself emits for ``kind``, decided by the parse the product gets."""
    return _emitters_in(
        {HARNESS_PATH: (REPO_ROOT / HARNESS_PATH).read_text(encoding="utf-8")}, kind
    )


#: The keys that make a dict literal ``LedgerEvent``-shaped rather than merely a mapping with
#: a ``kind`` in it. A payload can carry its own ``kind`` — ``policy_event``'s published body
#: does (apps/exchange/src/auction/routes.py) — and calling that an undecided event producer
#: would be noise in the one list that has to stay readable.
_LEDGER_EVENT_COMPANION_KEYS = frozenset({"event_id", "ts", "payload"})

#: The event-recording calls. A non-constant kind handed to one of these is a producer this
#: parse cannot resolve — but only when the call also names a ``LedgerEvent`` field as a
#: keyword (below), because ``record`` is a method name a dozen unrelated classes in this tree
#: use and ``x.record(y)`` on its own says nothing about ledger events.
_EVENT_BUILDERS = ("record", "build_event", "build_published_event")

#: The keyword arguments that make such a call a ledger record rather than someone else's
#: ``record``. ``LedgerRecorder.record(kind, *, auction_id, store_id, order_ref, payload)`` and
#: ``build_event`` take these; a robots cache, a lint visitor and an envelope store do not.
_LEDGER_RECORD_KEYWORDS = frozenset(
    {"event_id", "ts", "auction_id", "store_id", "order_ref", "payload"}
)


def _emitters_in(sources: dict[str, str], kind: str) -> _KindProducers:
    """Which of ``sources`` emit ``kind``, and which it cannot decide about.

    The parse :func:`_ledger_emitters` describes, and the ``undecided`` half is the part that
    makes an empty answer mean something.
    """
    import ast

    emitters: list[str] = []
    undecided: list[str] = []
    for name, text in sorted(sources.items()):
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - a module that does not parse emits nothing
            continue
        constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            for target in node.targets
            if isinstance(target, ast.Name) and isinstance(node.value.value, str)
        }

        def names(value: ast.AST | None, table: dict[str, str] = constants) -> str | None:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            if isinstance(value, ast.Name):
                return table.get(value.id)
            return None

        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = {names(key) for key in node.keys}
                if "kind" not in keys:
                    continue
                for key, value in zip(node.keys, node.values, strict=True):
                    if names(key) != "kind":
                        continue
                    if names(value) == kind:
                        emitters.append(name)
                    elif names(value) is None and keys & _LEDGER_EVENT_COMPANION_KEYS:
                        undecided.append(name)
                continue
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called not in _EVENT_BUILDERS or not node.args:
                continue
            first = names(node.args[0])
            if first == kind:
                emitters.append(name)
            elif (
                first is None
                and {keyword.arg for keyword in node.keywords} & _LEDGER_RECORD_KEYWORDS
            ):
                undecided.append(name)
    return _KindProducers(
        kind=kind,
        decided=tuple(sorted(set(emitters))),
        # A source that certainly emits this kind is not also "undecided about" it: the
        # dynamic construction beside the constant one has already been answered.
        undecided=tuple(sorted(set(undecided) - set(emitters))),
    )


#: The kinds the S1 chain needs that STILL have no production emitter, so the run would have
#: to write them itself. It is EMPTY, and it was ``("checkout_pixel",)`` until this ticket.
#:
#: The tuple is kept rather than deleted because its emptiness is the assertion: a kind added
#: here in future is a kind the driver is writing for itself, and
#: ``test_no_kind_in_the_s1_chain_is_left_without_a_production_emitter`` holds the claim to
#: the tree rather than to this comment.
KINDS_THE_RUN_STILL_EMITS_ITSELF: tuple[str, ...] = ()

#: The kinds a SERVED route produces for itself, which the run must therefore not write. Each
#: maps to the module that has to contain the producer, and for each one the AST search can
#: SEE that producer — the module names its kind as a constant.
SERVICE_PRODUCED_KINDS = {
    "bid_placed": "apps/exchange/src/auction/routes.py",
    "shown": "apps/exchange/src/ranking/serving.py",
    "claim_verified": "apps/exchange/src/ranking/verification.py",
    "checkout_pixel": "apps/merchant/svc/src/composition.py",
}

#: The same claim, for the kinds whose producer the AST search **cannot** see. ``order_paid``
#: is the whole of it: ``composition.ledger_event`` takes its kind from a mapping another
#: module built out of ``LEDGER_KIND_FOR_TOPIC``, which is cross-module dataflow off a runtime
#: topic and not something a per-file parse can resolve.
#:
#: Listing it here is the opposite of exempting it. The test below requires the owner to be
#: named in :attr:`_KindProducers.undecided` — so the search must still SEE the construction
#: and still SAY it cannot resolve the kind — and the provenance of the actual row is then
#: held by ``test_the_order_paid_row_was_written_by_the_served_webhook_route_and_not_by_the_run``,
#: which is a stronger check than any parse: it recomputes the row's DERIVED id from the exact
#: signed bytes the stub put on the wire.
UNDECIDABLE_PRODUCED_KINDS = {
    "order_paid": "apps/merchant/svc/src/composition.py",
}


def test_no_kind_in_the_s1_chain_is_left_without_a_production_emitter(s1_run) -> None:
    """The list of kinds this driver writes for itself is empty, and it is checked empty.

    It used to hold ``checkout_pixel``, on the stated grounds that ``pixel/src/`` held a real
    Web Pixel extension but nothing on a served path turned its beacon into a ledger event.
    That stopped being true and **nothing in this suite noticed**, which is the part worth
    keeping. ``merchant_svc.composition.publish_pixel_observation`` was on the served
    ``POST /pixel/collect`` path, writing a real ``checkout_pixel`` row; the exact per-kind
    multiset was supposed to double-count and turn red the moment such an emitter landed, and
    it stayed green because the driver never drove the route — it called the collector's
    library function directly and hand-built the row. A multiset cannot catch a second writer
    that is never invoked.

    Whatever remains in this tuple must have no producer anywhere in the tree; that half is
    unchanged. The half that failed is now
    ``test_the_pixel_row_was_written_by_the_served_collector_and_not_by_the_run``, which
    checks the row's provenance rather than its count.

    **A kind whose producer the search cannot decide about is refused here too**, and that is
    the second repair. ``order_paid`` was the same defect as the pixel's one hop over, and
    listing it in this tuple would NOT have caught it: the claim this tuple makes is "no
    producer exists", the parse could not resolve ``composition.ledger_event``'s kind, and it
    would have answered ``[]`` — agreeing. So "I could not tell" now fails this test rather
    than passing it, and a kind that lands in that state has to be moved to
    ``UNDECIDABLE_PRODUCED_KINDS`` with a provenance test of its own.
    """
    for kind in KINDS_THE_RUN_STILL_EMITS_ITSELF:
        found = _ledger_emitters(kind)
        assert not found.decided, (
            f"{kind!r} now has a production emitter ({found.summary()}). The S1 run emits its "
            "own, so the exact per-kind multiset is about to double-count: delete the run's "
            "emission in e2e/support/s1/flow.py and drive that emitter instead."
        )
        assert not found.undecided, (
            f"this file claims {kind!r} has no producer anywhere, and the search cannot back "
            f"that up: {', '.join(found.undecided)} build LedgerEvent-shaped rows whose kind "
            "this parse cannot resolve, so one of them may be emitting it. Drive the route "
            "and prove the row's provenance instead of asserting an absence nobody measured."
        )
        assert s1_run.kind_counts.get(kind, 0) > 0, f"the run produced no {kind} events at all"


def test_the_pixel_row_was_written_by_the_served_collector_and_not_by_the_run(s1_run) -> None:
    """``checkout_pixel`` came out of ``POST /pixel/collect``, proved by the row's own id.

    Provenance and not presence, because presence is what failed before: the run hand-built a
    ``checkout_pixel`` row while a production emitter sat on a served route, and every count
    in this file agreed with itself.

    The id is what makes this unfakeable by the driver.
    ``merchant_svc.composition.pixel_ledger_event`` DERIVES ``event_id`` from a digest of the
    projected body — deliberately, so a re-fired beacon collapses to one row — while
    ``exchange.auction.ledger.build_event``, the only event builder this driver has, mints a
    ``uuid4``. So recomputing the derived id from the observation the served collector
    recorded and finding it on the row in the ledger says the row came from that function, on
    that beacon. A driver-built row could not match it except by chance in 2^256.

    The other two halves: the beacon's own token is on the row (so it is THIS checkout's), and
    ``e2e/support/s1/flow.py`` contains no ``checkout_pixel`` emitter at all, measured with
    the same parse that decides whether the product has one.
    """
    from merchant_svc.composition import pixel_ledger_event

    (pixel,) = [event for event in s1_run.events if event["kind"] == "checkout_pixel"]
    observation = s1_run.pixel_observation
    assert observation is not None, "the served collector recorded no observation"

    assert pixel["event_id"] == pixel_ledger_event(observation)["event_id"], (
        "the checkout_pixel row's event_id is not the one composition.pixel_ledger_event "
        "derives from the beacon the served collector parsed, so the row did not come out of "
        f"publish_pixel_observation: {pixel['event_id']!r}"
    )
    assert pixel["payload"]["checkout_token"] == observation.checkout_token
    assert not _harness_emitters("checkout_pixel").decided, (
        "e2e/support/s1/flow.py builds a checkout_pixel event again; the multiset double-counts"
    )


def test_the_order_paid_row_was_written_by_the_served_webhook_route_and_not_by_the_run(
    s1_run,
) -> None:
    """``order_paid`` came out of ``POST /webhooks/shopify/orders/paid``, proved by its own id.

    **The same defect as the pixel's, one hop over, and it outlived the pixel fix.** The run
    caught the stub's signed delivery at a ``RecordingReceiver``, re-verified the bytes with a
    second ``handle_delivery`` call and hand-built the ledger row from what came back — while
    ``install/routes.receive_webhook`` sat served, one hop away, wired to ``default_sink`` ->
    ``composition.publish_ledger_record``. During the run that emitter fired on nothing; when
    it did fire it published at the unreachable deployment default and logged "trust ledger:
    NOT written, counted as lost". Every count in this file agreed with itself throughout.

    **The discriminator is the row's DERIVED id, and here it is stronger than the pixel's.**
    ``composition.ledger_event_id`` spells it ``merchant-<kind>-<sha256 of the SIGNED delivery
    bytes>`` — derived precisely so a Shopify retry after a restart collapses to one row —
    while ``exchange.auction.ledger.build_event``, the only event builder this driver has,
    mints a ``uuid4``. The pixel's id digests a projection this repo computes; this one digests
    the exact bytes the stub put on the wire and signed, which the test re-hashes from the
    delivery it captured. A driver-built row could not carry that value.

    Two more, each independent of the first and of each other:

    * ``payload.body_digest`` is the same hash again, in a key ``ledger_payload`` puts there
      and no caller of ``build_event`` in this repo has ever written; and
    * ``store_id`` is the shop's ``.myshopify.com`` domain, which is the merchant's OWN name
      for the store (``composition._store_id``, off the unsigned shop-domain header, through
      ``normalize_shop_domain``). The run's own name for that store is the platform
      ``store_id``, which is what the hand-built row carried — so the two spellings are a
      second fingerprint, and the fact that they differ is the identity gap
      ``_store_aliases`` now resolves the way ``POST /reconcile`` does.
    """
    from merchant_svc.composition import ledger_event
    from merchant_svc.install.webhooks import delivery_digest, ledger_record

    from e2e.support.s1.flow import store_domain

    (paid,) = [event for event in s1_run.events if event["kind"] == "order_paid"]
    body = s1_run.webhook_delivery["body"]
    assert isinstance(body, bytes) and body, "the run captured no raw webhook body"
    digest = delivery_digest(body)

    assert paid["event_id"] == f"merchant-order_paid-{digest}", (
        "the order_paid row's event_id is not the one composition.ledger_event_id derives "
        "from the bytes the stub signed, so the row did not come out of "
        f"publish_ledger_record: {paid['event_id']!r}"
    )
    # …and the same id recomputed the way the service computes it, from the delivery the
    # SERVED route authenticated and kept — `merchant_svc.install.webhooks.INBOX`, not a
    # second parse of the wire by this suite.
    recorded = s1_run.webhook_decision.event
    assert recorded is not None, "the served webhook route recorded no delivery"
    assert paid["event_id"] == ledger_event(ledger_record(recorded))["event_id"]

    assert paid["payload"]["body_digest"] == digest, (
        "the row does not carry the signed-body digest composition.ledger_payload puts on "
        "every order_paid it projects"
    )
    winner = s1_run.fixture["expected"]["winning_store"]
    assert paid["store_id"] == store_domain(winner), (
        "the order_paid row names the store by something other than the shop domain the "
        f"merchant's own _store_id writes: {paid['store_id']!r}"
    )
    assert paid["store_id"] != winner, (
        "the row carries the PLATFORM's store id, which is the run's name for the store and "
        "not the merchant's — the hand-built row is back"
    )
    assert not _harness_emitters("order_paid").decided, (
        "e2e/support/s1/flow.py builds an order_paid event again; the multiset double-counts"
    )


def test_the_run_records_the_three_auction_kinds_from_the_exchange_itself(s1_run) -> None:
    """``bid_placed``, ``shown`` and ``claim_verified`` come from the SERVED auction, not here.

    The counterpart of the test above, and the reason the S1 suite can be trusted about the
    auction path at all. This file's own driver used to compute the candidates, run the claim
    verifier and emit all three of these kinds itself — roughly 150 lines that made a served
    ``POST /auctions`` returning ``ranked: 0, slots: 0`` look like a healthy spine. Two
    assertions, and neither is satisfiable by the harness doing the work:

    * a producer for each kind exists in the module that owns that stage; and
    * ``e2e/support/s1/flow.py`` emits none of them, so every one in the run's ledger came out
      of a service.

    ``checkout_pixel`` joined the list when the driver stopped hand-building it and started
    beaconing at the served ``POST /pixel/collect``; the merchant's composition root is the
    module that owns that stage.

    ``order_paid`` is in :data:`UNDECIDABLE_PRODUCED_KINDS` instead, and the loop below holds
    it to the OTHER half of the same claim: the search has to name the merchant's composition
    root as a place it cannot decide about. That is what stops the search quietly regressing
    to "there is nothing there" — the state in which this whole class of defect hides.

    The counts are then the services' own answer, cross-checked against the auction's
    published response in
    ``test_the_run_covers_every_frozen_ledger_kind_with_an_exact_count``.
    """
    for kind, owner in SERVICE_PRODUCED_KINDS.items():
        found = _ledger_emitters(kind)
        assert owner in found.decided, (
            f"{kind!r} has no producer in {owner}; the served path stopped recording "
            f"it. Found in: {found.summary()}"
        )
        _assert_served_kind(s1_run, kind)
    for kind, owner in UNDECIDABLE_PRODUCED_KINDS.items():
        found = _ledger_emitters(kind)
        assert owner in found.undecided, (
            f"{kind!r} is listed as a kind whose producer this parse cannot resolve, and the "
            f"parse no longer says so about {owner}. Either the producer moved — in which "
            f"case the run may be writing that row itself again — or the kind became "
            f"decidable and belongs in SERVICE_PRODUCED_KINDS. Found: {found.summary()}"
        )
        _assert_served_kind(s1_run, kind)


def _assert_served_kind(s1_run, kind: str) -> None:
    """The run produced ``kind``, and the driver wrote none of it.

    The harness may READ these events back — that is the whole point of the run now — but it
    must not WRITE one. Measured with the same parse, so "the driver emits it" is decided the
    same way "the product emits it" is, and an *undecidable* construction in the driver fails
    here too: a hand-built row hidden behind a variable is the exact move this search was
    blind to in the product.
    """
    assert s1_run.kind_counts.get(kind, 0) > 0, (
        f"the services produced no {kind!r} events for a served auction that ranked "
        f"{len(s1_run.ranked)} candidates and filled {len(s1_run.shortlist['slots'])} slots"
    )
    harness = _harness_emitters(kind)
    assert not harness.decided and not harness.undecided, (
        f"e2e/support/s1/flow.py emits {kind!r} itself, or builds a LedgerEvent whose kind "
        f"this parse cannot resolve ({harness.summary()}); the run must not write a kind a "
        "served route produces, or the exact per-kind multiset double-counts"
    )


def test_the_checkout_token_seam_has_no_production_binding(s1_run) -> None:
    """The one link in S1 that the product does not join, pinned where a reader will find it.

    ``CheckoutProvider.checkout`` invents its ``checkout_token`` with ``secrets.token_hex(16)``
    (apps/exchange/src/checkout/provider.py:828) and never transmits it — the cart permalink it
    builds carries the discount code and nothing else (provider.py:1072). The merchant mints a
    different token when the cart is visited, and nothing in the tree joins the two.

    **The run no longer bridges it, and this test no longer reads a key the run invented.** It
    used to rewrite the merchant's ``order_paid`` row to carry the EXCHANGE's token under
    ``checkout_token``, keeping the platform's own beside it under ``platform_checkout_token``
    — a spelling no production emitter writes — and this test read that invented key. Both are
    gone: the served ``POST /pixel/collect`` publishes the MERCHANT's token, which is the value
    D24 has the pixel and the webhook meet on, so a rewritten webhook token puts the two halves
    of one checkout in different join groups (measured: ``pixel_missing: True`` on a run whose
    stub really posted a beacon). Every token below is now the value its own emitter wrote.

    What holds the chain together instead is the single-use discount code, which
    ``trust.reconcile`` reads with ``code_created`` / ``checkout_redirect`` as bridges. So the
    three assertions are: the two tokens really are different (the defect), the pixel and the
    webhook really do share the merchant's (the join that works), and the order really did
    redeem the code the exchange minted (the bridge's premise). If a production binding lands
    and the exchange's token starts reaching the merchant, the first assertion goes red — which
    is the correct moment to delete this test's premise, not to relax it.
    """
    (pixel,) = [event for event in s1_run.events if event["kind"] == "checkout_pixel"]
    (paid,) = [event for event in s1_run.events if event["kind"] == "order_paid"]
    accepted = next(event for event in s1_run.events if event["kind"] == "accepted")

    authorized = accepted["payload"]["checkout_token"]
    platform = paid["payload"]["checkout_token"]
    assert platform and authorized
    assert platform != authorized, (
        "the exchange's checkout token and the merchant's are now the same value — a "
        "production binding exists, so the discount-code bridge in trust.reconcile is no "
        "longer the only thing joining an order to its offer"
    )
    assert pixel["payload"]["checkout_token"] == platform, (
        "the beacon and the webhook do not name the same checkout, so R4's pixel<->webhook "
        "reconciliation has nothing to join on"
    )
    assert s1_run.completion["discount_code"] == s1_run.minted_code, (
        "the bridge's only premise is that the order redeemed the code the exchange minted"
    )


def test_the_snapshot_envelope_is_unwrapped_by_the_exchanges_own_published_reader(
    s1_fixture,
) -> None:
    """The builder's envelope is unreadable by the ranker; the product's unwrap is what fixes it.

    **This test used to be called ``test_the_exchange_cannot_read_the_snapshot_the_trust_
    service_serves`` and its premise was measurably false** — the same staleness this ticket
    found in the pixel claim, one seam over. It said ``build_snapshot``'s envelope was "the
    body DESIGN's ``GET /snapshot`` hands the exchange" and that "the exchange has no client
    for that endpoint at all". Neither holds at HEAD: ``apps/trust/src/snapshot/routes.py``:813
    serves ``{store_id: published_entry(entry)}`` — the FLAT mapping, with the version in
    ``ETag`` / ``X-Trust-Snapshot-Version`` — and ``exchange.composition``'s
    ``trust_snapshot_endpoint`` / ``HttpTrustSnapshot`` / ``LiveTrustSnapshot`` are a real
    client for it, with ``snapshot_rows`` as the published unwrap for either shape. Its own
    comment records the history: "until T-303 the exchange had no client for it".

    What IS still true, and is what this test now measures: ``build_snapshot`` — the in-process
    producer, which is what the S1 run uses because it has no trust service to GET from — still
    returns the envelope, and ``exchange.ranking.filters.trust_row``
    (apps/exchange/src/ranking/filters.py:140) still reads a FLAT mapping with
    ``snapshot.get(store_id)``, so handing the ranker the envelope denies *every* store under
    R12's fail-closed rule rather than merely the dishonest one. The run therefore has to
    unwrap — and it calls ``snapshot_rows`` to do it, so the S1 run and a deployment agree by
    construction rather than by two copies of one line agreeing today.
    """
    from exchange.composition import TRUST_SNAPSHOT_PATH, trust_snapshot_endpoint
    from exchange.eligibility.trust_backed import snapshot_rows
    from exchange.ranking.filters import blacklist_reason
    from trust.snapshot import build_snapshot

    from e2e.support.s1.flow import AS_OF, _seeded_blacklist

    honest = s1_fixture["expected"]["winning_store"]
    (blocked,) = s1_fixture["expected"]["denied"]
    served = build_snapshot(
        [
            {
                "store_id": store["store_id"],
                "business_identity": store["store_id"],
                "observations": [],
            }
            for store in s1_fixture["stores"]
        ],
        blacklist=_seeded_blacklist(s1_fixture),
        as_of=AS_OF,
    )

    # The client exists, and this is the assertion that keeps the docstring above honest: if
    # it is deleted, this test goes red rather than the prose going quietly stale again.
    url, _ = trust_snapshot_endpoint()
    assert url.endswith(TRUST_SNAPSHOT_PATH), (
        f"the exchange's snapshot client no longer addresses {TRUST_SNAPSHOT_PATH!r}: {url!r}"
    )

    assert "stores" in served, "build_snapshot no longer wraps its rows in a `stores` envelope"
    denial = blacklist_reason(honest, served)
    assert denial is not None and "unreadable" in denial, (
        "the ranker now reads the builder's envelope directly, so the unwrap in "
        "e2e/support/s1/flow.py::_trust_snapshot should be deleted. blacklist_reason "
        f"returned {denial!r}"
    )

    # …and through the exchange's OWN unwrap — the one `_trust_snapshot` calls — the same
    # document answers correctly for both stores.
    rows = snapshot_rows(served)
    assert rows is not None
    assert blacklist_reason(honest, rows) is None
    assert "blacklisted" in (blacklist_reason(blocked, rows) or "")


def test_the_unmapped_claims_are_the_agents_own_policy_telemetry(s1_run, s1_fixture) -> None:
    """The hosted agent publishes a claim that cannot route into any trust dimension.

    ``store_agent.runtime.bid`` puts a ``policy_action`` entry in the bid's ``claims`` list. It
    is the agent's own record of the discount decision, not an assertion about the product or
    the offer, and ``trust.scoring.claim_dimension`` raises ``UnmappedClaimType`` for it —
    correctly, since the approved manifest's table has no row for it. The run therefore counts
    it as telemetry rather than as an asserted claim, and holds the exempted set to exactly the
    key documented here so a genuinely unmappable *product* claim cannot hide in it.
    """
    keys = sorted({claim["key"] for claim in s1_run.unmapped_claims})
    assert keys == sorted(s1_fixture["expected"]["telemetry_claim_keys"]), (
        f"unexpected unmappable claims reached the trust boundary: {keys}"
    )
    for claim in s1_run.asserted_claims:
        assert claim["dim"], "an asserted claim reached the ledger without a dimension"


# --------------------------------------------------------------------------------------
# 8 — the adversarial half: what the run REFUSES
#
# Everything above this line observes one honest pass and asserts that it went well. That is
# necessary and it is not sufficient, and the gap is precise enough to name: a signature
# verifier replaced by `lambda *_: True` accepts the valid delivery exactly as the real one
# does, and a minter replaced by a constant produces a code that flows through every hop and
# matches itself at the far end. Both sabotages were run against the earlier draft of this
# file and all of its tests stayed green.
#
# So this section drives the two units with inputs they must REJECT, and cross-checks the
# minted code against a property no constant can have. Each test carries its own positive
# control, because a hostile input that never reaches the code under test is indistinguishable
# from one the code correctly refused.
# --------------------------------------------------------------------------------------
def test_a_tampered_unsigned_or_reserialised_paid_webhook_is_refused(s1_run) -> None:
    """The `orders/paid` HMAC is *checked*, not assumed — driven with deliveries it must 401.

    The runbook's claim about this hop is exact: the webhook is "signed with HMAC-SHA256 over
    the exact bytes on the wire" and "the merchant refuses an unsigned or re-serialised body".
    A test that only watches a correctly-signed delivery be accepted proves none of that; it
    is satisfied by a verifier that returns True unconditionally, which is precisely how the
    first version of this file passed with `merchant_svc.install.webhooks.verify` sabotaged.

    Every case below is the REAL delivery the stub signed during the run, mutated one way, and
    handed to the real `handle_delivery`. Each gets its own `WebhookInbox` so the merchant's
    de-duplication cannot answer for the signature check.
    """
    import json

    from merchant_svc.install.webhooks import HEADER_HMAC, WebhookInbox, handle_delivery, sign

    from e2e.support.s1.flow import WEBHOOK_SECRET

    delivery = s1_run.webhook_delivery
    body = delivery["body"]
    headers = dict(delivery["headers"])
    assert isinstance(body, bytes) and body, "the run captured no raw webhook body to tamper"
    assert headers.get(HEADER_HMAC.lower()), "the stub sent no HMAC header to tamper with"

    # POSITIVE CONTROL. The untouched delivery is accepted through this exact call, so every
    # refusal below is attributable to the mutation and not to the harness.
    pristine = handle_delivery(
        body=body, headers=headers, secret=WEBHOOK_SECRET, inbox=WebhookInbox()
    )
    assert pristine.accepted and pristine.status_code == 200, (
        f"the positive control failed ({pristine.status_code}: {pristine.reason}); the hostile "
        "cases below would prove nothing"
    )
    assert pristine.event is not None

    # A body that parses to the same document but is not the same bytes. This is the case the
    # runbook names, and the one a framework that hands you `request.json()` makes invisible.
    reserialised = json.dumps(json.loads(body), sort_keys=True, indent=2).encode("utf-8")
    assert reserialised != body, "the re-serialisation produced identical bytes; case is vacuous"

    flipped = bytearray(body)
    flipped[-2] ^= 0x01
    tampered_body = bytes(flipped)
    assert tampered_body != body

    unsigned = {key: value for key, value in headers.items() if key != HEADER_HMAC.lower()}
    assert HEADER_HMAC.lower() not in unsigned

    wrong_key = {**headers, HEADER_HMAC.lower(): sign(body, "not-the-merchants-secret")}
    # This case builds its hostile input with `sign` — the very module under test — so it has
    # to prove the input really is hostile. Measured with `sign` sabotaged to ignore its
    # secret argument: the "forged" header comes out byte-identical to the real one, the
    # merchant correctly accepts it, and the loop below fails with "the merchant ACCEPTED a
    # delivery it cannot have authenticated" — which is a TRUE red for a FALSE reason. The
    # merchant did nothing wrong; the test's own forgery was not a forgery. This assertion
    # does not add a catch, it makes the diagnosis honest, which is the same reason every
    # other hostile input above is proved distinct from the accepted one.
    assert wrong_key[HEADER_HMAC.lower()] != headers[HEADER_HMAC.lower()], (
        "signing with another secret produced the SAME header as the real delivery; this "
        "case cannot test the signature and `sign` is ignoring its key"
    )

    not_ascii = {**headers, HEADER_HMAC.lower(): "sig-éè"}

    hostile: dict[str, tuple[bytes, dict[str, str], str]] = {
        "one flipped byte, original signature": (tampered_body, headers, WEBHOOK_SECRET),
        "re-serialised body, original signature": (reserialised, headers, WEBHOOK_SECRET),
        "no signature header at all": (body, unsigned, WEBHOOK_SECRET),
        "signature minted with another secret": (body, wrong_key, WEBHOOK_SECRET),
        "non-ascii signature header": (body, not_ascii, WEBHOOK_SECRET),
        # An unconfigured deployment must refuse EVERY delivery, never accept every one.
        "the app holds no secret": (body, headers, ""),
    }
    for label, (case_body, case_headers, secret) in hostile.items():
        decision = handle_delivery(
            body=case_body, headers=case_headers, secret=secret, inbox=WebhookInbox()
        )
        assert not decision.accepted, (
            f"{label}: the merchant ACCEPTED a delivery it cannot have authenticated "
            f"({decision.status_code}: {decision.reason})"
        )
        assert decision.status_code == 401, (
            f"{label}: refused with {decision.status_code} ({decision.reason}), not 401 "
            "bad-signature — the refusal must come from the signature check"
        )
        assert decision.event is None, f"{label}: a refused delivery still produced an event"


def test_a_tampered_ledger_event_breaks_the_chain_it_is_in(s1_run) -> None:
    """Tamper-evidence, driven by an actual tamper.

    ``test_the_runs_ledger_is_a_verifiable_hash_chain`` asserts ``ok is True`` on the honest
    sequence, which a ``verify_chain`` that always answered ``ok`` would also satisfy. This
    edits one byte of one payload in the *sealed* chain and requires the verifier to name the
    break — that is what "append-only and hash-chained" has to mean to be worth saying.
    """
    import copy

    from trust.ledger.chain import chain_events, verify_chain

    sealed = chain_events(
        {key: value for key, value in event.items() if value is not None} for event in s1_run.events
    )
    assert len(sealed) == len(s1_run.events)

    # POSITIVE CONTROL — the sealed chain verifies before anything is touched.
    honest = verify_chain(sealed)
    assert honest["ok"] is True, f"the untampered chain did not verify: {honest}"

    index = len(sealed) // 2
    forged = copy.deepcopy(sealed)
    forged[index]["payload"] = {**forged[index].get("payload", {}), "tampered_by": "this test"}

    report = verify_chain(forged)
    assert report["ok"] is False, (
        f"an edited payload at index {index} ({forged[index]['kind']}) passed verification; "
        "the ledger is not tamper-evident"
    )
    assert report["broken_at"] is not None, f"the break was not located: {report}"
    assert report["reason"] in {"tampered", "broken_link"}, report


def test_the_minted_code_has_the_shape_and_the_variety_of_a_real_single_use_code(
    s1_run, s1_fixture
) -> None:
    """The discount code is *minted*, not a constant that happens to travel intact.

    Every value assertion the rest of this file makes about the code is self-referential: the
    permalink was built from the mint's return value, and the merchant was handed that same
    value to seed and to redeem, so a constant matches itself at every hop. Sabotaging
    ``exchange.checkout.codes.mint_code`` to return ``"NOT-A-PSX-CODE"`` left the earlier draft
    entirely green.

    Two independent properties fix that, and neither can hold for a constant:

    * **shape** — ``PSX-`` plus exactly eight Crockford base32 characters, checked against the
      minting module's own published constants rather than a copy of them here;
    * **variety** — the real ``accept()`` path, driven repeatedly on a fresh copy of the same
      auction record, hands out a *different* code every time.

    The auction record is deep-copied per call because a successful ``accept`` stamps
    ``accepted_bid_ref`` on it, and the next accept on a stamped record is refused. Refusals
    mint nothing, so without the copy this test would silently grade one code.
    """
    from exchange.accept import accept
    from exchange.checkout.codes import CODE_ALPHABET, CODE_BODY_LENGTH, CODE_PREFIX
    from exchange.checkout.sellers import StaticRegisteredDomains

    from e2e.support.s1.flow import _auction_record, _RecordingCodeCreator, store_domain

    alphabet = set(CODE_ALPHABET)

    def well_formed(code: str) -> bool:
        body = code[len(CODE_PREFIX) :]
        return (
            code.startswith(CODE_PREFIX) and len(body) == CODE_BODY_LENGTH and set(body) <= alphabet
        )

    assert well_formed(s1_run.minted_code), (
        f"the run's code {s1_run.minted_code!r} is not {CODE_PREFIX}+{CODE_BODY_LENGTH} "
        f"characters of {CODE_ALPHABET!r} — it was not produced by the published minter"
    )

    winning_bid_ref = s1_run.shortlist["slots"][0]["bid_ref"]
    domains = StaticRegisteredDomains(
        {store["store_id"]: store_domain(store["store_id"]) for store in s1_fixture["stores"]}
    )
    minted: list[str] = []
    for _ in range(32):
        record = copy.deepcopy(_auction_record(s1_run, winning_bid_ref))
        result = accept(
            record, winning_bid_ref, _RecordingCodeCreator(), "redirect", registered_domains=domains
        )
        assert result.accepted, (
            f"the accept path refused a legitimate offer: {result.denial_reason}"
        )
        minted.append(str(result.code))

    # POSITIVE CONTROL — the loop really did drive the minter 32 times.
    assert len(minted) == 32 and all(minted), "the accept loop produced no codes to grade"

    malformed = sorted({code for code in minted if not well_formed(code)})
    assert not malformed, (
        f"{len(malformed)} of 32 codes off the real accept path are not {CODE_PREFIX}-codes: "
        f"{malformed[:5]}"
    )
    assert len(set(minted)) == len(minted), (
        f"32 acceptances produced only {len(set(minted))} distinct codes "
        f"({sorted(set(minted))[:3]}...). A single-use code that repeats is not single-use, "
        "and a minter that repeats is a constant"
    )
    assert s1_run.minted_code not in minted, (
        "a fresh acceptance re-issued the code the run already spent at the merchant"
    )


def test_the_single_use_code_is_not_honoured_a_second_time(s1_run) -> None:
    """D22 calls the code single-use; the merchant was asked to honour it twice.

    Measured behaviour of ``services/shopify-stub``, and the reason this asserts on the ORDER
    rather than on a refusal: a code that is spent is *silently ignored* at the cart (app.py's
    documented Shopify behaviour), so the second checkout completes and simply carries no
    discount. Either shape is an acceptable merchant; a second order that redeemed the same
    code again is not.
    """
    second = s1_run.second_redemption
    assert second, "the run recorded no second-redemption probe"
    # Without this the whole test is vacuous: a probe that silently offered some OTHER code
    # would satisfy `redeemed != minted_code` by never having presented the code at all.
    assert second["code"] == s1_run.minted_code, (
        f"the probe offered {second['code']!r}, not the code the run minted "
        f"({s1_run.minted_code!r}); it did not test single use"
    )

    if second["refused"]:
        # An outright refusal IS the single-use property, and the code identity was already
        # pinned above, so there is nothing further to prove here. What this DOES check is
        # that a refusal is internally coherent: a reason to show a reader, and no completion
        # smuggled alongside it.
        #
        # This branch used to read `assert minted_code in error or error`, which `or` binds as
        # `(minted_code in error) or error` — and `error` is built as f"{type}: {exc}" and is
        # therefore never empty, so the whole assertion was unconditionally true and the `in`
        # was dead. Restoring the `in` would not fix it either: `StubClient.buy` raises on the
        # HTTP status and its message need not carry the code at all.
        assert second["error"], f"a refusal with no reason: {second}"
        assert not second["completion"], (
            f"the probe reported the second redemption as refused AND returned a completed "
            f"order: {second}"
        )
        return

    completion = second["completion"]
    assert completion, f"the second checkout neither completed nor was refused: {second}"
    assert completion.get("order_id") != s1_run.completion.get("order_id"), (
        "the probe read back the first order rather than making a second one"
    )
    redeemed = completion.get("discount_code")
    assert not redeemed or redeemed != s1_run.minted_code, (
        f"the merchant honoured the single-use code {s1_run.minted_code!r} a second time, on "
        f"order {completion.get('order_id')!r}"
    )
