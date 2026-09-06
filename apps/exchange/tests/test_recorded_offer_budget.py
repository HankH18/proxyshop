"""The recorded offer's size bound has to reach the LEAVES of a value, not its outer length.

    PROXYSHOP_WORKER=1 PROXYSHOP_GATE_WORKER=1 \
        .venv/bin/python -m pytest apps/exchange/tests/test_recorded_offer_budget.py -q

**The defect this file grades, as measured over the HTTP door before the repair.**
``auction.routes._recordable_offer`` bounded ``len(value) > 64`` on the OUTER container of
each kept value and never descended, so ``{"currency": [[0] * 80000]}`` presented an outer
length of ``1``, passed trivially, and was recorded whole — then deep-copied once per roster
ROW by ``InMemoryAuctionBids.record``. 500 duplicate rows naming one store, one hostile bid
of **234.6 KiB** — comfortably under ``composition.MAX_BID_RESPONSE_BYTES`` (256 KiB), so the
production reply cap never saw it::

    outer-length bound   ->  HTTP 201, 500 records, book retained 339.82 MiB, peak RSS 428 MiB
    NO bound whatsoever  ->  HTTP 201, 500 records, book retained 339.82 MiB
    recursive budget     ->  HTTP 201,   0 records, book retained   0.00 MiB, peak RSS  82 MiB

**The middle line is the reason this file exists in this shape.** With ``_recordable_offer``
replaced by a bare projection — no bound of any kind — the retained size was *identical*.
Against a nested payload the old bound was not weak, it was inert, and a test that merely
asserted "some bound is present" would have been green throughout. So the assertions here are
on the RETAINED BYTES the book holds, which is the thing that actually differs.

The container is 256 MiB (``compose.yaml``'s ``mem_limit``), which one unauthenticated request
of a quarter of a megabyte exceeded.

Nothing here reads or matches the source text of the file it grades — every assertion drives
``POST /auctions`` or calls the projection and measures what came back.
"""

from __future__ import annotations

import json
import sys
import tracemalloc
from array import array
from collections import OrderedDict, UserList, UserString, deque
from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from exchange.accept import use_registered_domains
from exchange.accept.routes import InMemoryAuctionBids, configure_accept
from exchange.auction.routes import (
    MAX_RECORDED_OFFER_DEPTH,
    MAX_RECORDED_OFFER_ITEMS,
    MAX_RECORDED_OFFER_VALUE_CHARS,
    RECORDED_OFFER_ACCEPTED_TYPES,
    RECORDED_OFFER_FIELDS,
    _recordable_offer,
    collected_bid_records,
    configure_auctions,
)
from exchange.composition import MAX_BID_RESPONSE_BYTES
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.serving import configure_ranking
from fastapi.testclient import TestClient

#: The padding one hostile value carries. Chosen so the whole bid reply lands just under
#: ``MAX_BID_RESPONSE_BYTES``; ``test_the_hostile_bid_is_armed`` asserts that it does, because
#: a payload the production cap would have refused proves nothing about this one.
PAD = 80_000

#: Roster rows in the end-to-end case. Fewer than the 500 the defect was first measured at,
#: because 500 costs the test run ~340 MiB while it is RED and the discrimination does not
#: need them: at this width the pre-repair book retained ~81 MiB against an honest 0.07 MiB.
ROWS = 120

STORE_ID = "store-budget-1"
DOMAIN = "a1b2c3d4.example"
VARIANT = "9876543210"
BID_REF = "bid-a1b2c3d4"


