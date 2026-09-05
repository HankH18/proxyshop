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


def expand_braces(pattern: str, cap: int = 64) -> list[str]:
    """``a/{b,c}/d`` -> ``["a/b/d", "a/c/d"]``, as ``swarmloop.py:_expand_braces`` does.

    Not decoration: without it, rewriting an owner's scope from ``docs/tests/**`` to
    ``docs/{tests}/**`` is a semantic NO-OP for the harness that actually enforces scope,
    and yet it silenced this gate completely. A braced entry already exists live — T-167's
    ``apps/trust/src/{scoring,reconcile,feedback,snapshot}/_binding.py``.
    """
    out = [pattern]
    while True:
        grown: list[str] = []
        changed = False
        for item in out:
            match = re.search(r"\{([^{}]*)\}", item)
            if match is None:
                grown.append(item)
                continue
            changed = True
            for alternative in match.group(1).split(","):
                grown.append(item[: match.start()] + alternative + item[match.end() :])
        out = grown[:cap]
        if not changed:
            return out


def scope_patterns(scope: Any) -> list[str]:
    """Every path pattern a ticket's ``scope`` actually grants.

    Four shapes, all measured against the live graph:

    * ``path:SUFFIX`` (40 entries) grants the part before the first colon;
    * a **space-bearing** entry grants its first token when that token looks like a path.
      28 live entries are shaped ``packages/store-agent/src/hooks/tools.py (price
      arithmetic)``, and an earlier draft rejected every one of them outright — which meant
      those 28 conferred zero ownership, and rewriting an owner's glob to ``docs/tests/**
      (runbook tests)`` silenced this gate while changing nothing about who owns what;
    * a genuinely prose entry ("ORCHESTRATION — measuring while the swarm runs") grants
      nothing, which is right: a ticket that names no files owns no files;
    * ``**`` is a catch-all and confers NO ownership, or every path in the repo would belong
      to the scaffold ticket. The arming test caps how many tickets may hold it, because
      widening an owner's scope to ``**`` is otherwise a way to make it own nothing.
    """
    patterns: list[str] = []
    for entry in scope or ():
        if not isinstance(entry, str):
            continue
        head = entry.split(":", 1)[0].strip()
        if not head:
            continue
        if " " in head:
            first = head.split()[0]
            if "/" not in first and "." not in first:
                continue
            head = first
        for pattern in expand_braces(_normalise(head)):
            if pattern and pattern != "**":
                patterns.append(pattern)
    return patterns


def scope_covers(path: str, scope: Any) -> bool:
    """``swarmloop.py:_scope_match`` semantics, reduced to what this gate needs.

    Case-folded on both sides; ``dir/**`` matches the bare directory as well as everything
    under it, which is what makes a respelt ``pytest docs/tests -q`` still a violation.
    """
    target = _normalise(path)
    for pattern in scope_patterns(scope):
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
    """Every NOT-YET-CLOSED ticket carrying a real gate — the ones a lane still has to build.

    ``status != "closed"``, not ``status == "open"``, and the difference is 8 of the 9
    violations this sweep finds. Only 66 tickets say ``"open"``; **124 carry ``status:
    null``** (findings mints omit the field) and 3 say ``"unknown"``. An open-only
    population left all 127 of those invisible, and it also handed the gate a one-word
    escape: setting the graded ticket's own status to anything but ``"open"`` removed it
    from the sweep. Closed tickets are excluded because the answer-key inversion can only
    bite a ticket that has not been built yet.
    """
    return [
        ticket
        for ticket in tickets
        if ticket.get("status") != "closed"
        and not str(ticket.get("verify", "")).startswith("false")
    ]


def ownership_violations(
    tickets: list[dict[str, Any]],
) -> list[tuple[str, str, list[str], list[str]]]:
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


def runs_whole_suite(verify: str) -> bool:
    """``verify.sh check``/``all`` or ``make verify`` — a gate that selects nothing in particular."""
    for segment in re.split(r"\s*(?:&&|\|\||;)\s*", verify):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        if any(w.endswith("verify.sh") for w in words) and {"check", "all"} & set(words):
            return True
        if "make" in words and "verify" in words:
            return True
    return False


