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
import pathlib
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


def test_the_committed_bundle_is_the_whole_protocol_on_disk() -> None:
    """Asserting `json.load(f) == BUNDLE` alone would be a tautology — `BUNDLE` IS `json.load(f)`,
    so it holds for an empty file too. The size and the pinned names are what make it a check."""
    with PROTOCOL_SCHEMA_PATH.open(encoding="utf-8") as handle:
        on_disk = json.load(handle)
    assert on_disk == BUNDLE
    assert on_disk["$id"] == "https://proxyshop.dev/schemas/protocol.schema.json"
    assert len(on_disk["$defs"]) >= 40
    assert set(PINNED_PROTOCOL_OBJECTS) <= set(on_disk["$defs"])
    for name in ("SigningEnvelope", "SignedBidSubmission", "RankingWeights", "Store"):
        assert name in on_disk["$defs"], f"{name} vanished from the bundle"


# --- codegen drift --------------------------------------------------------------------------


def test_the_committed_python_models_match_the_schema() -> None:
    """The whole point of generating: if this fails, the types describe a protocol that is gone."""
    problems = check(python=True, typescript=False)
    assert problems == [], "\n".join(problems)


def test_the_committed_typescript_types_match_the_schema() -> None:
    """Deliberately NOT skipped when `npx` is missing. Skipping would silently drop half the drift
    check on exactly the machines least likely to have regenerated the `.d.ts` — and this repo
    requires node anyway (`scripts/verify.sh` refuses to run without `node_modules`)."""
    assert shutil.which("npx") is not None, (
        "npx is required to check the generated TypeScript against the schema; run `npm ci`"
    )
    problems = check(python=False, typescript=True)
    assert problems == [], "\n".join(problems)


def test_the_drift_check_actually_detects_drift() -> None:
    """A `check()` that returned `[]` unconditionally would satisfy both tests above. Comparing a
    deliberately-wrong committed file proves the diff has teeth."""
    import tempfile

    from contracts.codegen import _diff, generate_python

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        fresh = generate_python(root / "fresh.py")
        stale = root / "stale.py"
        stale.write_text("# not the generated models\n", encoding="utf-8")
        assert _diff("stale", stale, fresh), "the drift check did not notice a wrong file"
        assert _diff("fresh", fresh, fresh) == [], "the drift check flagged an identical file"
        assert _diff("absent", root / "nope.py", fresh), "a missing artifact must be reported"


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


def test_the_object_and_enum_partitions_are_non_empty() -> None:
    """Guards every `@parametrize` in this file: an empty list turns them into SKIPS, which pytest
    reports as neither passing nor failing and everyone reads as green."""
    assert len(_OBJECTS) >= 25
    assert len(_ENUMS) >= 9
    assert set(_OBJECTS).isdisjoint(_ENUMS)
    assert set(_OBJECTS) | _ENUMS == set(DEFS)


# --- F9: exactly one definition of the codegen command --------------------------------------


def test_the_json2ts_invocation_is_defined_in_exactly_one_place() -> None:
    """`package.json` used to declare its own `codegen` script re-stating every json2ts flag.
    The two agreed, but the drift test only ever regenerates through `contracts.codegen`, so an
    edit to either was invisible to the other: the npm script could have kept
    `--unreachableDefinitions` after the Python side dropped it (or vice versa) and every test in
    this repo would still have been green.

    `_json2ts_argv` is the single definition. Nothing else may re-state it."""
    import json as _json

    from contracts.codegen import _json2ts_argv

    package_json = _json.loads(
        (PROTOCOL_SCHEMA.parent.parent / "package.json").read_text(encoding="utf-8")
    )
    scripts = package_json.get("scripts", {})
    offenders = [
        name for name, body in scripts.items() if "json2ts" in body or "json-schema-to" in body
    ]
    assert offenders == [], (
        f"package.json re-defines the codegen command in {offenders}; `_json2ts_argv` in "
        "contracts.codegen is the one definition"
    )

    # Control: the one definition really is the json2ts call, so this test cannot pass by the
    # command having quietly disappeared from the codebase altogether.
    argv = _json2ts_argv(TS_OUT)
    assert "json2ts" in argv
    assert "--unreachableDefinitions" in argv
