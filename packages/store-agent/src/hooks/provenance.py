"""Claim minting and the hook-provenance guard — R8's security property, in one place.

R8 says a hosted store-agent may put a fact into a bid *only* by calling a tool hook. That is
not a style rule about where code lives: the exchange trusts a hosted bid's claims precisely
because the harness, not the language model, decided what they say. Two pieces make it real:

**Minting.** Every hook builds its claims through :func:`mint_claim`, which stamps the one
provenance source DESIGN assigns that hook and takes the authority rank from
``contracts.PROVENANCE_AUTHORITY_RANK`` rather than inventing one. A hook cannot accidentally
mint another hook's source, and no two hooks can disagree about how authoritative the same kind
of evidence is.

**The guard.** :func:`enforce_hook_provenance` is the boundary the assembled bid crosses on its
way out. It admits a claim if and only if the hooks facade actually emitted a claim with that
content — it compares against a ledger of emissions, never against the claim's own self-report.

That distinction is the whole point. A guard that inspected ``provenance.source`` would wave
through anything that *says* ``owner_statement``, and a model that can write a bid can write
that string. The ledger cannot be written by the thing being guarded: it is appended to inside
:class:`~store_agent.hooks.tools.ToolHooks` at the moment a hook returns, and the fingerprint
covers the claim's key, value, type, unit and full provenance. Change any of them — say
"30 days" to "90 days" while keeping the envelope ref that made "30 days" true — and the
fingerprint misses.

What the guard therefore does NOT claim: a forgery that is bit-identical to a claim the hooks
really did emit passes. It should. Identical content under identical provenance is the same
fact from the same evidence; there is nothing smuggled about restating it. The property on
offer is "every claim in this bid is one the hooks emitted", and that is exactly what is
checked.

Nothing here reads a wall clock. `observed_at` comes from the evidence or from an explicit
``as_of``, falling back to :data:`UNKNOWN_OBSERVED_AT` — a hosted bid has to be reproducible
byte-for-byte from its inputs (S4), and a `datetime.now()` anywhere in this path would end that.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from contracts import (
    HOOK_PROVENANCE_SOURCES,
    Claim,
    ClaimType,
    Provenance,
    ProvenanceSource,
    canonical_authority_rank,
)
from contracts.signing import CanonicalisationError, canonical_json

#: Stamped as `observed_at` when the evidence carries no timestamp and the caller passed no
#: `as_of`. Deliberately the epoch rather than "now": trust decay reads `observed_at`, so an
#: unknown observation time must read as maximally stale, never as fresh. It is also a
#: constant, which is what keeps a cold-start bid byte-identical across two runs (S4).
UNKNOWN_OBSERVED_AT = "1970-01-01T00:00:00Z"

#: Prefix on a claim fingerprint, so a bare string in a log says what it is.
CLAIM_FINGERPRINT_PREFIX = "hookclaim"

#: Hash behind the fingerprint. Part of the value, so a future change is visible per entry.
CLAIM_FINGERPRINT_ALGORITHM = "sha256"


class HookProvenanceError(RuntimeError):
    """A claim reached the bid boundary that no tool hook emitted (R8/S5).

    Raised by :func:`enforce_hook_provenance`. `offenders` carries one ``(index, reason)`` pair
    per refused claim so a caller can log which claim failed and why, rather than only that
    something did.
    """

    def __init__(self, message: str, offenders: Iterable[tuple[int, str]] = ()) -> None:
        super().__init__(message)
        self.offenders: tuple[tuple[int, str], ...] = tuple(offenders)


class NotAClaimError(ValueError):
    """The object presented to the guard is not claim-shaped, so it has no identity at all."""


def _enum_value(raw: Any) -> Any:
    """Unwrap an enum member to its value, leaving anything else alone."""
    return getattr(raw, "value", raw)


def _read(node: Any, field: str, default: Any = None) -> Any:
    """Read `field` off a mapping or an object, so models and plain dicts behave alike.

    The frozen suite normalizes product objects to plain dicts before handing them back
    (`_claims_in` returns `model_dump`-ed views), so the guard is called with dicts even
    though the hooks emit `Claim` models. Both must fingerprint identically.
    """
    if isinstance(node, Mapping):
        return node.get(field, default)
    return getattr(node, field, default)


def mint_provenance(
    source: ProvenanceSource | str,
    ref: str,
    *,
    observed_at: str | None = None,
    authority_rank: int | None = None,
) -> Provenance:
    """Build the `Provenance` for a hook emission.

    `authority_rank` defaults to the canonical rank published for the source (D30), so two
    hooks cannot quietly assign different weights to the same class of evidence. It stays
    overridable because the schema explicitly allows a hook to down-rank a stale observation.
    """
    resolved = ProvenanceSource(_enum_value(source))
    rank = canonical_authority_rank(resolved) if authority_rank is None else int(authority_rank)
    return Provenance(
        source=resolved,
        ref=ref,
        observed_at=observed_at or UNKNOWN_OBSERVED_AT,
        authority_rank=rank,
    )


def mint_claim(
    *,
    key: str,
    value: Any,
    source: ProvenanceSource | str,
    ref: str,
    claim_type: ClaimType | str | None = None,
    unit: str | None = None,
    observed_at: str | None = None,
    authority_rank: int | None = None,
) -> Claim:
    """Build one provenance-tagged `Claim`. The only way a hook is allowed to make one."""
    return Claim(
        key=key,
        value=value,
        unit=unit,
        claim_type=ClaimType(_enum_value(claim_type)) if claim_type is not None else None,
        provenance=mint_provenance(
            source,
            ref,
            observed_at=observed_at,
            authority_rank=authority_rank,
        ),
    )


def claim_fingerprint(claim: Any) -> str:
    """A content hash identifying a claim, computed the same for a model or a plain dict.

    Covers key, value, claim type, unit and the whole provenance — source, ref, observed_at and
    authority rank. `claim_id` is deliberately excluded: it is derived from a subset of the same
    material and a consumer is free to attach one after the fact.

    Serialized with `contracts.signing.canonical_json` (RFC 8785) rather than
    `json.dumps(sort_keys=True)`, so the fingerprint of a claim does not depend on which of the
    two spellings of a float or of a non-ASCII key it happened to be built with — the same
    reason `contracts.claim_id` uses it.
    """
    provenance = _read(claim, "provenance")
    if provenance is None or isinstance(provenance, (str, bytes)):
        raise NotAClaimError("claim carries no structured provenance, so it has no hook identity")
    key = _read(claim, "key")
    if not isinstance(key, str) or not key:
        raise NotAClaimError(f"claim has no usable key: {key!r}")

    material: dict[str, Any] = {
        "key": key,
        "value": _enum_value(_read(claim, "value")),
        "claim_type": str(_enum_value(_read(claim, "claim_type")) or ""),
        "unit": str(_enum_value(_read(claim, "unit")) or ""),
        "source": str(_enum_value(_read(provenance, "source")) or ""),
        "ref": str(_enum_value(_read(provenance, "ref")) or ""),
        "observed_at": str(_enum_value(_read(provenance, "observed_at")) or ""),
        "authority_rank": _read(provenance, "authority_rank"),
    }
    rank = material["authority_rank"]
    material["authority_rank"] = int(rank) if isinstance(rank, (int, float)) else 0
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
    return f"{CLAIM_FINGERPRINT_PREFIX}:{CLAIM_FINGERPRINT_ALGORITHM}:{digest}"


def _as_claim_list(claims: Any) -> list[Any]:
    """Normalize the guard's first argument to a list of claim-shaped things."""
    if claims is None:
        return []
    if isinstance(claims, Mapping) or _read(claims, "provenance") is not None:
        return [claims]
    if isinstance(claims, (str, bytes)) or not isinstance(claims, Iterable):
        return [claims]
    return list(claims)


