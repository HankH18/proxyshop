"""Load the human-approved fixture manifest — and refuse to load a broken one.

The manifest at ``fixtures/manifest.json`` is ground truth (SPEC A3): the dishonest store's
behaviours, the episode budget, the new-store prior N, the expected trust trajectory, the
``claim_type -> trust dimension`` routing table and the persona scripts all live there so
that no consumer supplies both sides of its own comparison.

This module is the reader every consumer should use, because it enforces the three things a
plain ``json.load`` cannot:

1. **D53 — an unmapped ``claim_type`` RAISES.** Never a silent default to a dimension.
   A claim type with no dimension has no route into trust at all; defaulting it would
   quietly park product-fact dishonesty on whichever dimension the default named. The
   failure mode is the entire value of calling the table "exhaustive".
2. **The six-dimension vocabulary.** Every ``dishonest_store.behaviours[].dim`` and every
   right-hand side of the mapping table must be one of the six dimensions DESIGN publishes.
3. **The digest chain.** ``golden_set.sha256`` must match the golden set's bytes and
   ``seed_catalog.sha256`` the category config's, and both references live inside the body
   that ``approval.content_hash`` covers. So approving the manifest transitively approves
   those files, and editing any of them afterwards breaks the chain loudly instead of
   silently re-grading the verifier against a different answer key.

Nothing here approves anything. See :mod:`fixtures.approval` for the human gate.
"""

from __future__ import annotations

__all__ = [
    "CATALOG_DIM",
    "GOLDEN_SET_PATH",
    "GOLDEN_SET_REL",
    "MANIFEST_PATH",
    "MANIFEST_REL",
    "OFFER_FACT_CLAIM_DIMENSIONS",
    "PRODUCT_FACT_CLAIM_TYPES",
    "PUBLISHED_CLAIM_TYPES",
    "TRANSACTION_DIMENSIONS",
    "TRUST_DIMENSIONS",
    "VERIFICATION_STATUSES",
    "DigestMismatchError",
    "ManifestError",
    "UnmappedClaimTypeError",
    "body_digest",
    "canonical_body",
    "claim_dimension",
    "file_digest",
    "golden_claim_types",
    "load_golden_set",
    "load_manifest",
    "refresh_digests",
    "validate_claim_types",
    "validate_manifest",
]

import hashlib
import json
import pathlib
from collections.abc import Iterable, Mapping
from typing import Any

from fixtures import REPO_ROOT

#: The one pinned manifest path. There is no search and no fallback: a second manifest would
#: make every reader's ground truth ambiguous.
MANIFEST_REL = "fixtures/manifest.json"
GOLDEN_SET_REL = "fixtures/golden/golden_set.json"
MANIFEST_PATH = REPO_ROOT / MANIFEST_REL
GOLDEN_SET_PATH = REPO_ROOT / GOLDEN_SET_REL

#: The sixth trust dimension: the one that grades a *product fact* rather than an
#: offer-integrity promise (D53).
CATALOG_DIM = "catalog_claim_accuracy"

#: The five dimensions that grade an offer-integrity promise.
TRANSACTION_DIMENSIONS = frozenset(
    {"price_honored", "discount_honored", "shipped_on_time", "not_returned", "feedback_match"}
)

#: The six trust dimensions. One trust system, six dimensions — not two systems (D53).
TRUST_DIMENSIONS = frozenset(TRANSACTION_DIMENSIONS | {CATALOG_DIM})

#: The four verification statuses R18 freezes.
VERIFICATION_STATUSES = frozenset({"verified", "contradicted", "unsupported", "ambiguous"})

#: The published offer-integrity half of the claim-type table (DESIGN, amended by D53).
OFFER_FACT_CLAIM_DIMENSIONS = {
    "price": "price_honored",
    "unit_price": "price_honored",
    "total_price": "price_honored",
    "discount": "discount_honored",
    "promo_eligibility": "discount_honored",
    "delivery": "shipped_on_time",
    "shipping_speed": "shipped_on_time",
    "dispatch_window": "shipped_on_time",
    "return_policy": "not_returned",
    "warranty": "not_returned",
}

#: The product-fact half — every one of these lands on ``catalog_claim_accuracy``.
PRODUCT_FACT_CLAIM_TYPES = ("ingredients", "compatibility", "nutrition", "specifications")

