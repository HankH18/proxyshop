"""``python -m merchant_svc.onboarding`` — turn a recorded interview into an envelope.

The onboarding interview happens in the merchant dashboard (T-054), but the transcript it
records is an ordinary document, and turning one into an envelope is an ordinary offline
operation: an operator re-running an onboarding after a data loss, a support engineer
reproducing what a merchant's answers actually produced, a reviewer diffing the envelope a
transcript yields against the one on file.

Nothing here reads the clock, the network or a database. Given the same transcript it prints
the same bytes::

    python -m merchant_svc.onboarding fixtures/interviews/northwind-outfitters.json
    python -m merchant_svc.onboarding <transcript> --approval approval.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from merchant_svc.envelope.model import EnvelopeError
from merchant_svc.onboarding.flow import (
    activate,
    approval_artifact_template,
    envelope_from_transcript,
)
from merchant_svc.onboarding.interview import TranscriptRejected

#: The keys a transcript document may hide the interview under, matching the fixture
#: convention in ``fixtures/interviews/``.
_TRANSCRIPT_KEYS = ("transcript", "dialogue", "messages", "turns")


def _transcript_from(document: Any) -> Any:
    """The interview inside ``document`` — either the whole file, or its transcript key."""
    if isinstance(document, dict):
        for key in _TRANSCRIPT_KEYS:
            if document.get(key):
                return document[key]
    return document


def main(argv: list[str] | None = None) -> int:
    """Build (and optionally activate) an envelope from a transcript file. Returns an exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m merchant_svc.onboarding",
        description="Turn a recorded plain-language onboarding interview into an envelope.",
    )
    parser.add_argument("transcript", type=pathlib.Path, help="the interview transcript, JSON")
    parser.add_argument(
        "--approval",
        type=pathlib.Path,
        default=None,
        help="a written approval artifact; with it the envelope is activated, without it the "
        "envelope stays in shadow and the artifact the merchant must sign is printed instead",
    )
    parser.add_argument(
        "--approver",
        default="<the merchant, by name>",
        help="who the printed approval template is addressed to; ignored with --approval",
    )
    parser.add_argument(
        "--approved-at",
        default="<the instant they signed, ISO-8601 with a timezone>",
        help="the instant the printed approval template records; ignored with --approval",
    )
    args = parser.parse_args(argv)

    try:
        document = json.loads(args.transcript.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {args.transcript}: {exc}", file=sys.stderr)
        return 2

    try:
        envelope = envelope_from_transcript(_transcript_from(document))
        if args.approval is not None:
            envelope = activate(envelope, json.loads(args.approval.read_text()))
        else:
            template = approval_artifact_template(envelope, args.approver, args.approved_at)
            print(
                "# shadow. To activate, record this written approval and re-run with "
                f"--approval:\n# {json.dumps(template, sort_keys=True)}",
                file=sys.stderr,
            )
    except (TranscriptRejected, EnvelopeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {args.approval}: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(envelope.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
