"""The walk over a claim's opaque ``value``, and the properties it is not allowed to lose.

``Claim.value`` is the one field in the protocol with no schema: the published bundle declares it
``{}`` and the generated model types it ``Any``. Everything the boundary knows about a claim's
provenance and its authorisation therefore has to be found by WALKING that value, and a walk over
caller-shaped data is a wall with four ways to fail — it can miss what is hidden in a shape it did
not anticipate, it can refuse an honest payload, it can be made not to terminate, and it can
disagree with the other door about any of the above.

**Why this file exists.** The walk was added for T-161 and T-162 and then rebuilt twice, because an
adversarial sweep found thirteen defects across the two attempts — a reopened T-162, a bypass by
list-wrapping, a bypass by padding, false refusals on real documents already in this repo, a
denial-of-service regression, two crashes, and four cross-door divergences. Every one of those was
invisible to the suite: the ticket gates for T-161 and T-162 stayed green through all of it, and
each new behaviour could be deleted with the whole package still passing. A property nothing
asserts on is a property the next lane removes by accident.

Nothing here is an xfail and nothing here is a reproduction. These are the standing properties.
"""

from __future__ import annotations

import json
import pathlib
import time
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import pytest

from packages.contracts import EXTERNAL_PATH, HOSTED_PATH, validate_bid
from packages.contracts.tests._fixtures_protocol import (
    ASSERTED_PROVENANCE,
    HOOK_PROVENANCE,
    make_bid,
    make_claim,
    make_offer,
    make_snapshot_table,
)

NOW = "2026-06-01T00:00:00Z"
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: The same honest roster the reproduction gates use: it prices the fixture offer's product and
#: authorises more depth than any bid here declares, so the price wall refuses nothing and the
#: only thing that can refuse a bid in this file is the walk under test. Without it every
#: assertion below would pass on `price_unreconcilable` and measure nothing.
_HONEST_ROSTER: dict[str, Any] = {"prod-1": {"list_price": 49.0, "max_discount_pct": 25.0}}


def _check(bid: Any, path: str = HOSTED_PATH) -> Any:
    return validate_bid(
        bid,
        path=path,
        trust_snapshot=make_snapshot_table(),
        now=NOW,
        list_prices=_HONEST_ROSTER,
    )


def _with_value(value: Any, key: str = "policy", path: str = HOSTED_PATH) -> Any:
    claim = {"key": key, "value": value, "provenance": dict(HOOK_PROVENANCE)}
    return _check(make_bid(claims=[claim]), path)


def _wrap(depth: int, inner: Any) -> Any:
    value = inner
    for _ in range(depth):
        value = {"a": value}
    return value


def _unwalkable(result: Any) -> list[str]:
    return [reason for reason in result.reasons if reason.startswith("claim_value_unwalkable")]


# =============================================================================================
# The control. Every assertion below is about a `seller_asserted` block being FOUND, so the
# suite has to know the finder is armed at all.
# =============================================================================================


def test_the_walk_is_armed() -> None:
    """A hook-provenanced claim carrying nothing unusual is admitted, and the same claim with a
    top-level `seller_asserted` block is refused. If the first of these ever fails, every
    "refused" assertion below is passing for an unrelated reason."""
    assert _check(make_bid()).ok is True, _check(make_bid()).reasons

    refused = _check(make_bid(claims=[make_claim("x", "y", dict(ASSERTED_PROVENANCE))]))
    assert refused.ok is False
    assert "hosted_non_hook_provenance:0:seller_asserted" in refused.reasons


# =============================================================================================
# A provenance block is recognised WHEREVER it is written, not only under a key called
# `provenance`.
#
# Three earlier versions of this walk keyed off the wrapper shape, and each one was defeated by
# changing the shape: `{"provenance": <block>}` was caught, `{"provenance": [<block>]}` was not;
# that was fixed, and `{"provenance": [[<block>]]}` was not. A shape list is a blocklist and the
# attacker picks the shape, so the recogniser now fires on the protocol's own closed source
# vocabulary wherever the walk meets it.
# =============================================================================================


@pytest.mark.parametrize(
    ("shape", "value"),
    [
        ("under a provenance key", {"provenance": dict(ASSERTED_PROVENANCE)}),
        ("wrapped in a list", {"provenance": [dict(ASSERTED_PROVENANCE)]}),
        ("wrapped in two lists", {"provenance": [[dict(ASSERTED_PROVENANCE)]]}),
        ("a dict inside a list", {"provenance": [{"x": dict(ASSERTED_PROVENANCE)}]}),
        ("under a numeric-looking key", {"provenance": {"0": dict(ASSERTED_PROVENANCE)}}),
        ("under any other key at all", {"x": {"y": dict(ASSERTED_PROVENANCE)}}),
        ("as a bare list element", [dict(ASSERTED_PROVENANCE)]),
        ("as the whole value", dict(ASSERTED_PROVENANCE)),
        ("six wrappers down", _wrap(6, {"provenance": dict(ASSERTED_PROVENANCE)})),
        ("sixty wrappers down", _wrap(60, {"provenance": dict(ASSERTED_PROVENANCE)})),
    ],
)
def test_a_seller_asserted_block_in_a_claim_value_is_refused_on_the_hosted_path(
    shape: str, value: Any
) -> None:
    result = _with_value(value)
    assert result.ok is False, f"{shape}: laundered past the hosted door — {result.reasons}"
    assert any(r.startswith("hosted_non_hook_provenance:") for r in result.reasons), (
        f"{shape}: refused, but not for the reason this test is about — {result.reasons}"
    )


