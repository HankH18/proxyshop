"""Where the served bid path gets the store it advocates for.

``runtime.bid(request, context)`` needs two things and the wire carries only one. A `BidRequest`
names an auction, an intent and a buyer profile; it does **not** name a store, because a hosted
agent process *is* one store's advocate — the exchange picks which agent to solicit by choosing
which URL to POST to. So the store context is deployment configuration, and this module is the
one place that resolves it.

Three sources, in this order:

``configure_solicitation(app, context=…)``
    An explicit wiring. What tests use, and what a composition root that already holds the
    merchant's context in memory should use.

``STORE_AGENT_CONTEXT``
    A path to a JSON file holding the context. This is what the container gets: the Dockerfile
    runs ``uvicorn store_agent.main:app`` and nothing in that path could call a configure
    function, so without a file-shaped source the shipped image can serve the route and still
    never bid.

nothing
    The agent declines every solicitation, and says so in the response. Fail closed: an agent
    with no envelope has no authorization to offer anything, and inventing a context would be
    inventing the merchant's approval. This is not a hole in the auction — ``collect_bids``
    represents a silent store at catalogue list price (R10), which is exactly the case R10 is
    for.

**The domain, and why it is here.** ``AuctionContext`` reads the store's registered domain off
the context (:data:`~store_agent.runtime.STORE_DOMAIN_KEYS`) and an offer that has none carries
no ``checkout_url``, which costs the store every shortlist slot. A context file written by the
merchant service will state it; a hand-assembled one (the approved-envelope fixtures, say) will
not, so :data:`DOMAIN_ENV` lets the operator state it beside the file without editing an
approved artifact. It is overlaid **only when the file states none** — a file that names a
domain is the merchant's own word and outranks a deployment flag.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "CONTEXT_ENV",
    "DOMAIN_ENV",
    "StoreContextError",
    "configure_solicitation",
    "load_context_from_env",
    "store_context",
]

#: Path to a JSON file holding this agent's store context.
CONTEXT_ENV = "STORE_AGENT_CONTEXT"

#: The store's registered domain, used only when the context file states none.
DOMAIN_ENV = "STORE_AGENT_STORE_DOMAIN"

#: Set on ``app.state``. Read through :func:`store_context` rather than by name, so "has this app
#: been configured" is one question with one answer.
STATE_ATTR = "store_context"


class StoreContextError(RuntimeError):
    """The configured store context cannot be read. Raised at configuration time, never per-bid.

    Deliberately loud, and deliberately at *startup*: a container told to load a context file
    that does not parse must fail to boot rather than answer every solicitation with a decline
    that looks exactly like "this store chose not to bid".
    """


def configure_solicitation(app: Any, *, context: Mapping[str, Any] | None) -> None:
    """Give ``app`` the store context its bid route will answer from. ``None`` clears it."""
    setattr(app.state, STATE_ATTR, dict(context) if context is not None else None)


def store_context(app: Any) -> dict[str, Any] | None:
    """The store context this app bids for, or ``None`` when it has none.

    Resolved once per app and then cached on ``app.state``, so a route does not read the
    filesystem per request and so an operator changing the environment mid-process does not
    change what a running agent is authorized to offer half-way through.
    """
    state = app.state
    if hasattr(state, STATE_ATTR):
        return getattr(state, STATE_ATTR)
    resolved = load_context_from_env()
    setattr(state, STATE_ATTR, resolved)
    return resolved


def load_context_from_env(environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """The store context named by :data:`CONTEXT_ENV`, or ``None`` when the variable is unset.

    Raises :class:`StoreContextError` when the variable IS set and the file is missing, is not
    JSON, or is not a JSON object. An unset variable and an unreadable file are different
    conditions and only the first is a decision; conflating them is how a typo'd path becomes a
    store that silently never bids.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get(CONTEXT_ENV) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StoreContextError(f"{CONTEXT_ENV}={raw!r} cannot be read: {exc}") from exc
    except ValueError as exc:
        raise StoreContextError(f"{CONTEXT_ENV}={raw!r} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise StoreContextError(
            f"{CONTEXT_ENV}={raw!r} must hold a JSON object (the store context), got "
            f"{type(loaded).__name__}"
        )
    return _completed(dict(loaded), env)


def _completed(context: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """The loaded document, filled in from the two facts a fixture file tends not to carry.

    ``store_id`` is copied off the envelope when the document states none, which is what lets an
    approved-envelope fixture — ``{"envelope": {...}, "catalog": {...}}``, the shape this repo
    already ships under ``fixtures/envelopes/`` — be used as a context unchanged. ``bid()``
    declines an unidentified request, so a document that names the store only inside its envelope
    would otherwise be a context that loads and cannot bid.

    The domain is overlaid only when neither the context nor its envelope states one; see the
    module docstring for why the file wins.
    """
    envelope = context.get("envelope")
    if not context.get("store_id") and isinstance(envelope, Mapping):
        store_id = envelope.get("store_id")
        if store_id:
            context["store_id"] = str(store_id)

    domain = str(env.get(DOMAIN_ENV) or "").strip()
    if domain and not _states_a_domain(context) and not _states_a_domain(envelope):
        context["store_domain"] = domain
    return context


def _states_a_domain(source: Any) -> bool:
    # Imported here rather than at module scope so this module stays importable by tooling that
    # has not put `.pkgroot` on the path yet; the runtime package is a heavier import than a
    # configuration reader needs at definition time.
    from ..runtime import STORE_DOMAIN_KEYS  # noqa: PLC0415

    if not isinstance(source, Mapping):
        return False
    return any(str(source.get(key) or "").strip() for key in STORE_DOMAIN_KEYS)
