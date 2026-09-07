"""The devstack's half of the sign-in fix: the demo journey has to be completable.

The defect this guards is not in the auth service — that one refuses correctly. It is that
``npm run devstack`` produced a journey a person could not finish: ``Journey.tsx`` keeps the
confirm button OUT of the document until there is a session (``gateOnSignIn``), a session
comes only from redeeming a link, and ``apps/buyer/devstack/run.py`` set no magic-link
variables at all, so every sign-in on the demo stack was the deliberate ``503``.

Two properties, and the second is why this is a test rather than a comment:

* the launcher chooses the console transport and points it at the origin **it** serves on
  (8100), not at ``.env.example``'s compose origin (8081), which would mail a 404;
* an operator who chose something is left alone, including one who pointed the demo at a
  real MTA — a launcher that overwrote that would be deciding for them.

``run.py`` is executed by path rather than imported as a package (it puts the repo root and
``.pkgroot`` on ``sys.path`` itself), so it is loaded here the same way, from its real
location. No stack is booted: everything under test is pure environment reading.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

RUN_PY = Path(__file__).resolve().parents[3] / "buyer" / "devstack" / "run.py"

TRANSPORT_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT"
BASE_URL_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL"
SMTP_URL_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_URL"


@pytest.fixture(scope="module")
def devstack() -> ModuleType:
    """``apps/buyer/devstack/run.py``, loaded from its real path."""
    assert RUN_PY.is_file(), f"the devstack launcher is not at {RUN_PY}"
    spec = importlib.util.spec_from_file_location("proxyshop_devstack_run", RUN_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def no_transport() -> Any:
    """Clear the three variables, and put them back however the test left them.

    Not ``monkeypatch.delenv``: the thing under test WRITES to ``os.environ`` — that is its
    whole job — and monkeypatch records nothing for a variable that was absent when it was
    asked to delete it, so a launcher-set ``…_TRANSPORT=console`` would survive this module
    and silently give every later test in the session a console transport. Saved and restored
    by hand instead.
    """
    import os

    names = (TRANSPORT_ENV, BASE_URL_ENV, SMTP_URL_ENV)
    before = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_the_devstack_serves_the_page_on_the_port_its_links_point_at(
    devstack: ModuleType, no_transport: None
) -> None:
    """The link has to lead back to the page, and the port is where that goes wrong.

    ``.env.example`` states ``http://localhost:8081/``, which is right for
    ``apps/buyer/compose.yaml`` (``BUYER_SVC_PORT``) and wrong here: this launcher fixes the
    buyer app to 8100 so a person has a URL they can reload. A link built from the example's
    value 404s in the buyer's browser, which looks like a broken sign-in rather than a
    misconfiguration.
    """
    assert devstack.DEFAULT_BUYER_PORT == 8100
    chosen = devstack.configure_magic_link_delivery(
        f"http://{devstack.BUYER_HOST}:{devstack.DEFAULT_BUYER_PORT}/"
    )

    import os

    assert chosen == "console"
    assert os.environ[TRANSPORT_ENV] == "console"
    assert os.environ[BASE_URL_ENV] == "http://127.0.0.1:8100/", (
        "the demo's sign-in links do not point at the origin this stack serves the page on"
    )


def test_the_devstack_link_really_is_the_page_the_launcher_mounts(
    devstack: ModuleType, no_transport: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two halves are wired from one constant, so they cannot drift apart.

    Asserted through ``serve_on_port``'s own signature rather than by re-stating the host:
    the launcher builds the base URL before the server exists, and the only thing making that
    safe is that both read :data:`BUYER_HOST`.
    """
    import inspect

    host_default = inspect.signature(devstack.serve_on_port).parameters["host"].default
    assert host_default == devstack.BUYER_HOST

    devstack.configure_magic_link_delivery(f"http://{devstack.BUYER_HOST}:9999/")
    import os

    assert os.environ[BASE_URL_ENV] == f"http://{host_default}:9999/"


def test_an_operator_who_chose_a_transport_is_not_overruled(
    devstack: ModuleType, no_transport: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher decides what nobody decided, and nothing else.

    A demo stack pointed at a real MTA is a legitimate thing to run — that is how somebody
    checks the mail path works — and a launcher that silently redirected the links to stdout
    would be publishing credentials against an explicit instruction not to.
    """
    import os

    monkeypatch.setenv(SMTP_URL_ENV, "smtp://mail.example.net:2525")
    monkeypatch.setenv(BASE_URL_ENV, "https://buyer.example/")

    chosen = devstack.configure_magic_link_delivery("http://127.0.0.1:8100/")

    assert chosen == "smtp"
    assert TRANSPORT_ENV not in os.environ or not os.environ[TRANSPORT_ENV]
    assert os.environ[BASE_URL_ENV] == "https://buyer.example/", (
        "the launcher overwrote a base URL an operator stated"
    )


def test_the_banner_tells_a_reader_the_journey_has_a_sign_in_step(
    devstack: ModuleType, no_transport: None
) -> None:
    """A gate nobody was told about reads as a broken demo.

    The confirm button is absent rather than disabled, so there is nothing on the page to
    click and nothing to hover. The banner is where a reader finds out that a sign-in is
    coming, where the link will appear, and that it must be done before typing the
    conversation — redeeming reloads the page.
    """
    lines = "\n".join(devstack._sign_in_lines("http://127.0.0.1:8100", "console")).casefold()

    assert "sign in" in lines
    for fragment in ("terminal", "confirm", "reload"):
        assert fragment in lines, f"the banner never mentions {fragment!r}:\n{lines}"
    assert "8100" in lines, "the banner does not say where to open the page"
    assert "local-development" in lines or "development" in lines, (
        "the banner does not say that printing sign-in tokens is a development-only posture"
    )


def test_a_stack_pointed_at_an_mta_gets_a_banner_that_does_not_promise_a_terminal_link(
    devstack: ModuleType,
) -> None:
    """The banner describes what is actually configured, not what usually is."""
    lines: Any = "\n".join(devstack._sign_in_lines("http://127.0.0.1:8100", "smtp")).casefold()

    assert "mail" in lines
    assert "printed in this terminal" not in lines
