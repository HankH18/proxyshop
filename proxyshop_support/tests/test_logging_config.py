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
from collections.abc import MutableMapping
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
    # `type: ignore[attr-defined]` and NOT `getattr(record, "request_id")`: the attribute
    # genuinely is not on `logging.LogRecord`, which is the whole point — the filter puts it
    # there. Writing it through `getattr` would type-check by making the expression `Any`,
    # and would also make the assertion pass if the attribute were missing in a way this
    # test could not see. The direct access still raises `AttributeError` when the filter
    # stops stamping, which is the failure this line exists to catch.
    assert record.request_id == "deadbeef"  # type: ignore[attr-defined]

    outside = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    logging_config.RequestIdFilter().filter(outside)
    assert outside.request_id == logging_config.NO_REQUEST_ID  # type: ignore[attr-defined]


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


# `MutableMapping[str, Any]`, not `dict[str, Any]`: this is passed as the ASGI `send` of a
# typed `RequestIdMiddleware`, whose `_Send` is `Callable[[MutableMapping[str, Any]], ...]`.
# A parameter is contravariant, so a callable that only accepts `dict` is NOT a valid `send`
# and mypy rejects the call above — which is a real mismatch, not a typing nicety: a `send`
# annotated `dict` would refuse any ASGI server that hands over a Mapping subclass. The body
# is `return None` and annotations are strings here (`from __future__ import annotations`),
# so nothing this function does at runtime changed.
async def _null_send(message: MutableMapping[str, Any]) -> None:  # pragma: no cover - never called
    return None


# ======================================================================================
# Everything below was ADDED after an adversarial review of this file found the gaps it
# names. Nothing above was changed: the review's one worry about the existing middleware
# test — that it "pins verbatim adoption" — turned out not to bite, because the id it pins
# (b"from-the-caller") conforms to REQUEST_ID_PATTERN and is adopted exactly as before.
# ======================================================================================


# --------------------------------------------------------------------------------------
# the correlation id is attacker-controlled: two injections, one value
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("crlf-header-splitting", b"abc\r\nSet-Cookie: admin=1\r\nX-Injected: yes"),
        ("bare-lf-log-forging", b"real\n2026-01-01 CRITICAL security [-] FORGED LINE"),
        ("bare-cr", b"real\roverwritten"),
        ("nul", b"real\x00tail"),
        ("tab", b"real\ttail"),
        ("space", b"two words"),
        ("oversize", b"x" * 100_000),
        ("just-over-the-bound", b"y" * (logging_config.MAX_REQUEST_ID_LENGTH + 1)),
        ("empty", b""),
        ("whitespace-only", b"   "),
    ],
)
def test_a_hostile_incoming_request_id_is_discarded_not_adopted(label: str, raw: bytes) -> None:
    """A header value is chosen by the caller and lands in a response header AND a log line.

    Measured against the first draft of this module, which adopted it verbatim: the
    `crlf-header-splitting` case produced a response header value containing CR LF — that is
    response-header splitting, and uvicorn's httptools protocol does not validate header
    values — and the `bare-lf-log-forging` case produced a truncated record followed by a
    complete, attacker-authored CRITICAL line in the aggregator this module exists to feed.
    """
    seen: list[str] = []
    app = logging_config.RequestIdMiddleware(_echo_app(seen))
    sent = _drive(app, {"type": "http", "headers": [(b"x-request-id", raw)]})

    bound = seen[0]
    echoed = dict(sent[0]["headers"])[b"x-request-id"]

    assert bound != raw.decode("latin-1", "replace"), (
        f"[{label}] the caller's value was adopted verbatim as the correlation id"
    )
    assert logging_config.REQUEST_ID_PATTERN.fullmatch(bound), (
        f"[{label}] the bound id {bound!r} is not a safe correlation id"
    )
    assert echoed == bound.encode(), f"[{label}] header {echoed!r} != bound id {bound!r}"
    # The specific properties that make the two injections impossible, stated separately so
    # a regression names which one came back.
    for forbidden in (b"\r", b"\n", b"\x00", b"\t", b" "):
        assert forbidden not in echoed, f"[{label}] {forbidden!r} survived into the header"
    assert len(bound) <= logging_config.MAX_REQUEST_ID_LENGTH, f"[{label}] id is unbounded"