@pytest.mark.parametrize(
    ("shape", "value"),
    [
        (
            "a bare source key",
            {"source": "network", "x": {"provenance": dict(ASSERTED_PROVENANCE)}},
        ),
        (
            "a different hook source",
            {"source": "scraped", "x": {"provenance": dict(ASSERTED_PROVENANCE)}},
        ),
        (
            "an entirely honest block",
            dict(HOOK_PROVENANCE, x={"provenance": dict(ASSERTED_PROVENANCE)}),
        ),
        (
            "a lid one wrapper down",
            {"w": {"source": "network", "x": {"provenance": dict(ASSERTED_PROVENANCE)}}},
        ),
        (
            "two lids",
            {
                "source": "network",
                "a": {"source": "scraped", "b": {"provenance": dict(ASSERTED_PROVENANCE)}},
            },
        ),
        ("a lid over a list", {"source": "network", "x": [dict(ASSERTED_PROVENANCE)]}),
    ],
)
def test_an_honest_provenance_block_is_not_a_lid_the_walk_stops_at(shape: str, value: Any) -> None:
    """Recognising a block must not TERMINATE the walk through it.

    This is the failure mode that recognising-by-shape creates, and it is the one that cost the
    most to find. The recogniser used to report a block and stop, which was safe while it only
    fired under a key literally named `provenance`. Once it fired on shape, one added key turned
    any mapping into a recognised — and perfectly innocent — block, and everything underneath it
    became a region the walk never entered:

        {"source": "network", "x": {"provenance": <seller_asserted>}}

    Measured: that reopened T-161 AND T-162, on both doors, on both paths, at every claim-bearing
    site, with both language suites green and the ticket gates for both still passing. A shape
    list is a blocklist and the attacker picks the shape — including the shape of the LID.
    """
    result = _with_value(value)
    assert result.ok is False, f"{shape}: laundered under a lid — {result.reasons}"
    assert any(r.startswith("hosted_non_hook_provenance:") for r in result.reasons), result.reasons


def test_a_lid_does_not_hide_a_discount_authorisation_either() -> None:
    """The same lid, over T-162's payload rather than T-161's."""
    value = {"source": "network", "x": {"authorized_discount_pct": 25.0}}
    result = _with_value(value, path=EXTERNAL_PATH)
    assert result.ok is False or result.requires_verification is True, result.reasons


def test_the_same_block_is_admitted_and_flagged_on_the_external_path() -> None:
    """R18 is not suspended by nesting. An external agent may assert freely in `bid.claims`; what
    it may not do is have the assertion go unnoticed."""
    result = _with_value({"provenance": dict(ASSERTED_PROVENANCE)}, path=EXTERNAL_PATH)
    assert result.ok is True, result.reasons
    assert result.requires_verification is True
    assert list(result.unverified_claim_indexes) == [0]


@pytest.mark.parametrize(
    "source",
    [["seller_asserted"], 7, None, {"value": "seller_asserted"}, True],
)
def test_a_source_that_is_not_a_string_is_not_a_provenance_source(source: Any) -> None:
    """`Provenance.source` is a string enum in the published bundle, so a mapping whose `source`
    is a list or a number is not a provenance block — it is caller data, and the walk carries on
    through it as such.

    Asserted because the two doors used to disagree about it and Python was the lax one:
    `str(["seller_asserted"])` is `"['seller_asserted']"` and `String(["seller_asserted"])` is
    `"seller_asserted"`, so a source written as a one-element list was a provenance block to the
    TypeScript door and caller data to this one. Both now answer "not a block"."""
    from contracts.boundary import _declared_provenance_source  # noqa: PLC0415

    assert _declared_provenance_source({"source": source}) is None


@pytest.mark.parametrize(
    ("shape", "value"),
    [
        ("prose under a provenance key", {"provenance": "we read it off the label"}),
        ("a source outside the vocabulary", {"provenance": {"source": "our CRM"}}),
        ("a number under a provenance key", {"provenance": 7}),
        ("a hook source, nested", {"provenance": dict(HOOK_PROVENANCE)}),
        ("an ordinary structured value", {"colour": "blue", "sizes": ["s", "m"], "n": {"x": 1}}),
    ],
)
def test_a_legitimate_structured_value_is_still_admitted(shape: str, value: Any) -> None:
    """The objection that kept T-161 open was that walking arbitrary values for provenance-shaped
    dicts would reject legitimate structured values. It does not, and this is why: a mapping is
    read as provenance ONLY when its `source` is a member of the protocol's own closed
    vocabulary."""
    result = _with_value(value)
    assert result.ok is True, f"{shape}: a legitimate value was refused — {result.reasons}"