def _refusal(claim: Any, ledger: Any) -> str | None:
    """Why `claim` is inadmissible, or `None` when it is admissible.

    There is exactly ONE wall: the claim's fingerprint must be in the ledger of what the hooks
    emitted. The branches below add no second wall — every one of them describes a claim the
    ledger test would refuse anyway — they exist so the raised error names the actual problem
    instead of saying "not found" about a claim that has no provenance at all.
    """
    try:
        fingerprint = claim_fingerprint(claim)
    except NotAClaimError as exc:
        return str(exc)
    except CanonicalisationError as exc:
        # A value that cannot be canonicalized has no stable identity, so it can never be
        # matched against the ledger. Failing closed is the only honest answer.
        return f"claim value has no canonical form, so it has no stable identity: {exc}"
    if fingerprint in ledger:
        return None
    source = str(_enum_value(_read(_read(claim, "provenance"), "source")) or "")
    if source not in HOOK_PROVENANCE_SOURCES:
        return (
            f"provenance source {source!r} is not one a tool hook can mint "
            f"(hook sources: {sorted(HOOK_PROVENANCE_SOURCES)})"
        )
    return (
        f"no tool hook emitted this claim: key={_read(claim, 'key')!r} "
        f"value={_read(claim, 'value')!r} ref={_read(_read(claim, 'provenance'), 'ref')!r}"
    )


def enforce_hook_provenance(claims: Any, hooks: Any) -> list[Any]:
    """Admit `claims` only if `hooks` actually emitted every one of them. Returns them.

    Raises :class:`HookProvenanceError` naming every offender otherwise. `hooks` must expose
    `emitted_fingerprints`; a facade without one is refused rather than trusted, because "no
    ledger" and "an empty ledger" would otherwise be the same answer, and the first one would
    admit everything.
    """
    ledger = getattr(hooks, "emitted_fingerprints", None)
    if ledger is None:
        raise HookProvenanceError(
            f"{type(hooks).__name__} exposes no `emitted_fingerprints` ledger, so there is "
            "nothing to check these claims against; refusing rather than admitting them"
        )

    presented = _as_claim_list(claims)
    offenders = [
        (index, reason)
        for index, claim in enumerate(presented)
        if (reason := _refusal(claim, ledger)) is not None
    ]
    if offenders:
        detail = "; ".join(f"[{index}] {reason}" for index, reason in offenders)
        raise HookProvenanceError(
            f"{len(offenders)} of {len(presented)} claim(s) did not come from a tool hook "
            f"(R8: the hooks are the only way a fact enters a hosted bid) — {detail}",
            offenders,
        )
    return presented


__all__ = [
    "CLAIM_FINGERPRINT_ALGORITHM",
    "CLAIM_FINGERPRINT_PREFIX",
    "UNKNOWN_OBSERVED_AT",
    "HookProvenanceError",
    "NotAClaimError",
    "claim_fingerprint",
    "enforce_hook_provenance",
    "mint_claim",
    "mint_provenance",
]