def _deep_bytes(obj: Any, seen: set[int] | None = None) -> int:
    """Bytes retained by ``obj`` and everything it uniquely owns.

    Identity-deduplicated, which is what makes it the right instrument here: ``deepcopy``
    shares immutable strings, so a string echoed into 120 records is counted ONCE — exactly
    the reason the character cap was never the multiplier — while each copied list is counted
    every time, which is the cost being bounded.
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
    return total


def _honest_offer() -> dict[str, Any]:
    """A bid a real store makes: every value the published ``Offer`` declares, all scalar."""
    return {
        "product_ref": "prod-1",
        "variant_ref": VARIANT,
        "unit_price": 90.0,
        "total_price": 90.0,
        "currency": "USD",
        "expires_at": "2999-01-01T00:00:00Z",
        "checkout_url": f"https://{DOMAIN}/cart/{VARIANT}:1",
        # A REAL declared discount, and the roster row below states the cap that authorizes
        # it. Both halves are needed: T-177's wall in `auction/collect.py` judges any offer
        # declaring a discount, and an unauthorized one falls back `bid_price_unreconcilable`
        # — which records the exchange's own manufactured offer instead of the store's, and
        # would leave this file measuring a fallback rather than the bid it means to.
        "discount": {"type": "percentage", "value": 10},
    }


def _hostile_offer(vector: str) -> dict[str, Any]:
    """An honest offer with its padding hidden ONE LEVEL DOWN in ``vector``.

    Every shape here has an outer length of 1 or 2, which is the whole trick: the bound that
    read only the outermost container measured ``1`` and waved 80,000 items through.
    """
    offer = _honest_offer()
    if vector == "currency":
        offer["currency"] = [[0] * PAD]
    elif vector == "variant_ref":
        offer["variant_ref"] = [[0] * PAD]
    elif vector == "discount":
        # `_recordable_offer` projects `discount` onto {type, value}. That projection is a
        # whitelist, not a bound: the padding rides through it intact inside `type`.
        offer["discount"] = {"type": [[0] * PAD], "value": 0}
    else:  # pragma: no cover - a typo in a parametrize id must not pass silently
        raise AssertionError(f"unknown vector {vector!r}")
    return offer


def _bid_reply(offer: dict[str, Any]) -> dict[str, Any]:
    return {
        "store_id": STORE_ID,
        "bid": {
            "bid_id": BID_REF,
            "bid_ref": BID_REF,
            "store_id": STORE_ID,
            "store_domain": DOMAIN,
            # Round-tripped so every roster row gets its OWN object graph, exactly as 120
            # separate HTTP replies would. Handing back one shared object would let the
            # book's `deepcopy` be the only copy and understate the real cost.
            "offer": json.loads(json.dumps(offer)),
            "claims": [],
        },
    }


@pytest.fixture(autouse=True)
def unwired() -> Iterator[None]:
    """Leave the process-wide registered-domain seam as this file found it.

    ``configure_accept(registered_domains=…)`` turns a module-level seam as well as the app's,
    so a file that wires one and does not restore it changes what every later test in the
    process reads. Same fixture, same reason, as ``test_fallback_handoff.py``'s.
    """
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


def _deployed(offer: dict[str, Any]) -> TestClient:
    """A completely wired exchange whose one store bids ``offer``.

    Every seam a real deployment sets is set, and each is load-bearing for this gate: with no
    trust snapshot every candidate is excluded ``blacklist_unreadable``, with no registered
    domain every candidate is excluded on C10, and an excluded candidate is never recorded —
    so an under-wired app would retain nothing and pass without measuring anything.
    """

    class _Solicitor:
        def solicit(self, store: Any) -> Any:
            return _bid_reply(offer)

        __call__ = solicit

    known = {STORE_ID: ELIGIBLE}
    app = create_app()
    configure_auctions(app, eligibility=StaticSellerEligibility(known), solicitor=_Solicitor())
    configure_ranking(
        app,
        trust_snapshot={
            STORE_ID: {
                "store_id": STORE_ID,
                "blacklisted": False,
                "score": 0.9,
                "confidence": 0.9,
                "low_data": False,
            }
        },
    )
    configure_accept(
        app,
        registered_domains=lambda store_id: DOMAIN if str(store_id) == STORE_ID else None,
        eligibility=StaticSellerEligibility(known),
        checkout_mode="redirect",
    )
    return TestClient(app)


def _run_auction(offer: dict[str, Any], rows: int) -> tuple[int, list[dict[str, Any]], int]:
    """``POST /auctions`` with ``rows`` duplicate roster rows. Returns status, records, bytes."""
    body = {
        "intent": {"query": "a purchase", "hard_constraints": []},
        "roster": [
            {
                "store_id": STORE_ID,
                "tier": 1,
                "product_ref": "prod-1",
                "list_price": 100.0,
                # Authorizes the 10% the honest offer declares. Without it every bid here
                # falls back `bid_price_unreconcilable` and the book records a manufactured
                # offer, so the memory this file measures would not be the store's.
                "max_discount_pct": 20.0,
            }
            for _ in range(rows)
        ],
        "bid_timeout_seconds": 1.0,
    }
    with _deployed(offer) as client:
        response = client.post("/auctions", json=body)
        book = getattr(client.app.state, "auction_bids", None)
        stored = getattr(book, "_bids", {}) if book is not None else {}
        records = [record for _, recs in stored.values() for record in recs]
        retained = _deep_bytes(stored)
    return response.status_code, records, retained


# =====================================================================================
# Arming — the payload has to be one the production caps would NOT have stopped
# =====================================================================================


@pytest.mark.parametrize("vector", ["currency", "variant_ref", "discount"])
def test_the_hostile_bid_is_armed(vector: str) -> None:
    """The attack rides UNDER the reply-size cap and past the roster cap.

    Without this, a green suite would be consistent with the padding simply being too big to
    arrive — which is a statement about ``MAX_BID_RESPONSE_BYTES``, not about the bound this
    file grades.
    """
    encoded = len(json.dumps(_bid_reply(_hostile_offer(vector))).encode("utf-8"))
    assert encoded < MAX_BID_RESPONSE_BYTES, (
        f"{vector}: the bid is {encoded} bytes, which the {MAX_BID_RESPONSE_BYTES}-byte reply "
        "cap would refuse before the offer bound ever saw it — this corpus proves nothing"
    )
    assert encoded > 128 * 1024, f"{vector}: {encoded} bytes is not a payload worth measuring"


def test_the_honest_offer_is_armed() -> None:
    """The honest bid names every whitelisted field, so a regression cannot hide in a gap."""
    honest = _honest_offer()
    missing = [field for field in RECORDED_OFFER_FIELDS if field not in honest]
    assert not missing, f"the honest offer says nothing about {missing}"


# =====================================================================================
# The defect
# =====================================================================================


@pytest.mark.parametrize("vector", ["currency", "variant_ref", "discount"])
def test_padding_nested_one_level_down_cannot_reach_the_book(vector: str) -> None:
    """A value whose outer container is short but whose CONTENTS are not is refused.

    Measured before the repair, at ``ROWS`` rows: HTTP 201, 120 records, ~81 MiB retained
    against a 256 MiB container. After: HTTP 201, 0 records, ~0 MiB.
    """
    status, records, retained = _run_auction(_hostile_offer(vector), ROWS)

    assert status == 201, f"{vector}: the auction itself must still answer, got {status}"
    mib = retained / (1024 * 1024)
    assert mib < 8.0, (
        f"{vector}: the book retained {mib:.2f} MiB from ONE unauthenticated request of "
        f"under {MAX_BID_RESPONSE_BYTES // 1024} KiB — the size bound did not reach the "
        "padding, which sits one level below the container it measured"
    )
    assert not records, (
        f"{vector}: {len(records)} bid(s) recorded. A bid whose offer does not fit the budget "
        "is DROPPED, not truncated and not emptied — a recorded offer with its checkout URL "
        "removed reads as a fallback and would take the R10 handoff"
    )


def test_a_value_too_deep_to_copy_is_refused_before_anything_copies_it() -> None:
    """Depth is a second, far cheaper lever, and it faults rather than fills memory.

    ``json.loads`` parses a 2000-deep list happily — the C scanner's limit is far above the
    interpreter's — while ``InMemoryAuctionBids.record``'s ``deepcopy`` recurses once per
    level and raises ``RecursionError`` from roughly 600. Measured before the repair: 4.2 KiB
    of JSON took ``POST /auctions`` down with ``RecursionError`` out of
    ``copy._deepcopy_list``. Three orders of magnitude cheaper than the memory attack above.
    """
    nested: Any = 0
    for _ in range(2_000):
        nested = [nested]
    offer = _honest_offer()
    offer["currency"] = nested

    encoded = len(json.dumps(_bid_reply(offer)).encode("utf-8"))
    assert encoded < MAX_BID_RESPONSE_BYTES, "the stack bomb must ride under the reply cap"

    status, records, _ = _run_auction(offer, 4)
    assert status == 201, f"a stack bomb must be refused, not faulted on: got {status}"
    assert not records, f"{len(records)} bid(s) recorded from a value too deep to copy"


# =====================================================================================
# What must NOT change — T-349, and the character cap that was measured working
# =====================================================================================


def test_an_honest_bid_is_still_recorded_with_the_fields_the_accept_path_reads() -> None:
    """T-349 is what this whole stream repaired; a bound that empties the book undoes it.

    The book used to hold ``offer: {}`` for every bid. Recording the store's real published
    fields is the repair, so a size bound that drops or empties an HONEST offer would be a
    regression dressed as a hardening.
    """
    honest = _honest_offer()
    status, records, retained = _run_auction(honest, ROWS)

    assert status == 201
    assert len(records) == ROWS, f"expected one record per roster row, got {len(records)}"
    offer = records[0]["offer"]
    assert offer, "the recorded offer is empty — this is the T-349 defect back again"
    assert offer["checkout_url"] == honest["checkout_url"]
    assert offer["variant_ref"] == honest["variant_ref"]
    assert offer["currency"] == "USD"
    assert offer["unit_price"] == 90.0
    assert offer["total_price"] == 90.0
    assert offer["expires_at"] == honest["expires_at"]
    assert offer["product_ref"] == "prod-1"
    assert offer["discount"] == {"type": "percentage", "value": 10}
    mib = retained / (1024 * 1024)
    assert mib < 4.0, f"an honest auction should be cheap; retained {mib:.2f} MiB"


def test_every_whitelisted_string_field_at_the_character_cap_is_still_recorded() -> None:
    """The character budget stays PER VALUE, because characters were never the multiplier.

    ``deepcopy`` returns the SAME object for a string, so 500 records hold 500 references to
    one 4096-character string rather than 500 copies of it. Measured: every whitelisted field
    at exactly the cap, 500 duplicate rows — book retained 0.29 MiB, the same as an honest
    auction. A bound that started charging strings against a shared budget would refuse a bid
    for costing nothing, so this is asserted rather than left to taste.
    """
    offer: dict[str, Any] = {
        field: "x" * MAX_RECORDED_OFFER_VALUE_CHARS
        for field in RECORDED_OFFER_FIELDS
        if field != "discount"
    }
    offer["discount"] = {"type": "percentage", "value": 10}

    kept = _recordable_offer(offer)
    assert kept is not None, (
        "seven values at exactly the cap each spend their own budget and none exceeds it; "
        "dropping this bid would be the character cap turning into a per-offer one"
    )
    for field, value in offer.items():
        assert kept[field] == value, field


def test_the_character_cap_is_not_multiplied_by_a_duplicated_roster() -> None:
    """The same, over the HTTP door: a padded STRING does not multiply through the book.

    ``currency`` is padded rather than a priced or routed field, so the T-177 price wall and
    the C10 domain check see exactly what they see in the honest case and this measures the
    book alone.
    """
    offer = _honest_offer()
    offer["currency"] = "x" * MAX_RECORDED_OFFER_VALUE_CHARS

    status, records, retained = _run_auction(offer, ROWS)
    assert status == 201
    assert len(records) == ROWS, f"a bid at the character cap must record; got {len(records)}"
    assert records[0]["offer"]["currency"] == offer["currency"]
    mib = retained / (1024 * 1024)
    assert mib < 4.0, f"shared strings should not multiply; retained {mib:.2f} MiB"


def test_one_character_over_the_cap_still_drops_the_bid() -> None:
    """The behaviour the constant's docstring describes, unchanged by the recursion."""
    offer = _honest_offer()
    offer["checkout_url"] = "x" * (MAX_RECORDED_OFFER_VALUE_CHARS + 1)
    assert _recordable_offer(offer) is None


