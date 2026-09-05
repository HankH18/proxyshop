"""The exchange's own seal on a claim verdict — the thing a bidder cannot write (ESC-020).

R19 says a hard constraint is satisfied by a ``verified`` supporting claim and by nothing
else. Until this module existed, "verified" was a STRING IN THE BID: ``filters.py`` read
``claim["status"]``, that field is not on the published ``Claim`` at all
(``packages/contracts/schemas/protocol.schema.json`` — ``additionalProperties: false``, no
``status``), and nothing on the auction path validated a bid against the contract. So the
only producer of the field that decided eligibility was the bidder, and two otherwise
identical stores — one adding six characters — split into a shortlisted winner and a
candidate excluded ``hard_constraint_unsatisfied``. It moved ``verified_hard_fit_count`` as
well, which is the FIRST published tie-break (D13), so the lie won ties as well as filters.

**The rule this module exists to enforce: a verdict is evidence only if the exchange
produced it.** That is the same rule
:func:`store_agent.hooks.provenance.enforce_hook_provenance` already applies to a claim's
``provenance.source`` — "admits a claim if and only if the hooks facade actually emitted a
claim with that content … it compares against a ledger of emissions, never against the
claim's own self-report". Here the emission record is a keyed MAC rather than a ledger,
because the producer and the reader are the same process on the same request and a seal
costs no lookup.

How it works
------------
:func:`attest_claim` returns a COPY of the claim carrying an ``exchange_verification`` block:
the status this exchange decided, what it decided it against, and a ``seal`` that is
``HMAC-SHA256(process key, canonical_json(payload))``. :func:`sealed_status` recomputes the
MAC over the claim as the ranker actually sees it and returns the status only when the two
agree in constant time. A store that writes ``status: verified``, or that writes a whole
``exchange_verification`` block of its own invention, produces a seal that cannot match: it
does not hold the key, and the key never leaves this process — no response body, no log line
and no ledger event carries it (``CreateAuctionResponse`` publishes ``bid_ref``,
``rank_score``, ``components`` and exclusion reasons, and never a claim).

The seal covers the claim's IDENTITY as well as the verdict — ``key``, ``value`` and
``unit`` — so a seal minted for ``capacity_l = 35`` cannot be moved onto a claim of
``capacity_l = 300``. It also covers ``subject``, the store the verdict is about, and
:func:`sealed_status` refuses a seal whose subject is not the candidate's exchange-attributed
``store_id``. ``subject`` may be ``None``, which means "not bound to a store"; that is what a
fixture standing in for the exchange mints, and it is honest rather than lax — the payload
says so, in the bytes the MAC covers, so a ``None`` subject cannot be retro-fitted onto a
bound seal or vice versa.

The key
-------
``secrets.token_bytes(32)``, minted once per PROCESS, kept as an attribute of :mod:`sys`.

Not a constant, because a constant is not a secret. Not read from configuration by default,
because a deployment that has to be told a key is a deployment that can be deployed without
one. ``sys`` rather than a module global because this package is imported under two names in
this repo — ``exchange.ranking.attestation`` through ``.pkgroot`` and
``apps.exchange.src.ranking.attestation`` from the repo root — and two module objects would
hold two keys, so a claim sealed under one spelling would read as forged under the other.
:mod:`sys` is the one object both spellings agree on.

``EXCHANGE_CLAIM_SEAL_KEY`` overrides it, for a deployment that runs more than one exchange
process and needs a seal minted in one to be readable in another. It is deliberately NOT
required: with it unset the key is per-process, which is strictly the safer default, and the
mint and the check are the same request in the same process (``rank_auction``).

What this does NOT close
------------------------
The seal makes the exchange's verdict unforgeable; it says nothing about whether the verdict
is RIGHT. That is :mod:`claim_verification`'s job, and what the exchange asks it — the
catalog snapshot — is wired in :mod:`exchange.ranking.verification`. An exchange holding no
catalog snapshot verifies nothing, so every hard constraint goes unsatisfied and nobody is
shortlisted. That is the same direction :func:`~exchange.ranking.serving.trust_snapshot_of`
already fails in and it is the right one, but it is a real operational requirement rather
than a footnote: wire the catalog, or hard-constrained auctions come back empty.
"""

