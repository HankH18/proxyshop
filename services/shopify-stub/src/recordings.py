"""Loading and matching the recorded real-API shapes (T-013 acceptance 1, D21).

D21: *"recorded" means hand-authored, committed, human-reviewed fixture JSON derived from
published API documentation*, and every recording carries a provenance header naming the
doc version. There is no live capture and there are no Shopify credentials in this
environment (D3), so the recordings are the only description of the real API this repo has.
That makes their honesty load-bearing: a fixture invented from memory but labelled as
documentation-derived is worse than no fixture, because every consumer builds on it.

A recording file
----------------
::

    {
      "$provenance": {
        "source":       "<the exact doc URL the shape came from>",
        "api_version":  "<the version stamp that page carried>",
        "derivation":   "<how it was produced, in one honest sentence>",
        "caveats":      ["<anything NOT confirmed by that page>"]
      },
      "operation":  "<what this records>",
      "request":    { ...optional... },
      "response":   { ...the documented shape... },
      "documented_keys": { "<dotted path>": ["<every key the docs list here>"] }
    }

and it is checked in **both** directions, because each direction catches a different bug:

:func:`assert_conforms`
    Every key the *recording* has, the stub's live response also has, with the same JSON
    type. Catches a stub that dropped or renamed a field the real API returns.
:func:`assert_no_invented_keys`
    Every key the *stub* emits appears in ``documented_keys`` for its path. Catches a stub
    that made a field up — the failure mode a one-directional shape check cannot see, and
    the one that quietly teaches four downstream tickets to read a field that does not
    exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: ``services/shopify-stub/fixtures/recorded``.
RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "recorded"

#: Keys a provenance header must carry. A recording without them is rejected at load time
#: rather than at review time.
REQUIRED_PROVENANCE_FIELDS = ("source", "api_version", "derivation")


class RecordingError(ValueError):
    """A recording is missing, malformed, or missing its provenance header."""


def recording_paths() -> list[Path]:
    """Every recording file, sorted. Used by the test that checks all of them at once."""
    return sorted(RECORDINGS_DIR.glob("*.json"))


def load(name: str) -> dict[str, Any]:
    """Load one recording by file stem, validating its provenance header.

    Args:
        name: the file stem, e.g. ``"admin_discount_code_basic_create"``.

    Returns:
        The parsed recording.

    Raises:
        RecordingError: the file is missing, is not an object, or lacks a complete
            ``$provenance`` header. Loading a recording with no provenance would silently
            reintroduce exactly the "invented from memory" failure D21 exists to prevent.
    """
    path = RECORDINGS_DIR / f"{name}.json"
    if not path.is_file():
        raise RecordingError(f"no recording named {name!r} in {RECORDINGS_DIR}")
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_provenance(data, path)
    return data


def validate_provenance(data: Any, path: Path) -> None:
    """Raise unless ``data`` carries a complete ``$provenance`` header."""
    if not isinstance(data, dict):
        raise RecordingError(f"{path.name}: a recording must be a JSON object")
    provenance = data.get("$provenance")
    if not isinstance(provenance, dict):
        raise RecordingError(f"{path.name}: missing the $provenance header (D21)")
    missing = [field for field in REQUIRED_PROVENANCE_FIELDS if not provenance.get(field)]
    if missing:
        raise RecordingError(
            f"{path.name}: $provenance is missing {', '.join(missing)} (D21 requires the "
            f"doc version the shape was derived from)"
        )


def json_type(value: Any) -> str:
    """The JSON type name of ``value``.

    ``bool`` is checked before ``int`` because ``isinstance(True, int)`` is ``True`` in
    Python, and a stub that returned ``True`` where the API returns ``1`` would otherwise
    pass a type check.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


#: The ``$``-prefixed key a recording uses to name the arrays that are allowed to arrive
#: empty. See :func:`diff_shape`.
MAY_BE_EMPTY_KEY = "$may_be_empty"


