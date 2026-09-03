"""The public orchestration boundary — where the R12 eligibility gates actually live (D54).

Two callables, two owners, one package:

===================================================  =======  ================================
callable                                             ticket   gate
===================================================  =======  ================================
``solicit_bids(roster=, solicitor=, eligibility=,     T-030    before solicitation
now=)``
``accept_offer(auction=, bid_ref=, code_creator=,     T-033    before checkout (re-read)
mode=, eligibility=)``
===================================================  =======  ================================

They sit **above** the pure functions ``collect_bids(roster, responses, now)`` and
``accept(auction, bid_id, code_creator, mode)``, which keep their positional signatures and
gain no required parameter. Those two compute correctly on clean input; this layer is what
decides the input is clean, and R12's fail-closed promise is a property of this layer.

⚠️ **T-033 adds ``accept_offer`` here and must not modify ``solicit_bids``.** Done: the gate
itself lives in :mod:`apps.exchange.src.accept.gate`, beside the ``accept`` it guards, and is
re-exported below. Putting the *implementation* in the accept package rather than in a module
here keeps this file's diff to one import — ``solicitation.py`` is untouched, which matters
because a separate ticket owns it.
"""

from __future__ import annotations

from ..accept import accept_offer
from .solicitation import Denial, SolicitationResult, solicit_bids

__all__ = ["Denial", "SolicitationResult", "accept_offer", "solicit_bids"]
