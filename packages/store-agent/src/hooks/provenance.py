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
covers the claim's key, value, type, unit, source span and full provenance. Change any of them —
say "30 days" to "90 days" while keeping the envelope ref that made "30 days" true — and the
fingerprint misses.

A bit-identical restatement of a claim the hooks really did emit passes, and should: identical
content under identical provenance is the same fact from the same evidence, and there is nothing
smuggled about saying it twice.

**The second wall, for the claims that are not facts.** That reasoning holds for a *fact* and
fails for an *authorization*. "This store offers 30-day returns" is true of the store however
the bid was assembled; "15% is approved here" was only ever true of the product whose price
floor was actually checked to grant it — `prod-cap` clears 20% off 100.00 against a 10.00 floor
while `prod-floor` refuses the identical request against a 95.00 one. A grant is therefore not
restatable, only spendable, and exactly once. The ledger cannot see the difference — the claim
really was emitted by a real hook — so :func:`enforce_hook_provenance` takes the product the bid
is about and refuses a grant minted for a different one, and the hook binds the product into the
`ref` (:func:`scoped_ref`) so the two grants are not one ledger entry to begin with.

The property on offer is therefore "every claim in this bid is one the hooks emitted, and every
authorization in it was granted for this bid" — which is what is checked.

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

#: Separates a rule citation from the product the rule was *applied to*, inside a claim `ref`:
#: ``envelope:store-alpha:v3#max_discount_pct@prod-cap`` reads "the v3 envelope's discount cap,
#: as evaluated for prod-cap". One ref that names both the rule and the subject it was checked
#: against, so the fingerprint of a grant differs per product.
CLAIM_SCOPE_SEPARATOR = "@"

#: Claim keys that are an *authorization for one product*, not a fact about the store.
#:
#: A discount is the only one so far, and it is the clearest case: the walls it clears are
#: per product — `prod-cap` has a 10.00 floor and `prod-floor` a 95.00 one — so "15% is
#: allowed" is only ever true of the product whose floor was actually checked. A claim listed
#: here must carry a product scope in its ref, and :func:`enforce_hook_provenance` refuses one
#: presented in a bid about a different product.
PRODUCT_SCOPED_CLAIM_KEYS = frozenset({"authorized_discount_pct"})


class HookProvenanceError(RuntimeError):
    """A claim reached the bid boundary that no tool hook emitted (R8/S5).

    Raised by :func:`enforce_hook_provenance`. `offenders` carries one ``(index, reason)`` pair
    per refused claim so a caller can log which claim failed and why, rather than only that
    something did.
    """

    def __init__(self, message: str, offenders: Iterable[tuple[int, str]] = ()) -> None:
        super().__init__(message)
        self.offenders: tuple[tuple[int, str], ...] = tuple(offenders)


class ClaimScopeError(HookProvenanceError):
    """A genuinely hook-emitted claim was presented for a product it was not granted for.

    The ledger cannot catch this one: the claim *is* in the ledger — a real hook really did
    emit it — just for a different product. Subclasses :class:`HookProvenanceError` so a caller
    that already refuses un-hooked claims refuses transferred ones by the same `except`.
    """


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


def scoped_ref(rule_ref: str, product_ref: str) -> str:
    """``<rule_ref>@<product_ref>`` — a rule citation bound to the product it was applied to.

    The one place the scoping spelling lives, so the hook that mints a scoped ref and the guard
    that checks one cannot drift apart. Built rather than parsed at the check site: comparing
    against a constructed ref means a product_ref containing the separator is still matched
    exactly, where splitting the string would guess.
    """
    return f"{rule_ref}{CLAIM_SCOPE_SEPARATOR}{product_ref}"


def _claim_ref(claim: Any) -> str:
    """A claim's provenance `ref`, or the empty string when it has no structured provenance."""
    provenance = _read(claim, "provenance")
    if provenance is None or isinstance(provenance, (str, bytes)):
        return ""
    return str(_enum_value(_read(provenance, "ref")) or "")


