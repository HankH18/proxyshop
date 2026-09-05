"""Turn a store's sealed envelope into the activation decision the bidding path acts on.

One reader, one producer, one vocabulary. The consumer this is written against is
``packages/store-agent/src/modes/runner.py``'s ``_envelope_states``, which reads
``context["envelope"]["activation"]`` and coerces it through
``contracts.EnvelopeActivation`` — so that is the shape produced here, and the value is the
envelope's own ``activation`` string rather than some second spelling of "on".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from merchant_svc.envelope import ENVELOPES, SHADOW, EnvelopeVersions, UnknownStore

__all__ = ["BiddingDecision", "store_agent_context", "store_may_bid"]


@dataclass(frozen=True)
class BiddingDecision:
    """Whether a store may bid, and the envelope state that decided it.

    ``reason`` is carried because "this store is not bidding" is an operator-facing fact:
    "we never onboarded them", "the owner has not approved the terms yet" and "somebody hit
    the kill switch" are three different situations behind one ``False``.
    """

    store_id: str
    activation: str
    version: int | None
    may_bid: bool
    reason: str


def _decide(store_id: str, versions: EnvelopeVersions | None) -> BiddingDecision:
    store = versions if versions is not None else ENVELOPES
    try:
        envelope = store.current(store_id)
    except UnknownStore:
        # Fail closed, and in the SAME state the store-agent's gate defaults to, so the two
        # halves of the seam cannot disagree about what "no envelope" means.
        return BiddingDecision(
            store_id=store_id,
            activation=SHADOW,
            version=None,
            may_bid=False,
            reason="no envelope has ever been recorded for this store",
        )

    # `is_live` is the accessor, and it is asked rather than re-derived here: spelling the
    # equality a second time is how a fourth activation state eventually gets treated as
    # live by one of the two copies.
    live = store.is_live(store_id)
    if live:
        reason = "the store's current envelope is active"
    elif envelope.activation == SHADOW:
        reason = "the store's current envelope is in shadow and has not been approved live"
    else:
        reason = f"the store's current envelope is {envelope.activation!r}"
    return BiddingDecision(
        store_id=store_id,
        activation=envelope.activation,
        version=envelope.version,
        may_bid=live,
        reason=reason,
    )


def store_may_bid(store_id: str, *, versions: EnvelopeVersions | None = None) -> bool:
    """``True`` only when this store's current envelope is approved and active.

    Args:
        store_id: the store being asked about.
        versions: the history to ask. Defaults to the service-wide
            :data:`~merchant_svc.envelope.store.ENVELOPES`.

    An unknown store, a store in shadow and a killed store are all ``False``, and so is any
    activation this code does not recognise — the question is answered by equality against
    the one live value, never by "not killed".
    """
    return _decide(store_id, versions).may_bid


def store_agent_context(
    store_id: str, *, versions: EnvelopeVersions | None = None
) -> dict[str, Any]:
    """The store's envelope, in the shape the store-agent runner's activation gate reads.

    Returns a mapping carrying ``envelope`` with at least ``activation`` — what
    ``packages/store-agent/src/modes/runner.py:_envelope_states`` looks for — plus the
    decision this service already made, so a consumer that wants the *verdict* rather than
    the vocabulary does not have to re-derive it and risk deriving it differently.

    A store with no envelope still gets an ``envelope`` key, holding ``shadow``. Omitting it
    would also fail closed today, because the runner's gate defaults a missing activation to
    ``shadow`` — but it would fail closed by relying on the *consumer's* default rather than
    by stating the producer's own answer, and the day that default is loosened this seam
    would start granting activation by silence.
    """
    decision = _decide(store_id, versions)
    envelope: dict[str, Any] = {
        "store_id": decision.store_id,
        "activation": decision.activation,
    }
    if decision.version is not None:
        envelope["version"] = decision.version
    return {
        "store_id": decision.store_id,
        "envelope": envelope,
        "may_bid": decision.may_bid,
        "reason": decision.reason,
    }
