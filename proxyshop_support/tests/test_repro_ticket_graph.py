"""Reproductions for defects in the TICKET GRAPH itself, rather than in the product.

A gate about gates is a different animal from a gate about code, and this repo has already
shipped the failure mode: T-204 was a CLOSED ticket whose verify named one of the two nodes
that graded it, the named one passed, the unnamed one was still red, and it was found by
accident. So the test below is written against the property, not against the instance —
every escape it does not close is named in its docstring rather than left for someone to
discover.

This file READS ``tickets.json`` and never writes it. A gate about the ticket graph may not
be the thing that edits the ticket graph.

Covered here: T-262.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TICKETS_JSON = REPO_ROOT / "tickets.json"

#: A ticket with no gate yet.
GATE_PLACEHOLDER = "false  # NO GATE YET"

#: Flags that consume the next word, so ``-k runbook`` does not read ``runbook`` as a path.
_VALUE_FLAGS = frozenset(
    {
        "-k",
        "-m",
        "-p",
        "-n",
        "-c",
        "-o",
        "--deselect",
        "--ignore",
        "--ignore-glob",
        "--rootdir",
        "--confcutdir",
        "--timeout",
        "--maxfail",
    }
)

#: Words that are never operands even though they carry no leading dash.
_NOT_OPERANDS = frozenset({"pytest", "npx", "vitest", "run", "make", "python", "uv", "-m"})

#: Shared graders that no single ticket owns, allowlisted BY NAME and never by an existence
#: test. The distinction is the whole point of this gate: the defect's own worst case — a
#: third party creating the grader inside its own scope — SATISFIES an existence test, so an
#: invariant keyed on "does the file exist yet" is green exactly when the answer-key
#: inversion completes. An allowlist is auditable; existence is a race the defect wins.
_SHARED_GRADER_DIRS = ("swarm-loop/acceptance/",)
_SHARED_GRADER_BASENAME = "test_repro_*.py"


def _normalise(path: str) -> str:
    """``swarmloop.py:_scope_match``'s normalisation: backslashes, then strip leading ``./``.

    ``lstrip("./")`` removes every leading ``.`` and ``/`` character, not just the pair, so
    ``.swarm-loop/acceptance/x.py`` normalises to ``swarm-loop/acceptance/x.py``. A naive
    ``startswith(".swarm-loop/")`` allowlist would therefore never fire; this is why the
    constant above is spelled without the dot.
    """
    return path.strip().replace("\\", "/").lstrip("./").casefold()


def is_shared_grader(path: str) -> bool:
    """A path no ticket owns because every ticket may be graded by it."""
    normalised = _normalise(path)
    if any(normalised.startswith(prefix) for prefix in _SHARED_GRADER_DIRS):
        return True
    return fnmatch.fnmatch(Path(normalised).name, _SHARED_GRADER_BASENAME)


def pytest_segments(verify: str) -> list[list[str]]:
    """The ``&&``/``||``/``;``-separated command segments that invoke pytest directly.

    ``./scripts/verify.sh check`` and ``make verify`` run pytest too, but they carry no
    pytest token and select nothing by path, so they are not selection commands and are
    deliberately absent here.
    """
    segments: list[list[str]] = []
    for segment in re.split(r"\s*(?:&&|\|\||;)\s*", verify):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        if any(word == "pytest" or word.endswith("/pytest") for word in words):
            segments.append(words)
    return segments


def selection_operands(words: list[str]) -> list[str]:
    """Path operands a single pytest segment names.

    The ``"/" in word`` test is load-bearing and must not be "improved" away: with a plain
    "any non-flag word after pytest" rule, ``-k runbook`` donates ``runbook`` as an operand
    and clause B below stops detecting the ``-k``-only escape it exists to detect.
    """
    index = 0
    while index < len(words) and not (words[index] == "pytest" or words[index].endswith("/pytest")):
        index += 1
    index += 1
    operands: list[str] = []
    while index < len(words):
        word = words[index]
        if word in _VALUE_FLAGS:
            index += 2
            continue
        if word.startswith("-") or "=" in word or word in _NOT_OPERANDS:
            index += 1
            continue
        if "/" in word:
            operands.append(word.split("::", 1)[0])
        index += 1
    return operands


def scope_covers(path: str, scope: Any) -> bool:
    """``swarmloop.py:_scope_match`` semantics, reduced to what this gate needs.

    Case-folded on both sides; ``dir/**`` matches the bare directory as well as everything
    under it, which is what makes a respelt ``pytest docs/tests -q`` still a violation;
    ``**`` is a catch-all (T-000's whole scope) and confers no OWNERSHIP, or every path in
    the repo would belong to the scaffold ticket; a ``path:SUFFIX`` entry grants the part
    before the first colon; a prose entry grants nothing.
    """
    target = _normalise(path)
    for entry in scope or ():
        if not isinstance(entry, str):
            continue
        head = entry.split(":", 1)[0].strip()
        if not head or " " in head:
            continue
        pattern = _normalise(head)
        if not pattern or pattern == "**":
            continue
        if pattern.endswith("/**") and (target == pattern[:-3] or target.startswith(pattern[:-2])):
            return True
        if target == pattern or fnmatch.fnmatch(target, pattern):
            return True
        base = pattern.rstrip("*").rstrip("/")
        if base and target.startswith(base + "/"):
            return True
    return False


def _tickets() -> list[dict[str, Any]]:
    return list(json.loads(TICKETS_JSON.read_text(encoding="utf-8"))["tickets"])


def graded_population(tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open tickets carrying a real gate — the ones that still have a lane to be built by."""
    return [
        ticket
        for ticket in tickets
        if ticket.get("status") == "open" and not str(ticket.get("verify", "")).startswith("false")
    ]


def ownership_violations(tickets: list[dict[str, Any]]) -> list[tuple[str, str, list[str], list[str]]]:
    """Clause A — a ticket's gate must not be graded by a file another ticket owns."""
    owners: list[tuple[str, dict[str, Any]]] = [(t["id"], t) for t in tickets]
    found: list[tuple[str, str, list[str], list[str]]] = []
    for ticket in graded_population(tickets):
        own_scope = ticket.get("scope")
        for segment in pytest_segments(str(ticket["verify"])):
            for operand in selection_operands(segment):
                if is_shared_grader(operand) or scope_covers(operand, own_scope):
                    continue
                holders = [
                    f"{other_id} [{other.get('status') or 'no status'}]"
                    for other_id, other in owners
                    if other_id != ticket["id"] and scope_covers(operand, other.get("scope"))
                ]
                if holders:
                    found.append((ticket["id"], operand, list(own_scope or ()), holders))
    return found


def selector_violations(tickets: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Clause B — a pytest command that names no path selects by ``-k`` and owns nothing.

    Without this, clause A is escaped by pure spelling: ``pytest -k runbook -q`` extracts
    zero operands, so an ungrantable grader becomes invisible rather than repaired.
    """
    found: list[tuple[str, str]] = []
    for ticket in graded_population(tickets):
        for segment in pytest_segments(str(ticket["verify"])):
            if not selection_operands(segment):
                found.append((ticket["id"], " ".join(segment)))
    return found


# =====================================================================================
# T-262 — a ticket whose gate names a file its own scope forbids it to create, and that
#          file sits inside a DIFFERENT ticket's scope
# =====================================================================================


def test_t262_the_grader_ownership_sweep_is_armed() -> None:
    """Not xfail: the sweep below is worthless if it iterates nothing, and this repo has
    watched three sweeps go quiet rather than red (6->0 of 8, 70->0 of 79, 48->0 of 66).

    Also armed here is the thing a count cannot check — that the two allowlist predicates
    and the scope matcher still recognise the shapes they were written for. The
    ``docs/tests/**`` vs bare ``docs/tests`` case is the load-bearing one: if the
    ``/**``-matches-the-directory branch ever stops working, the sweep still iterates and
    still reports a number, and the respelt-as-a-directory escape opens silently.
    """
    tickets = _tickets()
    assert len(tickets) >= 250, f"the graph parsed {len(tickets)} tickets; it holds ~292"

    population = graded_population(tickets)
    assert len(population) >= 40, f"only {len(population)} open tickets carry a real gate"

    operands = [op for t in population for seg in pytest_segments(str(t["verify"])) for op in selection_operands(seg)]
    # Measured at HEAD across the open population: 44 operand occurrences, 19 distinct. The
    # distinct count is the one worth pinning low — a graph that started spelling gates as
    # `-k` selections would drop it, and clause B is what turns that into a failure rather
    # than a quiet shrink, so this floor only has to catch the extractor breaking outright.
    assert len(operands) >= 30, f"the sweep extracted {len(operands)} path operands to check"
    assert len(set(operands)) >= 15, f"only {len(set(operands))} distinct operands; 19 at HEAD"

    owning = [t for t in tickets if any(isinstance(e, str) and "/" in e for e in t.get("scope") or ())]
    assert len(owning) >= 100, (
        f"only {len(owning)} tickets carry a path-shaped scope entry, so almost nothing owns "
        "anything and the ownership question below can never be answered yes"
    )

    # The matcher, on the exact shapes this gate turns on.
    assert scope_covers("docs/tests/test_runbook.py", ["docs/tests/**"])
    assert scope_covers("docs/tests", ["docs/tests/**"]), (
        "`dir/**` no longer matches the bare directory, so respelling a gate as "
        "`pytest docs/tests -q` would slip past clause A"
    )
    assert not scope_covers("docs/tests/test_runbook.py", ["**"]), "`**` must confer no ownership"
    assert not scope_covers("docs/tests/test_runbook.py", ["ORCHESTRATION — a prose scope"])
    assert scope_covers("tickets.json", ["tickets.json:T-087"]), "a `path:SUFFIX` entry grants its prefix"

    # The allowlists, in both directions.
    assert is_shared_grader(".swarm-loop/acceptance/test_e8_proofs.py")
    assert is_shared_grader("services/sim/tests/test_repro_open_tickets.py")
    assert not is_shared_grader("docs/tests/test_runbook.py")
    assert not is_shared_grader("docs/tests/test_runbook_executability.py")

    # Clause B's operand test, in both directions.
    assert selection_operands(shlex.split("uv run python -m pytest docs/tests/x.py -q")) == ["docs/tests/x.py"]
    assert selection_operands(shlex.split("uv run python -m pytest -k runbook -q")) == [], (
        "a `-k`-only command now reports an operand, so clause B would stop firing on the "
        "spelling escape it exists to catch"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-262: T-087's verify runs `docs/tests/test_runbook.py`, a path its own scope "
        "(`docs/demo/shopify-onboarding-extension.md`) forbids it to create and that sits "
        "inside open T-085's `docs/tests/**` — so T-087 cannot write its own grader and "
        "would inherit an unearned green from a test another lane wrote. Remove this "
        "marker with the scope amendment"
    ),
)
def test_t262_no_open_ticket_is_graded_by_a_file_another_ticket_owns() -> None:
    """A ticket must be able to write the test that grades it.

    Reproduced live once already: the lane building T-082/T-085 wrote a 16,651-byte
    ``docs/tests/test_runbook.py`` inside its own granted ``docs/tests/**`` and was
    redirected by hand before it committed. That is the answer-key problem inverted — a
    third party writing the key, and the graded ticket inheriting a green it never earned.

    WHAT THIS TEST DELIBERATELY DOES NOT DO, and why it is the whole design. The obvious
    invariant is "the verify names a path that does not exist and that the ticket's scope
    forbids it to create". Measured, that narrows 78 candidate paths to exactly one, so it
    looks like a precise gate. It is the wrong gate: the defect's own worst case satisfies
    it. T-085 is still open and still owns the glob, and a 108,959-byte
    ``docs/tests/test_runbook_executability.py`` already sits there — one rename from
    making the path exist, at which point T-087 leaves the violation set and this gate
    reports green at the exact moment the inversion completes. So the question asked here
    is OWNERSHIP, never existence, and the three variants of the defect all stay red:

    * the file gets created -> red (nothing here calls ``exists()``);
    * the verify is repointed at ``docs/tests/test_runbook_executability.py``, which does
      exist -> red, because it is inside the same foreign glob;
    * the verify is respelt ``pytest docs/tests -q`` -> red, because ``docs/tests/**``
      matches the bare directory, and a ``-k``-only respelling is caught by clause B.

    All three defensible repairs turn it green, and each was measured on an in-memory copy
    of the graph: narrow T-085's ``docs/tests/**``; widen T-087's scope to cover the file
    its gate runs; or repoint the gate at a path no ticket owns. Marking T-085 CLOSED does
    NOT work, which is why ownership is asked of tickets of any status rather than of open
    ones only.

    The third repair is narrower than it sounds and the measured detail is worth carrying:
    ``docs/test_runbook.py`` is unowned and green, and any ``test_repro_*.py`` basename is
    green through the shared-grader allowlist, but ``docs/demo/test_runbook.py`` is NOT —
    T-315's scope is the bare directory ``docs/demo/``, and a directory entry is read here
    as owning what is under it. ``proxyshop_support/tests/test_runbook_shape.py`` is not
    either: closed T-122's scope reaches it through ``proxyshop_support/**``.

    ONE ESCAPE IS OPEN AND IS NAMED HERE RATHER THAN LEFT TO BE DISCOVERED: this asks
    "does someone else own it", not "may I create it". Repointing T-087 at an UNOWNED path
    outside its own scope — ``docs/test_runbook.py``, say — goes green with the original
    complaint, a ticket naming a file its scope forbids it to create, fully intact. The
    strict own-scope form of the invariant closes that and fires on 90 tickets at HEAD,
    nearly all of them legitimately pointing at shared graders, so it is not a trade worth
    making today. The next person to read this should not believe the invariant is
    stronger than it is.
    """
    tickets = _tickets()
    ownership = ownership_violations(tickets)
    selector = selector_violations(tickets)

    report = [
        f"  {ticket_id}: its gate runs `{operand}`, which its own scope {own} does not "
        f"grant, and which is owned by {', '.join(holders)}"
        for ticket_id, operand, own, holders in ownership
    ] + [f"  {ticket_id}: `{command}` names no path, so it selects by -k and grades nothing it owns" for ticket_id, command in selector]

    assert not report, (
        f"{len(ownership) + len(selector)} open ticket(s) are graded by a file they cannot "
        "write, so their lane cannot create its own grader and a third party's test would "
        "decide whether they pass:\n" + "\n".join(report) + "\n\nRepair: narrow the owning "
        "ticket's scope, widen the graded ticket's scope to cover the file its gate runs, "
        "or repoint the gate at a path no ticket owns. Note this is a SCOPE-field change "
        "and is not in the pre-approved verify-field amendment class."
    )
