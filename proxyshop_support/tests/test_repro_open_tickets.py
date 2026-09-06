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
claimed it is also demonstrated: T-160's sweep stays red after the three tickets its own
ticket names are repointed, because it finds a fourth instance the ticket never named.

Covered here: T-160 and T-210.

Nothing in this file touches product source.
"""

from __future__ import annotations

import ast
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
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

#: ``T-123`` and friends. The word boundaries are not decoration and match the harness's
#: own ``\bT-\d+\b``: without them ``T-1234`` and ``REPORT-1234`` both register as T-123, so
#: an unrelated four-digit id anywhere in any collected test file forges a grader.
_TICKET_ID = re.compile(r"\bT-\d{3}\b")

#: This module's own repo-relative path.
_SELF = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

#: The collected test files whose SUBJECT IS THE TICKET GRAPH. They are excluded from the
#: grader map below, and this module is only one of them.
#:
#: WHY A NAMED SET AND NOT A PROPERTY. The property is easy to state — "a file that is
#: itself a ticket-graph sweep should not grade tickets it merely describes" — and three
#: machine-checkable forms of it were measured against the live collection. All three fail:
#:
#: * "reads ``tickets.json``" catches ``apps/exchange/tests/test_accept_denials.py``, which
#:   scans the graph in ONE test (:767) and genuinely grades seven tickets with the rest of
#:   the file. Excluding it would delete seven real graders.
#: * "reads ``tickets.json`` and names N+ ticket ids" cannot be thresholded.
#:   ``apps/trust/tests/test_repro_open_tickets.py`` on main reads the graph (to check a
#:   ticket's declared scope globs) and names NINETEEN ids, more than either sweep — while
#:   being a per-ticket reproduction file that really does grade all nineteen.
#: * "names many ids but has test functions for almost none of them" — ids in string
#:   constants minus ids in ``test_tNNN_`` function names — comes closest and still does not
#:   separate: 15 for the ticket-graph sweep against 14 for that same trust file. One ticket
#:   of margin is not a rule, it is a false red waiting for the next merge.
#:
#: And an exclusion is not fail-safe the way the shared-grader allowlist next door is:
#: removing a grader can DELETE a violation, because a whole-suite gate is a violation only
#: ``if owned``. So a property any file could satisfy with a source edit would be a way to
#: make a ticket's violation disappear, not merely a way to be ignored. A named set can only
#: be widened by editing this file, which is visible in the diff of the gate itself.
#:
#: What the arming test pins instead is the direction that fails OPEN: every path listed here
#: must still be collected, because a renamed sweep leaves a dead entry behind and rejoins
#: the grader map silently. The escape that remains — a THIRD sweep added later and not
#: listed — is named there rather than left to be discovered.
#:
#: ``services/sim/tests/test_repro_open_tickets.py`` is deliberately NOT here, and it was
#: proposed. It names exactly one ticket, T-242, and it is T-242's own reproduction — it
#: exercises the ticket rather than describing it. Measured: adding it strips T-242's ONLY
#: grader, and since T-242's verify runs that very file while its scope is
#: ``services/sim/src/runner.py``, the sweep would report T-242 as "has no dedicated grader,
#: and nothing it selects is inside its own scope" the moment T-242 closes. That is the gate
#: inventing a violation, which is the same category of error as missing one.
_DESCRIBES_THE_GRAPH = frozenset(
    {
        "proxyshop_support/tests/test_repro_open_tickets.py",
        "proxyshop_support/tests/test_repro_ticket_graph.py",
    }
)

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

#: Words that are never path operands even without a leading dash.
_NOT_OPERANDS = frozenset({"pytest", "npx", "vitest", "run", "make", "python", "uv", "exec", "-m"})

#: Sentinel: this verify runs the WHOLE suite (``verify.sh check``/``all``, ``make verify``,
#: or a bare ``pytest`` carrying neither a flag nor an operand).
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
    # Never default to 0: that index is the scorer's, and it is the sole isolation key for
    # both the Postgres database name and the Redis logical DB, so a stray child badged
    # with it can collide with a scored measurement. Inherit, then fall back to the gate
    # worker the graph's own verify strings use, then to a fixed non-reserved index.
    if not env.get("PROXYSHOP_WORKER"):
        env["PROXYSHOP_WORKER"] = env.get("PROXYSHOP_GATE_WORKER") or "9"
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
    files = sorted(
        {line.split("::", 1)[0].strip() for line in completed.stdout.splitlines() if "::" in line}
    )
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

    EVERY TICKET-GRAPH SWEEP IS EXCLUDED FROM THIS MAP, and the reason is the whole reason a
    gate about gates is delicate. A test about the ticket graph names tickets in order to
    DESCRIBE them; the AST rule cannot tell that from naming them in order to test them.
    Left in, THIS file counted as a grader for nine closed tickets it does not exercise —
    T-000, T-109, T-111, T-112, T-118, T-122, T-123, T-129 and T-133 — so the sweep's own
    failure message recommended repointing T-112's verify at the sweep, and doing so turned
    the sweep green. The violator set is unchanged by the exclusion (measured both ways),
    because every one of those tickets has a real grader elsewhere; what changes is that
    the gate can no longer be satisfied by pointing a ticket at the gate.

    THE EXCLUSION WAS ONE PATH AND SHOULD ALWAYS HAVE BEEN A SET, and the file that proved it
    arrived in the very same merge. ``proxyshop_support/tests/test_repro_ticket_graph.py``
    names SIXTEEN ticket ids in prose — T-082, T-085, T-087, T-122, T-130, T-134, T-167,
    T-204, T-228, T-262, T-298, T-301, T-304, T-313, T-314, T-315 — and contains no test that
    exercises any of them, so it registered as a grader for all sixteen. That is verbatim the
    forgery the self-exclusion exists to prevent, reintroduced next door and reachable by a
    single verify-field edit.

    Measured on an in-memory copy of the graph, with T-122 first restored to the whole-suite
    gate it carried when it was a violator:

        T-122 verify = `./scripts/verify.sh check`        violations [T-122, T-133]
        T-122 verify = `pytest .../test_repro_ticket_graph.py`
                       exclusion = this file only         violations [T-133]
                       exclusion = both graph sweeps      violations [T-122, T-133]

    i.e. the forgery cured a violator with a file whose only mention of T-122 is one prose
    sentence, and the promoted exclusion refuses it. On the live graph the violator set is
    unchanged either way ([T-133]) — the ten tickets that lose their only "grader" here
    (T-085, T-087, T-130, T-134, T-167, T-262, T-304, T-313, T-314, T-315) are all
    not-yet-closed, and this sweep grades closed tickets only.
    """
    graders: dict[str, set[str]] = {}
    for rel in files:
        if rel in _DESCRIBES_THE_GRAPH:
            continue
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
        while index < len(words) and not (
            words[index] == "pytest" or words[index].endswith("/pytest")
        ):
            index += 1
        index += 1
        operands: list[str] = []
        narrowed = False
        while index < len(words):
            word = words[index]
            if word in _VALUE_FLAGS:
                narrowed = True
                index += 2
                continue
            if word.startswith("-") or "=" in word:
                index += 1
                continue
            if word in _NOT_OPERANDS:
                index += 1
                continue
            operands.append(word)
            index += 1
        if not operands:
            # A bare `pytest` with neither an operand nor a narrowing flag really is the
            # whole suite. `pytest -k schema -q` is NOT, and conflating the two was a
            # measured hole: it made a command that selects a handful of tests by name
            # score as maximally coupled, which took the sweep green on a one-field edit.
            if not narrowed:
                return WHOLE_SUITE
            continue
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