# =============================================================================================
# The bounds fail CLOSED — and they are wide enough that nothing honest reaches them.
#
# Both halves of that sentence are load-bearing and they pull in opposite directions. A bound
# that fails open publishes its own bypass: padding is free, so an attacker writes enough of it
# to spend the budget before the walk reaches the block. A bound that fails closed and is set
# too tight refuses honest bids: at depth 6 this door refused a recorded Shopify Admin response
# (13 deep), an MCP catalog reply (10), this repo's own `merchant.openapi.json` (12), and a
# merchant's 512-row shipping table that `contracts.Envelope` accepts and
# `ToolHooks.get_owner_commitments` copies verbatim.
# =============================================================================================


def test_the_bounds_are_the_numbers_they_were_measured_to_need() -> None:
    """Pinned, so that moving either one is a deliberate act with this comment in front of it.

    Nothing else in either suite constrains them, and they are wrong in BOTH directions:
    * too tight and the door refuses honest bids. At depth 6 / 512 it refused a recorded Shopify
      Admin response (12 deep), this repo's own `merchant.openapi.json` (11 deep, 591 entries) and
      a merchant's 512-row shipping table, and killed the whole bid each time.
    * too loose and the walk becomes the denial of service it exists to prevent, because every
      entry under the bound is read and sorted.

    Measured margins against every JSON document in this repo: deepest is 12 (5.3x under the
    depth bound); the largest protocol-plausible document is 1,673 entries (39x under the entry
    bound), and the largest document of any kind is `package-lock.json` at 9,176 (7.1x).
    """
    from contracts.boundary import CLAIM_VALUE_MAX_DEPTH, CLAIM_VALUE_MAX_ENTRIES  # noqa: PLC0415

    assert CLAIM_VALUE_MAX_DEPTH == 64
    assert CLAIM_VALUE_MAX_ENTRIES == 65536


def test_padding_does_not_buy_a_way_past_the_walk() -> None:
    """The bypass this fails closed against. The filler is honest — hook-provenanced blocks — and
    only its VOLUME is hostile."""
    from contracts.boundary import CLAIM_VALUE_MAX_ENTRIES  # noqa: PLC0415

    value: dict[str, Any] = {
        f"a{index:07d}": {"provenance": dict(HOOK_PROVENANCE)}
        for index in range(CLAIM_VALUE_MAX_ENTRIES + 1)
    }
    value["zzzz"] = {"provenance": dict(ASSERTED_PROVENANCE)}

    result = _with_value(value)
    assert result.ok is False, (
        "a seller_asserted block behind enough padding to exhaust the walk was ADMITTED; a bound "
        "that fails open is a published bypass"
    )
    assert _unwalkable(result), result.reasons


def test_nesting_deeper_than_the_bound_does_not_buy_a_way_past_it_either() -> None:
    from contracts.boundary import CLAIM_VALUE_MAX_DEPTH  # noqa: PLC0415

    value = _wrap(CLAIM_VALUE_MAX_DEPTH + 5, {"provenance": dict(ASSERTED_PROVENANCE)})
    result = _with_value(value)
    assert result.ok is False, "a block nested past the depth bound was admitted"
    assert _unwalkable(result), result.reasons


@pytest.mark.parametrize(
    ("shape", "value"),
    [
        ("an empty mapping", {}),
        ("an empty list", []),
        ("a string", "30 days"),
        ("a number", 7),
        ("null", None),
        # The leaf sits one step PAST the depth bound in each of these three. That is the exact
        # spot an earlier version got wrong: it refused seven wrappers around `{}` on a bound of
        # six. The wrappers themselves are all within the bound, so the only thing the walk
        # declined to enter is a leaf that had nothing in it.
        ("an empty mapping one step past the bound", _wrap(65, {})),
        ("an empty list one step past the bound", _wrap(65, [])),
        ("a scalar one step past the bound", _wrap(65, "x")),
    ],
)
def test_the_walk_does_not_claim_it_was_truncated_when_nothing_was_skipped(
    shape: str, value: Any
) -> None:
    """`claim_value_unwalkable` says "I could not finish looking". A scalar hid nothing and an
    empty container hid nothing, at any depth, so neither may produce it — an earlier version
    refused seven wrappers around `{}`, which is a false refusal on any reading."""
    result = _with_value(value)
    assert _unwalkable(result) == [], f"{shape}: falsely reported as unwalkable"
    assert result.ok is True, f"{shape}: {result.reasons}"


