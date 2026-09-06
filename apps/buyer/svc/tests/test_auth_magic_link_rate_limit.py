"""T-165 — a budget on the unauthenticated magic-link door.

``POST /buyer/auth/magic-link`` takes no credential, so everything about how many login
links land in a given mailbox was chosen by whoever could reach the route. The only ceiling
in front of it bounded this process's *memory*, and structurally could not see the abuse:
a repeated address supersedes its own pending link, so twenty requests left
``pending_links`` at 1 while twenty live bearer tokens went out.

These tests pin the budget, the answer on the wire, the bound on the limiter's own table,
and — as load-bearing as any of them — the property the budget must NOT have broken: the
pending-link table's thousand-call invariant, which is a different property measured on a
different code path.

Nothing here touches a datastore.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

VICTIM = "victim@example.com"


class _CountedInstant(datetime):
    """A timestamp that counts every staleness comparison made against it.

    The limiter decides whether a tracked address has left the window by comparing that
    address's newest admission against a cutoff. So "how many comparisons did one
    ``check()`` make" IS "how many tracked entries did it look at" — the quantity the two
    cost tests below are really about, and the quantity they used to infer from a stopwatch.

    Counting it directly is what takes those tests out of the machine's hands. An O(tracked)
    sweep and an O(1) one differ by orders of magnitude in comparisons on any box at any
    load, whereas the microseconds they take differ by whatever else the machine happened to
    be doing — MEASURED, and recorded at both tests.

    Subclassing ``datetime`` rather than wrapping it is deliberate: the limiter's own
    arithmetic (``now - window``) keeps working untouched — since 3.8 that arithmetic returns
    the subclass — and nothing in ``routes.py`` is aware of it, so what is counted is the
    shipped code path rather than a test-only one.
    """

    compared = 0

    @classmethod
    def reset(cls) -> None:
        """Begin a fresh measurement."""
        cls.compared = 0

    def __gt__(self, other: datetime) -> bool:
        type(self).compared += 1
        return super().__gt__(other)

    def __ge__(self, other: datetime) -> bool:
        type(self).compared += 1
        return super().__ge__(other)

    def __lt__(self, other: datetime) -> bool:
        type(self).compared += 1
        return super().__lt__(other)

    def __le__(self, other: datetime) -> bool:
        type(self).compared += 1
        return super().__le__(other)


def _client(app_service: object | None = None):
    """A client over the real app, with the login service doubled and the limiter real."""
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth
    from buyer_svc.auth.routes import get_auth_service
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    delivered: list[str] = []
    service = app_service or MagicLinkAuth(
        accounts=InMemoryAccountDirectory(),
        deliver=lambda email, token, expires_at: delivered.append(token),
    )
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False), service, delivered, app


def test_the_door_stops_mailing_after_the_budget_and_says_when_to_come_back() -> None:
    """The ticket's shape, plus the header a client can actually obey."""
    from buyer_svc.auth.routes import DEFAULT_MAGIC_LINK_RATE_LIMIT

    client, _service, delivered, _app = _client()

    codes = [
        client.post("/buyer/auth/magic-link", json={"email": VICTIM}).status_code
        for _ in range(DEFAULT_MAGIC_LINK_RATE_LIMIT + 3)
    ]
    assert codes[:DEFAULT_MAGIC_LINK_RATE_LIMIT] == [202] * DEFAULT_MAGIC_LINK_RATE_LIMIT, codes
    assert set(codes[DEFAULT_MAGIC_LINK_RATE_LIMIT:]) == {429}, codes
    assert len(delivered) == DEFAULT_MAGIC_LINK_RATE_LIMIT, (
        f"{len(delivered)} tokens were mailed under a budget of {DEFAULT_MAGIC_LINK_RATE_LIMIT}"
    )

    refused = client.post("/buyer/auth/magic-link", json={"email": VICTIM})
    assert refused.status_code == 429
    retry_after = refused.headers.get("Retry-After")
    assert retry_after and retry_after.isdigit() and int(retry_after) >= 1, refused.headers
    assert VICTIM not in refused.text, "the refusal echoed the address it refused"


def test_a_spent_budget_and_a_full_service_are_one_answer_on_the_wire() -> None:
    """The door must not become an oracle over which mailboxes have been asking for links.

    Same reason ``POST /buyer/auth/session`` collapses unknown/expired/already-used into one
    401. Two different refusals reach this route — "you have had enough" and "the service is
    full" — and an unauthenticated caller who can tell them apart can read the service's
    state, and something about a chosen mailbox, straight off the wire. Status, body and the
    PRESENCE of ``Retry-After`` are therefore identical; only its value differs, which is a
    deliberate trade recorded at ``_refuse_link``.
    """
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth
    from buyer_svc.auth.routes import DEFAULT_MAGIC_LINK_RATE_LIMIT

    # (a) the budget: one address, over its limit.
    client, _service, _delivered, _app = _client()
    for _ in range(DEFAULT_MAGIC_LINK_RATE_LIMIT + 1):
        client.post("/buyer/auth/magic-link", json={"email": VICTIM})
    budget_spent = client.post("/buyer/auth/magic-link", json={"email": VICTIM})

    # (b) the ceiling: fresh addresses, each inside its own budget, against a full table.
    full = MagicLinkAuth(accounts=InMemoryAccountDirectory(), max_pending=2)
    crowded, _s, _d, _a = _client(full)
    for index in range(2):
        assert (
            crowded.post(
                "/buyer/auth/magic-link", json={"email": f"filler-{index}@example.com"}
            ).status_code
            == 202
        )
    table_full = crowded.post("/buyer/auth/magic-link", json={"email": "overflow@example.com"})

    assert budget_spent.status_code == table_full.status_code == 429
    assert budget_spent.json() == table_full.json(), (
        f"the two refusals are distinguishable by body: {budget_spent.text} vs {table_full.text}"
    )
    assert ("Retry-After" in budget_spent.headers) == ("Retry-After" in table_full.headers), (
        "the presence of Retry-After alone says which refusal happened"
    )


def test_the_budget_is_per_address_and_not_a_global_door_closure() -> None:
    """A limiter that refused everyone after one abuser would be the DoS it exists to stop."""
    from buyer_svc.auth.routes import DEFAULT_MAGIC_LINK_RATE_LIMIT

    client, _service, _delivered, _app = _client()

    for _ in range(DEFAULT_MAGIC_LINK_RATE_LIMIT + 5):
        client.post("/buyer/auth/magic-link", json={"email": VICTIM})
    assert client.post("/buyer/auth/magic-link", json={"email": VICTIM}).status_code == 429

    for index in range(20):
        other = client.post("/buyer/auth/magic-link", json={"email": f"buyer{index}@example.com"})
        assert other.status_code == 202, (
            f"an unrelated buyer was refused ({other.status_code}) because one address was "
            "flooded; the limiter is global rather than per-address"
        )


def test_case_and_whitespace_do_not_buy_a_fresh_budget() -> None:
    """One mailbox is one budget. ``Dana@Example.com`` reaches the same inbox as ``dana@``."""
    from buyer_svc.auth.routes import DEFAULT_MAGIC_LINK_RATE_LIMIT

    client, _service, delivered, _app = _client()

    spellings = ["dana.reyes@example.com", "Dana.Reyes@Example.com", "DANA.REYES@EXAMPLE.COM"]
    codes = [
        client.post(
            "/buyer/auth/magic-link", json={"email": spellings[i % len(spellings)]}
        ).status_code
        for i in range(DEFAULT_MAGIC_LINK_RATE_LIMIT + 4)
    ]
    assert 429 in codes, (
        f"cycling the capitalisation of one address defeated the budget entirely: {codes}"
    )
    assert len(delivered) <= DEFAULT_MAGIC_LINK_RATE_LIMIT, (
        f"{len(delivered)} tokens were mailed to one mailbox spelled three ways"
    )


def test_the_budget_refills_as_the_window_passes() -> None:
    """A refused caller is not banned; the oldest admission ageing out gives one back.

    Sliding rather than fixed, and the difference is visible only when the admissions are
    spread out — which is why they are staggered here. A fixed window would hand the whole
    budget back at one instant, letting a patient caller mail ``2 * limit`` links across the
    boundary in a moment.
    """
    from buyer_svc.auth.routes import MagicLinkRateLimited, MagicLinkRateLimiter

    start = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    now = {"t": start}
    limiter = MagicLinkRateLimiter(limit=3, window=timedelta(minutes=15), clock=lambda: now["t"])

    for minutes in (0, 5, 10):
        now["t"] = start + timedelta(minutes=minutes)
        limiter.check(VICTIM)

    # Spent, and the advertised wait is until the FIRST admission leaves the window.
    with pytest.raises(MagicLinkRateLimited) as caught:
        limiter.check(VICTIM)
    assert caught.value.retry_after == 5 * 60, caught.value.retry_after

    # A minute short of that: still refused, and the wait has shrunk to match.
    now["t"] = start + timedelta(minutes=14)
    with pytest.raises(MagicLinkRateLimited) as later:
        limiter.check(VICTIM)
    assert later.value.retry_after == 60, later.value.retry_after

    # Past it: exactly the one that aged out comes back, not the whole budget.
    now["t"] = start + timedelta(minutes=16)
    limiter.check(VICTIM)
    with pytest.raises(MagicLinkRateLimited) as still:
        limiter.check(VICTIM)
    assert still.value.retry_after == 4 * 60, still.value.retry_after