def verify_operands(verify: str) -> list[str]:
    """Every path-like operand the command names, whatever the runner.

    Deliberately not restricted to pytest segments: a verify of ``npx vitest run
    packages/contracts`` names a real selector and a verify of ``true`` does not, and the
    sweep has to be able to say so. The ``(REPO_ROOT / head).exists()`` arm is why a bare
    ``pytest proxyshop_support -q`` counts — a slash-free operand is invisible to a
    "contains /" test, and that dropped operand is exactly the half of T-120's and T-109's
    gates that selects their grader. Existence is safe HERE and only here: this asks
    whether a selector was written at all, never who owns it, so it cannot reintroduce the
    fail-open that a "does the path exist yet" ownership test would.
    """
    operands: list[str] = []
    for segment in re.split(r"\s*(?:&&|\|\||;)\s*", verify):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        index = 0
        while index < len(words):
            word = words[index]
            if word in _VALUE_FLAGS:
                index += 2
                continue
            if word.startswith("-") or "=" in word or word in _NOT_OPERANDS:
                index += 1
                continue
            head = word.split("::", 1)[0]
            if "/" in head or (REPO_ROOT / head).exists():
                operands.append(head)
            index += 1
    return operands


def gate_violations(
    tickets: list[dict[str, Any]], files: list[str], graders: dict[str, set[str]]
) -> list[tuple[str, str, list[str]]]:
    """Every CLOSED ticket whose recorded gate cannot fail on the defect it graded.

    The property: a ticket closed on a gate must have been closable on it, so the command
    has to name a selector at all, and that selector has to be COUPLED to the ticket.

    Three clauses, and every one of them is a hole an adversarial reviewer measured open
    in an earlier draft of this sweep. Each was demonstrated by a ONE-FIELD edit to
    ``tickets.json`` that took the sweep fully green with both violators' defects intact —
    and the cheapest of them were verify-field edits, i.e. the very amendment class this
    test's own failure message invites. They are recorded here because "the gate can be
    silenced by the repair it recommends" is the exact shape of the defect T-160 is about.

    * **Names no selector.** ``verify: "true"``, ``bash -c true``, ``npx vitest run`` with
      no operand. The earlier draft dropped any ticket whose verify carried no pytest
      token, so it left the population entirely; the placeholder ratchet could not see it
      because ``"true"`` does not start with ``"false"``.
    * **``-k``-only.** ``pytest -k schema -q`` names zero paths. The earlier draft
      classified "no operands" as a whole-suite run and then expanded it to every collected
      file, so a command selecting a handful of tests by name scored as maximally coupled.
      Only a bare ``pytest`` with no flags AND no operands is a whole-suite run.
    * **Whole-suite.** ``./scripts/verify.sh check`` and ``make verify`` are now violations
      whenever the ticket has a dedicated grader, and this REVERSES a call I made earlier
      on this same sweep. The argument for accepting them is real — measured on T-133, its
      grader contributes 21 non-deselected tests, so if that grader reddens, ``verify.sh
      check`` reddens. But accepting them means a narrow-and-wrong gate can be repaired
      INTO a whole-suite gate and go green, which is verbatim T-111's recorded shape, and
      it is a verify-field edit. ``red-check`` already stamps such a gate ``weak`` rather
      than ``red``, and ESC-015 established that a ``weak`` stamp is not closable, so
      demanding a repoint asks for nothing the harness does not already imply.

    Coupling itself is asymmetric on purpose, and that closes a fourth hole: adding one
    broad glob such as ``apps/**`` to a violator's ``scope`` used to cure it, because
    own-scope locality was accepted as an alternative to naming a grader. Locality is now
    only a FALLBACK for a ticket that has NO dedicated grader — where there is nothing
    better to point at — so a ticket that has one must actually select it.

    Two population escapes are deliberately NOT handled here, because they are the
    arming test's job and it catches both: downgrading a verify to the placeholder, and
    nulling a ticket's ``status``. Either takes the count of closed tickets carrying a real
    gate from 57 to 48, and the ratchet's floor is 57.

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
        owned = graders.get(ticket["id"], set())
        selection = pytest_selection(verify, files)
        whole_suite = selection == WHOLE_SUITE
        operands = verify_operands(verify)
        if not operands and not whole_suite:
            violations.append(
                (
                    ticket["id"],
                    "names no selector at all, so it cannot fail for this ticket's reason",
                    sorted(owned),
                )
            )
            continue
        if whole_suite:
            if owned:
                violations.append(
                    (
                        ticket["id"],
                        "runs the WHOLE suite, so no failure is attributable to it even "
                        "though it has a dedicated grader",
                        sorted(owned),
                    )
                )
            continue
        selected = selection if isinstance(selection, set) else set()
        if owned and selection is not None:
            if selected & owned:
                continue
            violations.append(
                (
                    ticket["id"],
                    f"selects {len(selected)} collected file(s), none of which grades it",
                    sorted(owned),
                )
            )
            continue
        if any(_scope_covers(rel, ticket.get("scope")) for rel in selected) or any(
            _scope_covers(operand, ticket.get("scope")) for operand in operands
        ):
            continue
        violations.append(
            (
                ticket["id"],
                "has no dedicated grader, and nothing it selects is inside its own scope "
                f"{ticket.get('scope')}",
                sorted(owned),
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
    assert len(graders) >= 120, f"{len(graders)} tickets have a grader; 166 at HEAD"

    # THE GRAPH-SWEEP EXCLUSION, armed in the only direction that can fail open. An entry
    # that no longer names a collected file is a DEAD entry: the renamed sweep rejoins the
    # grader map and starts forging graders again, silently, with the count above unchanged.
    # So every entry must still be collected, and this module must still be in the set under
    # its current name.
    assert _SELF in _DESCRIBES_THE_GRAPH, (
        f"this module is now {_SELF} and is no longer in _DESCRIBES_THE_GRAPH, so it grades "
        "the nine closed tickets it merely describes again"
    )
    for rel in sorted(_DESCRIBES_THE_GRAPH):
        assert rel in files, (
            f"{rel} is excluded from the grader map but pytest does not collect it, so the "
            "entry is dead — a renamed or moved graph sweep is back in the map and forging "
            f"graders. Collected files: {len(files)}"
        )
    # And the forgery itself: the ticket-graph sweep names 16 ids in prose and exercises
    # none of them. None of the 16 may have it as a grader.
    _sweep = "proxyshop_support/tests/test_repro_ticket_graph.py"
    _named = ticket_ids_in_ast((REPO_ROOT / _sweep).read_text(encoding="utf-8"))
    assert len(_named) >= 10, (
        f"the ticket-graph sweep now names only {len(_named)} ticket ids ({sorted(_named)}); "
        "16 at HEAD. If it has stopped describing the graph this exclusion is measuring "
        "nothing, and if it was renamed the check above is the one that fires."
    )
    assert not [tid for tid in _named if _sweep in graders.get(tid, set())], (
        f"{_sweep} is registered as a grader for tickets it only names in prose: "
        f"{sorted(tid for tid in _named if _sweep in graders.get(tid, set()))}"
    )
    # NO COMPLETENESS ASSERTION ON THE SET, and that is a measured decision rather than an
    # omission — see `_DESCRIBES_THE_GRAPH` for the numbers. Three candidate properties for
    # "this file is a graph sweep" were tried against the live collection and every one of
    # them either misses a sweep or catches a real grader, so an assertion built on any of
    # them would be a false red on the next merge. The set is therefore maintained by hand,
    # and the checks above cover the direction that fails OPEN (a listed path going dead).
    # THE ESCAPE THIS LEAVES, named rather than left to be discovered: a THIRD ticket-graph
    # sweep added later joins the grader map silently and forges graders for every ticket it
    # describes, exactly as test_repro_ticket_graph.py did. Whoever adds one must add it here.

    graded = [t for t in closed_gated if t["id"] in graders_by_ticket(files)]
    assert len(graded) >= 40, (
        f"only {len(graded)} of {len(closed_gated)} closed gated tickets have a dedicated "
        "grader (48 at HEAD); the sweep's main clause would fall through to the own-scope "
        "fallback for nearly everything"
    )
    population = [t for t in closed_gated if pytest_selection(str(t["verify"]), files) is not None]
    assert len(population) >= 40, f"the sweep would iterate {len(population)} tickets; 54 at HEAD"
    # The operand extractor has to see selectors that carry no slash, or T-109 and T-120
    # look like they name nothing and the "names no selector" clause fires on two gates
    # that are fine.
    assert verify_operands("PROXYSHOP_WORKER=15 uv run python -m pytest proxyshop_support -q") == [
        "proxyshop_support"
    ], "a slash-free operand naming a real directory is invisible to the operand extractor"
    assert verify_operands("PROXYSHOP_WORKER=15 uv run python -m pytest -k schema -q") == [], (
        "a `-k`-only command now reports an operand, so the clause that catches it is dead"
    )
    # The PARAMETERISED worker form, which arrived in freeze-log amendment 22 and rewrote
    # the verify field of all 130 gated tickets — the exact field this sweep reads. The
    # extractor must see straight through it: the whole `PROXYSHOP_WORKER=...` word is
    # skipped because it contains `=`, so the `${...}` never reaches the operand list.
    _parameterised = (
        "export PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-2} && ./scripts/bootstrap.sh "
        "&& ./.venv/bin/python -m pytest packages/llm -q"
    )
    assert verify_operands(_parameterised) == [
        "./scripts/bootstrap.sh",
        "./.venv/bin/python",
        "packages/llm",
    ], "the parameterised worker form is leaking into the operand list"
    assert not any("PROXYSHOP" in operand for operand in verify_operands(_parameterised)), (
        "an operand carrying the worker expression means the extractor is reading the "
        "environment prefix as a selector"
    )
    assert (
        pytest_selection("PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-5} make verify", files)
        == WHOLE_SUITE
    ), "the parameterised worker form hides `make verify` from whole-suite detection"
    assert pytest_selection("uv run python -m pytest -k schema -q", files) == set(), (
        "a `-k`-only command is being resolved as a whole-suite run again, which scores it "
        "as maximally coupled — the measured one-field escape"
    )
    assert pytest_selection("PROXYSHOP_WORKER=15 ./scripts/verify.sh check", files) == WHOLE_SUITE
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


# MARKER REMOVED because the defect it tracked is FIXED, which is what its own final
# clause instructed ("Remove this marker with the fix"). It read, verbatim:
#
#   "T-160: closed tickets whose recorded `verify` cannot fail for their own reason —
#    it names no selector, or selects no test that grades the ticket, or runs the whole
#    suite so that no failure is attributable to it. Nine when this gate was written
#    (T-000, T-010, T-111, T-112, T-118, T-122, T-123, T-129, T-133); re-measured after
#    the orchestrator's repointing amendment, ONE remains — T-133 ..."
#
# What it encodes: a ticket closed on a gate must have been closable on that gate.
# That requirement is UNCHANGED and this test still enforces it — only the expectation
# of failure is removed, and no assertion below is touched.
#
# The discriminating question, answered: would this test still be wrong if the change
# were reverted? NO — revert the T-133 repoint and it fails again, correctly, and the
# marker would belong back. The marker is not wrong in principle; it is now STALE,
# because the last of its nine offenders was repaired. Leaving it is not the safe
# option: xfail(strict=True) turns a now-passing test into XPASS(strict) -> FAILED,
# which reds `make verify` and takes the frozen build_succeeds metric from 1 to 0.
#
# Fixed by freeze --amend 33 under the ESC-028 verify-field class: T-133's gate moved
# off the whole-suite `verify.sh check` and onto its own grader,
# apps/buyer/svc/tests/test_profile_identity_leaks.py (633 lines, names T-133 five
# times, inside T-133's own declared scope, 21 tests selected and passing at HEAD).
def test_t160_no_closed_ticket_was_closed_on_a_gate_that_cannot_fail() -> None:
    """A ticket that was closed on a gate must have been closable on that gate.

    T-160 names three. T-123's gate is
    ``bootstrap.sh && pytest packages/llm && python -c 'import contracts, llm, trust'``: the
    defect it grades lives in ``scripts/bootstrap.sh``, the only test in the repo that
    exercises that is ``proxyshop_support/tests/test_bootstrap_provisioning.py``, and that
    file is not under ``packages/llm``, so the command exits 0 with the defect present.

    THIS TEST DELIBERATELY DOES NOT PARAMETRIZE OVER THE THREE THE TICKET NAMES, and the
    measurement is the argument. Two of the three do not reproduce as recorded, and a gate
    that demanded a fix for them would be grading the ticket's text rather than the graph:

    * T-109's ``pytest proxyshop_support -q`` half does select its grader
      (``test_reachability_per_service.py``, 12 collected items covering its acceptance),
      so it has SELF-REPAIRED since the finding.
    * T-111's ``bootstrap.sh && verify.sh check`` reaches its grader only incidentally, as
      one of 5610 suite tests — but it does reach it. Measured on the grader itself: 21
      collected tests, none deselected by ``-m "not needs_model and not slow"``, so if the
      bootstrap defect returns that gate goes red. Weak attribution is a real complaint and
      a different one from the defect T-160 states.

    What the sweep finds instead is T-123 plus **eight instances the ticket never named**.
    The clearest is T-112: its verify runs ``apps/trust/tests/test_schema_grants.py``,
    whose role-password block grades T-110, whose two static tests read only
    ``db/init/00-roles.sql`` and pass with T-112's defect live, and whose three docker tests
    inject ``PROXYSHOP_ROLE_PASSWORD`` into a private container without ever invoking
    ``docker compose`` — so T-112's first acceptance, that compose forwards the variable, is
    structurally ungradeable there. Every real grader for it sits in a file its verify does
    not select. T-010 is the same shape: its verify runs ``packages/contracts``, its own
    scope, while the two tests written to grade it are in ``apps/buyer`` and ``e2e``. The
    remaining six (T-000, T-111, T-118, T-122, T-129, T-133) close on ``verify.sh check`` or
    ``make verify`` while having a dedicated grader to point at — the gates ``red-check``
    already stamps ``weak``.

    So the instance-versus-class proof, measured on an in-memory copy of the graph:
    repointing the two of the three tickets T-160 names that are actually violators leaves
    **seven still violating** — T-000, T-010, T-112, T-118, T-122, T-129, T-133. Repointing
    all nine takes it to zero. The property outlives the instance, which is the whole reason
    this is a sweep over all 57 closed gated tickets rather than three assertions.

    THE SWEEP HAS SINCE SURVIVED A BULK REWRITE OF THE FIELD IT GRADES, and that is worth
    more than any of the adversarial mutations above because nobody aimed it. An adversarial
    review had found this gate could be silenced by a one-field ``tickets.json`` edit, and
    noted the cheapest such edits were VERIFY-FIELD edits — the pre-approved amendment
    class. Freeze-log amendment 22 then made exactly that edit, to all 130 gated tickets at
    once, for an unrelated and legitimate reason: every ``PROXYSHOP_WORKER=15`` became
    ``PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-N}`` so that concurrent lanes stop sharing
    one Postgres database and one Redis logical DB. Not an adversary silencing the gate —
    the orchestrator's routine maintenance doing it as a side effect.

    Re-measured across that merge, the violator set is UNCHANGED: the same nine ids, for the
    same reasons, and the two ratchet floors read exactly 57 and 84 across it. (Both have
    since moved with the graph — 59 closed gated tickets here, and the sibling module's
    population floor re-baselined from 84 to 94 after it was found carrying ten tickets of
    slack.) The reason it
    held is narrow and worth naming, because it is the difference between surviving and
    getting lucky: ``verify_operands`` scans every word from index 0 and skips any word
    containing ``=``, so the entire ``PROXYSHOP_WORKER=...`` token — parameter expansion and
    all — is discarded before any operand is read. (``pytest_selection`` carries the same
    clause, but there it is redundant: that scan only starts after the ``pytest`` token, so
    position discards the prefix first. The distinction matters because the ``=`` sentence
    was copied into the sibling ticket-graph module, where position is the only reason it
    holds.) Nothing in this file parses a worker index, and the arming test now pins that
    with the parameterised form spelled out literally.

    AND IT HAS SINCE BEEN REPAIRED DOWN TO ONE, which is what a sweep over the property
    rather than the instance is for. The orchestrator's repointing amendment fixed eight of
    the nine; re-measured here the violator set is exactly ``[T-133]`` — its gate is
    ``verify.sh check`` while ``apps/buyer/svc/tests/test_profile_identity_leaks.py`` is a
    dedicated grader it could point at instead. The count moved because the graph was
    repaired, not because the sweep went quiet: the arming test's floors (57 closed gated
    tickets, 100 collected files, 120 tickets with a grader) all still read above their
    thresholds at 59, 147 and 166.
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
        + "\n\nRepair: repoint each verify at one of the tests listed beside it — a "
        "verify-field amendment, the precedent being freeze-log amendment 10, which "
        "repointed ten other tickets the same way."
    )


