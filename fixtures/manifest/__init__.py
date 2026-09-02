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
3. **The digest chain.** ``golden_set.sha256`` must match the golden set's bytes,
   ``seed_catalog.sha256`` the category config's, and ``approval.content_hash`` the
   manifest's *own* body. The first two references live inside the body the third covers,
   so approving the manifest transitively approves those files, and editing any of the
   three afterwards breaks the chain loudly instead of silently re-grading the verifier
   against a different answer key. All three are checked by :func:`load_manifest`; a
   digest nobody verifies at load time is a digest that evidences nothing.

Two boundaries this module is careful about
-------------------------------------------
**Re-pinning is bookkeeping, and it does not touch the approval block.** It is tempting to
read :func:`refresh_digests` as defeating the tamper evidence, since it re-pins
``approval.content_hash`` while ``approver`` / ``approved_at`` / ``artifact`` stay populated.
Staged against the frozen acceptance suite, that reading does not hold: the approval lives in
*two* documents that must agree, and re-pinning rewrites only this one, so the record the
manifest names no longer quotes the digest and the acceptance test goes red — as does
:func:`fixtures.approval.verify_pinned_digests`. See :func:`refresh_digests` for the measured
detail. The block is therefore left exactly as the human left it.

**A manifest's references resolve against its own root.** ``golden_set.path`` and
``seed_catalog.path`` are repo-relative, so they are resolved against the root that holds
*that manifest* (the directory ``<root>/fixtures/manifest.json`` hangs off), never against
this checkout. Resolving a foreign document's references against this repo's files would
validate somebody else's manifest — a fabricated ``approval.status: approved`` included —
against this repo's approved golden set.

