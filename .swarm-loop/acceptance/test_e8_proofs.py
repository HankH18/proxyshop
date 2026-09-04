"""Epic E8 — Fixtures & proofs: the human-approved ground truth everything else is graded against.

Covers the SPEC surface that E8 owns:

* **S2 / A3** — the dishonest-store behaviours, the episode budget, the new-store prior N
  and the expected trust trajectory live in a *human-approved fixture manifest*, not in the
  trust engine's own config. A3 is the whole reason this epic exists: "the trust engine
  catches the dishonest store" is circular unless the dishonesty is defined elsewhere and
  approved by a human. Tests 1 and 2 are the machine-checkable half of that human gate.
* **S8 / R18** — the golden pitch set labels claims with all four verification statuses and
  carries a case for every DESIGN §Verification eval gate. `packages.verification` (T-065)
  is *graded against* this set and never authors it (D11).
* **C9 / S4** — the seed generator is pure and deterministic under a fixed seed, and
  applying its output twice is a no-op the second time (`make demo-seed` idempotence,
  T-080 acceptance 3, tested at the pure-function surface because the frozen suite may not
  start the shopify-stub service).
* **S2** — the simulator's dishonest script emits exactly the manifest's behaviour
  sequence, element for element. The manifest is ground truth; the script is graded.
* **S6 / S1** — the demo runbook declares its required sections, every `make` target it
  references is really defined, and the interview beat references the T-053 transcript flow
  with the live-LLM variant flagged.

**Ground-truth direction is load-bearing in this file.** Every assertion that mentions the
manifest reads the manifest itself and compares the *product* to it — never the reverse.
A test that let the product supply both sides would restate A3's circularity.

**The frozen T-080 schema** (identical in `test_e6_trust.py`; see that file's header for the
same table). Exactly two documents, at exactly these paths, no globbing and no second copy:

* ``fixtures/manifest.json`` — one object with ``seed_category``, ``seed``,
  ``blacklist_threshold``, ``episode_budget``, ``new_store_prior_n``,
  ``claim_type_dimensions {claim_type: dimension}``,
  ``dishonest_store {store_id, behaviours[{kind, dim, type}]}``,
  ``expected_trust_trajectory[{episode, score, tolerance}]``,
  ``golden_set {path, sha256, count}`` and ``approval {approver, approved_at, artifact,
  content_hash}``.
* ``fixtures/golden/golden_set.json`` — one object with ``pitches[]``, each
  ``{pitch_id, text, gates[], catalog_snapshot, claims[{claim_ref, text, key, claim_type,
  value, expected_status}]}``.

``claim_type_dimensions`` is the **typed, exhaustive** `claim_type → trust dimension` table
(D18 as amended): product-fact claim types land on ``catalog_claim_accuracy``, the sixth
trust dimension, and offer-integrity claim types keep their transaction dimension. It lives
in the approved manifest, not in the trust engine's config, for the same reason the
dishonest script does — otherwise the engine grades itself.

The golden label key is ``expected_status`` everywhere — never ``status``, which is what the
*verifier* produces and must stay visibly distinct from what the human approved.

Authoring rules (see `.swarm-loop/acceptance/README.md`): every product import happens
INSIDE the test body, and every test carries `epic` + `ticket` markers. Module scope holds
stdlib and pytest only.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"

#: T-080's human-approved ground truth lives at exactly these two paths. One manifest, one
#: golden set, no globbing, no second copy. `test_e6_trust.py` reads the identical paths and
#: the identical key spellings; the two files are one schema.
MANIFEST_REL = "fixtures/manifest.json"
GOLDEN_SET_REL = "fixtures/golden/golden_set.json"

#: The four verification statuses R18 freezes. Nothing else may appear as a label.
VERIFICATION_STATUSES = frozenset({"verified", "contradicted", "unsupported", "ambiguous"})

#: The sixth trust dimension: the one that grades a *product fact* (is the thing what the
#: pitch said it is) rather than an offer-integrity promise. A false ingredient claim is not
#: `price_honored`, `shipped_on_time` or `not_returned`, and mapping it into one of those
#: corrupts the meaning of that dimension's score.
CATALOG_DIM = "catalog_claim_accuracy"

#: The five dimensions that grade an offer-integrity promise.
TRANSACTION_DIMENSIONS = frozenset(
    {"price_honored", "discount_honored", "shipped_on_time", "not_returned", "feedback_match"}
)

#: The six trust dimensions every manifest behaviour must name (DESIGN §Trust math). One
#: trust system, six dimensions — not two systems.
TRUST_DIMENSIONS = TRANSACTION_DIMENSIONS | {CATALOG_DIM}

#: The published `claim_type → dimension` table, offer-fact half.
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

#: The product-fact half — every one of these lands on `catalog_claim_accuracy`.
PRODUCT_FACT_CLAIM_TYPES = ("ingredients", "compatibility", "nutrition", "specifications")

#: The published claim-type vocabulary the approved mapping must cover exhaustively.
PUBLISHED_CLAIM_TYPES = tuple(OFFER_FACT_CLAIM_DIMENSIONS) + PRODUCT_FACT_CLAIM_TYPES

#: The eleven eval gates DESIGN §Verification strategy enumerates. The key is the canonical
#: gate id the golden set should use; the value set lists the spellings this suite accepts,
#: so a reasonable synonym is not a permanently unreachable goal — but a MISSING gate is.
EVAL_GATES = {
    "contradiction": {"contradiction", "contradictions", "contradicted"},
    "stale_evidence": {
        "stale_evidence",
        "stale_or_absent_evidence",
        "stale_absent_evidence",
        "absent_evidence",
    },
    "wrong_variant": {"wrong_variant", "variant_mismatch"},
    "wrong_units": {"wrong_units", "wrong_unit", "unit_mismatch"},
    "injection": {"injection", "prompt_injection"},
    "claim_splitting": {"claim_splitting", "claim_split"},
    "verbosity_gaming": {"verbosity_gaming", "verbosity"},
    "timeout": {"timeout", "timeouts"},
    "duplicate_pitch": {"duplicate_pitch", "duplicate_pitches", "duplicate"},
    "expired_offer": {"expired_offer", "expired_offers"},
    "blacklist_expiry": {"blacklist_expiry", "blacklist_expiration"},
}


# --- tiny read-only helpers (no product code touched) -------------------------------
def _normalize_token(value: str) -> str:
    """Lowercase and squash punctuation so `Wrong Units` and `wrong-units` are one token."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(value).lower())).strip("_")


def _manifest_path() -> pathlib.Path:
    """The one pinned manifest path. No search, no fallback — a second manifest is a defect."""
    canonical = REPO_ROOT / MANIFEST_REL
    if canonical.is_file():
        return canonical
    raise AssertionError(
        f"no fixture manifest: expected the human-approved manifest at {MANIFEST_REL} "
        "(S2 / A3 ground truth). This path is pinned; the manifest is never searched for."
    )