@pytest.mark.parametrize(
    "document",
    [
        "services/shopify-stub/fixtures/recorded/admin_orders_query.json",
        "fixtures/mcp/catalog_list_products.json",
        "packages/contracts/openapi/merchant.openapi.json",
    ],
)
def test_a_real_upstream_document_carried_as_a_claim_value_is_not_refused(document: str) -> None:
    """The realism floor for the bounds, and the reason they are the size they are.

    `Claim.value` is schema-unconstrained, so a claim may legitimately carry an upstream document
    verbatim — a recorded Shopify Admin response, an MCP catalog reply. These three are real files
    in this repo, they nest 12, 9 and 11 deep, and every one of them was REFUSED as unwalkable by
    the first version of this walk, which bounded depth at 6. A door that refuses what the
    published protocol permits, with no schema change announcing it, is a worse defect than the
    laundering it was closing: the refusal kills the whole bid, not the claim.

    Read from disk rather than pinned inline on purpose. If one of these documents grows deeper
    or wider than the walk allows, that is exactly the signal this test exists to raise — which is
    also why a missing file is a FAILURE here and not a skip. A silent skip would retire the
    realism floor the moment somebody moved a fixture.
    """
    path = _REPO_ROOT / document
    assert path.exists(), (
        f"{document} is the realism floor for the walk's bounds and it is no longer in the tree; "
        "point this at whatever replaced it rather than deleting the parameter"
    )

    result = _with_value(json.loads(path.read_text()))
    assert _unwalkable(result) == [], (
        f"{document} is a real document in this repo and the walk refused to finish reading it; "
        f"the bounds are too tight for what Claim.value is allowed to carry — {result.reasons}"
    )


def test_a_merchants_structured_shipping_table_is_not_refused() -> None:
    """`ToolHooks.get_owner_commitments` copies an envelope commitment's value verbatim into
    `offer.commitments`, and `contracts.Envelope` puts no shape on it. A 5,000-row table is a
    large but entirely ordinary standing commitment."""
    rows = [{"zone": f"US-{index:04d}", "days": 2, "fee": 4.99} for index in range(5000)]
    commitment = {"key": "shipping_table", "value": rows, "provenance": dict(HOOK_PROVENANCE)}

    result = _check(
        make_bid(
            claims=[make_claim("a", "b")],
            offer=make_offer(commitments=[commitment]),
        )
    )
    assert _unwalkable(result) == [], result.reasons
    assert result.ok is True, result.reasons


# =============================================================================================
# T-162's hole, and the shape in which it came back.
#
# The provenance walk and the authorisation walk were once two traversals over one value with
# one budget each, and they spent it at different rates — the provenance walk stops AT a
# recognised block, the authorisation walk descended into it. So there was a window, measured
# 255 payloads wide, in which the authorisation walk ran out first and an external submission
# carrying `authorized_discount_pct: 25.0` came back `ok=True, requires_verification=False,
# unverified_claim_indexes=[]`: verbatim the state T-162's own gate quotes as the defect.
# =============================================================================================


@pytest.mark.parametrize("padding", [0, 255, 256, 510, 511, 2000])
def test_padding_never_starves_the_authorisation_check_while_the_walk_still_finishes(
    padding: int,
) -> None:
    """Whatever else the filler does, the authorisation must not be silently admitted. Either the
    walk finished and flagged it, or the walk did not finish and the bid is refused — never
    `ok=True` with nothing said."""
    value: dict[str, Any] = {
        f"a{index:05d}": {"provenance": dict(HOOK_PROVENANCE)} for index in range(padding)
    }
    value["zzzz"] = {"authorized_discount_pct": 25.0}

    result = _with_value(value, path=EXTERNAL_PATH)
    assert result.ok is False or result.requires_verification is True, (
        f"padding={padding}: an external submission stating its own discount authorisation was "
        f"admitted with nothing said — ok={result.ok} reasons={result.reasons} "
        f"requires_verification={result.requires_verification}"
    )


@pytest.mark.parametrize(
    ("shape", "claim"),
    [
        ("as the claim key", {"key": "authorized_discount_pct", "value": 20.0}),
        ("in the value", {"key": "policy", "value": {"authorized_discount_pct": 25.0}}),
        ("the envelope's spelling", {"key": "policy", "value": {"max_discount_pct": 25.0}}),
        ("deep in the value", {"key": "policy", "value": _wrap(10, {"max_discount_pct": 1.0})}),
        ("inside a list", {"key": "policy", "value": {"g": [{"authorized_discount_pct": 1.0}]}}),
    ],
)
def test_an_external_claim_of_ones_own_discount_authority_is_never_taken_on_trust(
    shape: str, claim: dict[str, Any]
) -> None:
    payload = dict(claim, provenance=dict(HOOK_PROVENANCE))
    result = _check(make_bid(claims=[payload]), EXTERNAL_PATH)
    assert result.ok is False or result.requires_verification is True, (
        f"{shape}: admitted with nothing said — {result.reasons}"
    )


