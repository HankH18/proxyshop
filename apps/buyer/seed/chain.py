"""The seed marker, and why a caller cannot forge it (T-142).

``services/sim/seed`` put its marker inside the ledger's hash chain: the buyer service copies
``order_ref`` verbatim onto the sealed event, ``order_ref`` is one of ``EVENT_FIELDS``, and
``event_hash`` is computed over those fields — so un-marking a seeded observation or marking
an organic one both fail ``GET /events/verify``. That is the standard this module has to meet
for a **table**, which is a harder shape: rows here are upserted in place, there is no
``prev_hash`` column to break, and the row a store reads carries no signature.

The equivalent guarantee is built in two layers, and it is the *pair* that holds.

Layer 1 — the marker is the PRIMARY KEY, and the database refuses disagreement
------------------------------------------------------------------------------
A seeded row's ``pseudonym`` starts with :data:`SEED_PSEUDONYM_PREFIX` (``psn-seed-``), and
``db/migrations/0005_buyer_accounts_provenance.sql`` carries

    CHECK ((provenance = 'seed') = (pseudonym LIKE 'psn-seed-%'))

as an **equivalence**, so Postgres rejects a row whose label and whose key disagree, in either
direction, for every writer that will ever exist. Concretely, the two forgeries a caller might
attempt:

* **Make my real row look seeded.** A buyer's row is keyed by the pseudonym
  :func:`buyer_svc.vault.default_pseudonym` minted for them — ``psn-`` followed by 32
  characters of ``secrets.token_hex``. ``token_hex`` emits ``0-9a-f``; ``s`` is not among
  them, so no vault pseudonym can ever match ``psn-seed-%``, and the buyer chooses none of
  those bytes anyway. Labelling the row ``'seed'`` is then a CheckViolation, MEASURED.
* **Make a seeded row look real.** ``provenance`` cannot be updated to ``'live'`` while the
  key still matches the pattern, and changing the key is changing the PRIMARY KEY — which is
  not an UPDATE any writer in this tree issues, and which layer 2 detects.

Layer 2 — the marker is a digest OF the row, chained
-----------------------------------------------------
A prefix alone would only say "somebody wrote ``psn-seed-`` here". So the rest of the
pseudonym is not random: it is a hash chain over the corpus, exactly one link per seeded row.

    digest[i]     = sha256( digest[i-1] || canonical_json({"ordinal": i, "buckets": ...}) )
    pseudonym[i]  = "psn-seed-" + digest[i][:24]

with ``digest[-1]`` = :data:`CHAIN_GENESIS`. The final digest is the chain head, and it is
recorded in the corpus's ``collection.json`` and checked into this repository. Therefore:

* editing a seeded row's buckets moves that row's digest, so its pseudonym no longer matches
  the row it labels, and every later link moves too;
* inserting or removing a row renumbers every ordinal after it, so the tail is a different
  chain;
* minting a *new* ``psn-seed-…`` row requires the digest the chain would have produced at that
  ordinal — i.e. a sha256 preimage, or a rewrite of the checked-in head.

:func:`verify_chain` recomputes the whole thing from the corpus alone and is what the tests
and ``python -m apps.buyer.seed verify`` run. It is recomputable by ANYONE holding the corpus,
because there is no key.

The two things this is, and the two it is not
----------------------------------------------
Precision here matters more than reassurance, because every one of these was overstated in an
earlier draft of this file.

**Layer 1 holds against every caller.** No sequence of HTTP requests produces a row that reads
as seeded, or strips the marker from one that is. That is not a rule this code applies, it is
a statement Postgres refuses, and the alphabet argument above is why a buyer cannot even
collide into the namespace by accident.

**Layer 2 pins the CORPUS, not the table.** The chain proves that
``apps/buyer/seed-data/store-window/accounts.jsonl`` is internally consistent and ends where its
provenance says. It proves nothing about rows in ``app.buyer_accounts``: the table stores no
digest, so anything holding the ``app`` role can insert ``psn-seed-`` plus twenty-four
characters of its choosing, and that row is legal, is served as seeded, and is in no chain.
**The check for that is** ``python -m apps.buyer.seed audit``, which compares the table against
the corpus; nothing at read time can do it, and no constraint on this table could.

**It is not an authenticity claim about the buckets.** Nobody signs them. A party who can write
both the table and this repository can produce a consistent lie, and re-running
``python -m apps.buyer.seed run --out <dir>`` re-digests any corpus you like, head included —
so :func:`~apps.buyer.seed.store.load` alone proves self-consistency, not identity with what
was committed. The witness that closes THAT is
``test_the_committed_corpus_is_exactly_what_the_producer_produces``, which re-derives the whole
corpus from :data:`~apps.buyer.seed.population.COHORTS` and compares bytes.

**The truncation is not load-bearing.** 24 hex characters is what lands in the pseudonym; the
FULL digest carries the chain forward and classification is a prefix test, so nothing anywhere
depends on those 96 bits being hard to hit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "CHAIN_GENESIS",
    "DIGEST_CHARS",
    "SEED_MARKER",
    "SEED_PSEUDONYM_PREFIX",
    "ChainBroken",
    "canonical_bytes",
    "chain_head",
    "is_seeded_pseudonym",
    "link",
    "mint",
    "verify_chain",
]

#: The vault's own prefix. Imported rather than spelled, so a seeded pseudonym is a *valid*
#: pseudonym: ``buyer_svc.auth.sessions`` refuses any handle that does not carry it, and a
#: marker that made seeded rows structurally invalid would be a marker nothing could read
#: back through the product's own code.
try:  # pragma: no cover - exercised by whichever spelling resolves in this process
    from buyer_svc.vault import PSEUDONYM_PREFIX
except ImportError:  # pragma: no cover - the repo-root spelling
    from apps.buyer.svc.src.vault import PSEUDONYM_PREFIX  # type: ignore[no-redef]

#: What is inserted between the vault prefix and the digest. Chosen for the property the
#: migration's CHECK depends on: ``s`` is not a hexadecimal digit, so
#: ``PSEUDONYM_PREFIX + secrets.token_hex(16)`` — every pseudonym the vault can issue — cannot
#: collide with this namespace no matter how many are drawn.
SEED_MARKER = "seed-"

#: The full reserved namespace, ``psn-seed-``. This exact string is also the literal in
#: ``db/migrations/0005_buyer_accounts_provenance.sql``'s CHECK, and
#: ``test_the_seed_marker_the_database_enforces_is_the_one_python_mints`` reads that file and
#: asserts the two still agree — a marker split across a Python constant and an unrelated SQL
#: literal is one rename away from a table whose constraint passes while every row classifies
#: as ``'live'``.
SEED_PSEUDONYM_PREFIX = f"{PSEUDONYM_PREFIX}{SEED_MARKER}"

#: Where the chain starts. Sixty-four zeros, matching ``ledger.commerce_events``'s own genesis
#: ``prev_hash`` — the same convention, so a reader who knows one knows this.
CHAIN_GENESIS = "0" * 64

#: How much of each digest lands in the pseudonym. 24 hex characters is 96 bits, which is what
#: a forger would have to hit by preimage; the FULL digest is what carries forward to the next
#: link, so truncation costs the pseudonym entropy and costs the chain none.
DIGEST_CHARS = 24


class ChainBroken(Exception):
    """A corpus's pseudonyms are not the chain its own rows produce.

    Raised with the ordinal and both digests. Never swallowed anywhere: a corpus that cannot
    prove it is the corpus this repository committed to is worse than no corpus, because it
    looks accounted for.
    """


def canonical_bytes(value: Any) -> bytes:
    """One JSON value in the form two runs are compared on.

    ``sort_keys`` and the tightest separators — the same canonical form
    ``seed.store.canonical_bytes`` and ``trust.ledger.canonical`` use, deliberately, so that
    "canonical JSON" means one thing in this repository rather than three.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _buckets_of(row: Any) -> Any:
    """The buckets as plain JSON data, whether ``row`` is a mapping or a pydantic model."""
    if isinstance(row, Mapping):
        buckets = row.get("buckets")
    else:
        buckets = getattr(row, "buckets", None)
    if buckets is None:
        raise ValueError(f"a corpus row must carry `buckets`; got {type(row).__name__}")
    if hasattr(buckets, "model_dump"):
        return buckets.model_dump()
    if isinstance(buckets, Mapping):
        return dict(buckets)
    raise ValueError(f"`buckets` must be a mapping or a model, got {type(buckets).__name__}")


