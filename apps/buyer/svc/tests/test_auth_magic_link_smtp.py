"""The magic-link SMTP transport, driven over a real socket against a real MTA.

Why this file exists next to ``test_auth_deployment_wiring.py``, which already has an SMTP
test
-----------------------------------------------------------------------------------------
That test monkeypatches ``smtplib.SMTP`` with a recording double. It is a good test of the
wiring — it proves ``build_auth_service`` chose the transport out of the environment, that
the token reaches the message and not the response, and that the mailed link opens a session
— and it is *blind by construction* to everything that happens on a socket. The double's
``starttls`` sets a flag and returns; the double's ``login`` stores a tuple; there is no
certificate, no server, no protocol. So every one of the defects this module was written to
catch was invisible to it, and each one was real, MEASURED against the sink below on the code
as it stood:

* a session that advertised STARTTLS and never got it, delivering a live sign-in token in
  cleartext and answering ``202`` — because the upgrade was guarded by ``if username`` and a
  relay that authorises by source address has no username;
* an MTA that could not offer STARTTLS at all producing a bare ``500 Internal Server Error``
  with a traceback and nothing an operator could act on;
* a delivered message carrying neither ``Date`` nor ``Message-ID``, which
  ``smtplib.send_message`` does not add and RFC 5322 requires the first of;
* TLS with ``check_hostname=False`` and ``verify_mode=CERT_NONE``, because ``smtplib`` builds
  an *unverified* context when it is handed none.

The sink here is stdlib only — ``socketserver`` plus ``ssl`` — and that is a constraint
rather than a preference: ``aiosmtpd`` is not installed and is not in ``uv.lock``, and
``pyproject.toml`` belongs to another lane. It speaks enough SMTP to be a real peer: the
greeting, ``EHLO`` with a capability list it chooses, ``STARTTLS`` with a real handshake,
``AUTH PLAIN``/``AUTH LOGIN``, and a ``DATA`` phase it reassembles with
:func:`email.message_from_bytes`. It records what it saw at each step, which is how a test
can assert that a credential arrived *inside* TLS rather than merely that ``login`` was
called.

D40: it binds port 0 and reports the port it actually got. The root ``conftest`` turns on
``pytest-socket`` with ``allow_hosts=127.0.0.1``, which permits exactly this.
"""

from __future__ import annotations

import base64
import email
import pathlib
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
from collections.abc import Iterator
from email.message import Message
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

BUYER = "dana.reyes@example.com"

TRANSPORT_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT"
SMTP_URL_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_URL"
SENDER_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SENDER"
BASE_URL_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL"
USERNAME_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_USERNAME"
PASSWORD_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_PASSWORD"
STARTTLS_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_STARTTLS"
CA_BUNDLE_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_CA_BUNDLE"

MAGIC_LINK_ENVS = (
    TRANSPORT_ENV,
    SMTP_URL_ENV,
    SENDER_ENV,
    BASE_URL_ENV,
    USERNAME_ENV,
    PASSWORD_ENV,
    STARTTLS_ENV,
    CA_BUNDLE_ENV,
)

#: The origin the mailed link points at. Not this service: the SPA reads `?token=` off its own
#: address bar, so the link is a page the buyer opens, and this is the page's origin.
PAGE_ORIGIN = "http://127.0.0.1:8100/"

#: A password shaped like the ones real providers issue. SES hands out base64, so `/`, `+` and
#: `=` are routine, and `@` and `:` are what break a URL. Every credential test uses this one:
#: a password that only works because it happens to be alphanumeric proves nothing.
PROVIDER_PASSWORD = "p@ss/w0rd+Ab=="


# =======================================================================================
# The sink
# =======================================================================================


class _SinkHandler(socketserver.StreamRequestHandler):
    """One SMTP connection. Speaks enough of RFC 5321 to be a real peer for `smtplib`."""

    server: Any
    tls_active: bool

    def setup(self) -> None:
        self.tls_active = False
        super().setup()
        sink = self.server.sink
        if sink.implicit_tls:
            self.connection = sink.tls_context.wrap_socket(self.connection, server_side=True)
            self.rfile = self.connection.makefile("rb", -1)
            self.wfile = self.connection.makefile("wb", 0)
            self.tls_active = True
            sink.tls_negotiated = True

    def _say(self, line: str) -> None:
        self.wfile.write(line.encode("ascii") + b"\r\n")
        self.wfile.flush()

    def handle(self) -> None:  # noqa: C901 - a protocol switch is a switch
        sink = self.server.sink
        self._say("220 sink.invalid ESMTP proxyshop-test-sink")
        recipients: list[str] = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            sink.commands.append(verb)
            if verb == "EHLO":
                caps = ["250-sink.invalid"]
                if sink.offer_starttls and not self.tls_active:
                    caps.append("250-STARTTLS")
                caps.append("250-AUTH PLAIN LOGIN")
                caps.append("250 8BITMIME")
                for cap in caps:
                    self._say(cap)
            elif verb == "HELO":
                self._say("250 sink.invalid")
            elif verb == "STARTTLS":
                if not sink.offer_starttls:
                    self._say("502 5.5.1 not implemented")
                    continue
                self._say("220 2.0.0 ready to start TLS")
                self.connection = sink.tls_context.wrap_socket(self.connection, server_side=True)
                self.rfile = self.connection.makefile("rb", -1)
                self.wfile = self.connection.makefile("wb", 0)
                self.tls_active = True
                sink.tls_negotiated = True
            elif verb == "AUTH":
                self._authenticate(line)
            elif verb == "MAIL":
                sink.envelope_senders.append(line)
                self._say("250 2.1.0 ok")
            elif verb == "RCPT":
                recipients.append(line)
                if sink.refuse_recipient:
                    # `550 no such user` echoing the address back, which is what makes
                    # `SMTPRecipientsRefused` stringify with the buyer's mailbox in it. With
                    # `echo_local_part_only`, just the mailbox — an equally ordinary refusal,
                    # and the one that survives redacting the full address.
                    echoed = line.split(":", 1)[-1].strip().strip("<>")
                    if sink.echo_local_part_only:
                        echoed = f"<{echoed.partition('@')[0]}>"
                    self._say(f"550 5.1.1 no mailbox {echoed}")
                    continue
                self._say("250 2.1.5 ok")
            elif verb == "DATA":
                if sink.refuse_data:
                    # A real refusal, in the shape a provider uses for an unverified sender.
                    self._say("550 5.7.1 sender address not verified for this account")
                    continue
                self._say("354 end with <CRLF>.<CRLF>")
                self._say_ok_to(recipients, sink)
                recipients = []
            elif verb == "QUIT":
                self._say("221 2.0.0 bye")
                return
            elif verb in {"NOOP", "RSET"}:
                self._say("250 2.0.0 ok")
            else:
                self._say("502 5.5.2 not implemented")

    def _say_ok_to(self, recipients: list[str], sink: SMTPSink) -> None:
        chunks: list[bytes] = []
        while True:
            part = self.rfile.readline()
            if not part or part in (b".\r\n", b".\n"):
                break
            chunks.append(part[1:] if part.startswith(b"..") else part)
        sink.messages.append(email.message_from_bytes(b"".join(chunks)))
        sink.recipients.append(tuple(recipients))
        sink.message_encrypted.append(self.tls_active)
        self._say("250 2.0.0 queued")

    def _authenticate(self, line: str) -> None:
        sink = self.server.sink
        parts = line.split()
        mechanism = parts[1].upper() if len(parts) > 1 else ""
        if mechanism == "PLAIN":
            blob = parts[2] if len(parts) > 2 else ""
            if not blob:
                self._say("334 ")
                blob = self.rfile.readline().decode().strip()
            fields = base64.b64decode(blob).split(b"\0")
            sink.credentials = (fields[1].decode("utf-8"), fields[2].decode("utf-8"))
        elif mechanism == "LOGIN":
            self._say("334 " + base64.b64encode(b"Username:").decode())
            user = base64.b64decode(self.rfile.readline().strip()).decode("utf-8")
            self._say("334 " + base64.b64encode(b"Password:").decode())
            secret = base64.b64decode(self.rfile.readline().strip()).decode("utf-8")
            sink.credentials = (user, secret)
        else:
            self._say("504 5.5.4 unrecognised authentication type")
            return
        sink.credentials_encrypted = self.tls_active
        if sink.reject_credentials:
            self._say("535 5.7.8 authentication credentials invalid")
            return
        self._say("235 2.7.0 authentication succeeded")