def _load_json(path: pathlib.Path):
    """Parse a JSON document, turning a parse error into a clean test failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any read/parse problem is a failed criterion
        raise AssertionError(f"{path} is not readable JSON: {exc}") from None


def _manifest() -> dict:
    """Read the manifest as a JSON object."""
    data = _load_json(_manifest_path())
    assert isinstance(data, dict), "the fixture manifest must be a JSON object"
    return data


def _require(mapping: dict, key: str, kinds, where: str):
    """Fetch `key` off a manifest mapping, asserting presence and type."""
    assert isinstance(mapping, dict), f"{where} must be a JSON object, got {type(mapping).__name__}"
    assert key in mapping, f"{where} must declare {key!r}"
    value = mapping[key]
    assert isinstance(value, kinds), (
        f"{where}.{key} must be {kinds}, got {type(value).__name__}"
    )
    return value


def _nonempty_str(mapping: dict, key: str, where: str) -> str:
    value = _require(mapping, key, str, where)
    assert value.strip(), f"{where}.{key} must not be empty"
    return value


def _number(mapping: dict, key: str, where: str) -> float:
    value = _require(mapping, key, (int, float), where)
    assert not isinstance(value, bool), f"{where}.{key} must be a number, not a bool"
    return float(value)


def _canonical_json(payload) -> str:
    """Canonical serialization used for every byte-identity comparison in this file."""
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=None)
    except TypeError as exc:
        raise AssertionError(
            f"value is not JSON-serializable plain data (required for byte-identity): {exc}"
        ) from None


def _approval_hashes(manifest: dict) -> set:
    """The sha256 digests that a correct `approval.content_hash` may equal.

    The approved content is the manifest document with its own `approval` block removed,
    serialized as JSON with `sort_keys=True` and `separators=(",", ":")`. Both
    `ensure_ascii` settings are accepted so the contract does not hinge on one flag.
    """
    body = {k: v for k, v in manifest.items() if k != "approval"}
    digests = set()
    for ensure_ascii in (True, False):
        text = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=ensure_ascii
        )
        digests.add(hashlib.sha256(text.encode("utf-8")).hexdigest())
    return digests


def _golden_set_pitches(manifest: dict) -> list:
    """The approved golden pitches, loaded through the manifest's recorded digest.

    The golden set is a separate committed document (`fixtures/golden/golden_set.json`, the
    location `tickets.json` T-065 and decisions D11 name), and the manifest carries a
    *reference* to it — `{path, sha256, count}`. Because that reference sits inside the body
    the approval hash covers, approving the manifest transitively approves the exact bytes of
    the golden set: change either one and this chain breaks.
    """
    ref = _require(manifest, "golden_set", dict, "manifest")
    rel = _nonempty_str(ref, "path", "manifest.golden_set")
    recorded = _nonempty_str(ref, "sha256", "manifest.golden_set").strip().lower()
    count = _require(ref, "count", int, "manifest.golden_set")

    assert rel == GOLDEN_SET_REL, (
        "manifest.golden_set.path is pinned to the single approved golden set at "
        f"{GOLDEN_SET_REL}, got {rel!r} (one golden set, one location)"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", recorded), (
        "manifest.golden_set.sha256 must be the sha256 hex digest of the golden set file"
    )

    path = REPO_ROOT / rel
    assert path.is_file(), (
        f"missing approved golden set at {rel} — the manifest references it (T-080 acc 1)"
    )
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == recorded, (
        f"manifest.golden_set.sha256 does not match {rel}: the approved manifest does not "
        f"cover the golden set as committed (recorded {recorded}, file {actual})"
    )

    doc = _load_json(path)
    pitches = _require(doc, "pitches", list, rel)
    assert pitches, f"{rel}.pitches must contain the golden intents/pitches (T-080 acc 1)"
    assert not isinstance(count, bool) and count == len(pitches), (
        f"manifest.golden_set.count says {count} but {rel} holds {len(pitches)} pitches"
    )
    return pitches


def _claim_type_dimensions(manifest: dict) -> dict:
    """The approved `claim_type → trust dimension` table (D18 as amended).

    Read from the manifest, never from the trust engine: the engine is the thing being
    graded, so it cannot also be the document that says which dimension a contradicted
    ingredient claim ought to have penalised.
    """
    table = _require(manifest, "claim_type_dimensions", dict, "manifest")
    assert table, (
        "manifest.claim_type_dimensions must be a non-empty {claim_type: dimension} table — "
        "without it no verification outcome has a typed route into trust (D18)"
    )
    return table


def _markdown_under(directory: pathlib.Path):
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.rglob("*.md") if p.is_file())


# ------------------------------------------------------------------------------------
# 1 — S2 / A3: the manifest carries a recorded human approval artifact
# ------------------------------------------------------------------------------------
@pytest.mark.epic("E8")
@pytest.mark.ticket("T-080")
def test_fixture_manifest_carries_a_recorded_human_approval_artifact():
    """S2 / A3 — one manifest at one pinned path, whose recorded human approval covers it.

    **What this test can prove offline.** That exactly one manifest exists, at
    `fixtures/manifest.json`. That it records a named approver that is not an agent or a
    placeholder, an ISO-8601 instant, and a sha256 digest. That a *separate* committed
    approval record exists at the path the manifest names, and that it quotes both the same
    digest and the same approver. And that the digest genuinely covers the manifest body —
    so the manifest (and, through `golden_set.sha256`, the golden set) cannot drift after
    approval without this assertion turning red.

    **What it cannot prove.** That a human approved anything. Every field checked here is
    writable by whoever writes the file, and no offline test can distinguish a human's
    signature from an agent typing the human's name. The authenticity of this artifact is
    enforced by EXECUTION.md rule 6 (no agent may fabricate the approval artifact) and by the
    human gate on T-080 — never by a green here. What the suite adds is *tamper evidence*:
    the two-document digest chain means an approval cannot be silently reused for different
    content, and the anti-automation check means an agent cannot honestly self-attribute.
    """
    canonical = REPO_ROOT / MANIFEST_REL
    assert canonical.is_file(), (
        f"the human-approved manifest must live at {MANIFEST_REL} (S2 / A3 ground truth)"
    )
    strays = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in sorted(FIXTURES_DIR.rglob("manifest.json"))
        if p.is_file() and p != canonical
    ]
    assert not strays, (
        f"exactly one fixture manifest may exist, at {MANIFEST_REL}; also found {strays}. "
        "A second manifest makes every reader's ground truth ambiguous."
    )

    manifest = _manifest()

    approval = _require(manifest, "approval", dict, "manifest")
    approver = _nonempty_str(approval, "approver", "manifest.approval")
    approved_at = _nonempty_str(approval, "approved_at", "manifest.approval").strip()
    artifact_rel = _nonempty_str(approval, "artifact", "manifest.approval").strip()
    content_hash = _nonempty_str(approval, "content_hash", "manifest.approval").strip().lower()

    assert re.fullmatch(r"[0-9a-f]{64}", content_hash), (
        "manifest.approval.content_hash must be a sha256 hex digest of the approved content"
    )
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", approved_at
    ), (
        "manifest.approval.approved_at must be an ISO-8601 instant with an explicit offset, "
        f"got {approved_at!r} (recorded, never compared against a clock)"
    )
    assert approver.strip().lower() not in {"tbd", "todo", "none", "n/a", "unknown"}, (
        "manifest.approval.approver must name the human who approved the manifest"
    )
    assert not re.search(
        r"(claude|gpt|llm|\bagent\b|\bbot\b|swarm|automat|\bai\b|\bsystem\b)",
        approver.lower(),
    ), (
        f"manifest.approval.approver names automation ({approver!r}). A3 requires a HUMAN "
        "approval; EXECUTION.md rule 6 forbids any agent from fabricating this artifact."
    )

    # The approval is recorded twice, in two documents that must agree: the manifest's
    # `approval` block, and a committed human-authored record that quotes the same digest.
    assert artifact_rel.startswith("fixtures/") and ".." not in artifact_rel, (
        "manifest.approval.artifact must be the repo-relative path of the committed human "
        f"approval record under fixtures/, got {artifact_rel!r}"
    )
    artifact = REPO_ROOT / artifact_rel
    assert artifact.is_file(), (
        f"the recorded human approval artifact {artifact_rel} does not exist "
        "(T-080 acceptance 1: the approval artifact is committed)"
    )
    record = artifact.read_text(encoding="utf-8")
    assert content_hash in record.lower(), (
        f"{artifact_rel} does not quote the digest it approves ({content_hash}) — the "
        "approval record and the manifest do not describe the same document"
    )
    assert approver.strip() in record, (
        f"{artifact_rel} does not name the approver the manifest records ({approver!r})"
    )

    expected = _approval_hashes(manifest)
    assert content_hash in expected, (
        "manifest.approval.content_hash does not match the manifest content it claims to "
        "approve — the manifest changed after approval, or the hash was never computed. "
        f"expected one of {sorted(expected)}, found {content_hash}"
    )


# ------------------------------------------------------------------------------------
# 2 — S2: the manifest, not the trust engine, defines the dishonest store and the budget
# ------------------------------------------------------------------------------------
@pytest.mark.epic("E8")
@pytest.mark.ticket("T-080")
def test_manifest_defines_the_dishonest_store_behaviours_and_the_episode_budget():
    """S2 / A3 — the manifest supplies the dishonest behaviours, budget, prior N and trajectory."""
    manifest = _manifest()

    _nonempty_str(manifest, "seed_category", "manifest")
    _require(manifest, "seed", int, "manifest")

    threshold = _number(manifest, "blacklist_threshold", "manifest")
    assert 0.0 < threshold < 1.0, "manifest.blacklist_threshold must be a published score in (0,1)"

    budget = _require(manifest, "episode_budget", int, "manifest")
    assert not isinstance(budget, bool) and budget > 0, (
        "manifest.episode_budget must be a positive number of simulation episodes"
    )

    prior_n = _require(manifest, "new_store_prior_n", int, "manifest")
    assert not isinstance(prior_n, bool) and prior_n > 0, (
        "manifest.new_store_prior_n must be the positive N of clean episodes DESIGN's "
        "Beta prior is calibrated against"
    )

    store = _require(manifest, "dishonest_store", dict, "manifest")
    _nonempty_str(store, "store_id", "manifest.dishonest_store")
    behaviours = _require(store, "behaviours", list, "manifest.dishonest_store")
    assert behaviours, "manifest.dishonest_store.behaviours must script at least one behaviour"
    for i, behaviour in enumerate(behaviours):
        where = f"manifest.dishonest_store.behaviours[{i}]"
        # Three frozen keys, one spelling each. `kind` is what T-081's script is graded on;
        # `dim` + `type` are what T-062 turns the behaviour into as a trust observation.
        _nonempty_str(behaviour, "kind", where)
        dim = _nonempty_str(behaviour, "dim", where)
        _nonempty_str(behaviour, "type", where)
        assert dim in TRUST_DIMENSIONS, (
            f"{where}.dim must be one of the six trust dimensions "
            f"{sorted(TRUST_DIMENSIONS)}, got {dim!r}"
        )

    trajectory = _require(manifest, "expected_trust_trajectory", list, "manifest")
    assert trajectory, "manifest.expected_trust_trajectory must have at least two points"
    episodes = []
    for i, point in enumerate(trajectory):
        where = f"manifest.expected_trust_trajectory[{i}]"
        episode = _require(point, "episode", int, where)
        assert not isinstance(episode, bool), f"{where}.episode must be an integer"
        score = _number(point, "score", where)
        tolerance = _number(point, "tolerance", where)
        assert 0 <= episode <= budget, f"{where}.episode must lie inside the episode budget"
        assert 0.0 <= score <= 1.0, f"{where}.score must be a trust score in [0,1]"
        assert tolerance > 0.0, f"{where}.tolerance must be a positive tolerance band"
        episodes.append(episode)

    assert episodes == sorted(set(episodes)) and len(episodes) >= 2, (
        "manifest.expected_trust_trajectory must be at least two points at strictly "
        f"increasing episodes, got {episodes}"
    )

    first, last = trajectory[0], trajectory[-1]
    assert float(first["score"]) > threshold, (
        "the dishonest store must START above the blacklist threshold — a trajectory that "
        "begins below it proves nothing about the trust engine"
    )
    assert float(last["score"]) + float(last["tolerance"]) < threshold, (
        "S2: the manifest's expected trajectory must END below the blacklist threshold "
        "even at the top of its tolerance band, within the episode budget "
        f"(last={last['score']}±{last['tolerance']}, threshold={threshold})"
    )


# ------------------------------------------------------------------------------------
# 3 — S8 / R18: the golden set labels all four statuses and covers every eval gate
# ------------------------------------------------------------------------------------
@pytest.mark.epic("E8")
@pytest.mark.ticket("T-080")
def test_golden_set_covers_all_four_statuses_and_every_eval_gate_case():
    """S8 / R18 — the golden pitch set labels all four statuses and carries every eval-gate case."""
    manifest = _manifest()
    golden = _golden_set_pitches(manifest)

    seen_statuses = set()
    seen_gates = set()
    mixed_pitches = []
    pitch_ids = []

    for i, pitch in enumerate(golden):
        where = f"{GOLDEN_SET_REL}#pitches[{i}]"
        pitch_ids.append(_nonempty_str(pitch, "pitch_id", where))
        _nonempty_str(pitch, "text", where)

        # Each pitch carries the catalog snapshot it is labelled against, so the verifier
        # (T-065) is handed both sides of the comparison by the approved ground truth.
        snapshot = _require(pitch, "catalog_snapshot", dict, where)
        _nonempty_str(snapshot, "snapshot_id", f"{where}.catalog_snapshot")
        assert _require(snapshot, "products", list, f"{where}.catalog_snapshot"), (
            f"{where}.catalog_snapshot.products must not be empty"
        )

        gates = _require(pitch, "gates", list, where)
        seen_gates.update(_normalize_token(g) for g in gates)

        claims = _require(pitch, "claims", list, where)
        assert claims, f"{where}.claims must label at least one claim"
        statuses = set()
        claim_refs = []
        for j, claim in enumerate(claims):
            cwhere = f"{where}.claims[{j}]"
            claim_refs.append(_nonempty_str(claim, "claim_ref", cwhere))
            _nonempty_str(claim, "text", cwhere)
            status = _nonempty_str(claim, "expected_status", cwhere).strip().lower()
            assert status in VERIFICATION_STATUSES, (
                f"{cwhere}.expected_status must be one of {sorted(VERIFICATION_STATUSES)}, "
                f"got {status!r}"
            )
            statuses.add(status)
        assert len(set(claim_refs)) == len(claim_refs), (
            f"{where}.claims claim_refs must be unique, got {claim_refs}"
        )
        seen_statuses.update(statuses)
        if {"verified", "contradicted"} <= statuses:
            mixed_pitches.append(pitch_ids[-1])

    assert len(set(pitch_ids)) == len(pitch_ids), (
        f"{GOLDEN_SET_REL} pitch_ids must be unique, got duplicates in {pitch_ids}"
    )
    assert seen_statuses == VERIFICATION_STATUSES, (
        "S8: the golden set must label at least one claim with each of "
        f"{sorted(VERIFICATION_STATUSES)}; missing {sorted(VERIFICATION_STATUSES - seen_statuses)}"
    )
    assert mixed_pitches, (
        "S8 names a golden pitch containing BOTH true and false claims — no single pitch in "
        "the golden set carries a `verified` claim and a `contradicted` claim together"
    )

    missing = [
        canonical
        for canonical, aliases in EVAL_GATES.items()
        if not (aliases & seen_gates)
    ]
    assert not missing, (
        "the golden set is missing a case for these DESIGN eval gates: "
        f"{missing} (gates found: {sorted(seen_gates)})"
    )


# ------------------------------------------------------------------------------------
# 4 — C9 / S4: the seed generator is deterministic under a fixed seed, and idempotent
# ------------------------------------------------------------------------------------
@pytest.mark.epic("E8")
@pytest.mark.ticket("T-080")
def test_seed_generator_is_deterministic_and_idempotent_under_a_fixed_seed():
    """C9 / S4 — the seed generator reproduces byte-identical output and applies idempotently."""
    from fixtures.generator import apply, generate

    manifest = _manifest()
    seed_category = _nonempty_str(manifest, "seed_category", "manifest")
    seed = _require(manifest, "seed", int, "manifest")
    dishonest_id = _nonempty_str(
        _require(manifest, "dishonest_store", dict, "manifest"), "store_id",
        "manifest.dishonest_store",
    )

    first = generate(seed_category, seed)
    second = generate(seed_category, seed)

    assert _canonical_json(first) == _canonical_json(second), (
        "generate(SEED_CATEGORY, seed) must be byte-identical across two calls under the "
        "same seed (C9: fixed seeds reproduce identical streams)"
    )

    for key in ("catalog", "stores", "event_script"):
        value = _require(first, key, (list, dict), "generate(...)")
        assert value, f"generate(...)[{key!r}] must not be empty"

    stores = first["stores"]
    store_ids = {
        (s.get("store_id") if isinstance(s, dict) else s) for s in stores
    } if isinstance(stores, list) else set(stores)
    assert dishonest_id in store_ids, (
        "T-080 acc 2: the generator must seed the manifest's dishonest store "
        f"({dishonest_id!r}) among {sorted(map(str, store_ids))}"
    )

    other = generate(seed_category, seed + 1)
    assert _canonical_json(other) != _canonical_json(first), (
        "a different seed must produce different seeded data — a generator that ignores "
        "its seed is not seeded"
    )

    target: dict = {}
    report_a = apply(first, target)
    created_a = _require(report_a, "created", int, "apply(...) report")
    unchanged_a = _require(report_a, "unchanged", int, "apply(...) report")
    assert created_a > 0, "the first apply() must create the seeded objects"
    assert unchanged_a == 0, "the first apply() into an empty target cannot leave anything unchanged"

    report_b = apply(first, target)
    assert _require(report_b, "created", int, "apply(...) report") == 0, (
        "T-080 acc 3: applying the same seed payload twice must be a no-op the second time "
        "(`make demo-seed` is idempotent)"
    )
    assert _require(report_b, "unchanged", int, "apply(...) report") == created_a, (
        "the second apply() must report every previously-created object as unchanged"
    )


# ------------------------------------------------------------------------------------
# 5 — S2: the dishonest script is graded against the manifest, not consulted for truth
# ------------------------------------------------------------------------------------
@pytest.mark.epic("E8")
@pytest.mark.ticket("T-081")
def test_dishonest_script_emits_exactly_the_manifest_behaviours():
    """S2 — the simulator's dishonest script emits the manifest's behaviour sequence exactly."""
    from services.sim.src.dishonest import run_dishonest_script

    manifest = _manifest()
    budget = _require(manifest, "episode_budget", int, "manifest")
    seed = _require(manifest, "seed", int, "manifest")
    store = _require(manifest, "dishonest_store", dict, "manifest")
    expected_kinds = [
        _nonempty_str(b, "kind", f"manifest.dishonest_store.behaviours[{i}]")
        for i, b in enumerate(_require(store, "behaviours", list, "manifest.dishonest_store"))
    ]

    emitted = run_dishonest_script(manifest, seed)
    assert isinstance(emitted, list), "run_dishonest_script must return a list of emitted behaviours"

    emitted_kinds = []
    episodes = []
    for i, record in enumerate(emitted):
        where = f"emitted[{i}]"
        emitted_kinds.append(_nonempty_str(record, "kind", where))
        episode = _require(record, "episode", int, where)
        assert not isinstance(episode, bool), f"{where}.episode must be an integer"
        assert 0 <= episode <= budget, (
            f"{where}.episode={episode} falls outside the manifest's episode budget {budget}"
        )
        episodes.append(episode)

    assert emitted_kinds == expected_kinds, (
        "S2: the dishonest script's emitted behaviour sequence must equal the approved "
        f"manifest's, element for element. manifest={expected_kinds} emitted={emitted_kinds}"
    )
    assert episodes == sorted(episodes), (
        f"the emitted behaviours must be in non-decreasing episode order, got {episodes}"
    )

    again = run_dishonest_script(manifest, seed)
    assert _canonical_json(again) == _canonical_json(emitted), (
        "the dishonest script must be deterministic under the manifest's fixed seed"
    )


# ------------------------------------------------------------------------------------
# 6 — S6 / S1: the demo runbook is EXECUTABLE prose — every command it names resolves
# ------------------------------------------------------------------------------------
# Why this section is not a `make`-target-definition check any more.
#
# The earlier version of test 6 certified the runbook by checking that every `make <target>`
# it names is DEFINED in the Makefile. Definition is shape, not behaviour. `e2e-live:` was
# defined and running it printed `bash: docs/demo/e2e_live.sh: No such file or directory`
# (bash 127, make exit 2); `uv run python -m pytest e2e/test_s1_flow.py -q` named a file that
# was not in the repository at all. So E8 read 8/8 and acceptance read 120/120 for an unknown
# number of cycles while BOTH of the runbook's headline commands were broken. A gate that
# cannot go red on the defect it exists for is worthless, and that one could not.
#
# What follows grades RESOLVABILITY instead, and the command list is parsed out of the
# document rather than frozen here — a gate over a hardcoded list of command names stops
# working the moment the runbook changes, which is the same defect wearing a different hat.
#
# The parser is a condensed, self-contained port of `docs/tests/test_runbook_executability.py`
# (the unfrozen tier), which survived a 27-case bypass battery plus a second hardening round.
# The lessons that shaped it, each one measured rather than imagined:
#
#   * Fence-language bypasses. Re-labelling ```bash to ```console or ```text made a step
#     VANISH from that sweep with rc=0. This port therefore has NO fence model at all: it
#     sweeps the raw text line by line, so there is no fence rule to relabel around.
#   * A recipe head that is an interpreter already on PATH hid its operand: `bash
#     no_such_root.sh` and the extensionless `bash no_such_root` both slipped through a scan
#     that only looked for tokens containing a directory component. So an interpreter's first
#     operand is resolved as a repository file whether or not it has a `/` or an extension.
#   * Command position. `- make X`, `* make X`, `1. make X`, `Then make X`, `run make X`,
#     `if`/`while`/`until`/`xargs`/`watch make X`: 12 of 36 realistic positions were read
#     before hardening, 36 of 36 after, with 0 false positives on English prose.
#   * Sweeps go QUIET rather than red. Three in this repo did (6->0 of 8, 70->0 of 79,
#     48->0 of 66). Every assertion below is therefore preceded by an arming pin, and a
#     second, position-blind raw scan is reconciled against the first parse.
#
# HERMETICITY. This test reads exactly: every `*.md` under `docs/demo/`, the root `Makefile`,
# and the repository files those two name (`os.path` existence, `st_mode`, and the text of
# `*.sh` files). Its two subprocesses are `python -E -s -c "importlib.util.find_spec(...)"`
# and `pytest --collect-only --noconftest -o addopts= -o pythonpath= -p no:cacheprovider`,
# both run with `cwd=REPO_ROOT` and with `PYTEST_ADDOPTS`/`PYTHONPATH` stripped from the
# environment, so no worker-editable pytest config, no conftest and no inherited `sys.path`
# can steer the verdict. It requires no environment variable of its own — not even
# `PROXYSHOP_WORKER`, because `--noconftest` means D38's session guard never runs. Nothing it
# executes starts a service, opens a database, or reaches the network: `bash -n` parses
# without executing and `--collect-only` collects without running. `PYTHONDONTWRITEBYTECODE`
# and `-p no:cacheprovider` keep both probes from leaving a `__pycache__` or a
# `.pytest_cache` behind, so grading the tree does not modify it.


#: The one directory of operator-facing runbooks. Every markdown file under it is swept, not
#: one named document: the runbook itself promises an "extension runbook", and that page must
#: be graded the day it lands rather than the day someone remembers to add it here.
_RB_DIR = REPO_ROOT / "docs" / "demo"
_RB_MAKEFILE = REPO_ROOT / "Makefile"

#: Floor for commands naming something this gate can resolve. Deliberately NOT the raw count,
#: which three `VAR=value` lines would inflate. Today's measurement is far above it.
_RB_MIN_RESOLVABLE = 5

#: Programs that take a script path in their operand slot. Matched on the BASENAME, never
#: with `str.endswith` — that read every token ending in "sh" (`verify.sh`, `publish`) as an
#: interpreter.
_RB_INTERPRETERS = {"bash", "sh", "zsh", "dash", "python", "python3", "node"}

#: Command words that only wrap another command, plus the English and markdown lead-ins that
#: put a command in command position. `make` after any of these is a command; `make` after
#: "will" is the verb.
_RB_LEADINS = {
    "env", "sudo", "command", "exec", "time", "nohup", "xargs", "watch", "run", "then",
    "else", "do", "if", "while", "until", "unless", "and", "&&", "||", ";", "|",
}  # fmt: skip

#: Shell syntax and builtins: they name no program to resolve.
_RB_SHELL_WORDS = {
    "echo", "cd", "exit", "true", "false", "set", "test", "read", "printf", "shift", "local",
    "return", "eval", "trap", "unset", "export", "source", "wait", "umask", "ulimit",
    "[", "]", "[[", "]]", "{", "}", "(", ")", ":", "if", "then", "else", "elif", "fi", "for",
    "while", "until", "do", "done", "case", "esac", "function",
}  # fmt: skip

#: External tools whose presence is the OPERATOR's environment, not this repository's
#: contents. A recipe head that is none of these, is not a file in the repo and is not on
#: PATH is a FAIL — that is what stops a recipe being rewritten to `@proxyshop-live-runner`
#: and grading green. Whether `docker` itself is installed is INFO, so this frozen metric
#: does not move when the box it runs on changes.
_RB_EXTERNAL_TOOLS = {
    "docker", "docker-compose", "uv", "uvx", "node", "npm", "npx", "pnpm", "yarn", "git",
    "curl", "wget", "jq", "psql", "redis-cli", "cypher-shell", "rm", "mkdir", "cp", "mv",
    "sed", "awk", "grep", "cat", "tee", "sort", "find", "xargs", "chmod", "open",
}  # fmt: skip

_RB_PASS, _RB_FAIL, _RB_INFO, _RB_UNCLASSIFIED = "PASS", "FAIL", "INFO", "UNCLASSIFIED"

#: Categories whose commands name a repository artefact. None of them may ever grade INFO:
#: "nothing to check here" is the failure being hunted, not a way past it.
_RB_RESOLVABLE = {
    "make target", "pytest invocation", "python module", "python script", "shell script",
    "shell script named in the runbook", "cp source",
}  # fmt: skip

_RB_BACKTICKS = re.compile(r"`([^`]*)`")
#: An inline span that is EXACTLY a make invocation. `\s` matches a newline, so the
#: Troubleshooting section's ``Run `make\n  bootstrap`.`` — a span broken across two prose
#: lines — is still found. Deliberately narrow: a generous pattern drags `CheckoutProvider`
#: and `orders/paid` in.
_RB_INLINE_MAKE = re.compile(r"`(make\s+[A-Za-z0-9][A-Za-z0-9_.\-]*)`")
#: Any shell script path named anywhere, prose included: `docs/demo/e2e_live.sh` is named
#: ONLY in prose. `(?!\.\w)` stops `x.sh.bak` reading as `x.sh`, while a sentence-ending
#: period still matches — `…e2e_live.sh.` hid the path from both parses at once when the
#: guard was the blunter `(?![\w.])`.
_RB_SCRIPT_TOKEN = re.compile(r"(?<![\w/.\-])((?:[\w.\-]+/)*[\w.\-]+\.sh)(?![\w]|\.\w)")
#: A python path, for the raw cross-check only. A directory component is required so prose
#: mentioning a bare `foo.py` is not read as a repository artefact.
_RB_PY_TOKEN = re.compile(r"(?<![\w/.\-])((?:[\w.\-]+/)+[\w.\-]+\.py)(?![\w]|\.\w)")

#: Any REPO-RELATIVE path a recipe names, whatever its extension. Restricting this to `.sh`
#: and `.py` left every other program a recipe invokes ungraded. The lookbehind excludes
#: absolute paths: `/bin/bash` is not a file in this repository.
_RB_RECIPE_PATH = re.compile(r"(?<![\w/.\-$])((?:\./)?(?:[\w.\-]+/)+[\w.\-]+|\./[\w.\-]+)(?![\w/])")
_RB_RECIPE_MODULE = re.compile(r"-m\s+([A-Za-z_][\w.]*)")
_RB_UNEXPANDED = re.compile(r"\$[({](?!MAKE[)}])(?P<name>[A-Za-z_][\w.]*)[)}]")
_RB_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_RB_MAKEFILE_ASSIGN = re.compile(r"^\s*(?P<name>[A-Za-z_][\w.]*)\s*(?P<op>[:?+!]?=)\s*(?P<val>.*)$")
#: `target [more] [: | ::] [prereqs] [; recipe]`. The prerequisite list and the double-colon
#: form both silently erased this Makefile's inline `target: ; @recipe` bodies in an earlier
#: draft, and every one of its targets is written that way.
_RB_MAKEFILE_TARGET = re.compile(
    r"^(?P<names>[A-Za-z0-9_][A-Za-z0-9_.\-/ ]*?)\s*(?P<colons>::|:)(?!=)"
    r"(?P<prereqs>[^;]*)(?:;(?P<recipe>.*))?$"
)
_RB_MAKE_VALUE_FLAGS = {"-C", "-f", "-I", "-j", "-l", "-O", "-W", "--directory", "--file", "--jobs"}
_RB_PYTEST_VALUE_FLAGS = {
    "-k", "-m", "-p", "-n", "-o", "-c", "-W", "--timeout", "--maxfail", "--rootdir",
    "--junitxml", "--deselect", "--ignore", "--override-ini", "--import-mode",
}  # fmt: skip
_RB_HEADS = {"make", "pytest", "python", "python3", "uv", "bash", "sh", "zsh", "node", "cp", "docker"}


def _rb_env():
    """Environment for this test's two probes: the caller's, minus everything that steers.

    `PYTEST_ADDOPTS` and `PYTHONPATH` are removed because a frozen metric that a two-line
    environment edit can move is not frozen. This repo has already paid for that lesson once
    with a `pyproject.toml` `addopts` that drove a metric from 50 to 100.
    """
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTHONPATH", None)
    # Read-only means read-only: without this, importing a module to answer "does it exist"
    # would leave `__pycache__` directories behind in the tree being graded.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _rb_run(argv, timeout):
    """Run a read-only probe, returning `(returncode, combined_output)` or `(None, why)`."""
    try:
        proc = subprocess.run(
            argv, cwd=str(REPO_ROOT), env=_rb_env(), capture_output=True, text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


_RB_MODULE_CACHE = {}
_RB_COLLECT_CACHE = {}
_RB_SCRIPT_CACHE = {}
_RB_RULES_CACHE = {}


def _rb_module_resolves(module):
    """Can `python -m <module>` find something? Answered in an isolated subprocess.

    `-E -s` drop `PYTHONPATH`, `PYTHONHOME` and the user site directory, so the only thing
    on `sys.path` besides the stdlib is the repo root (`cwd`), which is exactly the position
    an operator standing in the checkout is in.
    """
    if module not in _RB_MODULE_CACHE:
        code = (
            "import importlib.util as u, sys\n"
            f"sys.exit(0 if u.find_spec({module!r}) is not None else 1)\n"
        )
        rc, out = _rb_run([sys.executable, "-B", "-E", "-s", "-c", code], 120)
        if rc is None:
            _RB_MODULE_CACHE[module] = (False, f"could not be probed ({out})")
        else:
            _RB_MODULE_CACHE[module] = (
                (True, "") if rc == 0 else (False, "is not importable from the repo root")
            )
    return _RB_MODULE_CACHE[module]


def _rb_pytest_collects(rel):
    """Does `pytest --collect-only` gather at least one test from `rel`?

    Collection, never a run: it needs no datastore and no network, finishes in a fraction of
    a second, and "pytest cannot collect this path" is a complete answer to "can an operator
    type this command". `--noconftest` is what makes it safe to run inside the scorer's own
    process on any worker — no root conftest, so no D38 worker guard, no socket plugin, no
    collection-time datastore probe, and no chance of touching worker 0's database. `-o
    addopts=` / `-o pythonpath=` mirror what the frozen runner itself passes.

    It is emphatically NOT evidence that the tests prove anything. Read a green here as "the
    runbook's step reaches real code", never as "the demo works".
    """
    if rel not in _RB_COLLECT_CACHE:
        rc, out = _rb_run(
            [
                sys.executable, "-m", "pytest", "--collect-only", "-q", "--noconftest",
                "-p", "no:cacheprovider", "-o", "addopts=", "-o", "pythonpath=", rel,
            ],
            300,
        )
        if rc is None:
            _RB_COLLECT_CACHE[rel] = (False, f"collection could not be run ({out})")
        elif rc == 0:
            _RB_COLLECT_CACHE[rel] = (True, "")
        else:
            tail = [ln for ln in out.splitlines() if ln.strip()][-3:]
            hint = "collected 0 tests" if rc == 5 else f"pytest exited {rc}"
            _RB_COLLECT_CACHE[rel] = (False, f"pytest cannot collect it ({hint}): {' / '.join(tail)}")
    return _RB_COLLECT_CACHE[rel]


def _rb_check_script(rel, must_be_executable):
    """A shell script exists, is a file, carries a command, and `bash -n` parses it.

    The emptiness check is the honest limit of static grading: a script whose only line is
    `exit 0` passes here. What a script must DO is the specification of the ticket that
    writes it, and a gate that guessed would be grading its own opinion.
    """
    key = (rel, must_be_executable)
    if key in _RB_SCRIPT_CACHE:
        return _RB_SCRIPT_CACHE[key]
    path = REPO_ROOT / rel
    if not path.is_file():
        what = "is a directory, not a script" if path.is_dir() else "does not exist in the repo"
        result = (_RB_FAIL, f"{rel} {what}")
    elif must_be_executable and not os.access(path, os.X_OK):
        result = (_RB_FAIL, f"{rel} exists but is not executable (mode {oct(path.stat().st_mode & 0o777)})")
    else:
        body = [
            ln.strip()
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not body:
            result = (_RB_FAIL, f"{rel} exists but contains no command — only blanks, comments or a shebang")
        elif not shutil.which("bash"):
            result = (_RB_INFO, f"{rel} exists and is non-empty; `bash` is not on PATH so it was not parsed")
        else:
            rc, out = _rb_run(["bash", "-n", str(path)], 120)
            if rc is None:
                result = (_RB_FAIL, f"{rel} could not be checked with `bash -n` ({out})")
            elif rc == 0:
                result = (_RB_PASS, f"{rel} exists, is non-empty and `bash -n` parses it")
            else:
                result = (_RB_FAIL, f"{rel} is not parseable by `bash -n`: {out.strip()[:300]}")
    _RB_SCRIPT_CACHE[key] = result
    return result


# --- the Makefile --------------------------------------------------------------------
def _rb_parse_makefile():
    """`{target: {recipe, prereqs, defs, double_colon}}` for both recipe forms.

    Simple `VAR = value` assignments are expanded, because `@bash $(E2E)` otherwise hides the
    very path this gate exists to check. An UNDEFINED variable is left intact rather than
    expanded to nothing — substituting the empty string made `@bash $(NOPE)` pass by making
    the path disappear.
    """
    if _RB_RULES_CACHE:
        return _RB_RULES_CACHE
    if not _RB_MAKEFILE.is_file():
        return {}
    bodies, prereqs, defs, dcolon, variables = {}, {}, {}, {}, {}
    current, counted = [], set()
    special = {".PHONY", ".DEFAULT", ".SUFFIXES", ".ONESHELL", ".SILENT", ".NOTPARALLEL"}

    for raw in _RB_MAKEFILE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("\t"):
            for name in current:
                bodies.setdefault(name, []).append(raw.strip())
                if name not in counted:
                    defs[name] = defs.get(name, 0) + 1
                    counted.add(name)
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            current = []
            continue
        assign = _RB_MAKEFILE_ASSIGN.match(raw)
        if assign and ":" not in assign.group("name"):
            variables[assign.group("name")] = assign.group("val").strip()
            current = []
            continue
        m = _RB_MAKEFILE_TARGET.match(stripped if raw[:1] == " " else raw)
        if not m:
            current = []
            continue
        names = [n for n in m.group("names").split() if n not in special]
        if not names or names[0].startswith("."):
            current = []
            continue
        current, counted = names, set()
        recipe = (m.group("recipe") or "").strip()
        deps = [d for d in m.group("prereqs").split() if d != "|"]
        for name in names:
            bodies.setdefault(name, [])
            prereqs.setdefault(name, []).extend(deps)
            dcolon[name] = dcolon.get(name, False) or m.group("colons") == "::"
            if recipe:
                bodies[name].append(recipe)
                defs[name] = defs.get(name, 0) + 1
                counted.add(name)

    def expand(text, depth=0):
        if depth > 8:
            return text
        out = re.sub(
            r"\$[({](?P<name>[A-Za-z_][\w.]*)[)}]",
            lambda mm: variables.get(mm.group("name"), mm.group(0)),
            text,
        )
        return out if out == text else expand(out, depth + 1)

    for name, body in bodies.items():
        _RB_RULES_CACHE[name] = {
            "recipe": expand("\n".join(body)),
            "prereqs": prereqs.get(name, []),
            "defs": defs.get(name, 0),
            "double_colon": dcolon.get(name, False),
        }
    return _RB_RULES_CACHE


def _rb_is_interpreter(token):
    tok = token.strip("\"'").lstrip("@-+")
    return tok in ("source", ".") or pathlib.PurePosixPath(tok).name in _RB_INTERPRETERS


def _rb_positional(args, value_flags):
    """Non-flag operands, skipping the value of every value-taking flag."""
    out, skip = [], False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok in value_flags:
                skip = True
            continue
        out.append(tok)
    return out


def _rb_recipe_chunks(recipe):
    """Each command in a recipe as a token list, split on newlines and shell separators."""
    chunks = []
    for line in recipe.split("\n"):
        for chunk in re.split(r"&&|\|\||[;|]", line):
            toks = chunk.split()
            if toks:
                chunks.append(toks)
    return chunks


def _rb_recipe_head(toks):
    """The program a recipe chunk runs, past `@`/`-`/`+` prefixes and `VAR=value` settings."""
    for idx, tok in enumerate(toks):
        word = tok.lstrip("@-+") if idx == 0 else tok
        if not word or _RB_ASSIGN.match(word):
            continue
        return word.strip("\"'")
    return ""


def _rb_undefined_program_vars(recipe):
    """Undefined `$(VAR)` sitting where a PROGRAM or SCRIPT PATH would go.

    The hole being closed is `@bash $(NOPE)`: expanding an unassigned variable to the empty
    string made the path vanish, so the target passed by naming nothing. The hole
    deliberately left open is `--category "$(SEED_CATEGORY)"` — the runbook states plainly
    that `demo-seed` reads that from the operator's environment, so an unassigned DATA
    argument is correct and flagging it was a false positive. Position, not presence.
    """
    found = []
    for chunk in re.split(r"[;&|]+", recipe.replace("\n", " ")):
        toks = chunk.split()
        for idx, tok in enumerate(toks):
            hits = [m.group("name") for m in _RB_UNEXPANDED.finditer(tok)]
            if hits and (idx == 0 or _rb_is_interpreter(toks[idx - 1])):
                found.extend(hits)
    return found


def _rb_preceding_token(recipe, path_token):
    """The token before each occurrence of `path_token`; an interpreter anywhere wins."""
    best = ""
    for chunk in re.split(r"[;&|]+", recipe.replace("\n", " ")):
        toks = chunk.split()
        for idx, tok in enumerate(toks):
            if tok.strip("\"'") != path_token:
                continue
            prev = toks[idx - 1] if idx else ""
            if prev and _rb_is_interpreter(prev):
                return prev
            best = prev
    return best


def _rb_provisioned(rel):
    """`.venv/` is what `make bootstrap` creates; it is not a repository artefact."""
    return rel.startswith(".venv/") or "/.venv/" in rel


def _rb_check_make_target(target, seen=None):
    """Is `make <target>` RUNNABLE? Definition, and everything its recipe reaches."""
    seen = set() if seen is None else seen
    if target in seen:
        return True, []
    seen.add(target)

    rules = _rb_parse_makefile()
    if target not in rules:
        return False, [f"no target `{target}:` is defined in the root Makefile"]
    rule = rules[target]
    problems = []

    # The Makefile sweep's arming pin, and it has no exemption. Letting a target with no
    # recipe pass because a PREREQUISITE had one is exactly how `e2e-live: preflight` would
    # turn the broken target green while `make -n e2e-live` printed only ./scripts/preflight.sh.
    if not rule["recipe"].strip():
        return False, [
            f"target `{target}:` is defined with NO recipe of its own — `make {target}` would "
            f"run only its prerequisites {rule['prereqs'] or '[]'}, so this gate cannot confirm "
            f"it runs anything the runbook's step promises"
        ]

    if rule["defs"] > 1 and not rule["double_colon"]:
        problems.append(
            f"target `{target}:` is given a recipe {rule['defs']} times; GNU make keeps only "
            f"the LAST for a single-colon rule, so what an operator would actually run cannot "
            f"be determined from this file"
        )

    for hit in dict.fromkeys(_rb_undefined_program_vars(rule["recipe"])):
        problems.append(
            f"recipe for `{target}` runs `$({hit})` as a program or script path and the "
            f"Makefile never assigns it, so a missing file behind that variable would be "
            f"invisible to this gate"
        )

    # (a) every repo-relative path the recipe names, whatever its extension.
    for token in dict.fromkeys(_RB_RECIPE_PATH.findall(rule["recipe"])):
        rel = token[2:] if token.startswith("./") else token
        if _rb_provisioned(rel):
            continue
        path = REPO_ROOT / rel
        if not path.exists():
            problems.append(f"recipe for `{target}` invokes {rel}, which does not exist in the repo")
            continue
        if rel.endswith((".sh", ".py")) and not path.is_file():
            problems.append(f"recipe for `{target}` invokes {rel}, which is not a file")
            continue
        if rel.endswith(".sh"):
            verdict, why = _rb_check_script(rel, must_be_executable=False)
            if verdict == _RB_FAIL:
                problems.append(f"recipe for `{target}` invokes {why}")
        prev = _rb_preceding_token(rule["recipe"], token)
        if not (prev and _rb_is_interpreter(prev)) and not os.access(path, os.X_OK):
            problems.append(
                f"recipe for `{target}` execs {rel} directly and it is not executable "
                f"(mode {oct(path.stat().st_mode & 0o777)})"
            )

    for module in dict.fromkeys(_RB_RECIPE_MODULE.findall(rule["recipe"])):
        if module != "pytest":
            ok, why = _rb_module_resolves(module)
            if not ok:
                problems.append(f"recipe for `{target}` runs `-m {module}`, which {why}")

    # (b) every PROGRAM the recipe runs, and — the measured bypass — the first operand of
    # every interpreter, whether or not that operand carries a directory component or an
    # extension. `@bash no_such_root.sh` and `@bash no_such_root` both slipped past a scan
    # that only looked for `dir/file`, because the head was already on PATH.
    for toks in _rb_recipe_chunks(rule["recipe"]):
        head = _rb_recipe_head(toks)
        if not head or head in _RB_SHELL_WORDS:
            continue
        rest = toks[toks.index(head) + 1 :] if head in toks else toks[1:]
        if re.fullmatch(r"\$[({]MAKE[)}]", head):
            for sub in _rb_positional(rest, _RB_MAKE_VALUE_FLAGS):
                if "=" in sub:
                    continue
                ok, sub_problems = _rb_check_make_target(sub, seen)
                if not ok:
                    problems.extend(f"via `$(MAKE) {sub}`: {p}" for p in sub_problems)
            continue
        if "$" in head:
            continue  # reported by _rb_undefined_program_vars, or an expanded path above
        base = pathlib.PurePosixPath(head).name
        if base in _RB_INTERPRETERS and not head.startswith("/"):
            if "-c" in rest or "-m" in rest or base == "node" and "-e" in rest:
                pass  # inline script / module form: nothing in the operand slot is a path
            else:
                operands = [t.strip("\"'") for t in _rb_positional(rest, set())]
                operands = [o for o in operands if "$" not in o and not o.startswith("/")]
                if operands:
                    rel = operands[0][2:] if operands[0].startswith("./") else operands[0]
                    if not _rb_provisioned(rel) and not (REPO_ROOT / rel).is_file():
                        problems.append(
                            f"recipe for `{target}` runs `{base} {operands[0]}` and "
                            f"{rel} is not a file in this repository, so the target dies with "
                            f"`No such file or directory` however well-formed it looks"
                        )
            continue
        if "/" in head:
            continue  # a repo path; already graded by scan (a)
        if base in _RB_EXTERNAL_TOOLS:
            continue  # the operator's environment, not this repository's contents
        if not shutil.which(head):
            problems.append(
                f"recipe for `{target}` invokes `{head}`, which is neither a file in this "
                f"repository, nor a known external tool, nor a program on PATH — so this "
                f"target cannot run as written"
            )

    for prereq in dict.fromkeys(rule["prereqs"]):
        if prereq in rules:
            ok, sub_problems = _rb_check_make_target(prereq, seen)
            if not ok:
                problems.extend(f"via prerequisite `{prereq}`: {p}" for p in sub_problems)

    return not problems, problems


# --- extracting the commands from the runbook text ------------------------------------
def _rb_split_chain(text):
    """Split a shell line on `&&` / `||` / `;` / `|` at top level."""
    parts, buf, quote, i = [], [], None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote, i = ch, i + 1
            buf.append(ch)
            continue
        if text[i : i + 2] in ("&&", "||"):
            parts.append("".join(buf))
            buf, i = [], i + 2
            continue
        if ch in (";", "|"):
            parts.append("".join(buf))
            buf, i = [], i + 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _rb_strip_comment(line):
    """Drop a trailing ` # comment`, respecting quotes."""
    out, quote, prev_space = [], None, True
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            prev_space = False
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            prev_space = False
            continue
        if ch == "#" and prev_space:
            break
        out.append(ch)
        prev_space = ch.isspace()
    return "".join(out).strip()