# ASSERTION REPLACED — the shedding policy this node pinned is itself the defect.
# justify-test-edit, recorded here rather than only in the commit body.
#
# QUOTED, the assertion that was here, and the name that went with it
# (``test_the_limiter_sheds_new_addresses_rather_than_growing_without_bound``):
#
#     with pytest.raises(MagicLinkRateLimited):
#         limiter.check("late@example.invalid")
#     # The four already tracked were not evicted to make room, so their budgets still bind.
#     assert limiter.tracked == 4
#     limiter.check("spray-0@example.invalid")
#     with pytest.raises(MagicLinkRateLimited):
#         limiter.check("spray-0@example.invalid")
#
# WHAT IT CLAIMED: that a never-seen address MUST be refused a login link once the limiter is
# tracking ``max_subjects`` addresses.
#
# WHY THAT IS WRONG INDEPENDENTLY OF THIS BRANCH'S CHANGE: it encodes a denial of service as
# the contract. MEASURED at HEAD (c0e4607) with ``max_subjects=3``: three admissions for three
# throwaway addresses, and then a never-seen buyer is answered ``MagicLinkRateLimited`` with
# ``retry_after=900``. The limiter's table is sized by whoever can reach an unauthenticated
# route, so filling it is an unauthenticated, service-wide denial of *login* — every buyer the
# table has not already seen, for a whole window — which is strictly worse than the mailbox
# flooding T-165 exists to bound. A refusal that hits buyers who have asked for nothing is not
# a rate limit.
#
# WOULD THIS TEST STILL BE WRONG IF I REVERTED MY CHANGE? Yes. Reverting restores the refusal
# and this node goes green again, and the contract it asserts is still "an unauthenticated
# caller may switch off login for everybody" — the assertion is wrong about the product, not
# about my edit. That is the case in which this skill permits an edit at all.
#
# NOT WEAKENED: the requirement this node really carries — the table must stay bounded, and a
# caller must not be able to clear a chosen victim's budget — is asserted MORE tightly below.
# ``tracked`` is still pinned at the ceiling; every surviving address is now shown to keep the
# budget it had already spent, which the old body never checked at all.
#
# NOT FROZEN: introduced by f688b32 (this branch's own T-165 work), not part of
# ``.swarm-loop/acceptance/``, and not the node T-165 is graded by — that is
# ``test_repro_open_tickets.py::test_t165_repeated_unauthenticated_magic_link_requests_are_
# eventually_refused``, which is untouched and passes.
#
# BLAST RADIUS: this was the only assertion anywhere in the tree about what happens at
# ``max_subjects`` (``git grep -n max_subjects`` finds this file and routes.py and nothing
# else). The door-level property it should have been asserting is now gated by
# ``test_a_flood_of_unseen_addresses_cannot_close_the_door_on_an_untracked_buyer``.
def test_the_limiter_makes_room_at_its_ceiling_rather_than_closing_the_door() -> None:
    """The limiter's own table is sized by an unauthenticated caller, so it needs a ceiling.

    And the ceiling must not be a second refusal. Meeting it evicts the address nearest to
    leaving the window — the one this limiter was about to forget first — which bounds memory
    without ever answering 429 to an address that has asked for nothing.

    What this does NOT establish, and an earlier version of this docstring wrongly claimed:
    that a caller cannot clear a chosen victim's budget. They can, whenever the eviction
    branch is reachable at all. A victim who has spent their budget and stopped asking IS the
    front of the expiry order, by construction, so no cleverness is needed —
    ``max_subjects`` fresh admissions evict them. MEASURED at ``max_subjects=50``: the victim
    is refused with ``retry_after=896``, fifty flood admissions later their entry is gone, and
    ten links reach one mailbox 55 seconds into a 900-second window against a nominal ceiling
    of five. Waiting out the window was not cheaper; the flood was eight times faster.

    What keeps that out of reach is the RATIO between this ceiling and the pending-link
    ceiling, not the eviction rule — see ``test_the_tracked_ceiling_stays_above_the_pending_
    ceiling`` and the refusal in ``build_rate_limiter``. At the shipped defaults a maximal
    flood saturates the tracked table an order of magnitude below this bound and the branch
    below is never entered at all.

    What this test does establish is narrower and still worth having: the table stays bounded,
    the door never closes on an address that has asked for nothing, and the eviction follows
    the expiry order rather than picking arbitrarily — so the addresses that survive keep the
    budgets they had spent.
    """
    from buyer_svc.auth.routes import MagicLinkRateLimited, MagicLinkRateLimiter

    now = {"t": datetime(2026, 3, 1, 12, 0, tzinfo=UTC)}
    limiter = MagicLinkRateLimiter(
        limit=2, window=timedelta(minutes=15), max_subjects=4, clock=lambda: now["t"]
    )
    for index in range(4):
        # Staggered so that "nearest to leaving the window" is a fact about this table rather
        # than a tie broken by insertion order.
        now["t"] += timedelta(seconds=1)
        limiter.check(f"spray-{index}@example.invalid")
    assert limiter.tracked == 4

    now["t"] += timedelta(seconds=1)
    limiter.check("late@example.invalid")
    assert limiter.tracked == 4, (
        f"the ceiling stopped bounding the table ({limiter.tracked} tracked, max 4); a "
        "limiter that fixes flooding by growing without limit has moved the denial of "
        "service rather than closed it"
    )

    # Room was made from ONE address — the oldest — and from nobody else: every other tracked
    # address still holds the admission it had spent, so a flood cannot be used to hand a
    # chosen victim their budget back.
    for index in (1, 2, 3):
        limiter.check(f"spray-{index}@example.invalid")
        with pytest.raises(MagicLinkRateLimited):
            limiter.check(f"spray-{index}@example.invalid")

    # Stale subjects are swept by the next request rather than by a timer.
    now["t"] += timedelta(hours=1)
    limiter.check("tomorrow@example.invalid")
    assert limiter.tracked == 1


def test_the_pending_link_ceiling_still_holds_and_is_a_different_property() -> None:
    """The budget must not have been bought by moving the memory bound.

    ``test_pending_links_do_not_accumulate_for_an_unauthenticated_caller`` asserts a thousand
    ``request_login`` calls for ONE address leave one pending record. That is the table
    invariant and it lives on the service; the budget lives on the door. Both must hold, and
    a repair that satisfied T-165 by making ``request_login`` itself refuse would have
    silently broken the first one.
    """
    from buyer_svc.auth import MagicLinkAuth

    now = {"t": datetime(2026, 3, 1, 12, 0, tzinfo=UTC)}
    auth = MagicLinkAuth(clock=lambda: now["t"])
    for _ in range(1000):
        auth.request_login("victim@example.invalid")
    assert auth.pending_links == 1


def test_two_applications_do_not_share_one_budget() -> None:
    """The budget belongs to a running service, not to the module that defines the route."""
    first, _s1, _d1, _a1 = _client()
    second, _s2, _d2, _a2 = _client()

    codes = [
        first.post("/buyer/auth/magic-link", json={"email": VICTIM}).status_code for _ in range(9)
    ]
    assert 429 in codes
    fresh = second.post("/buyer/auth/magic-link", json={"email": VICTIM})
    assert fresh.status_code == 202, (
        f"a second application inherited the first one's budget ({fresh.status_code}); the "
        "limiter is a module global rather than application state"
    )