# =====================================================================================
# T-210 — a Neo4j lock timeout is a non-zero `make verify`, indistinguishable to a
#          machine from a product failure, so build_succeeds records 0 for a machine
#          condition
# =====================================================================================

#: Worker ids this gate's child processes are allowed to claim. 0 is the scorer's and 15 is
#: the ticket-gate index; a child badged with either would collide with a real measurement,
#: since the worker id picks the Postgres database name and the Redis logical DB.
_CHILD_WORKERS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)

#: What the child pytest module looks like. ``neo4j_flock`` is imported from the repo, not
#: reimplemented: the lock primitive under test has to be the real one.
_CHILD_MODULE = """
import pytest
from proxyshop_support.neo4j_lock import neo4j_flock

@pytest.fixture
def graph_guard():
    with neo4j_flock(timeout={timeout!r}, poll={poll!r}, report=lambda message: None):
        yield True

{bodies}
"""

_HOLDER = """
import sys, time
from proxyshop_support.neo4j_lock import neo4j_flock
lock, sentinel, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
with neo4j_flock(timeout=30.0, poll=0.05, path=lock, report=lambda message: None):
    with open(sentinel, "w", encoding="utf-8") as handle:
        handle.write("held")
    time.sleep(hold)
"""


def _child_source(shape: dict[str, Any]) -> str:
    """One pytest module in the drawn shape: ``n_pass`` plain tests, one lock-taking test
    at ``guard_at``, and — when ``failing_at`` is not None — one test that asserts False."""
    bodies = []
    for index in range(shape["n_tests"]):
        name = f"test_{shape['names'][index]}"
        if index == shape["guard_at"]:
            bodies.append(f"def {name}(graph_guard):\n    assert graph_guard\n")
        elif index == shape["failing_at"]:
            bodies.append(f"def {name}():\n    assert False, {shape['message']!r}\n")
        else:
            bodies.append(f"def {name}():\n    assert True\n")
    return _CHILD_MODULE.format(
        timeout=shape["timeout"], poll=shape["poll"], bodies="\n".join(bodies)
    )