def _rb_logical_lines(text):
    """`[(lineno, text)]` with backslash continuations joined.

    A continued command is reported at its FIRST line. Grading only the first physical line
    would leave `uv run python -m pytest \\` + `  e2e/test_s1_flow.py` looking like a
    path-less pytest — which this gate fails closed, so the false red would be loud rather
    than silent, but it would still be a false red.
    """
    out, pending, first = [], None, 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if pending is None:
            first, pending = lineno, line
        else:
            pending = pending + " " + line.strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        out.append((first, pending))
        pending = None
    if pending is not None:
        out.append((first, pending))
    return out


def _rb_command_positions(line):
    """Offsets on `line` where a command word starts, with NO fence model whatsoever.

    Re-labelling ```bash to ```console or ```text made a step vanish from an earlier sweep
    with rc=0. There is no fence rule here to relabel around: every line of the document is
    read the same way, and position is decided by what precedes the word.

    A quote counts as a separator too: `bash -c 'make e2e-live'` hid the inner command from
    both parses at once, because this file does not expand inline scripts.

    Twelve of thirty-six realistic command positions were read before this rule existed —
    `- make X`, `* make X`, `1. make X`, `Then make X`, `run make X`, `if`/`while`/`until`/
    `xargs`/`watch make X` were all invisible. All thirty-six are read now, with no false
    positive on English prose: "will make check pass" is not command position, because
    "will" is not a lead-in and the text before it is not a separator.
    """
    starts = []
    for m in re.finditer(r"(?<![\w./\-])([A-Za-z0-9_./\-]+)", line):
        word = m.group(1)
        base = pathlib.PurePosixPath(word).name.lower()
        if base not in _RB_HEADS and not (word.startswith(("./", "../")) and word.endswith(".sh")):
            continue
        prefix = line[: m.start()].rstrip()
        last = prefix.split()[-1].strip("`'\"*_").lower() if prefix.split() else ""
        if (
            not prefix
            or prefix.endswith(
                ("`", "&", "|", ";", "(", "{", "$", "%", ">", "-", "*", "+", ".", ":", "'", '"')
            )
            or last in _RB_LEADINS
        ):
            starts.append(m.start())
    return starts


