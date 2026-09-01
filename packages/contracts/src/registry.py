"""Runtime access to the JSON Schema bundle, for callers that validate data rather than types.

The generated Pydantic models cover Python callers. This module covers the other two needs:

* validating a payload that is NOT going to be turned into a model — an OpenAPI example, a
  fixture, a body arriving at a boundary that must be refused before it is parsed;
* handing the raw schema to something else (an API doc, a consumer in another language).

`schema_for("Bid")` returns a SELF-CONTAINED subschema: a `$ref` into the bundle plus the whole
`$defs` table. Self-contained matters because it means the caller needs no resolver, no registry
and no network — the same schema object works in `jsonschema`, in Ajv, and pasted into a tool.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = PACKAGE_ROOT / "schemas"
PROTOCOL_SCHEMA_PATH = SCHEMA_DIR / "protocol.schema.json"
PROTOCOL_SCHEMA_ID = "https://proxyshop.dev/schemas/protocol.schema.json"


@lru_cache(maxsize=1)
def protocol_schema() -> Mapping[str, Any]:
    """The whole bundle, parsed once."""
    with PROTOCOL_SCHEMA_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def schema_names() -> tuple[str, ...]:
    """Every definition name in the bundle, sorted."""
    return tuple(sorted(protocol_schema()["$defs"]))


def schema_for(name: str) -> dict[str, Any]:
    """A self-contained JSON Schema for one protocol object."""
    bundle = protocol_schema()
    defs = bundle["$defs"]
    if name not in defs:
        raise KeyError(
            f"{name!r} is not a protocol object; the bundle defines {list(sorted(defs))}"
        )
    # Deliberately no `$id`: every subschema would otherwise be a distinct registration under the
    # same bundle id, and a second validator compiling the same object would collide with the
    # first. The `$ref` is local, so the schema resolves with no registry at all.
    return {
        "$schema": bundle["$schema"],
        "$ref": f"#/$defs/{name}",
        "$defs": defs,
    }


@cache
def _validator(name: str) -> Any:
    from jsonschema import Draft202012Validator

    return Draft202012Validator(schema_for(name))


def iter_errors(name: str, instance: Any) -> Iterator[Any]:
    """Every validation error for `instance` against the named schema, best-first."""
    yield from _validator(name).iter_errors(instance)


def validation_errors(name: str, instance: Any) -> list[str]:
    """Human-readable validation errors. Empty list means the instance is valid."""
    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(iter_errors(name, instance), key=lambda e: list(e.absolute_path))
    ]


def is_valid(name: str, instance: Any) -> bool:
    """True when `instance` validates against the named protocol schema."""
    return not any(True for _ in iter_errors(name, instance))


def validate(name: str, instance: Any) -> None:
    """Raise `ValueError` listing every problem when `instance` is not the named object."""
    errors = validation_errors(name, instance)
    if errors:
        raise ValueError(f"{name} is invalid:\n  " + "\n  ".join(errors))


__all__ = [
    "PROTOCOL_SCHEMA_ID",
    "PROTOCOL_SCHEMA_PATH",
    "SCHEMA_DIR",
    "is_valid",
    "iter_errors",
    "protocol_schema",
    "schema_for",
    "schema_names",
    "validate",
    "validation_errors",
]