def selector_violations(tickets: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Clauses B and C — a gate that names no grader cannot be owned, or repaired, at all.

    Clause A asks who owns the path a gate runs. Both of these exist because a gate can
    stop naming a path, at which point clause A has nothing to look at and goes quiet while
    the ownership defect is completely intact. Two spellings do that, and both were
    measured taking this sweep from red to green with one field:

    * **B** — ``pytest -k runbook -q``: a pytest segment that names no path selects by test
      name, so nothing it runs can be attributed to a file anyone owns.
    * **C** — ``make verify`` / ``./scripts/verify.sh check``: no pytest segment at all, so
      clause B never even looks. A whole-suite run is not a gate for one ticket; ``red-check``
      stamps it ``weak`` rather than ``red``, and ESC-015 established that a ``weak`` stamp
      is not closable, so refusing it here asks for nothing the harness does not already
      imply. At HEAD this fires on exactly two tickets besides the escape it closes.
    """
    found: list[tuple[str, str]] = []
    for ticket in graded_population(tickets):
        verify = str(ticket["verify"])
        if runs_whole_suite(verify):
            found.append(
                (ticket["id"], f"`{verify}` runs the whole suite and grades nothing it owns")
            )
            continue
        for segment in pytest_segments(verify):
            if not selection_operands(segment):
                found.append(
                    (ticket["id"], f"`{' '.join(segment)}` names no path, so it selects by -k")
                )
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
    # RATCHET, and it is the only thing that catches two one-field escapes clause A cannot
    # see: downgrading the graded ticket's verify to the placeholder, and flipping its
    # status to "closed". Either removes it from the population rather than repairing it,
    # and either drops this count. 84 at HEAD (42 open, 39 null, 3 "unknown"); it only ever
    # rises as gateless tickets acquire gates.
    assert len(population) >= 84, (
        f"{len(population)} not-yet-closed tickets carry a real gate; 84 did at HEAD. A "
        "graded ticket's gate was downgraded to the placeholder, or its status was flipped "
        "to closed, which removes it from this sweep rather than repairing it."
    )

    # A scope-degradation ceiling. Widening an owner's scope to `**` makes it own NOTHING
    # here (a catch-all confers no ownership), so it silences clause A while making the
    # real ownership problem worse. Exactly one ticket holds it at HEAD — T-000, the
    # scaffold root — so a second one is someone escaping rather than scoping.
    catchall = [t["id"] for t in tickets if list(t.get("scope") or ()) == ["**"]]
    assert len(catchall) <= 1, (
        f"{len(catchall)} tickets have a scope of exactly ['**'] ({catchall}); only the "
        "scaffold root should, and a catch-all confers no ownership, so this is how an "
        "owner stops owning the file it owns"
    )

    operands = [
        op
        for t in population
        for seg in pytest_segments(str(t["verify"]))
        for op in selection_operands(seg)
    ]
    # Measured at HEAD across the open population: 44 operand occurrences, 19 distinct. The
    # distinct count is the one worth pinning low — a graph that started spelling gates as
    # `-k` selections would drop it, and clause B is what turns that into a failure rather
    # than a quiet shrink, so this floor only has to catch the extractor breaking outright.
    assert len(operands) >= 30, f"the sweep extracted {len(operands)} path operands to check"
    assert len(set(operands)) >= 15, f"only {len(set(operands))} distinct operands; 19 at HEAD"

    owning = [
        t for t in tickets if any(isinstance(e, str) and "/" in e for e in t.get("scope") or ())
    ]
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
    assert scope_covers("tickets.json", ["tickets.json:T-087"]), (
        "a `path:SUFFIX` entry grants its prefix"
    )
    assert scope_covers("docs/tests/test_runbook.py", ["docs/{tests,demo}/**"]), (
        "brace expansion is gone, so respelling an owner's glob as `docs/{tests}/**` — a "
        "no-op for the harness that actually enforces scope — would silence clause A"
    )
    assert scope_covers(
        "packages/store-agent/src/hooks/tools.py",
        ["packages/store-agent/src/hooks/tools.py (price arithmetic)"],
    ), (
        "a space-bearing scope entry confers no ownership again; 28 live entries are shaped "
        "that way, and rewriting a glob as `docs/tests/** (runbook tests)` would silence it"
    )
    assert runs_whole_suite("PROXYSHOP_WORKER=15 make verify")
    assert runs_whole_suite("PROXYSHOP_WORKER=15 ./scripts/verify.sh check")
    assert not runs_whole_suite("PROXYSHOP_WORKER=15 uv run python -m pytest docs/tests/x.py -q")

    # The allowlists, in both directions.
    assert is_shared_grader(".swarm-loop/acceptance/test_e8_proofs.py")
    assert is_shared_grader("services/sim/tests/test_repro_open_tickets.py")
    assert not is_shared_grader("docs/tests/test_runbook.py")
    assert not is_shared_grader("docs/tests/test_runbook_executability.py")

    # Clause B's operand test, in both directions.
    assert selection_operands(shlex.split("uv run python -m pytest docs/tests/x.py -q")) == [
        "docs/tests/x.py"
    ]
    assert selection_operands(shlex.split("uv run python -m pytest -k runbook -q")) == [], (
        "a `-k`-only command now reports an operand, so clause B would stop firing on the "
        "spelling escape it exists to catch"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-262: not-yet-closed tickets that are graded by a file they cannot write. T-087's "
        "verify runs `docs/tests/test_runbook.py`, a path its own scope "
        "(`docs/demo/shopify-onboarding-extension.md`) forbids it to create and that sits "
        "inside open T-085's `docs/tests/**`, so it would inherit an unearned green from a "
        "test another lane wrote. Measured at HEAD: eleven, namely T-087, T-228, T-298, "
        "T-299, T-300, T-301, T-304, T-313 and T-314 by ownership, plus T-130 and T-134 "
        "whose gate is a whole-suite run that grades nothing they own. Remove this marker "
        "with the scope amendment"
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

    THE REFUSAL OF THE EXISTENCE CLAUSE IS NOT HYPOTHETICAL, and the sweep found the proof
    once its population was widened past ``status == "open"``. T-298, T-299, T-300 and
    T-301 all run ``proxyshop_support/tests/test_artifact_copyset.py``, a file none of them
    owns — and that file EXISTS, is collected, and its syntax tree already names all four
    ids. Their answer key has already been written by a third party. An invariant keyed on
    "the path does not exist yet" would report those four as clean precisely because the
    inversion is complete, which is the whole argument for asking about ownership instead.
    T-304, T-313 and T-314 name the same file and are worse off still: it does not mention
    them at all, so their gates select nothing.

    ONE ESCAPE IS OPEN AND IS NAMED HERE RATHER THAN LEFT TO BE DISCOVERED: this asks
    "does someone else own it", not "may I create it". Repointing T-087 at an UNOWNED path
    outside its own scope — ``docs/test_runbook.py``, say — goes green with the original
    complaint, a ticket naming a file its scope forbids it to create, fully intact. The
    strict own-scope form of the invariant closes that and fires on 90 tickets at HEAD,
    nearly all of them legitimately pointing at shared graders, so it is not a trade worth
    making today. The next person to read this should not believe the invariant is
    stronger than it is.

    Four one-field escapes WERE open in an earlier draft and are closed, each having been
    measured taking this test fully green with T-087's defect intact: closing the graded
    ticket, or downgrading its verify to the placeholder (both now caught by the
    population ratchet, 84 -> 83); rewriting the owner's glob as ``docs/{tests}/**`` or as
    ``docs/tests/** (runbook tests)``, neither of which changes ownership for the harness
    that enforces it; and replacing the gate with ``make verify`` or ``verify.sh check``,
    which left clause A nothing to look at.
    """
    tickets = _tickets()
    ownership = ownership_violations(tickets)
    selector = selector_violations(tickets)

    report = [
        f"  {ticket_id}: its gate runs `{operand}`, which its own scope {own} does not "
        f"grant, and which is owned by {', '.join(holders)}"
        for ticket_id, operand, own, holders in ownership
    ] + [
        f"  {ticket_id}: `{command}` names no path, so it selects by -k and grades nothing it owns"
        for ticket_id, command in selector
    ]

    assert not report, (
        f"{len(ownership) + len(selector)} open ticket(s) are graded by a file they cannot "
        "write, so their lane cannot create its own grader and a third party's test would "
        "decide whether they pass:\n" + "\n".join(report) + "\n\nRepair: narrow the owning "
        "ticket's scope, widen the graded ticket's scope to cover the file its gate runs, "
        "or repoint the gate at a path no ticket owns. Note this is a SCOPE-field change "
        "and is not in the pre-approved verify-field amendment class."
    )