Nothing here approves anything. See :mod:`fixtures.approval` for the human gate.
"""

from __future__ import annotations

__all__ = [
    "APPROVAL_STATUSES",
    "CATALOG_DIM",
    "GOLDEN_SET_PATH",
    "GOLDEN_SET_REL",
    "MANIFEST_PATH",
    "MANIFEST_REL",
    "OBSERVATION_KINDS",
    "OFFER_FACT_CLAIM_DIMENSIONS",
    "PENDING_APPROVAL",
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
    "manifest_root",
    "refresh_digests",
    "validate_claim_types",
    "validate_manifest",
]

import hashlib
import json
import pathlib
import re
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

#: The three ways an outcome can act on a dimension. ``none`` is not "unknown": it is the
#: published treatment of ``ambiguous``, which moves no mean at all (D53, R19).
OBSERVATION_KINDS = frozenset({"positive", "negative", "none"})

#: The status of a manifest nobody has approved.
PENDING_APPROVAL = "pending_human_approval"

#: The only two states ``approval.status`` may hold. A third spelling would let a manifest
#: describe itself as something no reader knows how to grade.
APPROVAL_STATUSES = frozenset({PENDING_APPROVAL, "approved"})

#: The three fields that, together, constitute a recorded human decision. Either all three
#: are present (``approved``) or none of them are (``pending_human_approval``); a manifest
#: carrying some of them is a half-fabricated approval and is refused.
APPROVAL_DECISION_FIELDS = ("approver", "approved_at", "artifact")

_SHA256 = re.compile(r"[0-9a-f]{64}")


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


#: ``("fixtures", "manifest.json")`` — the tail every manifest path must end with.
_MANIFEST_TAIL = tuple(MANIFEST_REL.split("/"))


def manifest_root(path: pathlib.Path | str) -> pathlib.Path:
    """The root a manifest's repo-relative references resolve against.

    A manifest records its covered documents as repo-relative paths
    (``fixtures/golden/golden_set.json``), so they are meaningful only underneath the root
    that holds *this* manifest — never underneath whichever checkout happens to be running
    the code. Resolving them against :data:`fixtures.REPO_ROOT` instead is a confusion of
    trust boundaries with teeth: ``load_manifest('/tmp/x/fixtures/manifest.json')`` would
    then validate a foreign document — with whatever ``approval`` block it chose to
    fabricate — against *this* repository's approved golden set and category config, and
    report it as sound ground truth.

    A manifest that does not sit at ``<root>/fixtures/manifest.json`` has no root to
    resolve against and is refused rather than guessed at.
    """
    resolved = pathlib.Path(path).expanduser().resolve()
    if resolved.parts[-len(_MANIFEST_TAIL) :] != _MANIFEST_TAIL:
        raise ManifestError(
            f"{resolved} is not a fixture manifest path: a manifest's references are "
            f"repo-relative, so it must live at <root>/{MANIFEST_REL} for them to resolve"
        )
    return resolved.parents[len(_MANIFEST_TAIL) - 1]


# --- validation ----------------------------------------------------------------------
def _number(mapping: Any, key: str, where: str) -> float:
    return float(_require(mapping, key, (int, float), where))


def _validate_observation_weights(manifest: Mapping[str, Any]) -> dict[str, float]:
    """Every published observation kind carries a numeric weight.

    Returned so the behaviour check can insist that a scripted ``type`` is one of them: an
    observation kind with no published weight has no defined effect on trust at all, which
    is the same silent-default failure D53 forbids for claim types.
    """
    weights = _require(manifest, "observation_weights", dict, "manifest")
    out: dict[str, float] = {}
    for key, value in weights.items():
        if key == "comment":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ManifestError(
                f"manifest.observation_weights[{key!r}] must be a published numeric weight, "
                f"got {type(value).__name__}"
            )
        if value < 0:
            raise ManifestError(f"manifest.observation_weights[{key!r}] must not be negative")
        out[key] = float(value)
    if not out:
        raise ManifestError("manifest.observation_weights must publish at least one weight")
    return out


def _validate_claim_outcome_treatment(manifest: Mapping[str, Any]) -> None:
    """The published treatment of each verification outcome — checked, not merely present.

    Key presence alone said nothing: ``{"ambiguous": null}`` satisfied it, and T-080
    acceptance 4 is about what each outcome *does* to trust. The rules asserted here are the
    ones DESIGN and D53/R19 publish, and they are what the trust engine (T-062) is graded
    against, so a manifest that contradicts them is not usable ground truth.
    """
    treatment = _require(manifest, "claim_outcome_treatment", dict, "manifest")
    for status in sorted(VERIFICATION_STATUSES):
        if status not in treatment:
            raise ManifestError(
                f"manifest.claim_outcome_treatment must state the treatment of {status!r} "
                "(T-080 acceptance 4)"
            )
    unknown = sorted(set(treatment) - VERIFICATION_STATUSES)
    if unknown:
        raise ManifestError(
            f"manifest.claim_outcome_treatment describes {unknown}, which are not "
            f"verification statuses {sorted(VERIFICATION_STATUSES)}"
        )

    for status in sorted(VERIFICATION_STATUSES):
        where = f"manifest.claim_outcome_treatment[{status!r}]"
        entry = _require(treatment, status, dict, "manifest.claim_outcome_treatment")
        observation = _nonempty_str(entry, "observation", where)
        if observation not in OBSERVATION_KINDS:
            raise ManifestError(
                f"{where}.observation must be one of {sorted(OBSERVATION_KINDS)}, "
                f"got {observation!r}"
            )
        weight = _number(entry, "weight", where)
        if weight < 0:
            raise ManifestError(f"{where}.weight must not be negative")
        for flag in ("moves_mean", "counts_as_evidence", "satisfies_hard_constraint"):
            _require(entry, flag, bool, where)

    verified = treatment["verified"]
    contradicted = treatment["contradicted"]
    unsupported = treatment["unsupported"]
    ambiguous = treatment["ambiguous"]

    if verified["observation"] != "positive" or not verified["moves_mean"]:
        raise ManifestError(
            "claim_outcome_treatment.verified must be a positive observation that moves the "
            "mean — an outcome that changes nothing is not evidence"
        )
    if contradicted["observation"] != "negative" or not contradicted["moves_mean"]:
        raise ManifestError(
            "claim_outcome_treatment.contradicted must be a negative observation that moves "
            "the mean; it is the outcome the whole S2 proof rests on"
        )
    if float(contradicted["weight"]) <= 0:
        raise ManifestError("claim_outcome_treatment.contradicted.weight must be positive")
    if not 0.0 < float(unsupported["weight"]) < float(contradicted["weight"]):
        raise ManifestError(
            "claim_outcome_treatment.unsupported.weight must be a published fraction "
            "STRICTLY between 0 and contradicted's weight "
            f"({unsupported['weight']} vs {contradicted['weight']}) — D53/R19"
        )
    if ambiguous["observation"] != "none" or float(ambiguous["weight"]) != 0.0:
        raise ManifestError(
            "claim_outcome_treatment.ambiguous must take NO observation at weight 0.0 "
            "(D53: no mean movement whatsoever)"
        )
    if ambiguous["moves_mean"] or ambiguous["counts_as_evidence"]:
        raise ManifestError(
            "claim_outcome_treatment.ambiguous must neither move the mean nor count as "
            "evidence; it lowers coverage and confidence only (D53, R19)"
        )
    for status in ("contradicted", "unsupported", "ambiguous"):
        if treatment[status]["satisfies_hard_constraint"]:
            raise ManifestError(
                f"claim_outcome_treatment.{status}.satisfies_hard_constraint must be false: "
                "only a VERIFIED claim can satisfy a buyer's hard constraint (R19)"
            )
    if not verified["satisfies_hard_constraint"]:
        raise ManifestError(
            "claim_outcome_treatment.verified.satisfies_hard_constraint must be true, or no "
            "outcome can ever satisfy a hard constraint and R19 is unsatisfiable"
        )


def _validate_stores(manifest: Mapping[str, Any], dishonest: Mapping[str, Any]) -> list[str]:
    """The store roster, which the generator seeds and the simulator plays. Returns its ids.

    S2 needs both halves: the scripted liar must be *on* the roster and flagged dishonest,
    and at least one honest store must exist — otherwise "the trust engine catches the
    dishonest store" is satisfied by a scorer that sinks everybody.
    """
    stores = _require(manifest, "stores", list, "manifest")
    if not stores:
        raise ManifestError("manifest.stores must list the store roster the demo seeds")

    ids: list[str] = []
    identities: dict[str, str] = {}
    honest_flags: dict[str, bool] = {}
    for i, store in enumerate(stores):
        where = f"manifest.stores[{i}]"
        store_id = _nonempty_str(store, "store_id", where)
        identities[store_id] = _nonempty_str(store, "business_identity", where)
        _nonempty_str(store, "display_name", where)
        _nonempty_str(store, "domain", where)
        honest_flags[store_id] = _require(store, "honest", bool, where)
        ids.append(store_id)

    duplicates = sorted({s for s in ids if ids.count(s) > 1})
    if duplicates:
        raise ManifestError(f"manifest.stores repeats store_id(s) {duplicates}")

    dishonest_id = str(dishonest["store_id"])
    if dishonest_id not in ids:
        raise ManifestError(
            f"manifest.dishonest_store.store_id {dishonest_id!r} is not on manifest.stores "
            f"{ids} — the scripted liar must be a store the demo actually seeds"
        )
    if honest_flags[dishonest_id]:
        raise ManifestError(
            f"manifest.stores entry for the scripted dishonest store {dishonest_id!r} is "
            "flagged honest: true"
        )
    declared_identity = dishonest.get("business_identity")
    if declared_identity is not None and declared_identity != identities[dishonest_id]:
        raise ManifestError(
            f"manifest.dishonest_store.business_identity {declared_identity!r} disagrees with "
            f"the roster's {identities[dishonest_id]!r} for {dishonest_id!r}"
        )
    if not any(honest_flags[s] for s in ids):
        raise ManifestError(
            "manifest.stores contains no honest store. S2 needs a control: without one, "
            "'the trust engine catches the dishonest store' is satisfied by a scorer that "
            "sinks every store it sees"
        )
    return ids


def _validate_personas(
    manifest: Mapping[str, Any], store_ids: list[str], table: Mapping[str, Any], dishonest_id: str
) -> None:
    """The persona scripts T-045 replays. Ground truth, so they are checked like ground truth."""
    personas = _require(manifest, "personas", dict, "manifest")
    if "aggressive" not in personas:
        raise ManifestError(
            "manifest.personas must script the 'aggressive' adversarial persona (T-045)"
        )
    all_truthful: list[str] = []
    for name, persona in personas.items():
        where = f"manifest.personas[{name!r}]"
        if not isinstance(name, str) or not name.strip():
            raise ManifestError("manifest.personas keys must be non-empty persona names")
        store_id = _nonempty_str(persona, "store_id", where)
        if store_id not in store_ids:
            raise ManifestError(f"{where}.store_id {store_id!r} is not on the roster {store_ids}")
        claims = _require(persona, "scripted_claims", list, where)
        if not claims:
            raise ManifestError(f"{where}.scripted_claims must script at least one claim")
        truths: list[bool] = []
        for j, claim in enumerate(claims):
            cwhere = f"{where}.scripted_claims[{j}]"
            _nonempty_str(claim, "key", cwhere)
            _nonempty_str(claim, "value", cwhere)
            claim_type = _nonempty_str(claim, "claim_type", cwhere)
            if claim_type not in table:
                raise UnmappedClaimTypeError(claim_type, table, cwhere)
            truths.append(_require(claim, "truthful", bool, cwhere))
        if all(truths):
            all_truthful.append(name)

    aggressive = personas["aggressive"]
    if aggressive["store_id"] != dishonest_id:
        raise ManifestError(
            f"manifest.personas['aggressive'].store_id is {aggressive['store_id']!r}; the "
            f"adversarial persona speaks for the scripted dishonest store {dishonest_id!r}"
        )
    if all(c["truthful"] for c in aggressive["scripted_claims"]):
        raise ManifestError(
            "manifest.personas['aggressive'] asserts nothing false, so nothing it says can "
            "ever be caught — the adversarial persona must actually lie"
        )
    if not all_truthful:
        raise ManifestError(
            "no persona scripts an entirely truthful answer to the fixture intent. The "
            "honest control is what stops S2 being satisfied by a scorer that sinks everybody"
        )


def _validate_fixture_intent(manifest: Mapping[str, Any]) -> None:
    """The one intent every persona answers and every golden pitch is pitched against."""
    where = "manifest.fixture_intent"
    intent = _require(manifest, "fixture_intent", dict, "manifest")
    _nonempty_str(intent, "intent_id", where)
    _nonempty_str(intent, "text", where)
    category = _nonempty_str(intent, "category", where)
    if category != manifest["seed_category"]:
        raise ManifestError(
            f"{where}.category is {category!r} but manifest.seed_category is "
            f"{manifest['seed_category']!r}; the demo would shop a catalog it never seeded"
        )
    budget = _require(intent, "budget_cents", int, where)
    if budget <= 0:
        raise ManifestError(f"{where}.budget_cents must be a positive amount in cents")
    ship_to = _require(intent, "ship_to", dict, where)
    _nonempty_str(ship_to, "country", f"{where}.ship_to")
    _nonempty_str(ship_to, "postal_code", f"{where}.ship_to")

    constraints = _require(intent, "constraints", list, where)
    hard = 0
    for i, constraint in enumerate(constraints):
        cwhere = f"{where}.constraints[{i}]"
        _nonempty_str(constraint, "key", cwhere)
        _nonempty_str(constraint, "op", cwhere)
        if "value" not in constraint:
            raise ManifestError(f"{cwhere} must declare 'value'")
        hard += bool(_require(constraint, "hard", bool, cwhere))
    if not hard:
        raise ManifestError(
            f"{where}.constraints declares no HARD constraint, so R19 — 'an unsupported or "
            "ambiguous claim never satisfies a hard constraint' — is never exercised"
        )

    for i, preference in enumerate(_require(intent, "preferences", list, where)):
        pwhere = f"{where}.preferences[{i}]"
        _nonempty_str(preference, "key", pwhere)
        if "value" not in preference:
            raise ManifestError(f"{pwhere} must declare 'value'")
        weight = _number(preference, "weight", pwhere)
        if not 0.0 < weight <= 1.0:
            raise ManifestError(f"{pwhere}.weight must lie in (0,1], got {weight}")


def _validate_approval_block(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    """The approval block is coherent — all of a decision, or none of it.

    This says nothing about whether a human approved anything; no offline check can. What it
    refuses is the *half*-fabricated state — an approver written into a manifest whose status
    is still pending, or a status of ``approved`` with nobody's name on it. Both are how a
    fabricated approval actually looks when it is assembled field by field.
    """
    approval = _require(manifest, "approval", dict, "manifest")
    status = _nonempty_str(approval, "status", "manifest.approval")
    if status not in APPROVAL_STATUSES:
        raise ManifestError(
            f"manifest.approval.status must be one of {sorted(APPROVAL_STATUSES)}, got {status!r}"
        )
    present = [f for f in APPROVAL_DECISION_FIELDS if approval.get(f) is not None]
    if status == "approved":
        missing = [f for f in APPROVAL_DECISION_FIELDS if f not in present]
        if missing:
            raise ManifestError(
                f"manifest.approval.status is 'approved' but {missing} are empty — an "
                "approval with no approver, no instant or no committed record is not one"
            )
        for field in APPROVAL_DECISION_FIELDS:
            _nonempty_str(approval, field, "manifest.approval")
    elif present:
        raise ManifestError(
            f"manifest.approval.status is {PENDING_APPROVAL!r} but {present} are filled in. "
            "A half-written approval is how a fabricated one looks; T-080 is a HUMAN gate "
            "(EXECUTION.md rule 6)"
        )
    return approval


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

    _validate_claim_outcome_treatment(manifest)
    weights = _validate_observation_weights(manifest)

    store = _require(manifest, "dishonest_store", dict, "manifest")
    dishonest_id = _nonempty_str(store, "store_id", "manifest.dishonest_store")
    behaviours = _require(store, "behaviours", list, "manifest.dishonest_store")
    if not behaviours:
        raise ManifestError("manifest.dishonest_store.behaviours must script a behaviour")
    scripted_dims = set()
    kinds: list[str] = []
    for i, behaviour in enumerate(behaviours):
        where = f"manifest.dishonest_store.behaviours[{i}]"
        kinds.append(_nonempty_str(behaviour, "kind", where))
        dim = _nonempty_str(behaviour, "dim", where)
        observation = _nonempty_str(behaviour, "type", where)
        if dim not in TRUST_DIMENSIONS:
            raise ManifestError(
                f"{where}.dim must be one of the six trust dimensions "
                f"{sorted(TRUST_DIMENSIONS)}, got {dim!r}"
            )
        if observation not in weights:
            raise ManifestError(
                f"{where}.type {observation!r} has no published weight in "
                f"manifest.observation_weights {sorted(weights)}, so the behaviour has no "
                "defined effect on trust at all"
            )
        # D53 on the SCRIPTED side. `claim_type` was carried but never checked, so a
        # behaviour could name a claim type that routes to a different dimension than the
        # one it says it lands on — and the simulator and the trust engine would then
        # disagree about the same scripted lie while both read "the manifest".
        if "claim_type" not in behaviour:
            raise ManifestError(
                f"{where} must declare 'claim_type' (null only for the buyer-reported "
                "feedback_match behaviour, which takes no verification outcome)"
            )
        claim_type = behaviour["claim_type"]
        if claim_type is None:
            if dim != "feedback_match":
                raise ManifestError(
                    f"{where}.claim_type is null but the behaviour lands on {dim!r}. Only "
                    "feedback_match takes no verification outcome — it is the buyer-reported "
                    "pitch/delivery match (R14, D53); every other dimension is reached by a "
                    "typed claim"
                )
        else:
            if not isinstance(claim_type, str) or not claim_type.strip():
                raise ManifestError(f"{where}.claim_type must be a claim type or null")
            if claim_type not in table:
                raise UnmappedClaimTypeError(claim_type, table, where)
            if table[claim_type] != dim:
                raise ManifestError(
                    f"{where} scripts claim_type {claim_type!r} against dimension {dim!r}, "
                    f"but the approved table routes it to {table[claim_type]!r}. A scripted "
                    "lie must land on the dimension its claim type routes to (D53)"
                )
            if dim == "feedback_match":
                raise ManifestError(
                    f"{where} routes a typed claim onto 'feedback_match', which takes no "
                    "verification outcome: that would score catalog dishonesty as a buyer "
                    "complaint that never happened (R14, D53)"
                )
        scripted_dims.add(dim)
    duplicate_kinds = sorted({k for k in kinds if kinds.count(k) > 1})
    if duplicate_kinds:
        raise ManifestError(
            f"manifest.dishonest_store.behaviours repeats kind(s) {duplicate_kinds}; T-081's "
            "script is graded against this sequence element for element"
        )
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

    store_ids = _validate_stores(manifest, store)
    _validate_personas(manifest, store_ids, table, dishonest_id)
    _validate_fixture_intent(manifest)
    _validate_approval_block(manifest)

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


def load_golden_set(
    manifest: Mapping[str, Any],
    *,
    check_digest: bool = True,
    root: pathlib.Path | str | None = None,
) -> dict[str, Any]:
    """The approved golden set, loaded *through* the manifest's recorded digest.

    ``root`` is the tree the manifest's repo-relative reference resolves against; it
    defaults to this checkout. :func:`load_manifest` passes the root of the manifest it read
    so that a manifest from elsewhere is never graded against this repository's answer key.
    """
    ref = _require(manifest, "golden_set", dict, "manifest")
    rel = _nonempty_str(ref, "path", "manifest.golden_set")
    if rel != GOLDEN_SET_REL:
        raise ManifestError(f"manifest.golden_set.path is pinned to {GOLDEN_SET_REL}, got {rel!r}")
    base = pathlib.Path(root) if root is not None else REPO_ROOT
    path = base / rel
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
    drifted from the digest recorded for it — including the manifest's **own body**, which
    ``approval.content_hash`` covers — and :class:`ManifestError` for every other way the
    document fails to be usable ground truth.

    Referenced documents resolve against the root of the manifest that was read, never
    against this checkout: see :func:`manifest_root`.
    """
    target = pathlib.Path(path) if path is not None else MANIFEST_PATH
    root = manifest_root(target)
    manifest = _read_json(target)
    validate_manifest(manifest)

    if check_digests:
        # The manifest's own body first. `golden_set.sha256` and `seed_catalog.sha256` live
        # INSIDE that body, so this is the digest the other two hang off — and it was the
        # one no runtime consumer verified: the acceptance suite checks it, but every
        # program that loaded the manifest trusted an `approval.content_hash` that no code
        # path on its side had compared against the document it claims to cover. This is
        # defence in depth, not a hole being closed: it moves the same evidence from a
        # once-a-suite assertion to every read, so a consumer running outside the suite
        # cannot proceed on a manifest whose body has drifted from its recorded digest.
        approval = _require(manifest, "approval", dict, "manifest")
        recorded_body = _nonempty_str(approval, "content_hash", "manifest.approval").strip().lower()
        if not _SHA256.fullmatch(recorded_body):
            raise ManifestError(
                "manifest.approval.content_hash must be the sha256 hex digest of the manifest "
                f"body, got {recorded_body!r}"
            )
        actual_body = body_digest(manifest)
        if actual_body != recorded_body:
            raise DigestMismatchError(
                "manifest.approval.content_hash does not match the manifest body it covers "
                f"(recorded {recorded_body}, body {actual_body}). The manifest was edited "
                "after its digest was pinned. Either restore it, or re-pin deliberately with "
                "`python -m fixtures.manifest --refresh-digests` and have a human approve the "
                "re-issued request — an approval recorded against the old digest no longer "
                "covers this document."
            )

        catalog_ref = manifest.get("seed_catalog")
        if isinstance(catalog_ref, Mapping):
            rel = _nonempty_str(catalog_ref, "path", "manifest.seed_catalog")
            recorded = _nonempty_str(catalog_ref, "sha256", "manifest.seed_catalog").lower()
            actual = file_digest(root / rel)
            if actual != recorded:
                raise DigestMismatchError(
                    f"manifest.seed_catalog.sha256 does not match {rel} (recorded {recorded}, "
                    f"file {actual}) — the approved manifest does not cover the category "
                    "config as committed"
                )

    golden = load_golden_set(manifest, check_digest=check_digests, root=root)
    validate_claim_types(manifest, golden_claim_types(golden))
    return manifest


