"""Who may bid, and the context the store-agent runtime reads that decision out of.

The envelope package answers *what the merchant agreed to*. This package answers the one
question the rest of the system actually asks of it — **may this store bid right now** — and
hands that answer over in the shape the consumer already reads.

Why it exists (T-246)
---------------------
``is_live`` was computed in two places and read in none. ``T-053`` built the kill switch and
the shadow default correctly and unit-tested both, and
``packages/store-agent/src/modes/runner.py`` built an activation gate
(``_envelope_states``) that fails closed on a missing or unreadable activation — but nothing
anywhere turned an envelope into the context that gate reads, so no executed path
demonstrated that a killed store actually stops bidding. A decision the product computes and
nobody asks for is not wired up, however well it is tested.

This module is the merchant half of that seam: :func:`store_agent_context` produces exactly
the mapping ``_envelope_states`` consumes, and :func:`store_may_bid` is the same decision as
a bare boolean for callers inside this service. The store-agent half — actually calling this
when it builds a runner context — lives in ``packages/store-agent``, which this ticket does
not own; it is reported rather than reached into.

Fail-closed, on both sides of the seam
--------------------------------------
A store with no envelope on file, or one whose envelope is in any state that is not exactly
``active``, is reported as ``shadow`` and may not bid. That is deliberately the *same*
default ``_envelope_states`` applies to a context it cannot read, so the two halves of the
seam cannot disagree about what silence means: an activation is granted by an approved
envelope, never by the absence of evidence against one.
"""

from __future__ import annotations

from .gate import BiddingDecision, store_agent_context, store_may_bid

__all__ = ["BiddingDecision", "store_agent_context", "store_may_bid"]