from __future__ import annotations

import hmac
import os
import secrets
import sys
import threading
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from contracts.signing import CanonicalisationError, canonical_json

__all__ = [
    "ATTESTATION_FIELD",
    "ENV_SEAL_KEY",
    "attest_claim",
    "sealed_status",
]

#: The key on a claim under which the exchange records its own verdict. A store may write it
#: too — nothing stops a bidder putting arbitrary keys in its own document — and writing it
#: gains exactly nothing, because the value is only ever read through :func:`sealed_status`.
ATTESTATION_FIELD = "exchange_verification"

#: Optional override for the process key. See the module docstring for why it is optional.
ENV_SEAL_KEY = "EXCHANGE_CLAIM_SEAL_KEY"

#: Where the process key lives on :mod:`sys`. Named with the package prefix because :mod:`sys`
#: is shared with every other library in the process.
_KEY_ATTRIBUTE = "_proxyshop_exchange_claim_seal_key"

_KEY_LOCK = threading.Lock()

#: The fields the MAC covers. Stated as data so the mint and the check cannot drift: both go
#: through :func:`_payload`, and a field added here is added to both at once.
SEALED_FIELDS: tuple[str, ...] = (
    "key",
    "value",
    "unit",
    "subject",
    "status",
    "verifier_version",
    "catalog_snapshot",
)


def _process_key() -> bytes:
    """This process's seal key, minted on first use.

    The lock matters: ``parallel_fan_out`` runs the auction's solicitations on a thread pool,
    and while attestation itself happens after collection on one thread, a key minted twice
    would seal under one value and check under another — a failure that would look like a
    forgery rather than like a race.
    """
    configured = os.environ.get(ENV_SEAL_KEY, "")
    if configured.strip():
        return configured.encode("utf-8")
    with _KEY_LOCK:
        key = getattr(sys, _KEY_ATTRIBUTE, None)
        if not isinstance(key, bytes) or len(key) < 32:
            key = secrets.token_bytes(32)
            setattr(sys, _KEY_ATTRIBUTE, key)
        return key


def _payload(
    *,
    key: Any,
    value: Any,
    unit: Any,
    subject: Any,
    status: Any,
    verifier_version: Any,
    catalog_snapshot: Any,
) -> dict[str, Any]:
    """The sealed payload, normalised so a round trip through JSON cannot change it.

    Identifiers are stringified and absent ones are ``None`` rather than missing, because
    ``{"unit": None}`` and ``{}`` canonicalise to different bytes and the ranker is handed
    claims that have been through pydantic, a dict literal and JSON on different paths.
    ``value`` is left alone: it is the claim's own datum and
    ``contracts.signing.canonical_json`` already writes ``35`` for both ``35`` and ``35.0``.
    """
    return {
        "key": None if key is None else str(key),
        "value": value,
        "unit": None if unit is None else str(unit),
        "subject": None if subject is None else str(subject),
        "status": None if status is None else str(status),
        "verifier_version": None if verifier_version is None else str(verifier_version),
        "catalog_snapshot": None if catalog_snapshot is None else str(catalog_snapshot),
    }


def _seal(payload: Mapping[str, Any]) -> str | None:
    """The MAC over ``payload``, or ``None`` when it cannot be canonicalised.

    A claim whose value is un-canonicalisable (a NaN, a non-string key, a set) cannot be
    sealed and therefore cannot be verified. Returning ``None`` rather than raising is what
    keeps one unrepresentable claim from taking down the ranking of a whole auction — it is
    the same choice :func:`claim_verification.verify` makes when it answers ``ambiguous``
    rather than raising for a malformed claim.
    """
    try:
        message = canonical_json(dict(payload))
    except (CanonicalisationError, ValueError, TypeError):
        return None
    return hmac.new(_process_key(), message.encode("utf-8"), sha256).hexdigest()