def test_a_deployment_can_change_the_budget_without_a_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limiter whose numbers need a release is one a deployment under attack cannot tighten."""
    from buyer_svc.auth.routes import (
        DEFAULT_MAGIC_LINK_RATE_LIMIT,
        MAGIC_LINK_RATE_LIMIT_ENV,
        MAGIC_LINK_RATE_WINDOW_ENV,
        build_rate_limiter,
    )

    monkeypatch.setenv(MAGIC_LINK_RATE_LIMIT_ENV, "2")
    monkeypatch.setenv(MAGIC_LINK_RATE_WINDOW_ENV, "60")
    tightened = build_rate_limiter()
    assert tightened.limit == 2
    assert tightened.window == timedelta(seconds=60)

    # A typo must not silently mean "no limit", and must not stop the service booting either.
    monkeypatch.setenv(MAGIC_LINK_RATE_LIMIT_ENV, "lots")
    monkeypatch.delenv(MAGIC_LINK_RATE_WINDOW_ENV)
    assert build_rate_limiter().limit == DEFAULT_MAGIC_LINK_RATE_LIMIT
    monkeypatch.setenv(MAGIC_LINK_RATE_LIMIT_ENV, "0")
    assert build_rate_limiter().limit == DEFAULT_MAGIC_LINK_RATE_LIMIT


def test_a_nonsense_limiter_configuration_is_refused_at_construction() -> None:
    from buyer_svc.auth.routes import MagicLinkRateLimiter

    for kwargs in ({"limit": 0}, {"window": timedelta(0)}, {"max_subjects": 0}):
        with pytest.raises(ValueError):
            MagicLinkRateLimiter(**kwargs)  # type: ignore[arg-type]


# =======================================================================================
# The limiter's own table must not become the outage. Appended: these gate two defects
# found in T-165's delivered code — the ceiling was a service-wide login refusal, and the
# admissions that fill it were free.
# =======================================================================================


def test_a_flood_of_unseen_addresses_cannot_close_the_door_on_an_untracked_buyer() -> None:
    """The security property, at the door rather than at the seam.

    The limiter's table is sized by whoever can reach an unauthenticated route, so "the table
    is full" is a state an attacker chooses. MEASURED before the repair, with
    ``max_subjects=3``: three throwaway addresses filled the table and a never-seen buyer was
    answered ``429`` with ``Retry-After: 900``. That is an unauthenticated denial of *login*
    for every buyer the table has not already seen, for a whole window — the limiter closing
    the door it was installed to keep open.

    Driven over the app because the seam refusing is not the property; the route answering is.
    """
    from buyer_svc.auth.routes import MagicLinkRateLimiter, get_rate_limiter
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth  # isort: skip
    from buyer_svc.auth.routes import get_auth_service  # isort: skip

    ceiling = 8
    limiter = MagicLinkRateLimiter(limit=5, window=timedelta(minutes=15), max_subjects=ceiling)
    service = MagicLinkAuth(accounts=InMemoryAccountDirectory())
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    client = TestClient(app, raise_server_exceptions=False)

    flood = [
        client.post(
            "/buyer/auth/magic-link", json={"email": f"flood-{index}@example.com"}
        ).status_code
        for index in range(ceiling * 3)
    ]
    # ARMED. A flood the route never accepted fills nothing, and the assertion below would
    # then be measuring an empty table and reporting it as a repair. (This is not
    # hypothetical: written with ``@example.invalid`` addresses every request answered 422 —
    # ``EmailStr`` refuses the special-use TLD — and the gate passed against a table that had
    # never held a single entry.)
    assert set(flood) == {202}, (
        f"the flood was not admitted, so nothing filled the table: {sorted(set(flood))}"
    )
    assert limiter.tracked == ceiling, (
        f"{len(flood)} accepted requests left the limiter tracking {limiter.tracked} of a "
        f"possible {ceiling}; either the flood filled nothing or the ceiling stopped bounding"
    )

    legitimate = client.post("/buyer/auth/magic-link", json={"email": VICTIM})
    assert legitimate.status_code == 202, (
        f"a buyer this limiter had never seen was answered {legitimate.status_code} because "
        f"an unauthenticated flood of {len(flood)} throwaway addresses had filled the "
        "limiter's table; filling that table is a service-wide login outage"
    )

    # And the budget still binds for the buyer who just got in, so the door did not open by
    # being switched off.
    codes = [
        client.post("/buyer/auth/magic-link", json={"email": VICTIM}).status_code for _ in range(6)
    ]
    assert 429 in codes, f"the flood disabled the budget instead of the refusal: {codes}"


def test_an_admission_charged_for_a_link_that_was_never_mailed_is_given_back() -> None:
    """Filling the limiter's table must cost the attacker a mailed link every time.

    The admission is charged BEFORE ``request_login`` — correctly, since the resource being
    protected is a mailbox — and the pending-link ceiling can refuse afterwards. MEASURED
    before the repair: a request answered 429 by the pending ceiling still left
    ``limiter.tracked == 1``. So past ``max_pending`` an attacker sizes the limiter's table
    without a single further link being mailed, which is what makes filling it cheap.
    """
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth
    from buyer_svc.auth.routes import MagicLinkRateLimiter, get_auth_service, get_rate_limiter
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    delivered: list[str] = []
    limiter = MagicLinkRateLimiter(limit=5, window=timedelta(minutes=15), max_subjects=10_000)
    service = MagicLinkAuth(
        accounts=InMemoryAccountDirectory(),
        deliver=lambda email, token, expires_at: delivered.append(token),
        max_pending=1,
    )
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    client = TestClient(app, raise_server_exceptions=False)

    assert client.post("/buyer/auth/magic-link", json={"email": "filler@example.com"}).status_code
    assert limiter.tracked == 1
    assert len(delivered) == 1

    refused = [
        client.post(
            "/buyer/auth/magic-link", json={"email": f"free-{index}@example.com"}
        ).status_code
        for index in range(25)
    ]
    # ARMED. 429 and not 422: a body the route rejected never reaches the limiter at all, so
    # a validation failure would leave the table unchanged and read exactly like the repair.
    assert set(refused) == {429}, f"the pending ceiling did not refuse: {sorted(set(refused))}"
    assert len(delivered) == 1, f"{len(delivered)} links were mailed past the ceiling"
    assert limiter.tracked == 1, (
        f"{len(refused)} requests that mailed nothing left {limiter.tracked - 1} admissions in "
        "the limiter's table; an admission charged for a link the service then refused is a "
        "slot filled for free"
    )


def test_the_refund_cannot_be_used_to_buy_budget() -> None:
    """The refund must undo a charge, never hand one back that was spent on a real link."""
    from buyer_svc.auth.routes import MagicLinkRateLimited, MagicLinkRateLimiter

    now = {"t": datetime(2026, 3, 1, 12, 0, tzinfo=UTC)}
    limiter = MagicLinkRateLimiter(limit=3, window=timedelta(minutes=15), clock=lambda: now["t"])

    for _ in range(3):
        limiter.check(VICTIM)
    with pytest.raises(MagicLinkRateLimited):
        limiter.check(VICTIM)

    # One refund gives back exactly one admission: one more link, then refused again.
    limiter.refund(VICTIM)
    limiter.check(VICTIM)
    with pytest.raises(MagicLinkRateLimited):
        limiter.check(VICTIM)

    # A refund with nothing to give back is inert rather than a credit.
    limiter.refund("never-asked@example.invalid")
    assert limiter.tracked == 1
    for _ in range(4):
        limiter.refund("never-asked@example.invalid")
    assert limiter.tracked == 1


def test_one_check_costs_the_same_at_a_thousand_and_at_a_hundred_thousand_tracked() -> None:
    """The sweep must not turn the limiter into the amplifier for the flood that fills it.

    MEASURED before the repair, on this machine: ``_forget_stale`` rebuilt every tracked
    address's hit list on every request, inside the global lock — 119.8 us per ``check()`` at
    1,000 tracked and 12,835.7 us at 100,000, a ratio of 107x. At the default ceiling that is
    roughly twelve milliseconds serialised onto every login request in the service, paid by
    every buyer, forever, because somebody sprayed the door once.

    COUNTED, not timed — a repair to this test, not a change of subject. It used to assert on
    ``time.perf_counter()`` deltas, a ratio and a one-millisecond ceiling, which handed a
    correct limiter's verdict to whatever else the machine was doing. MEASURED here: the
    identical deterministic workload produced ratios from 1.06 to 3.13 with nothing changed
    but the load on the box, and its sibling below — same shape, same 8x tolerance — went RED
    four runs in six under contention, in the always-run gate, against code that is right. The
    property was never about microseconds. It is that one ``check()`` examines a bounded
    number of tracked entries however many are tracked; the limiter's own staleness
    comparisons are that number, they are an integer, and they read the same on any machine.

    The table is seeded directly rather than through ``check()`` on purpose: filling 100,000
    addresses through the defective ``check()`` costs O(n^2) and took 151 s, so a gate that
    filled it honestly would hang rather than fail. Seeding is O(n) under both the defective
    and the repaired implementation, so the only thing this measures is one ``check()`` at n.
    """
    from buyer_svc.auth.routes import MagicLinkRateLimiter

    frozen = _CountedInstant(2026, 3, 1, 12, 0, tzinfo=UTC)
    window = timedelta(minutes=15)
    examined_ceiling = 4

    def _fresh(tracked: int) -> MagicLinkRateLimiter:
        limiter = MagicLinkRateLimiter(
            limit=5,
            window=window,
            max_subjects=10_000_000,  # out of the way: the ceiling is not what is measured
            clock=lambda: frozen,
        )
        limiter._hits.update({f"seed-{i}@example.invalid": [frozen] for i in range(tracked)})
        return limiter

    # ARMED: the shortcut is faithful. A seed that built a DIFFERENT structure from the one
    # `check()` maintains would be counting a table the limiter never actually holds, and the
    # measurement below would mean nothing.
    built = MagicLinkRateLimiter(limit=5, window=window, clock=lambda: frozen)
    for index in range(3):
        built.check(f"seed-{index}@example.invalid")
    assert list(built._hits.items()) == list(_fresh(3)._hits.items()), (
        "the direct seed does not reproduce what check() builds, so the count below is "
        "measuring a structure this limiter never holds"
    )

    # ARMED: the instrument is not blind. A sweep that walked the whole table WOULD be seen
    # doing it — this walks one by hand and reads the count back. Without this, an instrument
    # that never fires reports every implementation, O(1) or O(tracked), as costing nothing,
    # which is the failure mode a counter has and a stopwatch does not.
    walked = _fresh(1_000)
    cutoff = frozen - window
    _CountedInstant.reset()
    assert all(hits[-1] > cutoff for hits in walked._hits.values())
    assert _CountedInstant.compared == 1_000, (
        f"walking 1,000 tracked entries registered {_CountedInstant.compared} comparisons; the "
        "instrument cannot tell an O(tracked) sweep from an O(1) one"
    )

    def _entries_examined(tracked: int) -> int:
        limiter = _fresh(tracked)
        assert limiter.tracked == tracked, "the seed did not take; this would measure nothing"
        _CountedInstant.reset()
        limiter.check("probe@example.invalid")
        return _CountedInstant.compared

    small = _entries_examined(1_000)
    large = _entries_examined(100_000)

    assert large == small, (
        f"one check() examines {large} tracked entries at 100,000 tracked against {small} at "
        f"1,000. The per-request cost scales with the size of a table an unauthenticated "
        "caller chooses, so the limiter is the bottleneck the flood exploits"
    )
    assert large <= examined_ceiling, (
        f"one check() examines {large} tracked entries at 100,000 tracked, over the ceiling of "
        f"{examined_ceiling}; a cost that is flat because it is uniformly large is not the "
        "property this gates"
    )


def test_a_transport_that_mails_and_then_raises_does_not_get_the_budget_refunded() -> None:
    """The refund must not become the mailbomb.

    ``request_login`` calls ``deliver`` last, and mail delivery is at-least-once: an SMTP
    transport that hands the message over and then loses the connection reading the ``250``
    has put the link in the mailbox AND raised. A refund on every exception therefore un-does
    the charge for links that were actually delivered.

    MEASURED with a blanket ``except BaseException: refund``, budget of five: 200 requests for
    ONE address mailed 200 links and left ``limiter.tracked`` at 0. That is the budget
    switched off entirely — a worse defect than the un-refunded admission the refund exists to
    close, because it is an unbounded flood into a chosen mailbox rather than one wasted slot.

    The route cannot distinguish "raised before sending" from "sent, then raised", so it keeps
    the charge whenever it cannot know. The asymmetry is the whole argument: an un-refunded
    charge costs one buyer one of five links; a wrong refund costs a victim their mailbox.
    """
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth
    from buyer_svc.auth.routes import MagicLinkRateLimiter, get_auth_service, get_rate_limiter
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    mailed: list[str] = []

    def _at_least_once(email: str, token: str, expires_at: datetime) -> None:
        mailed.append(token)
        raise OSError("connection reset by peer after DATA was accepted")

    budget = 5
    limiter = MagicLinkRateLimiter(limit=budget, window=timedelta(minutes=15))
    service = MagicLinkAuth(accounts=InMemoryAccountDirectory(), deliver=_at_least_once)
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    client = TestClient(app, raise_server_exceptions=False)

    for _ in range(200):
        client.post("/buyer/auth/magic-link", json={"email": VICTIM})

    assert len(mailed) <= budget, (
        f"{len(mailed)} links were put in one mailbox under a budget of {budget}; a delivery "
        "transport that raises after sending is refunding the charge it already spent"
    )
    assert limiter.tracked == 1, (
        f"the flooded address is tracked {limiter.tracked} times; a budget that forgets the "
        "address it just charged is not a budget"
    )


def test_a_refusal_does_not_move_an_address_down_the_expiry_order() -> None:
    """The sweep's O(1) correctness rests on an ordering invariant. This is that invariant.

    ``_forget_stale`` collects a PREFIX and stops at the first survivor, which is only sound
    while ``_hits`` is ordered by each address's newest ADMISSION. A refusal is not an
    admission: moving a refused address to the back would put it behind addresses that expire
    later, and the sweep would then stop before reaching it and never collect it at all — an
    address kept alive for as long as its owner keeps being refused.

    Asserted behaviourally rather than by reading the code, because until now it was written
    down only in a comment.
    """
    from buyer_svc.auth.routes import MagicLinkRateLimited, MagicLinkRateLimiter

    start = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    now = {"t": start}
    window = timedelta(minutes=15)
    limiter = MagicLinkRateLimiter(limit=1, window=window, clock=lambda: now["t"])

    limiter.check("early@example.com")  # newest admission: t0
    now["t"] = start + timedelta(minutes=7)
    limiter.check("later@example.com")  # newest admission: t0 + 7m

    # `early` is refused. Its newest admission is unchanged, so its place in the order is too.
    now["t"] = start + timedelta(minutes=8)
    with pytest.raises(MagicLinkRateLimited):
        limiter.check("early@example.com")

    # Past `early`'s window and inside `later`'s: the sweep must reach `early` and stop at
    # `later`. If the refusal moved `early` behind `later`, the sweep stops at `later` first
    # and `early` survives forever.
    now["t"] = start + timedelta(minutes=16)
    limiter.check("newcomer@example.com")
    assert limiter.tracked == 2, (
        f"{limiter.tracked} addresses are tracked where 2 are live; a refused address was "
        "moved down the expiry order and the prefix sweep can no longer reach it"
    )


def test_a_refund_gives_back_the_newest_admission_and_not_the_oldest() -> None:
    """Which admission comes back decides when the address is forgotten.

    The charge being given back is the one this request just made, so the address must be left
    exactly as it was before — including its expiry, which is measured from its newest
    remaining admission. Popping the OLDEST instead leaves the same COUNT and a different
    lifetime: the address then survives a sweep it should not have, holding a slot against the
    ceiling on the strength of an admission that was refunded.
    """
    from buyer_svc.auth.routes import MagicLinkRateLimiter

    start = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    now = {"t": start}
    limiter = MagicLinkRateLimiter(limit=3, window=timedelta(minutes=15), clock=lambda: now["t"])

    limiter.check("dana@example.com")  # t0
    now["t"] = start + timedelta(minutes=10)
    limiter.check("dana@example.com")  # t0 + 10m
    limiter.refund("dana@example.com")  # gives back t0 + 10m, leaving t0

    # t0 has left the window; t0 + 10m has not. Only one of those two readings can be right.
    now["t"] = start + timedelta(minutes=16)
    limiter.check("someone-else@example.com")
    assert limiter.tracked == 1, (
        f"{limiter.tracked} addresses are tracked where 1 is live; the refund gave back the "
        "oldest admission, so the address is being kept alive by a charge that was returned"
    )


def test_the_address_evicted_at_the_ceiling_is_the_oldest_and_only_the_oldest() -> None:
    """Which address is evicted is the whole security argument, so it is measured, not assumed.

    Following the expiry order is not what makes eviction safe — a caller CAN reach a chosen
    victim, because a victim who has spent their budget and stopped asking is the front by
    construction (measured at ``max_subjects=50``: ten links in 55 seconds against a ceiling
    of five). What this pins is narrower: that the order is FOLLOWED at all. An implementation
    evicting arbitrarily would additionally reset addresses that are still being refused, and
    a gate that triggers ONE eviction over four addresses cannot tell the two apart: measured, a
    random-eviction implementation passed the suite about a third of the time. This drives
    five consecutive evictions over a full table of spent budgets and pins the exact
    partition, which a random choice survives with probability under one in a thousand.
    """
    from buyer_svc.auth.routes import MagicLinkRateLimiter

    now = {"t": datetime(2026, 3, 1, 12, 0, tzinfo=UTC)}
    ceiling, budget, evictions = 8, 2, 5
    limiter = MagicLinkRateLimiter(
        limit=budget, window=timedelta(minutes=15), max_subjects=ceiling, clock=lambda: now["t"]
    )

    # Every address spends its whole budget, staggered so the expiry order is unambiguous.
    for index in range(ceiling):
        for _ in range(budget):
            now["t"] += timedelta(seconds=1)
            limiter.check(f"spray-{index}@example.com")
    assert limiter.tracked == ceiling

    for index in range(evictions):
        now["t"] += timedelta(seconds=1)
        limiter.check(f"newcomer-{index}@example.com")
    assert limiter.tracked == ceiling

    # Read rather than probed. A probe is destructive here: asking whether an evicted address
    # still has budget re-admits it, which at a full table evicts somebody else and moves the
    # very thing being measured. (Written that way first — it reported six evictions out of
    # five, and the sixth was the probe's own doing.)
    survivors = sorted(
        int(subject.split("-")[1].split("@")[0])
        for subject in limiter._hits
        if subject.startswith("spray-")
    )
    assert survivors == list(range(evictions, ceiling)), (
        f"the addresses still tracked are {survivors}, not the {ceiling - evictions} newest "
        f"{list(range(evictions, ceiling))}. Eviction is not following the expiry order, so a "
        "caller can reach an address of their choosing and hand it its budget back"
    )


def test_the_sweep_stays_flat_when_it_actually_collects_something() -> None:
    """The other half of the cost claim: collecting, not just short-circuiting.

    ``test_one_check_costs_the_same_at_a_thousand_and_at_a_hundred_thousand_tracked`` seeds
    every entry at one instant, so the sweep stops at the first entry and collects nothing —
    it measures the short-circuit and would pass against a ``_forget_stale`` that returned
    immediately and never collected at all. The claim being made is amortised, so this
    measures the amortisation: every admission AND the request that finally collects them,
    divided by the number of admissions.

    The old sweep rebuilt every entry's hit list on every request, so filling n addresses cost
    O(n^2) and this ratio grew with n. Sizes are an order of magnitude apart and modest, for
    exactly that reason: at a hundred thousand the unrepaired version takes 151 s to fill.

    COUNTED, not timed, for the reason recorded on its sibling — and this is the node that
    proved the reason. As a stopwatch it FAILED four runs in six under load, in the always-run
    gate, against a limiter that is correct: ``one admission costs 10.1 us amortised at 10,000
    addresses against 0.8 us at 1,000 — a factor of 12.3``, over an 8x tolerance, purely
    because 64 other processes wanted the CPU. Comparisons are what the sweep actually spends;
    an O(n^2) sweep spends ten times more of them per admission at 10,000 than at 1,000, and
    no amount of load moves the number by one.
    """
    from buyer_svc.auth.routes import MagicLinkRateLimiter

    start = _CountedInstant(2026, 3, 1, 12, 0, tzinfo=UTC)
    flatness_tolerance = 1.5
    per_admission_ceiling = 4.0

    def _comparisons_per_admission(subjects: int) -> float:
        now = {"t": start}
        limiter = MagicLinkRateLimiter(
            limit=5,
            window=timedelta(minutes=15),
            max_subjects=10_000_000,
            clock=lambda: now["t"],
        )
        _CountedInstant.reset()
        for index in range(subjects):
            limiter.check(f"seed-{index}@example.com")
        # One request, long after every one of those has expired: it collects all of them.
        now["t"] = start + timedelta(hours=1)
        limiter.check("collector@example.com")
        spent = _CountedInstant.compared
        # ARMED: the collection really happened. Without this the "flat" reading below could
        # be a sweep that never collects, which is the gap this test exists to close.
        assert limiter.tracked == 1, f"the sweep collected nothing: {limiter.tracked} tracked"
        # ARMED: and the work was really watched. A run whose comparisons went uncounted would
        # report the cheapest possible sweep for every implementation, including the O(n^2) one.
        assert spent >= subjects, (
            f"{subjects} admissions and a collecting request registered only {spent} staleness "
            "comparisons; the instrument is not seeing this limiter's sweep"
        )
        return spent / subjects

    small = _comparisons_per_admission(1_000)
    large = _comparisons_per_admission(10_000)
    assert large <= flatness_tolerance * small, (
        f"one admission costs {large:.2f} staleness comparisons amortised at 10,000 addresses "
        f"against {small:.2f} at 1,000 — a factor of {large / small:.1f}, over the "
        f"{flatness_tolerance}x tolerance. The sweep's cost is scaling with the size of a table "
        "an unauthenticated caller chooses"
    )
    assert large <= per_admission_ceiling, (
        f"one admission costs {large:.2f} staleness comparisons amortised at 10,000 addresses, "
        f"over the ceiling of {per_admission_ceiling}; a cost that is flat because it is "
        "uniformly large is not the property this gates"
    )


def test_a_deployment_can_raise_the_tracked_address_ceiling_without_a_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling decides when the limiter starts trading budget accuracy for memory.

    It is the one number that governs whether the eviction branch is reachable at all — below
    the pending-link ceiling it starts releasing budgets that were still binding — and it was
    the one number of the three with no deployment override, so an operator who needed it
    raised could only get it in a release.
    """
    from buyer_svc.auth.routes import (
        DEFAULT_MAGIC_LINK_RATE_SUBJECTS,
        MAGIC_LINK_RATE_SUBJECTS_ENV,
        build_rate_limiter,
    )

    assert build_rate_limiter().max_subjects == DEFAULT_MAGIC_LINK_RATE_SUBJECTS

    monkeypatch.setenv(MAGIC_LINK_RATE_SUBJECTS_ENV, "250000")
    assert build_rate_limiter().max_subjects == 250_000

    # A typo must not silently mean "track one address", and must not stop the boot either.
    for nonsense in ("lots", "0", "-3"):
        monkeypatch.setenv(MAGIC_LINK_RATE_SUBJECTS_ENV, nonsense)
        assert build_rate_limiter().max_subjects == DEFAULT_MAGIC_LINK_RATE_SUBJECTS, nonsense


