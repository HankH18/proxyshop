"""Wave-6 hardening: the runtime defects an adversarial audit found in the E3 auction path.

Ticket verify: ``PROXYSHOP_WORKER=<n> pytest apps/exchange/tests/test_w6_hardening.py -q``.

Every test here was **written red**, against the code as it shipped, and each names the
defect it pins so a later reader can tell what would break if it were reverted:

============  =============================================================================
finding       what this file proves
============  =============================================================================
1  fan-out    a store that never answers cannot be turned into one leaked thread per request
2  deadline   the hard timeout is measured from the auction's deadline, not from fan-out
3  timeout    an unauthenticated caller cannot name its own multi-hour bidding window
6  domain     no call site can build a checkout whose "trusted" domain the store wrote
7  lint       the minting lint survives the aliasing spellings that used to walk past it
8  reasons    a reply that was malformed but on time is not reported as late
============  =============================================================================

Findings 4 and 5 from the same audit pass (``configure_auctions`` having no production
caller, and T-036's checkout port having no production consumer) were **refuted** and are
deliberately not covered here: the first is a documented fail-closed default, and the second
is the scheduled T-036/T-033 scope boundary, not an unwired seam.

Several tests deliberately import a symbol *inside* the test body rather than at module
scope. That is not style: a module-level import of a symbol the fix introduces turns every
test in the file red for the same uninformative reason, and the point of a red-first test is
to fail for the reason it is about.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from exchange.auction import collect_bids, parallel_fan_out
from exchange.auction.fanout import MAX_FAN_OUT_WORKERS
from exchange.auction.routes import configure_auctions
from exchange.checkout import (
    CheckoutRequest,
    code_minting_call_sites,
    resolve_provider,
)
from exchange.eligibility import (
    ELIGIBLE,
    EligibilityDecision,
    SellerEligibility,
    StaticSellerEligibility,
)
from exchange.main import create_app
from fastapi.testclient import TestClient

T_NOW = 1_700_000_000.0
T_FUTURE = 2_000_000_000.0
SELLER_DOMAIN = "store-a.example.com"


def rostered(store_id: str, list_price: float, tier: int = 1) -> dict[str, Any]:
    return {
        "store_id": store_id,
        "tier": tier,
        "product_ref": "product-1",
        "list_price": list_price,
    }


def response(store_id: str, price: float, received_at: float) -> dict[str, Any]:
    return {
        "store_id": store_id,
        "received_at": received_at,
        "bid": {
            "auction_id": "auction-1",
            "store_id": store_id,
            "offer": {"product_ref": "product-1", "unit_price": price, "total_price": price},
            "claims": [],
        },
    }


# =====================================================================================
# Finding 1 — a hung store must not cost a thread per request
#
# The old shape bought the hard timeout by abandoning a FRESH ThreadPoolExecutor on every
# call: `shutdown(wait=False, cancel_futures=True)` ended the *wait*, but the worker was
# still blocked inside the store, was not a daemon, and outlived the response. One silent
# store therefore leaked one non-daemon thread per request, without bound.
# =====================================================================================
REQUESTS_UNDER_LOAD = 64


class HangingSolicitor:
    """A store that answers only when the test lets it — the shape that used to leak."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.entered = 0
        self.finished = 0

    def solicit(self, store: dict[str, Any]) -> None:
        with self._lock:
            self.entered += 1
        self.release.wait(timeout=30.0)
        with self._lock:
            self.finished += 1
        return None

    __call__ = solicit

    def drain(self, timeout: float = 15.0) -> None:
        """Let every held worker go and wait for it, so the shared pool is healthy again."""
        self.release.set()
        limit = time.time() + timeout
        while time.time() < limit:
            with self._lock:
                if self.finished >= self.entered:
                    return
            time.sleep(0.01)


def test_a_store_that_never_answers_does_not_leak_a_thread_per_request() -> None:
    hanging = HangingSolicitor()
    roster = [rostered("store-hung", 120.0)]
    # A deadline already in the past: every call returns immediately, so this measures
    # thread accounting and nothing else. Nothing here sleeps waiting for a window.
    expired = {"deadline": T_NOW, "clock": lambda: T_NOW + 1.0}

    baseline = threading.active_count()
    try:
        for _ in range(REQUESTS_UNDER_LOAD):
            assert parallel_fan_out(roster, hanging, **expired) == []

        growth = threading.active_count() - baseline
        assert growth <= MAX_FAN_OUT_WORKERS, (
            f"{REQUESTS_UNDER_LOAD} requests against one hung store grew the process by "
            f"{growth} threads; the ceiling is {MAX_FAN_OUT_WORKERS} however many requests "
            f"arrive, or a slow store is a denial of service against the exchange itself"
        )
    finally:
        hanging.drain()


