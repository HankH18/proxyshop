"""R9's merchant dashboard — the page, and the reads that fill it.

R9 names five things a merchant must be able to see and do: every bid with its rationale;
win/loss aggregated by intent cluster with reason categories only and no rival amounts; the
trust score with its per-dimension breakdown and the event payloads behind it; a kill switch;
and versioned envelope editing.

Four of those five had a finished producer and no reader when this package was written, and
the fifth — the per-auction rationale — has a producer with no door. :mod:`.journal` states
that plainly rather than papering over it, and the page says so where a merchant will read it.

Layout, and why each piece is separate:

* :mod:`.config` — what a deployment must state. Every value is read from the environment with
  no default, so "unconfigured" is a fact this package can report rather than a shape it has
  to guess at.
* :mod:`.upstream` — the reads of the exchange, the trust service and the store's own agent,
  and the vocabulary their failures are reported in. The rival-narrowing lives here, on the
  server, because narrowing in a browser means the rival's rows were already served to it.
* :mod:`.journal` — what this store's agent answered when the merchant solicited it. Bounded,
  process-local, and explicitly not the store agent's own shadow log.
* :mod:`.routes` — the served surface: a `Mount` for the built bundle and two published
  operations.
"""

from __future__ import annotations

from .config import DashboardConfig, dashboard_config, ui_dist
from .journal import SOLICITATIONS, SolicitationJournal, SolicitationRecord
from .upstream import Panel

__all__ = [
    "SOLICITATIONS",
    "DashboardConfig",
    "Panel",
    "SolicitationJournal",
    "SolicitationRecord",
    "dashboard_config",
    "ui_dist",
]
