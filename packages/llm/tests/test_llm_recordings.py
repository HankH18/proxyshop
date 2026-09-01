"""D21: every recorded fixture is hand-authored, committed, and carries provenance.

"Recorded" in this repo does not mean captured — there are no credentials here (D3) and no
ticket performs a live capture. What makes a fixture trustworthy is therefore its
provenance header, so the loader refuses a file without one and this file proves the
refusal is real rather than documented.
"""

from __future__ import annotations

import json

import pytest

from packages.llm import (
    RECORDINGS_DIR,
    REQUIRED_PROVENANCE_FIELDS,
    RecordingError,
    available_recordings,
    load_provenance,
    load_recording,
    load_recording_file,
)

GOOD_PROVENANCE = {field: f"value for {field}" for field in REQUIRED_PROVENANCE_FIELDS}


def _write(tmp_path, document) -> str:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------------------
# what is committed
# --------------------------------------------------------------------------------------


def test_the_recordings_directory_is_inside_this_package() -> None:
    """D21 pins T-014's recordings to packages/llm/fixtures/recorded/**."""
    assert RECORDINGS_DIR.is_dir()
    assert RECORDINGS_DIR.parts[-3:] == ("llm", "fixtures", "recorded")


def test_at_least_one_fixture_is_committed() -> None:
    assert available_recordings(), "acceptance 3 requires committed recorded fixtures"


@pytest.mark.parametrize("name", available_recordings())
def test_every_fixture_loads_and_carries_a_full_provenance_header(name: str) -> None:
    provenance = load_provenance(name)
    for field in REQUIRED_PROVENANCE_FIELDS:
        assert str(provenance.get(field, "")).strip(), f"{name}: provenance.{field} is blank"
    assert "doc" in provenance["doc_version"].lower() or "api" in provenance["doc_version"].lower()
    assert provenance["capture"].strip().lower().startswith("none"), (
        f"{name}: D21 forbids live capture, so `capture` must start with 'none' and say "
        f"where the content actually came from"
    )
    recordings = load_recording(name)
    assert recordings and all(isinstance(v, str) and v for v in recordings.values())


@pytest.mark.parametrize("name", available_recordings())
def test_fixture_prompts_look_like_assembled_prompts(name: str) -> None:
    """A recording key is the exact string a live call would have sent."""
    for prompt in load_recording(name):
        assert len(prompt) > 40, f"{name}: {prompt!r} is too short to be a real prompt"
        assert "\n" in prompt, f"{name}: {prompt!r} has no sections"


def test_all_fixtures_merge_without_a_conflicting_answer() -> None:
    from llm.recordings import load_all_recordings

    merged = load_all_recordings()
    total = sum(len(load_recording(name)) for name in available_recordings())
    assert len(merged) == total, "two fixtures record the same prompt"


# --------------------------------------------------------------------------------------
# the loader's refusals
# --------------------------------------------------------------------------------------


def test_a_fixture_without_a_provenance_header_is_refused(tmp_path) -> None:
    path = _write(tmp_path, {"recordings": {"p": "r"}})
    with pytest.raises(RecordingError) as excinfo:
        load_recording_file(path)
    assert "provenance" in str(excinfo.value)
    assert "D21" in str(excinfo.value)


@pytest.mark.parametrize("missing", REQUIRED_PROVENANCE_FIELDS)
def test_a_partial_provenance_header_is_refused(tmp_path, missing: str) -> None:
    provenance = {k: v for k, v in GOOD_PROVENANCE.items() if k != missing}
    path = _write(tmp_path, {"provenance": provenance, "recordings": {"p": "r"}})
    with pytest.raises(RecordingError) as excinfo:
        load_recording_file(path)
    assert missing in str(excinfo.value)


def test_a_blank_provenance_value_is_refused(tmp_path) -> None:
    provenance = dict(GOOD_PROVENANCE, doc_version="   ")
    path = _write(tmp_path, {"provenance": provenance, "recordings": {"p": "r"}})
    with pytest.raises(RecordingError):
        load_recording_file(path)


@pytest.mark.parametrize(
    "document",
    [
        {"provenance": GOOD_PROVENANCE},
        {"provenance": GOOD_PROVENANCE, "recordings": {}},
        {"provenance": GOOD_PROVENANCE, "recordings": {"p": 7}},
        {"provenance": GOOD_PROVENANCE, "recordings": {"  ": "r"}},
        ["not", "an", "object"],
    ],
)
def test_a_malformed_recordings_table_is_refused(tmp_path, document) -> None:
    with pytest.raises(RecordingError):
        load_recording_file(_write(tmp_path, document))


def test_a_non_json_file_is_refused(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RecordingError):
        load_recording_file(path)


def test_a_missing_fixture_names_the_ones_that_exist() -> None:
    with pytest.raises(RecordingError) as excinfo:
        load_recording("no_such_fixture")
    message = str(excinfo.value)
    assert "no_such_fixture" in message
    for name in available_recordings():
        assert name in message
