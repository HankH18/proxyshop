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

"Exactly once" is implemented rather than merely asserted: an admitted grant is *consumed*
through :meth:`~store_agent.hooks.tools.ToolHooks.spend_authorization`, so presenting it a second
time inside the same bid is refused as spent. A membership test alone would let one
`authorize_discount` call furnish the discount for every offer in a bid — the exchange would see
one authorization and any number of discounted offers built on it. Consumption is atomic with
admission: a call that refuses anything spends nothing, so a bid rejected for an unrelated reason
does not burn an honest grant.

**The third wall: a bid is not a flat list.** The material a boundary is handed is a `Bid`, and a
`Bid` carries an `Offer` that carries its own ``commitments: list[Claim]`` and a `discount`.
A guard that inspected only the iterable it was passed left both of those unexamined, and the
exclusivity property was defeated by moving the payload one level down. So
:func:`collect_claim_material` walks the whole structure — nested claim lists, the offer, and the
offer's discount — and a discount is admissible only when the same bid also carries the
hook-minted grant that authorizes exactly that depth for exactly this product. A discount is
where the money is; it is the last field that should be checked by omission.

**And a wall around the depth, not around one spelling.** `authorized_discount_pct` is not the
only claim that can carry a discount into a bid: hook 5's `policy_action` carries the learned
policy's chosen `discount_pct` inside its value. Every discount-bearing claim is therefore
re-asked against the envelope, whatever its key — a rule that watched one key would be a rule
about a spelling.

The property on offer is therefore "every claim anywhere in this bid is one the hooks emitted,
every authorization in it was granted for this bid and spent once, and every discount in it is
backed by such an authorization" — which is what is checked.

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

#: Fields on a bid-shaped or offer-shaped object that carry claims. `Bid.claims` is the obvious
#: one; `Offer.commitments` is the one the auditor used, and it is `list[Claim]` on the same
#: object that carries the price.
CLAIM_BEARING_FIELDS: tuple[str, ...] = ("claims", "commitments")

#: Fields carrying a nested object that itself carries claims. A bid's offer is part of the bid.
NESTED_OBJECT_FIELDS: tuple[str, ...] = ("offer",)

#: The field carrying the priced discount. Not a `Claim` — `contracts.Discount` has no `key` —
#: so it cannot be checked against the ledger directly; it is checked against the grant that
#: authorizes it. See :func:`_discount_refusal`.
DISCOUNT_FIELD = "discount"

#: `Discount.type` values that mean "``value`` is a percentage depth", which is the only form a
#: hook can authorize: :meth:`~store_agent.hooks.tools.ToolHooks.authorize_discount` reasons in
#: percent against a cap in percent. Any other discount form is refused rather than guessed at —
#: an amount cannot be compared with a grant without re-deriving the price the bid is asserting.
PERCENTAGE_DISCOUNT_TYPES = frozenset({"percentage", "percent", "pct"})

#: Keys inside a claim's *value* that name a discount depth. Hook 5's `policy_action` value is a
#: mapping carrying `discount_pct`; the depth is no less real for being one level in.
DISCOUNT_VALUE_KEYS: tuple[str, ...] = ("discount_pct",)

#: Slack when matching an offer's discount against the grant that authorizes it. Both are binary
#: floats that travelled through JSON; without this a grant of 15.0 could fail to back a discount
#: of 15.0. Far below anything a merchant could feel, for the same reason as `WALL_TOLERANCE`.
DISCOUNT_MATCH_TOLERANCE = 1e-9


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
    """A genuinely hook-emitted claim is not spendable in *this* bid.

    The ledger cannot catch any of these: the claim *is* in the ledger — a real hook really did
    emit it — it is simply not this bid's to spend. Three ways that happens, and they are the
    same kind of mistake at different distances: the grant was minted for a different product,
    the rule that granted it has since been tightened, or it was already spent in this bid.
    Subclasses :class:`HookProvenanceError` so a caller that already refuses un-hooked claims
    refuses transferred ones by the same `except`.
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

    Split at the FIRST separator, because that is the one :func:`scoped_ref` wrote: a rule
    citation is ``envelope:<store>:v<n>#<path>`` and carries none of its own, so everything after
    it is the product — separators included. Splitting at the last one instead would report
    ``prod-cap`` for a grant minted for ``bundle@prod-cap``, which is not merely a cosmetic
    misreport: it is the same misreading :func:`claim_is_scoped_to` used to make when deciding.
    """
    ref = _claim_ref(claim)
    rule, separator, scope = ref.partition(CLAIM_SCOPE_SEPARATOR)
    return scope if separator and rule and scope else None


