"""``buyer_svc.auctions`` — the read-only view of one auction the buyer's page needs.

One route, and it exists because of a measured gap rather than a preference::

    GET /buyer/auctions/{auction_id}   -> the LIVE shortlist + the RECORDED diagnostics

``apps/exchange/src/auction/routes.py``'s ``read_auction`` answers with auction *state* —
``auction_id``, ``state``, ``intent_id``, ``cluster_id``, ``accepted_bid_ref``, ``history``
— and nothing else. There is no ``entries``, no ``excluded``, no ``denied``, no ``ranked``
on that door, so the exchange publishes the answer to "why is my shortlist empty" exactly
once, in the body of its ``POST /auctions`` reply. :mod:`buyer_svc.composition`'s client
keeps that body; this package serves it.

It is a **feature router** rather than a function on the composition root because
``buyer_svc.main.create_app`` is orchestrator-frozen (B6(iii)) and mounts routers by
globbing ``<feature>/routes.py``. A route defined anywhere else is a route
``uvicorn buyer_svc.main:app`` — what ``apps/buyer/Dockerfile`` runs — never serves.

This package holds no state of its own: the record lives on the one exchange client the
composition root wired, which is the same object ``buyer_svc.intent`` opened the auction
with. See :mod:`buyer_svc.auctions.routes`.
"""

from __future__ import annotations

# `buyer_svc.intent._spellings` is a general facility over the two dotted spellings of
# `apps/buyer/svc/src`, not an intent-specific one — `buyer_svc.composition` imports it the
# same way. This tree is reachable as `buyer_svc.auctions` AND as
# `apps.buyer.svc.src.auctions`; left alone Python executes every file here twice, and two
# `router` objects out of one `APIRouter(...)` call is two routers whose routes are
# registered on whichever app imported which spelling.
from ..intent._spellings import bind_package

__all__: list[str] = []

bind_package(__name__)