def test_the_fan_out_pool_is_one_shared_bounded_pool_not_one_per_request() -> None:
    """The mechanism behind the test above, asserted directly so a regression is legible."""
    from exchange.auction.fanout import fan_out_pool  # noqa: PLC0415 - see the docstring

    assert fan_out_pool() is fan_out_pool(), "a per-call pool is the leak, not a detail"
    assert fan_out_pool().max_workers == MAX_FAN_OUT_WORKERS


def test_a_full_pool_degrades_to_list_price_rather_than_queueing_behind_a_straggler() -> None:
    """No capacity means "not asked", never "asked later" — R10's own degradation path."""
    from exchange.auction.fanout import BoundedFanOutPool  # noqa: PLC0415

    pool = BoundedFanOutPool(max_workers=1)
    hanging = HangingSolicitor()
    roster = [rostered("store-hung", 120.0), rostered("store-b", 130.0)]
    try:
        # First call takes the only worker and never gives it back.
        assert (
            parallel_fan_out(roster, hanging, deadline=T_NOW, clock=lambda: T_NOW + 1.0, pool=pool)
            == []
        )
        # Second call finds nothing free. It must return, not block.
        started = time.time()
        assert (
            parallel_fan_out(roster, hanging, deadline=T_NOW, clock=lambda: T_NOW + 1.0, pool=pool)
            == []
        )
        assert time.time() - started < 2.0, "a full pool queued the request instead of degrading"

        entries = {e.store_id: e for e in collect_bids(roster, [], T_NOW)}
        assert all(entry.fallback for entry in entries.values())
        assert entries["store-b"].unit_price == 130.0, "an unasked store is at its list price"
    finally:
        hanging.drain()
        pool.shutdown()


# =====================================================================================
# Finding 2 — the hard timeout is measured from the DEADLINE, not from fan-out
#
# `ArrivalClock`'s origin used to be `monotonic()` at construction, so the fan-out was
# handed a full `window` of real seconds however much of the auction's own deadline had
# already been spent opening the auction and reading eligibility for every rostered store.
# R10's "hard timeout" was therefore not a bound on the request at all.
# =====================================================================================
class SlowGate(SellerEligibility):
    """An eligibility backend that takes real time — the I/O that used to be free."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self.reads: list[str] = []

    def check(self, store_id: str) -> EligibilityDecision:
        self.reads.append(store_id)
        time.sleep(self._delay)
        return EligibilityDecision(store_id=store_id, status=ELIGIBLE, reason="slow but sure")


class InstantSolicitor:
    """Answers the moment it is asked, at a price well under list."""

    def __init__(self, price: float = 80.0) -> None:
        self.price = price
        self.asked: list[str] = []

    def solicit(self, store: dict[str, Any]) -> dict[str, Any]:
        self.asked.append(store["store_id"])
        return response(store["store_id"], self.price, T_NOW - 1.0)

    __call__ = solicit


def test_a_slow_eligibility_gate_spends_the_window_rather_than_extending_it() -> None:
    """The gate's I/O is inside the window, so a store answering after it is late."""
    window = 0.4
    gate = SlowGate(delay=window * 2)  # the window is gone before anyone can be asked
    solicitor = InstantSolicitor()

    app = create_app()
    configure_auctions(app, solicitor=solicitor, eligibility=gate)
    client = TestClient(app)

    body = client.post(
        "/auctions",
        json={
            "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"},
            "roster": [rostered("store-a", 120.0)],
            "bid_timeout_seconds": window,
        },
    ).json()

    assert gate.reads == ["store-a"], "the gate must still have run for every rostered store"
    entry = body["entries"][0]
    assert entry["fallback"] is True, (
        "the window had already been spent in the eligibility gate, so this store's answer "
        "cannot be inside it — the timeout is measured from the auction's deadline"
    )
    assert entry["unit_price"] == 120.0, "the list price, not the 80.0 bid that arrived late"