def _rb_tokenize(text):
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _rb_normalise(toks):
    """Strip `VAR=value` prefixes, wrapper programs and the `uv run` prelude."""
    while toks and _RB_ASSIGN.match(toks[0]):
        toks = toks[1:]
    while toks and toks[0] in ("env", "sudo", "command", "exec", "time", "nohup", "xargs", "watch"):
        toks = toks[1:]
    if toks[:2] == ["uv", "run"]:
        toks = toks[2:]
        while toks and toks[0].startswith("-"):  # `uv run --frozen python -m pytest …`
            toks = toks[1:]
    return toks


def _rb_extract(path):
    """Every command a runbook instructs an operator to type, derived from the document.

    Three sources, none of them a fence: command-position words on every logical line;
    every inline backtick span (which is where prose keeps its commands, and which may be
    broken across two lines); and a document-wide sweep for `*.sh` paths, because
    `docs/demo/e2e_live.sh` is named ONLY in prose.
    """
    doc = str(path.relative_to(REPO_ROOT))
    text = path.read_text(encoding="utf-8")
    found, seen = [], set()

    def add(lineno, raw_text, kind):
        body = _rb_strip_comment(raw_text.strip())
        if body.startswith(("$ ", "% ")):
            body = body[2:]
        if not body or body.startswith("#"):
            return
        for piece in _rb_split_chain(body):
            toks = _rb_normalise(_rb_tokenize(piece))
            if not toks:
                continue
            if len(toks) == 1 and toks[0] != "pytest":
                # A head with NO operand names nothing, so there is nothing to resolve. This
                # is what a fence marker looks like once the backticks are stripped
                # (```bash -> `bash`), and what the tail of a backtick span broken across two
                # prose lines looks like (``Run `make`` / ``bootstrap`.``) — the whole-text
                # `_RB_INLINE_MAKE` sweep reads that span correctly instead. `pytest` is the
                # exception and stays: a path-less `pytest -q` is a real runbook step, and
                # letting it grade INFO was the cheapest way found to erase a missing test
                # module from this inventory, so it fails closed.
                continue
            key = (lineno, kind, tuple(toks))
            if key in seen:
                continue
            seen.add(key)
            found.append({"doc": doc, "lineno": lineno, "kind": kind, "text": piece, "toks": toks})

    for lineno, line in _rb_logical_lines(text):
        # A line carrying a backtick is prose, and markdown prose delimits its commands with
        # backticks. Reading the surrounding English as operands turned correct steps red:
        # ``Run `make demo-seed` against the stub you actually started.`` graded a target
        # named "against". So a backticked line is read through its spans, and a line with no
        # backtick — every line inside a fence — is read whole. Neither is a fence rule: the
        # raw cross-check below is blind to backticks AND to fences, so a command hidden from
        # this split still reports as a coverage gap rather than vanishing.
        if "`" in line:
            for span in _RB_BACKTICKS.finditer(line):
                inner = span.group(1).strip()
                for start in _rb_command_positions(inner):
                    add(lineno, inner[start:], "shell")
        else:
            stripped = line.strip()
            if stripped.startswith(("$ ", "% ")):
                stripped = stripped[2:]
            for start in _rb_command_positions(stripped):
                add(lineno, stripped[start:], "shell")

    # An inline `make …` span broken across two prose lines: `\s` matches the newline.
    for hit in _RB_INLINE_MAKE.finditer(text):
        lineno = text[: hit.start()].count("\n") + 1
        add(lineno, " ".join(hit.group(1).split()), "shell")

    # Every `*.sh` named anywhere, prose included.
    for hit in _RB_SCRIPT_TOKEN.finditer(text):
        lineno = text[: hit.start()].count("\n") + 1
        key = (lineno, "script-reference", hit.group(1))
        if key in seen:
            continue
        seen.add(key)
        found.append(
            {"doc": doc, "lineno": lineno, "kind": "script-reference", "text": hit.group(1), "toks": []}
        )
    return found


