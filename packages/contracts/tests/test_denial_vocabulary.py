"""T-204: the 409 `denial_reason` vocabulary, published where a consumer can reach it.

**What was actually measured, at 2998de1, before any of this was written.** The ticket says the
field is "exception class names and no enum is published". Half of that is stale. Booted with
`uvicorn exchange.main:app` and driven over real HTTP, four refusals came back:

    POST /auctions/{id}/accept  ->  409
      {"accepted":false,"denial_reason":"unknown_bid: auction 'auction-34f7…' carries no bid
        'bid-does-not-exist'; there is nothing to accept"}
      {"accepted":false,"denial_reason":"unavailable: the eligibility source speaks interface
        version '1', which this exchange does not support; …"}
      {"accepted":false,"denial_reason":"blacklisted: chargeback fraud"}
      {"accepted":false,"denial_reason":"checkout_refused: RuntimeError: merchant declined to mint"}

So the leading token is a DECLARED code (T-264 closed that half), the exception class name
survives only in the prose behind the colon, and `openapi/exchange.openapi.json` already
publishes a `pattern` and an `x-vocabulary` (T-267's lane). What was NOT published anywhere a
client can reach is the vocabulary itself as a machine-readable, cross-language artifact: it
lived as an `x-` extension inside one response of one OpenAPI document, and the document's own
description pointed the reader at `exchange.accept.DENIAL_REASONS` — a Python module inside a
service a client does not have.

This module pins the repair. `$defs/DenialCode` in the protocol bundle is the published
enumeration, generated into both languages like every other closed vocabulary, and the four
places the nine codes are now written must agree with each other and with the running exchange.

**`denial_reason` itself still cannot be an `enum`**, and
`test_repro_open_tickets.py::test_the_published_denial_reason_has_an_enumerated_vocabulary`
stays red for that reason — asserted here as an executable fact rather than a claim, so that
nobody closes it by publishing a constraint every real 409 violates.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest

from contracts.codegen import PYTHON_OUT, TS_OUT
from contracts.registry import protocol_schema

_HERE = pathlib.Path(__file__).resolve().parent
_CONTRACTS = _HERE.parent

BUNDLE = protocol_schema()

#: The committed cross-language table: one refusal string, and the code both parsers must read
#: out of it. `denial_vocabulary.test.ts` drives the TypeScript parser with the same file.
CORPUS_PATH = _HERE / "denial_reason_corpus.json"

#: U+001C. Python's `str.strip()` removes it; JavaScript's `trim()` does not. Spelled by code
#: point so this file carries no raw control character (T-108's habit).
INFORMATION_SEPARATOR_FOUR = chr(0x1C)


def _published_codes() -> list[str]:
    return list(BUNDLE["$defs"]["DenialCode"]["enum"])


def _409_schema() -> dict[str, Any]:
    document = json.loads((_CONTRACTS / "openapi/exchange.openapi.json").read_text("utf-8"))
    body = document["paths"]["/auctions/{auction_id}/accept"]["post"]["responses"]["409"]
    return dict(body["content"]["application/json"]["schema"]["properties"]["denial_reason"])


def test_the_bundle_publishes_the_denial_code_vocabulary() -> None:
    """The enumeration lives in the one file both languages are generated from."""
    definition = BUNDLE["$defs"].get("DenialCode")
    assert definition is not None, (
        "protocol.schema.json defines no DenialCode. The nine refusal codes were published only "
        "as an `x-vocabulary` extension inside one OpenAPI response, which no generator reads "
        "and no client gets a type for"
    )
    assert definition["type"] == "string"
    assert definition["enum"], "DenialCode is an empty enum"
    assert definition.get("title") == "DenialCode"
    assert definition.get("description", "").strip(), "DenialCode has no description"


def test_the_published_codes_are_exactly_the_ones_the_exchange_emits() -> None:
    """Against the RUNNING producer, not against a list someone typed twice.

    A tenth code added to `exchange.accept` without being published turns this red, which is the
    whole reason to publish a vocabulary rather than a sentence about one.
    """
    from exchange.accept import DENIAL_REASONS  # noqa: PLC0415

    assert sorted(_published_codes()) == sorted(DENIAL_REASONS), (
        f"the published DenialCode enum has drifted from the exchange's own vocabulary: "
        f"published {sorted(_published_codes())}, emitted {sorted(DENIAL_REASONS)}"
    )


def test_the_openapi_409_agrees_with_the_bundle_in_both_of_its_spellings() -> None:
    """`x-vocabulary`, the `pattern`'s alternation, and `$defs/DenialCode` are one list."""
    schema = _409_schema()
    codes = _published_codes()

    assert sorted(schema["x-vocabulary"]) == sorted(codes), (
        "the 409's x-vocabulary and the bundle's DenialCode enum disagree: "
        f"{sorted(schema['x-vocabulary'])} vs {sorted(codes)}"
    )

    alternation = re.match(r"\^\(([^)]+)\)", schema["pattern"])
    assert alternation is not None, (
        f"the published pattern no longer opens with an alternation of codes: {schema['pattern']}"
    )
    assert sorted(alternation.group(1).split("|")) == sorted(codes), (
        "the pattern's alternation and the DenialCode enum disagree, so the document constrains "
        f"a different set than it enumerates: {sorted(alternation.group(1).split('|'))}"
    )

    assert schema.get("x-denial-code-schema") == "protocol.schema.json#/$defs/DenialCode", (
        "the 409 does not point a client at the published enumeration; without it the only "
        "machine-readable vocabulary is an x- extension a generator will not follow"
    )


