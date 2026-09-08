"""The version algebra: edit, activate, kill, revive. Every one returns a new envelope.

Four rules hold the safety property together, and each exists because dropping it turns a
merchant's approval into a formality:

1. **An edit produces an unapproved version.** ``edit_envelope`` bumps the version, clears
   the recorded approval and drops the new version back to ``shadow``. Carrying ``active``
   forward would let a merchant approve a 20 % cap, edit the cap to 99 %, and have the store
   bidding at 99 % under an approval nobody ever gave for those terms.
2. **Nothing lifts a kill except a kill being lifted.** Editing a killed envelope does not
   quietly revive it — the state is carried forward — and ``activate_envelope`` refuses a
   killed version outright, whatever paperwork accompanies it. The one door out is
   :func:`revive_envelope`, which is a merchant saying *restart this store* and nothing else:
   it names no terms, carries no approval and cannot be arrived at as a side effect of an
   edit or an activation.
3. **A revive lands in ``shadow``, never in ``active``.** Reversing the kill switch and going
   back on the network are two separate acts, and only the second one takes a written
   approval. So a revived store is stopped-but-restartable: ``is_live`` is still ``False``,
   the agent still submits nothing, and the merchant must approve the terms again — through
   the same gate every other live envelope came through — before a single bid leaves.
4. **Activation does not bump the version.** The approval on file is bound to a digest of
   *this* version's terms; minting a new version at activation time would produce a live
   document that its own approval no longer covers. ``kill_envelope`` and
   :func:`revive_envelope` do not bump it either, for the same reason: they change the
   version's lifecycle state, not the terms anybody agreed to.

**Why the kill is reversible at all.** It was not, and the owner ruled that a merchant must be
able to restart their agent. Before this, ``put`` on a killed store minted a version that was
*born killed*, activation refused it, and the refusal's own advice — "publish a new version and
approve that" — named the thing that had just been done. Reversibility is therefore a verb of
its own rather than a softening of rules 1 and 2, which are unchanged and still measured.
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
    ReviveRefused,
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
    #
    # This line survives the kill switch becoming reversible, and it is the guard that makes
    # the reversal safe to have: restarting a store is a merchant saying *restart this store*
    # (`revive_envelope`), never a merchant saving new terms. If an edit reset the state, then
    # every PUT — the dashboard's envelope editor, a re-submitted interview, an integration
    # syncing floors on a schedule — would un-stop a store nobody meant to un-stop, and the
    # kill switch would hold only until the next save.
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
        # The remedy this names has to be one that WORKS. It used to read "publish a new
        # version and approve that", which was the one thing that could not work: `put` on a
        # killed store mints a version that is itself killed (see `edit_envelope`), so the
        # merchant who followed the advice arrived back at this exact refusal, one version
        # later. Restarting is now its own act, and it is the only one that clears this state.
        raise ApprovalRejected(
            f"store {current.store_id!r} envelope v{current.version} has been killed; a "
            "killed envelope is never reactivated by an approval, however well bound — "
            "restart the store first (POST /stores/{store_id}/revive), which brings it back "
            "in shadow, and approve these terms then"
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

    That the kill is now reversible (:func:`revive_envelope`) changes nothing here. Stopping
    still takes one call, no countersignature and no state check; a merchant watching their
    agent misbehave presses this and it stops, and every extra condition anyone is tempted to
    add to this function is a way for it not to.
    """
    current = Envelope.from_obj(envelope)
    return current.with_activation(KILLED, current.approval)


def revive_envelope(envelope: Any) -> Envelope:
    """Lift the kill on ``envelope``, returning it to ``shadow`` at the same version.

    This is the *whole* reversal of the kill switch, and it is deliberately half of a restart:
    it un-stops the store without putting it back on the network. What comes back is a version
    that is not live, carries no approval, and is exactly as unauthorized to bid as any freshly
    edited one — so the way back to ``active`` is the ordinary approval gate
    (:func:`activate_envelope`), which is where the merchant's written approval belongs and the
    only place it has ever been checked.

    Two things it refuses to be:

    * **not a reactivation.** It cannot produce ``active``, so no amount of reviving puts a
      store back on the network; something has to approve the terms afterwards, and that
      something is refused without a bound artifact.
    * **not a general "set this to shadow".** An envelope that is not killed is refused, so
      this can never be used to quietly take a live store off the network without filing the
      ``killed`` state the kill-switch card reads.

    The approval is dropped rather than carried forward, which is the difference between this
    and :func:`kill_envelope`. A kill keeps the artifact because it is the record of what the
    store *was* running; a revive is the moment the store stops being stopped, and leaving the
    pre-kill signature attached to a restartable version would leave the record saying that
    somebody approved this store being live *after* someone else stopped it. Nobody did.

    Raises:
        ReviveRefused: ``envelope`` is not killed, so there is no kill to lift.
    """
    current = Envelope.from_obj(envelope)
    if current.activation != KILLED:
        raise ReviveRefused(
            f"store {current.store_id!r} envelope v{current.version} is {current.activation!r}, "
            f"not {KILLED!r}; a revive lifts the kill switch and does nothing else — it is not "
            "a way to deactivate a live envelope, which is what the kill switch is for"
        )
    return current.with_activation(SHADOW, None)
