"""Where a magic link actually goes (T-361, SPEC R5).

R5's first clause is "buyers authenticate with email magic link", and until this module
existed the second half of that sentence had no implementation anywhere in the tree:
:func:`~buyer_svc.auth.routes.build_auth_service` decided the vault, the account directory
and the profile publisher, and never decided the transport, so
``build_auth_service().deliver is magic_link._drop`` was True on every production path. The
route answered ``202 Accepted``, :func:`~buyer_svc.auth.magic_link._drop` threw the token
away in-process, and the buyer waited for a mail nothing had been asked to send. Nothing
logged, nothing failed, and the login gesture could not be completed in any deployment.

Three states, and the second is the reason this module is not just an SMTP client:

* **A configured deployment mails the link.** :func:`build_magic_link_delivery` returns a
  callable of the exact shape ``MagicLinkAuth.deliver`` expects, built from the environment.
* **An unconfigured one refuses, loudly.** With no transport variables set the callable
  raises :class:`MagicLinkUndeliverable` rather than returning, so
  ``POST /buyer/auth/magic-link`` answers ``503`` instead of ``202``. Fail closed: a service
  that cannot deliver a login must not accept one. A HALF-configured deployment — an MTA
  named and no sender, a base URL that is not a URL — never reaches that state at all; it
  raises :class:`MagicLinkTransportMisconfigured` from the builder and the process does not
  start, in the same shape as
  :class:`~buyer_svc.auth.routes.ProcessLocalStateUnsafe`. An operator who configured half a
  transport meant to send mail, and silently demoting them to "no transport" would put them
  back in exactly the state this ticket is about while their configuration looked right.
* **A deployment that DEMANDS the console gets the console** —
  :data:`MAGIC_LINK_TRANSPORT_ENV` set to exactly ``console``, and nothing else reaches it.

The third state, and why it is not a hole in the second
-------------------------------------------------------
:func:`~buyer_svc.auth.magic_link._drop`'s docstring states the rule this module keeps: "a
service booted without a mail transport should fail to log buyers in, not publish bearer
credentials to stdout". Note the *without*. The rule is about a service that is MISSING
configuration — it must never quietly downgrade to something weaker, because nobody chose
the weaker thing and nobody will notice it happened. It is not a rule against an operator
who names the weaker thing on purpose.

So the console transport is reachable only through a variable whose sole job is to be that
choice: :data:`MAGIC_LINK_TRANSPORT_ENV`. Unset, empty, misspelled, or any value other than
the two literals in :data:`MAGIC_LINK_TRANSPORTS` is a refusal — an unset one falls through
to the SMTP reading above (and, with no MTA either, to the ``503``), and a set-but-unknown
one is :class:`MagicLinkTransportMisconfigured` at boot. There is no spelling of "nothing
configured" that produces a console transport, no default that decays into one, and no
value that is *close enough* to ``console``; that is the whole design of
:func:`_selected_transport`.

Why this is safe on a laptop and unsafe everywhere else
--------------------------------------------------------
``console`` prints a **live, single-use bearer credential** — the token that opens a session
as whichever address was typed — onto this process's stdout. On a developer's machine
running ``apps/buyer/devstack/run.py`` that stdout is one terminal on one workstation, the
mailbox is the developer's own, and the alternative is the journey dead-ending at a sign-in
step no local deployment can complete. Anywhere else the same line is a credential in a log
aggregator, a CI artifact, a container-runtime journal, or a screen-share — read by people
who never authenticated as anybody, and readable long after the token would have expired
from a mailbox. Hence: never set :data:`MAGIC_LINK_TRANSPORT_ENV` to ``console`` on a
deployment that serves anyone but you, and hence the transport shouts what it is doing at
boot (:func:`_announce_console`) rather than blending into the log.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_log = logging.getLogger(__name__)

__all__ = [
    "CONSOLE_TRANSPORT",
    "MAGIC_LINK_BASE_URL_ENV",
    "MAGIC_LINK_SENDER_ENV",
    "MAGIC_LINK_SMTP_TIMEOUT_SECONDS",
    "MAGIC_LINK_SMTP_URL_ENV",
    "MAGIC_LINK_SUBJECT",
    "MAGIC_LINK_TRANSPORTS",
    "MAGIC_LINK_TRANSPORT_ENV",
    "SMTP_TRANSPORT",
    "MagicLinkTransportMisconfigured",
    "MagicLinkUndeliverable",
    "build_magic_link_delivery",
]

#: Which transport this deployment CHOSE, spelled out. The only variable that can reach the
#: console transport, and the only one whose meaning is "I have decided", so an operator can
#: never arrive at stdout delivery by leaving something out. Unset means "no choice stated",
#: which is read as the SMTP question below and NOT as a licence to invent an answer.
MAGIC_LINK_TRANSPORT_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT"

#: Hand the link to an MTA. The same behaviour an unset :data:`MAGIC_LINK_TRANSPORT_ENV`
#: gets, said out loud — with one difference that is the point of saying it: a deployment
#: that states ``smtp`` and configures no MTA is a boot failure rather than a silent ``503``,
#: because it has told this service what it meant to do.
SMTP_TRANSPORT = "smtp"

#: Print the link to this process's stdout. A LOCAL DEVELOPMENT transport that publishes a
#: bearer credential; read the module docstring's last section before setting it.
CONSOLE_TRANSPORT = "console"

#: Every value :data:`MAGIC_LINK_TRANSPORT_ENV` accepts. Matched exactly, after stripping
#: surrounding whitespace and with no case folding, no aliases and no prefixes: the point of
#: the variable is that it cannot be hit by accident, and every spelling this tuple does not
#: contain — ``Console``, ``consol``, ``console!``, ``stdout`` — is a boot failure that names
#: what it would have accepted.
MAGIC_LINK_TRANSPORTS = (SMTP_TRANSPORT, CONSOLE_TRANSPORT)

#: The MTA to hand the message to, as a URL: ``smtp://host:port`` for a plain or
#: STARTTLS-upgraded session, ``smtps://host:port`` for implicit TLS. Credentials may ride in
#: the URL (``smtp://user:password@host``), in which case the session authenticates. Unset
#: means "this deployment has no mail transport", which is a refusal and not a default.
MAGIC_LINK_SMTP_URL_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_URL"

#: The envelope sender. Required whenever :data:`MAGIC_LINK_SMTP_URL_ENV` is set — an MTA that
#: accepts mail from this service still needs to be told who it is from, and guessing one
#: produces mail that is silently dropped as spam, which is this ticket's failure again.
MAGIC_LINK_SENDER_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SENDER"

#: The front door the mailed link points at. The token is appended as a ``token`` query
#: parameter, so ``https://buyer.example/auth/callback`` becomes
#: ``https://buyer.example/auth/callback?token=...``. Required alongside the MTA: a link with
#: no host is not a link, and a token nobody can click is the same outage in a nicer envelope.
MAGIC_LINK_BASE_URL_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL"

#: Seconds any single SMTP conversation may take before it is abandoned.
#:
#: Not a taste choice: ``POST /buyer/auth/magic-link`` is a ``def`` endpoint, so FastAPI runs
#: it in the threadpool, and delivery happens inside the request. ``smtplib`` with no timeout
#: inherits the OS default, which on Linux means a connect to a black-holed MTA parks the
#: worker for over two minutes; the pool is small and an unauthenticated caller chooses how
#: many requests are in it, so an MTA that stops answering turns into a whole-service outage
#: — routes with nothing to do with login included. Ten seconds is well past a healthy MTA's
#: round trip (a same-datacentre submission is single-digit milliseconds) and far short of
#: the pool's patience.
MAGIC_LINK_SMTP_TIMEOUT_SECONDS = 10.0

#: The subject line. Fixed text: it carries no address, no token and nothing about the buyer,
#: because a subject line is the part of a mail most likely to be quoted into a notification,
#: a preview pane or a support ticket.
MAGIC_LINK_SUBJECT = "Your Proxyshop sign-in link"

_TLS_SCHEMES = {"smtps"}
_PLAIN_SCHEMES = {"smtp"}
_DEFAULT_SMTP_PORTS = {"smtp": 587, "smtps": 465}


class MagicLinkTransportMisconfigured(RuntimeError):
    """The deployment asked for a mail transport and did not finish describing one.

    Raised by :func:`build_magic_link_delivery`, so it surfaces at construction and stops the
    process rather than at the first login attempt. Names the variable that is wrong.
    """


class MagicLinkUndeliverable(RuntimeError):
    """No mail transport is configured, so no login link can be sent (T-361).

    Raised by the callable :func:`build_magic_link_delivery` returns when nothing is
    configured, which reaches ``request_login`` from inside ``deliver`` and reaches the route
    as a ``503``. It is deliberately a distinct type from every transport error: this one
    means *nothing was attempted*, whereas an ``smtplib`` failure may well have handed the
    message over before it raised. Only the first is safe to treat as "no mail was sent".
    """


def _require(name: str, value: str | None, why: str, *, chosen: str) -> str:
    """``value`` stripped, or a boot failure naming the variable and what chose it.

    ``chosen`` is the half of the message an operator needs first — WHY this service thinks
    the variable was required. A message that names only the missing variable is read as a
    new requirement rather than as the consequence of a setting three lines further up.
    """
    if not value or not value.strip():
        raise MagicLinkTransportMisconfigured(f"{chosen}, but {name} is unset: {why}")
    return value.strip()


def _magic_link_url(base_url: str, token: str) -> str:
    """``base_url`` with ``token`` added as a query parameter, keeping any query it had."""
    parts = urlsplit(base_url)
    query = parse_qsl(parts.query, keep_blank_values=True) + [("token", token)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _message(sender: str, email: str, link: str, expires_at: datetime) -> Any:
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = MAGIC_LINK_SUBJECT
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "Someone asked to sign in to Proxyshop with this email address.\n\n"
        f"{link}\n\n"
        f"The link works once and stops working at {expires_at.isoformat()}.\n"
        "If this was not you, nothing has happened to your account and you can ignore this "
        "message.\n"
    )
    return message


def _smtp_delivery(url: str, sender: str, base_url: str) -> Callable[[str, str, datetime], None]:
    """A ``deliver`` that hands the link to the configured MTA. See the module docstring."""
    parts = urlsplit(url)
    scheme = parts.scheme.casefold()
    if scheme not in _PLAIN_SCHEMES | _TLS_SCHEMES:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SMTP_URL_ENV}={url!r} names the scheme {parts.scheme!r}; this "
            f"service speaks SMTP, so the URL must be smtp:// or smtps://"
        )
    if not parts.hostname:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SMTP_URL_ENV}={url!r} names no host to send through"
        )
    host = parts.hostname
    try:
        port = parts.port or _DEFAULT_SMTP_PORTS[scheme]
    except ValueError as exc:  # an unparseable port; urlsplit raises on `.port`
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SMTP_URL_ENV}={url!r} does not name a usable port"
        ) from exc
    username = parts.username
    password = parts.password

    def _deliver(email: str, token: str, expires_at: datetime) -> None:
        import smtplib

        message = _message(sender, email, _magic_link_url(base_url, token), expires_at)
        transport = smtplib.SMTP_SSL if scheme in _TLS_SCHEMES else smtplib.SMTP
        with transport(host, port, timeout=MAGIC_LINK_SMTP_TIMEOUT_SECONDS) as session:
            if scheme in _PLAIN_SCHEMES and username:
                # Credentials over a cleartext session would be handed to anybody on the path,
                # so upgrade first. A submission MTA that will not upgrade is one this service
                # must not authenticate to, and `starttls` raising is the right answer.
                session.starttls()
            if username:
                session.login(username, password or "")
            session.send_message(message)

    return _deliver


_CONSOLE_RULE = "=" * 86


def _shout(lines: list[str]) -> None:
    """Write ``lines`` to this process's stdout and flush.

    ``print`` rather than :data:`_log`, and deliberately: the console transport exists for a
    launcher whose logging is configured at ``warning`` on uvicorn's own loggers and not on
    this package's, so a ``_log.warning`` here is frequently swallowed. The one thing this
    transport must never do is publish a credential to a stream the operator is not reading.
    Both are emitted — the log line for anything scraping logs, the stdout block for the
    person watching the terminal.
    """
    stream = sys.stdout
    for line in lines:
        print(line, file=stream)
    stream.flush()


def _announce_console(base_url: str) -> None:
    """Say, at boot and in as many words, that this process publishes bearer credentials.

    An operator who reads one line of this service's start-up must be able to tell that
    sign-in tokens are going to stdout. That is what makes the console transport a decision
    somebody can be held to rather than a surprise found later in a log file.
    """
    _log.warning(
        "%s=%s: this process will PRINT every magic-link sign-in token to stdout. Local "
        "development only — anyone who can read this process's output can sign in as any "
        "address that asks for a link. Links point at %s",
        MAGIC_LINK_TRANSPORT_ENV,
        CONSOLE_TRANSPORT,
        base_url,
    )
    _shout(
        [
            "",
            _CONSOLE_RULE,
            f"  MAGIC-LINK TRANSPORT: {CONSOLE_TRANSPORT}"
            f"   ({MAGIC_LINK_TRANSPORT_ENV}={CONSOLE_TRANSPORT})",
            "",
            "  Every sign-in link this service issues will be PRINTED BELOW, in full,",
            "  including its token. That token is a live bearer credential: whoever reads",
            "  it signs in as whoever asked for it. This is a local-development transport.",
            "  Anything that can read this process's stdout — a log shipper, a CI artifact,",
            "  a screen share — is an authentication bypass while this is set.",
            "",
            f"  Links are built from {MAGIC_LINK_BASE_URL_ENV}={base_url}",
            _CONSOLE_RULE,
            "",
        ]
    )


def _console_delivery(base_url: str) -> Callable[[str, str, datetime], None]:
    """A ``deliver`` that prints the sign-in link to stdout. LOCAL DEVELOPMENT ONLY.

    Safe on a laptop, unsafe anywhere else, and the difference is who can read the stream.
    Under ``apps/buyer/devstack/run.py`` stdout is one terminal belonging to the one person
    who is also the buyer, and without it the demo journey stops dead at a sign-in gate that
    no local deployment can satisfy — there is no mail transport on a workstation, and the
    fail-closed default (correctly) refuses to invent one. On any shared deployment the same
    line lands wherever stdout is collected and is readable by people who authenticated as
    nobody, for as long as that log is kept.

    Reachable only from :data:`MAGIC_LINK_TRANSPORT_ENV` = :data:`CONSOLE_TRANSPORT`; see
    :func:`_selected_transport` for why nothing else can arrive here.

    Args:
        base_url: the origin the printed link points at, already validated.
    """

    def _deliver(email: str, token: str, expires_at: datetime) -> None:
        # The address IS printed, unlike everywhere else in this service. The whole use is
        # "I typed an address into the page, which link is mine" — with several links in one
        # scrollback and no address on them, the operator has to guess, and guessing wrong
        # means redeeming somebody else's single-use token. The audience for this stream is
        # already the person who typed the address.
        _shout(
            [
                "",
                _CONSOLE_RULE,
                "  MAGIC LINK (printed because this deployment asked for the console",
                "  transport; this is a live single-use credential)",
                "",
                f"    to       {email}",
                f"    open     {_magic_link_url(base_url, token)}",
                f"    expires  {expires_at.isoformat()}",
                _CONSOLE_RULE,
                "",
            ]
        )

    return _deliver


def _selected_transport() -> str | None:
    """The transport this deployment explicitly chose, or ``None`` if it stated nothing.

    The gate on the console transport, and the reason it cannot be tripped over. Every value
    outside :data:`MAGIC_LINK_TRANSPORTS` raises — it never degrades to the SMTP reading and
    it never degrades to the console, because "this value is not one I know" is a statement
    about the operator's intent that this function is not entitled to guess at. Only the
    absence of a statement (unset, or whitespace) returns ``None``, and ``None`` means the
    SMTP question below, which itself fails closed.

    Raises:
        MagicLinkTransportMisconfigured: a value was stated and is not one of
            :data:`MAGIC_LINK_TRANSPORTS`.
    """
    import os

    stated = str(os.environ.get(MAGIC_LINK_TRANSPORT_ENV) or "").strip()
    if not stated:
        return None
    if stated not in MAGIC_LINK_TRANSPORTS:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_TRANSPORT_ENV}={stated!r} is not a transport this service has; it "
            f"accepts exactly {' or '.join(repr(name) for name in MAGIC_LINK_TRANSPORTS)}. "
            f"Refused rather than guessed: {CONSOLE_TRANSPORT!r} prints live sign-in tokens "
            f"to stdout, so it is reachable only by naming it exactly, and a near miss must "
            f"never be read as either a request for it or a licence to ignore this setting."
        )
    return stated


def _base_url_from_env(*, chosen: str) -> str:
    """The validated front door every transport builds its link from.

    Shared by both transports on purpose: a console link that is not clickable is the same
    outage as a mailed one that is not, and the SPA reads its token off ``?token=`` at the
    ORIGIN ROOT, so a sub-path here 404s before the page loads.

    Raises:
        MagicLinkTransportMisconfigured: unset, or not an absolute http(s) URL.
    """
    import os

    base_url = _require(
        MAGIC_LINK_BASE_URL_ENV,
        os.environ.get(MAGIC_LINK_BASE_URL_ENV),
        "the link this service hands out needs a front door to point at",
        chosen=chosen,
    )
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_BASE_URL_ENV}={base_url!r} is not an absolute http(s) URL, so the "
            f"link this service hands out would not be clickable"
        )
    return base_url


def _undeliverable(email: str, token: str, expires_at: datetime) -> None:
    """The transport a deployment that configured none gets. Refuses; never returns.

    Takes the arguments and uses none of them: the message names the variables an operator
    must set and NOTHING about the request, because this string reaches a log line and a
    ``503`` body on an unauthenticated route.
    """
    raise MagicLinkUndeliverable(
        f"no magic-link mail transport is configured, so this service cannot deliver a login "
        f"link; set {MAGIC_LINK_SMTP_URL_ENV}, {MAGIC_LINK_SENDER_ENV} and "
        f"{MAGIC_LINK_BASE_URL_ENV}"
    )


def build_magic_link_delivery() -> Callable[[str, str, datetime], None]:
    """The ``deliver`` callable this process's login service is wired with (T-361).

    Returns:
        A callable ``deliver(email, token, expires_at)``. With
        :data:`MAGIC_LINK_TRANSPORT_ENV` set to :data:`CONSOLE_TRANSPORT` it prints the link
        to stdout; with :data:`MAGIC_LINK_SMTP_URL_ENV` set it mails the link; with neither,
        it is :func:`_undeliverable`, which raises. It is never
        :func:`~buyer_svc.auth.magic_link._drop`: silently discarding a login token is the
        defect, and a callable that returns normally having sent nothing is indistinguishable
        from a working transport to every caller above it.

    Raises:
        MagicLinkTransportMisconfigured: an MTA is named and the rest of the transport is
            missing or unusable; or :data:`MAGIC_LINK_TRANSPORT_ENV` names something that is
            not a transport. A boot failure on purpose — see the module docstring.
    """
    import os

    # FIRST, before any reading of the mail variables: this is the only question whose answer
    # can be "the console", and it is asked as an explicit statement of intent. An operator
    # who states nothing here reaches exactly the behaviour that shipped before it existed.
    selected = _selected_transport()
    if selected == CONSOLE_TRANSPORT:
        base_url = _base_url_from_env(
            chosen=f"{MAGIC_LINK_TRANSPORT_ENV}={CONSOLE_TRANSPORT} was chosen"
        )
        _announce_console(base_url)
        return _console_delivery(base_url)

    url = os.environ.get(MAGIC_LINK_SMTP_URL_ENV)
    if not url or not url.strip():
        if selected == SMTP_TRANSPORT:
            # An operator who wrote `smtp` here told this service what they meant to do, so
            # the silent 503 below would be a working-looking deployment that logs nobody in
            # — the state T-361 is about. Stated intent with nothing behind it is a boot
            # failure, exactly like the half-configured transport further down.
            raise MagicLinkTransportMisconfigured(
                f"{MAGIC_LINK_TRANSPORT_ENV}={SMTP_TRANSPORT} was chosen, but "
                f"{MAGIC_LINK_SMTP_URL_ENV} is unset: there is no MTA to hand the link to"
            )
        _log.warning(
            "%s is unset: this service will REFUSE magic-link logins rather than accept ones "
            "it cannot deliver. A local demo can set %s=%s instead, which PRINTS sign-in "
            "tokens to stdout and is for development only",
            MAGIC_LINK_SMTP_URL_ENV,
            MAGIC_LINK_TRANSPORT_ENV,
            CONSOLE_TRANSPORT,
        )
        return _undeliverable

    chosen = f"{MAGIC_LINK_SMTP_URL_ENV} is set, so this deployment intends to mail login links"
    sender = _require(
        MAGIC_LINK_SENDER_ENV,
        os.environ.get(MAGIC_LINK_SENDER_ENV),
        "an MTA needs an envelope sender, and mail from a guessed one is dropped as spam",
        chosen=chosen,
    )
    if "@" not in sender:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SENDER_ENV}={sender!r} is not an email address"
        )
    base_url = _base_url_from_env(chosen=chosen)
    return _smtp_delivery(url.strip(), sender, base_url)