def diff_shape(
    expected: Any, actual: Any, path: str = "", *, may_be_empty: bool = False
) -> list[str]:
    """Structural differences between a recorded template and a live value.

    Rules, and the reasoning for each:

    * A key in ``expected`` missing from ``actual`` is a difference. The reverse is **not**:
      the recording is a template of what must be there, and the stub is allowed to answer
      a wider selection set than the recording's example asked for.
    * Types must agree, with one exception: ``null`` in the recording matches any type,
      because a documented nullable field's example value tells you nothing about the type
      it carries when populated.
    * Arrays are compared element-wise against ``expected[0]``. An empty ``expected`` array
      only asserts "this is an array" — which is the honest reading of a doc example whose
      array is empty.
    * **A recorded non-empty array must not arrive empty**, unless the recording explicitly
      says that array may be. This is the rule that was missing, and its absence was the
      single largest hole in acceptance criterion 1.

    Why the last rule, and why it is per-array
    ------------------------------------------
    Element-wise comparison iterates ``actual``, so an empty ``actual`` iterated zero times
    and reported nothing. Making the ``orders/paid`` webhook ship **zero line items** —
    the payload's entire point — left the suite fully green and the parity check reporting
    ``PASSED``. :func:`collect_keys` could not see it either: ``{"line_items": []}`` yields
    no ``line_items[]`` path at all, so :func:`assert_no_invented_keys` has nothing to
    object to. Both guard mechanisms were blind to a dropped array, in the one direction
    that matters most.

    The obvious fix — "non-empty expected, empty actual, always a difference" — is wrong,
    and cheaply so: ``webhook_orders_paid`` records a populated ``discount_codes`` and
    ``discount_applications``, and both are *legitimately* empty for an undiscounted order.
    A blanket rule would fire on the first no-discount order any consumer asserts on.

    So the rule is **fail-closed with a per-array opt-out**: a recording names the arrays
    that may legitimately be empty in a sibling ``"$may_be_empty": [...]`` list (``$`` keys
    are skipped by the key walk above, so it is metadata, not shape). Fail-closed rather
    than fail-open because a new recording that forgets the marker should get the protection,
    not silently lose it — an opt-in ``$non_empty`` list would reintroduce exactly this hole
    for every recording anybody adds later.

    Args:
        expected: the recorded template.
        actual: the live value.
        path: dotted path, for the message.
        may_be_empty: set by the parent object when this value's key is named in its
            ``$may_be_empty`` list. Only meaningful for arrays.
    """
    problems: list[str] = []
    here = path or "<root>"
    if expected is None:
        return problems
    expected_type = json_type(expected)
    actual_type = json_type(actual)
    if expected_type != actual_type:
        return [f"{here}: expected {expected_type}, got {actual_type}"]
    if expected_type == "object":
        optional = expected.get(MAY_BE_EMPTY_KEY) or []
        for key, value in expected.items():
            if key.startswith("$"):
                continue
            if key not in actual:
                problems.append(f"{here}.{key}: missing")
                continue
            problems.extend(
                diff_shape(value, actual[key], f"{here}.{key}", may_be_empty=key in optional)
            )
    elif expected_type == "array" and expected:
        if not actual and not may_be_empty:
            problems.append(
                f"{here}: the recording documents a non-empty array here and the response "
                f"is empty. If this array is legitimately empty in some valid scenario, name "
                f"it in the enclosing object's {MAY_BE_EMPTY_KEY!r} list and say why."
            )
        for index, item in enumerate(actual):
            problems.extend(diff_shape(expected[0], item, f"{here}[{index}]"))
    return problems


def assert_conforms(expected: Any, actual: Any, *, label: str) -> None:
    """Raise :class:`AssertionError` listing every structural difference."""
    problems = diff_shape(expected, actual)
    if problems:
        raise AssertionError(
            f"{label} does not match the recorded shape:\n  " + "\n  ".join(problems)
        )


def collect_keys(value: Any, path: str = "") -> dict[str, set[str]]:
    """Map every object path in ``value`` to the set of keys found there.

    Array indices collapse to ``[]`` so that ``line_items[0]`` and ``line_items[1]`` are the
    same path — a recording documents an element shape, not a position.
    """
    found: dict[str, set[str]] = {}
    if isinstance(value, dict):
        found.setdefault(path, set()).update(value.keys())
        for key, item in value.items():
            child = f"{path}.{key}" if path else key
            for sub_path, keys in collect_keys(item, child).items():
                found.setdefault(sub_path, set()).update(keys)
    elif isinstance(value, list):
        for item in value:
            for sub_path, keys in collect_keys(item, f"{path}[]").items():
                found.setdefault(sub_path, set()).update(keys)
    return found


def assert_no_invented_keys(actual: Any, documented: dict[str, list[str]], *, label: str) -> None:
    """Raise unless every key the stub emits is documented for its path.

    ``documented`` maps a path (``""`` for the root, ``"line_items[]"`` for an element) to
    the list of key names the published documentation lists there.

    **Every object path the stub emits must be enumerated.** An earlier version skipped paths
    the recording did not name, reasoning that a gap in the recording should not read as a
    failure of the code. That was wrong, and adversarial review proved it: the recordings
    enumerated only 7 of 14 object paths in the ``orders/paid`` payload and 7 of 36 in the
    orders query, so an invented key planted at any unenumerated path — inside
    ``lineItems.edges[].node``, inside ``context.document.location``, inside ``duties[]`` —
    sailed through a fully green suite. The check advertised itself as catching invented
    fields while covering a minority of the surface it was pointed at.

    So an unenumerated path is now a failure, and the only way to satisfy it is to extend the
    recording — which is exactly the work that makes the recording worth having. The
    ``documented`` map is the claim "I enumerated the real key names here"; skipping
    unenumerated paths let that claim go unmade for most of the payload while still reading
    like coverage.

    A path in ``documented`` that is **absent** from ``actual`` is still not a failure here:
    that is the forward direction's job (:func:`assert_conforms`), and folding it in would
    fire on any documented element shape whose array is legitimately empty — ``userErrors:
    []`` on every successful mutation, for instance.
    """
    emitted = collect_keys(actual)
    problems: list[str] = []
    for path, keys in sorted(emitted.items()):
        if path not in documented:
            problems.append(
                f"{path or '<root>'}: path is NOT enumerated in documented_keys, so the "
                f"recording makes no claim about the keys present here "
                f"({', '.join(sorted(keys))}). Extend the recording rather than narrowing "
                f"this check."
            )
            continue
        invented = sorted(keys - set(documented[path]))
        if invented:
            problems.append(f"{path or '<root>'}: undocumented key(s) {', '.join(invented)}")
    if problems:
        raise AssertionError(
            f"{label} emits keys the recording does not document:\n  " + "\n  ".join(problems)
        )
