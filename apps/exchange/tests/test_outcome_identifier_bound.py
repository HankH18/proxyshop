"""``POST /internal/outcomes`` must bound the two caller-chosen names it KEEPS.

    PROXYSHOP_WORKER=1 .venv/bin/python -m pytest \
        apps/exchange/tests/test_outcome_identifier_bound.py -q

**The defect this file grades, measured over the served door before the repair.**
``POST /internal/outcomes`` is new on this branch, it is unauthenticated like every other route
this service serves (``git grep -nE "Depends|api_key|Authorization" apps/exchange/src`` is
empty), and it is genuinely mounted — ``create_app().state.mounted_routers`` contains
``exchange.policy.routes``. Its body is a ``contracts.protocol.TrustEventPayload``, whose
``store_id`` carries ``min_length=1`` and **no maximum**; ``pseudonymous_context.cluster_id``
carries neither. Both become keys in :class:`~exchange.policy.routes.InMemoryBanditPosteriors`'
``_stores`` / ``_clusters``, which live for the life of the process. So an anonymous caller
chose the length of a string this process keeps. Measured with 30,000-character names, every
send answering **204**::

    honest baseline                      1 send    book retained   0.003 MiB
    store_id at 30,000 chars           256 sends   book retained   7.416 MiB
    store_id at 30,000 chars          2048 sends   book retained   7.427 MiB
    cluster_id at 30,000 chars         256 sends   book retained   0.935 MiB
    both at 30,000 chars               512 sends   book retained   9.852 MiB

**The third line is why this is a bound and not an emergency, and it is stated so the next
reader does not overclaim it.** ``_stores`` caps at ``DEFAULT_BANDIT_STORES`` (256) and
``_clusters`` at ``DEFAULT_BANDIT_CLUSTERS`` (32), so growth is FLAT from 256 sends to 2048 and
the ceiling is ~10 MiB rather than unbounded. It is still three thousand times the honest book,
held by an unauthenticated caller, inside a 256 MiB container (``apps/exchange/compose.yaml``).
It is the same axis ``auction.routes.MAX_RECORDED_OFFER_VALUE_CHARS`` exists for, reached
through a door this branch introduced.

**What the repair is.** ``exchange.policy.routes._bounded_identifiers`` refuses both names above
``auction.routes.MAX_IDENTIFIER_LENGTH`` — the number ``RosterEntry.store_id`` already holds an
unauthenticated caller to at the auction door, imported rather than restated. Refused rather
than truncated: two 30,000-character ids sharing a 128-character prefix are the SAME key after
clipping, so truncation would fold one store's outcome into another store's posterior.

Every assertion below drives the real route through ``TestClient`` and measures what the book
retained. Nothing here reads the source text of the module it grades.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest
from exchange.main import create_app
from exchange.policy.routes import (
    DEFAULT_BANDIT_CLUSTERS,
    DEFAULT_BANDIT_STORES,
    DEFAULT_MAX_OUTCOME_BYTES,
    RETAINED_IDENTIFIER_FIELDS,
    identifier_ceiling,
)
from fastapi.testclient import TestClient

#: The width the defect was measured at. Far above any identifier and far under
#: ``DEFAULT_MAX_OUTCOME_BYTES`` (64 KiB), which is the point: the body cap never saw this.
HOSTILE_CHARS = 30_000

#: Sends per hostile run. 256 is ``DEFAULT_BANDIT_STORES`` — the width at which the store book
#: is full and its growth has already flattened, so this is the whole retained cost.
SENDS = 256

#: The honest book, in MiB, plus generous headroom. The pre-repair store_id run retained 7.4
#: MiB, so any threshold between the two discriminates; this one is ~70x under the defect.
HONEST_CEILING_MIB = 0.1


def _deep_bytes(obj: Any, seen: set[int] | None = None) -> int:
    """Bytes retained by ``obj`` and everything it uniquely owns.

    Identity-deduplicated, and it descends into ``__dict__`` as well as containers because the
    thing being measured is an OBJECT — the posterior book — whose two ``OrderedDict``s hold the
    caller's strings as KEYS. The same instrument, and the same reason for it, as
    ``test_recorded_offer_budget.py``'s: a string echoed into many records is counted once, so
    what this reports is genuinely retained rather than merely referenced.
    """
    if seen is None:
        seen = set()
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    total = sys.getsizeof(obj)
    if isinstance(obj, dict):
        for key, value in obj.items():
            total += _deep_bytes(key, seen) + _deep_bytes(value, seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            total += _deep_bytes(item, seen)
    elif hasattr(obj, "__dict__"):
        total += _deep_bytes(vars(obj), seen)
    return total


def _outcome(store_id: str, cluster_id: str) -> dict[str, Any]:
    """A ``TrustEventPayload`` the door accepts: every required field, nothing extra.

    ``extra="forbid"`` on the contract model means a body missing ``event.payload`` — or
    carrying a key the schema does not declare — is a 400 for a reason that has nothing to do
    with this file, which would make every assertion below vacuous. ``delta`` is non-zero
    because :func:`~exchange.policy.routes._record_outcome` folds nothing for a zero delta and
    would never reach the posterior book.
    """
    return {
        "store_id": store_id,
        "event": {
            "event_id": "ev-0001",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "feedback",
            "payload": {},
        },
        "dim": "price_honored",
        "delta": 1.0,
        "pseudonymous_context": {"cluster_id": cluster_id, "pseudonym": "psn-0001"},
    }


def _book_bytes(client: TestClient) -> int:
    """What the posterior book this app built retains, or 0 if it was never built.

    Zero is the honest answer for a run whose every request was refused: ``_posteriors`` creates
    the book on first use, so a door that refuses before reaching it leaves ``app.state`` with
    no ``bandit_posteriors`` at all.
    """
    book = getattr(client.app.state, "bandit_posteriors", None)
    return 0 if book is None else _deep_bytes(book)


def _drive(store: Any, cluster: Any, sends: int) -> tuple[set[int], int]:
    """``sends`` POSTs against a freshly built app. Returns the statuses seen and bytes kept."""
    with TestClient(create_app()) as client:
        statuses = {
            client.post("/internal/outcomes", json=_outcome(store(i), cluster(i))).status_code
            for i in range(sends)
        }
        return statuses, _book_bytes(client)


# =====================================================================================
# Arming — the attack has to be one the door's OTHER caps would not have stopped
# =====================================================================================


def test_the_route_is_actually_served_and_the_honest_outcome_is_recorded() -> None:
    """Everything below is worthless if this door 404s or refuses the honest body.

    Both halves are asserted: the router is mounted by ``create_app``'s discovery glob, and a
    well-formed outcome answers 204 and leaves a posterior book behind. A test that measured
    "nothing was retained" against a route that answers 404 would pass forever.
    """
    app = create_app()
    assert "exchange.policy.routes" in app.state.mounted_routers
    with TestClient(app) as client:
        recorded = client.post("/internal/outcomes", json=_outcome("store-1", "cluster-1"))
        assert recorded.status_code == 204
        assert getattr(client.app.state, "bandit_posteriors", None) is not None
        honest = _book_bytes(client) / (1024 * 1024)
    assert 0 < honest < HONEST_CEILING_MIB, f"the honest book measured {honest:.3f} MiB"


def test_the_hostile_identifier_rides_under_the_doors_other_caps() -> None:
    """A 30,000-character name is nowhere near the body ceiling, so nothing else refuses it.

    ``DEFAULT_MAX_OUTCOME_BYTES`` is 64 KiB and is applied while STREAMING, so if the attack
    only worked by exceeding it there would be no defect here at all. This pins that the vector
    is the identifier's length and not the body's weight.
    """
    body = json.dumps(_outcome("A" * HOSTILE_CHARS, "cluster-1")).encode()
    assert len(body) < DEFAULT_MAX_OUTCOME_BYTES, (
        f"the hostile body is {len(body)} bytes against a {DEFAULT_MAX_OUTCOME_BYTES}-byte "
        "ceiling; it must ride UNDER it or this file is measuring the wrong cap"
    )


# =====================================================================================
# The bound
# =====================================================================================


@pytest.mark.parametrize("field", RETAINED_IDENTIFIER_FIELDS)
def test_a_retained_identifier_over_the_ceiling_is_refused(field: str) -> None:
    """Both names the book KEEPS are bounded, not just the one that was reported.

    Parametrized over :data:`~exchange.policy.routes.RETAINED_IDENTIFIER_FIELDS` rather than
    over a list written here, so a third retained identifier added to this door arrives with a
    test already pointing at it. ``cluster_id`` is in that tuple because it is the same defect:
    it is caller-chosen, it becomes a key in ``_clusters``, and at 30,000 characters over 256
    sends it retained 0.935 MiB on its own.
    """
    ceiling = identifier_ceiling()
    over = "z" * (ceiling + 1)
    payload = _outcome("store-1", "cluster-1")
    if field == "store_id":
        payload["store_id"] = over
    else:
        payload["pseudonymous_context"]["cluster_id"] = over

    with TestClient(create_app()) as client:
        response = client.post("/internal/outcomes", json=payload)
        assert response.status_code == 400, (
            f"{field} at {len(over)} characters was accepted; the value becomes a dict key "
            "that lives for the life of the process"
        )
        detail = response.json()["detail"]
        assert field in detail and str(ceiling) in detail
        # The refusal must not echo the caller's value back — only its length and the field's
        # own name, which is a literal from `RETAINED_IDENTIFIER_FIELDS`.
        assert over not in detail
        assert _book_bytes(client) == 0, "a refused outcome must leave nothing behind"


@pytest.mark.parametrize("field", RETAINED_IDENTIFIER_FIELDS)
def test_a_retained_identifier_exactly_at_the_ceiling_is_still_recorded(field: str) -> None:
    """A ceiling, not an off-by-one refusal of every long-ish name.

    The direction that matters for a learning signal: refusing an outcome the trust service
    legitimately reported would silently stop the bandit learning about that store, and a bound
    that overshoots by one is how that happens.
    """
    ceiling = identifier_ceiling()
    at = "z" * ceiling
    payload = _outcome("store-1", "cluster-1")
    if field == "store_id":
        payload["store_id"] = at
    else:
        payload["pseudonymous_context"]["cluster_id"] = at

    with TestClient(create_app()) as client:
        assert client.post("/internal/outcomes", json=payload).status_code == 204
        assert client.app.state.bandit_posteriors is not None


def test_the_ceiling_is_the_one_the_auction_door_already_enforces() -> None:
    """Imported, never restated — the same discipline ``MAX_ROSTER_ENTRIES`` follows.

    A separate number here would be a second opinion about what an identifier is, and the two
    would drift. This is asserted rather than assumed because the whole argument for refusing
    (rather than clipping) rests on it: a store id this door would accept and the auction door
    would refuse could never name a store the exchange has rostered.
    """
    from exchange.auction.routes import MAX_IDENTIFIER_LENGTH  # noqa: PLC0415

    assert identifier_ceiling() == MAX_IDENTIFIER_LENGTH


@pytest.mark.parametrize(
    ("case", "store", "cluster"),
    [
        ("store_id", lambda i: "A" * HOSTILE_CHARS + f"-{i}", lambda i: "cluster-1"),
        ("cluster_id", lambda i: "store-1", lambda i: "B" * HOSTILE_CHARS + f"-{i}"),
        (
            "both",
            lambda i: "A" * HOSTILE_CHARS + f"-{i}",
            lambda i: "B" * HOSTILE_CHARS + f"-{i}",
        ),
    ],
)
def test_hostile_identifiers_do_not_grow_the_posterior_book(
    case: str, store: Any, cluster: Any
) -> None:
    """The measurement, in RETAINED BYTES, the way T-349's own gate grades this axis.

    Presence of a bound is not the property — the book's SIZE is, which is why this asserts on
    bytes rather than on a status code alone. Pre-repair, all 256 sends answered 204 and the
    book grew to 7.416 MiB (``store_id``), 0.935 MiB (``cluster_id``) and 9.852 MiB (both, at
    512 sends). With the bound the book is never even created.
    """
    statuses, retained = _drive(store, cluster, SENDS)
    mib = retained / (1024 * 1024)
    assert statuses == {400}, f"{case}: the door answered {sorted(statuses)} on a hostile name"
    assert mib < HONEST_CEILING_MIB, (
        f"{case}: {SENDS} unauthenticated sends grew the posterior book to {mib:.3f} MiB; the "
        "honest book is ~0.003 MiB and both names are keys the caller mints"
    )


def test_the_caps_that_bound_the_COUNT_are_still_the_ones_that_bound_the_count() -> None:
    """The length bound is a second bound, not a replacement for the two that already exist.

    ``_stores`` / ``_clusters`` evict at :data:`DEFAULT_BANDIT_STORES` and
    :data:`DEFAULT_BANDIT_CLUSTERS`, which is what made the growth flat from 256 sends to 2048
    and kept this a bound rather than an unbounded leak. Asserted here so a future change that
    reaches for "we cap the length now" as a reason to drop them has to fail this first.
    """
    statuses, _retained = _drive(
        lambda i: f"store-{i}", lambda i: f"cluster-{i}", DEFAULT_BANDIT_STORES + 64
    )
    assert statuses == {204}
    with TestClient(create_app()) as client:
        for index in range(DEFAULT_BANDIT_STORES + 64):
            client.post("/internal/outcomes", json=_outcome(f"store-{index}", f"cluster-{index}"))
        book = client.app.state.bandit_posteriors
        assert len(book._stores) == DEFAULT_BANDIT_STORES
        assert len(book._clusters) == DEFAULT_BANDIT_CLUSTERS
