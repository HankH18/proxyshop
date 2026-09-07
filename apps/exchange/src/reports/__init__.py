"""Merchant-facing reports built from what the exchange already knows (R9).

One report so far: :func:`build_loss_report`, which turns an auction log into per-store,
per-cluster loss counts. It is a privacy boundary as much as a feature — the rows it reads
carry the winning price and the rival's identity, and the report carries neither. See
:mod:`apps.exchange.src.reports.loss` for how that is enforced by projection rather than by a
list of forbidden field names.

The three pieces, and why all three had to exist before any of them was worth anything:

:mod:`.loss`      the aggregation and the privacy boundary. It was here first, alone.
:mod:`.log`       what WRITES the auction log ``build_loss_report`` reads. Until it existed,
                  that function's only input in the repository was a test fixture.
:mod:`.routes`    the served door. Until it existed, ``exchange.main``'s frozen
                  ``glob("*/routes.py")`` automount could not reach this package at all.
"""

from __future__ import annotations

from .log import (
    COMPONENT_CATEGORIES,
    EXCLUSION_CATEGORIES,
    LossLog,
    loss_rows,
    record_losses,
)
from .loss import (
    PROJECTED_FIELDS,
    REASON_CATEGORIES,
    LossReportLeak,
    build_loss_report,
)

__all__ = [
    "COMPONENT_CATEGORIES",
    "EXCLUSION_CATEGORIES",
    "PROJECTED_FIELDS",
    "REASON_CATEGORIES",
    "LossLog",
    "LossReportLeak",
    "build_loss_report",
    "loss_rows",
    "record_losses",
]