@pytest.mark.parametrize(
    "raw",
    [
        b"0123456789abcdef",
        b"3fa85f64-5717-4562-b3fc-2c963f66afa6",
        b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        b"svc:1234",
        b"user@host",
        b"a.b_c+d-e",
        b"x" * logging_config.MAX_REQUEST_ID_LENGTH,
    ],
)
def test_an_ordinary_incoming_request_id_is_still_adopted(raw: bytes) -> None:
    """The guard must not cost correlation for any id format anyone actually sends.

    Discarding is the right answer for a hostile value only if benign values survive — a
    pattern that rejected uuids or W3C traceparents would silently break tracing everywhere
    and would read, in the logs, exactly like the guard working.
    """
    seen: list[str] = []
    app = logging_config.RequestIdMiddleware(_echo_app(seen))
    _drive(app, {"type": "http", "headers": [(b"x-request-id", raw)]})
    assert seen == [raw.decode()], f"{raw!r} was rejected but is a legitimate correlation id"


def test_sanitise_request_id_is_reusable_and_answers_none_for_anything_unsafe() -> None:
    """Exposed because the HTTP header is not the only untrusted source of an id."""
    assert logging_config.sanitise_request_id(None) is None
    assert logging_config.sanitise_request_id("  padded  ") == "padded"
    assert logging_config.sanitise_request_id("a\r\nb") is None
    assert logging_config.sanitise_request_id("") is None
    assert logging_config.sanitise_request_id("x" * 129) is None


# --------------------------------------------------------------------------------------
# the keyword arguments. Every behavioural test above drives the ENVIRONMENT, so without
# these five a parameter could be a no-op and the file would stay green.
# --------------------------------------------------------------------------------------


def test_the_level_argument_beats_the_environment() -> None:
    info, warning = _sentinel(), _sentinel()
    completed = _run(
        f"""
        logging_config.configure_logging(level="WARNING")
        log = logging.getLogger("probe.arg.level")
        log.info("%s", {info!r})
        log.warning("%s", {warning!r})
        """,
        **{logging_config.LEVEL_ENV: "DEBUG"},
    )
    assert warning in completed.stderr
    assert info not in completed.stderr, "level= was ignored in favour of the environment"


def test_the_stream_argument_redirects_delivery() -> None:
    sentinel = _sentinel()
    completed = _run(
        f"""
        logging_config.configure_logging(stream=sys.stdout)
        logging.getLogger("probe.arg.stream").info("%s", {sentinel!r})
        """
    )
    assert sentinel in completed.stdout, "stream= was ignored"
    assert sentinel not in completed.stderr


def test_the_log_file_argument_beats_the_environment(tmp_path: Any) -> None:
    sentinel = _sentinel()
    chosen, ignored = tmp_path / "chosen.log", tmp_path / "ignored.log"
    _run(
        f"""
        logging_config.configure_logging(log_file={str(chosen)!r})
        logging.getLogger("probe.arg.file").info("%s", {sentinel!r})
        logging.shutdown()
        """,
        **{logging_config.FILE_ENV: str(ignored)},
    )
    assert chosen.is_file() and sentinel in chosen.read_text(encoding="utf-8")
    assert not ignored.exists(), "log_file= was ignored in favour of the environment"


def test_the_fmt_argument_beats_the_environment() -> None:
    sentinel = _sentinel()
    completed = _run(
        f"""
        logging_config.configure_logging(fmt="json")
        logging.getLogger("probe.arg.fmt").info("%s", {sentinel!r})
        """,
        **{logging_config.FORMAT_ENV: "text"},
    )
    line = next(line for line in completed.stderr.splitlines() if sentinel in line)
    assert json.loads(line)["message"] == sentinel, "fmt= was ignored"