def claim_is_scoped_to(claim: Any, product_ref: str) -> bool:
    """Whether `claim`'s ref is a rule citation bound to exactly `product_ref`.

    Structural, not a suffix test. ``ref.endswith('@' + product_ref)`` reads
    ``envelope:store-alpha:v3#max_discount_pct@bundle@prod-cap`` — a real grant, for a real
    product whose ref happens to contain the separator — as a grant for ``prod-cap``, and the
    separator is a legal character in a product ref that nothing upstream forbids. So the ref
    must be exactly what :func:`scoped_ref` would have built: the suffix, preceded by a rule
    citation that is non-empty and carries no separator of its own. A `product_ref` that itself
    contains separators still matches exactly — ``rule@kit@bundle@1`` splits at the first
    separator and nowhere else — while a longer product ref can no longer lend its tail.
    """
    suffix = f"{CLAIM_SCOPE_SEPARATOR}{product_ref}"
    ref = _claim_ref(claim)
    if not ref.endswith(suffix) or len(ref) <= len(suffix):
        return False
    rule = ref[: -len(suffix)]
    return CLAIM_SCOPE_SEPARATOR not in rule


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


def _is_claim_shaped(node: Any) -> bool:
    """Whether `node` is a claim rather than something that *carries* claims.

    Structured provenance plus a usable key. The key is what separates a `Claim` from a
    `Discount`, which also carries an optional provenance and is emphatically not a claim: it
    asserts nothing on its own, it spends an authorization someone else minted.
    """
    if _read(node, "provenance") is None:
        return False
    key = _read(node, "key")
    return isinstance(key, str) and bool(key)


def _structural_fields(node: Any) -> tuple[str, ...]:
    """Which claim-carrying fields `node` has, by presence rather than by value.

    Presence, because an `Offer` with `commitments=[]` and `discount=None` is still an offer and
    still needs walking rather than being mistaken for a malformed claim. A `Claim` carries none
    of these names, so it is never mistaken for a container.
    """
    names = (*CLAIM_BEARING_FIELDS, *NESTED_OBJECT_FIELDS, DISCOUNT_FIELD)
    if isinstance(node, Mapping):
        return tuple(name for name in names if name in node)
    return tuple(name for name in names if hasattr(node, name))


def _is_sequence(node: Any) -> bool:
    """Whether `node` is a collection of things to walk rather than one thing.

    A pydantic model answers `isinstance(node, Iterable)` — `BaseModel.__iter__` yields its own
    ``(field, value)`` pairs — so the obvious test reads a `Bid` as a list of two-tuples and
    refuses it as thirty-odd malformed claims. `model_dump` is the tell: a model is an object
    with fields, whatever its iteration protocol says.
    """
    if isinstance(node, (str, bytes, Mapping)):
        return False
    if callable(getattr(node, "model_dump", None)):
        return False
    return isinstance(node, Iterable)