# =====================================================================================
# The projection itself, at the boundaries
# =====================================================================================


def test_a_non_mapping_offer_is_recorded_as_empty_rather_than_dropped() -> None:
    """Unchanged: ``None`` means "drop the bid", and a shapeless offer is not that case."""
    for shapeless in ("a string", 7, None, ["a", "list"]):
        assert _recordable_offer(shapeless) == {}, shapeless


def test_the_slot_budget_is_charged_across_the_whole_offer_not_per_field() -> None:
    """64 slots spread over eight fields is the same retained memory as 64 in one.

    The same discipline ``MAX_HARD_CONSTRAINT_BYTES`` states for hard constraints. A per-field
    budget would let a store spend it once per whitelisted key.
    """
    per_field = MAX_RECORDED_OFFER_ITEMS // 2
    offer = {
        "currency": ["a"] * per_field,
        "variant_ref": ["b"] * per_field,
        "product_ref": ["c"] * per_field,
    }
    assert _recordable_offer(offer) is None, (
        "three fields at half the budget each spend 150% of it; a per-field budget would "
        "have recorded this"
    )


def test_a_value_exactly_at_the_slot_budget_is_kept() -> None:
    """The cap is a ceiling, not an off-by-one refusal of everything nested at all."""
    offer = {"currency": ["a"] * MAX_RECORDED_OFFER_ITEMS}
    kept = _recordable_offer(offer)
    assert kept is not None, "a value spending exactly the budget must be recordable"
    assert kept["currency"] == ["a"] * MAX_RECORDED_OFFER_ITEMS

    over = {"currency": ["a"] * (MAX_RECORDED_OFFER_ITEMS + 1)}
    assert _recordable_offer(over) is None