def test_the_tracked_ceiling_stays_above_the_pending_ceiling() -> None:
    """The ratio between the two ceilings is what keeps eviction out of reach, so pin it.

    A tracked address is one this service actually mailed a link to, and the pending-link
    ceiling — which now refunds what it refuses — caps how many links can be live at once. So
    long as the tracked ceiling is the larger of the two, the table saturates below its bound
    and the eviction branch is never reached in a real deployment. Invert them and the limiter
    starts evicting addresses whose budgets are still binding, which loosens the BUDGET in
    order to bound the MEMORY — the opposite of the trade either ceiling is there to make.

    Asserted rather than described, because it is a relationship between two constants in two
    modules that nothing else would notice being broken.
    """
    from buyer_svc.auth.magic_link import DEFAULT_MAX_PENDING
    from buyer_svc.auth.routes import DEFAULT_MAGIC_LINK_RATE_SUBJECTS

    assert DEFAULT_MAGIC_LINK_RATE_SUBJECTS > DEFAULT_MAX_PENDING, (
        f"the limiter tracks at most {DEFAULT_MAGIC_LINK_RATE_SUBJECTS} addresses while the "
        f"service will hold {DEFAULT_MAX_PENDING} unredeemed links, so a caller can fill the "
        "limiter's table and start evicting live budgets without ever meeting the pending "
        "ceiling that would have refused and refunded them"
    )


