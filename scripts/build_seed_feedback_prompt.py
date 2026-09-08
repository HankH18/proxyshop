#!/usr/bin/env python
"""Generate the buyer journey's seeded post-purchase feedback prompt.

WHY THIS EXISTS. ``apps/buyer/app/feedback/`` is built, tested, and mounted by nothing. The
gap is not the mount: R14's prompt is asked days after delivery and needs an ``order_ref``,
and the served journey ends at the checkout handoff without ever obtaining one. So there is
no real prompt for the app to show, and an empty panel would teach a demo audience nothing.

This script produces one manufactured prompt, and the OUTPUT is the checked-in artifact --
the same collect-once-replay-forever discipline as ``services/sim/seed-data/`` and
``fixtures/real-catalogs/``. The app renders these bytes through the real
``FeedbackPromptView``; it does not re-run anything, and it does not carry a hardcoded
screenshot.

WHAT IT WRITES, into ``apps/buyer/app/feedback/seed-data/``:

* ``prompt.json``      -- one ``FeedbackPromptData``, exactly the shape
                          ``apps/buyer/app/feedback/feedback.ts`` declares
* ``collection.json``  -- provenance: what produced it, from which constants, with a sha256
                          for every other file and the marker a reader needs

THE MARKER, and why it is not a flag. ``order_ref`` begins with
``seed.shoppers.SIMULATED_ORDER_PREFIX`` (``sim-fb-``). That prefix is not decoration:

* ``apps/buyer/svc/src/feedback/submission.py`` copies ``order_ref`` VERBATIM onto the sealed
  ledger event, beside ``event_hash`` and ``prev_hash``;
* so it is inside the trust ledger's hash chain, and an answer given from the seeded panel
  stays marked as manufactured for as long as the chain exists;
* a seeded observation cannot be un-marked, and an earned one cannot be marked, without
  breaking ``GET /events/verify``.

A payload flag would have been the obvious alternative and is worse in the way that matters:
the buyer service builds the payload from the caller's body, so a marker there would be a
claim any caller could make about somebody else's order. ``order_ref`` is a value the caller
already owns and the route already copies, so **the served route needs no notion of a
simulator at all** -- which is the whole of the live switch. See ``services/sim/seed/
provenance.py``, which makes the same argument at length and is this artifact's reader.

GOING LIVE. Server side: nothing. Client side: when the journey can obtain a real
``order_ref``, pass it to ``FeedbackPromptView`` instead of ``SEEDED_PROMPT`` and delete this
directory. ``apps/buyer/app/journey/seeded-feedback.ts`` is the only importer.

Usage::

    ./.venv/bin/python scripts/build_seed_feedback_prompt.py
    ./.venv/bin/python scripts/build_seed_feedback_prompt.py --check   # regenerate + diff
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / ".pkgroot"))

from buyer_svc.feedback.prompt import (  # noqa: E402 - after the sys.path bootstrap
    FEEDBACK_CHOICES,
    PROMPT_INPUT_TYPE,
    PROMPT_QUESTION,
    PROMPT_QUESTION_ID,
)
from seed.shoppers import SIMULATED_ORDER_PREFIX  # noqa: E402 - after the sys.path bootstrap

OUT = REPO_ROOT / "apps" / "buyer" / "app" / "feedback" / "seed-data"

PROMPT_FILE = "prompt.json"
COLLECTION_FILE = "collection.json"

#: Bumped when the on-disk layout changes in a way a reader must notice. The TypeScript reader
#: checks only the marker, so this is for a human and for the test.
ARTIFACT_VERSION = "1.0.0"
ARTIFACT_KIND = "seeded-buyer-feedback-prompt"

#: The store the manufactured order was placed with, and the auction it came from. Both name
#: real rows in ``deploy/demo/``: ``paradiseherbs.com`` is one of the four hosted store agents
#: and ``cluster-liver-support`` is the demo's only intent cluster. A seeded prompt pointing at
#: a store the demo does not run would be a second kind of fiction on top of the first.
SEED_STORE_ID = "paradiseherbs.com"
SEED_AUCTION_ID = "sim-fb-auc-liver-support-0001"

#: The episode and shopper coordinates the reference is built from, in the shape
#: ``seed.population`` mints (``f"{PREFIX}{episode:02d}-{shopper:02d}-{store_id}"``). Constants
#: rather than a counter: this artifact is one prompt, and a value that moved every run would
#: make ``--check`` report drift on a generator whose inputs had not changed.
SEED_EPISODE = 1
SEED_SHOPPER = 1


class SeedRefusal(RuntimeError):
    """The prompt this script was about to write is not safely marked as manufactured."""


def canonical_bytes(value: Any) -> bytes:
    """One JSON value in the canonical form two runs are compared on.

    The same helper ``seed.store.canonical_bytes`` uses -- sorted keys, no whitespace -- so a
    digest computed here and a digest computed there mean the same thing.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def order_ref() -> str:
    """The manufactured order reference, built from the prefix the simulator itself mints.

    Composed from :data:`seed.shoppers.SIMULATED_ORDER_PREFIX` rather than spelled out, so this
    writer and ``seed.provenance.is_seeded_event`` -- the reader -- cannot drift apart. If the
    prefix ever moves, this artifact moves with it and ``--check`` says so.
    """
    return f"{SIMULATED_ORDER_PREFIX}{SEED_EPISODE:02d}-{SEED_SHOPPER:02d}-{SEED_STORE_ID}"


