"""Reproductions for the orchestrator-facing tickets that carry a PLACEHOLDER gate.

Same mechanism as the per-service ``test_repro_open_tickets.py`` files: one
``xfail(strict=True)`` test per defect measured live at HEAD, so a normal run reports
``xfailed`` and exits 0 while the ticket's own gate runs ``--runxfail -k <id>`` and gets a
real ``1 failed``. ``strict=True`` turns the eventual repair into an XPASS *failure*, so
whoever fixes the defect must delete the marker.

What is different here, and it changes what a good test looks like: the subjects are the
INSTRUMENTS, not the product. T-160 is about ticket gates that cannot fail. A gate about
gates is easy to get wrong in one specific way — it can end up grading the patch that
landed rather than the property that was meant to hold — so every test below is written to
stay red when someone repairs the named instance and leaves the class alone. Where that is
claimed it is also demonstrated: see ``test_t160_...``'s docstring for the measured
"instance fixed, class alive" number.

Covered here: T-160 and T-210.

Nothing in this file touches product source.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

#: The repo root, reached from this file rather than from a hard-coded string so a moved
#: test cannot silently start scanning nothing.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The ticket graph. Read only — a gate about the ticket graph may not WRITE the ticket graph.
TICKETS_JSON = REPO_ROOT / "tickets.json"

#: The marker text a ticket carries when it has no gate yet.
GATE_PLACEHOLDER = "false  # NO GATE YET"

#: The marker expression ``scripts/verify.sh check`` runs the suite under (verify.sh:187).
#: Resolving a selection under any *other* expression would count a test that is deselected
#: at runtime as pointed-at, which is the marker-deselection false green already recorded
#: against this repo.
VERIFY_SH_MARKERS = "not needs_model and not slow"

#: ``T-123`` and friends.
_TICKET_ID = re.compile(r"T-\d{3}")

#: pytest flags that consume the NEXT word. Without this table ``-k t160 packages/llm``
#: would read ``t160`` as an operand, and ``-m docker apps/trust`` would read ``docker`` as
#: one. ``scripts/check_verify_contracts.py:408`` skips every ``-``-word and never learns
#: which flag takes a value, which is why this file parses the command itself rather than
#: importing that helper: it is also quote-blind (``-k "a/b or c"`` yields ``'"a/b'``).
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

#: Sentinel: this verify runs the WHOLE suite (``verify.sh check``/``all``, ``make verify``,
#: or a bare ``pytest`` with no operand).
WHOLE_SUITE = "<whole-suite>"


def _run_collection() -> list[str]:
    """Every test FILE pytest actually collects, under verify.sh's own marker expression.

    A subprocess, not an in-process ``pytest.main``: the parent session already has the
    socket guard armed and a rootdir configured, and re-entering collection in-process
    would measure this run's configuration rather than the repo's.

    Measured at HEAD: 5610 of 5611 tests collected across 141 files in 1.7s, so the cost of
    doing this properly instead of matching path strings is under two seconds.
    """
    env = dict(os.environ)
    env.setdefault("PROXYSHOP_WORKER", "0")
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            VERIFY_SH_MARKERS,
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    files = sorted({line.split("::", 1)[0].strip() for line in completed.stdout.splitlines() if "::" in line})
    if not files:
        raise AssertionError(
            "collection returned no test files, so every selection below would be empty and "
            f"the sweep would pass by measuring nothing.\nrc={completed.returncode}\n"
            f"stdout tail:\n{completed.stdout[-2000:]}\nstderr tail:\n{completed.stderr[-2000:]}"
        )
    return files


def ticket_ids_in_ast(source: str) -> set[str]:
    """Ticket ids a Python source names in its *syntax tree* — never in a comment.

    This is the anti-forgery core of T-160's gate and the reason it is AST rather than grep.
    A lexical scan makes ``# T-123`` on any line of any file under ``packages/llm`` into a
    T-123 grader, which turns the vacuous verify T-160 exists to remove into a passing one
    for the price of one comment. ``ast.parse`` discards comments entirely, so the cheapest
    forgery is not a comment but a real string literal or a renamed test — which is a source
    edit, in a file, that a lane repointing a verify field has no scope to make.

    Counted as naming the ticket: a function/class NAME, and any string constant — which is
    every docstring, every ``xfail(reason=...)`` and every assertion message.
    """
    ids: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ids |= set(_TICKET_ID.findall(node.name))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            ids |= set(_TICKET_ID.findall(node.value))
    return ids


def graders_by_ticket(files: list[str]) -> dict[str, set[str]]:
    """``{ticket_id: {collected test file that names it}}``.

    Built strictly from files pytest COLLECTS, which closes a second forgery: ``scripts/
    bootstrap.sh`` already contains the string "T-111" and is an operand of T-111's own
    verify, so a grader map built from "every committed file" would hand T-111 a free pass.
    A shell script is not collected and does not parse, so it cannot be a grader here.
    """
    graders: dict[str, set[str]] = {}
    for rel in files:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        for ticket_id in ticket_ids_in_ast(source):
            graders.setdefault(ticket_id, set()).add(rel)
    return graders


def pytest_selection(verify: str, files: list[str]) -> set[str] | str | None:
    """Resolve a verify command to the set of collected files it would run.

    Returns ``WHOLE_SUITE`` for a command that runs everything, ``None`` for a command that
    never invokes pytest at all (``npx vitest run …``), and otherwise the set of collected
    files the operands select.
    """
    selected: set[str] = set()
    invokes_pytest = False
    for segment in re.split(r"\s*(?:&&|\|\||;)\s*", verify):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        if not words:
            continue
        if any(w.endswith("verify.sh") for w in words) and {"check", "all"} & set(words):
            return WHOLE_SUITE
        if "make" in words and "verify" in words:
            return WHOLE_SUITE
        if not any(w == "pytest" or w.endswith("/pytest") for w in words):
            continue
        invokes_pytest = True
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
            if word.startswith("-") or "=" in word:
                index += 1
                continue
            operands.append(word)
            index += 1
        if not operands:
            return WHOLE_SUITE
        for operand in operands:
            head = operand.split("::", 1)[0]
            prefix = head.rstrip("/") + "/"
            selected |= {rel for rel in files if rel == head or rel.startswith(prefix)}
    return selected if invokes_pytest else None


def _scope_covers(rel: str, scope: Any) -> bool:
    """Does one of the ticket's scope entries cover this repo-relative path?

    Prose scope entries ("ORCHESTRATION — measuring while the swarm runs") grant nothing,
    which is correct: a ticket that names no files owns no files.
    """
    for entry in scope or ():
        if not isinstance(entry, str):
            continue
        head = entry.split(":", 1)[0].strip()
        if not head or " " in head or head == "**":
            continue
        base = head.rstrip("*").rstrip("/")
        if rel == head or (base and (rel == base or rel.startswith(base + "/"))):
            return True
    return False


def gate_violations(
    tickets: list[dict[str, Any]], files: list[str], graders: dict[str, set[str]]
) -> list[tuple[str, str, list[str]]]:
    """Every CLOSED ticket whose recorded gate cannot fail on the defect it graded.

    The property, stated once: a ticket that was closed on a gate must have been closable
    on it, so the command has to select at least one test COUPLED to the ticket — either a
    test that names the ticket, or a test living inside the ticket's own scope, which is
    the case for the many tickets whose fix and whose tests share a directory.

    ONE CLAUSE WAS DESIGNED IN AND THEN MEASURED OUT, and the reason belongs here because
    it is the difference between this gate and a plausible wrong one. The obvious extra
    rule is "a whole-suite verify (``verify.sh check``, ``make verify``) attributes nothing
    and therefore counts as selecting nothing"; ``red-check`` even agrees in spirit, since
    it stamps such a gate ``weak`` rather than ``red``. It is still the wrong rule HERE,
    because T-160's defect is a gate that passes IDENTICALLY with and without the fix, and
    a whole-suite run does not have that defect: measured on T-133, its grader
    ``apps/buyer/svc/tests/test_profile_identity_leaks.py`` contributes 21 tests with none
    deselected under ``-m "not needs_model and not slow"``, so if that grader goes red,
    ``verify.sh check`` goes red. Adding the clause turns this sweep from 2 violators into
    8 by flagging six tickets whose gates do discriminate. Weak attribution is a real
    complaint and a different one; it is reported in this lane's notes, not gated here.

    There is deliberately NO exemption field. An ``if ticket.get("bootstrap")`` escape
    hatch was written and removed: whatever its contract says elsewhere, inside this sweep
    it would be a one-word way to leave the population instead of repairing the gate.
    """
    violations: list[tuple[str, str, list[str]]] = []
    for ticket in tickets:
        if ticket.get("status") != "closed":
            continue
        verify = str(ticket.get("verify", ""))
        if verify.startswith("false"):
            continue
        selection = pytest_selection(verify, files)
        if selection is None:
            continue
        if selection == WHOLE_SUITE:
            selection = set(files)
        assert isinstance(selection, set)
        if selection & graders.get(ticket["id"], set()):
            continue
        if any(_scope_covers(rel, ticket.get("scope")) for rel in selection):
            continue
        violations.append(
            (
                ticket["id"],
                f"selects {len(selection)} collected file(s); none of them grades it and "
                f"none is inside its own scope {ticket.get('scope')}",
                sorted(graders.get(ticket["id"], set())),
            )
        )
    return violations


def _tickets() -> list[dict[str, Any]]:
    return list(json.loads(TICKETS_JSON.read_text(encoding="utf-8"))["tickets"])


# =====================================================================================
# T-160 — closed tickets whose recorded `verify` passes identically with and without
#          the defect it was supposed to grade
# =====================================================================================


def test_t160_the_gate_vacuity_sweep_is_armed() -> None:
    """Not xfail, and not optional: the T-160 test below is worthless without this.

    Six ways the sweep could pass while measuring nothing, all closed here. Three of them
    have actually happened in this repo — sweeps that went 6->0 of 8, 70->0 of 79 and
    48->0 of 66 rather than red — and T-160 is itself a ticket about gates that cannot
    fail, so a T-160 gate that went quiet would be the joke telling itself.

    The last assertion is the one that is not a count: it feeds the grader extractor a
    comment and a docstring and requires it to see only the docstring. That is the whole
    anti-forgery claim, checked rather than asserted in prose.
    """
    tickets = _tickets()
    assert len(tickets) >= 250, f"the graph parsed {len(tickets)} tickets; it holds ~292"

    closed = [t for t in tickets if t.get("status") == "closed"]
    assert len(closed) >= 80, f"only {len(closed)} closed tickets; the sweep grades closed ones"

    closed_gated = [t for t in closed if not str(t.get("verify", "")).startswith("false")]
    # RATCHET, and the reason it is a floor rather than a range: the cheapest way to make
    # the sweep below green without repairing anything is to downgrade an offending verify
    # back to the `false  # NO GATE YET` placeholder, which removes the ticket from the
    # population by deleting its gate. That drops this count; 57 was measured at HEAD.
    assert len(closed_gated) >= 57, (
        f"{len(closed_gated)} closed tickets carry a real verify; 57 did at HEAD. A closed "
        "ticket's gate was downgraded to the placeholder, which removes it from the sweep "
        "rather than repairing it."
    )

    files = _run_collection()
    assert len(files) >= 100, f"collection found {len(files)} test files; 141 at HEAD"

    graders = graders_by_ticket(files)
    assert len(graders) >= 120, f"{len(graders)} tickets have a grader; 157 at HEAD"

    population = [t for t in closed_gated if pytest_selection(str(t["verify"]), files) is not None]
    assert len(population) >= 40, f"the sweep would iterate {len(population)} tickets; 54 at HEAD"
    resolved = [
        t
        for t in population
        if isinstance(pytest_selection(str(t["verify"]), files), set)
        and pytest_selection(str(t["verify"]), files)
    ]
    assert len(resolved) >= 25, (
        f"only {len(resolved)} of {len(population)} verify commands resolved to a non-empty "
        "file set; the operand parser has stopped resolving selections and every ticket "
        "would look equally un-pointed"
    )

    bootstrap = [t["id"] for t in tickets if t.get("bootstrap") is True]
    assert len(bootstrap) <= 1, (
        f"{len(bootstrap)} tickets declare `bootstrap: true` ({bootstrap}); ESC-015 reserves "
        "that exemption for the graph's SOLE root. Widening it is how this gate would be "
        "escaped rather than satisfied."
    )

    assert ticket_ids_in_ast("# T-999 grades this\nvalue = 1\n") == set(), (
        "the grader extractor sees ticket ids in COMMENTS, so a one-line comment in any "
        "already-selected file forges a grader and the sweep below measures a substring "
        "instead of a test"
    )
    assert ticket_ids_in_ast('"""T-998 lives here."""\n') == {"T-998"}, (
        "the grader extractor no longer sees ticket ids in docstrings, so it would find no "
        "graders at all and the sweep would iterate an empty population"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-160: closed tickets whose recorded `verify` cannot fail in their own name — it "
        "either runs the whole suite, so no failure is attributable to the ticket, or it "
        "selects tests that neither grade the ticket nor live inside its scope. Measured at "
        "HEAD: 8 such tickets, including T-111 and T-123 by name. Remove this marker with "
        "the fix"
    ),
)
def test_t160_no_closed_ticket_was_closed_on_a_gate_that_cannot_fail() -> None:
    """A ticket that was closed on a gate must have been closable on that gate.

    T-160 measured three: ``./scripts/bootstrap.sh && ./scripts/verify.sh check`` (T-111)
    and ``bootstrap.sh && pytest packages/llm && python -c 'import contracts, llm, trust'``
    (T-123) both exit 0 with the defect present, and T-109's was green on the parent commit.
    The defect those two gate lives in ``scripts/bootstrap.sh``; the only test in the repo
    that exercises it is ``proxyshop_support/tests/test_bootstrap_provisioning.py``, which
    is not under ``packages/llm`` and is reached by ``verify.sh check`` only incidentally,
    as one of 5610 tests whose collective failure names no ticket.

    THIS TEST DELIBERATELY DOES NOT PARAMETRIZE OVER THOSE THREE. Retyping three strings
    would close exactly three gaming keys and leave the class alive, and the class is what
    T-160 is about. Measured, on an in-memory copy of the graph: repointing only T-109,
    T-111 and T-123 at their graders leaves SIX closed tickets still violating — T-000,
    T-112, T-118, T-122, T-129 and T-133 — so the instance can be fixed with this test
    still red. Repointing all eight takes it to zero. That gap is the difference between
    grading the patch and grading the property, and it is why the repair T-160 asks for is
    a verify-field amendment across the whole class, not three edits.

    T-109 is absent from today's violators and that is not an oversight: its
    ``pytest proxyshop_support -q`` half does select its grader, so it has self-repaired
    since the finding was recorded. A gate that still demanded a fix for T-109 would be
    grading the ticket text rather than the graph.
    """
    files = _run_collection()
    violations = gate_violations(_tickets(), files, graders_by_ticket(files))
    lines = [
        f"  {ticket_id}: {why}\n      tests that name it: {', '.join(owned) or '(none)'}"
        for ticket_id, why, owned in violations
    ]
    assert not violations, (
        f"{len(violations)} CLOSED ticket(s) carry a gate that cannot fail in their own "
        "name, so nothing about closing them distinguished the defect from its repair:\n"
        + "\n".join(lines)
        + "\n\nRepair: repoint each verify at one of the tests listed beside it (a "
        "verify-field amendment, the precedent being freeze-log amendment 10, which "
        "repointed ten other tickets the same way), or — for the graph's single root — "
        "declare `bootstrap: true` on it."
    )