def test_the_arrival_clock_can_be_anchored_to_when_the_window_opened() -> None:
    """The mechanism: elapsed time before the clock exists is elapsed time, not free time."""
    from exchange.auction.fanout import ArrivalClock  # noqa: PLC0415

    ticks = {"t": 0.0}

    def monotonic() -> float:
        return ticks["t"]

    ticks["t"] = 1.5  # 1.5s of a 2s window already spent getting here
    clock = ArrivalClock(T_NOW, window=2.0, monotonic=monotonic, started_at=0.0)

    assert clock() == T_NOW - 0.5, "only the remainder of the window is left"
    ticks["t"] = 2.5
    assert clock() == T_NOW + 0.5, "and past the deadline is detectably past it"


# =====================================================================================
# Finding 3 — an unauthenticated caller may not name its own bidding window
#
# `bid_timeout_seconds` is a float on the request body and this route is synchronous end to
# end, so the window a caller asks for is time a worker spends parked. It was clamped only
# from below.
# =====================================================================================
def test_a_caller_cannot_pin_a_worker_by_asking_for_an_enormous_bidding_window() -> None:
    app = create_app()
    configure_auctions(app, eligibility=StaticSellerEligibility({"store-a": ELIGIBLE}))
    client = TestClient(app)

    before = time.time()
    body = client.post(
        "/auctions",
        json={
            "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"},
            "roster": [rostered("store-a", 120.0)],
            "bid_timeout_seconds": 86_400.0,  # "hold a worker for a day, please"
        },
    ).json()

    record = app.state.auction_machine.store.load(body["auction_id"])
    assert record is not None and record.deadline is not None
    granted = record.deadline - before
    assert granted <= 60.0, (
        f"the caller was granted a {granted:.0f}s window on an unauthenticated request; "
        f"the server, not the caller, decides how long a worker may be held"
    )


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (1.0, 1.0),
        (0.0, 0.0),
        (-5.0, 0.0),
        (86_400.0, "ceiling"),
        (float("inf"), "ceiling"),
        (float("nan"), 0.0),
    ],
)
def test_the_bid_window_is_clamped_at_both_ends(requested: float, expected: Any) -> None:
    from exchange.auction.routes import (  # noqa: PLC4015, PLC0415
        MAX_BID_TIMEOUT_SECONDS,
        bid_window_seconds,
    )

    want = MAX_BID_TIMEOUT_SECONDS if expected == "ceiling" else expected
    assert bid_window_seconds(requested) == want


# =====================================================================================
# Finding 6 — the domain guard must not compare a store's claim against a store's claim
#
# With no `registered_domains` source, `registered_domain_for` returns `store_domain` — a
# field the bidding store wrote — so the exact-host check compares one store-authored value
# against another and passes for any store consistent about its own lie.
#
# The port cannot refuse this at runtime and the reason is worth stating, because it decides
# the shape of the fix: with no external source there is NO information separating a
# legitimate bid from a spoofed one, so "fail closed" here means refusing every honest
# checkout too. Frozen goal `test_e3_exchange.py:653` proves that is not merely
# inconvenient — it mints against `bid["store_domain"]` with no registry in the fixture at
# all, so an unconditional refusal would make three frozen E3 goals unreachable.
#
# So the guarantee is moved to where the information exists — the call site — and made
# mechanical rather than conventional, exactly as T-036 did for minting.
# =====================================================================================
UNBOUND_SPELLINGS = {
    "no source at all": (
        "from exchange.checkout import CheckoutRequest\n"
        "def build(bid):\n"
        "    return CheckoutRequest(\n"
        "        auction_id='a', bid_ref='b', store_id=bid['store_id'],\n"
        "        store_domain=bid['store_domain'], offer=bid['offer'],\n"
        "    )\n"
    ),
    "hidden in a splat": (
        "from exchange.checkout import CheckoutRequest\n"
        "def build(fields):\n"
        "    return CheckoutRequest(**fields)\n"
    ),
}


@pytest.mark.parametrize("spelling", sorted(UNBOUND_SPELLINGS))
def test_a_checkout_request_built_without_a_trusted_domain_source_is_flagged(
    spelling: str, tmp_path: Path
) -> None:
    from exchange.checkout import unbound_checkout_requests  # noqa: PLC0415

    (tmp_path / "accept.py").write_text(UNBOUND_SPELLINGS[spelling], encoding="utf-8")
    found = unbound_checkout_requests(tmp_path)
    assert [site.path.name for site in found] == ["accept.py"], (
        f"the {spelling!r} spelling builds a checkout whose 'trusted' domain the store "
        f"wrote, and nothing said so"
    )