def _run_child(path: Path, lock: Path, worker: int) -> int:
    """Exit status of one child pytest run, which is all the metric harness ever sees.

    ``cwd`` is the repo root and the module lives under it, so pytest loads the REAL root
    ``conftest.py`` and the real ``pyproject.toml`` ini. That is deliberate and it is what
    makes this gate satisfiable: the only unfrozen places a fix can live are
    ``conftest.py`` and ``proxyshop_support/neo4j_lock.py``, and a child that loaded a
    hand-built conftest instead would grade a fixture of this test's own making — the
    "graded a fixture rather than the system" failure this repo has already shipped once.
    """
    env = dict(os.environ)
    env["PROXYSHOP_WORKER"] = str(worker)
    env["PROXYSHOP_NEO4J_LOCK"] = str(lock)
    env["PROXYSHOP_NEO4J_LOCK_LOG"] = "0"
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            str(path.relative_to(REPO_ROOT)),
            "-q",
            "-p",
            "no:cacheprovider",
            "--tb=no",
            "-ra",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return completed.returncode


def _reap(holder: subprocess.Popen[bytes]) -> None:
    """Make sure the lock holder is gone, and never let its teardown mask a real result.

    Both parts were measured problems in an earlier draft. A bare ``wait(timeout=30)`` in a
    ``finally`` raises ``TimeoutExpired`` if SIGTERM is ignored, which REPLACES whatever the
    test was about to report with a teardown error — and leaks a process still holding the
    scratch flock, so every later case in the sweep would see contention it did not create.
    """
    holder.terminate()
    try:
        holder.wait(timeout=30)
    except subprocess.TimeoutExpired:
        holder.kill()
        try:
            holder.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - the OS has bigger problems
            pass


