"""The JSON Schema bundle itself, and the codegen that keeps the typed views honest.

Two failure modes are guarded here and they are the ones that make a contracts package quietly
useless:

* **the schema and the generated types drift.** Someone edits the schema, does not regenerate,
  and the models keep describing a protocol that no longer exists. `--check` regenerates into a
  temp dir and diffs.
* **the schema stops actually constraining anything.** An object that forgets
  `additionalProperties: false`, or that requires nothing, validates every payload and refuses
  none — which looks identical to a working contract until something malformed reaches a service.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from contracts.codegen import PROTOCOL_SCHEMA, PYTHON_OUT, TS_OUT, check
from contracts.registry import PROTOCOL_SCHEMA_PATH, protocol_schema
from packages.contracts import PINNED_PROTOCOL_OBJECTS, is_valid, schema_for, schema_names

BUNDLE = protocol_schema()
DEFS = BUNDLE["$defs"]

# Enums are `type: string` + `enum`, so they have no properties to close and nothing to require.
_ENUMS = {name for name, schema in DEFS.items() if "enum" in schema}
_OBJECTS = sorted(set(DEFS) - _ENUMS)


def test_the_bundle_is_draft_2020_12_and_self_identifying() -> None:
    assert BUNDLE["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert BUNDLE["$id"] == "https://proxyshop.dev/schemas/protocol.schema.json"


def test_every_pinned_protocol_object_is_in_the_bundle() -> None:
    assert set(PINNED_PROTOCOL_OBJECTS) <= set(schema_names())


@pytest.mark.parametrize("name", _OBJECTS)
def test_every_object_closes_its_property_set(name: str) -> None:
    """An open object is not a contract: it accepts a `weight` on a hard constraint, a seventh
    trust dimension, and a typo'd field that then silently disappears downstream."""
    schema = DEFS[name]
    if "properties" not in schema:
        pytest.skip(f"{name} is not a property-bearing object")
    assert schema.get("additionalProperties") is False, (
        f"{name} does not set additionalProperties: false"
    )


@pytest.mark.parametrize("name", _OBJECTS)
def test_every_object_documents_itself(name: str) -> None:
    """The description is the generated docstring in both languages; an undocumented protocol
    object is one a consumer has to guess the meaning of."""
    assert DEFS[name].get("title") == name
    assert DEFS[name].get("description", "").strip(), f"{name} has no description"


@pytest.mark.parametrize("name", sorted(_ENUMS))
def test_every_enum_is_non_empty_and_documented(name: str) -> None:
    assert DEFS[name]["enum"], f"{name} is an empty enum"
    assert DEFS[name].get("description", "").strip(), f"{name} has no description"


@pytest.mark.parametrize("name", PINNED_PROTOCOL_OBJECTS)
def test_every_pinned_object_requires_something(name: str) -> None:
    assert DEFS[name].get("required"), f"{name} requires no field, so it validates {{}}"


def test_schema_for_returns_a_self_contained_subschema() -> None:
    """Self-contained so `jsonschema`, Ajv and a pasted-in tool all resolve it without a registry."""
    schema = schema_for("Bid")
    assert schema["$ref"] == "#/$defs/Bid"
    assert set(schema["$defs"]) == set(DEFS)


def test_schema_for_refuses_a_name_the_bundle_does_not_define() -> None:
    with pytest.raises(KeyError):
        schema_for("NotAProtocolObject")


def test_the_signed_submission_is_exactly_bid_union_signing_envelope() -> None:
    """The composed definition duplicates the two it composes, so it needs a guard against drift."""
    composed = set(DEFS["Bid"]["properties"]) | set(DEFS["SigningEnvelope"]["properties"])
    assert set(DEFS["SignedBidSubmission"]["properties"]) == composed
    composed_required = set(DEFS["Bid"]["required"]) | set(DEFS["SigningEnvelope"]["required"])
    assert set(DEFS["SignedBidSubmission"]["required"]) == composed_required


def test_the_trust_dims_object_pins_all_six_and_admits_no_seventh() -> None:
    dims = DEFS["TrustDims"]
    assert set(dims["properties"]) == set(dims["required"]) == set(DEFS["TrustDimension"]["enum"])
    assert len(dims["required"]) == 6
    assert dims["additionalProperties"] is False


def test_the_hard_constraint_object_has_no_weight_property() -> None:
    """R19, at the schema level: a filter is not a score term."""
    assert "weight" not in DEFS["HardConstraint"]["properties"]
    assert "weight" in DEFS["Preference"]["properties"]


def test_the_bundle_validates_a_real_payload_through_the_registry() -> None:
    from packages.contracts.tests._fixtures_protocol import make_bid

    assert is_valid("Bid", make_bid())
    assert not is_valid("Bid", {})


def test_the_committed_bundle_is_valid_json_and_stable() -> None:
    with PROTOCOL_SCHEMA_PATH.open(encoding="utf-8") as handle:
        assert json.load(handle) == BUNDLE


# --- codegen drift --------------------------------------------------------------------------


def test_the_committed_python_models_match_the_schema() -> None:
    """The whole point of generating: if this fails, the types describe a protocol that is gone."""
    problems = check(python=True, typescript=False)
    assert problems == [], "\n".join(problems)


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx is not on PATH")
def test_the_committed_typescript_types_match_the_schema() -> None:
    problems = check(python=False, typescript=True)
    assert problems == [], "\n".join(problems)


def test_the_generated_artifacts_are_committed_where_the_scope_names_them() -> None:
    assert PYTHON_OUT.is_file() and PYTHON_OUT.parent.name == "python"
    assert TS_OUT.is_file() and TS_OUT.parent.name == "ts"
    assert PROTOCOL_SCHEMA.is_file()


def test_the_generated_python_carries_a_do_not_edit_header() -> None:
    header = PYTHON_OUT.read_text(encoding="utf-8")[:400]
    assert "GENERATED FILE" in header and "DO NOT EDIT" in header


def test_the_codegen_cli_reports_rather_than_writes_under_check() -> None:
    before = PYTHON_OUT.read_bytes()
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "contracts.codegen", "--check", "--python-only"],
        capture_output=True,
        text=True,
        cwd=str(PROTOCOL_SCHEMA.parent.parent.parent.parent),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert PYTHON_OUT.read_bytes() == before