def test_the_bound_descends_further_than_one_level() -> None:
    """Burying the padding deeper must not buy anything either.

    A fix that special-cased "one level down" would pass the vectors above and fail here.
    """
    for depth in range(1, MAX_RECORDED_OFFER_DEPTH + 1):
        padding: Any = ["x"] * (MAX_RECORDED_OFFER_ITEMS + 1)
        for _ in range(depth):
            padding = [padding]
        assert _recordable_offer({"currency": padding}) is None, f"evaded at depth {depth}"


def test_the_walk_terminates_on_a_value_that_refers_to_itself() -> None:
    """A direct caller of ``collect_bids`` can build a cycle even though JSON cannot.

    This grades the REPAIR rather than the defect: the bound it replaced never descended, so
    it could not loop — walking to the leaves is what makes a cycle a hazard at all, and a
    walk that visited one forever would hang the auction worker outright. Every slot visited
    spends one from a finite budget, so the value is refused instead.
    """
    cycle: list[Any] = ["a"]
    cycle.append(cycle)
    assert _recordable_offer({"currency": cycle}) is None


def test_refusing_a_wide_value_does_not_first_walk_all_of_it() -> None:
    """The check must stop AT the budget, not after reading everything and then refusing.

    This grades the repair against a hole the repair itself can open. The walk runs once per
    roster row — 500 times on the duplicated roster the memory attack uses — so a bound that
    reads a whole 30,000-key mapping before saying no hands the same attacker a CPU
    amplification on the axis the bound exists to close. Measured, 500 calls against one
    30,000-key mapping: 0.8186s when the pairs were materialised first, 0.0028s when they were
    consumed lazily. Same verdict; 292x the work to reach it.

    Asserted on ITEMS CONSUMED rather than wall clock, so it grades the algorithm rather than
    the machine it ran on.
    """
    consumed = 0
    width = 30_000

    class _CountingMapping(Mapping[str, int]):
        """A mapping that reports how much of itself the walk actually read."""

        def __getitem__(self, key: str) -> int:
            return 0

        def __iter__(self) -> Iterator[str]:
            nonlocal consumed
            for index in range(width):
                consumed += 1
                yield f"k{index}"

        def __len__(self) -> int:
            return width

    assert _recordable_offer({"currency": _CountingMapping()}) is None
    assert consumed <= MAX_RECORDED_OFFER_ITEMS + 2, (
        f"the walk read {consumed} of {width} keys before refusing a value it could have "
        f"refused after {MAX_RECORDED_OFFER_ITEMS}; that is work an attacker chooses the "
        "size of, repeated once per roster row"
    )