def test_a_subject_ceiling_at_or_below_the_pending_ceiling_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one override that can make the service less safe is the one that is checked.

    ``_positive_int_from_env`` validates only ">= 1", so an operator "bounding memory" could
    set the tracked ceiling to 500 against a pending ceiling of 10,000 and be given it with no
    error and no warning. That inverts the ratio the eviction branch's safety rests on: the
    tracked table can then be filled with links the service really mailed, and evicting the
    front is how a caller hands a chosen address its budget back. MEASURED over the real route
    at 500: the victim's five-link budget was reset every 500 attacker requests, reaching 25
    links against a nominal ceiling of five.

    ``test_the_tracked_ceiling_stays_above_the_pending_ceiling`` pins the two CONSTANTS and is
    structurally blind to the override, which is why this exists beside it.
    """
    from buyer_svc.auth.magic_link import DEFAULT_MAX_PENDING
    from buyer_svc.auth.routes import (
        DEFAULT_MAGIC_LINK_RATE_SUBJECTS,
        MAGIC_LINK_RATE_SUBJECTS_ENV,
        build_rate_limiter,
    )

    for refused in (1, 500, DEFAULT_MAX_PENDING - 1, DEFAULT_MAX_PENDING):
        monkeypatch.setenv(MAGIC_LINK_RATE_SUBJECTS_ENV, str(refused))
        built = build_rate_limiter()
        assert built.max_subjects == DEFAULT_MAGIC_LINK_RATE_SUBJECTS, (
            f"{MAGIC_LINK_RATE_SUBJECTS_ENV}={refused} was honoured against a pending ceiling "
            f"of {DEFAULT_MAX_PENDING}; the limiter will now evict budgets that still bind"
        )

    # Raising it is the safe direction and must still work — a refusal that refused everything
    # would have taken the knob away rather than validated it.
    for allowed in (DEFAULT_MAX_PENDING + 1, 250_000):
        monkeypatch.setenv(MAGIC_LINK_RATE_SUBJECTS_ENV, str(allowed))
        assert build_rate_limiter().max_subjects == allowed, allowed


def test_the_budget_window_is_not_longer_than_the_link_ttl() -> None:
    """Pin what the constants actually are, because a comment claimed otherwise.

    ``DEFAULT_MAGIC_LINK_RATE_WINDOW`` was documented as "deliberately longer than
    ``DEFAULT_LINK_TTL`` ... so a refused caller cannot simply wait for their own links to
    expire and start again at full budget". Both are fifteen minutes, and MEASURED, a caller
    who spends the budget at ``t0`` is admitted again at exactly ``t0 + DEFAULT_LINK_TTL``.

    The claim is gone and this pins what is really there, including the part the old claim was
    right to worry about: a caller who spends the budget in a BURST does get the whole budget
    back one window later, because every admission ages out at the same instant. That is the
    behaviour, it is measured here, and it is the intended rate rather than an escape from it
    — five links per fifteen minutes is what the budget says, whether they are spent together
    or spread out. A caller who spreads them gets them back one at a time, which is the other
    half and is asserted alongside.

    Neither half is claimed to be something it is not, which is the whole point of replacing
    the comment rather than the constant.
    """
    from buyer_svc.auth.magic_link import DEFAULT_LINK_TTL
    from buyer_svc.auth.routes import (
        DEFAULT_MAGIC_LINK_RATE_LIMIT,
        DEFAULT_MAGIC_LINK_RATE_WINDOW,
        MagicLinkRateLimited,
        MagicLinkRateLimiter,
    )

    assert DEFAULT_MAGIC_LINK_RATE_WINDOW == DEFAULT_LINK_TTL, (
        f"window {DEFAULT_MAGIC_LINK_RATE_WINDOW} vs link TTL {DEFAULT_LINK_TTL}: the "
        "relationship changed, so the reasoning recorded at both constants needs rereading"
    )

    # (a) spent in a burst: the whole budget returns together, one window later.
    start = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    now = {"t": start}
    burst = MagicLinkRateLimiter(clock=lambda: now["t"])
    for _ in range(DEFAULT_MAGIC_LINK_RATE_LIMIT):
        burst.check(VICTIM)
    with pytest.raises(MagicLinkRateLimited):
        burst.check(VICTIM)

    now["t"] = start + DEFAULT_LINK_TTL
    for index in range(DEFAULT_MAGIC_LINK_RATE_LIMIT):
        burst.check(VICTIM)  # all of them, not one: they expired together
    with pytest.raises(MagicLinkRateLimited):
        burst.check(VICTIM)
    assert index == DEFAULT_MAGIC_LINK_RATE_LIMIT - 1

    # (b) spread out: they come back one at a time, which is what "sliding" buys.
    now["t"] = start
    spread = MagicLinkRateLimiter(clock=lambda: now["t"])
    for index in range(DEFAULT_MAGIC_LINK_RATE_LIMIT):
        now["t"] = start + timedelta(minutes=index)
        spread.check(VICTIM)
    with pytest.raises(MagicLinkRateLimited):
        spread.check(VICTIM)

    now["t"] = start + DEFAULT_MAGIC_LINK_RATE_WINDOW
    spread.check(VICTIM)
    with pytest.raises(MagicLinkRateLimited):
        spread.check(VICTIM)


# =======================================================================================
# T-356 / T-374 / T-376 — three holes in the limiter built above.
#
# The budget, the window and the tracked-address ceiling each turned out to be escapable or
# unbounded from OUTSIDE the code that was reviewed: a plus sign in the address, an
# environment variable, and one extra digit. All three are reachable by whoever can reach the
# unauthenticated door, or by an operator with a keyboard and good intentions.
# =======================================================================================


def test_t356_sub_addressing_does_not_buy_a_fresh_budget() -> None:
    """The ticket's own reproduction: forty plus-tags, one mailbox, one budget.

    ``normalise_buyer_key`` strips and case-folds, which is why
    ``test_case_and_whitespace_do_not_buy_a_fresh_budget`` passes — and it is the whole of
    the normalisation, so ``dana.reyes+0@`` through ``dana.reyes+39@`` are forty keys with
    forty budgets landing in ONE physical mailbox. MEASURED against the served app on
    task/buyer-auth: 40 of 40 accepted under a configured budget of 5.

    The class docstring says "the mailbox is the thing being protected". Until this gate it
    protected the string.
    """
    from buyer_svc.auth.routes import DEFAULT_MAGIC_LINK_RATE_LIMIT

    client, _service, delivered, _app = _client()

    codes = [
        client.post(
            "/buyer/auth/magic-link", json={"email": f"dana.reyes+{tag}@example.com"}
        ).status_code
        for tag in range(40)
    ]
    assert 429 in codes, (
        f"forty plus-tagged spellings of one mailbox were all accepted: {sorted(set(codes))}; "
        "anyone who can type a plus sign has the limiter switched off"
    )
    assert len(delivered) <= DEFAULT_MAGIC_LINK_RATE_LIMIT, (
        f"{len(delivered)} login links were mailed into ONE mailbox under a budget of "
        f"{DEFAULT_MAGIC_LINK_RATE_LIMIT}"
    )


def test_t356_dot_insertion_does_not_buy_a_budget_where_the_provider_ignores_dots() -> None:
    """Gmail documents dots in the local part as insignificant, so they are one mailbox."""
    from buyer_svc.auth.routes import DEFAULT_MAGIC_LINK_RATE_LIMIT

    client, _service, delivered, _app = _client()

    spellings = [
        "danareyes@gmail.com",
        "dana.reyes@gmail.com",
        "d.a.n.a.r.e.y.e.s@gmail.com",
        "dana.reyes+shopping@gmail.com",
        "DanaReyes@GoogleMail.com",
    ]
    codes = [
        client.post(
            "/buyer/auth/magic-link", json={"email": spellings[i % len(spellings)]}
        ).status_code
        for i in range(DEFAULT_MAGIC_LINK_RATE_LIMIT + 5)
    ]
    assert 429 in codes, f"dot and tag spellings of one Gmail mailbox were all accepted: {codes}"
    assert len(delivered) <= DEFAULT_MAGIC_LINK_RATE_LIMIT, (
        f"{len(delivered)} links were mailed to one Gmail mailbox spelled five ways"
    )


def test_t356_normalisation_does_not_merge_distinct_mailboxes() -> None:
    """The other direction, and the one that costs a real buyer their login if it is wrong.

    A rule aggressive enough to collapse every spelling also collapses strangers. Each pair
    below is TWO mailboxes and must keep two budgets:

    * dots outside the providers that document them as insignificant — ``john.smith@`` and
      ``johnsmith@`` at a company domain are two people;
    * the same local part at two domains;
    * a local part that BEGINS with a plus, which has no tag to strip and whose whole local
      part would otherwise vanish.
    """
    from buyer_svc.auth.routes import DEFAULT_MAGIC_LINK_RATE_LIMIT, MagicLinkRateLimiter

    distinct = [
        ("john.smith@corp.example", "johnsmith@corp.example"),
        ("dana@example.com", "dana@example.net"),
        ("+dana@example.com", "dana@example.com"),
        ("dana.reyes@example.com", "dana@example.com"),
    ]
    for left, right in distinct:
        limiter = MagicLinkRateLimiter()
        for _ in range(DEFAULT_MAGIC_LINK_RATE_LIMIT):
            limiter.check(left)
        limiter.check(right)  # must not raise: a different mailbox has its own budget
        assert limiter.tracked == 2, (
            f"{left!r} and {right!r} were folded into one rate-limit key, so one of two real "
            "buyers is locked out of logging in by the other's traffic"
        )


def test_t356_the_stored_address_stays_exact_and_only_the_rate_limit_key_is_normalised() -> None:
    """Normalise the KEY, never the record. The vault's identity is not this ticket's to move.

    ``dana.reyes+work@example.com`` and ``dana.reyes+home@example.com`` share a budget because
    they share a mailbox, and they must still be two distinct accounts with two distinct
    pseudonym histories: the account directory, the vault key and the delivery address all
    keep the address the buyer actually typed. Merging those is a data change reaching far
    beyond the auth package, which is why T-356 says the vault's ``normalise_buyer_key`` needs
    its own ticket rather than a lane edit.
    """
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth
    from buyer_svc.auth.routes import MagicLinkRateLimiter, get_auth_service
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    mailed: list[tuple[str, str]] = []
    service = MagicLinkAuth(
        accounts=InMemoryAccountDirectory(),
        deliver=lambda email, token, expires_at: mailed.append((email, token)),
    )
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: service
    client = TestClient(app, raise_server_exceptions=False)

    tagged = "dana.reyes+work@example.com"
    other = "dana.reyes+home@example.com"
    assert client.post("/buyer/auth/magic-link", json={"email": tagged}).status_code == 202
    assert client.post("/buyer/auth/magic-link", json={"email": other}).status_code == 202

    assert [address for address, _ in mailed] == [tagged, other], (
        f"the links were addressed to {[a for a, _ in mailed]!r}; a normalised RATE-LIMIT key "
        "reached the mail transport, so the buyer's link goes to a mailbox they did not name"
    )

    # Redeeming is what writes the record and the vault history, so drive it: two logins, two
    # accounts, two pseudonyms, and no record under the stripped spelling.
    pseudonyms = []
    for _, token in mailed:
        opened = client.post("/buyer/auth/session", json={"token": token})
        assert opened.status_code == 201, opened.text
        pseudonyms.append(opened.json()["pseudonym"])

    assert service.accounts.get(tagged) is not None
    assert service.accounts.get(other) is not None
    assert service.accounts.get("dana.reyes@example.com") is None, (
        "the plus tag was stripped from the STORED address; two buyers now share one account "
        "record and one pseudonym history"
    )
    assert [service.vault.resolve(pseudonym) for pseudonym in pseudonyms] == [tagged, other], (
        "the vault resolved a pseudonym to a normalised address; the rate-limit key has "
        "reached the identity store, which is a data change T-356 explicitly keeps out of "
        "this lane"
    )

    # And the two tagged addresses really did share one budget on the way in.
    limiter = MagicLinkRateLimiter()
    limiter.check(tagged)
    limiter.check(other)
    assert limiter.tracked == 1


def test_t374_the_rate_window_override_cannot_outrun_the_link_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window was the one override with NO validation at all, and it re-arms the eviction.

    The eviction branch is safe only because tracked entries and pending links drain together,
    and they drain together only while the window is no longer than
    :data:`~buyer_svc.auth.magic_link.DEFAULT_LINK_TTL`: every tracked address is one this
    service mailed a link to, so with a window inside the TTL each tracked entry still has a
    live pending record and the tracked table cannot outgrow the pending ceiling. Stretch the
    window alone and the coupling breaks — tracked climbs epoch after epoch while pending
    stays pinned at its ceiling, reaches the subject ceiling, and ``max_subjects`` fresh
    admissions then hand a chosen victim their budget back.

    ``86400`` and ``31536000`` were both accepted silently, with the subject ceiling set or
    unset. A budget measured over a year is also not a budget: nothing ever ages out of it.
    """
    from buyer_svc.auth.magic_link import DEFAULT_LINK_TTL
    from buyer_svc.auth.routes import (
        DEFAULT_MAGIC_LINK_RATE_WINDOW,
        MAGIC_LINK_RATE_WINDOW_ENV,
        MAX_MAGIC_LINK_RATE_WINDOW,
        MIN_MAGIC_LINK_RATE_WINDOW,
        build_rate_limiter,
    )

    assert MAX_MAGIC_LINK_RATE_WINDOW == DEFAULT_LINK_TTL, (
        "the roof on the window is the link TTL because that is what makes a tracked address "
        "imply a live pending link; if the two constants have come apart, the reasoning at "
        "MAX_MAGIC_LINK_RATE_WINDOW needs rereading rather than this assertion relaxing"
    )

    for refused in ("86400", "31536000", str(int(MAX_MAGIC_LINK_RATE_WINDOW.total_seconds()) + 1)):
        monkeypatch.setenv(MAGIC_LINK_RATE_WINDOW_ENV, refused)
        assert build_rate_limiter().window == DEFAULT_MAGIC_LINK_RATE_WINDOW, (
            f"{MAGIC_LINK_RATE_WINDOW_ENV}={refused} was honoured; the tracked table now "
            "outlives the pending links that bound it and the eviction branch is reachable"
        )

    # A window too SHORT is the same defect wearing the other hat: at the shipped budget a
    # one-second window admits five links a second into one mailbox.
    for refused in ("1", "30", str(int(MIN_MAGIC_LINK_RATE_WINDOW.total_seconds()) - 1)):
        monkeypatch.setenv(MAGIC_LINK_RATE_WINDOW_ENV, refused)
        assert build_rate_limiter().window == DEFAULT_MAGIC_LINK_RATE_WINDOW, (
            f"{MAGIC_LINK_RATE_WINDOW_ENV}={refused} was honoured; the budget is no longer a "
            "rate a mailbox can survive"
        )

    # The knob is validated, not taken away: everything between the two bounds still works.
    for allowed in (
        int(MIN_MAGIC_LINK_RATE_WINDOW.total_seconds()),
        300,
        int(MAX_MAGIC_LINK_RATE_WINDOW.total_seconds()),
    ):
        monkeypatch.setenv(MAGIC_LINK_RATE_WINDOW_ENV, str(allowed))
        assert build_rate_limiter().window == timedelta(seconds=allowed), allowed


