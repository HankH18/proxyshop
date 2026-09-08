"""What a deployment has to state before the merchant dashboard can show anything.

Every value here is read from the environment with **no default and no fallback**, which is
the same posture ``merchant_svc.install.routes`` takes for the admin token and for the same
reason: a dashboard that invented an exchange address would render somebody else's numbers,
and one that invented an empty report would tell a merchant they lost nothing.

An empty string is exactly an unset variable. Every reader is
``str(environ.get(...) or "").strip()``, so a compose fragment carrying
``EXCHANGE_URL: "${EXCHANGE_URL:-}"`` — which is how three of these are already spelled in
this repo — takes the unconfigured branch rather than trying to open ``http://``.

The names are deliberately the ones the stack already uses. ``EXCHANGE_URL`` and
``TRUST_URL`` are read by ``buyer_svc.composition`` and ``merchant_svc.composition``
respectively; adding a second spelling of an address an operator has already stated is how a
deployment ends up half-configured with everything looking set.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "EXCHANGE_URL_ENV",
    "REPORT_TOKENS_ENV",
    "REPORT_TOKENS_JSON_ENV",
    "STORE_AGENT_URL_ENV",
    "TRUST_URL_ENV",
    "UI_DIST_ENV",
    "DashboardConfig",
    "ReportTokensUnreadable",
    "dashboard_config",
    "ui_dist",
]

#: Where ``npm --workspace @proxyshop/merchant run build:ui`` left the SPA. Absent means the
#: bundle was never built, which the mount renders as a page saying exactly that.
UI_DIST_ENV: Final[str] = "MERCHANT_UI_DIST"

#: The exchange's origin. R9's loss report lives at ``GET /reports/losses`` on it.
EXCHANGE_URL_ENV: Final[str] = "EXCHANGE_URL"

#: Path to a JSON document holding ``{store_id: token}`` — the bearer the exchange resolves
#: THIS store from. A file, because it is a secret and the deployment document is a plain JSON
#: file that gets pasted around; the same rule ``exchange.composition`` applies to its own
#: ``report_tokens_file``.
REPORT_TOKENS_ENV: Final[str] = "MERCHANT_REPORT_TOKENS"

#: The same table inline, for a container that would rather set a variable than mount a file.
#: ``MERCHANT_REPORT_TOKENS`` wins if both are set.
REPORT_TOKENS_JSON_ENV: Final[str] = "MERCHANT_REPORT_TOKENS_JSON"

#: The trust service's origin: ``GET /snapshot`` and ``GET /events``.
TRUST_URL_ENV: Final[str] = "TRUST_URL"

#: This store's own hosted agent — the door ``POST /v1/bid-requests`` is served on. It is the
#: agent the merchant is paying for, not a shared address: one agent process advocates for one
#: store (``store_agent.solicitation.advocate``), so a deployment serving several merchants
#: from one dashboard needs one of these per process, not one for all of them.
STORE_AGENT_URL_ENV: Final[str] = "STORE_AGENT_URL"


class ReportTokensUnreadable(RuntimeError):
    """The report-token table was NAMED and could not be read.

    Distinct from "no table was configured", and loudly so. A typo'd path that silently became
    "this merchant has no losses" is the failure this whole module is shaped against.
    """


@dataclass(frozen=True)
class DashboardConfig:
    """The addresses and the one secret the dashboard's reads need.

    Every field is a stated value or an empty string. Nothing here is optional-with-a-default,
    so "is this configured" is answered by asking the value, never by comparing it to a
    default somebody might also have typed.
    """

    exchange_url: str = ""
    report_tokens: Mapping[str, str] = ()  # type: ignore[assignment]
    trust_url: str = ""
    store_agent_url: str = ""

    def report_token_for(self, store_id: str) -> str:
        """This store's exchange bearer, or ``""`` when the table names no such store."""
        return str(dict(self.report_tokens).get(store_id, "") or "").strip()

    def losses_missing(self, store_id: str) -> tuple[str, ...]:
        """Which variables have to be set before a loss report can be fetched."""
        missing: list[str] = []
        if not self.exchange_url:
            missing.append(EXCHANGE_URL_ENV)
        if not self.report_token_for(store_id):
            missing.append(REPORT_TOKENS_ENV)
        return tuple(missing)

    def trust_missing(self) -> tuple[str, ...]:
        return () if self.trust_url else (TRUST_URL_ENV,)

    def store_agent_missing(self) -> tuple[str, ...]:
        return () if self.store_agent_url else (STORE_AGENT_URL_ENV,)


def _stated(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name) or "").strip()


def _report_tokens(env: Mapping[str, str]) -> dict[str, str]:
    """``{store_id: token}`` from the file, else from the inline document, else empty.

    A named-but-unreadable path raises rather than degrading to an empty table: an operator
    who stated where the secrets are and got a silent empty dashboard has been told the
    opposite of what happened.
    """
    path = _stated(env, REPORT_TOKENS_ENV)
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ReportTokensUnreadable(
                f"{REPORT_TOKENS_ENV}={path!r} cannot be read: {exc}"
            ) from exc
        return _table(raw, source=f"{REPORT_TOKENS_ENV}={path!r}")
    inline = _stated(env, REPORT_TOKENS_JSON_ENV)
    if inline:
        return _table(inline, source=REPORT_TOKENS_JSON_ENV)
    return {}


def _table(raw: str, *, source: str) -> dict[str, str]:
    try:
        loaded: Any = json.loads(raw)
    except ValueError as exc:
        raise ReportTokensUnreadable(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise ReportTokensUnreadable(
            f"{source} must hold a JSON object of {{store_id: token}}, got {type(loaded).__name__}"
        )
    # Empty keys and empty tokens are dropped rather than stored, so no store can ever be
    # resolved by presenting nothing — the rule `exchange.reports.routes.configure_reports`
    # applies to the far end of this same table.
    return {str(k): str(v) for k, v in loaded.items() if str(k).strip() and str(v).strip()}


def dashboard_config(env: Mapping[str, str] | None = None) -> DashboardConfig:
    """Resolve the dashboard's collaborators from the environment.

    Read per request rather than cached at import: the tests, the compose stack and an
    operator all change these, and a config frozen at import time is one an operator cannot
    fix without a restart they have no reason to know they need.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    return DashboardConfig(
        exchange_url=_stated(environ, EXCHANGE_URL_ENV),
        report_tokens=_report_tokens(environ),
        trust_url=_stated(environ, TRUST_URL_ENV),
        store_agent_url=_stated(environ, STORE_AGENT_URL_ENV),
    )


def ui_dist(env: Mapping[str, str] | None = None) -> Path | None:
    """The built SPA's directory, or ``None`` when it was never built or never stated.

    ``is_dir()`` is checked HERE rather than left to ``StaticFiles``, which raises at
    construction time — inside a request, on a mount that is resolved lazily, that would be a
    500 instead of the page that names the build command.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    stated = _stated(environ, UI_DIST_ENV)
    if not stated:
        return None
    candidate = Path(stated)
    return candidate if candidate.is_dir() else None