def _draw_shapes(count: int) -> list[dict[str, Any]]:
    """``count`` case shapes: pinned adversarial ones first, then SystemRandom draws.

    SystemRandom and not a seeded ``Random``: a pinned seed makes this a parametrized probe
    in a costume — the same handful of cases every run, which is exactly the thing that
    cannot notice a repair keyed on one of them. What varies is the SHAPE (how many tests,
    which one takes the lock, which one fails, their names and therefore their collection
    order, the worker id, the lock filename, the timeout and the poll interval), not just a
    value inside a fixed shape.
    """
    entropy = random.SystemRandom()
    shapes: list[dict[str, Any]] = []
    # Pinned: the guard first and the failure last, the guard last and the failure first,
    # and a single-test module. These are the orderings a fix keyed on item order would
    # get wrong, and leaving them to chance means sometimes not drawing them.
    pinned = [(3, 0, 2), (3, 2, 0), (2, 1, 0), (1, 0, None)]
    for index in range(count):
        if index < len(pinned):
            n_tests, guard_at, failing_at = pinned[index]
        else:
            # From 2, never 1, and that bound is a defect a control caught rather than a
            # taste: a one-test module has no slot left for the failing test, so it
            # contributes no product-failure leg, and drawing `randint(1, 5)` made the
            # count of usable cases binomial around 16 against a threshold of 15. One
            # control run duly failed on "only 14 cases carry a product defect" — the gate
            # going red for a reason that was not the defect, which is the exact failure
            # mode this lane's tickets are about. Now exactly one case (pinned) lacks a
            # defect leg and the count is 19 every time.
            n_tests = entropy.randint(2, 5)
            guard_at = entropy.randrange(n_tests)
            others = [i for i in range(n_tests) if i != guard_at]
            failing_at = entropy.choice(others)
        shapes.append(
            {
                "n_tests": n_tests,
                "guard_at": guard_at,
                "failing_at": failing_at,
                "names": [
                    f"{entropy.choice('abcdefghijklmnopqrstuvwxyz')}{entropy.randrange(10**6):06d}"
                    for _ in range(n_tests)
                ],
                "message": f"simulated product defect {entropy.randrange(10**9)}",
                "timeout": round(entropy.uniform(0.25, 0.6), 3),
                "poll": round(entropy.uniform(0.02, 0.09), 3),
                "worker": entropy.choice(_CHILD_WORKERS),
                "lock_name": f"t210-{os.getpid()}-{index}-{entropy.randrange(10**9)}.lock",
            }
        )
    return shapes


