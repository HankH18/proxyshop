"""Run it once, store it, replay it forever — the seed artifact and its provenance.

The owner's ruling is that the seeding population is *a script we run to create the data, and
then we just store it*, not a live-hosted service. This module is the "store it" half: it
writes what one seeded run produced to a directory, with enough provenance beside it that a
reader years later can say what was generated, when, by what, from which seed, and how to
check that the bytes have not moved.

The shape is not invented here
------------------------------
The repository already does collect-once-replay-forever twice, and this follows the older of
the two rather than adding a third convention. ``fixtures/real-catalogs/collection.json``
records, for a corpus collected by hand from the public internet: a corpus version, the
instant of collection, the collector that produced it, the policy it collected under, the
per-file ``sha256``, the raw and on-disk byte counts, and a ``notes`` list stating in plain
words what the data is and is not. Every one of those has an equivalent below, plus two this
corpus needs and that one does not:

* ``marker`` — how a reader tells a manufactured observation from an earned one. See
  :mod:`seed.provenance`; this block is what points a stranger at the right field.
* ``as_of`` — the instant the stored postures were decayed against. Without it the artifact
  is not replayable, only readable: ``GET /snapshot`` decays against the moment it is asked,
  so a replay at any other instant produces different numbers and can prove nothing about
  the chain it was given.

What is written
---------------
=====================  =====================================================================
file                   what it holds
=====================  =====================================================================
``collection.json``    the provenance record below. Not byte-stable: it carries the capture
                       instant and the digests of the volatile files.
``transcript.json``    :func:`seed.population.transcript` in canonical form. **This is the
                       byte-stable one** — the same seed, on any machine, in any process,
                       produces these exact bytes, and ``determinism.transcript_digest`` is
                       the value to compare.
``ledger.jsonl``       the trust chain the served routes actually wrote, one canonical JSON
                       event per line, in insertion order. NOT byte-stable, and the reason is
                       stated rather than hidden: every event carries a ``event_id`` minted
                       from :mod:`uuid` and a ``ts`` read from a wall clock, exactly the
                       values :func:`seed.population.transcript` excludes by name.
``roster.json``        ``{store_id, business_identity}`` for every store the run rostered, so
                       a replay needs the artifact and nothing else.
=====================  =====================================================================

Why the chain is stored at all, given the transcript already holds the postures: because a
transcript is this package's *account* of what happened, and the chain is the platform's. A
reader who only has the transcript has to trust the seeder. A reader who has the chain can
recompute every posture in the transcript from the events themselves — which is exactly what
:func:`replay_postures` does and what the suite asserts on.

Loading verifies
----------------
:func:`load` recomputes every digest in ``collection.json`` before returning anything, and
refuses the artifact if one does not match. A seed corpus whose provenance record and whose
bytes disagree is worse than no corpus: it is a corpus that *looks* accounted for.
"""

from __future__ import annotations

__all__ = [
    "ARTIFACT_KIND",
    "ARTIFACT_VERSION",
    "COLLECTION_FILE",
    "DEFAULT_ROOT",
    "LEDGER_FILE",
    "ROSTER_FILE",
    "TRANSCRIPT_FILE",
    "SeedArtifact",
    "SeedArtifactError",
    "canonical_bytes",
    "default_root",
    "load",
    "repo_root",
    "replay_postures",
    "write",
]

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import provenance
from .targets import describe_target

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .population import PopulationRun

#: Bumped when the on-disk layout changes in a way a reader must notice. Semantic, and read
#: by :func:`load`, so an artifact written by a future layout is refused with a sentence
#: rather than parsed into nonsense by a reader that predates it.
ARTIFACT_VERSION = "1.0.0"

#: What this corpus IS, in one machine-readable token. A directory of JSON is otherwise
#: indistinguishable from any other directory of JSON, and the one thing a stranger must not
#: have to guess about this one is that the sentiment in it was manufactured.
ARTIFACT_KIND = "simulated-buyer-feedback-population"

