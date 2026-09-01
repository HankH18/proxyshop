"""Committed codegen artifacts for `packages/contracts`.

Everything in this directory is produced by `python -m contracts.codegen` from
`packages/contracts/schemas/protocol.schema.json` and is checked in so that consumers do not
need a generator on the path. `packages/contracts/src/generated` is a tracked symlink to this
directory, which is what makes `contracts.generated.protocol` importable under the flat source
layout (D42) while the artifact itself stays in the `generated/python` path the ticket scope
names.

Do not hand-edit anything here: `tests/test_codegen_drift.py` regenerates and diffs.
"""
