"""``GET /buyer/auth/sign-in`` — the boolean that decides whether a shopper is asked to log in.

The defect this file guards was visible on the hosted demo. ``apps/buyer/app/journey/SignIn.tsx``
says "No password. We email you a single-use link"; the demo ran
``PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT=console`` with every SMTP variable empty and there is no
MTA anywhere in this project, so the sentence was false. The owner asked for a link, no mail
arrived, and the whole product sat behind a login gesture nobody could complete. The SPA now
asks this route before it renders anything and mounts the form only where the answer is ``true``.

That makes this one boolean load-bearing in BOTH directions, which is why this file asserts
both and not just the interesting one:

* a ``false`` that should be ``true`` silently deletes the login from a deployment whose email
  works — the operator configured an MTA, the page never offers a form, and nothing anywhere
  reports a problem;
* a ``true`` that should be ``false`` puts the broken promise back.

What would be missed without this file
--------------------------------------
``test_auth_deployment_wiring.py`` proves what each transport DOES with a link once it is
minted. Nothing there drives the route that decides whether a link is ever asked for, so every
one of these would pass unnoticed: a predicate "simplified" to
``os.environ.get(TRANSPORT_ENV) == "smtp"`` (which hides the login from a deployment that mails
without naming the transport — ``.env.example`` documents ``smtp`` as "Same as leaving this
blank"); an unknown transport word turning a page load into a 500 or a 503 instead of one
gesture fewer; a half-configured transport offering a form that ``POST /buyer/auth/magic-link``
can only answer 503 to; the probe being charged against a buyer's five-per-fifteen-minutes login
budget; and the probe firing the console transport's boot banner — which prints live bearer
credentials — on every page load.

Everything here is driven over real HTTP through ``buyer_svc.main.create_app()``. The point is
the served route, not the predicate underneath it: the predicate answering correctly while the
route 500s is the same outage to a buyer.

Nothing in this file needs an MTA to be listening. ``smtp://127.0.0.1:8025`` names a host and a
port and this route never opens a connection to them, which is exactly the property that makes
the probe cheap enough for the page to ask on load.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

BUYER = "dana.reyes@example.com"

SIGN_IN_PATH = "/buyer/auth/sign-in"
MAGIC_LINK_PATH = "/buyer/auth/magic-link"
SESSION_PATH = "/buyer/auth/session"

TRANSPORT_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT"
SMTP_URL_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_URL"
SENDER_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SENDER"
BASE_URL_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL"
STARTTLS_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_STARTTLS"
USERNAME_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_USERNAME"
PASSWORD_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_PASSWORD"
CA_BUNDLE_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_CA_BUNDLE"

#: Every variable that can change this route's answer. Cleared for every test: a shell that
#: exported one of these — or a devstack left running in the same session, which sets
#: ``…_TRANSPORT=console`` on purpose — would otherwise decide what "unconfigured" means here.
MAGIC_LINK_ENVS = (
    TRANSPORT_ENV,
    SMTP_URL_ENV,
    SENDER_ENV,
    BASE_URL_ENV,
    STARTTLS_ENV,
    USERNAME_ENV,
    PASSWORD_ENV,
    CA_BUNDLE_ENV,
)

#: The MTA a mailing deployment names here. Nothing listens on it and nothing needs to: this
#: route answers from the configuration and never opens a session, which is what makes it safe
#: for the SPA to ask on every page load. ``STARTTLS_ENV`` is set to ``disabled`` alongside it
#: because 127.0.0.1 is a local sink; the transport is loud about that choice on any other host.
SINK_URL = "smtp://127.0.0.1:8025"
SENDER = "Proxyshop <no-reply@proxyshop.example>"

#: The origin a link points at — the SPA's own root, where it reads ``?token=`` off the address
#: bar. Not this service's port: the link is a page the buyer opens.
PAGE_ORIGIN = "http://127.0.0.1:8100/"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing the runner's shell exports decides what any test here is configured with."""
    import os

    from buyer_svc.auth.routes import WORKER_COUNT_ENVS

    for name in list(os.environ):
        if name.startswith("PROXYSHOP_PG_DSN") or name in WORKER_COUNT_ENVS:
            monkeypatch.delenv(name, raising=False)
    for name in (*MAGIC_LINK_ENVS, "PROXYSHOP_BUYER_K_ANONYMITY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fresh_process_state():
    """Reset the two process-wide singletons around each test, whatever it does to them.

    ``auth_service()`` caches the built login stack for the life of the process, and this route
    calls it. A stack leaked in from an earlier test would answer from THAT test's environment —
    a mailing deployment would keep answering ``offered: true`` after its variables were gone —
    so every assertion here would be about nothing.
    """
    from buyer_svc.auth import routes as routes_mod

    previous_service = routes_mod._service
    previous_accounts = routes_mod._accounts
    routes_mod.set_auth_service(None)
    routes_mod.set_account_directory(None)
    try:
        yield routes_mod
    finally:
        routes_mod.set_auth_service(previous_service)
        routes_mod.set_account_directory(previous_accounts)


class _SMTPSession:
    """One connection to the stand-in MTA. Records instead of sending; touches no socket."""

    def __init__(self, mailbox: _Mailbox) -> None:
        self._mailbox = mailbox

    def __enter__(self) -> _SMTPSession:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def starttls(self, *_a: Any, **_k: Any) -> None:
        return None

    def login(self, *_a: Any, **_k: Any) -> None:
        return None

    def send_message(self, message: Any) -> None:
        self._mailbox.sent.append(message)

    def quit(self) -> None:
        return None


class _Mailbox:
    """A stand-in MTA, so a mailing deployment's login door can be exercised without one."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def transport(self, _host: str, _port: int, *_a: Any, **_k: Any) -> _SMTPSession:
        return _SMTPSession(self)


def _served_app() -> tuple[Any, Any]:
    """``(app, client)`` for the real application, with server errors answered rather than raised.

    ``raise_server_exceptions=False`` on purpose: several tests below assert that a deployment
    gets a *200 saying no* rather than a 500, and a client that re-raises would report those as
    an error in the test rather than as the page failing to load, which is what a buyer sees.
    """
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    return app, TestClient(app, raise_server_exceptions=False)


def _refused_the_form(answer: Any, deployment: str) -> None:
    """Assert the page was told not to offer a login, and told it in a way it can act on."""
    assert answer.status_code == 200, (
        f"{deployment} answered {answer.status_code} rather than 200. The SPA asks this route "
        f"before it renders anything, so this is not one gesture fewer — it is a journey that "
        f"does not load: {answer.text}"
    )
    assert answer.json() == {"offered": False}, (
        f"{deployment} offers the sign-in form, and that form promises 'we email you a "
        f"single-use link'. The buyer will ask for a link, no mail will be sent, and the "
        f"product stays behind a gate nobody can pass: {answer.json()}"
    )


def _offered_the_form(answer: Any, deployment: str) -> None:
    """Assert the page was told to offer a login on a deployment whose email really works."""
    assert answer.status_code == 200, (
        f"{deployment} answered {answer.status_code} rather than 200: {answer.text}"
    )
    assert answer.json() == {"offered": True}, (
        f"{deployment} really does mail sign-in links, and its page was told not to offer the "
        f"form. The login has been deleted from a working deployment and nothing reports it: "
        f"{answer.json()}"
    )


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every ERROR-or-worse line this service logged, as text."""
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


# =======================================================================================
# offered: false — every deployment that cannot put a link in a mailbox
# =======================================================================================


def test_a_deployment_with_no_magic_link_variables_at_all_does_not_offer_a_sign_in_form(
    clean_env: None, fresh_process_state: Any
) -> None:
    """The fail-closed default. Nothing is configured, so nothing is promised.

    This is the state every deployment of this service was in before a transport existed, and
    it is the one an operator reaches by doing nothing at all. ``POST /buyer/auth/magic-link``
    answers 503 here, so a form would be a button whose only outcome is an error.

    The body is asserted whole rather than by key: the answer is one boolean and nothing else,
    because a field naming the transport would ship a piece of this deployment's configuration
    to every browser that loads the page.
    """
    _, client = _served_app()

    answered = client.get(SIGN_IN_PATH)

    _refused_the_form(answered, "a deployment with no magic-link configuration at all")


def test_the_console_transport_does_not_offer_a_form_that_promises_an_email(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hosted demo, verbatim: console transport, no SMTP anywhere, a page promising mail.

    The console transport does complete a login — it prints the link to this process's stdout,
    and that is deliberate for a developer running the devstack. What it does not do is put
    anything in the mailbox the buyer typed, and the form is a promise about the mailbox. So
    the capability the page asks about is "would this reach a mail transfer agent", not "can a
    session be minted somehow".
    """
    monkeypatch.setenv(TRANSPORT_ENV, "console")
    monkeypatch.setenv(BASE_URL_ENV, PAGE_ORIGIN)
    _, client = _served_app()

    answered = client.get(SIGN_IN_PATH)

    _refused_the_form(answered, "a console-transport deployment (the hosted demo's own wiring)")


def test_a_transport_word_this_service_does_not_know_leaves_the_page_loadable(
    clean_env: None,
    fresh_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo in one variable must cost the operator a login form, never the whole journey.

    ``build_magic_link_delivery`` refuses an unknown transport word at construction, and it is
    right to: ``console`` prints live bearer credentials, so a near miss must never be read as
    a request for it. But this route is asked on every page load, so inheriting that refusal
    would turn one misspelling into a page that renders nothing — worse for the buyer than a
    page with one gesture fewer, and much harder for the operator to diagnose.

    The log is asserted too, and it is the half that says WHERE the answer came from: the
    capability question alone, without asking the login stack to build. A route that built it
    to find out would log one ERROR per page load, burying the deployment's real problem in
    its own noise.
    """
    _, client = _served_app()

    for stated in ("Console", "consol", "stdout"):
        monkeypatch.setenv(TRANSPORT_ENV, stated)
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="buyer_svc.auth.routes"):
            answered = client.get(SIGN_IN_PATH)

        _refused_the_form(answered, f"a deployment whose transport reads {stated!r}")
        assert not _errors(caplog), (
            f"{stated!r} is not a transport this service has, and answering that took a trip "
            f"through the login stack and logged an error. Every page load would do it: "
            f"{_errors(caplog)}"
        )


def test_a_deployment_that_stated_smtp_and_named_no_mta_does_not_offer_the_form(
    clean_env: None,
    fresh_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An intention is not a transport. ``smtp`` with nothing behind it mails nothing.

    This deployment told the service what it meant to do and did not finish, which is a boot
    failure at the login door — ``POST /buyer/auth/magic-link`` cannot answer anything but 503.
    Offering the form would hand the buyer the one gesture guaranteed to fail.

    Answered, like the misspelling above, from the transport configuration rather than by
    building the login stack and catching the failure: same 200, same silence in the log.
    """
    monkeypatch.setenv(TRANSPORT_ENV, "smtp")
    _, client = _served_app()

    with caplog.at_level(logging.ERROR, logger="buyer_svc.auth.routes"):
        answered = client.get(SIGN_IN_PATH)

    _refused_the_form(answered, "a deployment that stated smtp and named no MTA")
    assert not _errors(caplog), (
        f"answering a page load for a deployment that named no MTA went through the login "
        f"stack and logged an error; every load would repeat it: {_errors(caplog)}"
    )


def test_a_half_configured_mail_transport_does_not_offer_a_form_the_login_door_would_refuse(
    clean_env: None,
    fresh_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The subtle one: an MTA is named, so the deployment LOOKS like it mails, and it does not.

    ``magic_link_is_mailed()`` answers ``True`` here — it can see that an MTA was named and
    cannot see that the sender is missing — so a route that trusted it alone would offer the
    form to a deployment whose ``POST /buyer/auth/magic-link`` is a 503. That 503 is asserted
    below in the same test, because it is the whole reason the answer has to be ``false``: the
    page must not offer a gesture that cannot succeed.

    The error in the log is the other half of the contract. The page going quiet must not be
    the ONLY symptom of a half-finished transport, or an operator who mistyped one variable
    has no way to learn that their sign-in disappeared because of it.
    """
    monkeypatch.setenv(SMTP_URL_ENV, SINK_URL)
    monkeypatch.setenv(STARTTLS_ENV, "disabled")
    monkeypatch.setenv(BASE_URL_ENV, PAGE_ORIGIN)
    # SENDER_ENV deliberately unset: the operator named an MTA and stopped.
    _, client = _served_app()

    with caplog.at_level(logging.ERROR, logger="buyer_svc.auth.routes"):
        answered = client.get(SIGN_IN_PATH)

    _refused_the_form(answered, "a deployment with an MTA named and no envelope sender")
    logged = "\n".join(_errors(caplog))
    assert SENDER_ENV in logged, (
        f"the sign-in form vanished from this deployment and the log never named the variable "
        f"that did it, so the only symptom an operator gets is a page with no login on it:\n"
        f"{logged}"
    )

    # The reason the answer above has to be `false`, stated as the behaviour of the door the
    # form would have knocked on.
    refused = client.post(MAGIC_LINK_PATH, json={"email": BUYER})
    assert refused.status_code == 503, (
        f"the login door answered {refused.status_code} for a half-configured transport, so "
        f"this test no longer demonstrates why the form must not be offered: {refused.text}"
    )


# =======================================================================================
# offered: true — every deployment that really does put a link in a mailbox
# =======================================================================================


def test_a_deployment_that_names_its_transport_and_an_mta_offers_the_sign_in_form(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployment the login was built for: it mails, so the page asks for an address.

    The direction that fails silently. A ``false`` here deletes the entire login from a
    deployment whose email works — no error, no log line, just a journey that never asks who
    the buyer is — and the operator's own configuration says the opposite.
    """
    monkeypatch.setenv(TRANSPORT_ENV, "smtp")
    monkeypatch.setenv(SMTP_URL_ENV, SINK_URL)
    monkeypatch.setenv(STARTTLS_ENV, "disabled")
    monkeypatch.setenv(SENDER_ENV, SENDER)
    monkeypatch.setenv(BASE_URL_ENV, PAGE_ORIGIN)
    _, client = _served_app()

    answered = client.get(SIGN_IN_PATH)

    _offered_the_form(answered, "a deployment with smtp stated and an MTA, sender and origin set")


def test_a_deployment_that_mails_without_naming_the_transport_still_offers_the_form(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion most likely to be broken by "simplifying" the predicate later.

    ``.env.example`` says of ``smtp``: "Same as leaving this blank". So an operator who set an
    MTA, a sender and an origin and never touched the transport variable has a deployment that
    genuinely mails — it is the shape the service had before the variable existed — and reading
    the literal word ``smtp`` instead of the capability would hide the login from exactly that
    operator, whose email works and whose configuration nobody would think to question.

    This is why the fact is derived from what the transport can DO rather than from what it is
    called, and it is one string comparison away from being wrong again.
    """
    monkeypatch.delenv(TRANSPORT_ENV, raising=False)
    monkeypatch.setenv(SMTP_URL_ENV, SINK_URL)
    monkeypatch.setenv(STARTTLS_ENV, "disabled")
    monkeypatch.setenv(SENDER_ENV, SENDER)
    monkeypatch.setenv(BASE_URL_ENV, PAGE_ORIGIN)
    _, client = _served_app()

    answered = client.get(SIGN_IN_PATH)

    _offered_the_form(answered, "a deployment that mails without naming a transport")


# =======================================================================================
# What hiding the form must NOT cost: the routes, the budget, or the terminal
# =======================================================================================


def test_a_console_deployments_login_still_works_although_its_page_offers_no_form(
    clean_env: None,
    fresh_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Hidden from the page is not deleted from the service, and that distinction is the change.

    A developer running ``apps/buyer/devstack/run.py`` reads the link off their own terminal
    and signs in; the demo journey exists because of that. Nothing about answering ``false``
    to the SPA may take it away, so the whole gesture is driven here on the same deployment
    that answers ``false`` — 202 at the door, the token scraped out of what the process really
    printed, and a session opened with it.

    Without this, "clean up the console transport, the page does not offer it anyway" is a
    plausible-sounding change that breaks every local demo and no test.
    """
    monkeypatch.setenv(TRANSPORT_ENV, "console")
    monkeypatch.setenv(BASE_URL_ENV, PAGE_ORIGIN)
    _, client = _served_app()

    hidden = client.get(SIGN_IN_PATH)
    _refused_the_form(hidden, "a console-transport deployment")

    capsys.readouterr()  # drop anything printed so far; the next read is this login alone
    requested = client.post(MAGIC_LINK_PATH, json={"email": BUYER})
    assert requested.status_code == 202, (
        f"the console deployment's login door answered {requested.status_code}: the page no "
        f"longer offers the form AND the gesture behind it is gone, which is the demo "
        f"dead-ending again: {requested.text}"
    )

    printed = capsys.readouterr().out
    links = [
        word.rstrip(".,")
        for word in printed.split()
        if word.startswith(PAGE_ORIGIN) and "token=" in word
    ]
    assert len(links) == 1, f"expected one printed sign-in link, got {links!r}:\n{printed}"
    found = parse_qs(urlsplit(links[0]).query).get("token")
    assert found, f"the printed link carries no token: {links[0]}"

    session = client.post(SESSION_PATH, json={"token": found[0]})
    assert session.status_code == 201, (
        f"the link this deployment printed did not open a session ({session.status_code}), so "
        f"a developer following the devstack's own instructions cannot log in: {session.text}"
    )
    assert session.json()["pseudonym"].startswith("psn-")


def test_asking_whether_sign_in_is_offered_spends_none_of_any_buyers_login_budget(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page asks this on every load, so it has to be free. A charged probe is a lockout.

    ``POST /buyer/auth/magic-link`` allows five links per address per fifteen minutes. If this
    probe were charged to anybody — the caller, a shared key, an empty subject — then reloading
    the page four times would refuse a buyer their own login, and refresh a few more times and
    it refuses everyone's.

    Twenty probes, well past the budget, and then the FULL budget is spent for a fresh address
    with every request accepted. The sixth request is asserted to be refused, because "five
    succeeded" proves nothing about the probe unless the limiter is armed and counting.
    """
    import smtplib

    from buyer_svc.auth.routes import DEFAULT_MAGIC_LINK_RATE_LIMIT

    mailbox = _Mailbox()
    monkeypatch.setattr(smtplib, "SMTP", mailbox.transport)
    monkeypatch.setenv(TRANSPORT_ENV, "smtp")
    monkeypatch.setenv(SMTP_URL_ENV, SINK_URL)
    monkeypatch.setenv(STARTTLS_ENV, "disabled")
    monkeypatch.setenv(SENDER_ENV, SENDER)
    monkeypatch.setenv(BASE_URL_ENV, PAGE_ORIGIN)
    app, client = _served_app()

    for probe in range(20):
        answered = client.get(SIGN_IN_PATH)
        _offered_the_form(answered, f"a mailing deployment, on page load {probe + 1}")

    # The limiter belongs to the application, and it is built on the first request that needs
    # one — so after twenty probes it should either not exist yet or be tracking nobody.
    limiter = getattr(app.state, "magic_link_rate_limiter", None)
    assert limiter is None or limiter.tracked == 0, (
        f"twenty page loads left {limiter.tracked} address(es) holding a login-link admission; "
        f"the probe is being charged to somebody, and reloading the page spends a stranger's "
        f"budget"
    )

    fresh = "priya.nair@example.com"
    for spent in range(DEFAULT_MAGIC_LINK_RATE_LIMIT):
        requested = client.post(MAGIC_LINK_PATH, json={"email": fresh})
        assert requested.status_code == 202, (
            f"link {spent + 1} of this address's untouched budget of "
            f"{DEFAULT_MAGIC_LINK_RATE_LIMIT} was refused with {requested.status_code}: the "
            f"page's own probe spent part of it before the buyer typed anything: "
            f"{requested.text}"
        )
    assert len(mailbox.sent) == DEFAULT_MAGIC_LINK_RATE_LIMIT, (
        f"{len(mailbox.sent)} messages reached the MTA for "
        f"{DEFAULT_MAGIC_LINK_RATE_LIMIT} accepted requests"
    )

    over = client.post(MAGIC_LINK_PATH, json={"email": fresh})
    assert over.status_code == 429, (
        f"the budget past its limit answered {over.status_code}; the limiter is not counting "
        f"at all here, so the probes above proved nothing about what the probe costs"
    )


def test_asking_whether_sign_in_is_offered_prints_no_token_and_no_credential_banner(
    clean_env: None,
    fresh_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A page load must not make this process shout its console-transport warning, or anything.

    The console transport announces itself on stdout the moment it is built, in a block that
    exists to tell an operator that live sign-in tokens are about to be printed. The route
    answers ``false`` for a console deployment before it touches the transport, so the banner
    stays where it belongs — the first actual login — instead of being repeated on every page
    load until it is scrolled past, ignored, and worth nothing when it matters.

    Asserted on stdout because that is where the transport writes: this service's logging goes
    to stderr precisely so stdout stays clean.
    """
    monkeypatch.setenv(TRANSPORT_ENV, "console")
    monkeypatch.setenv(BASE_URL_ENV, PAGE_ORIGIN)
    _, client = _served_app()
    capsys.readouterr()  # the application was built; from here on stdout is this route's alone

    for _ in range(3):
        _refused_the_form(client.get(SIGN_IN_PATH), "a console-transport deployment")

    printed = capsys.readouterr()
    stream = printed.out + printed.err
    assert "token=" not in stream, (
        f"asking whether sign-in is offered put something link-shaped on this process's "
        f"output; the probe is unauthenticated and every page load would repeat it:\n{stream}"
    )
    for shouted in ("MAGIC-LINK TRANSPORT", "MAGIC LINK", "credential"):
        assert shouted not in printed.out, (
            f"a page load fired the console transport's boot announcement ({shouted!r}). The "
            f"one warning that says this process publishes bearer credentials is now printed "
            f"on every load, which is how it stops being read:\n{printed.out}"
        )
    assert not printed.out.strip(), (
        f"asking whether sign-in is offered printed to stdout at all; this route is meant to "
        f"return before it reaches any transport:\n{printed.out}"
    )