class _SinkServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    sink: SMTPSink

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Record rather than print.

        The default writes a traceback to stderr, and half the tests here EXPECT the
        connection to break — a client that walks away mid-handshake because the certificate
        did not verify is the assertion, not a bug. Printing it would bury the real failures.
        """
        import sys
        import traceback

        self.sink.errors.append("".join(traceback.format_exception(*sys.exc_info())))


class SMTPSink:
    """A real, throwaway MTA on a real ephemeral port. Records; never forwards.

    Args:
        offer_starttls: advertise the ``STARTTLS`` extension and perform the upgrade.
        implicit_tls: wrap the connection the instant it is accepted, as an ``smtps``
            submission port does. Mutually exclusive with ``offer_starttls`` in practice.
        tls_context: the server side of the handshake, built from a throwaway certificate.
        reject_credentials: answer ``AUTH`` with ``535``, the way a wrong password is refused.
        refuse_data: answer ``DATA`` with ``550``, the way an unverified sender is refused.
        refuse_recipient: answer ``RCPT TO`` with ``550`` quoting the address back, the way a
            receiving MTA refuses an unknown mailbox. That echo is what makes
            :class:`smtplib.SMTPRecipientsRefused` stringify with the buyer's address in it.
    """

    def __init__(
        self,
        *,
        offer_starttls: bool = False,
        implicit_tls: bool = False,
        tls_context: ssl.SSLContext | None = None,
        reject_credentials: bool = False,
        refuse_data: bool = False,
        refuse_recipient: bool = False,
        echo_local_part_only: bool = False,
    ) -> None:
        self.offer_starttls = offer_starttls
        self.implicit_tls = implicit_tls
        self.tls_context = tls_context
        self.refuse_recipient = refuse_recipient
        self.echo_local_part_only = echo_local_part_only
        self.reject_credentials = reject_credentials
        self.refuse_data = refuse_data

        self.messages: list[Message] = []
        self.recipients: list[tuple[str, ...]] = []
        self.envelope_senders: list[str] = []
        self.message_encrypted: list[bool] = []
        self.commands: list[str] = []
        self.credentials: tuple[str, str] | None = None
        self.credentials_encrypted: bool | None = None
        self.tls_negotiated = False
        self.errors: list[str] = []

        self._server = _SinkServer(("127.0.0.1", 0), _SinkHandler)
        self._server.sink = self
        self.host, self.port = self._server.socket.getsockname()[:2]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="smtp-sink", daemon=True
        )

    @property
    def url(self) -> str:
        scheme = "smtps" if self.implicit_tls else "smtp"
        return f"{scheme}://{self.host}:{self.port}"

    def __enter__(self) -> SMTPSink:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False

    # -- reading what arrived ------------------------------------------------------------

    def only_message(self) -> Message:
        assert len(self.messages) == 1, f"{len(self.messages)} messages reached the MTA"
        return self.messages[0]

    def body(self) -> str:
        payload = self.only_message().get_payload(decode=True)
        assert isinstance(payload, bytes)
        return payload.decode("utf-8")

    def link(self) -> str:
        for word in self.body().split():
            if word.startswith("http"):
                return word.rstrip(".,")
        raise AssertionError(f"no link in the delivered mail:\n{self.body()}")

    def token(self) -> str:
        found = parse_qs(urlsplit(self.link()).query).get("token")
        assert found, f"the delivered link carries no token: {self.link()}"
        return found[0]


def _dead_port() -> int:
    """A port nothing is listening on: bound to learn the number, then closed."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# =======================================================================================
# A throwaway certificate for the two tests that need a handshake to succeed or fail
# =======================================================================================