def test_the_hosted_path_is_not_second_guessed_about_authorisation() -> None:
    """The hosted grant already survived `store_agent/hooks/provenance.py`, which holds the hook
    ledger and the approved envelope. This door holds neither and does not pretend to."""
    result = _check(make_bid(claims=[make_claim("authorized_discount_pct", 20.0)]), HOSTED_PATH)
    assert result.ok is True, result.reasons
    assert result.requires_verification is False


# =============================================================================================
# It never raises, and it always terminates.
# =============================================================================================


class _RaisingBool:
    def __bool__(self) -> bool:
        raise RuntimeError("__bool__ exploded")


class _RaisingStr:
    def __str__(self) -> str:
        raise RuntimeError("__str__ exploded")

    def __format__(self, spec: str) -> str:
        raise RuntimeError("__format__ exploded")

    def __hash__(self) -> int:
        return 1

    def __eq__(self, other: object) -> bool:
        return False


class _RaisingGetattr:
    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"__getattr__({name}) exploded")


class _RaisingItems(dict):  # type: ignore[type-arg]
    def items(self) -> Any:
        raise RuntimeError("items() exploded")


class _RaisingSequence(Sequence):  # type: ignore[type-arg]
    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: Any) -> Any:
        raise RuntimeError("__getitem__ exploded")


class _LyingLength(dict):  # type: ignore[type-arg]
    def __len__(self) -> int:
        return 0


def _self_referential() -> dict[str, Any]:
    node: dict[str, Any] = {}
    node["provenance"] = node
    return node


@pytest.mark.parametrize("path", [HOSTED_PATH, EXTERNAL_PATH])
@pytest.mark.parametrize(
    ("shape", "value"),
    [
        ("a source whose __bool__ raises", {"provenance": {"source": _RaisingBool()}}),
        ("a source whose __str__ raises", {"provenance": {"source": _RaisingStr()}}),
        ("a source with a __getattr__ trap", {"provenance": {"source": _RaisingGetattr()}}),
        ("a key whose __format__ raises", {_RaisingStr(): {"provenance": "x"}}),
        ("a mapping whose items() raises", _RaisingItems({"a": 1})),
        ("a sequence whose __getitem__ raises", _RaisingSequence()),
        ("a self-referential mapping", _self_referential()),
        ("a value that is a generator", (index for index in range(3))),
        ("bytes", b"\x00\x01"),
    ],
)
def test_validate_bid_never_raises_on_a_hostile_claim_value(
    shape: str, value: Any, path: str
) -> None:
    """`validate_bid` never raises on bad input, and that is not a nicety: a boundary that threw
    would make "reject" and "crash" indistinguishable to the exchange, and every one of these
    reached the public door as a 500 while the walk was being built."""
    try:
        result = _with_value(value, path=path)
    except Exception as exc:  # noqa: BLE001 - the whole point of the assertion
        pytest.fail(f"{shape} on {path}: validate_bid raised {type(exc).__name__}: {exc}")
    assert isinstance(result.ok, bool)


@pytest.mark.parametrize(
    ("shape", "value"),
    [
        ("a mapping that will not be read", _RaisingItems({"a": 1})),
        ("a sequence that will not be read", _RaisingSequence()),
    ],
)
def test_a_container_that_cannot_be_read_fails_closed(shape: str, value: Any) -> None:
    """ "Not a container" and "a container I could not open" are opposite answers: the first hid
    nothing and the second might have hidden anything. Only the second refuses."""
    result = _with_value(value)
    assert _unwalkable(result), f"{shape}: failed OPEN — {result.reasons}"


def test_a_container_that_understates_its_own_length_is_still_walked() -> None:
    """`len()` is the caller's answer about an object the caller wrote, so it decides only what
    the walk can AFFORD to look at, never what it has finished looking at."""
    value = _LyingLength({"provenance": dict(ASSERTED_PROVENANCE)})
    result = _with_value(value)
    assert result.ok is False, f"a lying __len__ hid a seller_asserted block: {result.reasons}"


class _CountedKey(str):
    """A mapping key that counts every time the walk READS it.

    `_container_entries` is the only thing in the walk that turns a container's entries into
    `(key_text, child)` pairs, and it calls `_key_text` — that is, `str()` — on every key it
    produces. So "how many times were this payload's keys stringified" IS "how many entries did
    the walk read and sort", which is the quantity the test below is really about and the
    quantity it used to infer from a stopwatch. A node the walk declines to ENTER never reaches
    `_container_entries`, so its keys are read zero times; that difference is the whole subject.

    Subclassing `str` rather than wrapping one is deliberate, for the same reason the magic-link
    limiter's `_CountedInstant` subclasses `datetime`: the key stays a string to every part of
    the walk that handles it — hashing, the sort, `_trimmed`, the breadcrumb — and nothing in
    `boundary.py` is aware of it, so what is counted is the shipped code path rather than a
    test-only one. `self[:]` returns a plain `str`, so the text the walk carries onward cannot
    re-enter the counter and inflate the reading.
    """

    reads: ClassVar[int] = 0

    @classmethod
    def reset(cls) -> None:
        """Begin a fresh measurement."""
        cls.reads = 0

    def __str__(self) -> str:
        type(self).reads += 1
        return self[:]