def _walk(node: Any, path: str, claims: list[Any], discounts: list[tuple[str, Any]]) -> None:
    """Collect every claim and every discount reachable in `node`, depth first."""
    if node is None:
        return
    if _is_claim_shaped(node):
        # A claim is a leaf. Its `value` is arbitrary data covered by the fingerprint, so a
        # claim-shaped dict buried inside one is not a second claim — it is part of what the
        # ledger already vouches for, and descending would refuse honest evidence.
        claims.append(node)
        return

    fields = _structural_fields(node)
    if fields:
        for field in CLAIM_BEARING_FIELDS:
            if field in fields:
                _walk(_read(node, field), f"{path}.{field}", claims, discounts)
        for field in NESTED_OBJECT_FIELDS:
            if field in fields:
                _walk(_read(node, field), f"{path}.{field}", claims, discounts)
        if DISCOUNT_FIELD in fields:
            discount = _read(node, DISCOUNT_FIELD)
            if discount is not None:
                discounts.append((f"{path}.{DISCOUNT_FIELD}", discount))
        return

    if _is_sequence(node):
        for index, item in enumerate(node):
            _walk(item, f"{path}[{index}]", claims, discounts)
        return

    # Not claim-shaped, not a container, not a collection: a claim candidate that `_refusal`
    # will refuse with a message about what is wrong with it. Failing closed here rather than
    # dropping it silently is the whole difference between a guard and a filter.
    claims.append(node)


def collect_claim_material(presented: Any) -> tuple[list[Any], list[tuple[str, Any]]]:
    """Every claim and every discount inside `presented`: ``(claims, [(path, discount)])``.

    The boundary's field of view. A flat list of claims collects to itself, which is what keeps
    the two-argument call the frozen suite makes working unchanged; a `Bid` collects to its own
    claims *plus* its offer's commitments, with the offer's discount recorded separately because
    it is not a claim and cannot be checked like one.

    Everything unrecognized is collected as a claim candidate rather than skipped. A structure
    this function did not understand must fail the boundary, not slip through it.
    """
    claims: list[Any] = []
    discounts: list[tuple[str, Any]] = []
    _walk(presented, "", claims, discounts)
    return claims, discounts


def _as_depth(value: Any) -> float | None:
    """A discount depth as a float, or `None` when the value is not a number at all."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _claim_discount_depths(claim: Any) -> list[tuple[str, float | None]]:
    """Every discount depth `claim` would put into a bid, as ``(what, depth)``.

    Two shapes, because there are two: the grant, whose whole value is the depth, and hook 5's
    `policy_action`, whose value is a mapping with `discount_pct` inside it. A claim carrying a
    depth is an authorization however it spells its key, and the envelope wall is asked about all
    of them.
    """
    key = _read(claim, "key")
    value = _enum_value(_read(claim, "value"))
    found: list[tuple[str, float | None]] = []
    if isinstance(key, str) and (key in PRODUCT_SCOPED_CLAIM_KEYS or key.endswith("discount_pct")):
        found.append((key, _as_depth(value)))
    if isinstance(value, Mapping):
        for name in DISCOUNT_VALUE_KEYS:
            if name in value:
                found.append((f"{key}.{name}", _as_depth(value[name])))
    return found


def _refusal(claim: Any, ledger: Any) -> str | None:
    """Why `claim` is inadmissible, or `None` when it is admissible.

    This is the *provenance* wall, and there is exactly one of it: the claim's fingerprint must
    be in the ledger of what the hooks emitted. The branches below add nothing to it — every one
    of them describes a claim the ledger test would refuse anyway — they exist so the raised
    error names the actual problem instead of saying "not found" about a claim that has no
    provenance at all.

    Admissible here means "a hook emitted this", which for an authorization is not yet "this bid
    may spend it": that question belongs to :func:`_authorization_refusal`, and deliberately does not
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


