"""Counts written out in prose, graded against the thing they claim to count.

Prose that outlived its subject is this repository's largest single defect class -- roughly a
third of everything found -- and the cheapest instance of it is a NUMBER. ``candidates.py``
said ``FALLBACK_REASONS`` held NINE reasons in three separate places; the tuple has held
twelve since ``bid_claim_unprovenanced``,
``response_timed_out`` and ``fan_out_capacity_exhausted`` were added beside the nine. Nothing went red, because nothing was looking.

A number word in a docstring is machine-checkable, so it is checked here. The gate is
deliberately narrow: it grades the sentences that name a specific collection, and it fails
with the sentence quoted, so the fix is to correct the prose rather than to delete it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from exchange.auction.collect import FALLBACK_REASONS

REPO = Path(__file__).resolve().parents[3]

#: Number words this file can read, up to a ceiling well past any collection it grades.
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}

#: (module, the collection's real length, a regex whose one group is the number word).
#: Each pattern is anchored on the collection's own name, so it cannot drift onto some other
#: sentence's number, and every pattern must match at least once -- a rule that silently stops
#: matching is the failure mode a text gate is most prone to, and the one that makes a gate
#: look green while grading nothing.
COUNTED_PROSE = [
    (
        "apps/exchange/src/ranking/candidates.py",
        len(FALLBACK_REASONS),
        re.compile(
            r"(?:every one of the|one of the)\s+([A-Za-z]+)\s+(?:reasons|values)"
            r"(?=.{0,220}FALLBACK_REASONS)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "apps/exchange/src/ranking/candidates.py",
        len(FALLBACK_REASONS),
        re.compile(r"The count is\s+([A-Za-z]+)\s+rather than", re.IGNORECASE),
    ),
    # THE PUBLISHED BUNDLE, and it is here because the count got out of date in the SCHEMA
    # first. A partner integrator reading `protocol.schema.json` to decide whether to pin an
    # enum reads this sentence, and until this row existed nothing in the repo compared it to
    # anything. Its shape differs from the two above — the number word is followed by the
    # IDENTIFIER rather than by "reasons" or "values" — so it needs its own pattern; reusing
    # theirs would match nothing and trip this file's own "grading no prose at all" assertion,
    # which is red for the wrong reason.
    (
        "packages/contracts/schemas/protocol.schema.json",
        len(FALLBACK_REASONS),
        re.compile(
            r"every one of the\s+([A-Za-z]+)\s+`exchange\.auction\.collect\.FALLBACK_REASONS`",
            re.IGNORECASE,
        ),
    ),
    # ...and both generated mirrors, which are the files a consumer actually imports. They are
    # written by `contracts.codegen` from the schema above, so a correction there reaches them
    # only if somebody regenerates; this row is what notices when nobody did.
    (
        "packages/contracts/generated/python/protocol.py",
        len(FALLBACK_REASONS),
        re.compile(
            r"every one of the\s+([A-Za-z]+)\s+`exchange\.auction\.collect\.FALLBACK_REASONS`",
            re.IGNORECASE,
        ),
    ),
    (
        "packages/contracts/generated/ts/protocol.schema.d.ts",
        len(FALLBACK_REASONS),
        re.compile(
            r"every one of the\s+([A-Za-z]+)\s+`exchange\.auction\.collect\.FALLBACK_REASONS`",
            re.IGNORECASE,
        ),
    ),
]


@pytest.mark.parametrize("relative,expected,pattern", COUNTED_PROSE)
def test_a_count_written_out_in_prose_equals_the_collection_it_names(
    relative: str, expected: int, pattern: re.Pattern[str]
) -> None:
    text = (REPO / relative).read_text(encoding="utf-8")
    found = pattern.findall(text)

    assert found, (
        f"{relative}: this gate's pattern matched nothing, so it is grading no prose at all. "
        f"Either the sentences naming that collection were rewritten -- in which case repoint "
        f"the pattern -- or they were deleted, in which case delete this row."
    )

    for word in found:
        stated = NUMBER_WORDS.get(word.lower())
        assert stated is not None, (
            f"{relative}: prose states {word!r} where a number word was expected; either it is "
            f"a word this gate cannot read, or the sentence changed shape"
        )
        assert stated == expected, (
            f"{relative}: the prose says {word.upper()} ({stated}) but the collection it names "
            f"holds {expected}. The collection is the authority; correct the sentence."
        )