def test_a_bytearray_is_charged_as_characters_rather_than_waved_through() -> None:
    """``bytes`` is charged; its mutable twin has to be too.

    JSON produces neither, but ``collect_bids`` is a public seam and a mutable buffer is the
    one uncharged type that ``deepcopy`` would genuinely COPY per record rather than share.
    """
    assert _recordable_offer({"currency": bytearray(MAX_RECORDED_OFFER_VALUE_CHARS + 1)}) is None
    assert _recordable_offer({"currency": bytearray(8)}) == {"currency": bytearray(8)}


# =====================================================================================
# The accept-list is a WHITELIST — anything not on it is refused
# =====================================================================================
#
# These are the gate for the branch `847c9b6` shipped and never graded. That commit turned the
# walk's final branch from `else: continue` — a BLACKLIST that waved through every type it had
# not been told about — into `else: return None`, a whitelist. MEASURED with the commit
# reverted in a scratch tree: the whole `apps/exchange/tests` suite was 1003 passed / 10
# xfailed / 0 failed, byte-identical to the baseline, while
# `_recordable_offer({"currency": deque(range(200_000))})` came back RECORDED with 200,000
# items. So the repair could regress to the permissive behaviour and nothing anywhere noticed.
#
# **What these tests protect, and what they do NOT claim.** No reachable HTTP path can supply a
# now-refused type: the production solicitor parses every bid reply with `json.loads`
# (`composition.HttpBidSolicitor.solicit`, `composition.py:820`), whose output is exactly
# str / dict / list / int / float / bool / None — all of them on the accept-list. So this is a
# gate against FUTURE regression at a public seam, not the closing of a live hole. `collect_bids`
# is called with whatever the wired solicitor returns, and `configure_auctions(solicitor=...)`
# is a supported deployment seam; an in-process solicitor that returns an `array` or a `deque`
# — or any object holding a list — is the case the whitelist fails closed on.