# --- grading one command ---------------------------------------------------------------
def _rb_check_pytest_paths(args):
    paths = _rb_positional(args, _RB_PYTEST_VALUE_FLAGS)
    if not paths:
        # Fails CLOSED on purpose. Letting a path-less `pytest -q` grade INFO was the
        # cheapest way found to erase the missing e2e/test_s1_flow.py from this inventory.
        return _RB_FAIL, "names no path, so the proof this step promises cannot be shown to exist", []
    details, failed = [], False
    for rel in paths:
        target = REPO_ROOT / rel.split("::")[0]
        if not target.exists():
            failed = True
            details.append(f"{rel} does not exist in the repo")
            continue
        if rel.endswith(".py") and not target.is_file():
            failed = True
            details.append(f"{rel} exists but is a directory, not a test module")
            continue
        ok, why = _rb_pytest_collects(rel)
        if ok:
            details.append(f"{rel} exists and pytest collects it")
        else:
            failed = True
            details.append(f"{rel} exists but {why}")
    return (_RB_FAIL if failed else _RB_PASS), f"{len(paths)} pytest path(s) named", details


def _rb_grade(cmd):
    """Grade one extracted command: `(verdict, category, reason, details)`."""
    if cmd["kind"] == "script-reference":
        verdict, reason = _rb_check_script(cmd["text"], must_be_executable=False)
        return verdict, "shell script named in the runbook", reason, []

    toks = cmd["toks"]
    if toks[0] == "export" or all(_RB_ASSIGN.match(t) for t in toks):
        return _RB_PASS, "environment assignment", "sets shell state; nothing to resolve", []

    head = toks[0]
    # Lower-cased: this filesystem is case-insensitive, so `Make e2e-live` runs the real make.
    base = pathlib.PurePosixPath(head).name.lower()

    if base == "make" and "$" not in head:
        # The FIRST operand is always a target — an undefined one must still be able to fail.
        # Every operand AFTER it counts only while it is a target the Makefile really defines,
        # and the scan stops at the first that is not. Prose keeps commands inside sentences:
        # ``FATAL: run 'make bootstrap' first`` puts the English word "first" in the second
        # operand slot, and reading it as a second target turned a correct runbook red — while
        # taking only the first operand (what the frozen predecessor did) let the broken half
        # of a real `make lint types` go ungraded. Definedness separates the two cleanly.
        rules = _rb_parse_makefile()
        positional = [t for t in _rb_positional(toks[1:], _RB_MAKE_VALUE_FLAGS) if "=" not in t]
        targets = []
        for idx, word in enumerate(positional):
            cleaned = word.strip("`'\".,;:()*_")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", cleaned):
                break
            if idx and cleaned not in rules:
                break
            targets.append(cleaned)
        if not targets:
            return _RB_FAIL, "make target", "names no target, so nothing can be resolved", []
        details, failed = [], False
        for target in targets:
            ok, problems = _rb_check_make_target(target)
            if ok:
                details.append(f"`{target}` is defined and everything its recipe invokes resolves")
            else:
                failed = True
                details.extend(problems)
        return (_RB_FAIL if failed else _RB_PASS), "make target", f"{len(targets)} target(s) named", details

    if base.startswith("pytest") and "$" not in head:
        verdict, reason, details = _rb_check_pytest_paths(toks[1:])
        return verdict, "pytest invocation", reason, details

    if base.startswith("python") and "$" not in head:
        rest = toks[1:]
        if rest[:1] == ["-m"] and len(rest) >= 2:
            if rest[1] == "pytest":
                verdict, reason, details = _rb_check_pytest_paths(rest[2:])
                return verdict, "pytest invocation", reason, details
            ok, why = _rb_module_resolves(rest[1])
            return (
                (_RB_PASS if ok else _RB_FAIL),
                "python module",
                f"`{rest[1]}` resolves" if ok else f"`{rest[1]}` {why}",
                [],
            )
        scripts = [s for s in _rb_positional(rest, set()) if "$" not in s]
        if not scripts:
            return _RB_FAIL, "python script", "names no script path, so nothing resolves", []
        missing = [s for s in scripts if not (REPO_ROOT / s).exists()]
        if missing:
            return _RB_FAIL, "python script", f"{', '.join(missing)} does not exist in the repo", []
        return _RB_PASS, "python script", f"{', '.join(scripts)} exists", []

    if base in ("bash", "sh", "zsh") and "$" not in head and "-c" not in toks[1:]:
        scripts = [s for s in _rb_positional(toks[1:], set()) if "$" not in s]
        if not scripts:
            return _RB_FAIL, "shell script", "names no script path", []
        verdict, reason = _rb_check_script(scripts[0], must_be_executable=False)
        return verdict, "shell script", reason, []

    if head.startswith(("./", "../")) and "$" not in head:
        rel = head[2:] if head.startswith("./") else head
        if rel.endswith(".sh"):
            verdict, reason = _rb_check_script(rel, must_be_executable=True)
            return verdict, "shell script", reason, []
        path = REPO_ROOT / rel
        if not path.is_file():
            return _RB_FAIL, "python script", f"{rel} does not exist in the repo", []
        if not os.access(path, os.X_OK):
            return (
                _RB_FAIL, "python script",
                f"{rel} is invoked directly but is not executable "
                f"(mode {oct(path.stat().st_mode & 0o777)})", [],
            )
        return _RB_PASS, "python script", f"{rel} exists and is executable", []

    if base == "cp" and "$" not in head:
        operands = _rb_positional(toks[1:], set())
        if len(operands) < 2:
            return _RB_FAIL, "cp source", "no source/destination pair to resolve", []
        src = operands[0]
        if "$" in src:
            return _RB_UNCLASSIFIED, "unclassified command", f"the source `{src}` is behind a shell variable", []
        if not (REPO_ROOT / src).exists():
            return _RB_FAIL, "cp source", f"{src} does not exist, so this step cannot run", []
        return _RB_PASS, "cp source", f"{src} exists", []

    # A program this gate cannot classify must NEVER take the repository paths it names out
    # of the sweep with it: `$PYTHON -m pytest e2e/test_s1_flow.py` kept the missing module
    # visibly named while the classifier dropped the whole command into an uninspected
    # bucket. That was a real bypass, not a hypothetical.
    if "-m" in toks and toks[toks.index("-m") + 1 :][:1] == ["pytest"]:
        verdict, reason, details = _rb_check_pytest_paths(toks[toks.index("-m") + 2 :])
        return verdict, "pytest invocation", reason, details
    operands = [
        t for t in toks[1:]
        if not t.startswith("-") and "$" not in t
        and (t.split("::")[0].endswith(".sh") or ("/" in t and t.split("::")[0].endswith(".py")))
    ]
    if operands:
        details, failed = [], False
        for rel in operands:
            if rel.endswith(".sh"):
                verdict, why = _rb_check_script(rel, must_be_executable=False)
                failed = failed or verdict == _RB_FAIL
                details.append(why)
            elif not (REPO_ROOT / rel.split("::")[0]).exists():
                failed = True
                details.append(f"{rel} does not exist in the repo")
            else:
                ok, why = _rb_pytest_collects(rel)
                failed = failed or not ok
                details.append(f"{rel} exists and pytest collects it" if ok else f"{rel} {why}")
        return (
            (_RB_FAIL if failed else _RB_PASS), "python script",
            f"`{head}` is not a program this gate classifies, so it was graded by the "
            f"{len(operands)} repository path(s) it names", details,
        )

    if base in _RB_EXTERNAL_TOOLS or shutil.which(head):
        return (
            _RB_INFO, "external tool",
            f"`{head}` names no repository path; whether it is installed is the operator's "
            f"environment, which this gate does not enforce", [],
        )
    return (
        _RB_UNCLASSIFIED, "unclassified command",
        f"`{head}` is neither a program this gate classifies nor on PATH, and the command "
        f"names no repository path — it is REPORTED rather than skipped, because a command "
        f"that falls out of the sweep silently is how this gate would certify an ungraded step",
        [],
    )


