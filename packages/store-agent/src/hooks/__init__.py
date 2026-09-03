"""Tool hooks — the only way a fact or a discount enters a hosted bid (R8, T-040).

The public surface, in the order it is used:

* :class:`~.tools.ToolHooks` — the six hooks DESIGN §Tool hooks names, each stamping the one
  provenance source class assigned to it, each recording what it emitted.
* :class:`~.tools.Denied` — what `authorize_discount` returns when an envelope wall refuses a
  request. Falsy, typed, and not an exception, so it has to be looked at.
* :func:`~.provenance.enforce_bid_provenance` / :class:`~.provenance.HookProvenanceError` — the
  bid boundary. A claim is admitted because the hooks emitted it, never because it says so.
  **Hand it the whole `Bid`.** :func:`~.provenance.enforce_hook_provenance` checks exactly the
  material it is given, so calling it with `bid.claims` leaves the offer's discount and its own
  `commitments` outside the boundary — which is how an unauthorised 25% once reached a bid whose
  every top-level claim was genuine.
* :func:`~.provenance.mint_claim` / :func:`~.provenance.mint_provenance` — the single minting
  site. :mod:`.lint` refuses a `Claim(...)` built anywhere else on the hosted path.

Import path: both ``store_agent.hooks`` (the flat-src namespace, via ``.pkgroot``) and
``packages.store_agent.src.hooks`` (the repo-root dotted path the frozen suite binds to) resolve
here, and **they are the same module object** — see :data:`CANONICAL_MODULE`. Two spellings of
one file would otherwise be two module objects holding two of every class, so a `ToolHooks` built
through one spelling would not be an instance of the other's `ToolHooks` and an
``except HookProvenanceError`` written against one would not catch the other's. For a boundary
whose whole job is refusing things, an exception type that depends on how the caller spelled the
import is not a nuisance; it is a hole.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .lint import (
    CONSTRUCTOR_METHODS,
    EXEMPT_DIRECTORIES,
    GUARDED_CONSTRUCTORS,
    MINTING_SITE,
    Offence,
    format_offences,
    hosted_claim_construction_offenders,
)
from .provenance import (
    CLAIM_BEARING_FIELDS,
    CLAIM_FINGERPRINT_ALGORITHM,
    CLAIM_FINGERPRINT_PREFIX,
    CLAIM_SCOPE_SEPARATOR,
    DISCOUNT_FIELD,
    DISCOUNT_MATCH_TOLERANCE,
    DISCOUNT_VALUE_KEYS,
    NESTED_OBJECT_FIELDS,
    PERCENTAGE_DISCOUNT_TYPES,
    PRICE_FIELD,
    PRICE_RECONCILIATION_TOLERANCE,
    PRODUCT_SCOPED_CLAIM_KEYS,
    UNKNOWN_OBSERVED_AT,
    ClaimMaterial,
    ClaimScopeError,
    HookProvenanceError,
    NotAClaimError,
    claim_fingerprint,
    claim_is_scoped_to,
    claim_scope,
    collect_claim_material,
    enforce_bid_provenance,
    enforce_hook_provenance,
    mint_claim,
    mint_provenance,
    scoped_ref,
)
from .tools import (
    CLAIM_TYPE_BY_KEY,
    COLD_START_POLICY_VERSION,
    REASON_BELOW_PRICE_FLOOR,
    REASON_NEGATIVE_DISCOUNT,
    REASON_NON_FINITE_DISCOUNT,
    REASON_OVER_MAX_DISCOUNT,
    REASON_UNKNOWN_PRODUCT,
    REASON_UNPRICEABLE_PRODUCT,
    WALL_TOLERANCE,
    Denied,
    HookCall,
    HookInputError,
    ToolHooks,
)

#: The one spelling that owns the module objects. `packages.store_agent.src.hooks` is the same
#: files reached through the frozen suite's namespace alias; importing it yields *this* module.
CANONICAL_MODULE = "store_agent.hooks"

#: The submodules aliased alongside the package, so `packages.store_agent.src.hooks.tools` is
#: also one module object rather than a second copy of `ToolHooks`.
_ALIASED_SUBMODULES = ("lint", "provenance", "tools")


def _install_canonical_alias() -> bool:
    """Point this module's name at :data:`CANONICAL_MODULE`. True when the alias was installed.

    Runs at the end of import, under the non-canonical spelling only. The import machinery reads
    ``sys.modules[name]`` back after executing a module (`_find_and_load_unlocked` re-checks it
    explicitly), so replacing the entry here is what makes ``from packages.store_agent.src.hooks
    import ToolHooks`` hand back the canonical class rather than a second one.

    Deliberately defensive at every step. The canonical spelling depends on ``.pkgroot`` being
    importable, which is a packaging fact this module does not control and the frozen acceptance
    runner explicitly clears `pythonpath` for. If it is not importable, or if it somehow resolves
    to a *different* file, this does nothing at all and both spellings keep working as before —
    an aliasing convenience must never be able to break the boundary it is tidying.
    """
    if __name__ == CANONICAL_MODULE:
        return False
    try:
        canonical = importlib.import_module(CANONICAL_MODULE)
    except ImportError:  # pragma: no cover - depends on how the caller set sys.path
        return False
    here = getattr(canonical, "__file__", None)
    if here is None or Path(here).resolve() != Path(__file__).resolve():
        # A different file answering to that name is not this package; leave it alone.
        return False  # pragma: no cover - would mean two store_agent trees on one path
    sys.modules[__name__] = canonical
    for name in _ALIASED_SUBMODULES:
        submodule = getattr(canonical, name, None)
        if submodule is not None:
            sys.modules[f"{__name__}.{name}"] = submodule
    return True


#: The hook → provenance source table DESIGN pins. Published so a caller can assert the mapping
#: without reaching into the implementation, and so the six hook names have one spelling.
HOOK_SOURCE_CLASSES: dict[str, str] = {
    "get_product_fact": "scraped",
    "get_live_state": "pixel_feed",
    "get_owner_commitments": "owner_statement",
    "authorize_discount": "envelope_rule",
    "choose_policy_action": "learned_policy",
    "get_network_prior": "network",
}

__all__ = [
    "CANONICAL_MODULE",
    "CLAIM_BEARING_FIELDS",
    "CLAIM_FINGERPRINT_ALGORITHM",
    "CLAIM_FINGERPRINT_PREFIX",
    "CLAIM_SCOPE_SEPARATOR",
    "CLAIM_TYPE_BY_KEY",
    "COLD_START_POLICY_VERSION",
    "CONSTRUCTOR_METHODS",
    "DISCOUNT_FIELD",
    "DISCOUNT_MATCH_TOLERANCE",
    "DISCOUNT_VALUE_KEYS",
    "EXEMPT_DIRECTORIES",
    "GUARDED_CONSTRUCTORS",
    "HOOK_SOURCE_CLASSES",
    "MINTING_SITE",
    "NESTED_OBJECT_FIELDS",
    "PERCENTAGE_DISCOUNT_TYPES",
    "PRICE_FIELD",
    "PRICE_RECONCILIATION_TOLERANCE",
    "PRODUCT_SCOPED_CLAIM_KEYS",
    "REASON_BELOW_PRICE_FLOOR",
    "REASON_NEGATIVE_DISCOUNT",
    "REASON_NON_FINITE_DISCOUNT",
    "REASON_OVER_MAX_DISCOUNT",
    "REASON_UNKNOWN_PRODUCT",
    "REASON_UNPRICEABLE_PRODUCT",
    "UNKNOWN_OBSERVED_AT",
    "WALL_TOLERANCE",
    "ClaimMaterial",
    "ClaimScopeError",
    "Denied",
    "HookCall",
    "HookInputError",
    "HookProvenanceError",
    "NotAClaimError",
    "Offence",
    "ToolHooks",
    "claim_fingerprint",
    "claim_is_scoped_to",
    "claim_scope",
    "collect_claim_material",
    "enforce_bid_provenance",
    "enforce_hook_provenance",
    "format_offences",
    "hosted_claim_construction_offenders",
    "mint_claim",
    "mint_provenance",
    "scoped_ref",
]

# Last, deliberately: the module is fully built either way, so a caller that arrived through the
# non-canonical spelling gets a complete module whether or not the alias could be installed.
_ALIAS_INSTALLED = _install_canonical_alias()
