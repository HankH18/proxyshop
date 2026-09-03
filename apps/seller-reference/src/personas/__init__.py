"""T-045 — the unharnessed reference seller personas (R18 / S8 / A3).

An "unharnessed" seller is one the platform does not host: it runs its own agent, speaks
through the external door, and nothing it says has been through a tool hook. This module is
the reference implementation of two such sellers — the adversarial `aggressive` persona and
the `honest` control — and it exists so the trust engine has something real to be graded
against.

Two properties are the whole point, and both are enforced here rather than assumed.

**The script is ground truth, and it lives somewhere else (SPEC A3).**
"the trust engine catches the dishonest store" is a circular claim if the dishonest
behaviours come out of the trust engine's own config — and it is *equally* circular if they
come out of the persona's own source code, because then the persona and the grader are
written by the same hand against the same idea of what a lie looks like. So this module
holds no claim text at all. Every claim it emits is read from
``fixtures/manifest.json`` — the document a human approved, digest-pinned — through
:func:`fixtures.manifest.load_manifest`, which refuses to hand over a manifest whose body has
drifted from the digest the approval covers. Change the approved script and the pitch changes
with it; there is no second copy to drift.

**The persona has exactly one provenance available to it (R8 / S5).**
``seller_asserted`` is the only :class:`contracts.ProvenanceSource` an external agent can
assert freely — it is what ``contracts.NON_HOOK_PROVENANCE_SOURCES`` names, and the hosted
path rejects it outright. The other six are HOOK provenances: a hosted store-agent mints them
by calling a tool hook, and that is precisely what makes the hosted path checkable. A
reference seller that could stamp ``scraped`` or ``envelope_rule`` onto its own claims would
make R8 vacuous — the adversary would simply self-certify. So a scripted claim that names any
other source is refused at build time with :class:`UnharnessedProvenanceError` rather than
quietly downgraded: a script asking for a provenance this persona must not have is a defect in
the script, and silently rewriting it would hide the one thing the criterion is watching for.

Determinism. A pitch has no clock and no randomness in it. ``observed_at`` is the instant the
human approved the script being replayed (``manifest.approval.approved_at``), not "now", so
two runs of the same persona against the same intent produce byte-identical pitches — which
is what lets S4's seeded-determinism criterion and the simulator's replay schedule mean
anything.

Offline by construction: no network, no LLM, no database. Reading the approved manifest off
disk is the only I/O.
"""

from __future__ import annotations

__all__ = [
    "PERSONA_PROVENANCE_SOURCE",
    "Persona",
    "PersonaError",
    "PersonaPitch",
    "UnharnessedProvenanceError",
    "UnknownPersonaError",
    "build_persona",
    "persona_names",
]

import copy
from dataclasses import dataclass
from typing import Any

from contracts import (
    Claim,
    ClaimSourceSpan,
    ClaimType,
    Provenance,
    ProvenanceSource,
    canonical_authority_rank,
    claim_id,
)

from fixtures.manifest import load_manifest

#: The ONLY provenance an unharnessed seller may assert. See the module docstring: the other
#: six sources are hook-minted, and a persona able to stamp one would make R8 vacuous.
PERSONA_PROVENANCE_SOURCE = ProvenanceSource.seller_asserted

#: Fallback `observed_at` for a manifest with no recorded approval instant. A pinned constant
#: rather than a wall-clock read, because a pitch must be reproducible: see the module
#: docstring on determinism.
_UNDATED_SCRIPT_OBSERVED_AT = "1970-01-01T00:00:00Z"

#: Separator between the per-claim segments of a pitch's prose. One character wide so the
#: `source_span` offsets stay easy to reason about.
_SEGMENT_SEPARATOR = " "


class PersonaError(ValueError):
    """Base class for every way a persona script fails to be usable."""


class UnknownPersonaError(PersonaError):
    """The approved manifest scripts no persona under the requested name.

    Loud rather than empty: a persona that answered an unknown name with a pitch carrying no
    claims would satisfy "emits nothing it was not scripted to emit" trivially, and S8 would
    pass against a seller that does not exist.
    """


class UnharnessedProvenanceError(PersonaError):
    """A scripted claim asked for a provenance an unharnessed seller cannot assert."""