#: The published claim-type vocabulary the approved mapping must cover exhaustively.
PUBLISHED_CLAIM_TYPES = tuple(OFFER_FACT_CLAIM_DIMENSIONS) + PRODUCT_FACT_CLAIM_TYPES


class ManifestError(Exception):
    """The fixture manifest is not usable as ground truth."""


class UnmappedClaimTypeError(ManifestError):
    """A ``claim_type`` has no entry in ``claim_type_dimensions`` (D53).

    Raised rather than defaulted. A default would give the outcome a dimension nobody
    approved, and the point of an exhaustive table is that the gap is visible.
    """

    def __init__(self, claim_type: str, mapped: Iterable[str], where: str = "manifest") -> None:
        self.claim_type = claim_type
        self.mapped = sorted(mapped)
        super().__init__(
            f"{where}: claim_type {claim_type!r} is not in claim_type_dimensions, so its "
            f"verification outcome has no typed route into trust. Mapped types: {self.mapped}. "
            "An unmapped claim type raises; it never defaults to a dimension (D53)."
        )


class DigestMismatchError(ManifestError):
    """A referenced document's bytes do not match the digest the manifest records."""


# --- tiny structural helpers ---------------------------------------------------------
def _require(mapping: Any, key: str, kinds: type | tuple[type, ...], where: str) -> Any:
    if not isinstance(mapping, Mapping):
        raise ManifestError(f"{where} must be a JSON object, got {type(mapping).__name__}")
    if key not in mapping:
        raise ManifestError(f"{where} must declare {key!r}")
    value = mapping[key]
    if not isinstance(value, kinds) or isinstance(value, bool) and kinds is not bool:
        raise ManifestError(f"{where}.{key} must be {kinds}, got {type(value).__name__}")
    return value


def _nonempty_str(mapping: Any, key: str, where: str) -> str:
    value = _require(mapping, key, str, where)
    if not value.strip():
        raise ManifestError(f"{where}.{key} must not be empty")
    return value