def link(previous: str, ordinal: int, buckets: Any) -> str:
    """The digest for the row at ``ordinal``, given the digest before it.

    ``sha256(previous || canonical_json({"buckets": ..., "ordinal": ...}))``. The ordinal is
    inside the hashed body on purpose: without it, two rows with identical buckets would be
    identical links, and a corpus could be reordered — or a duplicate row dropped — without
    moving the head.
    """
    if not isinstance(previous, str) or len(previous) != 64:
        raise ValueError(f"a chain link needs the 64-character digest before it, got {previous!r}")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise ValueError(f"an ordinal must be a non-negative integer, got {ordinal!r}")
    digest = hashlib.sha256()
    digest.update(previous.encode("ascii"))
    digest.update(canonical_bytes({"buckets": buckets, "ordinal": ordinal}))
    return digest.hexdigest()


def mint(previous: str, ordinal: int, buckets: Any) -> tuple[str, str]:
    """``(pseudonym, digest)`` for the row at ``ordinal``.

    The pseudonym is the marker plus the first :data:`DIGEST_CHARS` of the digest; the digest
    returned in full is what the next call must be handed.
    """
    digest = link(previous, ordinal, buckets)
    return f"{SEED_PSEUDONYM_PREFIX}{digest[:DIGEST_CHARS]}", digest