def _authorization_refusal(claim: Any, hooks: Any, product_ref: str | None) -> str | None:
    """Why a hook-emitted authorization is not spendable in *this* bid, or `None`.

    The second wall, and the only one the ledger cannot stand in for. A claim in
    :data:`PRODUCT_SCOPED_CLAIM_KEYS` is an authorization rather than a fact: it was granted by
    checking walls that belong to one product at one moment. The ledger says a hook emitted it —
    which is true, and says nothing about *where* or *when* it may be spent.

    Three questions, because a grant can go stale three different ways:

    **Whose is it?** Fails closed on an unnamed bid. "Which product is this about" has no safe
    default: answering it with "any" would restore the whole defect, admitting a grant precisely
    because nobody said what it was being spent on.

    **Is it still true?** The ledger records what *was* authorized. A merchant who tightens the
    envelope is changing what *is*, and a grant obtained under a 20% cap must not survive a drop
    to 5% on the strength of a ledger entry. So the walls are re-asked live, through
    :meth:`~store_agent.hooks.tools.ToolHooks.would_authorize`, which computes the same
    arithmetic the hook did without minting anything. A facade that cannot answer is refused
    rather than trusted, for the same reason a facade with no ledger is.

    **Has it already been spent?** A grant is one authorization, not a licence to discount every
    offer in the bid, so an admitted grant is consumed and a second presentation is refused.

    **And it is asked of every claim that carries a depth**, not only of the one whose key is
    `authorized_discount_pct`. Hook 5's `policy_action` carries the learned policy's chosen
    `discount_pct` inside its value; a wall that watched one key would be a wall around a
    spelling, and the auditor walked around it by using the other one.
    """
    key = _read(claim, "key")
    depths = _claim_discount_depths(claim)
    is_grant = key in PRODUCT_SCOPED_CLAIM_KEYS
    if not is_grant and not depths:
        return None

    granted_for = claim_scope(claim)
    if is_grant:
        if product_ref is None:
            return (
                f"claim {key!r} authorizes one product (granted for {granted_for!r}), but the "
                "boundary was not told which product this bid is about; pass product_ref=... — "
                "refusing rather than assuming an authorization transfers"
            )
        if not claim_is_scoped_to(claim, str(product_ref)):
            return (
                f"claim {key!r} was granted for {granted_for!r} and cannot be spent on "
                f"{str(product_ref)!r}: the envelope's price floors are per product, so the "
                "walls cleared for one product were never checked for the other"
            )
        spent = getattr(hooks, "spent_fingerprints", None)
        if spent is None or not callable(getattr(hooks, "spend_authorization", None)):
            return (
                f"{type(hooks).__name__} cannot record that an authorization was spent (no "
                "`spent_fingerprints` / `spend_authorization`), so 'exactly once' is "
                "unenforceable here; refusing"
            )
        if claim_fingerprint(claim) in spent:
            return (
                f"claim {key!r} granted for {granted_for!r} was already spent in this bid: a "
                "grant is one authorization, not a licence to discount every offer in the bid — "
                "call the hook again for a second one"
            )

    recheck = getattr(hooks, "would_authorize", None)
    if not callable(recheck):
        return (
            f"{type(hooks).__name__} cannot re-check an authorization (no `would_authorize`), "
            f"so whether the envelope still grants {key!r} is unknowable here; refusing"
        )

    for what, depth in depths:
        if depth is None:
            return (
                f"claim {key!r} carries a discount at {what!r} that is not a number, so no "
                "envelope wall can be asked about it; refusing rather than passing it through"
            )
        if depth == 0.0:
            continue
        if product_ref is None:
            return (
                f"claim {key!r} carries a {depth}% discount at {what!r}, but the boundary was "
                "not told which product this bid is about; pass product_ref=... — the price "
                "floor a discount must clear is per product"
            )
        if not recheck(str(product_ref), depth):
            return (
                f"claim {key!r} is in the ledger but the envelope does not authorize the "
                f"{depth}% discount it carries at {what!r} for {str(product_ref)!r}: a claim "
                "does not get to carry a depth the walls refuse"
            )
        claimed_product = _read(_enum_value(_read(claim, "value")), "product_ref")
        if isinstance(claimed_product, str) and claimed_product and claimed_product != product_ref:
            return (
                f"claim {key!r} carries a {depth}% discount chosen for {claimed_product!r} and "
                f"cannot be spent on {str(product_ref)!r}"
            )
    return None