@dataclass(frozen=True)
class PersonaPitch:
    """One persona's answer to one intent.

    Every field is plain data. Nothing here carries a ``provenance`` key except the entries of
    :attr:`claims`, which matters: the frozen acceptance criterion counts *every*
    provenance-bearing node reachable from a pitch, so a stray one — an echoed intent, a
    nested envelope — would read as an extra claim the persona was never scripted to emit.
    """

    #: The manifest key this pitch was built from (`aggressive`, `honest`).
    persona: str
    #: The store the persona speaks for, per the manifest's persona block.
    store_id: str | None
    #: The intent answered, by id.
    intent_id: str | None
    #: The stable pointer every claim's `source_span` and `provenance.ref` hang off.
    pitch_ref: str
    #: The prose the seller "said". Each claim's `source_span` indexes into exactly this text.
    text: str
    #: The scripted claims, in the manifest's own order.
    claims: tuple[Claim, ...]


@dataclass(frozen=True)
class Persona:
    """A scripted reference seller. Build one with :func:`build_persona`."""

    name: str
    store_id: str | None
    description: str | None
    #: The approved script, deep-copied at build time so a caller cannot mutate it afterwards.
    script: tuple[dict[str, Any], ...]

    def pitch(self, intent: Any) -> PersonaPitch:
        """Answer `intent` with exactly the scripted claims — no more, no fewer.

        `intent` may be a :class:`contracts.Intent`, a mapping, or anything exposing an
        ``intent_id``; only the id is read, and only to address the pitch. The persona does not
        *respond* to the intent's contents, and that is deliberate: a scripted adversary whose
        claims varied with the question would not be replayable, and the simulator's episode
        schedule (and therefore the manifest's expected trust trajectory) depends on replay.
        """
        intent_id = _intent_id(intent)
        pitch_ref = _pitch_ref(self.name, self.store_id, intent_id)
        text, spans = _render(self.script, pitch_ref)
        observed_at = self._observed_at
        claims = tuple(
            _build_claim(entry, pitch_ref=pitch_ref, span=span, observed_at=observed_at)
            for entry, span in zip(self.script, spans, strict=True)
        )
        return PersonaPitch(
            persona=self.name,
            store_id=self.store_id,
            intent_id=intent_id,
            pitch_ref=pitch_ref,
            text=text,
            claims=claims,
        )

    #: Set by :func:`build_persona`; see the module docstring on determinism.
    _observed_at: str = _UNDATED_SCRIPT_OBSERVED_AT


def persona_names(manifest: Any = None) -> tuple[str, ...]:
    """Every persona the approved manifest scripts, sorted."""
    return tuple(sorted(_personas_block(_resolve_manifest(manifest))))


def build_persona(name: str, *, manifest: Any = None) -> Persona:
    """Build the persona the approved manifest scripts under `name`.

    `manifest` is for tests and for grading a *foreign* manifest; left unset, the persona reads
    the repository's own approved document, digests and all.

    Raises :class:`UnknownPersonaError` if no such persona is scripted, and
    :class:`UnharnessedProvenanceError` if the script asks for a provenance an unharnessed
    seller may not assert.
    """
    document = _resolve_manifest(manifest)
    personas = _personas_block(document)
    if name not in personas:
        raise UnknownPersonaError(
            f"the approved fixture manifest scripts no persona named {name!r}; "
            f"it scripts {sorted(personas)}"
        )
    block = personas[name]
    if not isinstance(block, dict):
        raise PersonaError(
            f"manifest.personas.{name} must be an object, got {type(block).__name__}"
        )

    scripted = block.get("scripted_claims") or block.get("claims")
    if not isinstance(scripted, list) or not scripted:
        raise PersonaError(
            f"manifest.personas.{name} scripts no claims; a persona with no script has no "
            "ground truth to be graded against"
        )

    script = tuple(
        _validate_entry(entry, persona=name, index=i) for i, entry in enumerate(scripted)
    )
    return Persona(
        name=name,
        store_id=_optional_str(block.get("store_id")),
        description=_optional_str(block.get("description")),
        script=script,
        _observed_at=_script_observed_at(document),
    )


# --- internals -------------------------------------------------------------------------


def _resolve_manifest(manifest: Any) -> dict[str, Any]:
    if manifest is None:
        return load_manifest()
    if not isinstance(manifest, dict):
        raise PersonaError(f"manifest must be a mapping, got {type(manifest).__name__}")
    return manifest


def _personas_block(document: dict[str, Any]) -> dict[str, Any]:
    personas = document.get("personas")
    if not isinstance(personas, dict) or not personas:
        raise PersonaError(
            "the fixture manifest declares no `personas` block — the persona scripts are "
            "ground truth and live only there (SPEC A3)"
        )
    return personas


