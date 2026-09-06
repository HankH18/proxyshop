"""The gate on :mod:`proxyshop_support.logging_config` (T-308).

**Every behavioural reading here is taken in a fresh interpreter, and that is not style.**
``logging`` is process-global, so the two obvious in-process idioms are both wrong:

* ``caplog`` installs its own handler *and* forces a level, i.e. it configures the very
  thing under test and would make these assertions pass against a module that does nothing;
* calling ``configure_logging()`` in this process would reconfigure logging for every test
  that runs after it in the same worker — the neighbour-corrupting behaviour T-247 punishes.

So the subprocess measures, and this process only ever inspects pure values (a fabricated
``LogRecord``, an ASGI call) that touch no global state. The subprocess idiom — PYTHONPATH
pointing at *this* checkout ahead of any ``.pth`` — is the one
``test_repro_observability.py`` uses, and for the same reason: this venv's
``site-packages/_proxyshop.pth`` names whichever checkout provisioned it, so a bare
``python -c`` in a second checkout imports the first one's modules.

What is deliberately NOT asserted here: that the *services* call ``configure_logging()``.
That is what ``test_repro_observability.py`` grades, against the real ASGI startup path of
every package derived from ``.pkgroot``. This file grades the mechanism; that file grades
whether the product uses it. A mechanism gate that also claimed the product was wired would
be the "five files nothing imports" failure that made an earlier draft of that gate green.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import subprocess
import sys
import textwrap
import uuid
from typing import Any

import pytest

from proxyshop_support import logging_config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _run(source: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    """Run ``source`` in a fresh interpreter that imports THIS checkout."""
    roots = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / ".pkgroot")])
    inherited = os.environ.get("PYTHONPATH")
    env = dict(
        os.environ,
        PYTHONPATH=f"{roots}{os.pathsep}{inherited}" if inherited else roots,
        **env_overrides,
    )
    # These are inherited from the caller's shell otherwise, and would silently steer the
    # readings below. Removed unless a test sets one explicitly.
    for name in (logging_config.LEVEL_ENV, logging_config.FILE_ENV, logging_config.FORMAT_ENV):
        if name not in env_overrides:
            env.pop(name, None)
    body = _PREAMBLE + textwrap.dedent(source)
    compile(body, "<logging-config-probe>", "exec")  # a typo must fail here, loudly
    completed = subprocess.run(
        [sys.executable, "-c", body],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"the probe exited {completed.returncode}\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    resolved = [line for line in completed.stdout.splitlines() if line.startswith("MODULE ")]
    assert resolved, f"the probe printed no MODULE line:\n{completed.stdout}"
    assert pathlib.Path(resolved[-1][len("MODULE ") :]).is_relative_to(REPO_ROOT), (
        f"the probe imported {resolved[-1]}, outside the tree under test ({REPO_ROOT}) — a "
        f".pth leak, not a measurement"
    )
    return completed


_PREAMBLE = """
import json, logging, pathlib, sys
from proxyshop_support import logging_config
print("MODULE " + str(pathlib.Path(logging_config.__file__).resolve()))
"""


def _sentinel() -> str:
    """Unguessable per run: a deterministic marker is enumerable, and a stray ``print`` of
    it in the module under test would read as delivery with nothing logged."""
    return f"t308-probe-{uuid.uuid4().hex}"


# --------------------------------------------------------------------------------------
# the defect this module closes, measured in a fresh process
# --------------------------------------------------------------------------------------


def test_a_fresh_interpreter_starts_with_no_handlers_and_drops_info() -> None:
    """The baseline, so the assertions below are known to be measuring a change.

    Without this, a Python that happened to configure logging for us would make every test
    in this file pass while ``configure_logging`` did nothing at all.
    """
    sentinel = _sentinel()
    completed = _run(
        f"""
        root = logging.getLogger()
        print("BOOT " + json.dumps([[type(h).__name__ for h in root.handlers],
                                    root.getEffectiveLevel()]))
        logging.getLogger("probe.unconfigured").info("%s", {sentinel!r})
        """
    )
    boot_line = next(line for line in completed.stdout.splitlines() if line.startswith("BOOT "))
    boot = json.loads(boot_line[len("BOOT ") :])
    assert boot == [[], logging.WARNING], (
        f"a fresh interpreter came up with {boot}, not [[], 30]; the 'INFO is dropped by "
        f"default' baseline this module exists to fix does not hold here"
    )
    assert sentinel not in completed.stdout + completed.stderr, (
        "an unconfigured interpreter delivered an INFO record, so the rest of this file "
        "cannot tell a working configure_logging() from a no-op"
    )


def test_configure_logging_delivers_an_info_record_to_stderr() -> None:
    """Constructed AND delivered — a handler that writes nowhere is not observability."""
    sentinel = _sentinel()
    completed = _run(
        f"""
        installed = logging_config.configure_logging()
        print("INSTALLED " + json.dumps(installed))
        logging.getLogger("probe.configured").info("%s", {sentinel!r})
        """
    )
    assert "INSTALLED true" in completed.stdout
    assert sentinel in completed.stderr, (
        f"configure_logging() returned True but the INFO record reached no stream.\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert sentinel not in completed.stdout, (
        "the record went to stdout, which programs pipe and parse; application logs belong "
        "on stderr"
    )
    line = next(line for line in completed.stderr.splitlines() if sentinel in line)
    assert "INFO" in line and "probe.configured" in line, (
        f"the delivered line names neither its level nor its logger, so an operator cannot "
        f"tell where it came from: {line!r}"
    )


def test_a_second_call_installs_nothing_and_does_not_double_the_line() -> None:
    """``create_app()`` and a lifespan hook may both call it; one line must come out."""
    sentinel = _sentinel()
    completed = _run(
        f"""
        first = logging_config.configure_logging()
        second = logging_config.configure_logging()
        print("CALLS " + json.dumps([first, second]))
        logging.getLogger("probe.twice").info("%s", {sentinel!r})
        print("HANDLERS " + json.dumps(len(logging.getLogger().handlers)))
        """
    )
    assert "CALLS [true, false]" in completed.stdout
    assert "HANDLERS 1" in completed.stdout
    assert completed.stderr.count(sentinel) == 1, (
        f"the record was delivered {completed.stderr.count(sentinel)} times, so a service "
        f"that configures logging from two startup hooks duplicates every line"
    )


def test_the_file_sink_receives_the_record_as_well_as_the_stream(tmp_path: Any) -> None:
    """``$PROXYSHOP_LOG_FILE`` adds a sink; it must not silently replace stderr."""
    sentinel = _sentinel()
    target = tmp_path / "proxyshop.log"
    completed = _run(
        f"""
        logging_config.configure_logging()
        logging.getLogger("probe.file").info("%s", {sentinel!r})
        logging.shutdown()
        """,
        **{logging_config.FILE_ENV: str(target)},
    )
    assert target.is_file(), f"{logging_config.FILE_ENV} was set but no file was written"
    assert sentinel in target.read_text(encoding="utf-8")
    assert sentinel in completed.stderr, (
        "the file sink swallowed the stream: `docker logs` would be empty for a service "
        "that happens to have a log file configured"
    )


def test_the_level_comes_from_the_environment_and_really_gates() -> None:
    """A level that is read but not applied is the defect wearing a fix's clothes."""
    info, warning = _sentinel(), _sentinel()
    completed = _run(
        f"""
        logging_config.configure_logging()
        log = logging.getLogger("probe.level")
        log.info("%s", {info!r})
        log.warning("%s", {warning!r})
        print("ROOT " + json.dumps(logging.getLogger().getEffectiveLevel()))
        """,
        **{logging_config.LEVEL_ENV: "WARNING"},
    )
    assert "ROOT 30" in completed.stdout
    assert warning in completed.stderr
    assert info not in completed.stderr, (
        f"{logging_config.LEVEL_ENV}=WARNING still delivered an INFO record, so the level "
        f"is decorative"
    )