def test_t374_a_window_longer_than_the_link_ttl_really_does_unpin_the_tracked_table() -> None:
    """The mechanism the bound above exists for, demonstrated at scaled proportions.

    Not a restatement of the configuration check: this drives the limiter and the link table
    together and watches the invariant the eviction branch's safety rests on — *tracked never
    outgrows the pending table* — hold with a window inside the TTL and break with one outside
    it. Scaled down (pending ceiling 10, window 4x the TTL) so it runs in milliseconds; the
    production proportions are 10,000 pending against a 24h window.
    """
    from buyer_svc.auth.magic_link import MagicLinkAuth
    from buyer_svc.auth.routes import MagicLinkRateLimiter

    ttl = timedelta(minutes=15)
    start = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    def _run(window: timedelta) -> tuple[int, int]:
        now = {"t": start}
        links = MagicLinkAuth(link_ttl=ttl, max_pending=10, clock=lambda: now["t"])
        limiter = MagicLinkRateLimiter(
            limit=1, window=window, max_subjects=10_000, clock=lambda: now["t"]
        )
        address = 0
        for epoch in range(4):
            now["t"] = start + epoch * ttl
            for _ in range(10):
                address += 1
                email = f"flood{address}@example.com"
                limiter.check(email)
                links.request_login(email)
        return limiter.tracked, links.pending_links

    inside_tracked, inside_pending = _run(ttl)
    assert inside_tracked <= inside_pending, (
        f"with the window at the link TTL the tracked table ({inside_tracked}) already "
        f"outgrew the pending table ({inside_pending}); the coupling this bound relies on is "
        "not the one described"
    )
    assert inside_tracked <= 10

    outside_tracked, outside_pending = _run(ttl * 4)
    assert outside_tracked > outside_pending, (
        "the scaled attack did not separate the two tables, so this test is not measuring "
        "the mechanism it claims to"
    )
    assert (outside_tracked, outside_pending) == (40, 10)