def test_force_replaces_the_handlers_instead_of_returning_false() -> None:
    sentinel = _sentinel()
    completed = _run(
        f"""
        logging_config.configure_logging(level="WARNING")
        again = logging_config.configure_logging(level="DEBUG", force=True)
        print("AGAIN " + json.dumps([again, len(logging.getLogger().handlers)]))
        logging.getLogger("probe.force").info("%s", {sentinel!r})
        """
    )
    assert "AGAIN [true, 1]" in completed.stdout, (
        f"force=True did not reinstall exactly one handler:\n{completed.stdout}"
    )
    assert sentinel in completed.stderr, "force=True did not apply the new level"


def test_a_failed_force_leaves_the_working_handlers_in_place() -> None:
    """The reload path must not be able to turn a working process dark.

    Measured against an earlier draft, which removed AND CLOSED the installed handlers
    before building the replacements: `configure_logging(force=True, fmt="yaml")` raised and
    left `root.handlers == []` at level 30 — this module's own defect, walking back in
    through the one path meant to repair it.
    """
    before, after = _sentinel(), _sentinel()
    completed = _run(
        f"""
        logging_config.configure_logging()
        logging.getLogger("probe.reload").info("%s", {before!r})
        try:
            logging_config.configure_logging(fmt="yaml", force=True)
        except ValueError as exc:
            print("RAISED " + type(exc).__name__)
        logging.getLogger("probe.reload").info("%s", {after!r})
        print("HANDLERS " + json.dumps(len(logging.getLogger().handlers)))
        """
    )
    assert "RAISED ValueError" in completed.stdout
    assert before in completed.stderr
    assert "HANDLERS 1" in completed.stdout, (
        f"a failed reload tore down the working handler:\n{completed.stdout}"
    )
    assert after in completed.stderr, (
        "a failed reload left the process permanently dark — the record emitted after it "
        "reached nothing"
    )


def test_a_failed_force_on_an_unusable_log_file_also_keeps_the_handlers(tmp_path: Any) -> None:
    """The same property for the other way the swap can raise: an unopenable file sink."""
    before, after = _sentinel(), _sentinel()
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    completed = _run(
        f"""
        logging_config.configure_logging()
        logging.getLogger("probe.reload.file").info("%s", {before!r})
        try:
            logging_config.configure_logging(log_file={str(directory)!r}, force=True)
        except OSError as exc:
            print("RAISED " + type(exc).__name__)
        logging.getLogger("probe.reload.file").info("%s", {after!r})
        print("HANDLERS " + json.dumps(len(logging.getLogger().handlers)))
        """
    )
    assert "RAISED " in completed.stdout
    assert before in completed.stderr and after in completed.stderr
    assert "HANDLERS 1" in completed.stdout


# --------------------------------------------------------------------------------------
# formatter properties the module docstring claims
# --------------------------------------------------------------------------------------


def test_the_text_format_carries_the_correlation_id_too() -> None:
    """Only the JSON path pinned this, so `[%(request_id)s]` could have been deleted."""
    sentinel = _sentinel()
    completed = _run(
        f"""
        logging_config.configure_logging()
        with logging_config.request_id_scope("textid42"):
            logging.getLogger("probe.textid").info("%s", {sentinel!r})
        """
    )
    line = next(line for line in completed.stderr.splitlines() if sentinel in line)
    assert "textid42" in line, f"the text format dropped the correlation id: {line!r}"


def test_the_json_format_renders_a_traceback_into_the_same_object() -> None:
    """A traceback as trailing free text does not survive an aggregator that splits lines."""
    sentinel = _sentinel()
    completed = _run(
        f"""
        logging_config.configure_logging()
        try:
            raise RuntimeError({sentinel!r})
        except RuntimeError:
            logging.getLogger("probe.exc").exception("boom", stack_info=True)
        """,
        **{logging_config.FORMAT_ENV: "json"},
    )
    line = next(line for line in completed.stderr.splitlines() if sentinel in line)
    record = json.loads(line)
    assert record["message"] == "boom"
    assert sentinel in record["exc_info"], "exc_info was not rendered into the JSON object"
    assert record["stack_info"], "stack_info was not rendered into the JSON object"


