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
  raises :class:`MagicLinkTransportMisconfigured` from the builder rather than being demoted
  to "no transport", because an operator who configured half a transport meant to send mail
  and the quiet demotion would put them back in exactly the state this ticket is about while
  their configuration looked right.

  This paragraph used to end "and the process does not start, in the same shape as
  :class:`~buyer_svc.auth.routes.ProcessLocalStateUnsafe`". That was NOT TRUE of any
  deployment and the claim is removed rather than softened. MEASURED against the served app
  with an MTA named and no sender: ``create_app()`` returns, the ASGI lifespan completes, the
  container healthcheck passes, and the service runs — because nothing builds the login stack
  at start-up. :func:`~buyer_svc.auth.routes.auth_service` builds it lazily on first use, so
  the refusal lands on the FIRST SIGN-IN REQUEST, and it landed there as an unhandled ``500``.
  It is now a ``503`` with the reason in the log, translated in
  :func:`~buyer_svc.auth.routes.get_auth_service` — which is where it has to happen, since
  FastAPI resolves that dependency before any handler body a ``try`` could live in. The
  refusal is the same; only the claim about *when* and *how* it surfaces was wrong.
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

What the SMTP transport does on the wire, and what it used to do
----------------------------------------------------------------
``smtp`` was an accepted value from the day this module existed and it did deliver mail —
MEASURED against a real SMTP sink, a request produced a real conversation, a real message,
and a link that opened a session. Three things it did on the way there were wrong, and all
three are about a session that is weaker than the configuration says it is:

* **TLS was never verified.** ``smtplib`` builds its own SSL context when handed none, using
  ``ssl._create_stdlib_context()`` — another name for :func:`ssl._create_unverified_context`,
  which reports ``check_hostname=False`` and ``verify_mode=CERT_NONE``. So ``smtps://`` and
  every STARTTLS upgrade were encrypted against a passive listener and open to an active one:
  anything able to answer for the MTA's address could present any certificate, take the SMTP
  password and read every sign-in token. :func:`_tls_context` is now passed explicitly to
  both, and it is :func:`ssl.create_default_context`, which checks the chain and the hostname.
* **A session with no credentials was never upgraded at all.** The old STARTTLS call was
  guarded by ``if username``, on the reasoning that TLS protects the password. It does, but
  the password is not the only credential in the conversation: the MESSAGE carries a live
  single-use sign-in token, so a relay that authorises by source address — a very ordinary
  shape — sent every buyer's login link across the network in the clear. STARTTLS is now
  unconditional on ``smtp://`` and *required*: an MTA that does not offer it ends the
  delivery rather than being sent to anyway. :data:`MAGIC_LINK_SMTP_STARTTLS_ENV` can name
  the weaker thing, in the same shape as the console transport above, and says so at boot.
* **A credential could only be given in the URL, where it could not be spelled.**
  ``urlsplit`` does not percent-decode userinfo, and a raw ``/`` in a password re-parses the
  URL into a different host entirely, so the documented way to configure a real provider did
  not work for the passwords real providers issue.
  :data:`MAGIC_LINK_SMTP_USERNAME_ENV` and :data:`MAGIC_LINK_SMTP_PASSWORD_ENV` take it
  literally instead; the URL form still works and is now decoded.

A fourth was about what this module SAYS rather than what it sends: the boot failures for a
bad MTA URL interpolated that URL verbatim, so a deployment following the documented
credentials-in-the-URL form printed its SMTP password into its own crash. Every one of those
messages now goes through :func:`_safe_url`, which rebuilds the URL from parsed components
and never carries userinfo. That is the same property the fail-closed path already had for
the magic-link token, extended to the other credential in this module.

What being *mailed* costs, and what is done about each part
-----------------------------------------------------------
A link in a mailbox is a bearer credential in a place this service does not control, and
three specific things happen to it that do not happen to a link printed on a terminal:

* **It gets scanned.** Mail security products fetch every URL in a message before the human
  sees it. That does not burn the link here, and the reason is structural rather than lucky:
  the mailed URL points at the SPA's ORIGIN ROOT, and redemption is
  ``POST /buyer/auth/session`` — a different path, a different method, and a request the SPA
  makes only after it reads ``?token=`` out of its own address bar. A scanner that GETs the
  link loads a page. Only a scanner that also executes that page's JavaScript would spend the
  token. Keeping redemption off a GET is therefore load-bearing rather than stylistic, and
  there is a test that spends a GET at the mailed link and then redeems it.