def _entries_read(width: int, fanout: int) -> tuple[int, Any]:
    """One walk over a `width` x `fanout` value, and the entries it actually read getting there.

    Every child is its own mapping, as a parser handing this door a wire payload would produce.
    The key OBJECTS are shared between children because they are immutable strings and the count
    is kept on the class, which keeps a two-million-entry payload cheap to build.
    """
    inner = [_CountedKey(f"c{index:06d}") for index in range(fanout)]
    value = {_CountedKey(f"k{index:06d}"): dict.fromkeys(inner, 1) for index in range(width)}
    _CountedKey.reset()
    return_value = _with_value(value)
    return _CountedKey.reads, return_value


def test_enforcing_the_bound_costs_less_than_ignoring_it() -> None:
    """The payload here is REJECTED, and that is the point.

    An earlier version read and sorted the children of every node the budget had already decided
    to refuse — so the more obviously hostile the payload, the more work it bought, and a
    5,000-wide value took 5.6 seconds. A bound that costs more to enforce than the payload it is
    bounding is a denial of service of its own.

    COUNTED, not timed — a repair to this test, not a change of subject. It used to wrap one walk
    in `time.perf_counter()` and assert under half a second, with the margin held deliberately
    close to the measurement rather than at a decorative two seconds, because "a 100x margin
    notices nothing". That reasoning is kept and the margin is now TIGHTER: the ceiling below is
    the walk's own entry budget and the measurement sits 0.05% under it. What is dropped is the
    stopwatch, which was grading the machine rather than the walk — in BOTH directions:

    * RED on correct code. MEASURED on this box, 4 runs in 4 under contention: 1.75 s, 1.99 s,
      2.03 s and 2.11 s against the 0.5 s threshold, with the walk unchanged and right. Idle,
      the same call is 0.10 s. A gate in the always-run suite that goes red because something
      else wanted the CPU teaches people to ignore the suite.
    * GREEN on the defect it exists for. MEASURED: the reject-then-read walk restored and run on
      this test's own former payload took 0.148 s — it PASSED, on an idle machine, with the
      denial of service present. The wall clock only ever caught that walk by being SLOW at it,
      so the machine decided both verdicts and neither of them was about the walk.

    The property was never about seconds. The bound is an ENTRY BUDGET and the walk spends it,
    so the work is countable: enforcing the bound costs at most the budget, however much payload
    is dangled behind the refusal, whereas ignoring it costs the whole payload. On the two
    payloads below — 501,000 entries and 2,004,000 — this walk reads 65,500 either way, and the
    reject-then-read walk reads 501,000 and 2,004,000, which is 7.6x and 30.6x over the ceiling.
    Those are integers: they read the same on any machine at any load.
    """
    from contracts.boundary import CLAIM_VALUE_MAX_ENTRIES  # noqa: PLC0415

    # ARMED: the instrument is not blind. A value small enough to be walked in FULL is read
    # exactly once per entry — without this, a run whose keys went uncounted would report every
    # implementation, bounded or not, as having done no work at all, which is the failure mode a
    # counter has and a stopwatch does not.
    walked_in_full, admitted = _entries_read(width=20, fanout=10)
    assert admitted.ok is True, admitted.reasons
    assert walked_in_full == 20 + 20 * 10, (
        f"a 220-entry value the walk read in full registered {walked_in_full} key reads; the "
        "counter is not seeing what `_container_entries` produces"
    )

    small_reads, refused = _entries_read(width=1_000, fanout=500)
    large_reads, refused_larger = _entries_read(width=4_000, fanout=500)

    # ARMED: both payloads are REJECTED. If the budget ever stops refusing them this test has
    # stopped exercising the reject-then-read path it exists for, and would be measuring a walk
    # that finished rather than one that refused.
    for label, result in (("500,000", refused), ("2,000,000", refused_larger)):
        assert _unwalkable(result), (
            f"the {label}-entry payload no longer exceeds the budget, so this test has stopped "
            f"exercising the reject-then-read path it exists for: {result.reasons}"
        )

    assert large_reads == small_reads, (
        f"refusing a 2,004,000-entry value read {large_reads:,} entries against {small_reads:,} "
        f"for the 501,000-entry one — a factor of {large_reads / small_reads:.1f}. The cost of "
        "enforcing the bound is scaling with the payload the bound already refused, so the more "
        "hostile the value the more work it buys"
    )
    assert large_reads <= CLAIM_VALUE_MAX_ENTRIES, (
        f"refusing an over-budget claim value read {large_reads:,} entries against a budget of "
        f"{CLAIM_VALUE_MAX_ENTRIES:,}; the walk is reading and sorting entries under nodes it "
        "had already decided it could not afford to enter"
    )


