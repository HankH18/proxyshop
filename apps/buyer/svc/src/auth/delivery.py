"""Where a magic link actually goes (T-361, SPEC R5).

R5's first clause is "buyers authenticate with email magic link", and until this module
existed the second half of that sentence had no implementation anywhere in the tree:
:func:`~buyer_svc.auth.routes.build_auth_service` decided the vault, the account directory
and the profile publisher, and never decided the transport, so
``build_auth_service().deliver is magic_link._drop`` was True on every production path. The
route answered ``202 Accepted``, :func:`~buyer_svc.auth.magic_link._drop` threw the token
away in-process, and the buyer waited for a mail nothing had been asked to send. Nothing
logged, nothing failed, and the login gesture could not be completed in any deployment.

Two things fix that, and the second is the reason this module is not just an SMTP client:

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

What is deliberately NOT here
-----------------------------
A "print the link to the console" transport for local development. That is the thing
:func:`~buyer_svc.auth.magic_link._drop`'s docstring refuses — "a service booted without a
mail transport should fail to log buyers in, not publish bearer credentials to stdout" — and
a dev convenience that writes live login tokens into a log file is a credential leak wearing
a friendly name. A developer who wants to see the link injects their own ``deliver`` through
:func:`~buyer_svc.auth.routes.set_auth_service`, which is what every test in this tree does.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_log = logging.getLogger(__name__)

__all__ = [
    "MAGIC_LINK_BASE_URL_ENV",
    "MAGIC_LINK_SENDER_ENV",
    "MAGIC_LINK_SMTP_TIMEOUT_SECONDS",
    "MAGIC_LINK_SMTP_URL_ENV",
    "MAGIC_LINK_SUBJECT",
    "MagicLinkTransportMisconfigured",
    "MagicLinkUndeliverable",
    "build_magic_link_delivery",
]

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


def _require(name: str, value: str | None, why: str) -> str:
    if not value or not value.strip():
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SMTP_URL_ENV} is set, so this deployment intends to mail login "
            f"links, but {name} is unset: {why}"
        )
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
        :data:`MAGIC_LINK_SMTP_URL_ENV` set it mails the link; unset, it is
        :func:`_undeliverable`, which raises. It is never
        :func:`~buyer_svc.auth.magic_link._drop`: silently discarding a login token is the
        defect, and a callable that returns normally having sent nothing is indistinguishable
        from a working transport to every caller above it.

    Raises:
        MagicLinkTransportMisconfigured: an MTA is named and the rest of the transport is
            missing or unusable. A boot failure on purpose — see the module docstring.
    """
    import os

    url = os.environ.get(MAGIC_LINK_SMTP_URL_ENV)
    if not url or not url.strip():
        _log.warning(
            "%s is unset: this service will REFUSE magic-link logins rather than accept ones "
            "it cannot deliver",
            MAGIC_LINK_SMTP_URL_ENV,
        )
        return _undeliverable

    sender = _require(
        MAGIC_LINK_SENDER_ENV,
        os.environ.get(MAGIC_LINK_SENDER_ENV),
        "an MTA needs an envelope sender, and mail from a guessed one is dropped as spam",
    )
    if "@" not in sender:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SENDER_ENV}={sender!r} is not an email address"
        )
    base_url = _require(
        MAGIC_LINK_BASE_URL_ENV,
        os.environ.get(MAGIC_LINK_BASE_URL_ENV),
        "the mailed link needs a front door to point at",
    )
    if urlsplit(base_url).scheme not in {"http", "https"} or not urlsplit(base_url).netloc:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_BASE_URL_ENV}={base_url!r} is not an absolute http(s) URL, so the "
            f"link this service mails would not be clickable"
        )
    return _smtp_delivery(url.strip(), sender, base_url)
