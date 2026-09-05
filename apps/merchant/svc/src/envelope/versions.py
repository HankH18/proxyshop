"""The version algebra: edit, activate, kill. Every one returns a new envelope.

Three rules hold the safety property together, and each exists because dropping it turns a
merchant's approval into a formality:

1. **An edit produces an unapproved version.** ``edit_envelope`` bumps the version, clears
   the recorded approval and drops the new version back to ``shadow``. Carrying ``active``
   forward would let a merchant approve a 20 % cap, edit the cap to 99 %, and have the store
   bidding at 99 % under an approval nobody ever gave for those terms.
2. **A killed envelope stays killed.** Editing one does not quietly revive it, and
   ``activate_envelope`` refuses it outright. The kill switch is the one control that has to
   work when everything else is wrong, so nothing here clears it implicitly.
3. **Activation does not bump the version.** The approval on file is bound to a digest of
   *this* version's terms; minting a new version at activation time would produce a live
   document that its own approval no longer covers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .digest import approval_covers, approval_digest
from .model import (
    ACTIVE,
    EDITABLE_FIELDS,
    KILLED,
    SHADOW,
    ApprovalArtifact,
    ApprovalRejected,
    Envelope,
    EnvelopeEditRefused,
)


def edit_envelope(envelope: Any, changes: Mapping[str, Any]) -> Envelope:
    """Apply ``changes`` to ``envelope``, returning the next version.

    Args:
        envelope: the version being edited. It is never mutated — it cannot be.
        changes: a mapping of :data:`~merchant_svc.envelope.model.EDITABLE_FIELDS` to their
            new values. Must not be empty: an "edit" that changes nothing would still mint a
            version and silently deactivate the live one.

    Returns:
        A new :class:`~merchant_svc.envelope.model.Envelope` at ``version + 1``, in
        ``shadow`` (or still ``killed``), carrying no approval.

    Raises:
        EnvelopeEditRefused: ``changes`` is empty, is not a mapping, or names a field an edit
            may not set — ``version`` (derived), ``activation`` (a lifecycle transition, and
            the field this whole module exists to protect), ``store_id`` (whose envelope this
            is), ``approval`` (minted, never asserted), or a field the Envelope has no room
            for at all.
        EnvelopeInvalid: the edited document is not a valid Envelope — a cap above 100, a
            negative floor, a malformed commitment.
    """
    current = Envelope.from_obj(envelope)

    if not isinstance(changes, Mapping):
        raise EnvelopeEditRefused(
            f"an envelope edit is a mapping of field to new value, not a {type(changes).__name__}"
        )
    if not changes:
        raise EnvelopeEditRefused(
            "an envelope edit that changes nothing still mints a version and drops the live "
            "one back to shadow; say what is changing"
        )

    refused = sorted(set(map(str, changes.keys())) - set(EDITABLE_FIELDS))
    if refused:
        raise EnvelopeEditRefused(
            f"these envelope fields are not editable: {refused}; editable fields are "
            f"{list(EDITABLE_FIELDS)} (version is derived, activation goes through "
            "activate/kill, and store_id names whose envelope this is)"
        )

    document = current.to_dict()
    document.update({str(key): value for key, value in changes.items()})
    document["version"] = current.version + 1
    # A killed envelope that is edited stays killed; anything else lands in shadow. Both are
    # "not live", which is the only property that matters here.
    document["activation"] = KILLED if current.activation == KILLED else SHADOW
    return Envelope.from_obj(document, approval=None)


def activate_envelope(envelope: Any, approval: Any) -> Envelope:
    """Flip ``envelope`` to ``active`` against a recorded written approval.

    Args:
        envelope: the version being activated.
        approval: the written approval artifact — ``approver``, ``approved_at`` and an
            ``envelope_hash`` equal to :func:`~merchant_svc.envelope.digest.approval_digest`
            of this very envelope.

    Returns:
        A new envelope at the same version, ``activation == "active"``, carrying the artifact
        that authorized it.

    Raises:
        ApprovalRejected: there is no artifact, it is incomplete, its timestamp is unusable,
            it is bound to a different envelope, or the envelope has been killed.
    """
    current = Envelope.from_obj(envelope, approval=None)

    if current.activation == KILLED:
        raise ApprovalRejected(
            f"store {current.store_id!r} envelope v{current.version} has been killed; it is "
            "not reactivated by an approval — publish a new version and approve that"
        )

    artifact = ApprovalArtifact.parse(approval)
    if not approval_covers(current, artifact.envelope_hash):
        raise ApprovalRejected(
            f"the approval by {artifact.approver!r} is bound to {artifact.envelope_hash!r}, "
            f"which is not store {current.store_id!r} envelope v{current.version} "
            f"({approval_digest(current)!r}); an approval only activates the exact terms it "
            "was given for"
        )
    return current.with_activation(ACTIVE, artifact)


def kill_envelope(envelope: Any) -> Envelope:
    """Move ``envelope`` to ``killed`` immediately. No approval, no version bump, no excuse.

    The kill switch takes no artifact on purpose: stopping is always allowed, and a control
    that could be refused for a paperwork reason is not a kill switch. The approval that was
    on file stays attached — it is the record of what the store *was* running.
    """
    current = Envelope.from_obj(envelope)
    return current.with_activation(KILLED, current.approval)