def canonical_body(manifest: Mapping[str, Any]) -> str:
    """The exact bytes the approval digest is taken over: the manifest minus ``approval``.

    Frozen contract, reproduced identically in the acceptance suite::

        body = {k: v for k, v in manifest.items() if k != "approval"}
        json.dumps(body, sort_keys=True, separators=(",", ":"))
    """
    body = {k: v for k, v in manifest.items() if k != "approval"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def body_digest(manifest: Mapping[str, Any]) -> str:
    """sha256 of :func:`canonical_body` — what ``approval.content_hash`` must equal."""
    return hashlib.sha256(canonical_body(manifest).encode("utf-8")).hexdigest()


def file_digest(path: pathlib.Path) -> str:
    """sha256 of a referenced document's bytes, exactly as committed."""
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


# --- validation ----------------------------------------------------------------------
def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Structural validation with no file IO. Returns the manifest, or raises.

    Kept data-in/data-out on purpose: a doctored document can be handed straight to it,
    which is how the D53 guard is proved to refuse something rather than merely existing.
    """
    if not isinstance(manifest, Mapping):
        raise ManifestError("the fixture manifest must be a JSON object")

    _nonempty_str(manifest, "seed_category", "manifest")
    _require(manifest, "seed", int, "manifest")

    threshold = float(_require(manifest, "blacklist_threshold", (int, float), "manifest"))
    if not 0.0 < threshold < 1.0:
        raise ManifestError("manifest.blacklist_threshold must be a published score in (0,1)")

    budget = _require(manifest, "episode_budget", int, "manifest")
    if budget <= 0:
        raise ManifestError("manifest.episode_budget must be a positive number of episodes")

    prior_n = _require(manifest, "new_store_prior_n", int, "manifest")
    if prior_n <= 0:
        raise ManifestError("manifest.new_store_prior_n must be a positive episode count")

    table = _require(manifest, "claim_type_dimensions", dict, "manifest")
    if not table:
        raise ManifestError(
            "manifest.claim_type_dimensions must be a non-empty {claim_type: dimension} table"
        )
    for claim_type, dimension in table.items():
        if not isinstance(claim_type, str) or not claim_type.strip():
            raise ManifestError("claim_type_dimensions keys must be non-empty strings")
        if dimension not in TRUST_DIMENSIONS:
            raise ManifestError(
                f"manifest.claim_type_dimensions[{claim_type!r}] = {dimension!r} is not one of "
                f"the six trust dimensions {sorted(TRUST_DIMENSIONS)}"
            )
    missing = [t for t in PUBLISHED_CLAIM_TYPES if t not in table]
    if missing:
        raise UnmappedClaimTypeError(missing[0], table, "manifest.claim_type_dimensions")
    for claim_type in PRODUCT_FACT_CLAIM_TYPES:
        if table[claim_type] != CATALOG_DIM:
            raise ManifestError(
                f"product-fact claim type {claim_type!r} maps to {table[claim_type]!r}; a claim "
                f"about what the goods ARE belongs on {CATALOG_DIM!r} (D53)"
            )
    for claim_type, dimension in OFFER_FACT_CLAIM_DIMENSIONS.items():
        if table[claim_type] != dimension:
            raise ManifestError(
                f"offer-integrity claim type {claim_type!r} must keep its transaction dimension "
                f"{dimension!r}, got {table[claim_type]!r}"
            )

    treatment = _require(manifest, "claim_outcome_treatment", dict, "manifest")
    for status in sorted(VERIFICATION_STATUSES):
        if status not in treatment:
            raise ManifestError(
                f"manifest.claim_outcome_treatment must state the treatment of {status!r} "
                "(T-080 acceptance 4)"
            )

    store = _require(manifest, "dishonest_store", dict, "manifest")
    _nonempty_str(store, "store_id", "manifest.dishonest_store")
    behaviours = _require(store, "behaviours", list, "manifest.dishonest_store")
    if not behaviours:
        raise ManifestError("manifest.dishonest_store.behaviours must script a behaviour")
    scripted_dims = set()
    for i, behaviour in enumerate(behaviours):
        where = f"manifest.dishonest_store.behaviours[{i}]"
        _nonempty_str(behaviour, "kind", where)
        dim = _nonempty_str(behaviour, "dim", where)
        _nonempty_str(behaviour, "type", where)
        if dim not in TRUST_DIMENSIONS:
            raise ManifestError(
                f"{where}.dim must be one of the six trust dimensions "
                f"{sorted(TRUST_DIMENSIONS)}, got {dim!r}"
            )
        scripted_dims.add(dim)
    if CATALOG_DIM not in scripted_dims:
        raise ManifestError(
            f"no scripted dishonest behaviour lands on {CATALOG_DIM!r}; S2's dishonest store "
            "must lie about the goods, not only about the deal (D53)"
        )

    trajectory = _require(manifest, "expected_trust_trajectory", list, "manifest")
    episodes = []
    for i, point in enumerate(trajectory):
        where = f"manifest.expected_trust_trajectory[{i}]"
        episode = _require(point, "episode", int, where)
        score = float(_require(point, "score", (int, float), where))
        tolerance = float(_require(point, "tolerance", (int, float), where))
        if not 0 <= episode <= budget:
            raise ManifestError(f"{where}.episode must lie inside the episode budget")
        if not 0.0 <= score <= 1.0:
            raise ManifestError(f"{where}.score must be a trust score in [0,1]")
        if tolerance <= 0.0:
            raise ManifestError(f"{where}.tolerance must be a positive tolerance band")
        episodes.append(episode)
    if len(episodes) < 2 or episodes != sorted(set(episodes)):
        raise ManifestError(
            "manifest.expected_trust_trajectory must be at least two points at strictly "
            f"increasing episodes, got {episodes}"
        )
    first, last = trajectory[0], trajectory[-1]
    if float(first["score"]) <= threshold:
        raise ManifestError(
            "the dishonest store must START above the blacklist threshold — a trajectory that "
            "begins below it proves nothing about the trust engine"
        )
    if float(last["score"]) + float(last["tolerance"]) >= threshold:
        raise ManifestError(
            "S2: the expected trajectory must END below the blacklist threshold even at the "
            "top of its tolerance band, within the episode budget"
        )

    return dict(manifest)


def claim_dimension(manifest: Mapping[str, Any], claim_type: str) -> str:
    """The approved trust dimension for ``claim_type``. Raises if it is unmapped (D53)."""
    table = manifest.get("claim_type_dimensions") or {}
    if claim_type not in table:
        raise UnmappedClaimTypeError(claim_type, table)
    return str(table[claim_type])


def validate_claim_types(manifest: Mapping[str, Any], claim_types: Iterable[str]) -> None:
    """Every claim type in ``claim_types`` must have a route into trust, or raise."""
    for claim_type in claim_types:
        claim_dimension(manifest, claim_type)


def golden_claim_types(golden: Mapping[str, Any]) -> list[str]:
    """Every ``claim_type`` the golden set uses, in document order (duplicates kept)."""
    out: list[str] = []
    for pitch in golden.get("pitches") or []:
        for claim in pitch.get("claims") or []:
            out.append(str(claim.get("claim_type")))
    return out


# --- loading -------------------------------------------------------------------------
def _read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"missing ground-truth document {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path} is not readable JSON: {exc}") from exc


def load_golden_set(manifest: Mapping[str, Any], *, check_digest: bool = True) -> dict[str, Any]:
    """The approved golden set, loaded *through* the manifest's recorded digest."""
    ref = _require(manifest, "golden_set", dict, "manifest")
    rel = _nonempty_str(ref, "path", "manifest.golden_set")
    if rel != GOLDEN_SET_REL:
        raise ManifestError(f"manifest.golden_set.path is pinned to {GOLDEN_SET_REL}, got {rel!r}")
    path = REPO_ROOT / rel
    if check_digest:
        recorded = _nonempty_str(ref, "sha256", "manifest.golden_set").strip().lower()
        actual = file_digest(path)
        if actual != recorded:
            raise DigestMismatchError(
                f"manifest.golden_set.sha256 does not match {rel}: the approved manifest does "
                f"not cover the golden set as committed (recorded {recorded}, file {actual})"
            )
    doc = _read_json(path)
    pitches = _require(doc, "pitches", list, rel)
    if not pitches:
        raise ManifestError(f"{rel}.pitches must contain the golden intents/pitches")
    count = ref.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and count != len(pitches):
        raise ManifestError(
            f"manifest.golden_set.count says {count} but {rel} holds {len(pitches)} pitches"
        )
    return doc


def load_manifest(
    path: pathlib.Path | str | None = None, *, check_digests: bool = True
) -> dict[str, Any]:
    """Read, validate and return the human-approved fixture manifest.

    Raises :class:`UnmappedClaimTypeError` when the golden set uses a ``claim_type`` the
    approved table does not map, :class:`DigestMismatchError` when a referenced document has
    drifted from the digest recorded for it, and :class:`ManifestError` for every other way
    the document fails to be usable ground truth.
    """
    manifest = _read_json(pathlib.Path(path) if path is not None else MANIFEST_PATH)
    validate_manifest(manifest)

    if check_digests:
        catalog_ref = manifest.get("seed_catalog")
        if isinstance(catalog_ref, Mapping):
            rel = _nonempty_str(catalog_ref, "path", "manifest.seed_catalog")
            recorded = _nonempty_str(catalog_ref, "sha256", "manifest.seed_catalog").lower()
            actual = file_digest(REPO_ROOT / rel)
            if actual != recorded:
                raise DigestMismatchError(
                    f"manifest.seed_catalog.sha256 does not match {rel} (recorded {recorded}, "
                    f"file {actual}) — the approved manifest does not cover the category "
                    "config as committed"
                )

    golden = load_golden_set(manifest, check_digest=check_digests)
    validate_claim_types(manifest, golden_claim_types(golden))
    return manifest


# --- maintenance ---------------------------------------------------------------------
def refresh_digests(path: pathlib.Path | str | None = None) -> dict[str, str]:
    """Recompute every digest the manifest records and rewrite it in place.

    Recomputed: ``seed_catalog.sha256``, ``golden_set.sha256``, ``golden_set.count`` and
    ``approval.content_hash``. This is a *bookkeeping* operation — it records what the
    documents currently say. It approves nothing: ``approval.approver`` is never written
    here (see :mod:`fixtures.approval`, which a human runs).
    """
    target = pathlib.Path(path) if path is not None else MANIFEST_PATH
    manifest = _read_json(target)

    catalog_ref = manifest.get("seed_catalog")
    if isinstance(catalog_ref, dict):
        catalog_ref["sha256"] = file_digest(REPO_ROOT / catalog_ref["path"])

    golden_ref = manifest["golden_set"]
    golden_path = REPO_ROOT / golden_ref["path"]
    golden_ref["sha256"] = file_digest(golden_path)
    golden_ref["count"] = len(_read_json(golden_path)["pitches"])

    digest = body_digest(manifest)
    manifest.setdefault("approval", {})["content_hash"] = digest

    target.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "content_hash": digest,
        "golden_set_sha256": golden_ref["sha256"],
        "seed_catalog_sha256": (catalog_ref or {}).get("sha256", ""),
    }