def test_passing_the_platforms_lookup_satisfies_the_check(tmp_path: Path) -> None:
    """The negative control: a lint that flags everything proves nothing."""
    from exchange.checkout import unbound_checkout_requests  # noqa: PLC0415

    (tmp_path / "accept.py").write_text(
        "from exchange.checkout import CheckoutRequest, StaticRegisteredDomains\n"
        "SELLERS = StaticRegisteredDomains({'store-a': 'store-a.example.com'})\n"
        "def build(bid):\n"
        "    return CheckoutRequest(\n"
        "        auction_id='a', bid_ref='b', store_id=bid['store_id'],\n"
        "        store_domain=bid['store_domain'], offer=bid['offer'],\n"
        "        registered_domains=SELLERS,\n"
        "    )\n",
        encoding="utf-8",
    )
    assert unbound_checkout_requests(tmp_path) == []


def test_no_checkout_request_in_the_exchange_is_built_unbound() -> None:
    """The standing assertion. Vacuous today; binding the moment T-033 writes a call site."""
    from exchange.checkout import unbound_checkout_requests  # noqa: PLC0415

    src = Path(__file__).resolve().parents[1] / "src"
    unbound = unbound_checkout_requests(src)
    assert unbound == [], f"the domain guard is decorative at: {[str(u) for u in unbound]}"


def test_a_checkout_says_whether_its_domain_was_verified_by_the_platform() -> None:
    """A decorative pass and a real one must not be indistinguishable from the outside."""
    from exchange.checkout import StaticRegisteredDomains  # noqa: PLC0415

    def build(sellers: Any) -> CheckoutRequest:
        return CheckoutRequest(
            auction_id="auction-1",
            bid_ref="bid-a",
            store_id="store-a",
            store_domain=SELLER_DOMAIN,
            offer={
                "product_ref": "product-1",
                "unit_price": 100.0,
                "total_price": 100.0,
                "checkout_url": f"https://{SELLER_DOMAIN}/cart/1:1?discount=NET",
                "expires_at": T_FUTURE,
            },
            mode="redirect",
            now=T_NOW,
            registered_domains=sellers,
        )

    # The store wrote the domain it was checked against: the guard ran, and proved nothing.
    unverified = resolve_provider("redirect").checkout(build(None))
    assert unverified.domain_verified is False
    redirect = next(e for e in unverified.events if e["kind"] == "checkout_redirect")
    assert redirect["payload"]["domain_verified"] is False

    # The platform wrote it: the same permalink, and now it means something.
    verified = resolve_provider("redirect").checkout(
        build(StaticRegisteredDomains({"store-a": SELLER_DOMAIN}))
    )
    assert verified.domain_verified is True
    redirect = next(e for e in verified.events if e["kind"] == "checkout_redirect")
    assert redirect["payload"]["domain_verified"] is True


def test_the_explicit_empty_registry_refuses_rather_than_falling_back_to_the_bid() -> None:
    """`NoRegisteredDomains` is how a caller says "nothing is registered yet" and means it."""
    from exchange.checkout import NoRegisteredDomains, OffDomainCheckout  # noqa: PLC0415

    with pytest.raises(OffDomainCheckout):
        resolve_provider("redirect").checkout(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-a",
                store_id="store-a",
                store_domain="attacker.tld",
                offer={
                    "product_ref": "product-1",
                    "unit_price": 100.0,
                    "total_price": 100.0,
                    "checkout_url": "https://attacker.tld/cart/1:1?discount=X",
                },
                mode="redirect",
                now=T_NOW,
                registered_domains=NoRegisteredDomains(),
            )
        )


