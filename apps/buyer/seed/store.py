"""Write and read the checked-in corpus. The OUTPUT is the artefact, not this script.

The convention is ``services/sim/seed/store.py``'s, followed rather than re-invented:

* the producer is a script, the corpus is the deliverable, and the corpus is committed;
* ``collection.json`` records the digest of every other file, and :func:`load` refuses to
  return anything whose bytes do not match — **and refuses just as loudly when a digest is
  absent**, because "no digest recorded" is indistinguishable from "not checked" to every
  caller downstream;
* the exact command that reproduces the bytes is recorded, with every value resolved rather
  than as it was typed;
* a corpus written by a future layout is refused, not parsed into a plausible-looking wrong
  answer.

Two files, and no more::

    apps/buyer/seed-data/store-window/
      collection.json   the provenance record; indent=2, sort_keys, human-read
      accounts.jsonl    one canonical JSON row per line, in chain order

``accounts.jsonl`` rather than one JSON array for the reason ``services/sim/seed`` writes its
ledger that way: this is an ordered chain, and a reader should be able to see at a glance that
line *n* is ordinal *n*, diff it line by line, and stream it.

What is NOT in the corpus, on purpose
--------------------------------------
The accounts. :mod:`apps.buyer.seed.population` generates region, order history and a
synthetic address for each buyer and throws all three away — see its module docstring. The
corpus holds what ``app.buyer_accounts`` holds and one field more (``cohort``, so the census is
readable), and the ``cohort`` label is **not** part of the chain, so it can be renamed without
invalidating a digest and cannot smuggle anything into the marker.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .chain import CHAIN_GENESIS, SEED_PSEUDONYM_PREFIX, canonical_bytes, verify_chain
from .population import COHORTS, DEFAULT_MEMBERS_PER_COHORT, DEFAULT_SEED, census

__all__ = [
    "ACCOUNTS_FILE",
    "ARTIFACT_KIND",
    "ARTIFACT_VERSION",
    "COLLECTION_FILE",
    "DEFAULT_ROOT",
    "SeedArtifactError",
    "SeedCorpus",
    "default_root",
    "load",
    "repo_root",
    "write",
]

#: Bumped only when the LAYOUT changes — a file added, removed or renamed, or a field that
#: :func:`load` reads moving. Compared on the major component: a corpus written by a future
#: major is refused rather than read.
ARTIFACT_VERSION = "1.0.0"

#: What this corpus is, in one string, for a reader who found the directory and nothing else.
ARTIFACT_KIND = "seeded-buyer-store-window"

COLLECTION_FILE = "collection.json"
ACCOUNTS_FILE = "accounts.jsonl"


class SeedArtifactError(Exception):
    """The corpus on disk cannot be trusted: wrong layout, missing digest, or edited bytes."""


def repo_root() -> Path:
    """This checkout's root — ``apps/buyer/seed/store.py`` is three directories deep."""
    return Path(__file__).resolve().parents[3]


def default_root() -> Path:
    """Where the committed corpus lives."""
    return repo_root() / "apps" / "buyer" / "seed-data" / "store-window"


DEFAULT_ROOT = default_root()


@dataclass(frozen=True)
class SeedCorpus:
    """A corpus that has been loaded, digest-checked and chain-verified.

    There is no way to construct one that skipped those checks: :func:`load` is the only
    producer, and it raises rather than returning a partially-verified object.
    """

    root: Path
    collection: dict[str, Any]
    rows: tuple[dict[str, Any], ...]

    @property
    def chain_head(self) -> str:
        """The head recorded in the provenance, which :func:`load` has already recomputed."""
        head = self.collection.get("determinism", {}).get("chain_head")
        if not isinstance(head, str) or not head:
            raise SeedArtifactError(f"{self.root / COLLECTION_FILE} records no chain head")
        return head

    @property
    def seed(self) -> int:
        """The draw seed, so a reader can reproduce the corpus from the artefact alone."""
        value = self.collection.get("run", {}).get("seed")
        if not isinstance(value, int) or isinstance(value, bool):
            raise SeedArtifactError(f"{self.root / COLLECTION_FILE} records no integer seed")
        return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_revision() -> dict[str, Any]:
    """Where these bytes came from. Empty strings when git cannot answer — never invented.

    A corpus that cannot say what revision produced it must say so, because "unknown" and "the
    revision I guessed" are answers a reader treats very differently.
    """
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git on the box
        return {"commit": "", "dirty": None}
    return {
        "commit": commit.stdout.strip() if commit.returncode == 0 else "",
        "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
    }