def test_the_denial_code_enum_reached_both_generated_languages() -> None:
    """The bundle is only a source of truth if the generated views carry it."""
    python_view = PYTHON_OUT.read_text("utf-8")
    assert "class DenialCode(" in python_view, (
        "the generated Python view carries no DenialCode; run `python -m contracts.codegen`"
    )
    ts_view = TS_OUT.read_text("utf-8")
    assert "DenialCode" in ts_view, (
        "the generated TypeScript view carries no DenialCode; run `python -m contracts.codegen`"
    )
    for code in _published_codes():
        assert f'"{code}"' in ts_view, f"{code!r} is missing from the generated TypeScript union"


# --- the cross-language parser table --------------------------------------------------------
#
# The document tells a client "parse only the token before the first colon". A client that
# implements that with `split(':')[0].trim()` gets it wrong in JavaScript, because the exchange
# uses Python's `str.strip()`, which removes U+001C-U+001F and JavaScript's `trim()` does not.
# That is the T-115 class of defect one layer out, so the parser is published rather than
# described, and this table is what holds the two implementations together.


def _corpus() -> list[dict[str, Any]]:
    assert CORPUS_PATH.exists(), (
        f"{CORPUS_PATH.name} is missing; the TypeScript parser has nothing to be graded against"
    )
    rows = json.loads(CORPUS_PATH.read_text("utf-8"))["rows"]
    return [dict(row) for row in rows]


def test_every_corpus_row_is_what_the_exchanges_own_parser_answers() -> None:
    """The committed table is re-derived from `exchange.accept.reasons.denial_code` here, so a
    row cannot be edited to match a broken TypeScript parser without failing on this side."""
    from exchange.accept.reasons import denial_code  # noqa: PLC0415

    wrong = [
        (row["reason"], row["code"], denial_code(row["reason"]))
        for row in _corpus()
        if denial_code(row["reason"]) != row["code"]
    ]
    assert wrong == [], (
        f"denial_reason_corpus.json disagrees with the exchange's own parser: {wrong[:8]}"
    )


def test_the_corpus_covers_the_shapes_that_actually_diverge() -> None:
    """A table of easy rows proves nothing. These are the ones that separate the two languages."""
    rows = _corpus()
    reasons = [str(row["reason"]) for row in rows]
    codes = {row["code"] for row in rows}

    assert len(rows) >= 24, f"the corpus collapsed to {len(rows)} rows"
    assert None in codes, "no row exercises an UNDECLARED token, which must parse to null"
    assert len(codes - {None}) >= 5, "the corpus exercises too few of the declared codes"

    def _present(predicate: Any, description: str) -> None:
        assert any(predicate(reason) for reason in reasons), f"no row {description}"

    _present(lambda r: ":" in r and ": " not in r, "uses a BARE colon with no space behind it")
    _present(lambda r: r.endswith(":"), "ends at the colon with no prose at all")
    _present(lambda r: r[:1].isspace(), "carries leading whitespace before the token")
    _present(
        lambda r: INFORMATION_SEPARATOR_FOUR in r,
        "carries U+001C, which Python strips and JavaScript's trim() does not",
    )
    _present(lambda r: r.count(":") >= 2, "carries a second colon inside the prose")
    _present(lambda r: r == "", "is the empty string")


# --- why the field itself is still not an enum ----------------------------------------------


def test_a_real_served_denial_reason_is_not_a_member_of_the_published_enum() -> None:
    """The executable form of T-204's remaining blocker.

    Driven through the exchange's real producer and its real 409 boundary. Every value a client
    receives carries prose behind the code, so an `enum` of the bare codes on `denial_reason`
    would be a published constraint that essentially every real 409 violates. Publishing it
    would turn a documentation gap into a false contract.
    """
    from exchange.accept.gate import _denial_reason  # noqa: PLC0415
    from exchange.accept.routes import _denied  # noqa: PLC0415
    from exchange.eligibility import EligibilityDecision  # noqa: PLC0415

    decision = EligibilityDecision(
        store_id="store-1", status="blacklisted", reason="chargeback fraud"
    )
    served = json.loads(bytes(_denied(_denial_reason(decision)).body))["denial_reason"]

    assert ":" in served, f"the served shape stopped carrying prose: {served!r}"
    assert served not in _published_codes(), (
        f"the exchange now serves a BARE declared code ({served!r}). If that is true of every "
        "refusal, `denial_reason` can finally be published as an enum and T-204's gate in "
        "test_repro_open_tickets.py can be closed — remove this test in the same change"
    )
    assert re.compile(_409_schema()["pattern"]).match(served), (
        f"the published pattern refuses a value the exchange really serves: {served!r}"
    )


@pytest.mark.parametrize("name", ["DenialCode"])
def test_the_new_definition_did_not_break_the_bundles_own_invariants(name: str) -> None:
    """Cheap belt-and-braces: the bundle's generic guards run over `$defs` by name, so a new
    entry has to satisfy them the same way every other one does."""
    definition = BUNDLE["$defs"][name]
    assert "properties" not in definition, f"{name} is an enum, not an object"
    assert definition.get("enum")