def claim_scope(claim: Any) -> str | None:
    """The product a claim's ref is bound to, or `None` when the ref names no product.

    Reads the ref rather than a dedicated field because `Claim` has none: the ref is the claim's
    citation, and "which product this grant was evaluated for" is part of the citation, not
    metadata beside it. It is covered by :func:`claim_fingerprint`, so a scope cannot be edited
    onto a claim without the ledger noticing.

    Reported, not decided with: a `product_ref` containing the separator would split here in a
    place the minting hook never chose. :func:`claim_is_scoped_to` is what the guard asks, and it
    constructs rather than parses. This one names the scope in an error message.
    """
    ref = _claim_ref(claim)
    rule, separator, scope = ref.rpartition(CLAIM_SCOPE_SEPARATOR)
    return scope if separator and rule and scope else None


def claim_is_scoped_to(claim: Any, product_ref: str) -> bool:
    """Whether `claim`'s ref is a rule citation bound to exactly `product_ref`.

    Asked by construction — does the ref end in the suffix :func:`scoped_ref` would have
    appended? — rather than by splitting the ref and comparing halves. A `product_ref` that
    itself contains the separator then still matches exactly, where a split would guess which
    separator was the minting one. The length test keeps the rule half non-empty, so a ref that
    is *only* a scope cites no rule and matches nothing.
    """
    suffix = f"{CLAIM_SCOPE_SEPARATOR}{product_ref}"
    ref = _claim_ref(claim)
    return len(ref) > len(suffix) and ref.endswith(suffix)


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


