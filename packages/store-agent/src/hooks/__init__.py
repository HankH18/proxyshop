"""Tool hooks — the only way a fact or a discount enters a hosted bid (R8, T-040).

The public surface, in the order it is used:

* :class:`~.tools.ToolHooks` — the six hooks DESIGN §Tool hooks names, each stamping the one
  provenance source class assigned to it, each recording what it emitted.
* :class:`~.tools.Denied` — what `authorize_discount` returns when an envelope wall refuses a
  request. Falsy, typed, and not an exception, so it has to be looked at.
* :func:`~.provenance.enforce_hook_provenance` / :class:`~.provenance.HookProvenanceError` — the
  bid boundary. A claim is admitted because the hooks emitted it, never because it says so.
* :func:`~.provenance.mint_claim` / :func:`~.provenance.mint_provenance` — the single minting
  site. :mod:`.lint` refuses a `Claim(...)` built anywhere else on the hosted path.

Import path: both ``store_agent.hooks`` (the flat-src namespace, via ``.pkgroot``) and
``packages.store_agent.src.hooks`` (the repo-root dotted path the frozen suite binds to) resolve
here; every import below is relative so both spellings work.
"""

from __future__ import annotations

from .lint import (
    EXEMPT_DIRECTORIES,
    GUARDED_CONSTRUCTORS,
    MINTING_SITE,
    Offence,
    format_offences,
    hosted_claim_construction_offenders,
)
from .provenance import (
    CLAIM_FINGERPRINT_ALGORITHM,
    CLAIM_FINGERPRINT_PREFIX,
    UNKNOWN_OBSERVED_AT,
    HookProvenanceError,
    NotAClaimError,
    claim_fingerprint,
    enforce_hook_provenance,
    mint_claim,
    mint_provenance,
)
from .tools import (
    CLAIM_TYPE_BY_KEY,
    COLD_START_POLICY_VERSION,
    REASON_BELOW_PRICE_FLOOR,
    REASON_NEGATIVE_DISCOUNT,
    REASON_OVER_MAX_DISCOUNT,
    REASON_UNKNOWN_PRODUCT,
    WALL_TOLERANCE,
    Denied,
    HookCall,
    HookInputError,
    ToolHooks,
)

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
    "CLAIM_FINGERPRINT_ALGORITHM",
    "CLAIM_FINGERPRINT_PREFIX",
    "CLAIM_TYPE_BY_KEY",
    "COLD_START_POLICY_VERSION",
    "EXEMPT_DIRECTORIES",
    "GUARDED_CONSTRUCTORS",
    "HOOK_SOURCE_CLASSES",
    "MINTING_SITE",
    "REASON_BELOW_PRICE_FLOOR",
    "REASON_NEGATIVE_DISCOUNT",
    "REASON_OVER_MAX_DISCOUNT",
    "REASON_UNKNOWN_PRODUCT",
    "UNKNOWN_OBSERVED_AT",
    "WALL_TOLERANCE",
    "Denied",
    "HookCall",
    "HookInputError",
    "HookProvenanceError",
    "NotAClaimError",
    "Offence",
    "ToolHooks",
    "claim_fingerprint",
    "enforce_hook_provenance",
    "format_offences",
    "hosted_claim_construction_offenders",
    "mint_claim",
    "mint_provenance",
]
