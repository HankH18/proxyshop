"""T-045 — the unharnessed reference seller personas (R18 / S8 / A3).

These are the ticket-owned tests for `seller_reference.personas`. They exist beside the
frozen acceptance criterion (`.swarm-loop/acceptance/test_e4_store_agent.py::
test_aggressive_persona_emits_exactly_the_manifest_scripted_claims`), and they deliberately
assert MORE than it does:

* the frozen goal checks the aggressive persona's keys, values and `seller_asserted`
  provenance against the manifest;
* these check that the script is *read* from the human-approved manifest rather than
  transcribed into product code (A3 — a persona that supplied its own answer key would make
  the criterion circular), that the honest control persona is driven by the same machinery,
  that a pitch is deterministic, and that the persona physically CANNOT stamp a hook
  provenance onto a claim (R8/S5 — a seller that could self-certify `scraped` or
  `envelope_rule` would make the hosted-path provenance wall vacuous).

Offline: no network, no LLM, no database. The only I/O is reading `fixtures/manifest.json`
through `fixtures.manifest.load_manifest`, which is the approved document itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _plain(obj):
    """The frozen suite's normalizer, in miniature: product objects -> plain Python."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    for attr in ("model_dump", "_asdict", "to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            return _plain(fn())
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields:
        return {str(f): _plain(getattr(obj, f, None)) for f in fields}
    return str(obj)


def _walk(plain):
    stack = [plain]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _claims_in(obj) -> list:
    """Every provenance-bearing node reachable from `obj` — the frozen suite's own rule."""
    return [n for n in _walk(_plain(obj)) if isinstance(n.get("provenance"), (dict, str))]


@pytest.fixture(scope="module")
def manifest() -> dict:
    from fixtures.manifest import load_manifest

    return load_manifest()


@pytest.fixture(scope="module")
def intent(manifest: dict) -> dict:
    return manifest["fixture_intent"]


def test_module_resolves_inside_this_workspace() -> None:
    """The module under test is this member's own file, not a stray copy on sys.path."""
    from seller_reference import personas

    resolved = Path(personas.__file__).resolve()
    assert resolved.is_relative_to((REPO_ROOT / "apps/seller-reference/src").resolve()), resolved


def test_aggressive_pitch_is_exactly_the_manifest_script(intent, manifest) -> None:
    """R18/S8: exactly the scripted claims — no more, no fewer — at the scripted values."""
    from seller_reference.personas import build_persona

    scripted = manifest["personas"]["aggressive"]["scripted_claims"]
    emitted = _claims_in(build_persona("aggressive").pitch(intent))

    assert {c["key"] for c in emitted} == {c["key"] for c in scripted}
    by_key = {c["key"]: c for c in emitted}
    for claim in scripted:
        assert by_key[claim["key"]]["value"] == claim["value"]
        assert by_key[claim["key"]]["claim_type"] == claim["claim_type"]


def test_every_emitted_claim_is_seller_asserted_at_the_published_rank(intent) -> None:
    """R8: the unharnessed seller has exactly one provenance available to it."""
    from contracts import PROVENANCE_AUTHORITY_RANK
    from seller_reference.personas import build_persona

    emitted = _claims_in(build_persona("aggressive").pitch(intent))
    assert emitted, "the persona emitted nothing"
    for claim in emitted:
        assert claim["provenance"]["source"] == "seller_asserted"
        assert claim["provenance"]["authority_rank"] == PROVENANCE_AUTHORITY_RANK["seller_asserted"]
        assert claim["provenance"]["ref"]
        assert claim["provenance"]["observed_at"]


def test_the_pitch_carries_no_provenance_bearing_node_beyond_its_claims(intent) -> None:
    """The frozen goal counts EVERY provenance-bearing node, so the pitch may carry no others."""
    from seller_reference.personas import build_persona

    pitch = build_persona("aggressive").pitch(intent)
    assert len(_claims_in(pitch)) == len(pitch.claims)