def _canonical_span(span: Any) -> Any:
    """`source_span` in one shape, whether it arrived as a model or as its `model_dump()` form.

    Both spellings must hash alike for the same reason the rest of the material does: the frozen
    suite hands the guard dicts where the hooks emit models, and a fingerprint that differed
    between them would refuse honest claims at random.
    """
    if span is None:
        return None
    if isinstance(span, Mapping):
        return {str(name): _enum_value(value) for name, value in span.items()}
    dump = getattr(span, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return {str(name): _enum_value(value) for name, value in dumped.items()}
    return str(span)


def claim_fingerprint(claim: Any) -> str:
    """A content hash identifying a claim, computed the same for a model or a plain dict.

    Covers key, value, claim type, unit, `source_span` and the whole provenance — source, ref,
    observed_at and authority rank. `claim_id` is the one excluded field, and only because it is
    *derived*: `contracts.claim_id` hashes a subset of the material already covered here, and a
    consumer is free to attach one after the fact without changing what the claim asserts.

    `source_span` is covered precisely because it is not derived. `contracts.claim_id_for` reads
    `source_span.pitch_ref` to decide a claim's downstream identity, so a span bolted onto a
    hook-minted claim re-attributes the merchant's own words to a pitch they were never in —
    with every visible provenance field still matching the genuine article. Leaving it out of
    the material would leave that edit invisible to the ledger.

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
        "source_span": _canonical_span(_read(claim, "source_span")),
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

    This is the *provenance* wall, and there is exactly one of it: the claim's fingerprint must
    be in the ledger of what the hooks emitted. The branches below add nothing to it — every one
    of them describes a claim the ledger test would refuse anyway — they exist so the raised
    error names the actual problem instead of saying "not found" about a claim that has no
    provenance at all.

    Admissible here means "a hook emitted this", which for an authorization is not yet "this bid
    may spend it": that question belongs to :func:`_scope_refusal`, and deliberately does not
    live in this function, because the ledger genuinely does contain the replayed grant.
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


def _scope_refusal(claim: Any, product_ref: str | None) -> str | None:
    """Why a hook-emitted claim is not spendable in *this* bid, or `None`.

    The second wall, and the only one the ledger cannot stand in for. A claim in
    :data:`PRODUCT_SCOPED_CLAIM_KEYS` is an authorization rather than a fact: it was granted by
    checking walls that belong to one product. The ledger says a hook emitted it — which is true,
    and says nothing about *where* it may be spent.

    Fails closed on an unnamed bid. "Which product is this about" has no safe default: answering
    it with "any" would restore the whole defect, admitting a grant precisely because nobody said
    what it was being spent on.
    """
    key = _read(claim, "key")
    if key not in PRODUCT_SCOPED_CLAIM_KEYS:
        return None
    granted_for = claim_scope(claim)
    if product_ref is None:
        return (
            f"claim {key!r} authorizes one product (granted for {granted_for!r}), but the "
            "boundary was not told which product this bid is about; pass product_ref=... — "
            "refusing rather than assuming an authorization transfers"
        )
    if not claim_is_scoped_to(claim, str(product_ref)):
        return (
            f"claim {key!r} was granted for {granted_for!r} and cannot be spent on "
            f"{str(product_ref)!r}: the envelope's price floors are per product, so the walls "
            "cleared for one product were never checked for the other"
        )
    return None


def enforce_hook_provenance(
    claims: Any, hooks: Any, *, product_ref: str | None = None
) -> list[Any]:
    """Admit `claims` only if `hooks` emitted every one of them *for this bid*. Returns them.

    Raises :class:`HookProvenanceError` naming every offender otherwise. `hooks` must expose
    `emitted_fingerprints`; a facade without one is refused rather than trusted, because "no
    ledger" and "an empty ledger" would otherwise be the same answer, and the first one would
    admit everything.

    `product_ref` is the product the bid is about. It is only consulted for claims that are
    authorizations rather than facts (:data:`PRODUCT_SCOPED_CLAIM_KEYS`) — a scraped material or
    an owner's returns policy is true of the store however the bid is assembled. For a grant it
    is required, and a claim set carrying one without it is refused: see :func:`_scope_refusal`.
    Keyword-only so the two-argument call the frozen boundary already makes keeps working, and
    so a caller cannot pass a product by accident into the `hooks` position.

    A refusal that is *only* about scope raises :class:`ClaimScopeError`, which subclasses
    :class:`HookProvenanceError` — the claim did come from a hook, so "no tool hook emitted this"
    would be a false explanation, while an existing ``except HookProvenanceError`` still catches
    it without being touched.
    """
    ledger = getattr(hooks, "emitted_fingerprints", None)
    if ledger is None:
        raise HookProvenanceError(
            f"{type(hooks).__name__} exposes no `emitted_fingerprints` ledger, so there is "
            "nothing to check these claims against; refusing rather than admitting them"
        )

    presented = _as_claim_list(claims)
    offenders: list[tuple[int, str]] = []
    unhooked = 0
    for index, claim in enumerate(presented):
        reason = _refusal(claim, ledger)
        if reason is not None:
            offenders.append((index, reason))
            unhooked += 1
            continue
        reason = _scope_refusal(claim, product_ref)
        if reason is not None:
            offenders.append((index, reason))

    if offenders:
        detail = "; ".join(f"[{index}] {reason}" for index, reason in offenders)
        if unhooked == 0:
            raise ClaimScopeError(
                f"{len(offenders)} of {len(presented)} claim(s) are hook-emitted authorizations "
                f"that were not granted for this bid (R8: a discount clears the walls of the "
                f"product it was checked against, and of no other) — {detail}",
                offenders,
            )
        raise HookProvenanceError(
            f"{len(offenders)} of {len(presented)} claim(s) did not come from a tool hook "
            f"(R8: the hooks are the only way a fact enters a hosted bid) — {detail}",
            offenders,
        )
    return presented


__all__ = [
    "CLAIM_FINGERPRINT_ALGORITHM",
    "CLAIM_FINGERPRINT_PREFIX",
    "CLAIM_SCOPE_SEPARATOR",
    "PRODUCT_SCOPED_CLAIM_KEYS",
    "UNKNOWN_OBSERVED_AT",
    "ClaimScopeError",
    "HookProvenanceError",
    "NotAClaimError",
    "claim_fingerprint",
    "claim_is_scoped_to",
    "claim_scope",
    "enforce_hook_provenance",
    "mint_claim",
    "mint_provenance",
    "scoped_ref",
]