def _rb_audit():
    """Grade every runbook under `docs/demo/`. Returns `[(cmd, verdict, category, reason, details)]`."""
    graded = []
    for path in _markdown_under(_RB_DIR):
        for cmd in _rb_extract(path):
            graded.append((cmd,) + _rb_grade(cmd))
    return graded


def _rb_render(row):
    cmd, verdict, category, reason, details = row
    head = f"  [{verdict}] {cmd['doc']}:{cmd['lineno']}  `{cmd['text']}`\n        {category}: {reason}"
    return head + "".join(f"\n            {d}" for d in details)


def _rb_coverage_gaps(path, graded):
    """A second, position-aware but otherwise DELIBERATELY STUPID scan of the raw bytes.

    It knows nothing about chains, quoting, continuations or tokenisation — it looks for
    every Makefile-defined target after the word `make`, every `*.sh` token and every
    `dir/file.py` token, and demands that the real parser produced a graded command naming
    it on that same line. Three earlier drafts of the unfrozen sibling were defeated by edits
    that changed how the document PARSES without changing what it SAYS; reconciling two
    independent parses catches those, because the stupid one cannot be hidden from.
    """
    text = path.read_text(encoding="utf-8")
    targets = set(_rb_parse_makefile())
    graded_makes, graded_paths = {}, {}
    for cmd, _v, _c, _r, _d in graded:
        ln = cmd["lineno"]
        if cmd["kind"] == "script-reference":
            graded_paths.setdefault(ln, set()).add(cmd["text"])
            continue
        toks = cmd["toks"]
        if toks and pathlib.PurePosixPath(toks[0]).name.lower() == "make":
            for target in _rb_positional(toks[1:], _RB_MAKE_VALUE_FLAGS):
                if "=" not in target:
                    graded_makes.setdefault(ln, set()).add(target)
        for tok in toks[1:]:
            bare = tok.split("::")[0]
            graded_paths.setdefault(ln, set()).add(bare)
            graded_paths[ln].add(bare[2:] if bare.startswith("./") else bare)

    gaps = []
    for idx, raw in enumerate(text.splitlines(), start=1):
        missing = []
        spans = [(m.start(), m.end()) for m in _RB_BACKTICKS.finditer(raw)]
        for m in re.finditer(r"(?<![\w\-])make\b", raw, re.IGNORECASE):
            prefix = raw[: m.start()].rstrip()
            last = prefix.split()[-1].strip("`'\"*_").lower() if prefix.split() else ""
            in_span = any(a < m.start() < b for a, b in spans)
            positioned = (
                not prefix
                or prefix.endswith(
                    ("`", "&", "|", ";", "(", "{", "$", "%", ">", "-", "*", "+", ".", ":", "'", '"')
                )
                or last in _RB_LEADINS
            )
            if not (positioned or in_span):
                continue
            skip_value = False
            for tok in raw[m.end() :].split():
                word = tok.strip("`'\".,;:()*_")
                if skip_value:
                    skip_value = False
                    continue
                if word.startswith("-"):
                    skip_value = "=" not in word and word in _RB_MAKE_VALUE_FLAGS
                    continue
                if word in targets:
                    if word not in graded_makes.get(idx, set()):
                        missing.append(f"`make {word}`")
                    continue
                break
        for hit in _RB_SCRIPT_TOKEN.finditer(raw):
            if hit.group(1) not in graded_paths.get(idx, set()):
                missing.append(hit.group(1))
        for hit in _RB_PY_TOKEN.finditer(raw):
            if hit.group(1) not in graded_paths.get(idx, set()):
                missing.append(hit.group(1))
        if missing:
            gaps.append(
                f"  {path.relative_to(REPO_ROOT)}:{idx} mentions {', '.join(sorted(set(missing)))} "
                f"but the command parser graded nothing naming it on that line"
                f"\n      | {raw.strip()[:110]}"
            )
    return gaps


