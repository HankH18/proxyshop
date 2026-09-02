"""`claim_id` — the content hash that makes re-extraction idempotent (D25).

The property being bought is narrow and specific: extract the same pitch twice and the same claim
must get the same id, so a verification result written against it still points at something. Every
test below is about that one property, from both sides — what must NOT change the id, and what
must.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from contracts.claims import canonicalize_value
from packages.contracts import canonical_json, claim_id, claim_id_for


def base(**overrides):
    material = {
        "pitch_ref": "pitch:p-1",
        "key": "free_returns",
        "value": "30 days",
        "claim_type": "return_policy",
    }
    material.update(overrides)
    return claim_id(**material)


def test_the_same_content_hashes_the_same_every_time() -> None:
    assert base() == base()


def test_the_id_is_self_describing() -> None:
    identifier = base()
    assert identifier.startswith("claim:sha256:")
    assert len(identifier) > 40


def test_re_extraction_that_produces_an_equal_value_produces_an_equal_id() -> None:
    """Whitespace and case are extraction noise, not different claims."""
    assert base(value="  30 Days ") == base(value="30 days")


def test_an_integral_float_and_an_int_are_the_same_value() -> None:
    assert base(value=30.0) == base(value=30)


def test_key_order_inside_a_structured_value_does_not_change_the_id() -> None:
    assert base(value={"days": 30, "restocking_fee": 0}) == base(
        value={"restocking_fee": 0, "days": 30}
    )


def test_list_order_inside_a_value_does_change_the_id() -> None:
    """A list of ingredients is ordered; treating it as a set would merge two different claims."""
    assert base(value=["water", "glycerin"]) != base(value=["glycerin", "water"])


def test_each_component_of_the_hash_actually_participates() -> None:
    reference = base()
    assert base(pitch_ref="pitch:p-2") != reference, "the pitch scopes the claim"
    assert base(key="free_shipping") != reference
    assert base(value="14 days") != reference
    assert base(claim_type="warranty") != reference, (
        "a 30-day return policy and a 30-day warranty are different claims"
    )


def test_provenance_is_not_part_of_the_hash() -> None:
    """The same claim re-observed from a fresher snapshot is the same claim; folding provenance in
    would mint a new id on every crawl and defeat the whole point."""
    fresh = {
        "key": "free_returns",
        "value": "30 days",
        "claim_type": "return_policy",
        "source_span": {"pitch_ref": "pitch:p-1", "start": 0, "end": 8},
        "provenance": {
            "source": "scraped",
            "ref": "snapshot://store/policies@sha256:aaaa",
            "observed_at": "2026-01-01T00:00:00Z",
            "authority_rank": 4,
        },
    }
    stale = {
        **fresh,
        "provenance": {
            "source": "owner_statement",
            "ref": "envelope:store-1:v3#c-2",
            "observed_at": "2020-01-01T00:00:00Z",
            "authority_rank": 1,
        },
    }
    assert claim_id_for(fresh) == claim_id_for(stale)


def test_claim_id_for_reads_the_pitch_off_the_source_span() -> None:
    claim = {
        "key": "free_returns",
        "value": "30 days",
        "claim_type": "return_policy",
        "source_span": {"pitch_ref": "pitch:p-1", "start": 0, "end": 8},
    }
    assert claim_id_for(claim) == base()
    assert claim_id_for(claim, pitch_ref="pitch:p-2") == base(pitch_ref="pitch:p-2")


def test_claim_id_for_reads_a_model_as_well_as_a_mapping() -> None:
    from packages.contracts import Claim
    from packages.contracts.tests._fixtures_protocol import make_claim

    payload = {**make_claim(), "claim_type": "return_policy"}
    assert claim_id_for(Claim.model_validate(payload)) == claim_id_for(payload)


def test_a_claim_with_no_pitch_still_gets_a_stable_id() -> None:
    """A hook-minted commitment has no pitch, and must still be addressable."""
    assert claim_id(pitch_ref=None, key="free_returns", value="30 days") == claim_id(
        pitch_ref="", key="free_returns", value="30 days"
    )


def test_canonicalization_leaves_booleans_alone() -> None:
    """`True` is an `int` subclass; collapsing it to 1 would merge "is vegan" with "count is 1"."""
    assert canonicalize_value(True) is True
    assert base(value=True) != base(value=1)


# --- the id is stable ACROSS PROCESSES, not merely within one --------------------------------


GOLDEN_CLAIM_ID = "claim:sha256:b672c908f89a24f5e5721f9e1b0ee63d92eceb17b39c37da320678683b24478d"


def test_the_id_for_the_reference_claim_is_a_pinned_literal() -> None:
    """Every other test here compares one `claim_id(...)` to another `claim_id(...)`, which stays
    true no matter what the function computes. This one pins the actual value, so a change to the
    hashing material or the canonicalization is a visible, reviewable diff rather than a silent
    re-issue of every claim id in the system."""
    assert base() == GOLDEN_CLAIM_ID


def test_the_id_survives_a_different_process_hash_seed() -> None:
    """`PYTHONHASHSEED` randomizes set and (historically) str iteration order. An id that moved
    with it would be idempotent only within a single run, which is not idempotent at all."""
    import subprocess
    import sys

    script = (
        "import sys; sys.path.insert(0, '.pkgroot');"
        "from contracts.claims import claim_id;"
        "print(claim_id(pitch_ref='pitch:p-1', key='free_returns', value={'a', 'b', 'c', 'd'},"
        " claim_type='return_policy'))"
    )
    ids = set()
    for seed in ("0", "1", "12345", "random"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            cwd=str(pathlib.Path(__file__).resolve().parents[3]),
        )
        assert result.returncode == 0, result.stderr
        ids.add(result.stdout.strip())
    assert len(ids) == 1, f"claim_id changed with PYTHONHASHSEED: {ids}"


def test_a_set_value_is_canonicalized_by_order_not_by_iteration() -> None:
    """An unordered collection has no wire order to preserve, so two spellings of the same set are
    the same claim."""
    assert claim_id(pitch_ref="p", key="k", value={"b", "a"}) == claim_id(
        pitch_ref="p", key="k", value={"a", "b"}
    )
    assert canonicalize_value({"b", "a"}) == ["a", "b"]


def test_a_value_that_cannot_be_canonicalized_is_refused_rather_than_stringified() -> None:
    """`repr` of a memory-addressed object is not stable across processes, so an id built from one
    would silently differ on the next run — the exact failure this module exists to prevent."""
    with pytest.raises(TypeError):
        claim_id(pitch_ref="p", key="k", value=object())


# --- T-107: the bytes are RFC 8785, not `json.dumps(sort_keys=True)` -------------------------


def hashed_material(value: object) -> dict[str, object]:
    """The exact mapping `claim_id` hashes for `base(value=...)`.

    Reconstructed rather than inspected, so the assertions below are about the SERIALIZATION and
    would not quietly pass if `claim_id` stopped hashing this material at all.
    """
    return {
        "pitch_ref": "pitch:p-1",
        "key": "free_returns",
        "value": canonicalize_value(value),
        "claim_type": "return_policy",
    }


def id_over(text: str) -> str:
    return f"claim:sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def dumps_rendering(material: dict[str, object]) -> str:
    """What this module used to hash: `json.dumps(sort_keys=True, separators=(",", ":"))`."""
    return json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


#: `(value, JCS rendering, json.dumps rendering)` for the two ways the two serializations
#: disagree. Both are reachable from a real claim value — `Claim.value` is unconstrained — and a
#: JS peer computing a claim id produces the JCS column in both rows.
JCS_DIVERGENCES: list[tuple[object, str, str]] = [
    (
        # `json.dumps` writes numbers with `repr`; ECMAScript — and so RFC 8785 §3.2.2.3, and so
        # every JS peer — writes `0.00001`.
        {"rate": 1e-5},
        '{"claim_type":"return_policy","key":"free_returns","pitch_ref":"pitch:p-1",'
        '"value":{"rate":0.00001}}',
        '{"claim_type":"return_policy","key":"free_returns","pitch_ref":"pitch:p-1",'
        '"value":{"rate":1e-05}}',
    ),
    (
        # `sort_keys` orders by CODE POINT (U+1F600 > U+FFFF); RFC 8785 §3.2.3 orders by UTF-16
        # CODE UNIT, where the emoji's leading surrogate D83D sorts BELOW FFFF. The two put the
        # same two keys in opposite orders.
        {"\U0001f600": 1, "￿": 2},
        '{"claim_type":"return_policy","key":"free_returns","pitch_ref":"pitch:p-1",'
        '"value":{"\U0001f600":1,"￿":2}}',
        '{"claim_type":"return_policy","key":"free_returns","pitch_ref":"pitch:p-1",'
        '"value":{"￿":2,"\U0001f600":1}}',
    ),
]


@pytest.mark.parametrize(("value", "jcs", "dumps"), JCS_DIVERGENCES)
def test_the_id_is_the_jcs_rendering_and_not_the_json_dumps_one(
    value: object, jcs: str, dumps: str
) -> None:
    """The two serializations really do differ here, and `claim_id` follows the JCS one.

    Pinning both sides matters: an assertion that only said "the id equals the JCS hash" would
    also pass if the two renderings happened to agree, which is exactly the case that proves
    nothing."""
    material = hashed_material(value)
    assert canonical_json(material) == jcs
    assert dumps_rendering(material) == dumps
    assert jcs != dumps, "this case no longer distinguishes the two serializations"

    assert base(value=value) == id_over(jcs)
    assert base(value=value) != id_over(dumps)


def test_a_claim_id_is_reproducible_from_the_canonical_bytes_alone() -> None:
    """The whole point of moving to JCS: a peer that can produce RFC 8785 bytes can produce the
    id, without reimplementing anything of this module but the material's four keys."""
    for value, jcs, _ in JCS_DIVERGENCES:
        assert base(value=value) == id_over(jcs)


def test_an_integer_no_double_can_state_has_no_claim_id() -> None:
    """RFC 8785 §3.1 arrives with the canonicalizer, and that is the right answer: a JS peer
    reading `9007199254740993` off the wire holds `9007199254740992`, so the two could not agree
    on an id for it. Refusing is the only honest option."""
    with pytest.raises(TypeError):
        base(value=2**53 + 1)
    with pytest.raises(ValueError):
        base(value={"quantity": 10**23})
    # The control: an integer that IS an exact double still hashes.
    assert base(value=2**53).startswith("claim:sha256:")
    assert base(value=10**16).startswith("claim:sha256:")


def test_a_lone_surrogate_in_a_claim_value_is_refused_rather_than_hashed() -> None:
    """`Claim.value` is unconstrained, so a lone surrogate is trivially reachable. Hashing one
    meant `.encode("utf-8")` raising `UnicodeEncodeError` — a 500 where a refusal belongs."""
    with pytest.raises(TypeError):
        base(value="\ud800")