def is_seeded_pseudonym(value: Any) -> bool:
    """Whether ``value`` is a handle this package minted.

    A prefix test on the pseudonym, and deliberately **not** a read of the ``provenance``
    column — so a reader holding only a served response can classify a row without trusting the
    label that came with it.

    That is worth exactly what it is worth, and no more. This and the column are two READERS of
    one fact, not two independent facts: the database's CHECK forces them to agree, so
    ``test_the_column_and_the_key_agree_on_every_released_row`` can only catch a route that
    mixed fields across rows — it cannot catch a forged row, because a forged row satisfies both
    readers. The genuinely independent witness is the committed corpus, and the check that
    consults it is ``python -m apps.buyer.seed audit``.

    An unrecognised value reads as **not seeded**, which is the fail-safe direction: an
    unmarked row is treated as a real buyer, so nothing manufactured is ever mistaken for
    organic in the *other* direction, where it would be laundered into the record.
    """
    return isinstance(value, str) and value.startswith(SEED_PSEUDONYM_PREFIX)


def chain_head(rows: Sequence[Any]) -> str:
    """The head this sequence of rows produces. :data:`CHAIN_GENESIS` for an empty corpus."""
    previous = CHAIN_GENESIS
    for ordinal, row in enumerate(rows):
        previous = link(previous, ordinal, _buckets_of(row))
    return previous


def verify_chain(rows: Iterable[Any], *, expected_head: str | None = None) -> str:
    """Recompute the chain over ``rows`` and return its head. Raises on any divergence.

    Two independent checks, and both matter:

    * every row's ``pseudonym`` must equal the one its own buckets and ordinal mint — this is
      what makes an edited bucket detectable from the corpus alone, with no outside witness;
    * the head must equal ``expected_head`` when one is supplied — this is the outside witness,
      and it is what makes a *truncated* corpus detectable, since a truncated chain's head is
      itself a perfectly valid chain head. The same reason ``trust.ledger.chain.verify_chain``
      takes ``expected_length``/``expected_head``.

    Raises:
        ChainBroken: a pseudonym does not match its link, or the head does not match.
    """
    previous = CHAIN_GENESIS
    count = 0
    for ordinal, row in enumerate(rows):
        buckets = _buckets_of(row)
        expected_pseudonym, previous = mint(previous, ordinal, buckets)
        actual = row.get("pseudonym") if isinstance(row, Mapping) else getattr(row, "pseudonym", "")
        if actual != expected_pseudonym:
            raise ChainBroken(
                f"row {ordinal} carries pseudonym {actual!r} but its buckets and its position "
                f"mint {expected_pseudonym!r}. Either the buckets were edited after the corpus "
                "was produced, or a row was inserted, removed or reordered ahead of this one."
            )
        count += 1
    if expected_head is not None and previous != expected_head:
        raise ChainBroken(
            f"the corpus recomputes to head {previous!r} over {count} row(s), and its "
            f"provenance records {expected_head!r}. A chain that ends early is still a valid "
            "chain, which is exactly why the head is recorded outside it."
        )
    return previous