def _accounts_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """The corpus as JSONL: one canonical row per line, chain order preserved."""
    return b"".join(canonical_bytes(dict(row)) + b"\n" for row in rows)


def write(
    rows: Sequence[Mapping[str, Any]],
    *,
    chain_head: str,
    producer: str,
    seed: int = DEFAULT_SEED,
    members: int = DEFAULT_MEMBERS_PER_COHORT,
    root: Path | None = None,
) -> Path:
    """Write the corpus and its provenance to ``root``. Returns the directory.

    The chain is verified BEFORE anything is written. A producer that wrote first and checked
    afterwards would leave a corpus on disk that its own next ``load`` refuses, and the person
    who finds it has to work out whether the generator or the disk is at fault.
    """
    verify_chain(rows, expected_head=chain_head)

    destination = Path(root) if root is not None else default_root()
    destination.mkdir(parents=True, exist_ok=True)

    accounts = _accounts_bytes(rows)
    collection: dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "kind": ARTIFACT_KIND,
        "simulated": True,
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "producer": producer,
        "producer_module": "apps.buyer.seed.population",
        "run": {
            "seed": seed,
            "members_per_cohort": members,
            "cohorts": len(COHORTS),
            "rows": len(rows),
        },
        "target": {
            "table": "app.buyer_accounts",
            "columns": ["pseudonym", "buckets", "provenance"],
            "loader": "python -m apps.buyer.seed load --dsn $PROXYSHOP_PG_DSN_APP",
            "reader": "GET /buyer/store-window (buyer_svc.window.routes)",
        },
        "marker": {
            "field": "pseudonym",
            "prefix": SEED_PSEUDONYM_PREFIX,
            "column": "provenance",
            "reader": "apps.buyer.seed.chain.is_seeded_pseudonym",
            "enforced_by": (
                "db/migrations/0005_buyer_accounts_provenance.sql, constraint "
                "buyer_accounts_provenance_is_the_key"
            ),
            "why": (
                "Every row in this corpus is manufactured. The marker is the PRIMARY KEY "
                "rather than a column beside it, and the key is a hash chain over the rows "
                "themselves, so a seeded row cannot be un-marked and a real buyer's row "
                "cannot be marked: the vault mints `psn-` + 32 hex characters and `s` is not "
                "a hex digit, and the database refuses any row whose `provenance` and whose "
                "pseudonym disagree. See apps/buyer/seed/chain.py."
            ),
            "what_it_does_not_cover": (
                "MEMBERSHIP. The constraint guarantees no caller of a served route can forge "
                "the marker; it cannot tell a row this corpus pins from one somebody with the "
                "`app` role inserted under a made-up `psn-seed-` key, because the table stores "
                "no signature. Run `python -m apps.buyer.seed audit --dsn <dsn>` to compare "
                "the table against these rows; it exits non-zero on any seeded row that is "
                "unpinned, altered or missing."
            ),
            "audit": "python -m apps.buyer.seed audit --dsn $PROXYSHOP_PG_DSN_APP",
            "seeded_rows": len(rows),
            "rows_by_cohort": census(rows),
        },
        "source": _source_revision(),
        "determinism": {
            "canonical_file": ACCOUNTS_FILE,
            "chain_genesis": CHAIN_GENESIS,
            "chain_head": chain_head,
            "reproduce": producer,
            "reproducible_files": [ACCOUNTS_FILE],
            "volatile_files": [COLLECTION_FILE],
            "scope": (
                "byte-identical across processes and machines at the same source revision. "
                "The buckets are produced by buyer_svc.profile.build_buckets, so a change to "
                "the coarseners, to BUDGET_BANDS, to FREQUENCY_TIERS or to CATEGORY_LIMIT "
                "moves these bytes and the chain head with them, and is meant to. See "
                "`source` for the revision these were produced at."
            ),
        },
        "files": [COLLECTION_FILE, ACCOUNTS_FILE],
        "file_sha256": {ACCOUNTS_FILE: _sha256(accounts)},
        "bytes": {ACCOUNTS_FILE: len(accounts)},
        "notes": [
            "Seeded buyers have NO entry in vault.pseudonym_history. The pseudonyms here are "
            "minted by a digest, not issued by the vault, so nothing anywhere can resolve one "
            "to a person -- which is the strongest form of R5 available and is true of the "
            "seeded half of the window by construction rather than by policy.",
            "Going live changes no code on the serving path: GET /buyer/store-window reads "
            "app.buyer_accounts and copies the `provenance` column it finds. Stop loading "
            "this corpus, delete its rows (`delete from app.buyer_accounts where provenance = "
            "'seed'`), and the window serves only the buyers real logins published.",
            "The floor the window releases under is a deployment knob "
            "(PROXYSHOP_BUYER_STORE_WINDOW_K, default 2) and is NOT part of this corpus. The "
            "cohorts hold 5 members each so the same bytes demonstrate any floor up to 5.",
        ],
    }

    (destination / ACCOUNTS_FILE).write_bytes(accounts)
    (destination / COLLECTION_FILE).write_bytes(
        json.dumps(collection, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )
    return destination


def load(root: Path | None = None) -> SeedCorpus:
    """Read, digest-check and chain-verify the corpus at ``root``.

    Raises:
        SeedArtifactError: the directory is missing a file, was written by a layout this
            reader predates, records no digest for a file, or records one that does not match
            the bytes on disk.
        apps.buyer.seed.chain.ChainBroken: the digests match and the rows are still not the
            chain the provenance claims — which is the case a file digest cannot catch,
            because a hand-edited corpus can be re-digested. The chain head is the outside
            witness that re-digesting does not move.
    """
    source = Path(root) if root is not None else default_root()
    collection_path = source / COLLECTION_FILE
    if not collection_path.is_file():
        raise SeedArtifactError(
            f"no {COLLECTION_FILE} at {source}; produce one with `python -m apps.buyer.seed run`"
        )
    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    if not isinstance(collection, Mapping):
        raise SeedArtifactError(f"{collection_path} is not a JSON object")

    stored_version = str(collection.get("artifact_version", ""))
    if stored_version.split(".")[0] != ARTIFACT_VERSION.split(".")[0]:
        raise SeedArtifactError(
            f"{collection_path} declares layout {stored_version!r} and this reader "
            f"understands {ARTIFACT_VERSION!r}. A layout this reader predates is refused "
            "rather than parsed into a plausible-looking wrong answer."
        )

    digests = collection.get("file_sha256")
    if not isinstance(digests, Mapping):
        raise SeedArtifactError(f"{collection_path} records no file_sha256 block")

    accounts_path = source / ACCOUNTS_FILE
    if not accounts_path.is_file():
        raise SeedArtifactError(f"{accounts_path} is missing; the corpus is incomplete")
    body = accounts_path.read_bytes()
    expected = digests.get(ACCOUNTS_FILE)
    if not expected:
        raise SeedArtifactError(
            f"{COLLECTION_FILE} records no digest for {ACCOUNTS_FILE}, so nothing about these "
            "bytes can be checked"
        )
    actual = _sha256(body)
    if expected != actual:
        raise SeedArtifactError(
            f"{accounts_path} does not match the digest {COLLECTION_FILE} records for it "
            f"(recorded {expected}, found {actual}). A seed corpus whose bytes and whose "
            "provenance disagree is worse than none: it looks accounted for."
        )

    rows = tuple(json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip())

    # A missing head is refused, not tolerated, and it is the same argument as the missing
    # digest above. `verify_chain` without an `expected_head` checks only that each row's
    # pseudonym matches its own link — which a TRUNCATED corpus satisfies perfectly, because a
    # chain that ends early is a valid chain over the rows that remain. The recorded head is
    # the only witness to how many rows there were supposed to be, so a corpus that carries
    # none has not been checked in the way this loader's callers believe it has.
    head = collection.get("determinism", {}).get("chain_head")
    if not isinstance(head, str) or not head:
        raise SeedArtifactError(
            f"{collection_path} records no determinism.chain_head, so a corpus with rows "
            "removed from the end would verify. The head is the outside witness and it is "
            "required, for the same reason a missing file digest is refused rather than skipped."
        )
    verify_chain(rows, expected_head=head)
    return SeedCorpus(root=source, collection=dict(collection), rows=rows)