def test_a_container_that_will_not_stop_producing_entries_does_not_hang_the_door() -> None:
    """`len()` is the caller's answer about the caller's own object, so it may not decide how much
    the walk actually READS.

    A `Sequence` reporting length 0 whose `__getitem__` never raises `IndexError` made
    `validate_bid` run forever — measured, still going after 45 seconds, on the public door. The
    walk now produces at most one entry more than the budget can spend, so the read is bounded by
    the walk rather than by the payload's willingness to end.
    """

    class _NeverEnds(Sequence):  # type: ignore[type-arg]
        def __len__(self) -> int:
            return 0

        def __getitem__(self, index: Any) -> Any:
            return 1

    class _UnderstatesLength(dict):  # type: ignore[type-arg]
        def __len__(self) -> int:
            return 0

    for shape, value in (
        ("a sequence that never ends", _NeverEnds()),
        (
            "a mapping understating 200k entries",
            _UnderstatesLength({f"k{i}": i for i in range(200000)}),
        ),
    ):
        started = time.perf_counter()
        result = _with_value(value)
        elapsed = time.perf_counter() - started
        assert elapsed < 5.0, f"{shape}: took {elapsed:.2f}s"
        assert _unwalkable(result), f"{shape}: read past the budget and admitted the bid"


def test_a_very_deep_chain_is_judged_rather_than_crashing_the_door() -> None:
    """Ten thousand wrappers. The walk is iterative, so the depth bound is a policy rather than a
    stand-in for CPython's stack limit, and a payload deeper than the bound is a refusal rather
    than a `RecursionError` escaping the public door."""
    result = _with_value(_wrap(10000, {"provenance": dict(ASSERTED_PROVENANCE)}))
    assert result.ok is False
    assert _unwalkable(result), result.reasons


# =============================================================================================
# One payload, one verdict — whichever door reads it.
# =============================================================================================


@pytest.mark.parametrize(
    "written",
    # Both orders are non-canonical on purpose. A parameter already in sorted order passes with
    # the sort removed and would be measuring nothing.
    [("zzz", "aaa", "mmm"), ("mmm", "zzz", "aaa")],
)
def test_the_walk_reports_in_key_order_however_the_object_was_written(
    written: tuple[str, str, str],
) -> None:
    """Python walks `dict.items()` in insertion order; JavaScript's `Object.entries` hoists
    integer-like keys to the front. Two doors reading one object in two orders is how a budget
    that decides what gets seen turned identical bytes into different verdicts — measured, and in
    both directions.

    Fail-closed truncation is what actually removed that: a walk that does not finish now refuses,
    so no ORDER can decide what is admitted. The canonical order is what is left of the argument
    and it is still worth asserting, because the reason list is what the exchange logs and what a
    seller reads — and two doors that name the same three blocks in two different orders are two
    doors telling the story differently.
    """
    block = {"provenance": dict(ASSERTED_PROVENANCE)}
    value = {key: dict(block) for key in written}

    result = _with_value(value)
    assert result.ok is False, result.reasons

    reported = [r.split(":")[1] for r in result.reasons if r.startswith("hosted_non_hook_")]
    assert reported == [
        "0.value.aaa.provenance",
        "0.value.mmm.provenance",
        "0.value.zzz.provenance",
    ], (
        f"written {written}, reported {reported} — the traversal order followed the object's "
        "insertion order rather than a canonical one"
    )


def test_a_field_the_model_already_refused_is_not_reported_a_second_time() -> None:
    """An empty `observed_at` violates the published bundle's `minLength` AND its `format`, and
    both steps of this door see it. One field reported twice under a byte-identical reason is a
    mislabelling, not a second finding — the same rule the price walk states for `not_positive`
    and `below_price_floor`."""
    provenance = dict(HOOK_PROVENANCE, observed_at="")
    result = _check(make_bid(claims=[make_claim("f", "x", provenance)]))

    assert result.ok is False
    assert len(result.reasons) == len(set(result.reasons)), (
        f"the same reason was reported more than once: {result.reasons}"
    )


#: Measured against the validator the TypeScript door actually runs — `ajv-formats` 3.0.1 through
#: `addFormats(new Ajv2020(...))`, which is what `packages/contracts/src/ts/schemas.ts` builds.
#: These are not a reading of RFC 3339; several of them contradict a strict reading on purpose,
#: because the property that matters is that the two doors admit the SAME set. Each was checked
#: in both languages.
_AJV_DATE_TIME_VERDICTS: tuple[tuple[str, bool], ...] = (
    ("2026-01-01T00:00:00Z", True),
    ("2026-01-01t00:00:00z", True),
    ("2026-01-01 00:00:00Z", True),  # a space is a separator to ajv
    ("2026-01-01\t00:00:00Z", True),  # so is a tab
    ("2026-01-01\xa000:00:00Z", True),  # ...and every other code point in ECMAScript's \\s
    ("2026-01-01T00:00:00+01", True),
    ("2026-01-01T00:00:00+0100", True),
    ("2026-01-01T00:00:00-08:00", True),
    ("2026-01-01T18:59:60-05:00", True),  # a real leap second, stated from New York
    ("2026-01-01T23:59:60Z", True),
    ("2024-02-29T00:00:00Z", True),
    ("2026-01-01T00:00:00.123456789Z", True),
    ("not-a-date", False),
    ("", False),
    ("2026-01-01T00:00:00", False),  # no offset
    ("2026-01-01T00:00:00Z\n", False),  # a second separator
    ("2026-01-01T00:00:00+24:00", False),  # the offset is range-checked
    ("2026-01-01T00:00:00+00:60", False),
    ("2026-01-01T00:00:00+99:99", False),
    ("2026-01-01T23:59:60+01:00", False),  # not a leap second anywhere
    ("2026-02-30T00:00:00Z", False),
    ("2026-13-01T00:00:00Z", False),
    ("2026-02-29T00:00:00Z", False),
    ("2026-01-01T24:00:00Z", False),
    ("٢٠٢٦-٠١-٠١T00:00:00Z", False),  # Arabic-Indic digits
)