def _discount_refusal(
    path: str, discount: Any, backing: list[float], product_ref: str | None
) -> str | None:
    """Why an offer's `discount` is not authorized, or `None` when a grant in this bid backs it.

    A `Discount` is not a `Claim` and cannot be checked like one: it has no key, so it has no
    ledger identity, and its `provenance` is optional by contract because the reconciliation path
    compares it against a webhook that carries none. What makes it admissible is not its own
    paperwork but the grant beside it — the bid must carry a hook-minted
    `authorized_discount_pct` for this product at this depth, which is precisely what
    :meth:`~store_agent.hooks.tools.ToolHooks.authorize_discount` refuses to mint past a wall.

    A zero discount needs no grant, because it takes nothing off the price. Every other form is
    refused rather than guessed at: an amount off cannot be compared with a percentage grant
    without re-deriving the price the bid is asserting, and a boundary that re-derived prices
    would be deciding what the offer means instead of checking it.
    """
    depth = _as_depth(_enum_value(_read(discount, "value")))
    kind = str(_enum_value(_read(discount, "type")) or "")
    if depth is None:
        return (
            f"the offer's discount at {path} carries no numeric value, so no grant can be "
            f"matched to it: {discount!r}"
        )
    if depth == 0.0:
        return None
    if product_ref is None:
        return (
            f"the offer at {path} carries a {depth} discount, but the boundary was not told "
            "which product this bid is about; pass product_ref=... or call "
            "enforce_bid_provenance()"
        )
    if kind.lower() not in PERCENTAGE_DISCOUNT_TYPES:
        return (
            f"the offer's discount at {path} is of type {kind!r}, which no tool hook can "
            f"authorize (hooks grant percentage depths: {sorted(PERCENTAGE_DISCOUNT_TYPES)})"
        )
    if not any(abs(granted - depth) <= DISCOUNT_MATCH_TOLERANCE for granted in backing):
        return (
            f"the offer at {path} takes {depth}% off {product_ref!r}, and no hook-emitted "
            f"authorization in this bid grants that depth (granted here: {sorted(backing)}) — "
            "R8: a discount enters a bid through authorize_discount() or not at all"
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

    The first argument is **claim-bearing material, not necessarily a list of claims**. A flat
    iterable of claims still behaves exactly as before — that is the call the frozen boundary
    makes — but a `Bid` or an `Offer` is walked whole by :func:`collect_claim_material`, so the
    offer's own `commitments` and its `discount` are inside the boundary rather than beside it.
    They were outside it once, and that is how an unauthorised 25% reached a bid whose every
    top-level claim was genuine. The returned list is what was checked.

    **A bidding runtime must therefore hand this function the whole bid, never `bid.claims`.**
    This is not a style preference and it is not something the function can check for itself: a
    guard sees exactly what it is passed, and passing it the claim list means the offer's price
    and discount were never inside the boundary at all. :func:`enforce_bid_provenance` is the
    call to make, because it also reads the product off the bid instead of asking for it twice.

    `product_ref` is the product the bid is about. It is consulted for anything that carries an
    authorization — a grant, a claim carrying a discount depth, the offer's discount — and
    ignored for facts: a scraped material or an owner's returns policy is true of the store
    however the bid is assembled. For an authorization it is required, and material carrying one
    without it is refused. Prefer :func:`enforce_bid_provenance`, which reads it off the bid.
    Keyword-only so the two-argument call the frozen boundary already makes keeps working, and
    so a caller cannot pass a product by accident into the `hooks` position.

    A refusal that is *only* about spendability raises :class:`ClaimScopeError`, which subclasses
    :class:`HookProvenanceError` — the claim did come from a hook, so "no tool hook emitted this"
    would be a false explanation, while an existing ``except HookProvenanceError`` still catches
    it without being touched.

    Admitted grants are **consumed** before returning, and only if nothing was refused: a call
    that raises spends nothing, so a bid rejected for an unrelated reason does not burn an honest
    authorization.
    """
    ledger = getattr(hooks, "emitted_fingerprints", None)
    if ledger is None:
        raise HookProvenanceError(
            f"{type(hooks).__name__} exposes no `emitted_fingerprints` ledger, so there is "
            "nothing to check these claims against; refusing rather than admitting them"
        )

    presented, discounts = collect_claim_material(claims)
    offenders: list[tuple[int, str]] = []
    unhooked = 0
    grants: list[Any] = []
    authorized_depths: list[float] = []
    for index, claim in enumerate(presented):
        reason = _refusal(claim, ledger)
        if reason is not None:
            offenders.append((index, reason))
            unhooked += 1
            continue
        reason = _authorization_refusal(claim, hooks, product_ref)
        if reason is not None:
            offenders.append((index, reason))
            continue
        if _read(claim, "key") in PRODUCT_SCOPED_CLAIM_KEYS:
            grants.append(claim)
            depth = _as_depth(_enum_value(_read(claim, "value")))
            if depth is not None:
                authorized_depths.append(depth)

    for offset, (path, discount) in enumerate(discounts):
        reason = _discount_refusal(path, discount, authorized_depths, product_ref)
        if reason is not None:
            offenders.append((len(presented) + offset, reason))

    if offenders:
        detail = "; ".join(f"[{index}] {reason}" for index, reason in offenders)
        if unhooked == 0:
            raise ClaimScopeError(
                f"{len(offenders)} of {len(presented) + len(discounts)} item(s) are hook-emitted "
                f"authorizations that were not granted for this bid, or discounts no "
                f"authorization in it covers (R8: a discount clears the walls of the product it "
                f"was checked against, and of no other) — {detail}",
                offenders,
            )
        raise HookProvenanceError(
            f"{len(offenders)} of {len(presented) + len(discounts)} item(s) did not come from a "
            f"tool hook (R8: the hooks are the only way a fact enters a hosted bid) — {detail}",
            offenders,
        )

    spend = getattr(hooks, "spend_authorization", None)
    if callable(spend):
        for claim in grants:
            spend(claim_fingerprint(claim))
    return presented


def enforce_bid_provenance(bid: Any, hooks: Any, *, product_ref: str | None = None) -> list[Any]:
    """:func:`enforce_hook_provenance` over a whole bid, with the product read off the bid.

    "Which product is this bid about" is not a question a caller should be able to forget: the
    scope wall fails closed on an unnamed product, so forgetting it stops the honest path rather
    than opening a hole — but a bid already carries the answer on `offer.product_ref`, and a
    boundary that made the runtime restate it would be inviting the two to disagree.

    Pass `product_ref` explicitly only to check a bid against a product it does not name, which
    is a thing to be deliberate about.
    """
    if product_ref is None:
        offer = _read(bid, "offer")
        named = _read(offer if offer is not None else bid, "product_ref")
        product_ref = str(named) if isinstance(named, str) and named else None
    return enforce_hook_provenance(bid, hooks, product_ref=product_ref)


__all__ = [
    "CLAIM_BEARING_FIELDS",
    "CLAIM_FINGERPRINT_ALGORITHM",
    "CLAIM_FINGERPRINT_PREFIX",
    "CLAIM_SCOPE_SEPARATOR",
    "DISCOUNT_FIELD",
    "DISCOUNT_MATCH_TOLERANCE",
    "DISCOUNT_VALUE_KEYS",
    "NESTED_OBJECT_FIELDS",
    "PERCENTAGE_DISCOUNT_TYPES",
    "PRODUCT_SCOPED_CLAIM_KEYS",
    "UNKNOWN_OBSERVED_AT",
    "ClaimScopeError",
    "HookProvenanceError",
    "NotAClaimError",
    "claim_fingerprint",
    "claim_is_scoped_to",
    "claim_scope",
    "collect_claim_material",
    "enforce_bid_provenance",
    "enforce_hook_provenance",
    "mint_claim",
    "mint_provenance",
    "scoped_ref",
]
