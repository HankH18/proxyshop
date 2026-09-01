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
  ``dishonest_store {store_id, behaviours[{kind, dim, type}]}``,
  ``expected_trust_trajectory[{episode, score, tolerance}]``,
  ``golden_set {path, sha256, count}`` and ``approval {approver, approved_at, artifact,
  content_hash}``.
* ``fixtures/golden/golden_set.json`` — one object with ``pitches[]``, each
  ``{pitch_id, text, gates[], catalog_snapshot, claims[{claim_ref, text, key, value,
  expected_status}]}``.

The golden label key is ``expected_status`` everywhere — never ``status``, which is what the
*verifier* produces and must stay visibly distinct from what the human approved.

Authoring rules (see `.swarm-loop/acceptance/README.md`): every product import happens
INSIDE the test body, and every test carries `epic` + `ticket` markers. Module scope holds
stdlib and pytest only.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re

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

#: The five trust dimensions every manifest behaviour must name (DESIGN §Trust math).
TRUST_DIMENSIONS = frozenset(
    {"price_honored", "discount_honored", "shipped_on_time", "not_returned", "feedback_match"}
)

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
            f"{where}.dim must be one of the five trust dimensions "
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
# 6 — S6 / S1: the demo runbook is executable prose with real make targets behind it
# ------------------------------------------------------------------------------------
@pytest.mark.epic("E8")
@pytest.mark.ticket("T-085")
def test_demo_runbook_has_the_required_sections_and_its_make_targets_exist():
    """S6 / S1 — the demo runbook declares its required sections and every make target it names exists."""
    demo_dir = REPO_ROOT / "docs" / "demo"
    pages = _markdown_under(demo_dir)
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

    # Every `make <target>` the runbook tells a human to run must really be defined.
    referenced = set()
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("$").strip()
        if not line.startswith("make "):
            continue
        for word in line[len("make "):].split():
            if word.startswith("-") or "=" in word:
                continue
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", word):
                referenced.add(word)
            break

    assert "e2e-live" in referenced, (
        "S6: the runbook must document the `make e2e-live` procedure; make targets it "
        f"references: {sorted(referenced)}"
    )

    makefile = REPO_ROOT / "Makefile"
    assert makefile.is_file(), "the repo root Makefile must exist for the runbook to reference"
    defined = set(
        re.findall(
            r"(?m)^([A-Za-z0-9][A-Za-z0-9_.\-/]*)\s*:(?!=)", makefile.read_text(encoding="utf-8")
        )
    )
    missing = sorted(referenced - defined)
    assert not missing, (
        "T-085 acceptance 1: the runbook references make targets that the Makefile does not "
        f"define: {missing} (defined: {sorted(defined)})"
    )
