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
      "recordings": {"<exact prompt text>": "<reply>"}
    }

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


def load_recording_file(path: Path | str) -> tuple[dict[str, str], dict[str, Any]]:
    """Load and validate one recorded-fixture file.

    Args:
        path: the ``.json`` file.

    Returns:
        ``(recordings, provenance)`` — an exact prompt→reply mapping, and the header.

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

    recordings = document.get("recordings")
    if not isinstance(recordings, dict) or not recordings:
        raise RecordingError(f"{file_path} must carry a non-empty `recordings` object")
    for prompt, reply in recordings.items():
        if not isinstance(prompt, str) or not prompt.strip():
            raise RecordingError(f"{file_path} has a blank prompt key")
        if not isinstance(reply, str):
            raise RecordingError(
                f"{file_path} recording for {prompt[:60]!r} is {type(reply).__name__}, not a string"
            )
    return dict(recordings), dict(provenance)


def load_recording(name: str) -> dict[str, str]:
    """The prompt→reply table of the recorded fixture ``name`` (its filename stem).

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


def load_all_recordings() -> dict[str, str]:
    """Every committed recording, merged into one table.

    Raises:
        RecordingError: on a duplicate prompt across two files — two different reviewed
            answers to the same prompt is an ambiguity, not a merge.
    """
    merged: dict[str, str] = {}
    origins: dict[str, str] = {}
    for name in available_recordings():
        for prompt, reply in load_recording(name).items():
            if prompt in merged and merged[prompt] != reply:
                raise RecordingError(
                    f"prompt {prompt[:60]!r} is recorded differently in "
                    f"{origins[prompt]!r} and {name!r}"
                )
            merged[prompt] = reply
            origins[prompt] = name
    return merged