@pytest.mark.epic("E8")
@pytest.mark.ticket("T-085")
def test_demo_runbook_has_the_required_sections_and_its_make_targets_exist():
    """S6 / S1 — the demo runbook declares its required sections and every command it names RESOLVES.

    Two halves, and the second one is the amendment.

    **Shape** (unchanged): the runbook carries the provisioning, auction and interview
    headings, the interview beat references the T-053 transcript flow, and it flags the
    live-LLM variant (T-085 acceptance 2).

    **Executability** (this is what replaced the definition check): every `make <target>` the
    document names is defined, has a recipe OF ITS OWN, and every script, module, program and
    prerequisite that recipe reaches resolves; every `pytest <path>` and `python -m pytest
    <path>` names a path that exists and that `pytest --collect-only` can collect; every
    `python -m <module>` resolves; every `python <path>` and `cp <src>` exists; and every
    `*.sh` named anywhere in the document — prose included — exists, carries a command, and
    parses under `bash -n`. The command list is PARSED OUT OF THE DOCUMENT, never hardcoded.

    What this gate deliberately does NOT grade is semantics. `docs/demo/e2e_live.sh`
    containing only `exit 0` resolves and passes here; what that script must DO is the
    specification of the ticket that writes it, and a gate that guessed would be grading its
    own opinion. The same holds for collection: it proves pytest can reach the module, not
    that the module proves S1. Read a green as "the runbook's steps reach real code", never
    as "the demo works".

    Known limitations, stated rather than papered over, because this file is frozen:

    * MEASURED OPEN HOLE. `uv`, `pytest` and `node` take SUBCOMMANDS in the operand slot, so
      a command headed by one of them is graded only by the repository-shaped paths it names
      (`*.sh`, or a `.py` carrying a directory component). `uv run python -m pytest <path>`
      IS fully graded — the `uv run` prelude is stripped — but `uv sync --script no_such.py`
      is not: a bare `foo.py` with no directory component is deliberately not read as a
      repository artefact, because prose mentions bare filenames all the time.
    * MEASURED OPEN HOLE. `docker compose` — and its legacy `docker-compose` spelling — is
      graded as an external tool, never against the compose config: this suite's module scope
      is stdlib-and-pytest only, so it cannot parse YAML. A runbook step naming a compose
      service that does not exist passes here.
    * `make <a> <b>` grades `<a>` unconditionally and `<b>` only while `<b>` is a target the
      Makefile really defines. `make lint types` is fully graded; ``run 'make bootstrap'
      first`` grades `bootstrap` and stops, rather than reporting a target named "first".
    * Whether `docker`, `uv`, `node` or any other external tool is INSTALLED is the
      operator's environment and grades INFO, so this frozen number does not move when the
      box it runs on changes. A recipe head that is not a repository file, not a known
      external tool and not on PATH is still a FAIL.
    * `bash -n` proves a script parses, not that it works; an empty-but-for-`exit 0` script
      passes.
    * The raw cross-check reconciles two parses that share a command-position rule. A word in
      an unrecognised position hides from both — which is why that rule is written against 36
      measured positions rather than guessed.
    """
    pages = _markdown_under(_RB_DIR)
    assert pages, "S6: the demo runbook must exist as markdown under docs/demo/"

    text = "\n".join(p.read_text(encoding="utf-8") for p in pages)
    lowered = text.lower()

    headings = [
        _normalize_token(m.group(1))
        for m in re.finditer(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$", text)
    ]
    heading_blob = " ".join(headings)
    for token, what in (
        ("provisioning", "dev-store provisioning (app install, storefront password, Bogus Gateway)"),
        ("auction", "the live-auction demo beat"),
        ("interview", "the interview -> bidding onboarding demo beat"),
    ):
        assert token in heading_blob, (
            f"the runbook needs a section heading for {what}; headings found: {headings}"
        )

    # T-085 acceptance 2 — the interview beat names the T-053 transcript flow and flags the
    # live-LLM variant.
    assert "t-053" in lowered, (
        "the interview beat must reference the T-053 transcript flow (T-085 acceptance 2)"
    )
    assert re.search(r"live[\s_\-]*llm", lowered), (
        "the interview beat must flag the live-LLM variant (T-085 acceptance 2)"
    )

    assert _RB_MAKEFILE.is_file(), "the repo root Makefile must exist for the runbook to reference"
    rules = _rb_parse_makefile()
    assert rules, (
        "the root Makefile parsed to ZERO targets. Every assertion below would then be "
        "vacuously satisfied, which is the quiet-sweep failure this suite keeps being bitten "
        "by — refusing to grade the runbook against a Makefile this gate cannot read."
    )

    graded = _rb_audit()

    # --- arming, before any conclusion is drawn ---------------------------------------
    # Three sweeps in this repo were caught PASSING because they iterated zero cases
    # (6->0 of 8, 70->0 of 79, 48->0 of 66). A loop over an empty list is green, and green
    # here would read as "the runbook is executable".
    assert graded, (
        f"the parser extracted ZERO commands from {[str(p.relative_to(REPO_ROOT)) for p in pages]}. "
        "Refusing to report PASS on an empty sweep."
    )

    resolvable = [row for row in graded if row[2] in _RB_RESOLVABLE]
    assert len(resolvable) >= _RB_MIN_RESOLVABLE, (
        f"the sweep extracted {len(graded)} command(s) but only {len(resolvable)} name anything "
        f"this gate can resolve (floor {_RB_MIN_RESOLVABLE}). A sweep padded with environment "
        f"assignments grades nothing while looking healthy."
    )

    failing_open = [row for row in graded if row[1] == _RB_INFO and row[2] in _RB_RESOLVABLE]
    assert not failing_open, (
        "these commands name something in this repository but were graded INFO rather than "
        "PASS/FAIL — the gate is failing OPEN on them:\n"
        + "\n".join(_rb_render(r) for r in failing_open)
    )

    categories = {row[2] for row in graded}
    for required, label in (
        ("make target", "any `make <target>` command"),
        ("pytest invocation", "any pytest invocation"),
        ("shell script named in the runbook", "any `*.sh` reference"),
    ):
        assert required in categories, (
            f"the sweep found no {label} under docs/demo/; the parser and the documents have "
            f"drifted apart, so a healthy-looking count would be grading something else. "
            f"Categories found: {sorted(categories)}"
        )

    gaps = []
    for path in pages:
        gaps.extend(_rb_coverage_gaps(path, [row for row in graded if row[0]["doc"] == str(path.relative_to(REPO_ROOT))]))
    assert not gaps, (
        "a raw-text scan found repository artefacts the command parser never graded — a step "
        "is invisible to it, so the verdict below would be green over an ungraded command:\n"
        + "\n".join(gaps)
    )

    # --- the executability verdict ------------------------------------------------------
    failures = [row for row in graded if row[1] == _RB_FAIL]
    passes = [row for row in graded if row[1] == _RB_PASS]
    infos = [row for row in graded if row[1] == _RB_INFO]
    unclassified = [row for row in graded if row[1] == _RB_UNCLASSIFIED]
    tally = (
        f"(swept {len(graded)} commands out of "
        f"{[str(p.relative_to(REPO_ROOT)) for p in pages]}; {len(passes)} PASS, {len(failures)} "
        f"FAIL, {len(infos)} INFO, {len(unclassified)} UNCLASSIFIED)"
    )
    # The UNCLASSIFIED bucket is printed on BOTH paths. A command this parser could not grade
    # is a hole in the sweep, and the one thing it must never be is invisible.
    unclassified_report = (
        "\n\n--- UNCLASSIFIED: named nothing this gate could resolve ---\n"
        + "\n".join(_rb_render(r) for r in unclassified)
        if unclassified
        else ""
    )

    assert not failures, (
        f"S6 / S1: {len(failures)} of {len(graded)} commands the demo runbook instructs an "
        f"operator to run do not resolve — the runbook is certified prose, not a procedure:\n\n"
        + "\n".join(_rb_render(r) for r in failures)
        + f"\n\n--- the {len(passes) + len(infos)} that do ---\n"
        + "\n".join(_rb_render(r) for r in passes + infos)
        + unclassified_report
        + f"\n\n{tally}"
    )


# ------------------------------------------------------------------------------------
# 7 — R12 / D18: the approved manifest publishes the typed, exhaustive claim-type table
#     (extends test 2 — same document, the half that routes verification into trust)
# ------------------------------------------------------------------------------------
@pytest.mark.epic("E8")
@pytest.mark.ticket("T-080")
def test_manifest_publishes_a_typed_exhaustive_claim_type_to_dimension_table():
    """R12 / D18 — every published claim type maps to one of the six dimensions, by hand.

    The table is ground truth for routing, so it is approved with the rest of the manifest
    rather than inferred by the trust engine at runtime. `test_e6_trust.py` grades the engine
    against exactly this table; here we only check that the approved document is complete and
    that product facts are not being smuggled back into promise dimensions.
    """
    manifest = _manifest()
    table = _claim_type_dimensions(manifest)

    for claim_type in sorted(table):
        where = f"manifest.claim_type_dimensions[{claim_type!r}]"
        assert isinstance(claim_type, str) and claim_type.strip(), (
            f"{where}: claim types must be non-empty strings"
        )
        dimension = table[claim_type]
        assert isinstance(dimension, str) and dimension in TRUST_DIMENSIONS, (
            f"{where} = {dimension!r} is not one of the six trust dimensions "
            f"{sorted(TRUST_DIMENSIONS)}"
        )

    missing = [t for t in PUBLISHED_CLAIM_TYPES if t not in table]
    assert not missing, (
        "the approved mapping must be EXHAUSTIVE over the published claim-type vocabulary — "
        f"a type with no dimension has no route into trust at all. Unmapped: {missing}"
    )

    for claim_type in PRODUCT_FACT_CLAIM_TYPES:
        assert table[claim_type] == CATALOG_DIM, (
            f"product-fact claim type {claim_type!r} is mapped to {table[claim_type]!r}. A "
            f"claim about what the goods ARE belongs on {CATALOG_DIM!r}; mapping it into a "
            "transaction dimension corrupts that dimension's meaning."
        )

    for claim_type, dimension in sorted(OFFER_FACT_CLAIM_DIMENSIONS.items()):
        assert table[claim_type] == dimension, (
            f"offer-integrity claim type {claim_type!r} must keep its transaction dimension "
            f"{dimension!r}, got {table[claim_type]!r}"
        )

    # ...and the scripted dishonest store must actually exercise the new dimension, or the
    # trust engine is never graded on catalog honesty by the one scenario A3 rests on.
    store = _require(manifest, "dishonest_store", dict, "manifest")
    behaviours = _require(store, "behaviours", list, "manifest.dishonest_store")
    scripted_dims = {
        str(b.get("dim")) for b in behaviours if isinstance(b, dict) and "dim" in b
    }
    assert CATALOG_DIM in scripted_dims, (
        f"no scripted dishonest behaviour lands on {CATALOG_DIM!r} (dims scripted: "
        f"{sorted(scripted_dims)}). S2's dishonest store must lie about the goods, not only "
        "about the deal — otherwise the sixth dimension is never exercised end to end."
    )


# ------------------------------------------------------------------------------------
# 8 — S8 / R12: the golden claims are typed, and include a false product fact
#     (extends test 3 — same document, the half that makes an outcome routable)
# ------------------------------------------------------------------------------------
@pytest.mark.epic("E8")
@pytest.mark.ticket("T-080")
def test_golden_claims_are_typed_and_cover_true_and_false_product_facts():
    """S8 / R12 — every approved claim carries a mapped `claim_type`, product facts included.

    A verification outcome can only reach a trust dimension if the claim it decided is typed.
    Requiring the type on the *approved* side (rather than trusting whatever the verifier
    emits) keeps the routing graded against ground truth like everything else in this file.
    """
    manifest = _manifest()
    table = _claim_type_dimensions(manifest)
    golden = _golden_set_pitches(manifest)

    typed = 0
    product_fact_statuses = {}
    for i, pitch in enumerate(golden):
        where = f"{GOLDEN_SET_REL}#pitches[{i}]"
        for j, claim in enumerate(_require(pitch, "claims", list, where)):
            cwhere = f"{where}.claims[{j}]"
            claim_type = _nonempty_str(claim, "claim_type", cwhere)
            assert claim_type in table, (
                f"{cwhere}.claim_type = {claim_type!r} is not in the approved "
                "manifest.claim_type_dimensions table, so this claim's outcome has no "
                f"typed route into trust (mapped types: {sorted(table)})"
            )
            typed += 1
            status = _nonempty_str(claim, "expected_status", cwhere).strip().lower()
            if table[claim_type] == CATALOG_DIM:
                product_fact_statuses.setdefault(status, []).append(
                    f"{pitch.get('pitch_id')}#{claim.get('claim_ref')}"
                )

    assert typed > 0, f"{GOLDEN_SET_REL} labelled no claims at all"
    assert product_fact_statuses.get("contradicted"), (
        "the approved golden set contains no FALSE product-fact claim — the case the sixth "
        f"dimension exists for (a claim about the goods that the catalog contradicts). "
        f"Product-fact claims found by status: "
        f"{ {k: len(v) for k, v in product_fact_statuses.items()} }"
    )
    assert product_fact_statuses.get("verified"), (
        "the approved golden set contains no TRUE product-fact claim, so a verifier that "
        f"contradicted every product fact would still be graded green on {CATALOG_DIM!r}"
    )
