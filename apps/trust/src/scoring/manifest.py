"""Read-only access to the human-approved fixture manifest (T-080).

D18 / SPEC A3: "the trust engine catches the dishonest store" is circular if the dishonest
behaviours, the weights and the claim-type routing come from the trust engine's own config.
They come from ``fixtures/manifest.json``, a human approves that document, and **this engine
never writes it**. Every read in this package goes through here, and nothing in this package
opens that file for writing.

Why the constants below still carry published fallbacks
-------------------------------------------------------
``apps/trust/Dockerfile`` copies ``packages/contracts``, ``proxyshop_support`` and
``apps/trust/src`` — and NOT ``fixtures/``. A scoring module that required the manifest on
disk would pass every test in this repo and then fail to import inside the deployed
container. So each constant is resolved as *"the manifest's value when the approved document
is reachable, else the value published in DESIGN/the manifest as of manifest_version 1.0.0"*,
and :data:`MANIFEST_SOURCE` records which of the two actually happened. That keeps the
service deployable without letting the engine quietly invent ground truth: when the document
IS present — which is the case in every test run and in the simulator — it wins, and the
acceptance suite compares the engine's routing against it directly.

The fallbacks are deliberately not a second opinion. They are a transcription of the same
approved table, and ``apps/trust/tests/test_scoring.py`` compares every one of them against
the manifest on disk, so a drift between the two is a test failure in this lane rather than a
silent divergence discovered in production.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "MANIFEST_ENV_VAR",
    "MANIFEST_RELATIVE_PATH",
    "load_manifest",
    "manifest_int",
    "manifest_mapping",
    "manifest_number",
    "manifest_path",
    "manifest_source",
    "reset_manifest_cache",
]

#: Point the engine at a manifest explicitly (the deployed service, or a simulator run
#: against an alternative approved document). An unset or unreadable value falls back to the
#: search below rather than failing: see the module docstring.
MANIFEST_ENV_VAR = "PROXYSHOP_FIXTURES_MANIFEST"

#: Where the one approved manifest lives, relative to the checkout root. Pinned, never
#: globbed — a second manifest would make ground truth ambiguous (T-080 frozen schema).
MANIFEST_RELATIVE_PATH = Path("fixtures") / "manifest.json"

_CACHE: dict[str, Any] = {}


def manifest_path() -> Path | None:
    """The approved manifest's location, or ``None`` when it is not reachable.

    ``Path(__file__).resolve()`` collapses the ``.pkgroot/trust`` symlink first, so the walk
    starts from the real ``apps/trust/src/scoring/`` under either import spelling and finds
    the same checkout root either way.
    """
    override = os.environ.get(MANIFEST_ENV_VAR)
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    for parent in Path(__file__).resolve().parents:
        candidate = parent / MANIFEST_RELATIVE_PATH
        if candidate.is_file():
            return candidate
    return None


def load_manifest() -> Mapping[str, Any]:
    """The approved manifest as plain data, or an empty mapping when it is unreachable.

    Cached: the document is immutable ground truth for the life of a process, and the
    alternative is re-reading and re-parsing it on every ``score()`` call. Use
    :func:`reset_manifest_cache` in a test that deliberately points the engine somewhere
    else.

    A manifest that is present but unreadable or not a JSON object is treated exactly like an
    absent one — the published fallbacks take over. It is NOT silently half-used: a partly
    parsed document is the one shape that could route a claim type to a dimension nobody
    approved.
    """
    if "manifest" in _CACHE:
        return _CACHE["manifest"]
    path = manifest_path()
    loaded: Mapping[str, Any] = {}
    source = "published-fallback"
    if path is not None:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parsed = None
        if isinstance(parsed, Mapping):
            loaded = parsed
            source = str(path)
    _CACHE["manifest"] = loaded
    _CACHE["source"] = source
    return loaded


def manifest_source() -> str:
    """``str(path)`` of the manifest actually in use, or ``"published-fallback"``."""
    load_manifest()
    return str(_CACHE.get("source", "published-fallback"))


def reset_manifest_cache() -> None:
    """Forget the cached manifest. For tests that repoint :data:`MANIFEST_ENV_VAR`."""
    _CACHE.clear()


def manifest_number(key: str, default: float) -> float:
    """One numeric manifest constant, falling back to its published value."""
    value = load_manifest().get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(default)
    return float(value)


def manifest_int(key: str, default: int) -> int:
    """One integer manifest constant, falling back to its published value.

    An INTEGRAL float counts. JSON has one number type and a serialiser that round-trips the
    manifest through a float is free to write ``5.0``; refusing that would silently discard a
    published value while :func:`manifest_source` still reported the document as live, which
    is the one failure mode this module's fallback design has to avoid. A genuinely
    fractional value is refused instead of truncated — ``new_store_prior_n: 4.5`` is a
    manifest error, and rounding it here would hide it.
    """
    value = load_manifest().get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return int(default)
    if isinstance(value, float) and not value.is_integer():
        return int(default)
    return int(value)


def manifest_mapping(key: str) -> dict[str, Any]:
    """One manifest sub-object as plain data (empty when absent or the wrong shape)."""
    value = load_manifest().get(key)
    return dict(value) if isinstance(value, Mapping) else {}