def _script_observed_at(document: dict[str, Any]) -> str:
    approval = document.get("approval")
    approved_at = approval.get("approved_at") if isinstance(approval, dict) else None
    text = _optional_str(approved_at)
    return text or _UNDATED_SCRIPT_OBSERVED_AT


def _validate_entry(entry: Any, *, persona: str, index: int) -> dict[str, Any]:
    """Check one scripted claim and return a private copy of it."""
    where = f"manifest.personas.{persona}.scripted_claims[{index}]"
    if not isinstance(entry, dict):
        raise PersonaError(f"{where} must be an object, got {type(entry).__name__}")

    key = _optional_str(entry.get("key"))
    if not key:
        raise PersonaError(f"{where} must carry a non-empty `key`")
    if "value" not in entry:
        raise PersonaError(f"{where} must carry a `value` — that is what gets verified")

    claim_type = entry.get("claim_type")
    if claim_type is not None:
        try:
            ClaimType(str(claim_type))
        except ValueError as exc:
            raise PersonaError(
                f"{where}.claim_type {claim_type!r} is not one of the published claim types; "
                "an unmapped type has no route into a trust dimension (D53)"
            ) from exc

    _reject_hook_provenance(entry, where=where)
    return copy.deepcopy(entry)


def _reject_hook_provenance(entry: dict[str, Any], *, where: str) -> None:
    """Refuse a script that asks this seller to stamp a provenance it cannot have.

    Refuse, never rewrite. Downgrading the source to `seller_asserted` would make the script
    and the emitted claim disagree in exactly the direction R8 exists to catch, and it would do
    so silently.
    """
    for field in ("provenance", "source"):
        raw = entry.get(field)
        if raw is None:
            continue
        source = raw.get("source") if isinstance(raw, dict) else raw
        if source is None:
            continue
        source = str(getattr(source, "value", source))
        if source != PERSONA_PROVENANCE_SOURCE.value:
            raise UnharnessedProvenanceError(
                f"{where} asks for provenance source {source!r}, but an unharnessed seller can "
                f"only assert {PERSONA_PROVENANCE_SOURCE.value!r}. The other sources are minted "
                "by tool hooks, and a persona able to stamp one would let the adversary "
                "self-certify (R8/S5)."
            )


def _intent_id(intent: Any) -> str | None:
    if isinstance(intent, dict):
        return _optional_str(intent.get("intent_id"))
    return _optional_str(getattr(intent, "intent_id", None))


def _pitch_ref(persona: str, store_id: str | None, intent_id: str | None) -> str:
    return f"pitch:{store_id or 'unknown-store'}:{persona}:{intent_id or 'unknown-intent'}"


def _render(
    script: tuple[dict[str, Any], ...], pitch_ref: str
) -> tuple[str, tuple[ClaimSourceSpan, ...]]:
    """Render the script as prose, and the span of each claim's value inside it.

    The spans are real offsets into the returned text, not decoration: `VerificationResult`
    addresses a claim by `claim_ref` (D25), and a span that points nowhere makes a claim
    unverifiable in exactly the case verification matters most.
    """
    segments: list[str] = []
    spans: list[ClaimSourceSpan] = []
    cursor = 0
    for index, entry in enumerate(script):
        label = str(entry["key"]).replace("_", " ")
        rendered = _render_value(entry["value"])
        prefix = "" if index == 0 else _SEGMENT_SEPARATOR
        segment = f"{prefix}{label}: {rendered}."
        start = cursor + len(prefix) + len(label) + 2
        spans.append(ClaimSourceSpan(pitch_ref=pitch_ref, start=start, end=start + len(rendered)))
        segments.append(segment)
        cursor += len(segment)
    return "".join(segments), tuple(spans)


def _render_value(value: Any) -> str:
    """The scripted value as it appears in the prose — verbatim for the strings a script uses."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _build_claim(
    entry: dict[str, Any], *, pitch_ref: str, span: ClaimSourceSpan, observed_at: str
) -> Claim:
    key = str(entry["key"])
    value = entry["value"]
    raw_type = entry.get("claim_type")
    claim_type = ClaimType(str(raw_type)) if raw_type is not None else None
    return Claim(
        claim_id=claim_id(pitch_ref=pitch_ref, key=key, value=value, claim_type=claim_type),
        key=key,
        claim_type=claim_type,
        value=value,
        unit=_optional_str(entry.get("unit")),
        source_span=span,
        provenance=Provenance(
            source=PERSONA_PROVENANCE_SOURCE,
            ref=f"{pitch_ref}#{key}",
            observed_at=observed_at,
            authority_rank=canonical_authority_rank(PERSONA_PROVENANCE_SOURCE.value),
        ),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