def test_the_json_format_emits_one_parseable_object_per_record() -> None:
    """Structured output, for the aggregation the ticket records as absent."""
    sentinel = _sentinel()
    completed = _run(
        f"""
        logging_config.configure_logging()
        with logging_config.request_id_scope("abc123"):
            logging.getLogger("probe.json").info("%s", {sentinel!r})
        """,
        **{logging_config.FORMAT_ENV: "json"},
    )
    line = next(line for line in completed.stderr.splitlines() if sentinel in line)
    record = json.loads(line)
    assert record["level"] == "INFO"
    assert record["logger"] == "probe.json"
    assert record["message"] == sentinel
    assert record["request_id"] == "abc123", (
        f"the correlation id did not reach the record, so a request cannot be followed "
        f"across services: {record}"
    )
    assert record["ts"], "the record carries no timestamp"


def test_reset_logging_removes_only_the_handlers_this_module_installed() -> None:
    """A blanket ``root.handlers.clear()`` would delete pytest's and uvicorn's handlers."""
    completed = _run(
        """
        foreign = logging.StreamHandler(sys.stderr)
        logging.getLogger().addHandler(foreign)
        logging_config.configure_logging()
        removed = logging_config.reset_logging()
        remaining = logging.getLogger().handlers
        print("RESET " + json.dumps([removed, len(remaining), remaining[0] is foreign]))
        """
    )
    assert "RESET [1, 1, true]" in completed.stdout, (
        f"reset_logging() did not leave exactly the foreign handler in place:\n{completed.stdout}"
    )