def build_prompt() -> dict[str, Any]:
    """One ``FeedbackPromptData``, in the field order ``feedback.ts`` declares it.

    The question, the input type and all five options are READ OUT OF the buyer service's own
    ``prompt.py`` rather than retyped. A seeded prompt that asked a question the service does
    not publish, or offered a choice ``CHOICE_IDS`` would reject, would be a demo of something
    that cannot happen -- and would fail on submit, which is the one moment it is meant to work.
    """
    return {
        "order_ref": order_ref(),
        "store_id": SEED_STORE_ID,
        "auction_id": SEED_AUCTION_ID,
        "question_id": PROMPT_QUESTION_ID,
        "question": PROMPT_QUESTION,
        "input_type": PROMPT_INPUT_TYPE,
        "options": [choice.to_dict() for choice in FEEDBACK_CHOICES],
    }


def _refuse_unless_marked(prompt: dict[str, Any]) -> None:
    """Refuse to write a prompt that is not marked as manufactured.

    The same refusal ``seed.population._Services.submit`` makes before the first byte leaves,
    and for the same reason: this prompt's answers reach an append-only ledger with no delete.
    It is labelled or it is not written.
    """
    reference = prompt.get("order_ref")
    if not isinstance(reference, str) or not reference.startswith(SIMULATED_ORDER_PREFIX):
        raise SeedRefusal(
            f"refusing to write a seeded prompt for {reference!r}: it does not carry the "
            f"{SIMULATED_ORDER_PREFIX!r} prefix. An answer given from this panel reaches an "
            "append-only ledger with no delete; it is labelled or it is not written."
        )