#: Elements each corpus value carries. Over `MAX_RECORDED_OFFER_ITEMS` on purpose, so that an
#: on-list container of the same width is refused by the SLOT budget: that keeps the corpus
#: honest about which bound is doing the work for which value.
CORPUS_ITEMS = 1024


class _HoldsAList:
    """An ordinary object with a list attribute — a type no blacklist could be written against.

    Constructed HERE rather than taken from the stdlib because that is the whole argument for a
    whitelist: this class did not exist when the walk was written, `deepcopy` copies its
    attribute per record exactly as it copies a `list`, and no enumeration of "types to refuse"
    could ever have named it.
    """

    def __init__(self, payload: list[int]) -> None:
        self.payload = payload


def _corpus() -> dict[str, Any]:
    """One shared payload, wrapped every way a caller of ``collect_bids`` could wrap it.

    Both halves matter. The on-list wrappers are what make the partition below non-vacuous —
    without them a whitelist narrowed to nothing would satisfy the assertions trivially — and
    the off-list ones are the vectors.
    """
    payload = list(range(CORPUS_ITEMS))
    text = "x" * CORPUS_ITEMS
    return {
        "list": payload,
        "tuple": tuple(payload),
        "set": set(payload),
        "frozenset": frozenset(payload),
        "dict": dict.fromkeys(payload, 0),
        "OrderedDict": OrderedDict.fromkeys(payload, 0),
        "str": text,
        "bytes": text.encode(),
        "bytearray": bytearray(CORPUS_ITEMS),
        "int": 10**CORPUS_ITEMS,
        "float": 1.5,
        "complex": complex(1, 2),
        "None": None,
        "bool": True,
        "deque": deque(payload),
        "array": array("q", payload),
        "UserList": UserList(payload),
        "UserString": UserString(text),
        "memoryview": memoryview(bytearray(CORPUS_ITEMS)),
        "range": range(CORPUS_ITEMS),
        "SimpleNamespace": SimpleNamespace(payload=payload),
        "object holding a list": _HoldsAList(payload),
    }


def _partitioned() -> tuple[dict[str, Any], dict[str, Any]]:
    """The corpus split BY THE ACCEPT-LIST THE SOURCE USES, never by a list restated here.

    This is the difference between gating the class and gating one type. Naming ``deque`` in an
    assertion would grade exactly ``deque``; deriving the hostile half from
    :data:`RECORDED_OFFER_ACCEPTED_TYPES` grades "not on the accept-list", so a fifth exotic
    container needs no test change — and widening the whitelist on purpose moves that value into
    the accepted half rather than leaving a test failing on a type the code now allows.
    """
    corpus = _corpus()
    accepted = {
        name: value
        for name, value in corpus.items()
        if isinstance(value, RECORDED_OFFER_ACCEPTED_TYPES)
    }
    refused = {name: value for name, value in corpus.items() if name not in accepted}
    return accepted, refused


def test_the_off_accept_list_corpus_is_armed() -> None:
    """The partition has to be non-empty on BOTH sides or the gate below proves nothing.

    A whitelist widened to ``object`` would empty the hostile half and every assertion in
    :func:`test_a_value_whose_type_is_off_the_accept_list_is_refused_rather_than_recorded`
    would pass over an empty loop. A whitelist narrowed to nothing would empty the accepted
    half and make the refusals meaningless. Both are asserted against here rather than assumed.
    """
    accepted, refused = _partitioned()
    assert len(accepted) >= 8, f"the accept-list matched almost nothing: {sorted(accepted)}"
    assert len(refused) >= 5, (
        "every wrapper in the corpus is now on the accept-list, so the gate below iterates "
        f"over nothing; accepted={sorted(accepted)}"
    )
    # The archetype has to be in the hostile half, or the corpus drifted away from the defect.
    assert "object holding a list" in refused


