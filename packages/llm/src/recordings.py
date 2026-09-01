"""Loader for the hand-authored recorded fixtures under ``packages/llm/fixtures/recorded``.

D21: *"recorded" means hand-authored, committed, human-reviewed fixture JSON derived from
published API documentation.* Nothing in this repo captures a live response — there are no
credentials here (D3) — so a "recording" is a reviewed file, and the thing that makes it
trustworthy is its **provenance header**. This module refuses to load a file that does not
carry one, so an undocumented fixture cannot quietly become a test's ground truth.

File shape::

    {
      "provenance": {
        "source": "...", "doc_version": "...", "authored_by": "...",
        "authored_at": "...", "capture": "..."
      },
      "system": "<the system contract every recording below was authored against>",
      "recordings": {"<exact user turn>": "<reply>"}
    }

The ``system`` key is what makes a recording honest. A request carries two halves — the
system blocks and the user turn — and the system half is the prompt *contract*: "copy
values verbatim; never infer", the envelope rules, the tool list. It is what decides the
answer against a live model, so a fixture that folds it into the prompt string (or omits
it) lets an inverted contract keep replaying the old reviewed reply. Each file therefore
records one contract and the exchanges authored against it, and
:class:`llm.doubles.RecordedLLM` keys on the pair.

``system`` is optional and defaults to ``""``, which means "this fixture was authored for
calls with no system contract at all".

Read one with :func:`load_recording` (by stem) or build a double straight from it with
:meth:`llm.doubles.RecordedLLM.from_fixture`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm.errors import RecordingError

#: ``packages/llm/fixtures/recorded``. Derived from this file's location — resolving the
#: ``.pkgroot/llm`` symlink — so it is correct from any working directory.
RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"

#: Every field a provenance header must carry (D21). ``doc_version`` is the one D21 names
#: explicitly; the rest exist so a reader can tell where a fixture came from without
#: needing the commit that added it.
REQUIRED_PROVENANCE_FIELDS: tuple[str, ...] = (
    "source",
    "doc_version",
    "authored_by",
    "authored_at",
    "capture",
)


def available_recordings() -> list[str]:
    """The stems of every recorded fixture, sorted. ``[]`` when none are committed."""
    if not RECORDINGS_DIR.is_dir():
        return []
    return sorted(path.stem for path in RECORDINGS_DIR.glob("*.json"))


def recording_path(name: str) -> Path:
    """The path a recording stem maps to, whether or not it exists."""
    return RECORDINGS_DIR / f"{name}.json"


def load_recording_file(path: Path | str) -> tuple[dict[tuple[str, str], str], dict[str, Any]]:
    """Load and validate one recorded-fixture file.

    Args:
        path: the ``.json`` file.

    Returns:
        ``(recordings, provenance)`` — a mapping keyed by the ``(system, prompt)`` pair a
        request actually carries, and the header. Every key in one file shares that
        file's ``system`` value.

    Raises:
        RecordingError: the file is missing, is not JSON, has no ``provenance`` header
            with every field in :data:`REQUIRED_PROVENANCE_FIELDS`, or has a
            ``recordings`` table that is not a non-empty mapping of string to string.
    """
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecordingError(f"cannot read recorded fixture {file_path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecordingError(f"{file_path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RecordingError(
            f"{file_path} must contain a JSON object, got {type(document).__name__}"
        )

    provenance = document.get("provenance")
    if not isinstance(provenance, dict):
        raise RecordingError(
            f"{file_path} has no `provenance` object. D21 requires every recorded fixture "
            f"to say where it came from; required fields: "
            f"{', '.join(REQUIRED_PROVENANCE_FIELDS)}"
        )
    missing = [
        field for field in REQUIRED_PROVENANCE_FIELDS if not str(provenance.get(field, "")).strip()
    ]
    if missing:
        raise RecordingError(
            f"{file_path} provenance header is missing: {', '.join(missing)} (D21)"
        )

    system = document.get("system", "")
    if not isinstance(system, str):
        raise RecordingError(
            f"{file_path} `system` must be a string (the contract these recordings were "
            f"authored against), got {type(system).__name__}"
        )

    recordings = document.get("recordings")
    if not isinstance(recordings, dict) or not recordings:
        raise RecordingError(f"{file_path} must carry a non-empty `recordings` object")
    keyed: dict[tuple[str, str], str] = {}
    for prompt, reply in recordings.items():
        if not isinstance(prompt, str) or not prompt.strip():
            raise RecordingError(f"{file_path} has a blank prompt key")
        if not isinstance(reply, str):
            raise RecordingError(
                f"{file_path} recording for {prompt[:60]!r} is {type(reply).__name__}, not a string"
            )
        keyed[(system, prompt)] = reply
    return keyed, dict(provenance)


def load_recording(name: str) -> dict[tuple[str, str], str]:
    """The ``(system, prompt)``→reply table of the fixture ``name`` (its filename stem).

    Raises:
        RecordingError: if there is no such fixture, or it fails validation. The message
            lists the fixtures that do exist.
    """
    path = recording_path(name)
    if not path.is_file():
        known = ", ".join(available_recordings()) or "(none committed)"
        raise RecordingError(
            f"no recorded fixture named {name!r} in {RECORDINGS_DIR}; have: {known}"
        )
    recordings, _ = load_recording_file(path)
    return recordings


def load_provenance(name: str) -> dict[str, Any]:
    """The provenance header of the recorded fixture ``name``."""
    _, provenance = load_recording_file(recording_path(name))
    return provenance


def load_system_contract(name: str) -> str:
    """The system contract the recorded fixture ``name`` was authored against."""
    document = json.loads(recording_path(name).read_text(encoding="utf-8"))
    system = document.get("system", "")
    return system if isinstance(system, str) else ""


def load_all_recordings() -> dict[tuple[str, str], str]:
    """Every committed recording, merged into one table.

    Raises:
        RecordingError: on a duplicate prompt across two files — two different reviewed
            answers to the same prompt is an ambiguity, not a merge.
    """
    merged: dict[tuple[str, str], str] = {}
    origins: dict[tuple[str, str], str] = {}
    for name in available_recordings():
        for key, reply in load_recording(name).items():
            if key in merged and merged[key] != reply:
                raise RecordingError(
                    f"prompt {key[1][:60]!r} is recorded differently under the same "
                    f"system contract in {origins[key]!r} and {name!r}"
                )
            merged[key] = reply
            origins[key] = name
    return merged