def _source() -> dict[str, str]:
    """Best-effort git provenance. Never invented -- an unavailable git records ``""``."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "", "dirty": ""}
    return {"commit": commit, "dirty": str(dirty).lower()}


def build_collection(prompt_bytes: bytes) -> dict[str, Any]:
    """The provenance record. Written last, because it digests everything else.

    Deliberately carries NO ``captured_at``: a wall clock in a digested output makes ``--check``
    report drift on every run of a generator whose inputs have not changed, and the thing worth
    recording about this artifact is what produced it, not when. ``source.commit`` is the
    reproducible answer to "which tree was this".
    """
    return {
        "artifact_version": ARTIFACT_VERSION,
        "kind": ARTIFACT_KIND,
        # Redundant with `marker` below, stated anyway, at the top, in one word: the first
        # thing anyone opening this file must learn is that the order in it is not a purchase.
        "simulated": True,
        "producer": "python scripts/build_seed_feedback_prompt.py",
        "producer_module": "scripts.build_seed_feedback_prompt",
        "marker": {
            "field": "order_ref",
            "prefix": SIMULATED_ORDER_PREFIX,
            "reader": "apps/buyer/app/journey/seeded-feedback.ts::readSeededPrompt",
            "audit": "seed.provenance.is_seeded_event",
            "why": (
                "apps/buyer/svc/src/feedback/submission.py copies order_ref verbatim onto the "
                "sealed ledger event, so the marker is inside the trust chain's hashes: a "
                "manufactured observation cannot be un-marked and an earned one cannot be "
                "marked without breaking GET /events/verify. A payload flag would instead be a "
                "claim any caller could make about somebody else's order."
            ),
            # Said plainly, because the strong version of this claim would be false. The
            # marker guarantees what happens to an answer ONCE SUBMITTED; it does not mean
            # this reference is already on a chain somewhere. It is not: no purchase was
            # simulated for it. `services/sim/seed-data/feedback-population/ledger.jsonl`
            # holds references that ARE on a chain, but they belong to that corpus's five
            # simulated coffee stores, and a prompt asking this demo's shopper about a coffee
            # order they never placed would trade one honest fiction for a confusing one.
            "on_chain_today": False,
            "on_chain_note": (
                "This reference names no existing ledger event: the order was manufactured for "
                "the panel, not simulated through a purchase. What the marker guarantees is "
                "that IF the seeded panel is answered, the event the real POST /buyer/feedback "
                "seals carries this reference verbatim and is therefore permanently "
                "separable from earned feedback. For references that are already on a chain, "
                "see services/sim/seed-data/feedback-population/ledger.jsonl."
            ),
        },
        "source": _source(),
        "determinism": {
            "reproduce": "python scripts/build_seed_feedback_prompt.py",
            "reproducible_files": [PROMPT_FILE],
            "volatile_files": [COLLECTION_FILE],
            "scope": (
                "byte-identical across processes and machines at the same source revision. The "
                "question, the input type and all five options are read out of "
                "buyer_svc.feedback.prompt, so a change to the served prompt legitimately moves "
                "these bytes and is meant to; --check is what reports it."
            ),
        },
        "reads": {
            "question_and_options": "buyer_svc.feedback.prompt.FEEDBACK_CHOICES",
            "marker_prefix": "seed.shoppers.SIMULATED_ORDER_PREFIX",
        },
        "files": {"prompt": PROMPT_FILE},
        "file_sha256": {"prompt": hashlib.sha256(prompt_bytes).hexdigest()},
        "bytes": {"prompt": len(prompt_bytes)},
        "notes": [
            "EVERY FIELD IN prompt.json WAS MANUFACTURED. No person answered anything.",
            "The question and the five options are the real ones the buyer service publishes.",
            "Answering the seeded panel posts to the real POST /buyer/feedback.",
            "Going live: obtain a real order_ref in the journey and delete this directory.",
        ],
    }


def build() -> dict[str, bytes]:
    """``{relative filename: exact bytes}`` for every file this script owns."""
    prompt = build_prompt()
    _refuse_unless_marked(prompt)
    prompt_bytes = json.dumps(prompt, indent=2, ensure_ascii=False) + "\n"
    encoded = prompt_bytes.encode("utf-8")
    collection = build_collection(encoded)
    collection_bytes = json.dumps(collection, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return {PROMPT_FILE: encoded, COLLECTION_FILE: collection_bytes.encode("utf-8")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate in memory and report drift instead of writing",
    )
    args = parser.parse_args()

    try:
        documents = build()
    except SeedRefusal as exc:
        print(f"the artifact was NOT written: {exc}", file=sys.stderr)
        return 2

    if args.check:
        return _check(documents)

    for name, body in documents.items():
        path = OUT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        print(f"wrote {path.relative_to(REPO_ROOT)} ({len(body):,} bytes)")
    return 0


def _check(documents: dict[str, bytes]) -> int:
    """Report drift without writing. Two different questions, because they have two answers.

    ``prompt.json`` is byte-compared against what the served constants imply: it is the
    reproducible file, and a difference means the artifact and the buyer service disagree
    about what the prompt says.

    ``collection.json`` is NOT byte-compared, and that is deliberate rather than lax. It
    records ``source.commit``, which moves whenever anything in the tree is committed — so
    byte-comparing it would report drift on a generator whose inputs had not changed, which
    is the failure mode that teaches a reader to ignore a check. What is verified instead is
    the thing that actually matters: that the digest it records for ``prompt.json`` matches
    the bytes on disk. A MISSING digest is as fatal as a wrong one, because otherwise
    deleting the digest is how an edit gets laundered past this check.
    """
    problems: list[str] = []

    prompt_path = OUT / PROMPT_FILE
    on_disk = prompt_path.read_bytes() if prompt_path.is_file() else b""
    if on_disk != documents[PROMPT_FILE]:
        problems.append(
            f"{prompt_path.relative_to(REPO_ROOT)} differs from what the constants imply"
        )

    record_path = OUT / COLLECTION_FILE
    if not record_path.is_file():
        problems.append(
            f"{record_path.relative_to(REPO_ROOT)} is missing, so nothing can be checked"
        )
    else:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        recorded = str(record.get("file_sha256", {}).get("prompt") or "")
        actual = hashlib.sha256(on_disk).hexdigest()
        if not recorded:
            problems.append(
                f"{record_path.relative_to(REPO_ROOT)} records no digest for {PROMPT_FILE}, "
                "so nothing about those bytes can be checked"
            )
        elif recorded != actual:
            problems.append(
                f"{prompt_path.relative_to(REPO_ROOT)} does not match the digest "
                f"{COLLECTION_FILE} records for it (recorded {recorded}, found {actual}). An "
                "artifact whose bytes and whose provenance disagree is worse than none: it "
                "looks accounted for."
            )

    if problems:
        print("the seeded prompt is not in the state its provenance claims:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("apps/buyer/app/feedback/seed-data is in sync with the served prompt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
