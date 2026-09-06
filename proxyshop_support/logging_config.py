"""The one place application logging is configured (T-308).

Before this module, **nothing anywhere in the repo configured logging**: a repo-wide grep
for ``basicConfig|dictConfig|FileHandler|structlog|logging.config`` across ``apps/``,
``packages/``, ``services/``, ``proxyshop_support/`` and ``scripts/`` returned zero hits.
The measured consequence is not "logs are ugly", it is that they do not exist: a freshly
started service process has ``root.handlers == []`` at effective level ``30``, so every
``_log.info(...)`` in the product is dropped *before a record is even constructed*, and only
``WARNING`` and above reaches stderr — through ``logging.lastResort``, which has no
timestamp, no logger name and no format. Six INFO call sites were invisible that way,
including the ones an operator actually needs: the lossy pixel, the un-minted discount code,
the envelope that stays in shadow, the webhook handed to the default sink, and the recorded
feedback.

Design notes, each of which is load bearing rather than taste:

**No import side effects.** Importing this module configures nothing. ``logging`` is
process-global state, so configuring it at import time would reconfigure the logging of
every test that happens to import anything that imports this — the neighbour-corrupting
behaviour T-247 exists to punish. Configuration happens only when something *calls*
:func:`configure_logging`, which services do on their startup path.

**Idempotent, and it says so.** :func:`configure_logging` returns ``True`` the first time
and ``False`` afterwards, so a service that calls it from both ``create_app()`` and a
lifespan hook installs one handler, not two, and does not print every line twice. The
handlers it owns are tagged (:data:`OWNED_BY`) rather than identified by type, so a handler
installed by uvicorn, pytest or an operator is never removed by mistake.

**A level, always.** ``logging.basicConfig()`` with no ``level=`` leaves the root logger on
``WARNING``, which is the defect wearing a fix's clothes. The level here always comes from
somewhere explicit: the argument, else ``$PROXYSHOP_LOG_LEVEL``, else :data:`DEFAULT_LEVEL`
(``INFO``). An unparseable value raises rather than falling back — a service that was told
``LOG_LEVEL=verbose`` and silently logged nothing is the failure this module exists to end.

**Delivery, not merely a handler.** ``root.setLevel(INFO)`` plus a ``NullHandler`` satisfies
every "is logging configured" grep and shows an operator precisely nothing. The handler
installed here writes to a real stream (``sys.stderr`` by default, so stdout stays clean for
programs that pipe it), and ``$PROXYSHOP_LOG_FILE`` adds a real file alongside it.

**Correlation ids.** ``$PROXYSHOP_LOG_FORMAT=json`` emits one JSON object per line for an
aggregator, and every record — text or JSON — carries a ``request_id`` field maintained in a
:class:`~contextvars.ContextVar`. :class:`RequestIdMiddleware` is a plain ASGI middleware
(no framework import) that mints or adopts one per request and echoes it in the response
header, so a request can be followed across services by grepping one id.

**Why the standard library and not ``structlog``.** ``structlog`` is a declared dependency
with zero importers, and it is tempting to reach for it here. It is not what is broken:
``structlog``'s default configuration still renders *through* the stdlib root logger, so
without the configuration below it would be just as silent. Configuring the stdlib root is
the load-bearing half and the half every existing ``logging.getLogger(__name__)`` call site
in the product already speaks to; a ``structlog`` layer can be added on top of this later
without moving any of it.

Usage, on a service's startup path::

    from proxyshop_support.logging_config import RequestIdMiddleware, configure_logging

    def create_app() -> FastAPI:
        configure_logging()
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)
        ...
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, TextIO

__all__ = [
    "DEFAULT_LEVEL",
    "FILE_ENV",
    "FORMAT_ENV",
    "LEVEL_ENV",
    "OWNED_BY",
    "REQUEST_ID_HEADER",
    "JsonFormatter",
    "RequestIdFilter",
    "RequestIdMiddleware",
    "bind_request_id",
    "configure_logging",
    "current_request_id",
    "new_request_id",
    "request_id_scope",
    "reset_logging",
    "resolve_level",
]

#: Read when ``configure_logging(level=...)`` is not given one. A name (``"debug"``) or a
#: number (``"10"``); anything else raises.
LEVEL_ENV = "PROXYSHOP_LOG_LEVEL"

#: When set, records are ALSO written to this file. The stream handler stays: a file sink
#: that silently replaced stderr would make ``docker logs`` empty.
FILE_ENV = "PROXYSHOP_LOG_FILE"

#: ``"text"`` (default) or ``"json"``. JSON is one object per line, for an aggregator.
FORMAT_ENV = "PROXYSHOP_LOG_FORMAT"

#: The level used when neither the caller nor the environment names one. INFO rather than
#: WARNING on purpose: WARNING is exactly the level at which this system was already dark.
DEFAULT_LEVEL = "INFO"

#: Stamped on the handlers this module installs. Ownership is recorded rather than inferred
#: from the handler class, so :func:`reset_logging` can never remove pytest's, uvicorn's or
#: an operator's handler — only the ones installed here.
OWNED_BY = "_proxyshop_logging_owner"

#: The request-correlation header :class:`RequestIdMiddleware` reads and echoes.
REQUEST_ID_HEADER = "x-request-id"

#: The value a record carries when it was emitted outside any request scope. A printable
#: placeholder rather than ``None`` so the text format stays column-aligned.
NO_REQUEST_ID = "-"

_TEXT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_request_id: ContextVar[str] = ContextVar("proxyshop_request_id", default=NO_REQUEST_ID)


# --------------------------------------------------------------------------------------
# correlation ids
# --------------------------------------------------------------------------------------


def current_request_id() -> str:
    """The correlation id in force on this task/thread, or :data:`NO_REQUEST_ID`."""
    return _request_id.get()


def new_request_id() -> str:
    """A fresh correlation id. Short enough to read in a terminal, wide enough to be unique."""
    return uuid.uuid4().hex[:16]


def bind_request_id(value: str | None = None) -> Token[str]:
    """Bind ``value`` (or a fresh id) as the current correlation id.

    Returns the :class:`~contextvars.Token` needed to restore the previous value. Prefer
    :func:`request_id_scope`, which cannot leak the binding.
    """
    return _request_id.set(value or new_request_id())


@contextmanager
def request_id_scope(value: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the duration of the block, then restore the previous one."""
    token = bind_request_id(value)
    try:
        yield _request_id.get()
    finally:
        _request_id.reset(token)


