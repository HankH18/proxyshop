"""``buyer_svc.window`` — the served reader for ``app.buyer_accounts`` (T-142).

One route, ``GET /buyer/store-window``, discovered and mounted by the frozen
:func:`buyer_svc.main.create_app`. See :mod:`buyer_svc.window.routes` for who may read it,
what is coarsened before it leaves, and why this package contains no notion of seeded data.
"""

from __future__ import annotations

__all__: list[str] = []