def test_importing_the_module_configures_nothing() -> None:
    """T-247: importing a module must not reconfigure logging for whoever imported it."""
    completed = _run(
        """
        root = logging.getLogger()
        print("AFTER_IMPORT " + json.dumps([len(root.handlers), root.getEffectiveLevel()]))
        """
    )
    assert "AFTER_IMPORT [0, 30]" in completed.stdout, (
        f"importing proxyshop_support.logging_config changed this process's logging state, "
        f"so any test that imports it inherits configuration it did not ask for:\n"
        f"{completed.stdout}"
    )


# --------------------------------------------------------------------------------------
# pure values — safe in this process because they touch no global logging state
# --------------------------------------------------------------------------------------


def test_an_unparseable_level_is_refused_rather_than_defaulted() -> None:
    """``PROXYSHOP_LOG_LEVEL=verbose`` must not silently mean "log nothing"."""
    with pytest.raises(ValueError, match="is not a logging level"):
        logging_config.resolve_level("verbose")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("debug", logging.DEBUG), ("INFO", logging.INFO), ("30", 30), (5, 5)],
)
def test_resolve_level_accepts_names_and_numbers(value: str | int, expected: int) -> None:
    assert logging_config.resolve_level(value) == expected


def test_the_request_id_filter_stamps_every_record_it_sees() -> None:
    """Attached to the handler, so a formatter naming ``%(request_id)s`` can never raise —
    and a raising formatter drops the line silently, which is the defect returning."""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    assert not hasattr(record, "request_id")

    with logging_config.request_id_scope("deadbeef"):
        assert logging_config.RequestIdFilter().filter(record) is True
    assert record.request_id == "deadbeef"

    outside = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    logging_config.RequestIdFilter().filter(outside)
    assert outside.request_id == logging_config.NO_REQUEST_ID


def test_the_request_id_scope_restores_the_previous_binding() -> None:
    assert logging_config.current_request_id() == logging_config.NO_REQUEST_ID
    with logging_config.request_id_scope("outer"):
        with logging_config.request_id_scope("inner"):
            assert logging_config.current_request_id() == "inner"
        assert logging_config.current_request_id() == "outer"
    assert logging_config.current_request_id() == logging_config.NO_REQUEST_ID


# --------------------------------------------------------------------------------------
# the ASGI middleware, driven directly — no framework, no server, no socket
# --------------------------------------------------------------------------------------


def _drive(app: Any, scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Push one message through an ASGI app and collect what it sends."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _echo_app(seen: list[str]) -> Any:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(logging_config.current_request_id())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


def test_the_middleware_adopts_an_incoming_correlation_id_and_echoes_it() -> None:
    seen: list[str] = []
    app = logging_config.RequestIdMiddleware(_echo_app(seen))
    sent = _drive(app, {"type": "http", "headers": [(b"x-request-id", b"from-the-caller")]})

    assert seen == ["from-the-caller"], (
        "the downstream app did not see the caller's id, so its log lines cannot be joined "
        "to the caller's"
    )
    headers = dict(sent[0]["headers"])
    assert headers[b"x-request-id"] == b"from-the-caller"


def test_the_middleware_mints_an_id_when_the_caller_sends_none() -> None:
    seen: list[str] = []
    app = logging_config.RequestIdMiddleware(_echo_app(seen))
    sent = _drive(app, {"type": "http", "headers": []})

    assert seen and seen[0] != logging_config.NO_REQUEST_ID, (
        "no id was minted, so a request that arrives without one is untraceable"
    )
    assert dict(sent[0]["headers"])[b"x-request-id"] == seen[0].encode()


def test_the_middleware_leaves_the_binding_clean_after_the_request() -> None:
    """A leaked ContextVar would stamp one request's id onto the next one's lines."""
    app = logging_config.RequestIdMiddleware(_echo_app([]))
    _drive(app, {"type": "http", "headers": [(b"x-request-id", b"leaky")]})
    assert logging_config.current_request_id() == logging_config.NO_REQUEST_ID


def test_a_non_http_scope_passes_straight_through() -> None:
    """Lifespan and websocket scopes have no headers; touching them would break startup."""
    calls: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        calls.append(scope["type"])

    middleware = logging_config.RequestIdMiddleware(app)
    asyncio.run(middleware({"type": "lifespan"}, _null_receive, _null_send))
    assert calls == ["lifespan"]


async def _null_receive() -> dict[str, Any]:  # pragma: no cover - never awaited
    return {"type": "lifespan.startup"}


async def _null_send(message: dict[str, Any]) -> None:  # pragma: no cover - never called
    return None