class RequestIdFilter(logging.Filter):
    """Stamps :func:`current_request_id` onto every record that reaches the handler.

    Attached to the *handler* rather than to a logger, so it applies to every record the
    handler is asked to emit whatever logger produced it. Without that, a record from a
    logger nobody thought to decorate would hit ``%(request_id)s`` with no such attribute,
    the formatter would raise, and — since ``logging`` swallows handler errors — the line
    would silently vanish. That failure mode is the one this whole module is here to remove,
    so it must not be reintroduced by the formatter.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger, correlation id, message.

    ``exc_info``/``stack_info`` are rendered into the same object rather than as trailing
    free text, so a traceback survives an aggregator that treats a line as a record.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, _DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", NO_REQUEST_ID),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        # `default=str` so a record carrying an unserialisable extra logs a repr instead of
        # raising inside the handler, which `logging` would swallow and drop the line for.
        return json.dumps(payload, default=str)


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


def resolve_level(value: str | int | None) -> int:
    """The numeric level for ``value``, or the environment's, or :data:`DEFAULT_LEVEL`.

    Raises:
        ValueError: if a level was named and is not a level. Falling back to a default here
            would mean ``PROXYSHOP_LOG_LEVEL=verbose`` silently logs nothing, which is
            indistinguishable from the defect this module closes.
    """
    if value is None:
        value = os.environ.get(LEVEL_ENV) or DEFAULT_LEVEL
    if isinstance(value, int):
        return value
    text = value.strip()
    if text.isdigit():
        return int(text)
    named = logging.getLevelName(text.upper())
    if not isinstance(named, int):
        raise ValueError(
            f"{text!r} is not a logging level. Set {LEVEL_ENV} to one of "
            f"DEBUG, INFO, WARNING, ERROR, CRITICAL, or to a number."
        )
    return named


def _owned_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [handler for handler in logger.handlers if getattr(handler, OWNED_BY, False)]


def _build_formatter(fmt: str | None) -> logging.Formatter:
    chosen = (fmt or os.environ.get(FORMAT_ENV) or "text").strip().lower()
    if chosen == "json":
        return JsonFormatter()
    if chosen == "text":
        return logging.Formatter(_TEXT_FORMAT, datefmt=_DATE_FORMAT)
    raise ValueError(f"{chosen!r} is not a log format. Set {FORMAT_ENV} to 'text' or 'json'.")


def configure_logging(
    *,
    level: str | int | None = None,
    stream: TextIO | None = None,
    log_file: str | os.PathLike[str] | None = None,
    fmt: str | None = None,
    force: bool = False,
) -> bool:
    """Install this process's application logging. Safe to call more than once.

    Args:
        level: a level name or number. Defaults to ``$PROXYSHOP_LOG_LEVEL``, else ``INFO``.
        stream: where text goes. Defaults to ``sys.stderr`` — resolved at call time, not at
            import time, so a caller that has redirected ``sys.stderr`` is honoured.
        log_file: an additional file sink. Defaults to ``$PROXYSHOP_LOG_FILE`` if set.
        fmt: ``"text"`` or ``"json"``. Defaults to ``$PROXYSHOP_LOG_FORMAT``, else text.
        force: replace the handlers this module previously installed. Without it a second
            call is a no-op, which is what makes calling from both ``create_app()`` and a
            lifespan hook safe.

    Returns:
        ``True`` if handlers were installed by this call, ``False`` if a previous call had
        already configured logging and ``force`` was not set.
    """
    root = logging.getLogger()
    existing = _owned_handlers(root)
    if existing and not force:
        return False

    for handler in existing:
        root.removeHandler(handler)
        handler.close()

    resolved = resolve_level(level)
    formatter = _build_formatter(fmt)

    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stderr)]
    destination = log_file if log_file is not None else os.environ.get(FILE_ENV) or None
    if destination:
        handlers.append(logging.FileHandler(destination, encoding="utf-8"))

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RequestIdFilter())
        setattr(handler, OWNED_BY, True)
        root.addHandler(handler)

    # The ROOT level, not a per-handler one. A handler at INFO under a root still at WARNING
    # never sees an INFO record at all: `Logger.isEnabledFor` rejects it before any handler
    # is consulted, which is exactly how six INFO call sites were dark.
    root.setLevel(resolved)
    return True


def reset_logging() -> int:
    """Remove only the handlers this module installed. Returns how many were removed.

    For tests and for a process that reconfigures itself. Handlers belonging to pytest,
    uvicorn or an operator are left alone — hence the ownership tag rather than a class check
    or a blanket ``root.handlers.clear()``.
    """
    root = logging.getLogger()
    removed = _owned_handlers(root)
    for handler in removed:
        root.removeHandler(handler)
        handler.close()
    return len(removed)


# --------------------------------------------------------------------------------------
# request correlation over ASGI
# --------------------------------------------------------------------------------------

_Scope = MutableMapping[str, Any]
_Message = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]
_App = Callable[[_Scope, _Receive, _Send], Awaitable[None]]


class RequestIdMiddleware:
    """Adopt or mint a correlation id per HTTP request, and echo it back to the caller.

    Deliberately a plain ASGI callable rather than a Starlette ``BaseHTTPMiddleware``: this
    package is imported by things that must not depend on a web framework, and the ASGI
    signature is what ``app.add_middleware(RequestIdMiddleware)`` wants anyway.

    Non-HTTP scopes (``lifespan``, ``websocket``) are passed straight through untouched.
    """

    def __init__(self, app: _App, header_name: str = REQUEST_ID_HEADER) -> None:
        self.app = app
        self.header = header_name.lower().encode("latin-1")

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for key, value in scope.get("headers") or ():
            if key.lower() == self.header:
                incoming = value.decode("latin-1", "replace").strip() or None
                break

        with request_id_scope(incoming) as request_id:
            encoded = request_id.encode("latin-1", "replace")

            async def send_with_header(message: _Message) -> None:
                if message.get("type") == "http.response.start":
                    headers = list(message.get("headers") or ())
                    if not any(key.lower() == self.header for key, _v in headers):
                        headers.append((self.header, encoded))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, send_with_header)