def _generate_certificate(directory: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path] | None:
    """A self-signed cert for ``127.0.0.1``, or ``None`` if this box cannot make one.

    Generated rather than committed. A private key in the tree is a private key a scanner
    flags and a reader has to reason about, and there is nothing here worth keeping between
    runs. Two routes because neither is guaranteed: ``cryptography`` is importable in this
    virtualenv but is NOT in ``uv.lock``, so ``uv sync --frozen`` may remove it, and the
    ``openssl`` binary is present on a workstation but is not in every slim container.

    The SAN is an IP SAN. It has to be: ``smtplib`` passes the host it dialled as
    ``server_hostname``, so with ``check_hostname`` on — which is the whole point — a
    certificate carrying only a CN would be rejected by the client before any test could say
    anything about it.
    """
    certificate = directory / "sink-cert.pem"
    key = directory / "sink-key.pem"
    try:
        import datetime as _datetime
        import ipaddress as _ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        pass
    else:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        now = _datetime.datetime.now(_datetime.UTC)
        built = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(private.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _datetime.timedelta(minutes=5))
            .not_valid_after(now + _datetime.timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(_ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(private, hashes.SHA256())
        )
        certificate.write_bytes(built.public_bytes(serialization.Encoding.PEM))
        key.write_bytes(
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        return certificate, key

    openssl = shutil.which("openssl")
    if openssl is None:
        return None
    done = subprocess.run(  # noqa: S603 - a fixed argv, no shell, no caller input
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        capture_output=True,
        timeout=120,
    )
    return (certificate, key) if done.returncode == 0 else None


@pytest.fixture(scope="session")
def sink_certificate(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[pathlib.Path, ssl.SSLContext]:
    """``(pem_path, server_context)`` for a TLS-speaking sink, or a skip naming what is unproven."""
    made = _generate_certificate(tmp_path_factory.mktemp("smtp-sink-tls"))
    if made is None:
        pytest.skip(
            "neither the `cryptography` package nor an `openssl` binary is available to mint a "
            "throwaway certificate, so the two tests that need a real TLS handshake cannot run "
            "here. UNPROVEN on this box: that a certificate which does not verify stops the "
            "delivery, and that a trusted one carries the credential. Every other property in "
            "this module — including that an MTA which will not upgrade is refused rather than "
            "sent to in the clear — is still asserted."
        )
    certificate, key = made
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(key))
    return certificate, context


# =======================================================================================
# Fixtures. Named for this module; sibling test files own their own.
# =======================================================================================


@pytest.fixture
def smtp_clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing the runner's shell exports decides what any test here is configured with."""
    import os

    from buyer_svc.auth.routes import WORKER_COUNT_ENVS

    for name in list(os.environ):
        if name.startswith("PROXYSHOP_PG_DSN") or name in WORKER_COUNT_ENVS:
            monkeypatch.delenv(name, raising=False)
    for name in (*MAGIC_LINK_ENVS, "PROXYSHOP_BUYER_K_ANONYMITY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def smtp_process_state() -> Iterator[Any]:
    """Reset the two process-wide singletons around each test, whatever it does to them."""
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


def _configure(monkeypatch: pytest.MonkeyPatch, url: str, **extra: str) -> None:
    """The three variables every SMTP deployment sets, plus whatever the test is about."""
    monkeypatch.setenv(TRANSPORT_ENV, "smtp")
    monkeypatch.setenv(SMTP_URL_ENV, url)
    monkeypatch.setenv(SENDER_ENV, "Proxyshop <no-reply@proxyshop.example>")
    monkeypatch.setenv(BASE_URL_ENV, PAGE_ORIGIN)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def _client(routes_mod: Any) -> Any:
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    routes_mod.set_auth_service(routes_mod.build_auth_service())
    return TestClient(create_app(), raise_server_exceptions=False)


# =======================================================================================
# The headline: the whole login gesture, over a real socket, with no console bypass
# =======================================================================================


def test_the_whole_login_gesture_runs_over_a_real_smtp_conversation(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Request a link, an email is really delivered, redeem it, get a session.

    This is the deliverable. Nothing is monkeypatched: `smtplib` opens a socket, an SMTP
    conversation happens on it, a message arrives at a server that parses it, and the token
    that opens the session is scraped out of the message body that server received. The
    transport is the one `build_auth_service` chose from the environment, and it is neither
    the console (which prints credentials) nor a double (which proves nothing about a wire).

    `STARTTLS=disabled` is stated, because the sink is on this machine and speaks plain SMTP.
    That is the one setting a local proof needs, it is named rather than defaulted, and the
    test two below asserts that leaving it unnamed refuses to send at all.
    """
    with SMTPSink() as sink:
        _configure(monkeypatch, sink.url, **{STARTTLS_ENV: "disabled"})
        client = _client(smtp_process_state)

        requested = client.post("/buyer/auth/magic-link", json={"email": BUYER})
        assert requested.status_code == 202, requested.text

        assert sink.commands[:1] == ["EHLO"], f"no SMTP conversation happened: {sink.commands}"
        assert "DATA" in sink.commands, f"nothing was ever submitted: {sink.commands}"
        assert len(sink.messages) == 1, f"{len(sink.messages)} messages reached the MTA"
        assert sink.recipients[0][0].upper().startswith("RCPT TO:")
        assert BUYER in sink.recipients[0][0], sink.recipients[0]
        # MAIL FROM as well as RCPT TO. `smtplib` COMPUTES the envelope sender from the
        # `From:` header, so it is a value this service produces rather than one it sets, and
        # leaving it unasserted is how a sender that parses into a different mailbox ships.
        assert "<no-reply@proxyshop.example>" in sink.envelope_senders[0], (
            f"the envelope sender is not the configured mailbox: {sink.envelope_senders[0]!r}"
        )

        message = sink.only_message()
        assert message["To"] == BUYER
        assert "no-reply@proxyshop.example" in str(message["From"])
        assert message["Subject"] == "Your Proxyshop sign-in link"

        token = sink.token()
        assert sink.link().startswith(PAGE_ORIGIN), (
            f"the mailed link does not point at the configured front door: {sink.link()}"
        )
        assert token not in requested.text, (
            "the token is on the HTTP response as well as in the mail; the mailbox is no "
            "longer the credential"
        )

        session = client.post("/buyer/auth/session", json={"token": token})
        assert session.status_code == 201, (
            f"the token out of the delivered mail did not open a session "
            f"({session.status_code}): {session.text}"
        )
        assert session.json()["pseudonym"].startswith("psn-")

        # Single use, exactly as every other path: a mailed link is not a reusable credential.
        assert client.post("/buyer/auth/session", json={"token": token}).status_code == 401


def test_the_delivered_message_is_shaped_like_one_a_provider_will_accept(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headers a receiving MTA and a spam filter look for, read off the wire.

    `Date` and `Message-ID` were both ABSENT from the delivered message before this — measured
    against this sink, not inferred. `smtplib.send_message` adds neither (it reads
    `Resent-Date` and writes nothing), `Date` is mandatory under RFC 5322 §3.6, and a mail
    with no `Message-ID` cannot be found in the provider's delivery log when a buyer reports
    that no link arrived.
    """
    with SMTPSink() as sink:
        _configure(monkeypatch, sink.url, **{STARTTLS_ENV: "disabled"})
        client = _client(smtp_process_state)
        assert client.post("/buyer/auth/magic-link", json={"email": BUYER}).status_code == 202

        message = sink.only_message()
        assert message["Date"], "the delivered mail has no Date header; RFC 5322 requires one"
        assert message["Message-ID"], "the delivered mail has no Message-ID"
        assert message["Message-ID"].endswith("@proxyshop.example>"), (
            f"the Message-ID's domain is not the sender's, so it leaks or misstates the "
            f"sending host: {message['Message-ID']}"
        )
        assert message["Auto-Submitted"] == "auto-generated", (
            "nothing tells a vacation responder or a ticketing system not to reply, and an "
            "auto-reply to this mail quotes a live sign-in credential into a support queue"
        )
        assert not message.is_multipart(), (
            "the sign-in mail grew a multipart body; a single text/plain part is the least "
            "spam-scored shape and cannot show a link text that differs from its target"
        )
        assert message.get_content_type() == "text/plain", message.get_content_type()
        body = sink.body()
        assert "works once" in body and "stops working" in body, (
            f"the mail does not tell the buyer the link is single use and expiring, which is "
            f"the whole security of a credential they may forward:\n{body}"
        )


# =======================================================================================
# TLS: the transport used to be encrypted-if-convenient and verified never
# =======================================================================================


def test_an_mta_that_will_not_upgrade_to_tls_is_refused_rather_than_sent_to_in_the_clear(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression that matters most here, and it is not hypothetical.

    MEASURED on the previous code, against this sink: with no credentials configured the
    STARTTLS call was skipped entirely (it was guarded by `if username`), the sink recorded
    `DATA` on an unencrypted connection, and the route answered `202`. Every buyer's sign-in
    token went across the network readable by anything on the path, and nothing anywhere said
    so. The message is a bearer credential whether or not there is also a password to protect.

    Now the upgrade is unconditional and required, so an MTA that does not offer it ends the
    delivery. Both halves are asserted: the refusal on the wire, and — the half that is
    actually the security property — that the sink received NOTHING.
    """
    with SMTPSink(offer_starttls=False) as sink:
        _configure(monkeypatch, sink.url)  # no STARTTLS variable: the default must be enough
        client = _client(smtp_process_state)

        answered = client.post("/buyer/auth/magic-link", json={"email": BUYER})

        assert answered.status_code == 502, (
            f"an MTA that refuses to encrypt was answered {answered.status_code}; a 202 here "
            f"means the buyer was told a link is coming that this service either did not send "
            f"or sent in the clear"
        )
        assert sink.messages == [], (
            "the sign-in link was handed to an MTA that would not encrypt the session"
        )
        assert "STARTTLS" not in sink.commands or not any(sink.message_encrypted), sink.commands
        # No oracle on an unauthenticated route: the MTA's own words stay in the log.
        for fragment in ("dana", "reyes", "starttls", "127.0.0.1"):
            assert fragment not in answered.text.casefold(), answered.text


def test_a_certificate_that_does_not_verify_stops_the_delivery(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    sink_certificate: tuple[pathlib.Path, ssl.SSLContext],
) -> None:
    """TLS that does not check the certificate is TLS against nobody who matters.

    `smtplib` builds its own SSL context when handed none, and it builds it with
    `ssl._create_stdlib_context()` — which is `_create_unverified_context` under another name.
    MEASURED on this interpreter: `check_hostname=False`, `verify_mode=CERT_NONE`. So before
    this, anything able to answer for the MTA's address could present any certificate at all,
    take the SMTP password and read every sign-in token, and the delivery would have succeeded
    exactly as if nothing were wrong.

    The sink here presents a certificate no CA issued and the trust bundle is not configured,
    so the handshake must fail and the message must not be sent. Nothing else about this test
    differs from the one below, which is the same sink with the certificate trusted.
    """
    _, server_context = sink_certificate
    with SMTPSink(offer_starttls=True, tls_context=server_context) as sink:
        _configure(monkeypatch, sink.url)
        client = _client(smtp_process_state)

        answered = client.post("/buyer/auth/magic-link", json={"email": BUYER})

        assert answered.status_code == 502, answered.text
        assert sink.messages == [], (
            "a sign-in token was delivered over a TLS session whose certificate this service "
            "never verified; an active attacker on the path reads every one of them"
        )
        assert "STARTTLS" in sink.commands, f"the upgrade was never even attempted: {sink.commands}"


def test_a_trusted_certificate_carries_the_link_and_the_credential_inside_tls(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    sink_certificate: tuple[pathlib.Path, ssl.SSLContext],
) -> None:
    """The provider-shaped path: STARTTLS, a verified certificate, AUTH, then the message.

    The two assertions a recording double cannot make are the point of this one: the sink
    reports whether the socket was ENCRYPTED at the moment the credential arrived and at the
    moment the message did. A double's `login()` records a tuple and knows nothing about
    either.

    `PROXYSHOP_BUYER_MAGIC_LINK_SMTP_CA_BUNDLE` is what makes the self-signed certificate
    trusted, and it is also the operator-facing feature for an MTA a public CA did not issue
    for — an internal submission host. It is ADDED to the system trust store rather than
    replacing it, so a deployment that sets it does not silently stop verifying a public
    provider.
    """
    certificate, server_context = sink_certificate
    with SMTPSink(offer_starttls=True, tls_context=server_context) as sink:
        _configure(
            monkeypatch,
            sink.url,
            **{
                CA_BUNDLE_ENV: str(certificate),
                USERNAME_ENV: "apikey",
                PASSWORD_ENV: PROVIDER_PASSWORD,
            },
        )
        client = _client(smtp_process_state)

        requested = client.post("/buyer/auth/magic-link", json={"email": BUYER})
        assert requested.status_code == 202, requested.text

        assert sink.tls_negotiated, "the session was never upgraded"
        assert sink.credentials == ("apikey", PROVIDER_PASSWORD), (
            f"the MTA did not receive the configured credential: {sink.credentials!r}"
        )
        assert sink.credentials_encrypted is True, (
            "the SMTP credential was sent over a connection that was not encrypted"
        )
        assert sink.message_encrypted == [True], (
            "the message carrying the sign-in token was sent over a connection that was not "
            "encrypted"
        )
        session = client.post("/buyer/auth/session", json={"token": sink.token()})
        assert session.status_code == 201, session.text


def test_implicit_tls_really_negotiates_tls_before_it_says_anything(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    sink_certificate: tuple[pathlib.Path, ssl.SSLContext],
) -> None:
    """`smtps://` is the other submission shape (port 465), and it is verified too."""
    certificate, server_context = sink_certificate
    with SMTPSink(implicit_tls=True, tls_context=server_context) as sink:
        assert sink.url.startswith("smtps://"), sink.url
        _configure(monkeypatch, sink.url, **{CA_BUNDLE_ENV: str(certificate)})
        client = _client(smtp_process_state)

        assert client.post("/buyer/auth/magic-link", json={"email": BUYER}).status_code == 202
        assert sink.tls_negotiated and sink.message_encrypted == [True], sink.message_encrypted
        assert "STARTTLS" not in sink.commands, (
            f"an implicit-TLS session tried to upgrade an already-encrypted one: {sink.commands}"
        )


def test_implicit_tls_against_an_unverifiable_certificate_stops_the_delivery(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    sink_certificate: tuple[pathlib.Path, ssl.SSLContext],
) -> None:
    """The same regression as the STARTTLS one, on the `smtps://` path. Both were unverified."""
    _, server_context = sink_certificate
    with SMTPSink(implicit_tls=True, tls_context=server_context) as sink:
        _configure(monkeypatch, sink.url)  # no CA bundle: the certificate must not verify
        client = _client(smtp_process_state)

        answered = client.post("/buyer/auth/magic-link", json={"email": BUYER})
        assert answered.status_code == 502, answered.text
        assert sink.messages == [], "an smtps:// delivery accepted an unverifiable certificate"


# =======================================================================================
# Credentials: configuring one, and never publishing one
# =======================================================================================


def test_a_password_a_real_provider_would_issue_survives_being_configured(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both forms, checked against what the MTA actually received.

    `urlsplit` does not percent-decode userinfo, so before this a URL-configured password came
    out of the parser still encoded and the operator saw a `535` they could not explain. Worse,
    a raw `/` — which SES's base64 passwords contain routinely — re-parses the whole URL:
    MEASURED on `smtp://user:ab/cd@mail.example.net:587`, `hostname` came back `'user'`. The
    dedicated variables are the answer to that and the URL form is decoded.
    """
    from urllib.parse import quote

    with SMTPSink() as sink:
        _configure(
            monkeypatch,
            sink.url,
            **{
                STARTTLS_ENV: "disabled",
                USERNAME_ENV: "apikey",
                PASSWORD_ENV: PROVIDER_PASSWORD,
            },
        )
        client = _client(smtp_process_state)
        assert client.post("/buyer/auth/magic-link", json={"email": BUYER}).status_code == 202
        assert sink.credentials == ("apikey", PROVIDER_PASSWORD), sink.credentials

    with SMTPSink() as sink:
        userinfo = f"{quote('api@key', safe='')}:{quote(PROVIDER_PASSWORD, safe='')}"
        _configure(
            monkeypatch,
            f"smtp://{userinfo}@127.0.0.1:{sink.port}",
            **{STARTTLS_ENV: "disabled"},
        )
        monkeypatch.delenv(USERNAME_ENV)
        monkeypatch.delenv(PASSWORD_ENV)
        client = _client(smtp_process_state)
        assert client.post("/buyer/auth/magic-link", json={"email": BUYER}).status_code == 202
        assert sink.credentials == ("api@key", PROVIDER_PASSWORD), (
            f"a percent-encoded credential in the URL was not decoded: {sink.credentials!r}"
        )


def test_a_boot_failure_about_the_mta_url_never_prints_the_password_in_it(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule the fail-closed path already keeps for the token, for the other credential.

    The URL is the DOCUMENTED place to put a provider's password, and every misconfiguration
    message about it used to interpolate that URL with `{url!r}`. So a deployment that
    followed the documentation and mistyped the scheme, the port or the host printed its SMTP
    password into its own boot failure — from where it reaches a log aggregator, a CI artifact
    and a crash-looping container's journal, and where it is a standing right to send mail as
    this deployment rather than one buyer's fifteen-minute link.
    """
    from buyer_svc.auth.delivery import MagicLinkTransportMisconfigured

    secret = "SUPERSECRET-smtp-password"  # noqa: S105 - the value under test, not a real one
    broken = [
        f"http://apikey:{secret}@mail.example.net:587",  # a scheme this service does not speak
        f"smtp://apikey:{secret}@:587",  # no host
        f"smtp://apikey:{secret}@mail.example.net:not-a-port",  # unparseable port
    ]
    for url in broken:
        _configure(monkeypatch, url)
        with pytest.raises(MagicLinkTransportMisconfigured) as raised:
            smtp_process_state.build_auth_service()
        message = str(raised.value)
        assert secret not in message, (
            f"the boot failure for {url.replace(secret, '<secret>')!r} published the SMTP "
            f"password: {message.replace(secret, '<LEAKED>')}"
        )
        assert SMTP_URL_ENV in message, (
            f"the boot failure no longer names the variable that is wrong: {message}"
        )


def test_a_delivery_failure_never_prints_the_password_or_the_token(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A rejected credential is the one failure most likely to be pasted into a bug report."""
    import logging
    import secrets as secrets_mod

    token = "test-token-must-never-reach-a-log"
    monkeypatch.setattr(secrets_mod, "token_urlsafe", lambda _bytes: token)
    password = "SUPERSECRET-smtp-password"  # noqa: S105 - the value under test

    with SMTPSink(reject_credentials=True) as sink:
        _configure(
            monkeypatch,
            sink.url,
            **{
                STARTTLS_ENV: "disabled",
                USERNAME_ENV: "apikey",
                PASSWORD_ENV: password,
            },
        )
        client = _client(smtp_process_state)
        with caplog.at_level(logging.DEBUG):
            answered = client.post("/buyer/auth/magic-link", json={"email": BUYER})

    assert answered.status_code == 502, answered.text
    assert sink.messages == [], "the message was sent after the MTA refused the credential"

    # ONE readouterr, then both streams off the same result. Calling it twice DRAINS the
    # buffer on the first call, so the second returns empty and `.err` was permanently "" —
    # the stderr half of this assertion was dead code. stderr is exactly where a leak would
    # land if `smtplib`'s debuglevel were ever raised: `_print_debug` writes the base64 AUTH
    # blob there, and `caplog` does not see a raw stream.
    streams = capsys.readouterr()
    written = caplog.text + streams.out + streams.err + answered.text
    assert password not in written, "the SMTP password reached a log line or the response"
    assert token not in written, "the sign-in token reached a log line or the response"
    # The operator still gets something to act on, in the log and not on the wire.
    assert "535" in caplog.text, (
        f"the MTA's refusal was swallowed; an operator cannot tell a wrong password from an "
        f"unreachable host:\n{caplog.text}"
    )
    assert "535" not in answered.text, answered.text


def test_a_credential_is_refused_on_a_cleartext_session_to_another_host(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming `disabled` states a decision about this deployment's tokens, not about a password.

    A sink on this machine never puts the session on a network, and that is the case
    `disabled` exists for. Pointed at anything else it would hand the provider credential —
    the standing right to send mail as this deployment, which no single buyer's link is
    comparable to — to whoever is on the path. Refused at boot rather than at the first login,
    because a service that would do this on request is misconfigured whether or not anybody
    has asked it to yet.
    """
    from buyer_svc.auth.delivery import MagicLinkTransportMisconfigured

    _configure(
        monkeypatch,
        "smtp://mail.example.net:587",
        **{STARTTLS_ENV: "disabled", USERNAME_ENV: "apikey", PASSWORD_ENV: PROVIDER_PASSWORD},
    )
    with pytest.raises(MagicLinkTransportMisconfigured) as raised:
        smtp_process_state.build_auth_service()
    assert STARTTLS_ENV in str(raised.value), raised.value
    assert PROVIDER_PASSWORD not in str(raised.value), "the refusal printed the password"

    # The same credential to a sink on this machine is fine, and that asymmetry is the rule.
    with SMTPSink() as sink:
        _configure(
            monkeypatch,
            sink.url,
            **{
                STARTTLS_ENV: "disabled",
                USERNAME_ENV: "apikey",
                PASSWORD_ENV: PROVIDER_PASSWORD,
            },
        )
        client = _client(smtp_process_state)
        assert client.post("/buyer/auth/magic-link", json={"email": BUYER}).status_code == 202


def test_cleartext_smtp_to_another_host_announces_itself_at_boot(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same rule as the console transport: a stated weakening is said out loud, at start-up.

    Without a credential this is allowed — the operator named it — but it is still every
    sign-in token crossing a network in the clear, and an operator reading the service's
    start-up has to be able to see that is what they chose.
    """
    _configure(monkeypatch, "smtp://mail.example.net:587", **{STARTTLS_ENV: "disabled"})

    smtp_process_state.build_auth_service()

    announced = capsys.readouterr().out.casefold()
    assert announced.strip(), "an unencrypted transport to another host said nothing at all"
    for fragment in (STARTTLS_ENV.casefold(), "clear", "mail.example.net"):
        assert fragment in announced, f"the boot announcement never says {fragment!r}:\n{announced}"


def test_a_sink_on_this_machine_is_not_announced_as_a_network_leak(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The carve-out is narrow and it is real: 127.0.0.1 is not a network.

    A warning that fires on the one configuration it does not apply to is a warning operators
    learn to scroll past, which is how the announcement above stops working.
    """
    with SMTPSink() as sink:
        _configure(monkeypatch, sink.url, **{STARTTLS_ENV: "disabled"})
        smtp_process_state.build_auth_service()
    assert "UNENCRYPTED" not in capsys.readouterr().out


# =======================================================================================
# What being mailed costs: scanners, duplicates, and an MTA that says no
# =======================================================================================


def test_a_scanner_that_follows_the_mailed_link_does_not_spend_it(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mail security product fetches every URL in a message before the human sees it.

    That is the failure mode that kills naive magic links: the buyer opens their mail to find
    the link already used. It does not happen here, and the reason is structural — the mailed
    URL is the SPA's origin, redemption is `POST /buyer/auth/session`, and no GET anywhere in
    this service consumes a token. This test spends the scanner's requests against the service
    and then redeems the link, so the property is asserted rather than assumed; it is what
    makes "keep redemption off a GET" a rule somebody can break loudly rather than quietly.

    Two requests, because a scanner makes the first and a curious one makes the second: the
    mailed URL exactly as it appears in the message (whose path is the SPA's, not this
    service's), and the redemption path with the token hung off the query string, which is
    what a route added later without thinking would answer.

    `GET /buyer/auth/session` IS a route — it reads a session out of `X-Buyer-Session` — so
    the assertion is not about the status code it happens to return. It is that no GET
    carrying this token grants anything, and that the token still works afterwards.
    """
    with SMTPSink() as sink:
        _configure(monkeypatch, sink.url, **{STARTTLS_ENV: "disabled"})
        client = _client(smtp_process_state)
        assert client.post("/buyer/auth/magic-link", json={"email": BUYER}).status_code == 202
        token = sink.token()

        mailed = urlsplit(sink.link())
        for path in (f"{mailed.path or '/'}?{mailed.query}", f"/buyer/auth/session?token={token}"):
            scanned = client.get(path)
            assert scanned.status_code >= 400, (
                f"GET {path.split('?')[0]} carrying a sign-in token was answered "
                f"{scanned.status_code}: {scanned.text[:200]}"
            )
            assert "session_id" not in scanned.text, (
                f"a GET carrying the token handed back a session: {scanned.text[:200]}"
            )

        session = client.post("/buyer/auth/session", json={"token": token})
        assert session.status_code == 201, (
            f"the link stopped working after a scanner fetched it — a link scanner walks every "
            f"URL in a mail, so this burns the buyer's login before they open their inbox: "
            f"{session.text}"
        )


def test_the_same_link_arriving_twice_opens_exactly_one_session(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMTP is at-least-once and so is every provider on top of it.

    A duplicated message means the buyer has two copies of one link and may click both. The
    second is refused as already used — the same answer a replay gets, which is the right one
    — so nothing needs to deduplicate and a duplicate is not a second credential.
    """
    with SMTPSink() as sink:
        _configure(monkeypatch, sink.url, **{STARTTLS_ENV: "disabled"})
        client = _client(smtp_process_state)
        assert client.post("/buyer/auth/magic-link", json={"email": BUYER}).status_code == 202
        token = sink.token()

        first = client.post("/buyer/auth/session", json={"token": token})
        second = client.post("/buyer/auth/session", json={"token": token})
        assert first.status_code == 201, first.text
        assert second.status_code == 401, (
            f"the second copy of a duplicated mail opened a second session: {second.text}"
        )


def test_the_envelope_sender_is_the_mailbox_the_operator_configured(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MAIL FROM`, read off the wire — the half of the sender nothing was asserting.

    `smtplib.send_message` computes the envelope from the `From:` header with
    `email.utils.getaddresses`, and the shape of what it computes is what a receiving MTA
    accepts or rejects. The service's own check was `"@" in sender`, which is not how the
    library reads it, so a display name containing an unquoted comma — `Proxyshop, Inc.
    <no-reply@…>`, the one an operator actually writes — booted clean and sent
    `MAIL FROM:<Proxyshop>`. Asserted here against a real MTA rather than against the header.
    """
    with SMTPSink() as sink:
        _configure(monkeypatch, sink.url, **{STARTTLS_ENV: "disabled"})
        monkeypatch.setenv(SENDER_ENV, '"Proxyshop, Inc." <no-reply@proxyshop.example>')
        client = _client(smtp_process_state)
        assert client.post("/buyer/auth/magic-link", json={"email": BUYER}).status_code == 202

        assert len(sink.envelope_senders) == 1, sink.envelope_senders
        envelope = sink.envelope_senders[0]
        assert "<no-reply@proxyshop.example>" in envelope, (
            f"the envelope sender is not the configured mailbox, so a real MTA would refuse "
            f"this message: {envelope!r}"
        )
        assert sink.only_message()["Message-ID"].endswith("@proxyshop.example>"), (
            sink.only_message()["Message-ID"]
        )


def test_a_sender_that_would_produce_a_broken_envelope_is_refused_at_boot(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three senders that passed `"@" in sender` and could not send. All measured.

    * an unquoted comma in a display name parses as TWO addresses and `send_message` takes the
      first, so the envelope was `MAIL FROM:<Proxy>`;
    * `@` and `a@` have an `@` and no mailbox, so the envelope was `MAIL FROM:<>` — the null
      reverse-path RFC 5321 reserves for bounces, and mail claiming to be a bounce is filtered
      as one;
    * two comma-separated addresses, of which only the first was ever used.

    All three also fell through to a `Message-ID` ending `@localhost`, the untraceable spam
    signal that stamping the sender's domain exists to avoid.
    """
    from buyer_svc.auth.delivery import MagicLinkTransportMisconfigured

    for sender in (
        "Proxy, Shop <a@b.example>",
        "@",
        "a@",
        "no-reply@",
        "@x.example",
        "Proxyshop <no-reply@x.example>, other@y.example",
    ):
        _configure(monkeypatch, "smtp://mail.example.net:587")
        monkeypatch.setenv(SENDER_ENV, sender)
        with pytest.raises(MagicLinkTransportMisconfigured) as raised:
            smtp_process_state.build_auth_service()
        assert SENDER_ENV in str(raised.value), (
            f"the refusal of {sender!r} does not name the variable: {raised.value}"
        )

    # A correctly QUOTED display name with a comma is the fix, not a second refusal.
    _configure(monkeypatch, "smtp://mail.example.net:587")
    monkeypatch.setenv(SENDER_ENV, '"Proxyshop, Inc." <no-reply@proxyshop.example>')
    smtp_process_state.build_auth_service()


def test_a_non_ascii_credential_is_refused_at_boot_and_never_echoed(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMTP AUTH cannot carry one, and finding that out at login time leaked it.

    `smtplib.SMTP.auth` builds the AUTH PLAIN blob as `("\\0%s\\0%s" % (user, password))
    .encode('ascii')`. A non-ASCII byte therefore raises `UnicodeEncodeError` mid-login —
    which is a `ValueError`, NOT an `OSError`, so the delivery clause did not catch it: the
    route answered a bare 500, nothing an operator could act on was logged, and the exception
    object carried `'\\x00apikey\\x00pässwörd…'` — the entire credential — in its `object`
    attribute, where anything that reprs an exception would find it.

    No working deployment is refused by this: the protocol cannot carry those bytes at all.
    """
    from buyer_svc.auth.delivery import MagicLinkTransportMisconfigured

    secret = "pässwörd-SUPERSECRET-smtp"  # noqa: S105 - the value under test
    _configure(
        monkeypatch,
        "smtp://mail.example.net:587",
        **{USERNAME_ENV: "apikey", PASSWORD_ENV: secret},
    )
    with pytest.raises(MagicLinkTransportMisconfigured) as raised:
        smtp_process_state.build_auth_service()

    message = str(raised.value)
    assert PASSWORD_ENV in message, f"the refusal does not name the variable: {message}"
    assert secret not in message, "the refusal echoed the credential it was refusing"
    assert "SUPERSECRET" not in message, message


def test_implicit_tls_with_starttls_disabled_still_boots_with_a_credential(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    sink_certificate: tuple[pathlib.Path, ssl.SSLContext],
) -> None:
    """`smtps://` is encrypted from the first byte, so the cleartext refusal must not fire.

    An operator who sets `STARTTLS=disabled` once in their environment — reasonable alongside
    a local sink — and then points a provider at port 465 was refused at boot with a message
    that said "a session in the clear" about a session that is not, and refused it moments
    after `_starttls_policy` had logged that the setting has no effect on this scheme. The
    refusal now asks whether ANYTHING encrypts the session, not just whether STARTTLS does.

    The first half uses a REMOTE host and never sends anything, and that is the half that
    actually pins the fix: the cleartext-credential refusal is skipped entirely for a loopback
    MTA, so a version of this test that only drove the local sink passed with the defect fully
    reintroduced — measured, by reintroducing it.
    """
    _configure(
        monkeypatch,
        "smtps://smtp.provider.example:465",
        **{
            STARTTLS_ENV: "disabled",
            USERNAME_ENV: "apikey",
            PASSWORD_ENV: PROVIDER_PASSWORD,
        },
    )
    smtp_process_state.build_auth_service()  # must not raise: port 465 is TLS from byte one

    certificate, server_context = sink_certificate
    with SMTPSink(implicit_tls=True, tls_context=server_context) as sink:
        _configure(
            monkeypatch,
            sink.url,
            **{
                STARTTLS_ENV: "disabled",
                CA_BUNDLE_ENV: str(certificate),
                USERNAME_ENV: "apikey",
                PASSWORD_ENV: PROVIDER_PASSWORD,
            },
        )
        client = _client(smtp_process_state)

        assert client.post("/buyer/auth/magic-link", json={"email": BUYER}).status_code == 202
        assert sink.credentials_encrypted is True, (
            "the credential was sent over a connection that was not encrypted"
        )
        assert sink.message_encrypted == [True], sink.message_encrypted


def test_a_boot_failure_leaks_no_password_through_the_chained_traceback(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_safe_url` cleans the message; `__cause__` was carrying the rest.

    `urlsplit`'s ValueError stringifies as "Port could not be cast to integer value as
    'BJf7q'" — the text it failed on — and for the exact password shape this module documents
    (a raw `/` in a base64 provider password, in the URL as `.env.example` says it may be),
    that text IS a slice of the password: `smtp://apikey:BJf7q/rest@host` re-parses so the
    fragment before the first `/` becomes the "port". Chaining it put that slice in the
    `__cause__` traceback, which is what an uncaught boot failure writes to stderr.

    Asserted against the RENDERED TRACEBACK rather than `str(exc)`, which is the whole point:
    the message was already clean while the traceback was not, so a test that reads only the
    message cannot see this.
    """
    import traceback

    from buyer_svc.auth.delivery import MagicLinkTransportMisconfigured

    fragment = "ZZLEAKZZ"
    _configure(monkeypatch, f"smtp://apikey:{fragment}/rest+Ab==@mail.example.net:587")
    with pytest.raises(MagicLinkTransportMisconfigured) as raised:
        smtp_process_state.build_auth_service()

    rendered = "".join(
        traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__)
    )
    assert fragment not in rendered, (
        f"a slice of the SMTP password reached the boot failure's traceback:\n"
        f"{rendered.replace(fragment, '<LEAKED>')}"
    )
    assert raised.value.__cause__ is None, (
        f"the chained cause is back, and it carries the text it failed to parse: "
        f"{raised.value.__cause__!r}"
    )
    assert SMTP_URL_ENV in str(raised.value), raised.value


def test_an_mta_that_echoes_only_the_local_part_still_does_not_log_it(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`550 <dana.reyes> unknown user` is an ordinary refusal, and it names the mailbox.

    Redacting only the full address left this open: a log line naming the mailbox is the same
    disclosure whether or not the domain came with it.
    """
    import logging

    with SMTPSink(refuse_recipient=True, echo_local_part_only=True) as sink:
        _configure(monkeypatch, sink.url, **{STARTTLS_ENV: "disabled"})
        client = _client(smtp_process_state)
        with caplog.at_level(logging.DEBUG):
            answered = client.post("/buyer/auth/magic-link", json={"email": BUYER})

    assert answered.status_code == 502, answered.text
    assert sink.messages == []
    local_part = BUYER.split("@")[0]
    assert local_part not in caplog.text + answered.text, (
        f"the buyer's mailbox reached the log through the MTA's error text:\n{caplog.text}"
    )
    assert "550" in caplog.text, f"the refusal itself was thrown away:\n{caplog.text}"


def test_an_mta_that_refuses_the_message_is_a_clear_failure_and_not_a_202(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`550 sender not verified` is the first thing a new provider account says.

    Before this it escaped the handler and FastAPI answered a bare `500 Internal Server Error`
    — a traceback in the log, nothing in the response, and a buyer told the service is broken
    rather than that no link was sent. The distinct 502 exists so an operator can tell "this
    deployment has no transport" (503, a standing fact) from "the transport it has said no".
    """
    import logging

    with SMTPSink(refuse_data=True) as sink:
        _configure(monkeypatch, sink.url, **{STARTTLS_ENV: "disabled"})
        client = _client(smtp_process_state)
        with caplog.at_level(logging.ERROR):
            answered = client.post("/buyer/auth/magic-link", json={"email": BUYER})

    assert answered.status_code == 502, answered.text
    assert sink.messages == [], "the MTA refused the message and it was recorded as delivered"
    assert "550" in caplog.text, (
        f"the MTA's own refusal never reached the log, so an operator cannot fix it:\n{caplog.text}"
    )
    assert "Retry-After" in answered.headers, answered.headers


def test_an_mta_that_refuses_the_recipient_does_not_log_the_buyers_address(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The MTA's own words are the one place this route does not control what is said.

    Every other refusal here is worded to name no address at all — the rate-limit log line
    says "this address", the 503 names only variables — because a message on an
    unauthenticated route that says WHICH address it was about is an oracle, and a log line
    that says it is a mailing list somebody can export. `smtplib.SMTPRecipientsRefused`
    stringifies as `{'dana.reyes@example.com': (550, b'...')}`, so trusting the MTA's text
    would have quietly reintroduced exactly what the rest of the route avoids.

    The code and the reason survive: those are what an operator needs and they are not about
    any particular buyer.
    """
    import logging

    with SMTPSink(refuse_recipient=True) as sink:
        _configure(monkeypatch, sink.url, **{STARTTLS_ENV: "disabled"})
        client = _client(smtp_process_state)
        with caplog.at_level(logging.DEBUG):
            answered = client.post("/buyer/auth/magic-link", json={"email": BUYER})

    assert answered.status_code == 502, answered.text
    assert sink.messages == [], "the message was sent after every recipient was refused"

    written = caplog.text + answered.text
    assert BUYER not in written, (
        f"the buyer's email address reached a log line or the response through the MTA's "
        f"error text:\n{written}"
    )
    assert BUYER.split("@")[0] not in written, f"the local part leaked on its own:\n{written}"
    assert "550" in caplog.text, (
        f"redacting the address threw away the refusal an operator needs:\n{caplog.text}"
    )


def test_an_mta_that_is_not_listening_is_a_clear_failure(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commonest production failure of all: a wrong port, or an MTA that fell over."""
    _configure(monkeypatch, f"smtp://127.0.0.1:{_dead_port()}", **{STARTTLS_ENV: "disabled"})
    client = _client(smtp_process_state)

    answered = client.post("/buyer/auth/magic-link", json={"email": BUYER})

    assert answered.status_code == 502, (
        f"an unreachable MTA was answered {answered.status_code}; a 202 tells the buyer to "
        f"wait for a mail that was never handed to anything"
    )
    assert "Retry-After" in answered.headers


def test_a_misconfigured_transport_refuses_the_served_route_instead_of_500ing(
    smtp_clean_env: None,
    smtp_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Driven through the app, because the claim about this was inferred and was wrong.

    `delivery.py` said a half-configured transport meant "the process does not start". It does
    not: MEASURED here, `create_app()` returns, the ASGI lifespan completes, and the service
    serves. Nothing builds the login stack at start-up — `auth_service()` builds it lazily on
    first use — so the refusal lands on the first sign-in request, and it landed there as an
    unhandled exception: a bare 500 with a traceback and a buyer told the service is broken.

    The refusal is not softened by this test or by the fix behind it. No login is accepted,
    nothing is delivered, and the operator's message still reaches the log in full. What
    changes is that the caller gets the same 503 that "this deployment has no transport"
    already answers, because from outside the two are one fact.

    `raise_server_exceptions=False` is deliberate: with the default, an unhandled exception is
    re-raised into the test instead of being rendered, and this test would pass for the wrong
    reason by never seeing a status code at all.
    """
    import logging

    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    # An MTA named and no sender: the operator meant to mail and did not finish saying how.
    monkeypatch.setenv(TRANSPORT_ENV, "smtp")
    monkeypatch.setenv(SMTP_URL_ENV, "smtp://mail.example.net:587")
    monkeypatch.setenv(BASE_URL_ENV, PAGE_ORIGIN)

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        with caplog.at_level(logging.ERROR):
            answered = client.post("/buyer/auth/magic-link", json={"email": BUYER})

    assert answered.status_code == 503, (
        f"a deployment whose transport is half-configured answered {answered.status_code}; a "
        f"500 tells the buyer this service is broken and tells the operator nothing they can "
        f"act on without reading a traceback"
    )
    assert SENDER_ENV in caplog.text, (
        f"the log line does not name the variable that is wrong, which is the whole reason "
        f"the builder refuses instead of guessing:\n{caplog.text}"
    )
    # Still no oracle, and still nothing about the request on an unauthenticated route.
    for fragment in ("dana", "reyes", "mail.example.net"):
        assert fragment not in answered.text.casefold(), answered.text


def test_stating_smtp_still_boots_and_the_transport_is_not_the_console(
    smtp_clean_env: None, smtp_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing added here reaches stdout delivery, and nothing added here prints a token.

    The fail-closed design is the thing most easily broken by making the SMTP path pleasant,
    so it is asserted from inside the SMTP path: a fully configured, working `smtp` transport
    prints NOTHING that looks like a link, which is what separates it from the console.
    """
    import secrets as secrets_mod

    token = "test-token-must-never-reach-stdout"
    monkeypatch.setattr(secrets_mod, "token_urlsafe", lambda _bytes: token)

    with SMTPSink() as sink:
        _configure(monkeypatch, sink.url, **{STARTTLS_ENV: "disabled"})
        client = _client(smtp_process_state)
        import io
        import sys

        captured = io.StringIO()
        original = sys.stdout
        sys.stdout = captured
        try:
            answered = client.post("/buyer/auth/magic-link", json={"email": BUYER})
        finally:
            sys.stdout = original

    assert answered.status_code == 202, answered.text
    printed = captured.getvalue()
    assert token not in printed, (
        "a working SMTP transport printed the sign-in token to stdout; that is the console "
        "transport's behaviour and it is not supposed to be reachable from here"
    )
    assert "token=" not in printed, f"something link-shaped reached stdout: {printed!r}"
    assert sink.token() == token, "the mail did not carry the link after all"