COLLECTION_FILE = "collection.json"
TRANSCRIPT_FILE = "transcript.json"
LEDGER_FILE = "ledger.jsonl"
ROSTER_FILE = "roster.json"


class SeedArtifactError(RuntimeError):
    """The stored seed corpus is missing, malformed, or does not match its own digests."""


def repo_root() -> Path:
    """The checkout root — ``services/sim/seed/store.py`` is three directories deep in it."""
    return Path(__file__).resolve().parents[3]


def default_root() -> Path:
    """Where a seed run stores its artifact when the caller names no directory.

    Beside the package that produces it and outside ``services/sim/src/``, which is the
    directory the simulation image copies. Two consequences, both wanted: the container's
    ``COPY services/sim/src/`` cannot pick up a corpus it has no use for, and the corpus sits
    next to the script that made it rather than in a tree owned by something else.
    """
    return repo_root() / "services" / "sim" / "seed-data" / "feedback-population"


#: Module-level convenience for callers that want the path without calling anything.
DEFAULT_ROOT = default_root()


def canonical_bytes(value: Any) -> bytes:
    """One JSON value, in the canonical form two runs are compared on.

    ``sort_keys`` and the tightest separators, exactly as
    :func:`seed.population.transcript_digest` serialises before hashing — so the bytes this
    writes to ``transcript.json`` are the bytes that function digests, and the artifact's
    recorded digest is directly comparable with a fresh run's.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ledger_bytes(events: Sequence[Mapping[str, Any]]) -> bytes:
    """The chain as JSONL: one canonical event per line, insertion order preserved.

    JSONL rather than one JSON array because a chain is append-ordered and a reader should be
    able to stream it, diff it line by line, and see at a glance that line *n* is event *n*.
    """
    return b"".join(canonical_bytes(event) + b"\n" for event in events)


def _decode_ledger(data: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise SeedArtifactError(f"{LEDGER_FILE} line {number} is not JSON: {exc}") from exc
        if not isinstance(event, dict):
            raise SeedArtifactError(
                f"{LEDGER_FILE} line {number} is a {type(event).__name__}, not a ledger event"
            )
        events.append(event)
    return events


@dataclass(frozen=True)
class SeedArtifact:
    """One stored seed corpus, loaded and digest-verified."""

    root: Path
    collection: dict[str, Any]
    transcript: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    roster: tuple[dict[str, str], ...]

    @property
    def as_of(self) -> str:
        """The instant every stored posture was decayed against. See :func:`replay_postures`."""
        value = self.collection.get("as_of")
        if not isinstance(value, str) or not value.strip():
            raise SeedArtifactError(
                f"{COLLECTION_FILE} names no `as_of`, so the stored postures cannot be "
                "reproduced: a replay at any other instant decays differently and would be "
                "comparing two different questions"
            )
        return value

    @property
    def seed(self) -> int:
        return int(self.transcript["seed"])

    @property
    def closing_postures(self) -> dict[str, Any]:
        """The postures the served ``GET /snapshot`` returned at the end of the stored run."""
        closing = self.transcript.get("closing_snapshot")
        if not isinstance(closing, dict):
            raise SeedArtifactError(f"{TRANSCRIPT_FILE} carries no closing_snapshot")
        return closing


def write(
    root: Path | str,
    *,
    run: PopulationRun,
    events: Iterable[Mapping[str, Any]],
    roster: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    producer: str,
    episodes: int,
) -> dict[str, Any]:
    """Store one run, and return the provenance record that was written beside it.

    Args:
        root: the directory to write into; created if absent, overwritten if present.
        run: what :func:`seed.population.run_population` produced.
        events: the trust chain the served routes wrote during the run, in insertion order.
        roster: the stores the run rostered — only ``store_id`` and ``business_identity`` are
            kept, see :func:`seed.provenance.thin_roster`.
        manifest: the human-approved fixture manifest the run was bounded by. Its digest is
            recorded, not its body: the manifest is a tracked file and copying it here would
            create a second copy free to drift from the one the approval covers.
        producer: the exact command a reader should run to reproduce this artifact.
        episodes: how many simulated days the run covered.

    Raises:
        SeedArtifactError: the run produced nothing a reader could replay.
    """
    from seed.population import transcript, transcript_digest

    chain = [dict(event) for event in events]
    thin = provenance.thin_roster(roster)
    if not thin:
        raise SeedArtifactError(
            "this run rostered no stores, so there are no postures to store and a replay "
            "would have nothing to reproduce"
        )

    body = transcript(run)
    transcript_bytes = canonical_bytes(body) + b"\n"
    ledger_bytes = _ledger_bytes(chain)
    roster_bytes = canonical_bytes(thin) + b"\n"

    seeded, organic = provenance.partition_events(chain)
    # Every rostered store gets a census row, including one nobody ever transacted with. An
    # absent row and a row of zeros read the same to a careless eye and mean different things:
    # "this store is not in this corpus" versus "this store is in it and earned nothing".
    census = {row["store_id"]: {"seeded": 0, "organic": 0} for row in thin}
    census.update(provenance.observation_census(chain))
    collection: dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "kind": ARTIFACT_KIND,
        # Redundant with `marker` below and stated anyway, at the top, in one word: the first
        # thing anyone opening this file must learn is that the sentiment in it is synthetic.
        "simulated": True,
        "captured_at": datetime.now(tz=UTC).isoformat(timespec="milliseconds"),
        "producer": producer,
        "producer_module": "seed.population",
        "as_of": provenance.as_of_of(run.closing_snapshot),
        "run": {
            "seed": int(run.seed),
            "episodes": int(episodes),
            "shoppers_per_episode": int(run.shoppers_per_episode),
            "policy": run.policy.to_json(),
            "purchases": len(run.purchases),
            "answers": len(run.answered),
            "manifest_version": manifest.get("manifest_version"),
            "manifest_digest": _manifest_digest(manifest),
            "episode_budget": manifest.get("episode_budget"),
            # "Which service did this synthetic sentiment get written into" is the first
            # question anyone auditing a manufactured corpus asks, and the answer that matters
            # is loopback-or-not rather than a port number. `describe_target` says which, in
            # the same words the run's own report used, so an artifact produced against a real
            # host says REMOTE here and cannot be mistaken for one that was not.
            "buyer_service": describe_target(run.buyer_url),
            "trust_service": describe_target(run.trust_url),
        },
        "marker": {
            "field": provenance.SEED_MARKER_FIELD,
            "prefix": provenance.SEED_MARKER_PREFIX,
            "reader": "seed.provenance.is_seeded_event",
            "audit": "seed.provenance.posture_split",
            "why": (
                "Every observation in this corpus was manufactured. The marker is inside the "
                "hash chain because the buyer service copies `order_ref` verbatim onto the "
                "sealed event, so a seeded observation cannot be un-marked and an earned one "
                "cannot be marked without breaking GET /events/verify. That is what makes "
                "this seed data rather than fake data: a reader can compute any store's "
                "posture with these observations and without them, retroactively, forever."
            ),
            "seeded_events": len(seeded),
            "organic_events": len(organic),
            "observations_by_store": census,
        },
        "source": _source_revision(),
        "determinism": {
            "canonical_file": TRANSCRIPT_FILE,
            "transcript_digest": transcript_digest(run),
            "reproduce": producer,
            "reproducible_files": [TRANSCRIPT_FILE, ROSTER_FILE],
            "volatile_files": [COLLECTION_FILE, LEDGER_FILE],
            # The scope of the claim, stated so it is not read wider than it is. "Same seed,
            # same bytes" is a property of the SEEDER at one revision of the platform, not a
            # promise across revisions: the transcript records what the real exchange, the real
            # reconciler and the real trust engine did, so a change to any of them legitimately
            # moves these bytes. Measured on this tree — closing the `apps/exchange` defect that
            # dropped `delivery_estimate_days` from the accepted event changed the digest,
            # correctly, because dispatch promises became gradeable and shoppers started
            # answering 'as_described_but_late'.
            #
            # This is why `replay` checks the artifact against ITSELF rather than against a
            # fresh run. An artifact must stay internally consistent forever; it must not
            # oblige the platform to stand still.
            "scope": (
                "byte-identical across processes and machines at the same source revision; a "
                "change to the exchange, the reconciler or the trust engine moves these bytes "
                "and is meant to. See `source`."
            ),
            "excluded_from_the_canonical_form": [
                "minted discount codes, checkout tokens and permalinks (drawn from `secrets`;"
                " a reproducible discount code is a guessable one)",
                "each feedback event's `event_id` (a UUID) and `ts` (a wall clock)",
                "the served posture's `decayed_at` (the serve instant)",
                "the low-order digits of a served score, below 1e-6 rounding",
            ],
        },
        "files": {
            "transcript": TRANSCRIPT_FILE,
            "ledger": LEDGER_FILE,
            "roster": ROSTER_FILE,
        },
        "file_sha256": {
            "transcript": _sha256(transcript_bytes),
            "ledger": _sha256(ledger_bytes),
            "roster": _sha256(roster_bytes),
        },
        "bytes": {
            "transcript": len(transcript_bytes),
            "ledger": len(ledger_bytes),
            "roster": len(roster_bytes),
        },
        "stores": [row["store_id"] for row in thin],
        "notes": [
            "SIMULATED BUYER FEEDBACK. Not one answer in this corpus came from a person.",
            "Every answer travelled through the real POST /buyer/feedback route and became a "
            "hash-chained ledger event; the processing path is the production one, and only "
            "the caller was synthetic.",
            "The postures here are a function of the chain and of `as_of`. Replay with "
            "`python -m seed replay`, which recomputes them from ledger.jsonl and must "
            "reproduce transcript.json's closing_snapshot exactly.",
            "Going live changes no code on the serving path: stop running the seeder and let "
            "real shoppers answer the prompt the buyer app already renders. See the `marker` "
            "block for how the two stay distinguishable afterwards.",
        ],
    }

    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / TRANSCRIPT_FILE).write_bytes(transcript_bytes)
    (directory / LEDGER_FILE).write_bytes(ledger_bytes)
    (directory / ROSTER_FILE).write_bytes(roster_bytes)
    (directory / COLLECTION_FILE).write_bytes(
        json.dumps(collection, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )
    return collection


def _source_revision() -> dict[str, Any]:
    """``{commit, dirty}`` for the checkout that produced this corpus, best-effort.

    Recorded because ``determinism.reproduce`` is only reproducible against the code that ran:
    the transcript is a record of what the real exchange, reconciler and trust engine did, so
    a reader comparing digests needs to know which revision to compare at.

    ``dirty`` is not decoration and it is usually True here — this corpus is produced during
    development, from a working tree with uncommitted changes, and a bare commit hash would
    claim a precision the bytes do not have. An unavailable git is recorded as ``""`` rather
    than guessed; a corpus that cannot say where it came from must not pretend it can.
    """
    import subprocess  # noqa: S404 - reading our own revision, no user input reaches it

    def _git(*args: str) -> str:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", *args],
                cwd=repo_root(),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - git may be absent
            return ""
        return done.stdout.strip() if done.returncode == 0 else ""

    commit = _git("rev-parse", "HEAD")
    return {
        "commit": commit,
        "dirty": bool(_git("status", "--porcelain")) if commit else None,
    }


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    """The approved manifest's own body digest, or ``""`` if it cannot be computed.

    Lazily imported and tolerant on purpose: the digest is provenance, and an artifact that
    refused to be written because a helper moved would be a worse outcome than one whose
    provenance says "not recorded". It is never *invented* — the empty string is the honest
    answer and a reader can tell it from a hash.
    """
    try:
        from fixtures.manifest import body_digest

        return str(body_digest(manifest))
    except Exception:  # pragma: no cover - provenance is best-effort, never load-bearing
        return ""


def load(root: Path | str | None = None) -> SeedArtifact:
    """Read a stored seed corpus, verifying every digest its own record claims.

    Args:
        root: the artifact directory. Defaults to :func:`default_root`.

    Raises:
        SeedArtifactError: the directory is missing, a file is absent, the layout version is
            newer than this reader, or a file's bytes do not hash to the value
            ``collection.json`` records for it.
    """
    directory = Path(root) if root is not None else default_root()
    record = directory / COLLECTION_FILE
    if not record.is_file():
        raise SeedArtifactError(
            f"no seed corpus at {directory}: {COLLECTION_FILE} is not there. Produce one with "
            f"`python -m seed run`, which runs the population once and stores what it made."
        )
    try:
        collection = json.loads(record.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SeedArtifactError(f"{record} is not readable JSON: {exc}") from exc
    if not isinstance(collection, dict):
        raise SeedArtifactError(f"{record} is a {type(collection).__name__}, not a record")

    stored_version = str(collection.get("artifact_version") or "")
    if stored_version.split(".")[0] != ARTIFACT_VERSION.split(".")[0]:
        raise SeedArtifactError(
            f"{record} declares artifact_version {stored_version!r}; this reader understands "
            f"{ARTIFACT_VERSION!r}. A layout this reader predates is refused rather than "
            "parsed into a plausible-looking wrong answer."
        )

    digests = collection.get("file_sha256")
    digests = digests if isinstance(digests, Mapping) else {}
    payloads: dict[str, bytes] = {}
    for name, filename in (
        ("transcript", TRANSCRIPT_FILE),
        ("ledger", LEDGER_FILE),
        ("roster", ROSTER_FILE),
    ):
        path = directory / filename
        if not path.is_file():
            raise SeedArtifactError(f"{record} lists {filename}, and {path} is not there")
        data = path.read_bytes()
        expected = str(digests.get(name) or "")
        actual = _sha256(data)
        if expected and expected != actual:
            raise SeedArtifactError(
                f"{path} does not match the digest {COLLECTION_FILE} records for it "
                f"(recorded {expected}, found {actual}). A seed corpus whose bytes and whose "
                "provenance disagree is worse than none: it looks accounted for."
            )
        if not expected:
            raise SeedArtifactError(
                f"{COLLECTION_FILE} records no digest for {filename}, so nothing about these "
                "bytes can be checked"
            )
        payloads[name] = data

    try:
        transcript = json.loads(payloads["transcript"].decode("utf-8"))
        roster = json.loads(payloads["roster"].decode("utf-8"))
    except ValueError as exc:
        raise SeedArtifactError(f"{directory} holds unreadable JSON: {exc}") from exc
    if not isinstance(transcript, dict):
        raise SeedArtifactError(f"{TRANSCRIPT_FILE} is not a transcript object")
    if not isinstance(roster, list):
        raise SeedArtifactError(f"{ROSTER_FILE} is not a list of store rows")

    return SeedArtifact(
        root=directory,
        collection=collection,
        transcript=transcript,
        events=tuple(_decode_ledger(payloads["ledger"])),
        roster=tuple({str(k): str(v) for k, v in row.items()} for row in roster),
    )


def replay_postures(artifact: SeedArtifact) -> dict[str, dict[str, Any]]:
    """Recompute every store's posture from the stored chain. Offline, and nothing simulated.

    This is the "replay" the owner asked for: no auctions, no shoppers, no buyer service, no
    trust service, no sockets — the stored ledger events projected through the platform's own
    scorer at the stored ``as_of``. It must reproduce ``transcript.json``'s
    ``closing_snapshot`` exactly, and the suite asserts that it does; if it ever stops doing
    so, either the chain in the artifact is not the chain that produced those postures or the
    trust engine's arithmetic has moved, and both are things a reader needs to be told.
    """
    return provenance.postures(artifact.events, artifact.roster, as_of=artifact.as_of)
