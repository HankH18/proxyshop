"""`ShortlistSlot.store_domain`: published, nullable, and unable to be the empty string.

The field exists to carry ONE value — the platform's registered domain for the store — to the
one place that can act on it: the buyer, which performs the redirect and is therefore the last
code between the exchange's checkout permalink and a browser (R3/D22/C10). Until it was
published, the frozen contract carried no store domain at all, so the buyer's guard compared
the permalink's host against nothing on every slot of every deployment.

Three properties, and the third is the one that keeps this from happening again:

* it is OPTIONAL and admits ``null`` — for the reason ``product``, ``price`` and
  ``commitments`` do: ``additionalProperties: false`` means a required field would break every
  existing producer on the same commit, and a deployment whose exchange has no
  ``store_id -> domain`` registry genuinely has no domain to publish;
* ``null`` and ABSENT mean the same thing, *the platform holds no registered domain here*;
* ``""`` means NOTHING and is INVALID. That is deliberate. The empty string was the silent
  default the buyer used, it reads to a client as a present-and-blank domain, and every caller
  that forgot to check its truthiness silently got "no host constraint" instead of a refusal.
  ``minLength: 1`` makes the value that hid the defect unspellable by any producer.

Both generated languages are checked because a schema constraint that reached neither is a
constraint no consumer enforces.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from jsonschema import Draft202012Validator
from packages.contracts.registry import protocol_schema

from packages.contracts import ShortlistSlot

_CONTRACTS = pathlib.Path(__file__).resolve().parents[1]

_SLOT = {
    "slot": "fit",
    "bid_ref": "bid-0001",
    "fit_score": 0.91,
    "trust_summary": {"score": 0.72},
    "provenance_labels": ["store_confirmed"],
}


def _slot_schema() -> dict:
    return dict(protocol_schema()["$defs"]["ShortlistSlot"])


def _errors(payload: dict) -> list[str]:
    """Validated against the WHOLE bundle with a root ``$ref``, not the fragment alone.

    ``ShortlistSlot.slot`` is ``{"$ref": "#/$defs/ShortlistSlotName"}``, so handing the
    fragment to the validator on its own makes every pointer into ``$defs`` unresolvable and
    turns a legal slot into a `PointerToNowhere` — an error that looks like a schema defect
    and is really a test that pointed at half a schema.
    """
    bundle = dict(protocol_schema())
    validator = Draft202012Validator({**bundle, "$ref": "#/$defs/ShortlistSlot"})
    return [error.message for error in validator.iter_errors(payload)]


def test_the_contract_declares_the_field_at_all() -> None:
    """The whole of Gap B in one assertion: the value had nowhere to be published."""
    properties = _slot_schema()["properties"]
    assert "store_domain" in properties, (
        "ShortlistSlot publishes no store_domain, so the exchange has nowhere to put the "
        f"registered domain and the buyer has nothing to pin against: {sorted(properties)}"
    )


@pytest.mark.parametrize(
    "payload, why",
    [
        ({**_SLOT, "store_domain": "store-one.example.com"}, "a real registered domain"),
        ({**_SLOT, "store_domain": None}, "the platform holds no domain for this store"),
        (dict(_SLOT), "absent says the same thing as null"),
    ],
)
def test_the_three_legal_shapes_validate(payload: dict, why: str) -> None:
    assert _errors(payload) == [], f"{why} was refused: {_errors(payload)}"


def test_the_empty_string_is_refused() -> None:
    """The value that made the guarantee inert cannot be published by any producer.

    A buyer that received ``""`` could not tell it from a domain without a truthiness check,
    and the missing check is exactly what made the host cross-check skip silently. Refusing it
    at the schema means the ambiguity has one spelling — ``null`` — rather than two.
    """
    problems = _errors({**_SLOT, "store_domain": ""})
    assert problems, (
        'store_domain: "" validates against the published contract, so the silent default '
        "that made the anti-spoofing check inert is still a legal thing to publish"
    )


def test_the_pinned_model_agrees_with_the_schema_about_the_empty_string() -> None:
    """Pydantic and ajv must refuse the same payload; a producer uses one and a consumer the
    other, and a disagreement is a payload one door admits and the other rejects."""
    assert ShortlistSlot.model_validate({**_SLOT, "store_domain": None}).store_domain is None
    assert (
        ShortlistSlot.model_validate({**_SLOT, "store_domain": "s.example"}).store_domain
        == "s.example"
    )
    with pytest.raises(Exception):
        ShortlistSlot.model_validate({**_SLOT, "store_domain": ""})


def test_the_field_reached_both_generated_languages() -> None:
    """A constraint in the bundle that reached neither generated view enforces nothing."""
    python_view = (_CONTRACTS / "generated/python/protocol.py").read_text(encoding="utf-8")
    ts_view = (_CONTRACTS / "generated/ts/protocol.schema.d.ts").read_text(encoding="utf-8")

    assert "store_domain: str | None" in python_view, (
        "the Python model does not carry store_domain; regenerate with "
        "`python -m contracts.codegen`"
    )
    assert "min_length=1" in python_view.split("store_domain: str | None")[1][:120], (
        "the Python model dropped the minLength constraint, so pydantic would accept the "
        "empty string that ajv refuses"
    )
    assert "store_domain?: string | null" in ts_view, (
        "the TypeScript view does not carry store_domain; regenerate with "
        "`python -m contracts.codegen`"
    )


def test_a_shortlist_of_slots_still_round_trips_through_json() -> None:
    """The field is on the wire, not only in the model."""
    slot = ShortlistSlot.model_validate({**_SLOT, "store_domain": "store-one.example.com"})
    wire = json.loads(slot.model_dump_json())
    assert wire["store_domain"] == "store-one.example.com"
    assert _errors(wire) == []
