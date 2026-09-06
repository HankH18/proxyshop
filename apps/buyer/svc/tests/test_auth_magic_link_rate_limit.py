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


def test_the_limiter_sheds_new_addresses_rather_than_growing_without_bound() -> None:
    """The limiter's own table is sized by an unauthenticated caller, so it needs a ceiling.

    Refusing rather than evicting, for the reason ``request_login`` already refuses: dropping
    the oldest tracked address would let anyone who can reach the route clear a chosen
    victim's — or their own — budget on demand, which turns the limiter into its own bypass.
    """
    from buyer_svc.auth.routes import MagicLinkRateLimited, MagicLinkRateLimiter

    now = {"t": datetime(2026, 3, 1, 12, 0, tzinfo=UTC)}
    limiter = MagicLinkRateLimiter(
        limit=2, window=timedelta(minutes=15), max_subjects=4, clock=lambda: now["t"]
    )
    for index in range(4):
        limiter.check(f"spray-{index}@example.invalid")
    assert limiter.tracked == 4

    with pytest.raises(MagicLinkRateLimited):
        limiter.check("late@example.invalid")
    # The four already tracked were not evicted to make room, so their budgets still bind.
    assert limiter.tracked == 4
    limiter.check("spray-0@example.invalid")
    with pytest.raises(MagicLinkRateLimited):
        limiter.check("spray-0@example.invalid")

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