# --- maintenance ---------------------------------------------------------------------
def refresh_digests(path: pathlib.Path | str | None = None) -> dict[str, str]:
    """Recompute every digest the manifest records and rewrite it in place.

    Recomputed: ``seed_catalog.sha256``, ``golden_set.sha256``, ``golden_set.count`` and
    ``approval.content_hash``. This is a *bookkeeping* operation — it records what the
    documents currently say. It approves nothing: ``approval.approver`` is never written
    here (see :mod:`fixtures.approval`, which a human runs).

    **It deliberately does NOT clear the approval block.** The obvious-looking reading — that
    re-pinning ``content_hash`` while ``approver`` / ``approved_at`` / ``artifact`` stay
    populated silently re-seals an approval over edited content — was investigated against
    the frozen suite on staged trees, and it is wrong. The approval is recorded in *two*
    documents that must agree: this block, and the committed human-authored record it names.
    This function rewrites only ``fixtures/manifest.json``, so the re-pinned digest is by
    construction absent from that record, and
    ``.swarm-loop/acceptance/test_e8_proofs.py`` turns RED on "the approval record and the
    manifest do not describe the same document" — including after the strongest
    agent-runnable follow-up, re-issuing the request, which rewrites the request and never
    the record. :func:`fixtures.approval.verify_pinned_digests` refuses independently, naming
    the drifted file. The only route back to green is a fresh
    :func:`fixtures.approval.record_approval`, which demands a non-automation human name.
    Clearing the block here would substitute a weaker guard for one that already fires.

    Referenced documents resolve against the root of the manifest being re-pinned, never
    against this checkout (:func:`manifest_root`).
    """
    target = pathlib.Path(path) if path is not None else MANIFEST_PATH
    root = manifest_root(target)
    manifest = _read_json(target)

    catalog_ref = manifest.get("seed_catalog")
    if isinstance(catalog_ref, dict):
        catalog_ref["sha256"] = file_digest(root / catalog_ref["path"])

    golden_ref = manifest["golden_set"]
    golden_path = root / golden_ref["path"]
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