@pytest.mark.parametrize(("stamp", "admitted"), _AJV_DATE_TIME_VERDICTS)
def test_the_python_door_admits_exactly_the_timestamps_ajv_formats_admits(
    stamp: str, admitted: bool
) -> None:
    """T-194 is not "the Python door is strict about timestamps"; it is "the two doors admit the
    same documents". A Python door that refuses what the TypeScript door accepts is the same
    defect with the doors swapped, and the first repair for T-194 did exactly that on 23 of 68
    corpus strings."""
    from contracts.boundary import _is_rfc3339_date_time  # noqa: PLC0415

    assert _is_rfc3339_date_time(stamp) is admitted, (
        f"{stamp!r}: this door says {not admitted}, ajv-formats says {admitted}"
    )


@pytest.mark.parametrize("stamp", [s for s, ok in _AJV_DATE_TIME_VERDICTS if not ok])
def test_a_timestamp_the_published_format_refuses_does_not_reach_the_auction(stamp: str) -> None:
    """The predicate above, through the real door, at the position the T-194 gate names."""
    provenance = dict(HOOK_PROVENANCE, observed_at=stamp)
    result = _check(make_bid(claims=[make_claim("f", "x", provenance)]))
    assert result.ok is False, f"{stamp!r} was admitted: {result.reasons}"


def test_the_root_level_timestamp_is_spelled_the_way_the_model_spells_it() -> None:
    """One location, one spelling. The format walk once reported `<root>.issued_at` while the
    model reported `issued_at` for the same field — `<root>` is the model's placeholder for an
    EMPTY location and ajv never produces it, so that was a third vocabulary for one field."""
    from packages.contracts import validate_external_submission  # noqa: PLC0415
    from packages.contracts.tests._fixtures_protocol import make_submission  # noqa: PLC0415

    result = validate_external_submission(
        make_submission(issued_at="not-a-date"),
        trust_snapshot=make_snapshot_table(),
        now=NOW,
        list_prices=_HONEST_ROSTER,
    )
    assert "schema_invalid:issued_at" in result.reasons, result.reasons
    assert not any("<root>" in reason for reason in result.reasons), result.reasons


def test_the_two_doors_normalise_whitespace_the_same_way() -> None:
    """`str.strip()` and `String.prototype.trim()` are different functions — Python strips
    U+001C-U+001F and U+0085, `trim` strips U+FEFF — so a claim key padded with one of the six
    was read by one door and not the other. Both now use the union, spelled by code point."""
    from contracts.boundary import DISCOUNT_AUTHORISATION_CLAIM_KEYS, _trimmed  # noqa: PLC0415

    for pad in ("﻿", "\x1c", "\x1d", "\x1e", "\x1f", "\x85", " ", "　"):
        assert _trimmed(f"{pad}authorized_discount_pct{pad}") in DISCOUNT_AUTHORISATION_CLAIM_KEYS

        result = _check(
            make_bid(claims=[make_claim(f"{pad}authorized_discount_pct", 20.0)]), EXTERNAL_PATH
        )
        assert result.ok is False or result.requires_verification is True, (
            f"a claim key padded with U+{ord(pad):04X} slipped past the authorisation check: "
            f"{result.reasons}"
        )


def test_the_walk_reads_mappings_and_sequences_and_nothing_else() -> None:
    """A guard on the walk's own idea of a container, so that widening it later is a deliberate
    act. A `str` is a `Sequence` and walking one character by character would be both useless and
    quadratic; a `set` has no stable order and cannot be addressed by a breadcrumb."""
    from contracts.boundary import _container_length  # noqa: PLC0415

    assert _container_length({"a": 1}) == 1
    assert _container_length([1, 2, 3]) == 3
    assert _container_length(()) == 0
    for scalar in ("text", b"bytes", bytearray(b"x"), 7, 1.5, True, None, {1, 2}):
        assert _container_length(scalar) is None, scalar
    assert isinstance({"a": 1}, Mapping)
