"""Schema → typed models, in both languages, from one source of truth.

`packages/contracts/schemas/protocol.schema.json` is the only hand-maintained description of the
protocol. This module regenerates the two typed views of it:

* `packages/contracts/generated/python/protocol.py` — Pydantic v2 models, via
  `datamodel-code-generator`;
* `packages/contracts/generated/ts/protocol.schema.d.ts` — TypeScript types, via
  `json-schema-to-typescript` (the `codegen` script already declared in this package's
  `package.json`).

Run it with::

    ./.venv/bin/python -m contracts.codegen            # rewrite both artifacts in place
    ./.venv/bin/python -m contracts.codegen --check    # exit 1 if either is stale

Both artifacts are COMMITTED, and `tests/test_schema_bundle.py` runs the `--check` path, so a
schema edit that is not followed by a regeneration fails the build rather than leaving the
generated types quietly describing a protocol that no longer exists. Nothing hand-edits either
generated file.

Two generator flags carry real weight and are not cosmetic:

``--use-subclass-enum``
    emits ``class ProvenanceSource(StrEnum)`` instead of a bare ``Enum``. A bare ``Enum`` member
    serializes through ``json.dumps(..., default=str)`` as ``"ProvenanceSource.owner_statement"``,
    which no longer parses back into the enum — the protocol objects would stop round-tripping.

``--type-mappings string+date-time=string``
    keeps timestamps as ``str`` in Python while the schema still declares ``format: date-time``
    for JSON-Schema and Ajv validation. The wire format is the contract here; coercing to
    ``datetime`` would make ``model_dump()`` non-JSON-native and would reject the epoch-seconds
    and offset-less spellings other services legitimately send.
"""

from __future__ import annotations

import argparse
import difflib
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_ROOT.parent.parent

SCHEMA_DIR = PACKAGE_ROOT / "schemas"
PROTOCOL_SCHEMA = SCHEMA_DIR / "protocol.schema.json"
PYTHON_OUT = PACKAGE_ROOT / "generated" / "python" / "protocol.py"
TS_OUT = PACKAGE_ROOT / "generated" / "ts" / "protocol.schema.d.ts"

_PYTHON_HEADER = """\
# GENERATED FILE — DO NOT EDIT.
#
# Source:    packages/contracts/schemas/protocol.schema.json
# Generator: python -m contracts.codegen   (datamodel-code-generator, pydantic v2)
#
# `tests/test_schema_bundle.py` re-runs the generator and fails if this file no longer matches
# the schema, so editing it by hand is a defect the suite catches rather than a shortcut.
# ruff: noqa
"""


def _datamodel_codegen_argv(output: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "datamodel_code_generator",
        "--input",
        str(PROTOCOL_SCHEMA),
        "--input-file-type",
        "jsonschema",
        "--output",
        str(output),
        "--output-model-type",
        "pydantic_v2.BaseModel",
        "--target-python-version",
        "3.12",
        "--use-subclass-enum",
        # `Field(min_length=1)` rather than `constr(min_length=1)`. A `con*` call inside an
        # annotation is not a valid type to mypy — it fails with "Cannot use a function call in a
        # type annotation" on every constrained field, so the generated module would be
        # untypecheckable and `make types` would be red on code nobody wrote by hand.
        "--field-constraints",
        "--type-mappings",
        "string+date-time=string",
        "--use-schema-description",
        "--use-field-description",
        "--use-double-quotes",
        "--disable-timestamp",
        "--custom-file-header",
        _PYTHON_HEADER,
        # Ruff, not black+isort: `make lint` runs `ruff format --check .` over the whole repo,
        # including this generated file. Formatting it with a different formatter would leave the
        # repo one `ruff format` away from a codegen-drift failure that nobody caused.
        "--formatters",
        "ruff-check",
        "ruff-format",
    ]


def _json2ts_argv(output: Path) -> list[str]:
    return [
        "npx",
        "--no-install",
        "json2ts",
        "-i",
        str(PROTOCOL_SCHEMA),
        "-o",
        str(output),
        # Without this, json2ts emits ONLY the root type: every protocol object lives under
        # `$defs` and nothing in the bundle's root references them, so all 45 types would be
        # silently dropped and the TypeScript half of the contract would be an empty file.
        "--unreachableDefinitions",
        "--additionalProperties",
        "false",
        "--no-style.singleQuote",
    ]


def _run(argv: Sequence[str], *, cwd: Path) -> None:
    # The generators shell out to `ruff` and `npx`. Putting the running interpreter's own bin
    # directory first means the venv's ruff is found even when the caller's PATH has no venv on
    # it — otherwise the drift test would ERROR rather than pass or fail, depending only on how
    # pytest happened to be invoked.
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).resolve().parent), env.get("PATH", "")])
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(argv), cwd=str(cwd), capture_output=True, text=True, env=env
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"codegen step failed: {' '.join(argv)}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


def generate_python(output: Path | None = None) -> Path:
    """Write the Pydantic models. Returns the path written."""
    target = PYTHON_OUT if output is None else output
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(_datamodel_codegen_argv(target), cwd=REPO_ROOT)
    return target


def generate_typescript(output: Path | None = None) -> Path:
    """Write the TypeScript declarations. Returns the path written."""
    target = TS_OUT if output is None else output
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(_json2ts_argv(target), cwd=REPO_ROOT)
    return target


def _diff(label: str, committed: Path, fresh: Path) -> list[str]:
    if not committed.is_file():
        return [f"{label}: {committed} is missing — run `python -m contracts.codegen`"]
    old = committed.read_text(encoding="utf-8").splitlines(keepends=True)
    new = fresh.read_text(encoding="utf-8").splitlines(keepends=True)
    if old == new:
        return []
    delta = list(
        difflib.unified_diff(old, new, fromfile=f"committed {label}", tofile=f"regenerated {label}")
    )
    return [f"{label} is stale — run `python -m contracts.codegen`:"] + [
        line.rstrip("\n") for line in delta[:60]
    ]


def check(*, python: bool = True, typescript: bool = True) -> list[str]:
    """Regenerate into a temp dir and report every artifact that has drifted."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        if python:
            fresh = generate_python(root / "protocol.py")
            problems.extend(_diff("generated/python/protocol.py", PYTHON_OUT, fresh))
        if typescript:
            fresh = generate_typescript(root / "protocol.schema.d.ts")
            problems.extend(_diff("generated/ts/protocol.schema.d.ts", TS_OUT, fresh))
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of writing when stale")
    parser.add_argument("--python-only", action="store_true")
    parser.add_argument("--typescript-only", action="store_true")
    args = parser.parse_args(argv)

    want_python = not args.typescript_only
    want_typescript = not args.python_only

    if args.check:
        problems = check(python=want_python, typescript=want_typescript)
        for line in problems:
            print(line)
        return 1 if problems else 0

    if want_python:
        print(f"wrote {generate_python()}")
    if want_typescript:
        print(f"wrote {generate_typescript()}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