def attest_claim(
    claim: Any,
    *,
    status: Any,
    subject: Any = None,
    verifier_version: Any = None,
    catalog_snapshot: Any = None,
    evidence_refs: Any = (),
    confidence: Any = None,
    reason: Any = None,
) -> dict[str, Any]:
    """A copy of ``claim`` carrying this exchange's sealed verdict.

    The copy is assembled rather than mutated in place, and anything the source document
    already held under :data:`ATTESTATION_FIELD` or under ``status`` is DROPPED rather than
    carried through. Neither omission is load-bearing — a forged block fails the MAC and a
    forged ``status`` is read by nothing — but a candidate record that still carries the
    field the defect was about is a record the next reader has to re-derive the harmlessness
    of.

    Args:
        claim: the claim as its author wrote it.
        status: the verdict, one of :data:`claim_verification.VERIFICATION_STATUSES`. Passed
            explicitly and never read off ``claim``: reading it off the claim is the defect.
        subject: the store this verdict is about, or ``None`` for a verdict bound to no store.
        verifier_version: the comparator generation the verdict was produced under.
        catalog_snapshot: the id of the snapshot it was decided against.
        evidence_refs, confidence, reason: the verifier's own record, carried for the reader.
            They are NOT sealed — they explain a verdict, they do not decide one — so nothing
            downstream may branch on them.
    """
    source: Mapping[str, Any] = claim if isinstance(claim, Mapping) else {}
    out = {k: v for k, v in source.items() if k not in (ATTESTATION_FIELD, "status")}
    payload = _payload(
        key=out.get("key"),
        value=out.get("value"),
        unit=out.get("unit"),
        subject=subject,
        status=status,
        verifier_version=verifier_version,
        catalog_snapshot=catalog_snapshot,
    )
    attestation: dict[str, Any] = dict(payload)
    attestation["evidence_refs"] = [str(ref) for ref in evidence_refs or ()]
    attestation["confidence"] = None if confidence is None else float(confidence)
    attestation["reason"] = None if reason is None else str(reason)
    attestation["seal"] = _seal(payload)
    out[ATTESTATION_FIELD] = attestation
    return out


def sealed_status(
    attestation: Any,
    *,
    key: Any,
    value: Any,
    unit: Any = None,
    subject: Any = None,
) -> str | None:
    """The status this exchange sealed for this claim, or ``None``.

    ``None`` for every way of not being an exchange verdict, deliberately collapsed into one
    answer so no caller can treat some of them as milder than others: no attestation, an
    attestation that is not a mapping, a missing or non-string seal, a seal that does not
    match, and a seal whose subject is not this candidate's store. An unreadable verdict is
    not a permissive verdict (R12's rule, applied to the same shape).

    The comparison is :func:`hmac.compare_digest` over the whole digest. It is not a
    performance note: a short-circuiting ``==`` over a MAC is a timing oracle, and this one is
    reachable from an unauthenticated ``POST /auctions``.
    """
    if not isinstance(attestation, Mapping):
        return None
    presented = attestation.get("seal")
    if not isinstance(presented, str) or not presented:
        return None
    status = attestation.get("status")
    if status is None:
        return None
    declared_subject = attestation.get("subject")
    if declared_subject is not None and str(declared_subject) != str(subject or ""):
        return None
    expected = _seal(
        _payload(
            key=key,
            value=value,
            unit=unit,
            subject=declared_subject,
            status=status,
            verifier_version=attestation.get("verifier_version"),
            catalog_snapshot=attestation.get("catalog_snapshot"),
        )
    )
    if expected is None:
        return None
    if not hmac.compare_digest(expected, presented):
        return None
    return str(status)