def test_t376_the_subject_ceiling_override_has_a_roof_as_well_as_a_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One typo'd extra digit removed the memory bound the constant exists to provide.

    ``PROXYSHOP_BUYER_MAGIC_LINK_RATE_SUBJECTS=99999999999999999999999`` was accepted verbatim
    with no warning: the tracked table is then unbounded in every sense that matters, which is
    the failure the constant's own docstring names — "a limiter that fixes flooding by growing
    without limit has moved the denial of service rather than closed it". The floor added last
    cycle refuses 9999 and 10000 correctly and says nothing about the other end.
    """
    from buyer_svc.auth.routes import (
        DEFAULT_MAGIC_LINK_RATE_SUBJECTS,
        MAGIC_LINK_RATE_SUBJECTS_ENV,
        MAX_MAGIC_LINK_RATE_SUBJECTS,
        build_rate_limiter,
    )

    for refused in ("99999999999999999999999", str(MAX_MAGIC_LINK_RATE_SUBJECTS + 1), "10" * 40):
        monkeypatch.setenv(MAGIC_LINK_RATE_SUBJECTS_ENV, refused)
        assert build_rate_limiter().max_subjects == DEFAULT_MAGIC_LINK_RATE_SUBJECTS, (
            f"{MAGIC_LINK_RATE_SUBJECTS_ENV}={refused[:24]}... was honoured; the limiter's "
            "table has no memory bound at all"
        )

    # Still a knob. The roof is the largest table that fits, not a refusal of every raise.
    for allowed in (250_000, MAX_MAGIC_LINK_RATE_SUBJECTS):
        monkeypatch.setenv(MAGIC_LINK_RATE_SUBJECTS_ENV, str(allowed))
        assert build_rate_limiter().max_subjects == allowed, allowed


def test_t376_the_subject_roof_is_a_memory_bound_and_not_a_round_number() -> None:
    """Pin the arithmetic the roof comes from, because nothing else would notice it drifting.

    The tracked table is the largest thing this service holds at its ceiling, and the service
    runs under ``mem_limit: 256m`` (``apps/buyer/compose.yaml``). A roof chosen for looking
    tidy would be a bound in name only.
    """
    from buyer_svc.auth.routes import (
        BUYER_SVC_MEMORY_LIMIT_BYTES,
        DEFAULT_MAGIC_LINK_RATE_SUBJECTS,
        MAGIC_LINK_RATE_SUBJECT_BYTES,
        MAX_MAGIC_LINK_RATE_SUBJECTS,
    )

    assert MAX_MAGIC_LINK_RATE_SUBJECTS >= DEFAULT_MAGIC_LINK_RATE_SUBJECTS, (
        "the roof is below the shipped default, so the default itself would be refused"
    )
    held = MAX_MAGIC_LINK_RATE_SUBJECTS * MAGIC_LINK_RATE_SUBJECT_BYTES
    assert held <= BUYER_SVC_MEMORY_LIMIT_BYTES // 2, (
        f"a full table at the roof is {held / 1024 / 1024:.0f} MiB against a container limit "
        f"of {BUYER_SVC_MEMORY_LIMIT_BYTES / 1024 / 1024:.0f} MiB; the roof is not a bound "
        "the process can survive meeting"
    )


# --------------------------------------------------------------------------------------
# The T-376 ceiling's own refusal path. Added after the ceiling landed, because closing a
# hole with a guard that crashes at a bigger input does not close it — it converts a bounded
# configuration mistake into a 500 on a route that takes no credential, which is strictly
# worse than the unbounded table it was replacing.
# --------------------------------------------------------------------------------------

#: Magnitudes the refusal has to survive, named by what each one used to break.
#:
#: ``400`` is past the point where ``subjects * MAGIC_LINK_RATE_SUBJECT_BYTES / 1024 / 1024``
#: — an *eagerly evaluated logging argument*, so it ran whether or not anything was logged —
#: exceeded the largest float and raised ``OverflowError: integer division result too large
#: for a float``. ``20`` is past the point where ``timedelta(seconds=...)`` raised
#: ``OverflowError: Python int too large to convert to C int``. ``5000`` is past CPython's
#: own 4300-digit int-parsing limit, which is a different exception again and is only a
#: backstop anyway — it moves with ``PYTHONINTMAXSTRDIGITS``. A bound that depends on which
#: of those three fires first is not a bound; the point of testing all of them is that the
#: answer must be the same refusal every time.
_ABSURD_DIGIT_COUNTS = (20, 21, 306, 400, 4301, 5000, 20000)


def test_an_over_long_configuration_integer_is_refused_and_never_crashes_the_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every knob, at every magnitude, answers with the shipped default and no exception.

    The defect this pins: ``PROXYSHOP_BUYER_MAGIC_LINK_RATE_SUBJECTS`` with 400 digits raised
    ``OverflowError`` from inside ``_log.warning``'s argument list, and
    ``PROXYSHOP_BUYER_MAGIC_LINK_RATE_WINDOW_SECONDS`` with 20 raised it from
    ``timedelta(seconds=...)`` *before* the T-374 bound could refuse the value. Both are on
    ``build_rate_limiter``'s path, which runs on the first request to an unauthenticated
    route.

    Asserting on all three variables together is deliberate. The two failures were the same
    shape — an integer of unbounded magnitude handed to arithmetic — reached through two
    different knobs, so a fix that only repaired the two known call sites would leave the
    third knob one arithmetic operation away from the same 500.
    """
    from buyer_svc.auth.routes import (
        DEFAULT_MAGIC_LINK_RATE_LIMIT,
        DEFAULT_MAGIC_LINK_RATE_SUBJECTS,
        DEFAULT_MAGIC_LINK_RATE_WINDOW,
        MAGIC_LINK_RATE_LIMIT_ENV,
        MAGIC_LINK_RATE_SUBJECTS_ENV,
        MAGIC_LINK_RATE_WINDOW_ENV,
        MAX_ENV_INT_DIGITS,
        build_rate_limiter,
    )

    for env in (
        MAGIC_LINK_RATE_SUBJECTS_ENV,
        MAGIC_LINK_RATE_WINDOW_ENV,
        MAGIC_LINK_RATE_LIMIT_ENV,
    ):
        for digits in _ABSURD_DIGIT_COUNTS:
            monkeypatch.setenv(env, "9" * digits)
            # Building at all is the assertion. Every failure this test was written for was an
            # exception raised out of this call, not a wrong number returned from it.
            built = build_rate_limiter()
            if env is MAGIC_LINK_RATE_SUBJECTS_ENV:
                assert built.max_subjects == DEFAULT_MAGIC_LINK_RATE_SUBJECTS, (env, digits)
            if env is MAGIC_LINK_RATE_WINDOW_ENV:
                assert built.window == DEFAULT_MAGIC_LINK_RATE_WINDOW, (env, digits)
            if env is MAGIC_LINK_RATE_LIMIT_ENV and digits > MAX_ENV_INT_DIGITS:
                assert built.limit == DEFAULT_MAGIC_LINK_RATE_LIMIT, (env, digits)
        monkeypatch.delenv(env)

    # NOT asserted above, and stated here rather than left as a silent gap in the loop: at
    # exactly `MAX_ENV_INT_DIGITS` the LIMIT knob is HONOURED — a budget of 10^20 links per
    # window is accepted, which is the limiter switched off by a knob that reads like a
    # tightening. That is a missing roof on a third knob, in the same family as T-376's, and
    # it is NOT what this test or its fix is about: it predates both, it is not an exception,
    # and choosing the roof is a design decision with its own memory arithmetic to do (one
    # address's history is `limit` timestamps). Pinning the current behaviour here would
    # enshrine it, so this test pins only crash-freedom for that knob and the gap is left
    # named for a ticket.


