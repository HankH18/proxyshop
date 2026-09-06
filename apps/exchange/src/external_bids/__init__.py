"""The signed external bid door the exchange publishes (T-244).

``packages/contracts/openapi/exchange.openapi.json`` declares
``POST /v1/auctions/{auction_id}/bids`` — ``submitExternalBid`` — and until this package
existed nothing served it. The guard for that direction is
:func:`store_agent.external.door.receive_bid`, which lives under ``packages/store-agent``
because it is the code a Tier-2 *seller* is judged by; the door it stands in is the
exchange's, which is why the route is here and not there.
"""

from __future__ import annotations

__all__ = ["router"]


def __getattr__(name: str) -> object:
    """Expose ``router`` without importing the route module at package-import time.

    ``exchange.main.create_app`` imports ``<feature>.routes`` directly, so nothing here has
    to eagerly pull FastAPI in for the sake of a re-export.
    """
    if name == "router":
        from .routes import router  # noqa: PLC0415 - deferred on purpose, see docstring

        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