def test_the_script_is_read_from_the_manifest_not_transcribed(intent, manifest) -> None:
    """A3: swap the approved script and the pitch follows it. The persona owns no answer key."""
    from seller_reference.personas import build_persona

    swapped = json.loads(json.dumps(manifest))
    swapped["personas"]["aggressive"]["scripted_claims"] = [
        {"key": "warranty", "value": "lifetime", "claim_type": "warranty", "truthful": False}
    ]
    emitted = _claims_in(build_persona("aggressive", manifest=swapped).pitch(intent))
    assert [(c["key"], c["value"]) for c in emitted] == [("warranty", "lifetime")]


def test_the_honest_control_persona_runs_on_the_same_machinery(intent, manifest) -> None:
    """S2 needs a non-sinking control: `honest` is built the same way from its own script."""
    from seller_reference.personas import build_persona

    scripted = manifest["personas"]["honest"]["scripted_claims"]
    emitted = _claims_in(build_persona("honest").pitch(intent))
    assert {c["key"] for c in emitted} == {c["key"] for c in scripted}
    assert {c["provenance"]["source"] for c in emitted} == {"seller_asserted"}


def test_a_persona_cannot_stamp_a_hook_provenance(intent, manifest) -> None:
    """R8/S5: a seller that could self-certify `scraped` would make the provenance wall vacuous."""
    from seller_reference.personas import UnharnessedProvenanceError, build_persona

    forged = json.loads(json.dumps(manifest))
    forged["personas"]["aggressive"]["scripted_claims"][0]["provenance"] = {"source": "scraped"}
    with pytest.raises(UnharnessedProvenanceError):
        build_persona("aggressive", manifest=forged).pitch(intent)


def test_the_pitch_is_deterministic(intent) -> None:
    """S4: same persona, same intent, byte-identical pitch. No clock, no randomness."""
    from seller_reference.personas import build_persona

    first = _plain(build_persona("aggressive").pitch(intent))
    second = _plain(build_persona("aggressive").pitch(intent))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_an_unscripted_persona_name_raises(intent) -> None:
    """An unknown persona is a loud error, never an empty pitch that silently passes S8."""
    from seller_reference.personas import UnknownPersonaError, build_persona

    with pytest.raises(UnknownPersonaError):
        build_persona("no-such-persona")


def test_every_claims_source_span_indexes_its_own_value_in_the_pitch_text(intent) -> None:
    """D25: a `source_span` that points nowhere makes the claim unverifiable.

    Nothing above this line reads `pitch.text` at all, so the offsets `_render` computes were
    free to be arbitrary and every other assertion in this file — and the frozen criterion —
    would still have been green. A `VerificationResult` addresses a claim through its
    `claim_ref`/span, so an off-by-one here is only discovered by the verifier that cannot
    quote the sentence it is supposed to be checking.
    """
    from seller_reference.personas import build_persona

    pitch = build_persona("aggressive").pitch(intent)
    assert pitch.claims, "the persona emitted nothing"
    for claim in pitch.claims:
        span = claim.source_span
        assert span.pitch_ref == pitch.pitch_ref
        assert pitch.text[span.start : span.end] == str(claim.value), (
            f"claim {claim.key!r} span ({span.start},{span.end}) selects "
            f"{pitch.text[span.start : span.end]!r}, not its value {claim.value!r}"
        )


def test_every_scripted_persona_answers_the_fixture_intent_concurrently(intent, manifest) -> None:
    """T-045 acceptance 1: every scripted persona answers the one fixture intent at once.

    The personas are scripted and LLM-free, so the "LLM doubles" of the acceptance criterion
    are vacuously in place — there is no model call to stub. What is worth asserting is that a
    pitch holds no cross-persona shared state: two personas built and pitched from separate
    threads must produce exactly what each produces alone, or the simulator's concurrent
    episode schedule would be reading one seller's claims out of another's pitch.
    """
    from concurrent.futures import ThreadPoolExecutor

    from seller_reference.personas import build_persona, persona_names

    names = persona_names()
    assert set(names) == set(manifest["personas"]), names

    def answer(name: str):
        return _plain(build_persona(name).pitch(intent))

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        concurrent = list(pool.map(answer, names, timeout=30))

    serial = [answer(name) for name in names]
    assert concurrent == serial
    for name, pitch in zip(names, concurrent, strict=True):
        scripted = manifest["personas"][name]["scripted_claims"]
        assert {c["key"] for c in _claims_in(pitch)} == {c["key"] for c in scripted}