def test_the_refusal_runs_with_logging_switched_off_because_the_crash_was_a_log_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``logging.disable(CRITICAL)`` must not change the answer — and used not to.

    This is the whole point of the original bug and the reason it is worth its own test.
    Python evaluates a logging call's arguments before the logger decides whether to emit
    anything, so ``subjects * MAGIC_LINK_RATE_SUBJECT_BYTES / 1024 / 1024`` ran on a
    production box with WARNING suppressed exactly as it ran here. A reader who assumes "it
    is only a log line" is assuming the thing that was false.
    """
    import logging

    from buyer_svc.auth.routes import (
        DEFAULT_MAGIC_LINK_RATE_SUBJECTS,
        MAGIC_LINK_RATE_SUBJECTS_ENV,
        build_rate_limiter,
    )

    monkeypatch.setenv(MAGIC_LINK_RATE_SUBJECTS_ENV, "9" * 400)
    logging.disable(logging.CRITICAL)
    try:
        assert build_rate_limiter().max_subjects == DEFAULT_MAGIC_LINK_RATE_SUBJECTS
    finally:
        logging.disable(logging.NOTSET)


def test_an_absurd_ceiling_is_a_clean_answer_on_the_wire_and_not_a_500() -> None:
    """The measurement that says why this matters: the route, not the helper.

    ``POST /buyer/auth/magic-link`` takes no credential. Before the fix this returned 500
    with the limiter's own ``OverflowError`` behind it; the misconfiguration is the
    operator's, but the crash was reachable by anyone who could reach the door.
    """
    import os

    from buyer_svc.auth.routes import MAGIC_LINK_RATE_SUBJECTS_ENV, MAGIC_LINK_RATE_WINDOW_ENV

    for env, value in (
        (MAGIC_LINK_RATE_SUBJECTS_ENV, "9" * 400),
        (MAGIC_LINK_RATE_WINDOW_ENV, "9" * 20),
    ):
        previous = os.environ.get(env)
        os.environ[env] = value
        try:
            client, _service, _delivered, _app = _client()
            response = client.post("/buyer/auth/magic-link", json={"email": VICTIM})
        finally:
            if previous is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = previous
        assert response.status_code < 500, (
            f"{env} with {len(value)} digits answered {response.status_code}; a refused "
            "configuration value must never reach an unauthenticated caller as a server error"
        )
        assert response.status_code == 202, (env, response.status_code)


def test_the_refusal_does_not_echo_the_whole_over_long_value_into_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A refusal must not be sized by the input it refuses.

    ``_positive_int_from_env`` logged ``%r`` of the raw environment value, so refusing a
    400-character number wrote 400 characters, and refusing a megabyte wrote a megabyte. The
    value is the one quantity here the caller chose, so it is the one quantity that must not
    be repeated back whole.
    """
    import logging

    from buyer_svc.auth.routes import (
        MAGIC_LINK_RATE_SUBJECTS_ENV,
        MAX_ENV_INT_DIGITS,
        build_rate_limiter,
    )

    absurd = "9" * 4000
    monkeypatch.setenv(MAGIC_LINK_RATE_SUBJECTS_ENV, absurd)
    with caplog.at_level(logging.WARNING, logger="buyer_svc.auth.routes"):
        build_rate_limiter()

    assert caplog.records, "the refusal was silent; an operator would never learn of it"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert absurd not in logged, "the refusal echoed the whole over-long value back"
    assert len(logged) < 4 * MAX_ENV_INT_DIGITS + 400, (
        f"the refusal log is {len(logged)} characters for a {len(absurd)}-character input; "
        "it is still sized by the value it refuses"
    )
    assert MAGIC_LINK_RATE_SUBJECTS_ENV in logged, "the refusal does not name the knob"


def test_the_length_gate_refuses_nothing_an_operator_could_have_meant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is on absurdity, not on the knob — and the T-376 roof still does its job.

    Two properties in one test because they are the same trade. ``MAX_ENV_INT_DIGITS`` sits
    far above every value these knobs can legitimately take, so it cannot be the thing that
    refuses a real setting; and the value-range checks it protects — the T-376 memory roof,
    its pending-link floor, and the T-374 window bounds — still refuse exactly what they
    refused before, which is what stops "make the refusal cheap" from quietly becoming "make
    the refusal absent".
    """
    from buyer_svc.auth.routes import (
        DEFAULT_MAGIC_LINK_RATE_SUBJECTS,
        MAGIC_LINK_RATE_SUBJECTS_ENV,
        MAX_ENV_INT_DIGITS,
        MAX_MAGIC_LINK_RATE_SUBJECTS,
        build_rate_limiter,
    )

    assert len(str(MAX_MAGIC_LINK_RATE_SUBJECTS)) < MAX_ENV_INT_DIGITS, (
        "the digit ceiling is at or below the largest ceiling an operator may ask for, so it "
        "would refuse a legitimate setting rather than only an absurd one"
    )

    # Inside the length gate and inside the range: honoured, exactly as before.
    for allowed in (250_000, MAX_MAGIC_LINK_RATE_SUBJECTS):
        monkeypatch.setenv(MAGIC_LINK_RATE_SUBJECTS_ENV, str(allowed))
        assert build_rate_limiter().max_subjects == allowed, allowed

    # Inside the length gate, outside the range: still refused by T-376's roof, which is the
    # check that must not have been lost to the cheaper one in front of it.
    for refused in (MAX_MAGIC_LINK_RATE_SUBJECTS + 1, 10**18, 10**19):
        monkeypatch.setenv(MAGIC_LINK_RATE_SUBJECTS_ENV, str(refused))
        assert len(str(refused)) <= MAX_ENV_INT_DIGITS, (
            f"{refused} is long enough to be refused on length, so it does not exercise the "
            "memory roof this assertion is about"
        )
        assert build_rate_limiter().max_subjects == DEFAULT_MAGIC_LINK_RATE_SUBJECTS, refused