# =====================================================================================
# Finding 7 — the minting lint must survive being spelled around
#
# The mechanical guarantee T-036 rests on matched three literal callee names plus one
# `getattr` string. A plain attribute spelling, a name alias or an aliased import walked
# straight past it, and the call that followed was through a name it had never heard of.
# =====================================================================================
EVASIONS = {
    "attribute alias": (
        "def go(creator):\n    handler = creator.create_code\n    return handler('store-a', {})\n"
    ),
    "name alias": (
        "from exchange.checkout.codes import mint_code\n"
        "def go():\n"
        "    fn = mint_code\n"
        "    return fn()\n"
    ),
    "aliased import": (
        "from exchange.checkout.codes import mint_code as _m\ndef go():\n    return _m()\n"
    ),
    "attrgetter": (
        "from operator import attrgetter\n"
        "def go(creator):\n"
        "    return attrgetter('create_code')(creator)('store-a', {})\n"
    ),
    "getattr then call in one expression": (
        "def go(creator):\n    return getattr(creator, 'create_code')('store-a', {})\n"
    ),
    "the injected client, invoked indirectly": (
        "def go(state):\n    return getattr(state, 'code_creator', None)('store-a', {})\n"
    ),
    "list of handlers": ("from exchange.checkout.codes import mint_code\nHANDLERS = [mint_code]\n"),
}


@pytest.mark.parametrize("spelling", sorted(EVASIONS))
def test_the_minting_lint_catches_every_aliasing_spelling(spelling: str, tmp_path: Path) -> None:
    (tmp_path / "sneaky.py").write_text(EVASIONS[spelling], encoding="utf-8")
    found = code_minting_call_sites(tmp_path)
    assert found, f"the {spelling!r} spelling walked straight past the lint"


LEGITIMATE = {
    "receiving the client as a parameter": (
        "def accept(auction, bid_id, code_creator, mode):\n    return {'mode': mode}\n"
    ),
    "forwarding the client without invoking it": (
        "def accept(auction, bid_id, code_creator, mode):\n"
        "    return dict(code_creator=code_creator, mode=mode)\n"
    ),
    "reading the client off app state": (
        "def build(state):\n    return getattr(state, 'code_creator', None)\n"
    ),
}


@pytest.mark.parametrize("spelling", sorted(LEGITIMATE))
def test_the_minting_lint_does_not_flag_merely_holding_the_client(
    spelling: str, tmp_path: Path
) -> None:
    """Holding and forwarding the merchant client is the port's own design, not an escape."""
    (tmp_path / "innocent.py").write_text(LEGITIMATE[spelling], encoding="utf-8")
    assert code_minting_call_sites(tmp_path) == [], (
        f"{spelling!r} was flagged as minting; a lint that forbids forwarding the client "
        f"pushes authors to rename the thing rather than to stop calling it"
    )


# =====================================================================================
# Finding 8 — "late" must mean late
#
# Four distinct conditions collapsed into one `late` set, all reported as
# `response_after_deadline`. A store that answered well inside the window with a malformed
# payload was recorded, and would be reported back to its operator, as slow.
# =====================================================================================
def test_a_reply_that_arrived_in_time_but_malformed_is_not_reported_as_late() -> None:
    roster = [
        rostered("store-nobid", 150.0),
        rostered("store-unstamped", 140.0),
        rostered("store-junk", 130.0),
        rostered("store-late", 120.0),
        rostered("store-silent", 110.0),
    ]

    no_bid = {"store_id": "store-nobid", "received_at": T_NOW - 1.0}
    unstamped = response("store-unstamped", 1.0, T_NOW - 1.0)
    del unstamped["received_at"]
    junk = response("store-junk", 1.0, T_NOW - 1.0)
    junk["received_at"] = "whenever"
    late = response("store-late", 1.0, T_NOW + 1.0)

    entries = {
        entry.store_id: entry
        for entry in collect_bids(roster, [no_bid, unstamped, junk, late], T_NOW)
    }

    assert all(entry.fallback for entry in entries.values()), "every one of these fails closed"
    assert entries["store-nobid"].fallback_reason == "response_carried_no_bid"
    assert entries["store-unstamped"].fallback_reason == "response_not_stamped"
    assert entries["store-junk"].fallback_reason == "arrival_stamp_unparseable"
    assert entries["store-late"].fallback_reason == "response_after_deadline"
    assert entries["store-silent"].fallback_reason == "no_response"


def test_every_recorded_fallback_reason_is_one_the_module_publishes() -> None:
    from exchange.auction import FALLBACK_REASONS, MALFORMED_RESPONSE_REASONS  # noqa: PLC0415

    assert set(MALFORMED_RESPONSE_REASONS) < set(FALLBACK_REASONS)
    assert "response_after_deadline" not in MALFORMED_RESPONSE_REASONS, (
        "a malformed reply is not a late reply; collapsing them is the defect"
    )