def test_an_unknown_log_format_is_refused_rather_than_silently_text() -> None:
    """Same reasoning as the level: a misconfigured sink must not read as a working one."""
    completed = _run(
        """
        try:
            logging_config.configure_logging(fmt="yaml")
        except ValueError as exc:
            print("REFUSED " + str(exc))
        print("HANDLERS " + json.dumps(len(logging.getLogger().handlers)))
        """
    )
    assert "REFUSED " in completed.stdout and "is not a log format" in completed.stdout
    assert "HANDLERS 0" in completed.stdout, "a refused format still installed a handler"


def test_reset_logging_is_idempotent() -> None:
    completed = _run(
        """
        logging_config.configure_logging()
        print("RESETS " + json.dumps([logging_config.reset_logging(),
                                      logging_config.reset_logging()]))
        """
    )
    assert "RESETS [1, 0]" in completed.stdout


# ======================================================================================
# Added by a second adversarial verifier. Nothing above was modified except the three
# typing repairs recorded in that commit; everything here is NEW.
# ======================================================================================


def test_a_non_http_scope_is_not_given_a_correlation_id_at_all() -> None:
    """The half ``test_a_non_http_scope_passes_straight_through`` cannot see.

    That test asserts only that the downstream app was reached with the same scope, and
    MEASURED, it still passes with the ``scope.get("type") != "http"`` short-circuit deleted:
    a ``lifespan`` scope then falls into the HTTP path, finds no ``headers``, mints an id,
    wraps ``send`` — and still calls the app with the same scope, so ``calls == ["lifespan"]``
    holds either way. Sabotaging the branch to ``== "never"`` left all 48 tests green.

    The thing that actually differs is the binding: a non-HTTP scope must not be given a
    correlation id, because a ``lifespan`` scope lives for the whole process and a websocket
    scope for the whole connection, so an id minted there would be stamped on every record
    either of them ever emits and would correlate unrelated work under one id. This asserts
    that, and goes red for the same sabotage.
    """
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(logging_config.current_request_id())

    middleware = logging_config.RequestIdMiddleware(app)
    asyncio.run(middleware({"type": "lifespan"}, _null_receive, _null_send))

    assert seen == [logging_config.NO_REQUEST_ID], (
        f"a lifespan scope was given the correlation id {seen!r}. A non-HTTP scope outlives "
        f"any one request — a process lifespan, a whole websocket connection — so an id bound "
        f"there is stamped on unrelated records for as long as it lasts"
    )


def test_a_websocket_scope_is_passed_through_with_its_send_unwrapped() -> None:
    """A websocket scope DOES carry headers, so the header loop is not what saves it.

    ``lifespan`` has no ``headers`` key, which makes it a weak witness for the short-circuit:
    the HTTP path happens to survive a missing key via ``scope.get("headers") or ()``. A
    websocket scope carries a real ``x-request-id`` header, so if the branch were removed it
    would be adopted and the id bound — and the messages the app sends would go through the
    wrapping ``send``. Both are asserted here.
    """
    seen: list[str] = []
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(logging_config.current_request_id())
        await send({"type": "websocket.accept", "headers": []})

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(dict(message))

    middleware = logging_config.RequestIdMiddleware(app)
    asyncio.run(
        middleware(
            {"type": "websocket", "headers": [(b"x-request-id", b"from-the-caller")]},
            _null_receive,
            send,
        )
    )

    assert seen == [logging_config.NO_REQUEST_ID], (
        f"a websocket scope adopted the caller's correlation id ({seen!r}); one id would then "
        f"cover the whole connection instead of one request"
    )
    assert sent == [{"type": "websocket.accept", "headers": []}], (
        f"the websocket message was rewritten on its way out: {sent!r}. Only "
        f"`http.response.start` may be touched"
    )
