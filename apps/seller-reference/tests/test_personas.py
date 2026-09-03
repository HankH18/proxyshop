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


def _plain(obj, _depth: int = 0):
    """The frozen suite's normalizer — a FAITHFUL copy, not a miniature.

    This used to be an abridged version, and the abridgement was load-bearing in the wrong
    direction: it lacked the frozen walker's `obj.__dict__` fallback, so it stopped at
    `str(obj)` exactly where the frozen walker descends into a plain object's attributes.
    `test_the_pitch_carries_no_provenance_bearing_node_beyond_its_claims` exists to pre-empt
    the frozen rule, and a normalizer weaker than that rule cannot do it — redesigning
    `PersonaPitch` into a non-dataclass would have kept this file green while the frozen
    criterion started counting nodes it had never seen. Kept byte-comparable with
    `.swarm-loop/acceptance/test_e4_store_agent.py::_plain` on purpose.
    """
    if _depth > 40:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_plain(v, _depth + 1) for v in obj]
    if isinstance(obj, (set, frozenset)):
        items = [_plain(v, _depth + 1) for v in obj]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, default=str))
    if isinstance(obj, dict):
        return {str(k): _plain(v, _depth + 1) for k, v in obj.items()}
    for attr in ("model_dump", "_asdict", "to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _plain(fn(), _depth + 1)
            except Exception:  # pragma: no cover - fall through to the next shape
                pass
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields:
        return {str(f): _plain(getattr(obj, f, None), _depth + 1) for f in fields}
    namespace = getattr(obj, "__dict__", None)
    if isinstance(namespace, dict) and namespace:
        return {
            str(k): _plain(v, _depth + 1)
            for k, v in namespace.items()
            if not str(k).startswith("_")
        }
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
        assert span is not None, f"claim {claim.key!r} carries no source_span at all"
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


# ---------------------------------------------------------------------------------------
# Regression teeth. An adversarial review mutated the module five ways and every test above
# this line stayed green under all five: a real wall-clock `observed_at`; an `observed_at`
# hard-coded so the approval instant is never read; `claim_id` set to None on every claim;
# `provenance.ref` collapsed to the bare `pitch_ref` so all five claims cite one pointer; and
# the script emitted in REVERSE manifest order. Each test below fails under exactly the
# mutation it names — verified by re-running the mutation after writing it.
# ---------------------------------------------------------------------------------------


def test_observed_at_is_the_approval_instant_and_not_a_clock(intent, manifest) -> None:
    """The determinism claim, asserted rather than assumed.

    `test_the_pitch_is_deterministic` compares two builds in one process, so ANY clock coarser
    than a microsecond survives it. This pins the value to the document instead: `observed_at`
    is the instant the human approved the script being replayed, so replaying an episode a
    year later reproduces the same bytes.
    """
    from seller_reference.personas import build_persona

    approved_at = manifest["approval"]["approved_at"]
    assert approved_at, "the approved manifest records no approval instant"
    for claim in build_persona("aggressive").pitch(intent).claims:
        assert claim.provenance.observed_at == approved_at


def test_a_manifest_with_no_approval_falls_back_to_a_constant_not_a_clock(intent, manifest) -> None:
    """The fallback path must also be reproducible — a missing approval is not a licence to."""
    from seller_reference.personas import build_persona

    undated = json.loads(json.dumps(manifest))
    undated.pop("approval")
    first = build_persona("aggressive", manifest=undated).pitch(intent)
    second = build_persona("aggressive", manifest=undated).pitch(intent)
    observed = {c.provenance.observed_at for c in first.claims}
    assert observed == {"1970-01-01T00:00:00Z"}, observed
    assert _plain(first) == _plain(second)


def test_each_claim_carries_its_own_claim_id_and_evidence_pointer(intent) -> None:
    """D25: one claim, one identity, one pointer to where it was said.

    Nothing else in this file reads `claim_id` at all, and `provenance.ref` was asserted only
    for truthiness — so deleting every claim id, or pointing all five claims at the same ref,
    was invisible. Both defeat the ledger's ability to tell two statements apart.
    """
    from seller_reference.personas import build_persona

    claims = build_persona("aggressive").pitch(intent).claims
    ids = [c.claim_id for c in claims]
    refs = [c.provenance.ref for c in claims]
    assert all(ids), f"a claim carries no claim_id: {ids}"
    assert len(set(ids)) == len(ids), f"claim_id collision across scripted claims: {ids}"
    assert len(set(refs)) == len(refs), f"claims share an evidence pointer: {refs}"
    for claim, ref in zip(claims, refs, strict=True):
        assert ref.endswith(f"#{claim.key}"), ref


def test_claims_are_emitted_in_the_manifests_own_order(intent, manifest) -> None:
    """`PersonaPitch.claims` is documented "in the manifest's own order" — so assert the order.

    Every other key comparison in this file and in the frozen criterion is a SET comparison,
    which reversal survives. Order is what makes `pitch.text` read as the sentence the script
    describes, and the `source_span` offsets follow it.
    """
    from seller_reference.personas import build_persona

    scripted = manifest["personas"]["aggressive"]["scripted_claims"]
    emitted = build_persona("aggressive").pitch(intent).claims
    assert [c.key for c in emitted] == [c["key"] for c in scripted]
    assert [c.value for c in emitted] == [c["value"] for c in scripted]


def test_a_caller_cannot_rewrite_the_approved_script_through_the_persona(intent) -> None:
    """A3: the persona owns no answer key, and it must not be given one after the fact.

    `@dataclass(frozen=True)` freezes the attribute binding, not the dicts behind it, so with
    a plain-dict script `persona.script[0]["value"] = ...` silently rewrote the approved claim
    for every later `pitch()` — the persona supplying its own answer key, one assignment away.
    """
    from seller_reference.personas import build_persona

    persona = build_persona("aggressive")
    before = persona.pitch(intent).claims[0].value
    with pytest.raises(TypeError):
        persona.script[0]["value"] = "MUTATED BY CALLER"  # type: ignore[index]
    assert persona.pitch(intent).claims[0].value == before


def test_an_unusable_script_is_refused_at_build_time_not_pitch_time(intent, manifest) -> None:
    """The module promises build-time refusal; `build_persona` alone must raise.

    Deliberately does NOT call `.pitch()`. The existing provenance test wraps both calls in one
    `pytest.raises`, so it cannot tell a build-time refusal from a pitch-time crash — which is
    the distinction the module docstring makes and the one a caller needs, since a pitch-time
    failure surfaces as a `contracts` exception nobody here has reason to catch.
    """
    from seller_reference.personas import PersonaError, UnharnessedProvenanceError, build_persona

    def mutated(fn):
        doc = json.loads(json.dumps(manifest))
        fn(doc["personas"]["aggressive"]["scripted_claims"])
        return doc

    forged = mutated(lambda cl: cl[0].__setitem__("provenance", {"source": "scraped"}))
    with pytest.raises(UnharnessedProvenanceError):
        build_persona("aggressive", manifest=forged)

    bare_source = mutated(lambda cl: cl[0].__setitem__("source", "envelope_rule"))
    with pytest.raises(UnharnessedProvenanceError):
        build_persona("aggressive", manifest=bare_source)

    for label, mutate in (
        ("structured value", lambda cl: cl[0].__setitem__("value", {"provenance": {"s": 1}})),
        ("list value", lambda cl: cl[0].__setitem__("value", [{"provenance": "scraped"}])),
        ("uncanonicalizable value", lambda cl: cl[0].__setitem__("value", float("inf"))),
        ("duplicate key", lambda cl: cl.append({"key": cl[0]["key"], "value": "9.99"})),
        ("unpublished claim_type", lambda cl: cl[0].__setitem__("claim_type", "not_a_type")),
    ):
        with pytest.raises(PersonaError):
            build_persona("aggressive", manifest=mutated(mutate)), label


def test_a_structured_scripted_value_cannot_smuggle_an_extra_provenance_node(
    intent, manifest
) -> None:
    """The invariant the frozen criterion actually counts on.

    `contracts.Claim.value` is typed `Any`, so a structured value rides through `model_dump()`
    intact and the frozen walker — which counts EVERY reachable node carrying a `provenance`
    key — sees a sixth, keyless claim. Measured before the fix: `extra=['none']`. The module
    refuses the script instead, so the invariant `PersonaPitch` documents is enforced rather
    than merely true of today's manifest.
    """
    from seller_reference.personas import PersonaError, build_persona

    smuggled = json.loads(json.dumps(manifest))
    smuggled["personas"]["aggressive"]["scripted_claims"][0]["value"] = {
        "amount": "18.50",
        "provenance": {"source": "scraped"},
    }
    with pytest.raises(PersonaError):
        build_persona("aggressive", manifest=smuggled)

    # And the property itself, on the approved script: node count == claim count.
    pitch = build_persona("aggressive").pitch(intent)
    assert len(_claims_in(pitch)) == len(pitch.claims)