def _measure(shape: dict[str, Any], workdir: Path, scratch: Path) -> dict[str, int | bool]:
    """Run the three legs of one case and report only what a machine could read."""
    lock = scratch / shape["lock_name"]
    module = workdir / f"test_lane_{shape['names'][0]}.py"

    module.write_text(_child_source({**shape, "failing_at": None}), encoding="utf-8")
    green_rc = _run_child(module, lock, shape["worker"])

    held = False
    sentinel = scratch / (shape["lock_name"] + ".held")
    holder_script = scratch / "hold_the_lock.py"
    holder_script.write_text(_HOLDER, encoding="utf-8")
    holder = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(holder_script), str(lock), str(sentinel), "20"],
        cwd=str(REPO_ROOT),
        env={
            **os.environ,
            "PROXYSHOP_WORKER": str(shape["worker"]),
            "PROXYSHOP_NEO4J_LOCK_LOG": "0",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if sentinel.exists():
                held = True
                break
            time.sleep(0.02)
        contention_rc = _run_child(module, lock, shape["worker"]) if held else -1
    finally:
        _reap(holder)

    if shape["failing_at"] is not None:
        module.write_text(_child_source(shape), encoding="utf-8")
        product_rc = _run_child(module, lock, shape["worker"])
    else:
        product_rc = -1
    return {"green": green_rc, "contention": contention_rc, "product": product_rc, "held": held}


def test_t210_the_lock_attribution_sweep_is_armed() -> None:
    """Not xfail, and the separation matters: under ``xfail(strict=True)`` ANY exception in
    the graded body reads as ``xfailed``, i.e. green, so a probe that had stopped working
    would be indistinguishable from the defect it is supposed to detect.

    Two of these are the vacuity traps this specific gate has. The first: the real
    ``_neo4j_guard`` calls ``_require_services("neo4j-bolt")`` BEFORE taking the lock, so
    with the stack down a contention case SKIPS and exits 0 — byte-identical to green, and
    the whole sweep passes while measuring nothing. This gate therefore uses
    ``neo4j_flock`` directly rather than the guard fixture, and asserts below that a
    contended acquisition really does raise; what it gives up is coverage of the
    reachability probe, which is not what T-210 is about. The second: the holder must
    actually hold. If the holder process fails to take the flock, the "contention" leg is
    just another green run, so every case asserts its own sentinel.
    """
    shapes = _draw_shapes(20)
    assert len(shapes) >= 20, f"only {len(shapes)} cases drawn"
    assert len({tuple(s["names"]) for s in shapes}) >= 15, "the draw is not varying the shapes"
    assert len({s["n_tests"] for s in shapes}) >= 3, "every case has the same test count"
    assert len({s["guard_at"] for s in shapes}) >= 2, "the lock-taking test is always in one slot"
    # 10, not 20, and not 15 either: the timeouts come from `round(uniform(.25,.6), 3)`,
    # which is 351 possible values, so 20 draws collide by the birthday argument. Measured
    # over 2000 draws the minimum distinct count was 16, i.e. a threshold of 15 sits ONE
    # collision from a spurious red — the same near-threshold flakiness a control already
    # caught in this gate's product-defect count. Ten distinct values out of twenty is
    # ample evidence the draw is live and needs eleven collisions to fail by chance.
    assert len({s["timeout"] for s in shapes}) >= 10, "the timeouts are not being drawn"

    # The primitive really is the repo's, and a contended acquisition really does raise.
    from proxyshop_support import neo4j_lock

    assert neo4j_lock.DEFAULT_TIMEOUT > 0
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw)
        lock = scratch / "armed.lock"
        holder_script = scratch / "hold_the_lock.py"
        holder_script.write_text(_HOLDER, encoding="utf-8")
        sentinel = scratch / "armed.held"
        holder = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, str(holder_script), str(lock), str(sentinel), "20"],
            cwd=str(REPO_ROOT),
            env={**os.environ, "PROXYSHOP_WORKER": "6", "PROXYSHOP_NEO4J_LOCK_LOG": "0"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and not sentinel.exists():
                time.sleep(0.02)
            assert sentinel.exists(), "the holder subprocess never took the scratch flock"
            with pytest.raises(neo4j_lock.Neo4jLockTimeout):
                with neo4j_lock.neo4j_flock(
                    timeout=0.3, poll=0.05, path=lock, report=lambda _m: None
                ):
                    pass
        finally:
            holder.terminate()
            holder.wait(timeout=30)


def test_t210_lock_contention_and_a_product_defect_do_not_share_an_exit_status() -> None:
    """Three outcomes, three exit statuses. That is the whole property.

    THE MARKER IS GONE BECAUSE THE DEFECT IS CLOSED. `proxyshop_support/lock_exit_status.py` had
    carried the whole mechanism -- exit status 77 and its four fail-closed evidence conditions --
    since it was written, while the root `conftest.py` imported nothing from it, so a run that
    failed only because another worker held the Neo4j lock exited 1 exactly like a run that failed
    on a real product defect, and the frozen `build_succeeds` probe recorded 0 for a machine
    condition. The remaining work was never the mechanism; it was registering its hooks.

    Note for whoever touches this next: registering `pytest_sessionfinish` ALONE is a wiring that
    looks right and does nothing -- the module's own docstring says so, and it is measured: the
    full hook set gives green 0 / contention 77 / defect 1, while sessionfinish alone leaves
    contention at 1. Verified here under `--runxfail` before this marker was removed.

    The metric this defect corrupts is a zero/non-zero test on a child process, so the
    exit status is the ONLY channel that counts, and the property is stated on it alone.
    That choice is the measured correction to the obvious design. Asserting instead that
    the three states are "distinguishable by exit status and output stream" is GREEN ON DAY
    ONE and would XPASS: a lock timeout is raised in fixture SETUP, so pytest already
    prints ``ERROR <nodeid>`` and ``N passed, 1 error`` for contention against ``FAILED``
    and ``1 failed, N passed`` for a defect. Measured, both harnesses agreeing: contention
    (rc=1, ERROR), defect (rc=1, FAILED), green (rc=0). Those markers are pytest's, not the
    repo's, so a gate keyed on them measures pytest and calls it a fix.

    Neither existing token can carry the property either, and both were measured rather
    than assumed. ``Neo4jLockTimeout`` reaches stdout only inside a default traceback: it
    is absent under ``--tb=no`` and the short-summary line truncates it to
    ``proxyshop_support.neo4j_lock.Neo4jLockTime...`` at 80 columns, which is what
    ``verify.sh``'s ``tee`` gets. And ``[neo4j-lock]`` — the one string the repo emits
    deliberately — is printed by any contended WAIT, including one that succeeds, so a run
    whose only failure is a genuine product bug carries it too.

    So: exit status alone, one fixed rule across every case, never the drawn test name or
    message text. That last clause is the T-233 lesson — keying on per-case text closes one
    cheat and leaves the class alive — and it is why the assertions below are made over the
    SET of statuses collected across all 20 cases rather than case by case.

    Every defensible repair passes and only the wrong outcome fails. The property does not
    name a mechanism: a distinct status for a machine condition, or making contention wait
    until it wins so it never fails at all, both satisfy it. What it refuses is the two
    failures being the same number, and — through the second assertion — the banned repair
    of skipping instead of failing, which the ticket rules out because a skip is invisible
    in the metrics. Measured as satisfiable, so this is not an unsatisfiable gate: an
    evidence-gated ``pytest_sessionfinish`` in the unfrozen root ``conftest.py`` that
    re-stamps ``session.exitstatus`` only when the sole failure is a ``Neo4jLockTimeout``
    yields 0 / 1 / 77, and 77 survives ``scripts/verify.sh``'s ``run_pytest`` unchanged
    (``rc=$PIPESTATUS[0]`` recovered from behind the ``tee``, then ``return "$rc"``) even
    though that file is hash-frozen.
    Whoever writes it must avoid 5 (verify.sh remaps it to 1, laundering the machine
    condition back into a product failure) and 2 (verify.sh's own fatals), must not fire
    when a real defect is also present, and must not fire unconditionally or the
    empty-suite gate dies with it.

    HONEST LIMIT, recorded here so the fix cannot overclaim: ``build_succeeds`` is
    ``if make verify; then echo 1; else echo 0; fi``. It reads zero-vs-non-zero and never
    the value, so a distinct status does NOT stop the false zeros — six of them are already
    in history.csv. What it buys is machine triage: an orchestrator reading the child can
    tell a machine condition from a defect without a human. The cure for the zeros is
    scheduler-level, which the ticket says itself and which no lane can reach.
    """
    # A UNIQUE directory per run, and DELIBERATELY NO SWEEP OF OLD ONES. Both halves of
    # that were mistakes I made in turn and had measured back at me.
    #
    # The name has to be unique because this test's own control harness calls it four times
    # in ONE process, so a `.t210-<pid>` name had every run sharing a directory and one
    # run's teardown deleting another's module.
    #
    # Then the stale-sweep that came with it — `for stale in REPO_ROOT.glob(".t210-*")` —
    # was worse, and it is worth spelling out because it is the exact failure this file is
    # about. It had no age check and no ownership check, so a second run's PROLOGUE deleted
    # a concurrent first run's in-flight directory and its live child module; the first run
    # then raised FileNotFoundError. Under `--runxfail` that is a red for the wrong reason,
    # and under a normal run `xfail(strict=True)` reports ANY exception as `xfailed`, so the
    # collision would have been completely invisible in `make verify`. Leftovers need no
    # sweeping anyway: the leading dot keeps them outside `norecursedirs = [".*"]` and they
    # are not under `testpaths`, so the repo's own collection can never pick them up.
    workdir = Path(tempfile.mkdtemp(prefix=".t210-", dir=REPO_ROOT))
    try:
        with tempfile.TemporaryDirectory() as raw:
            scratch = Path(raw)
            shapes = _draw_shapes(20)
            assert len(shapes) >= 20, "the sweep would iterate fewer than 20 cases"
            results = [(shape, _measure(shape, workdir, scratch)) for shape in shapes]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # Arming, inside the body as well as beside it: under `--runxfail -k t210` the gate
    # command selects both tests, and a dead probe must be a failure in either.
    assert all(outcome["held"] for _shape, outcome in results), (
        "a holder subprocess never took the scratch flock, so its 'contention' leg was "
        "just another uncontended run: "
        + str([s["lock_name"] for s, o in results if not o["held"]])
    )
    assert all(outcome["green"] == 0 for _shape, outcome in results), (
        "an uncontended run of the generated module did not exit 0, so the harness itself "
        "is broken and every comparison below is meaningless: "
        + str([(s["names"][0], o["green"]) for s, o in results])
    )
    with_defect = [(s, o) for s, o in results if s["failing_at"] is not None]
    # 19 of 20 by construction: only the pinned single-test case has no slot for a failing
    # test. A threshold below that would let a broken draw shrink the comparison silently.
    assert len(with_defect) >= 18, f"only {len(with_defect)} cases carry a product defect"
    assert all(o["product"] != 0 for _s, o in with_defect), (
        "a run containing a failing test exited 0: " + str([o["product"] for _s, o in with_defect])
    )

    green_statuses = {o["green"] for _s, o in results}
    contention_statuses = {o["contention"] for _s, o in results}
    product_statuses = {o["product"] for _s, o in with_defect}

    assert len(contention_statuses) == 1, (
        "lock contention does not produce ONE exit status across the drawn shapes "
        f"({sorted(contention_statuses)}), so whatever separates it is keyed on the shape "
        "of the run rather than on the condition"
    )
    assert not (contention_statuses & product_statuses), (
        "a run that failed ONLY because another worker held the Neo4j lock exits "
        f"{sorted(contention_statuses)}, and a run that failed on a real product defect "
        f"exits {sorted(product_statuses)} — the same status. Nothing reading the child "
        "process can tell a machine condition from a defect, so `make verify` is non-zero "
        "either way and the frozen build_succeeds probe records 0 for a busy lock.\n"
        "Repair (measured as satisfiable, and both frozen files stay untouched): give the "
        "machine condition its own exit status from inside the pytest process — an "
        "evidence-gated `pytest_sessionfinish` in conftest.py that re-stamps "
        "`session.exitstatus` only when the run's sole failure is a Neo4jLockTimeout — or "
        "make a contended acquisition wait until it wins so it never fails at all. Do NOT "
        "use 5 or 2, do not fire when a real failure is also present, and do not skip "
        "instead of failing: a skip is invisible in the metrics."
    )
    assert not (contention_statuses & green_statuses), (
        "lock contention now exits with the same status as a clean green run "
        f"({sorted(contention_statuses)}), which is the one repair this ticket rules out: "
        "a machine condition that reports success is invisible in the metrics rather than "
        "merely misattributed"
    )
