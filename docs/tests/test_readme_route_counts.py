"""The README's route counts, held to the census that measures them.

WHY THIS FILE EXISTS. ``README.md`` states a headline — "the six product services serve N
routes between them, and every one of the N is driven by at least one test" — and then a
table with a per-service count in it. Both are measurements of the tree, and nothing checked
them: the number went 57 -> 58 -> 59 over three commits, and on two of those three it was a
human noticing rather than a gate. Prose that outlived its subject is the most common defect
in this repo, and a route count is the cheapest possible instance of it, because the sentence
around it stays perfectly plausible while the number goes wrong.

WHAT IT DOES NOT DO. It checks the COUNTS in that sentence and the table — the two route
totals, the word that counts the services, and every per-service row — and nothing else about
them. A README that described the wrong routes in the right quantity would pass here; that is
what reading is for. What it forbids is the specific failure that keeps happening: someone
adds a served route or a service and the paragraph hundreds of lines away silently becomes
false.

The census is imported rather than shelled out to. It is a static AST walk over
``src/*/routes.py`` and imports no application, so this costs milliseconds and needs no
datastore, no port and no worker index.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from proxyshop_support.route_census import SERVICES, census

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"

#: The README's table keys its rows by repository path; the census keys its counts by service
#: name. This is the join, and it is spelled out rather than derived from ``Service.root`` so
#: that a service whose directory moves fails here loudly instead of matching nothing.
ROW_PATH_BY_SERVICE = {
    "buyer": "apps/buyer",
    "exchange": "apps/exchange",
    "merchant": "apps/merchant",
    "trust": "apps/trust",
    "ingest": "services/ingest",
    "store-agent": "packages/store-agent",
}


def measured() -> Counter[str]:
    """Routes per product service, as the census counts them. Dev doubles excluded."""
    served = [service for service in SERVICES if not service.dev_double]
    return Counter(route.service for route in census(served, repo_root=REPO_ROOT))


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_the_headline_states_the_measured_total() -> None:
    """Both halves of the headline sentence state one number, and it is the measured one."""
    text = readme_text()
    match = re.search(
        r"serve (\d+) routes between them, and\s+every one of the (\d+) is driven",
        text,
    )
    assert match is not None, (
        "README.md no longer carries the 'serve N routes between them, and every one of the "
        "N is driven' sentence in the shape this gate reads. If the sentence was rewritten, "
        "rewrite this pattern with it — do not delete the check."
    )
    stated, restated = int(match.group(1)), int(match.group(2))
    total = sum(measured().values())
    assert stated == total, (
        f"README.md says the six product services serve {stated} routes; the census counts "
        f"{total}. Run `./.venv/bin/python -m proxyshop_support.route_census` and correct the "
        f"README — the tree is the measurement, the sentence is the claim."
    )
    assert restated == stated, (
        f"the same sentence says {stated} in its first half and {restated} in its second."
    )


#: The census's service count, spelled the way the headline spells it. The sentence writes
#: that count as a word, so reading it back needs the word. A census that grows past this map
#: fails below rather than quietly skipping the check — the failure this whole file exists to
#: prevent is a check that stops checking while still reporting green.
SERVICE_COUNT_WORDS = {
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def test_the_headline_counts_the_measured_services() -> None:
    """The word "six" in that sentence is a count too, and this opens README.md to read it.

    Two assertions doing two jobs. The first pins the join table to the census, so a service
    added to one and not the other is caught. The second READS THE SENTENCE, and it is here
    because without it this function passed against a README saying "The seventeen product
    services serve 59 routes" — a gate that could not fail the case it was named for.
    """
    served = [service for service in SERVICES if not service.dev_double]
    assert len(ROW_PATH_BY_SERVICE) == len(served), (
        "a product service was added to or removed from the census. Update "
        "ROW_PATH_BY_SERVICE, the README's 'six product services', and its table together."
    )
    word = SERVICE_COUNT_WORDS.get(len(served))
    assert word is not None, (
        f"the census counts {len(served)} product services and SERVICE_COUNT_WORDS spells no "
        f"word for that. Add it rather than deleting the assertion below it."
    )
    stated = re.search(r"\*\*The (\w+) product services serve", readme_text())
    assert stated is not None, (
        "README.md no longer carries the '**The <word> product services serve' headline in "
        "the shape this gate reads. If the sentence was rewritten, rewrite this pattern with "
        "it — do not delete the check."
    )
    assert stated.group(1) == word, (
        f"README.md says the {stated.group(1)} product services serve those routes; the "
        f"census counts {len(served)}, which this file spells '{word}'."
    )


@pytest.mark.parametrize("service", sorted(ROW_PATH_BY_SERVICE))
def test_each_table_row_states_its_own_measured_count(service: str) -> None:
    """The `| `apps/buyer` | 8081 | 18 | ...` column, row by row.

    Parametrised so a wrong row names the service that is wrong, rather than failing the
    whole table on the first one and hiding the rest.
    """
    row_path = ROW_PATH_BY_SERVICE[service]
    pattern = re.compile(
        rf"^\|\s*`{re.escape(row_path)}`\s*\|\s*\d+\s*\|\s*(\d+)\s*\|", re.MULTILINE
    )
    match = pattern.search(readme_text())
    assert match is not None, (
        f"README.md has no service-table row for `{row_path}` in the expected "
        f"`| path | port | routes | ... |` shape."
    )
    stated = int(match.group(1))
    counted = measured()[service]
    assert stated == counted, (
        f"README.md's `{row_path}` row claims {stated} route(s); the census counts "
        f"{counted} for the `{service}` service."
    )


def test_the_rows_sum_to_the_headline() -> None:
    """The arithmetic a reader does on the table has to come out at the headline."""
    text = readme_text()
    headline = re.search(r"serve (\d+) routes between them", text)
    assert headline is not None
    rows = 0
    for row_path in ROW_PATH_BY_SERVICE.values():
        match = re.search(
            rf"^\|\s*`{re.escape(row_path)}`\s*\|\s*\d+\s*\|\s*(\d+)\s*\|", text, re.MULTILINE
        )
        assert match is not None, f"no table row for `{row_path}`"
        rows += int(match.group(1))
    assert rows == int(headline.group(1)), (
        f"the {len(ROW_PATH_BY_SERVICE)} table rows sum to {rows}, and the headline says "
        f"{headline.group(1)}."
    )
