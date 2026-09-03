"""Merchant-facing reports built from what the exchange already knows (R9).

One report so far: :func:`build_loss_report`, which turns an auction log into per-store,
per-cluster loss counts. It is a privacy boundary as much as a feature — the rows it reads
carry the winning price and the rival's identity, and the report carries neither. See
:mod:`apps.exchange.src.reports.loss` for how that is enforced by projection rather than by a
list of forbidden field names.
"""

from __future__ import annotations

from .loss import (
    PROJECTED_FIELDS,
    REASON_CATEGORIES,
    LossReportLeak,
    build_loss_report,
)

__all__ = [
    "PROJECTED_FIELDS",
    "REASON_CATEGORIES",
    "LossReportLeak",
    "build_loss_report",
]