def test_a_value_whose_type_is_off_the_accept_list_is_refused_rather_than_recorded() -> None:
    """The property `847c9b6` established: not on the accept-list means refused, not waved past.

    Reverting that commit — restoring ``else: continue`` — records every one of these instead,
    and every one of them is a value ``InMemoryAuctionBids.record`` would then ``deepcopy``
    once per roster row.
    """
    _accepted, refused = _partitioned()
    recorded = {name: _recordable_offer({"currency": value}) for name, value in refused.items()}
    leaked = {name: kept for name, kept in recorded.items() if kept is not None}
    assert not leaked, (
        "these types are not on `RECORDED_OFFER_ACCEPTED_TYPES` and were recorded anyway, "
        f"which is the blacklist behaviour: {sorted(leaked)}"
    )

    # Positive control, in the same test so a blanket-refusing `_recordable_offer` cannot pass
    # it: an ON-list value of a size the budgets allow still comes back recorded.
    assert _recordable_offer({"currency": ["USD"]}) == {"currency": ["USD"]}


def test_an_off_accept_list_value_is_refused_at_every_depth_a_bid_can_reach_it() -> None:
    """Burying the wrapper does not buy anything either — the same discipline as the slot budget.

    A repair that checked only the outermost value would pass the test above and leave the
    hole one level down, which is exactly how the bound this file was written for failed.
    ``discount`` is included by name because :func:`_recordable_offer` re-projects it, so it is
    the one field whose value is rebuilt after the caller wrote it.
    """
    _accepted, refused = _partitioned()
    for name, value in refused.items():
        for depth in range(1, MAX_RECORDED_OFFER_DEPTH):
            buried: Any = value
            for _ in range(depth):
                buried = [buried]
            assert _recordable_offer({"currency": buried}) is None, (
                f"{name} evaded the accept-list at depth {depth}"
            )
        assert _recordable_offer({"discount": {"type": value, "value": 0}}) is None, (
            f"{name} evaded the accept-list through the `discount` projection"
        )


#: Roster rows and element count for the multiplication measurement below. Small enough that a
#: RED run costs ~13 MiB rather than the 792.7 MiB the defect was first measured at, wide
#: enough that the two verdicts are an order of magnitude apart.
OFF_LIST_ROWS = 32
OFF_LIST_WIDTH = 50_000


def test_an_off_accept_list_container_does_not_multiply_through_the_bid_book() -> None:
    """The consequence, in RETAINED BYTES, the way T-349's own gate grades this path.

    Refusal is the mechanism; what it buys is that ``InMemoryAuctionBids.record``'s ``deepcopy``
    never runs over a container the walk did not charge. Measured here at 32 rows x 50,000
    elements, over the ``collected_bid_records`` -> ``record`` seam the served route uses:

        blacklist (``else: continue``)    -> 32 records, book grew  12.62 MiB
        whitelist (``else: return None``) ->  0 records, book grew   0.00 MiB

    Asserted on the allocation the ``record`` call itself retains, so the corpus built before it
    is not counted and the number is the book's own growth.
    """
    payload = list(range(OFF_LIST_WIDTH))
    candidates = [
        {
            "eligible": True,
            "bid_id": f"bid-{index}",
            "store_id": STORE_ID,
            # Its OWN deque per row, exactly as 32 separate solicitor replies would be, so the
            # `deepcopy` in `record` is not the only copy and the cost is not understated.
            "offer": {"currency": deque(payload)},
        }
        for index in range(OFF_LIST_ROWS)
    ]
    records = collected_bid_records(candidates, [])

    book = InMemoryAuctionBids()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        book.record("auction-off-list", records)
        grew = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    mib = grew / (1024 * 1024)

    assert records == [], (
        f"{len(records)} bids carrying an off-accept-list container were recorded; each is "
        "deep-copied once per roster row"
    )
    assert mib < 1.0, f"the book grew {mib:.2f} MiB on bids that should never have been recorded"
