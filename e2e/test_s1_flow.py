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

It does **not** mean the ledger has a production writer for every kind. Three kinds in this
chain have no emitter anywhere in the tree and the run writes them itself from the real
upstream data; ``test_the_kinds_with_no_production_emitter_are_the_three_we_know_about``
states which, so the gap is in the suite's output and not only in a docstring. Nor does it
mean the checkout seam is whole: the exchange's ``checkout_token`` and the merchant's are
unrelated values, and ``test_the_checkout_token_seam_has_no_production_binding`` pins that
defect where a reader will find it.

Markers: none. Only ``docker``/``graph``/``slow``/``needs_model`` are registered
(``pyproject.toml``) and this run needs no datastore, so it must never be skippable — a
skipped e2e is indistinguishable from a passing one in the metrics.
"""

from __future__ import annotations

import copy
from collections import Counter
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
    """The HMAC is the reason the webhook may be believed over the pixel.

    ``handle_delivery`` answers 401 on a bad signature, so an accepted decision is proof the
    stub signed the exact bytes the merchant verified. Re-serialising the body anywhere in
    between would break it, which is what makes this a real check and not a formality.
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
        assert not permalink, (
            f"{label}: accept returned {permalink!r} for a checkout URL outside the "
            f"registered domain {seller!r} ({url})"
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
#: * ``auction_opened`` / ``auction_closed`` — ``AuctionStateMachine._transition``
#:   (apps/exchange/src/auction/state.py:267) writes ``{state, intent_id, cluster_id, reason}``
#:   where the published shapes are ``("intent_id","cluster_id","roster_size")`` and
#:   ``("shortlist_size","reason")``.
#: * ``code_created`` — the checkout port (apps/exchange/src/checkout/provider.py:984) writes
#:   ``{checkout_token, discount_code}`` where the published shape is
#:   ``("code","permalink_url","expires_at")``.
KNOWN_NONCONFORMING_KINDS = frozenset({"auction_opened", "auction_closed"})


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


def test_the_known_nonconforming_emitters_are_still_exactly_the_two_reported(s1_run) -> None:
    """The exemption above is a live measurement, not a permanent excuse.

    If an emitter is fixed, this test goes red and the exemption is deleted. If a *fourth*
    kind starts violating its published shape, the test above catches it. Between them the
    list cannot quietly grow.
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
def test_the_kinds_with_no_production_emitter_are_the_three_we_know_about(s1_run) -> None:
    """``shown``, ``checkout_pixel`` and ``claim_verified`` have no writer in the tree.

    The run builds these three from real upstream data — the ranker's real slots, the stub's
    real beacon, the verifier's real verdicts — because nothing under ``apps/``, ``packages/``
    or ``services/`` writes them. This test is the visible record of that: it searches the
    source tree for a producer, and turns red when one appears, at which point the run must
    stop emitting its own or the exact multiset will double-count.
    """
    import re

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

    # A ledger write in this codebase is `recorder.record("<kind>", ...)` or
    # `build_event("<kind>", ...)`. Matching the call and the literal together is what keeps
    # the frozen vocabulary lists — which name every kind and emit none — out of the result.
    for kind in ("shown", "checkout_pixel", "claim_verified"):
        pattern = re.compile(rf"""(?:\.record|build_event)\(\s*["']{kind}["']""")
        emitters = sorted(name for name, text in sources.items() if pattern.search(text))
        assert not emitters, (
            f"{kind!r} now has a production emitter ({emitters}). The S1 run emits its own, "
            "so the exact per-kind multiset is about to double-count: delete the run's "
            "emission in e2e/support/s1/flow.py and drive that emitter instead."
        )
        assert s1_run.kind_counts.get(kind, 0) > 0, f"the run produced no {kind} events at all"


def test_the_checkout_token_seam_has_no_production_binding(s1_run) -> None:
    """The one link in S1 that the product does not join, pinned where a reader will find it.

    ``CheckoutProvider.checkout`` invents its ``checkout_token`` with ``secrets.token_hex(16)``
    (apps/exchange/src/checkout/provider.py:828) and never transmits it — the cart permalink it
    builds carries the discount code and nothing else (provider.py:1072). The merchant mints a
    different token when the cart is visited. ``trust.reconcile.reconcile`` joins an order to
    its offer on exactly those tokens (apps/trust/src/reconcile/engine.py:227), so without a
    binding the S1 chain produces zero ``reconciled`` events with every other stage green.

    The run derives the binding from the single-use discount code, which the exchange minted
    and the order came back carrying. This test holds that derivation to its premise: the two
    tokens really are different, and the code really is the shared handle. If a production
    binding lands and the tokens become the same value, this test goes red — which is the
    correct moment to delete the run's own binding.
    """
    (pixel,) = [event for event in s1_run.events if event["kind"] == "checkout_pixel"]
    (paid,) = [event for event in s1_run.events if event["kind"] == "order_paid"]
    accepted = next(event for event in s1_run.events if event["kind"] == "accepted")

    authorized = accepted["payload"]["checkout_token"]
    platform = paid["payload"]["platform_checkout_token"]
    assert platform and authorized
    assert platform != authorized, (
        "the exchange's checkout token and the merchant's are now the same value — a "
        "production binding exists, so e2e/support/s1/flow.py::authorized_checkout_token "
        "should be deleted and the merchant's own token used directly"
    )
    assert pixel["payload"]["checkout_token"] == authorized
    assert paid["payload"]["checkout_token"] == authorized
    assert s1_run.completion["discount_code"] == s1_run.minted_code, (
        "the binding's only premise is that the order redeemed the code the exchange minted"
    )


def test_the_exchange_cannot_read_the_snapshot_the_trust_service_serves(s1_fixture) -> None:
    """The second unjoined seam in S1, and this one fails CLOSED across the whole auction.

    ``trust.snapshot.build_snapshot`` returns the served document
    ``{version, score_version, dimensions, as_of, stores: {...}}`` — the body DESIGN's
    ``GET /snapshot`` hands the exchange. ``exchange.ranking.filters.trust_row``
    (apps/exchange/src/ranking/filters.py:108) reads a FLAT ``{store_id: row}`` mapping with
    ``snapshot.get(store_id)``, so on the served document every lookup misses and R12's
    fail-closed rule denies *every* store, not merely the dishonest one. Nothing in the tree
    bridges the two shapes, because the exchange has no client for that endpoint at all.

    The run unwraps ``["stores"]`` in one visible line
    (``e2e/support/s1/flow.py::_trust_snapshot``). This test is why that line is a reported
    defect and not a convenience: it measures both halves, so when a real snapshot client
    lands the served document will start reading cleanly and this test will say so.
    """
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

    assert "stores" in served, "the served snapshot no longer carries a `stores` envelope"
    denial = blacklist_reason(honest, served)
    assert denial is not None and "unreadable" in denial, (
        "the exchange now reads trust's served snapshot document directly — the "
        "`['stores']` unwrap in e2e/support/s1/flow.py::_trust_snapshot should be deleted. "
        f"blacklist_reason returned {denial!r}"
    )

    # …and unwrapped, the same document answers correctly for both stores, which is what the
    # run relies on and what a real client would have to produce.
    assert blacklist_reason(honest, served["stores"]) is None
    assert "blacklisted" in (blacklist_reason(blocked, served["stores"]) or "")


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
    wrong_key = {**headers, HEADER_HMAC.lower(): sign(body, "not-the-merchants-secret")}
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

    if second["refused"]:
        assert s1_run.minted_code in second["error"] or second["error"], second
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