* **It arrives twice.** SMTP is at-least-once and so is every provider on top of it; a
  message can be duplicated, and a buyer can click both copies. Both carry the same token, so
  the second redemption is refused as *already used* by
  :meth:`~buyer_svc.auth.magic_link.MagicLinkAuth.redeem` — the same answer a replay gets,
  which is the correct one. Nothing here needs to deduplicate.
* **It gets forwarded.** Nothing in a mail can stop that, and this module does not pretend
  otherwise: whoever holds the link can sign in as the address it was sent to, until it is
  redeemed once or until it expires (:data:`~buyer_svc.auth.magic_link.DEFAULT_LINK_TTL`,
  fifteen minutes), whichever is first — and requesting a new link kills the old one. That is
  the security of the scheme, stated plainly rather than implied, and it is why the mail says
  in as many words that the link works once and when it stops working.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import ssl
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import SplitResult, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

_log = logging.getLogger(__name__)

__all__ = [
    "CONSOLE_TRANSPORT",
    "MAGIC_LINK_BASE_URL_ENV",
    "MAGIC_LINK_SENDER_ENV",
    "MAGIC_LINK_SMTP_CA_BUNDLE_ENV",
    "MAGIC_LINK_SMTP_PASSWORD_ENV",
    "MAGIC_LINK_SMTP_STARTTLS_ENV",
    "MAGIC_LINK_SMTP_STARTTLS_POLICIES",
    "MAGIC_LINK_SMTP_TIMEOUT_SECONDS",
    "MAGIC_LINK_SMTP_URL_ENV",
    "MAGIC_LINK_SMTP_USERNAME_ENV",
    "MAGIC_LINK_SUBJECT",
    "MAGIC_LINK_TRANSPORTS",
    "MAGIC_LINK_TRANSPORT_ENV",
    "SMTP_TRANSPORT",
    "STARTTLS_DISABLED",
    "STARTTLS_REQUIRED",
    "MagicLinkDeliveryFailed",
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

#: The MTA to hand the message to, as a URL: ``smtp://host:port`` for a session this service
#: upgrades with STARTTLS, ``smtps://host:port`` for implicit TLS. Credentials may ride in the
#: URL (``smtp://user:password@host``) and are **percent-decoded**, but see
#: :data:`MAGIC_LINK_SMTP_USERNAME_ENV` for why a real provider's password does not belong
#: here. Unset means "this deployment has no mail transport", a refusal and not a default.
MAGIC_LINK_SMTP_URL_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_URL"

#: The SMTP username, kept out of the URL. The reason is not taste: ``urlsplit`` does not
#: percent-decode userinfo, so a credential embedded in the URL has to be encoded by hand and
#: is easy to get wrong — and a password containing a raw ``/`` (SES issues base64, so ``/``
#: and ``+`` are routine) does not merely fail to decode, it re-parses the whole URL. MEASURED
#: on ``smtp://user:ab/cd@mail.example.net:587``: ``hostname`` came back ``'user'`` and the
#: rest of the credential landed in the URL's path, which is a service that boots clean and
#: dials a host that does not exist. Set here instead and the value is taken literally.
MAGIC_LINK_SMTP_USERNAME_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_USERNAME"

#: The SMTP password. Used **byte for byte** — unlike every other variable this module reads,
#: it is not stripped, because altering a credential to be helpful is how a service ends up
#: authenticating as something nobody configured. A trailing newline from ``$(cat secret)`` is
#: therefore a real character and the MTA's ``535`` is the honest answer; the delivery failure
#: says so. Never logged, never echoed into an exception, never put on an HTTP response.
MAGIC_LINK_SMTP_PASSWORD_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_PASSWORD"

#: Whether a plain ``smtp://`` session must be upgraded to TLS before anything is said on it.
#: Accepts exactly :data:`STARTTLS_REQUIRED` or :data:`STARTTLS_DISABLED`; unset is
#: :data:`STARTTLS_REQUIRED`. Meaningless for ``smtps://``, which is already TLS.
MAGIC_LINK_SMTP_STARTTLS_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_STARTTLS"

#: Upgrade the session with STARTTLS and abandon delivery if the MTA will not. The default,
#: and not an opportunistic one: the message body carries a live single-use bearer credential,
#: so a session that quietly stays in cleartext because the server did not offer the extension
#: publishes sign-in tokens to anybody on the path. Every submission provider offers it.
STARTTLS_REQUIRED = "required"

#: Speak plain SMTP and never upgrade. For a sink on this host — a test double, a local
#: catch-all MTA — where the session never leaves the machine. Naming it is the whole point:
#: it is a stated decision to put sign-in tokens on the wire in the clear, it is announced at
#: boot when the MTA is not loopback, and credentials are refused on it (see
#: :func:`_credentials_from_env`).
STARTTLS_DISABLED = "disabled"

#: Every value :data:`MAGIC_LINK_SMTP_STARTTLS_ENV` accepts. Matched exactly after stripping,
#: with no case folding and no aliases, for :data:`MAGIC_LINK_TRANSPORTS`' reason: ``off``,
#: ``Disabled``, ``false`` and ``0`` are boot failures naming what would have been accepted,
#: because every one of them is a request to stop encrypting and none may be guessed at.
MAGIC_LINK_SMTP_STARTTLS_POLICIES = (STARTTLS_REQUIRED, STARTTLS_DISABLED)

#: A PEM file of extra certificate authorities to trust, for an MTA whose certificate no
#: public CA issued — a company's internal submission host, or a sink in a test. **Added to**
#: the system trust store rather than replacing it, so setting it never silently stops a
#: public provider verifying. Unset is the system trust store alone, which is what a public
#: provider needs. A path that does not exist is a boot failure, not a quiet fallback to
#: trusting less.
MAGIC_LINK_SMTP_CA_BUNDLE_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_CA_BUNDLE"

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


class MagicLinkDeliveryFailed(RuntimeError):
    """A transport IS configured and the conversation with the MTA did not complete.

    The third answer, and deliberately not either of the two above. It is not
    :class:`MagicLinkTransportMisconfigured`, because nothing about the configuration is
    provably wrong — the MTA was refusing connections, or the certificate did not verify, or
    the credentials were rejected, and all three are conditions a running service meets and
    recovers from rather than reasons to refuse to boot. And it is emphatically not
    :class:`MagicLinkUndeliverable`, whose whole contract is *nothing was attempted*: SMTP is
    at-least-once, so a session that hands the message over and then breaks reading the
    ``250`` has put a live link in the buyer's mailbox AND raised. Callers that treat this as
    "no mail was sent" are wrong in the direction that matters — see the rate-limit refund
    reasoning in :func:`~buyer_svc.auth.routes.request_magic_link`.

    **What its message may contain, and what it may never contain.** It reaches a log line, so
    it names the host and port that were dialled and the MTA's own words, which is what an
    operator needs to tell "connection refused" from "535 authentication failed" from "the
    certificate does not verify". It never carries the SMTP password, the magic-link token,
    the URL the credential rode in on, or the buyer's address — the first two are credentials
    and the last is an oracle on an unauthenticated route.
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


def _sender_address(sender: str) -> str:
    """The single mailbox ``sender`` names, or a boot failure. Never guesses one.

    Validated with :func:`email.utils.getaddresses` because that is what
    ``smtplib.send_message`` uses to compute the envelope ``MAIL FROM`` — checking the sender
    a different way than the library will read it is how a value passes validation and then
    produces a message nobody accepts. The old check was ``"@" in sender``, and MEASURED
    against a real MTA that admitted three deployments that boot clean and cannot send:

    * ``Proxy, Shop <a@b.example>`` — an UNQUOTED COMMA in a display name, which is the one
      an operator actually writes (``Proxyshop, Inc. <no-reply@…>``). It parses as two
      addresses, and ``send_message`` took the first, so the envelope was
      ``MAIL FROM:<Proxy>``. Every real MTA rejects that. Quoting it —
      ``"Proxyshop, Inc." <no-reply@…>`` — is accepted here, because it is correct.
    * ``@`` and ``a@`` — one ``@``, so the old check passed, and ``getaddresses`` returns an
      empty address: the envelope became ``MAIL FROM:<>``, the NULL REVERSE-PATH that RFC 5321
      reserves for bounce notifications. Mail claiming to be a bounce is filtered as one.
    * two addresses separated by a comma, where only the first was ever used.

    All three then fell through to a ``Message-ID`` ending ``@localhost`` — the exact "spam
    signal, and untraceable in the provider's delivery log" that stamping the sender's domain
    exists to avoid. With this refusal that fallback is unreachable.

    Returns:
        The address part, e.g. ``no-reply@proxyshop.example`` for
        ``Proxyshop <no-reply@proxyshop.example>``.

    Raises:
        MagicLinkTransportMisconfigured: ``sender`` does not name exactly one mailbox with
            both a local part and a domain. The value IS echoed — unlike the MTA URL, an
            envelope sender is a public identity and not a credential.
    """
    from email.utils import getaddresses

    found = getaddresses([sender])
    addresses = [address for _, address in found]
    if len(addresses) != 1:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SENDER_ENV}={sender!r} names {len(addresses)} addresses, not one; "
            f"`smtplib` would take the first as the envelope sender. A comma inside a display "
            f"name has to be quoted — '\"Proxyshop, Inc.\" <no-reply@example.com>'"
        )
    address = addresses[0]
    local, _, domain = address.rpartition("@")
    if not local or not domain:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SENDER_ENV}={sender!r} is not an email address: it reads as "
            f"{address!r}, which has no {'domain' if local else 'local part'}. An empty "
            f"envelope sender is the null reverse-path RFC 5321 reserves for bounce "
            f"notifications, and mail that claims to be a bounce is filtered as one"
        )
    return address


def _sender_domain(sender: str) -> str:
    """The domain half of ``sender``, for :func:`email.utils.make_msgid`.

    Passed explicitly because ``make_msgid()`` with no domain falls back to
    :func:`socket.getfqdn`, which stamps the container's internal hostname into a header that
    travels to the buyer's mailbox and into their provider's logs. The sending domain is the
    right answer anyway: a ``Message-ID`` whose right-hand side matches the ``From`` domain is
    what receivers expect, and one reading ``@buyer-svc-7d9f.internal`` is a spam signal.

    :func:`_sender_address` has already refused everything that has no domain, so the
    ``localhost`` below is unreachable from :func:`build_magic_link_delivery` and is kept only
    so a direct caller cannot produce a ``Message-ID`` with no right-hand side at all.
    """
    _, _, domain = _sender_address(sender).rpartition("@")
    return domain or "localhost"


def _message(sender: str, email: str, link: str, expires_at: datetime) -> Any:
    """The mail itself: plain text, four headers past the obvious ones, and no HTML.

    The headers are not decoration, and each one is a deliverability defect that was in the
    delivered message before it was added — MEASURED by reading what a real SMTP sink received
    off the wire, which reported ``Date`` and ``Message-ID`` both absent:

    * **Date** is mandatory under RFC 5322 §3.6 and ``smtplib.send_message`` does NOT add one
      (it reads ``Resent-Date`` and writes nothing). A submission without it is rewritten by
      some MTAs, scored by most filters and rejected outright by a few.
    * **Message-ID** is what every downstream system uses to say "this mail" — deduplication
      at the receiver, threading, and the operator's own search of the provider's delivery
      log when a buyer says no link arrived. Without it a hosted provider mints one, so the
      id in the provider's dashboard exists nowhere in this service's world.
    * **Auto-Submitted: auto-generated** (RFC 3834) tells vacation responders and ticketing
      systems not to reply. A support desk that auto-replies to a sign-in link opens a ticket
      containing a live bearer credential.
    * **X-Auto-Response-Suppress** is the same instruction in the dialect Exchange and Outlook
      actually honour, which is a large share of the mailboxes a demo will be sent to.

    Plain text and no ``multipart/alternative`` HTML part, deliberately. A single text part is
    the least spam-scored shape there is, it cannot render the link's visible text differently
    from its target (the exact pattern phishing filters quarantine), and the link survives
    being copied out of a client that strips markup.
    """
    from email.message import EmailMessage
    from email.utils import format_datetime, make_msgid

    message = EmailMessage()
    message["Subject"] = MAGIC_LINK_SUBJECT
    message["From"] = sender
    message["To"] = email
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = make_msgid(domain=_sender_domain(sender))
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content(
        "Someone asked to sign in to Proxyshop with this email address.\n\n"
        f"{link}\n\n"
        f"The link works once and stops working at {expires_at.isoformat()}.\n"
        "If this was not you, nothing has happened to your account and you can ignore this "
        "message.\n"
    )
    return message


def _safe_url(parts: SplitResult, *, port: object = "…") -> str:
    """``parts`` rendered for a human, rebuilt from components so no credential can ride out.

    Every misconfiguration message about the MTA URL goes through here, and the reason is a
    real leak rather than a stylistic one: those messages used to interpolate the raw
    ``PROXYSHOP_BUYER_MAGIC_LINK_SMTP_URL`` with ``{url!r}``, so a deployment that put the
    provider's password in the URL — which is the documented way to configure one — printed
    that password into its own boot failure, from where it reaches a log aggregator, a CI
    artifact and a crash-loop's container journal.

    Rebuilt from ``urlsplit``'s parsed pieces rather than redacted with a substitution over
    the string, because the string is exactly what cannot be trusted: a password containing a
    raw ``/`` re-parses so that half of it is the "host" and half is the path, and a
    search-and-replace over the original has no idea which characters were the secret.
    """
    userinfo = "<credentials-redacted>@" if (parts.username or parts.password) else ""
    return f"{parts.scheme}://{userinfo}{parts.hostname or '<no-host>'}:{port}"


def _without_recipient(words: str, email: str) -> str:
    """``words`` with the buyer's address taken out, wherever the MTA put it.

    Every other refusal this route can produce is worded to name no address — a message on an
    unauthenticated route that says *which* address it was about is an oracle, and a log line
    that says it is a mailing list somebody exported. An MTA's own error text is the one place
    that rule is not under this module's control: :class:`smtplib.SMTPRecipientsRefused`
    stringifies to ``{'dana@example.com': (550, b'...')}``, and a provider is free to echo the
    recipient in any other response as well.

    Matched case-insensitively because a receiving MTA may echo back a different casing than
    the one it was handed, and substring rather than exact because the address is embedded in
    a larger structure. Everything else in the MTA's words is kept: the code and the reason
    are what an operator needs, and they are not about any particular buyer.

    The LOCAL PART is taken out as well as the whole address, because an MTA is free to echo
    only that half — ``550 5.1.1 <dana.reyes> unknown user`` is an ordinary refusal — and a
    log line naming the mailbox is the same disclosure whether or not the domain came with it.
    Only when it is at least three characters: shorter than that the local part is as likely
    to be a substring of the MTA's own words as of anybody's address, and mangling the
    operator's diagnostic to hide two letters is a worse trade than leaving them.
    """
    redacted = words
    for needle in (email, email.partition("@")[0]):
        if len(needle) >= 3:
            redacted = _replace_fold(redacted, needle, "<recipient-redacted>")
    return redacted


def _replace_fold(text: str, needle: str, replacement: str) -> str:
    """``text`` with every case-insensitive occurrence of ``needle`` replaced."""
    lowered = text.casefold()
    target = needle.casefold()
    out: list[str] = []
    cursor = 0
    while (found := lowered.find(target, cursor)) != -1:
        out.append(text[cursor:found])
        out.append(replacement)
        cursor = found + len(target)
    out.append(text[cursor:])
    return "".join(out)


def _is_loopback(host: str) -> bool:
    """Does ``host`` name this machine, such that a session to it never touches a network?

    Deliberately mechanical and deliberately narrow — a literal loopback address, or the two
    names every operating system ships pointing at one. It is not a general "is this
    private": ``10.x`` and a company's internal hostname are *networks*, with switches and
    span ports and other tenants on them, and the carve-out this answers gates whether a
    credential may cross one in the clear.
    """
    if host.casefold() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _starttls_policy(*, scheme: str) -> str:
    """The STARTTLS policy this deployment stated, defaulting to :data:`STARTTLS_REQUIRED`.

    Note which way the default falls, because it is the opposite of the transport question one
    section down. There, "nothing stated" cannot mean the console, because the weaker thing
    must be chosen. Here, "nothing stated" means the STRONGER thing: an operator who says
    nothing about TLS gets TLS, and it is turning it OFF that has to be named.

    Raises:
        MagicLinkTransportMisconfigured: a value was stated and is not a policy.
    """
    stated = str(os.environ.get(MAGIC_LINK_SMTP_STARTTLS_ENV) or "").strip()
    if not stated:
        return STARTTLS_REQUIRED
    if stated not in MAGIC_LINK_SMTP_STARTTLS_POLICIES:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SMTP_STARTTLS_ENV}={stated!r} is not a policy this service has; it "
            f"accepts exactly "
            f"{' or '.join(repr(name) for name in MAGIC_LINK_SMTP_STARTTLS_POLICIES)}. "
            f"Refused rather than guessed: every plausible reading of an unknown value here "
            f"is a request to STOP ENCRYPTING a session that carries live sign-in tokens, and "
            f"that is not a decision this service may infer from a spelling."
        )
    if stated == STARTTLS_DISABLED and scheme in _TLS_SCHEMES:
        _log.warning(
            "%s=%s has no effect on an %s:// URL: implicit TLS is not STARTTLS, and this "
            "session is encrypted from the first byte either way",
            MAGIC_LINK_SMTP_STARTTLS_ENV,
            STARTTLS_DISABLED,
            scheme,
        )
    return stated


def _tls_context() -> ssl.SSLContext:
    """A context that actually VERIFIES the MTA's certificate.

    This exists because the default does not, and that is not a hypothetical. ``smtplib``
    builds its own context when handed none, and it builds it with
    ``ssl._create_stdlib_context()`` — which is :func:`ssl._create_unverified_context` under
    another name. MEASURED on this interpreter: that context reports ``check_hostname=False``
    and ``verify_mode=CERT_NONE``, in both :meth:`smtplib.SMTP.starttls` and
    :class:`smtplib.SMTP_SSL`. So the ``smtps://`` scheme, and every STARTTLS upgrade, bought
    encryption against a passive listener and NOTHING against an active one: anybody able to
    answer for the MTA's address could present any certificate at all, take the SMTP password,
    and read every sign-in token this service sends. :func:`ssl.create_default_context` is the
    one that checks the chain and the hostname.

    Raises:
        MagicLinkTransportMisconfigured: :data:`MAGIC_LINK_SMTP_CA_BUNDLE_ENV` names a file
            this process cannot read as PEM. Loudly, rather than carrying on with the system
            trust store: an operator who named a private CA and got the default one back is a
            deployment whose TLS silently does not do what its configuration says.
    """
    context = ssl.create_default_context()
    bundle = str(os.environ.get(MAGIC_LINK_SMTP_CA_BUNDLE_ENV) or "").strip()
    if bundle:
        try:
            context.load_verify_locations(cafile=bundle)
        except OSError as exc:
            raise MagicLinkTransportMisconfigured(
                f"{MAGIC_LINK_SMTP_CA_BUNDLE_ENV}={bundle!r} could not be loaded as a PEM "
                f"certificate bundle ({exc.__class__.__name__}: {exc}); refusing to fall back "
                f"to the system trust store, which is not what this deployment asked for"
            ) from exc
    return context


def _credentials_from_env(
    parts: SplitResult, *, host: str, starttls: str, implicit_tls: bool
) -> tuple[str, str]:
    """The SMTP username and password, from the two variables or from the URL — never both.

    Three rules, and the first two are about a credential that silently is not the one the
    operator typed:

    * **The URL's userinfo is percent-DECODED.** ``urlsplit`` does not decode it, so the old
      reading handed ``p%40ss`` to the MTA verbatim and the operator saw a ``535`` they could
      not explain. A password that has to survive URL parsing is a bad place to keep one at
      all, which is what the two dedicated variables are for, but a URL that documents
      percent-encoding must honour it.
    * **Stating a credential twice is a boot failure.** Silently preferring one source would
      mean an operator who rotated the password in the variable while an old one sat in the
      URL — or the reverse — authenticating with the stale one and having no way to tell.
    * **Half a credential is a boot failure.** A username with no password used to become
      ``login(username, "")``, an empty-password AUTH that every provider rejects with the
      same unhelpful ``535`` a wrong password gets.

    Returns:
        ``(username, password)``, both empty strings when this deployment does not
        authenticate — which is a normal shape for a relay that authorises by source address.

    Raises:
        MagicLinkTransportMisconfigured: the credential is ambiguous, half-stated, or would
            be sent over a session this service knows is in the clear.
    """
    url_user = unquote(parts.username) if parts.username else ""
    url_password = unquote(parts.password) if parts.password else ""
    # `username` is an identifier and is stripped like every other setting here. `password` is
    # NOT: see MAGIC_LINK_SMTP_PASSWORD_ENV. Only "" means "unset" for it.
    env_user = str(os.environ.get(MAGIC_LINK_SMTP_USERNAME_ENV) or "").strip()
    env_password = os.environ.get(MAGIC_LINK_SMTP_PASSWORD_ENV) or ""

    if (env_user or env_password) and (url_user or url_password):
        raise MagicLinkTransportMisconfigured(
            f"an SMTP credential is stated twice: in {MAGIC_LINK_SMTP_URL_ENV}'s userinfo and "
            f"in {MAGIC_LINK_SMTP_USERNAME_ENV}/{MAGIC_LINK_SMTP_PASSWORD_ENV}. Refused rather "
            f"than ranked: whichever this service preferred, a rotation that updated the other "
            f"one would leave it authenticating with a credential the operator believes is "
            f"gone. Keep the credential in exactly one of the two places"
        )
    username = env_user or url_user
    password = env_password or url_password
    if bool(username) != bool(password):
        missing = MAGIC_LINK_SMTP_PASSWORD_ENV if username else MAGIC_LINK_SMTP_USERNAME_ENV
        raise MagicLinkTransportMisconfigured(
            f"half an SMTP credential is configured and {missing} is the missing half; an MTA "
            f"asked to authenticate with only one of the two answers 535, which looks exactly "
            f"like a wrong password"
        )
    for name, value in (
        (MAGIC_LINK_SMTP_USERNAME_ENV, username),
        (MAGIC_LINK_SMTP_PASSWORD_ENV, password),
    ):
        # ASCII, checked HERE, because the alternative is a credential in an exception object.
        # `smtplib.SMTP.auth` builds the AUTH PLAIN blob as ``("\0%s\0%s" % (user, password))
        # .encode('ascii')``, so a non-ASCII byte raises UnicodeEncodeError at login time —
        # and UnicodeEncodeError is a ValueError, NOT an OSError, so the delivery clause below
        # does not catch it: the route answered a bare 500, nothing was logged that an
        # operator could act on, and the exception object carried
        # ``'\x00apikey\x00pässwörd…'`` — the whole credential — in its ``object`` attribute,
        # where any handler that reprs an exception would find it. SMTP AUTH cannot carry
        # these bytes at all, so there is no working deployment being refused here; the value
        # is named by VARIABLE and never echoed.
        try:
            value.encode("ascii")
        except UnicodeEncodeError:
            raise MagicLinkTransportMisconfigured(
                f"{name} contains a non-ASCII character. SMTP AUTH cannot carry one — "
                f"`smtplib` encodes the credential as ASCII and would raise mid-login, which "
                f"this service cannot tell apart from a network failure. Refused at boot "
                f"instead, and the value is deliberately not repeated here"
            ) from None
    # The session is in the clear only when NOTHING encrypts it. `implicit_tls` matters as much
    # as the STARTTLS policy: an `smtps://` URL is encrypted from the first byte, so a
    # deployment that sets STARTTLS=disabled once in its environment — a perfectly reasonable
    # thing to do alongside a local sink — must not then be blocked from booting against a
    # provider on port 465. `_starttls_policy` has ALREADY logged that the setting is inert on
    # that scheme; refusing on it here as well said the opposite of that log line, and said it
    # with a message ("a session in the clear") that was factually wrong.
    in_the_clear = not implicit_tls and starttls == STARTTLS_DISABLED
    if username and in_the_clear and not _is_loopback(host):
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SMTP_STARTTLS_ENV}={STARTTLS_DISABLED} was chosen for the MTA at "
            f"{host!r}, which is not this machine, and an SMTP credential is configured. "
            f"Refusing to hand that credential to a session in the clear: it is not one buyer's "
            f"link but the standing right to send mail as this deployment, and it is not this "
            f"service's to spend. Either drop {MAGIC_LINK_SMTP_STARTTLS_ENV} (the default "
            f"requires TLS) or point the transport at a sink on this host — spelled as a "
            f"literal loopback address, so 127.0.0.1 rather than 127.1 or a name that merely "
            f"resolves to it"
        )
    return username, password


def _announce_cleartext_smtp(host: str, port: int) -> None:
    """Say, at boot, that sign-in tokens are about to cross a network unencrypted.

    :data:`STARTTLS_DISABLED` is a legitimate choice against a sink on this machine and is a
    credential leak against anything else, and — exactly as with the console transport — the
    difference has to be visible in the start-up of the process that made it, not discovered
    later. On stdout as well as the logger for :func:`_shout`'s reason: uvicorn's logging
    configuration frequently swallows this package's warnings.
    """
    _log.warning(
        "%s=%s and the MTA at %s:%s is not this machine: every magic-link sign-in token this "
        "service sends will cross the network in the clear, readable by anything on the path",
        MAGIC_LINK_SMTP_STARTTLS_ENV,
        STARTTLS_DISABLED,
        host,
        port,
    )
    _shout(
        [
            "",
            _CONSOLE_RULE,
            f"  MAGIC-LINK SMTP IS UNENCRYPTED  ({MAGIC_LINK_SMTP_STARTTLS_ENV}="
            f"{STARTTLS_DISABLED})",
            "",
            f"  Mail to {host}:{port} is sent over plain SMTP with no STARTTLS upgrade, so",
            "  every sign-in link this service issues crosses the network IN THE CLEAR.",
            "  Each message contains a live single-use credential, so anything that can read",
            "  this connection can sign in as the buyer who asked for it. That is a",
            "  local-sink setting; on a network it is an authentication bypass.",
            _CONSOLE_RULE,
            "",
        ]
    )


def _smtp_delivery(url: str, sender: str, base_url: str) -> Callable[[str, str, datetime], None]:
    """A ``deliver`` that hands the link to the configured MTA. See the module docstring."""
    parts = urlsplit(url)
    scheme = parts.scheme.casefold()
    if scheme not in _PLAIN_SCHEMES | _TLS_SCHEMES:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SMTP_URL_ENV}={_safe_url(parts)} names the scheme {parts.scheme!r}; "
            f"this service speaks SMTP, so the URL must be smtp:// or smtps://"
        )
    if not parts.hostname:
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SMTP_URL_ENV}={_safe_url(parts)} names no host to send through"
        )
    host = parts.hostname
    try:
        port = parts.port or _DEFAULT_SMTP_PORTS[scheme]
    except ValueError:  # an unparseable port; urlsplit raises on `.port`
        # `from None`, and it is the SECOND half of the same leak `_safe_url` closes rather
        # than a style choice. `urlsplit`'s ValueError stringifies as "Port could not be cast
        # to integer value as 'BJf7q'" — the text it failed on — and for the exact password
        # shape this module documents, a raw `/` in a base64 provider password, that text IS a
        # slice of the password: `smtp://apikey:BJf7q/rest@host` re-parses so the fragment
        # before the first `/` becomes the "port". Chaining it with `from exc` puts that slice
        # in the `__cause__` traceback, which is what an uncaught boot failure writes to
        # stderr — the log aggregator and crash-loop journal `_safe_url` exists to keep it out
        # of. Suppressed rather than scrubbed: nothing in the cause is worth the risk of a
        # future urllib wording that quotes more of the string, and the message below already
        # says the only thing an operator can act on.
        raise MagicLinkTransportMisconfigured(
            f"{MAGIC_LINK_SMTP_URL_ENV}={_safe_url(parts, port='<unparseable>')} does not name "
            f"a usable port. NOTE: if the credential is in this URL, a raw '/', '@' or ':' in "
            f"the password re-parses it — percent-encode it, or use "
            f"{MAGIC_LINK_SMTP_USERNAME_ENV}/{MAGIC_LINK_SMTP_PASSWORD_ENV} instead"
        ) from None
    implicit_tls = scheme in _TLS_SCHEMES
    starttls = _starttls_policy(scheme=scheme)
    username, password = _credentials_from_env(
        parts, host=host, starttls=starttls, implicit_tls=implicit_tls
    )
    context = _tls_context()
    encrypted = implicit_tls or starttls == STARTTLS_REQUIRED
    if not encrypted and not _is_loopback(host):
        _announce_cleartext_smtp(host, port)

    def _deliver(email: str, token: str, expires_at: datetime) -> None:
        import smtplib

        message = _message(sender, email, _magic_link_url(base_url, token), expires_at)
        try:
            session = (
                smtplib.SMTP_SSL(
                    host, port, timeout=MAGIC_LINK_SMTP_TIMEOUT_SECONDS, context=context
                )
                if implicit_tls
                else smtplib.SMTP(host, port, timeout=MAGIC_LINK_SMTP_TIMEOUT_SECONDS)
            )
            with session:
                if not implicit_tls and starttls == STARTTLS_REQUIRED:
                    # Unconditional now, where it used to happen only when there were
                    # credentials to protect. The MESSAGE is the credential: it carries a live
                    # single-use bearer token, so a session left in cleartext because nobody
                    # had a password to send publishes sign-in links to the path. An MTA that
                    # does not offer the extension raises SMTPNotSupportedError here and the
                    # delivery is abandoned — refusing to send is the correct answer, and
                    # falling through to a cleartext send is the bug this replaced.
                    session.starttls(context=context)
                if username:
                    session.login(username, password)
                session.send_message(message)
        except OSError as exc:
            # `smtplib.SMTPException` and `ssl.SSLError` are both OSError subclasses, so this
            # one clause covers a refused connection, a timeout, a certificate that does not
            # verify, a rejected credential and a refused recipient. `str(exc)` is the MTA's
            # own words or the TLS stack's and carries no secret of OURS; `password` and
            # `token` are deliberately not in it, and neither is the URL they were configured
            # in — see MagicLinkDeliveryFailed.
            #
            # It can carry the BUYER'S, though, and that is not ours to log either:
            # `SMTPRecipientsRefused` stringifies as `{'dana@example.com': (550, b'...')}`,
            # which would put the address of everyone a failing MTA rejects into this
            # service's log. Every other refusal on this route is worded to name no address at
            # all (see `_undeliverable` and the rate-limit log lines), so the MTA's words go
            # through `_without_recipient` rather than being trusted to contain none.
            hint = (
                f" (the MTA refused the credential; {MAGIC_LINK_SMTP_PASSWORD_ENV} is used byte "
                f"for byte, so check it for a trailing newline)"
                if isinstance(exc, smtplib.SMTPAuthenticationError)
                else ""
            )
            raise MagicLinkDeliveryFailed(
                f"the MTA at {host}:{port} did not accept the sign-in link: "
                f"{_without_recipient(f'{exc.__class__.__name__}: {exc}', email)}{hint}"
            ) from exc

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
    _sender_address(sender)
    base_url = _base_url_from_env(chosen=chosen)
    return _smtp_delivery(url.strip(), sender, base_url)
