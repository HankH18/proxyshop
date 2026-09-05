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

#: Ways of spelling "the repo root" that a shell expands to the same directory this gate
#: reads paths relative to. Every one of them names the SAME FILE as a bare repo-relative
#: operand while looking nothing like it to a string comparison, which is the whole reason
#: they are here rather than in a style guide: ``pytest "$PWD/docs/tests/test_runbook.py"``
#: runs the identical third-party-owned file, and before this was normalised it took
#: ``scope_covers`` past every owner and the ownership set from [T-087, T-228] to [].
_PWD_PREFIXES = ("$pwd/", "${pwd}/", "$(pwd)/", "`pwd`/")


def _repo_relative(path: str) -> str:
    """One spelling for a path, whatever the shell would have expanded it from.

    Absolute and ``$PWD``-prefixed operands under this tree are rewritten to the
    repo-relative form. A path that is absolute but genuinely OUTSIDE this tree is returned
    unchanged rather than re-rooted into the repo, which would invent an owner for a file the
    repo does not contain. (``_normalise``'s ``lstrip("./")`` still strips its leading slash
    afterwards; that is ``swarmloop.py``'s own behaviour and is left alone here.)
    """
    text = path.strip().replace("\\", "/")
    # Sliced comparison rather than `lowered.startswith(...)`: case-folding is not
    # length-preserving in general, so an index taken from the folded string is not a valid
    # index into the original.
    for prefix in _PWD_PREFIXES:
        if text[: len(prefix)].casefold() == prefix:
            return text[len(prefix) :]
    root = str(REPO_ROOT).replace("\\", "/") + "/"
    if text[: len(root)].casefold() == root.casefold():
        return text[len(root) :]
    return text


def _normalise(path: str) -> str:
    """``swarmloop.py:_scope_match``'s normalisation: backslashes, then strip leading ``./``.

    ``lstrip("./")`` removes every leading ``.`` and ``/`` character, not just the pair, so
    ``.swarm-loop/acceptance/x.py`` normalises to ``swarm-loop/acceptance/x.py``. A naive
    ``startswith(".swarm-loop/")`` allowlist would therefore never fire; this is why the
    constant above is spelled without the dot.

    ``_repo_relative`` runs FIRST and is not cosmetic: ``lstrip("./")`` on an absolute path
    eats only the leading slash, so ``/abs/path/to/repo/docs/tests/x.py`` used to normalise
    to ``users/…/docs/tests/x.py`` and match no owner at all.
    """
    return _repo_relative(path).lstrip("./").casefold()


def graders_named_by(tickets: list[dict[str, Any]]) -> dict[str, set[str]]:
    """``{path: {ticket ids whose verify names it}}`` — how SHARED each grader actually is."""
    named: dict[str, set[str]] = {}
    for ticket in tickets:
        verify = str(ticket.get("verify", ""))
        if verify.startswith("false"):
            continue
        for segment in pytest_segments(verify):
            for operand in selection_operands(segment):
                named.setdefault(_normalise(operand), set()).add(ticket["id"])
    return named


def is_shared_grader(path: str, named_by: dict[str, set[str]] | None = None) -> bool:
    """A path no single ticket owns, because it is a gate several tickets are graded by.

    Three tests, all by explicit rule and NEVER by asking whether the file exists — the
    defect's own worst case satisfies an existence test, so keying on existence reports
    green exactly when the answer-key inversion completes.

    The third test is the principled one and the other two are conventions that predate it.
    A grader named by two or more DISTINCT tickets is shared by construction: no one of them
    can own it, and a lane that fixes its own defect makes only its own selected test go
    green. That measurement is what separates the real defect from its look-alike here.
    ``docs/tests/test_runbook.py`` is named by exactly ONE ticket, T-087, and sits inside a
    different ticket's scope — nobody else is graded by it, so it is T-087's own grader that
    T-087 may not write. ``proxyshop_support/tests/test_artifact_copyset.py`` is named by
    SEVEN, which makes it a shared gate of the same kind as the ``test_repro_*.py`` files
    (themselves named by five to ten tickets apiece) and not an ownership defect at all.
    """
    normalised = _normalise(path)
    if any(normalised.startswith(prefix) for prefix in _SHARED_GRADER_DIRS):
        return True
    if fnmatch.fnmatch(Path(normalised).name, _SHARED_GRADER_BASENAME):
        return True
    return len((named_by or {}).get(normalised, set())) >= 2


#: Shells whose ``-c`` argument is an entire command line packed into ONE word.
_SHELL_NAMES = frozenset({"bash", "sh", "zsh", "dash", "ksh"})

#: How deep a ``bash -c "bash -c ..."`` nest is opened before the expander stops. A cap
#: rather than unbounded recursion because the input is a graph field, not a trusted string.
_SHELL_DEPTH = 4


def _split_words(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _shell_script(words: list[str]) -> str | None:
    """The command line a ``bash -c <script>`` word list carries, or ``None``.

    An environment prefix is allowed to precede the shell (``FOO=1 bash -c "…"``), and the
    flag is matched as "a dash-word ending in ``c``" so the combined spellings ``-lc``,
    ``-ec`` and ``-euxc`` are opened too rather than being a respelling of the same escape.
    """
    index = 0
    while index < len(words) and "=" in words[index]:
        index += 1
    if index >= len(words):
        return None
    head = words[index]
    if head not in _SHELL_NAMES and Path(head).name not in _SHELL_NAMES:
        return None
    for offset in range(index + 1, len(words) - 1):
        word = words[offset]
        if word.startswith("-") and word.endswith("c"):
            return words[offset + 1]
    return None


def command_segments(verify: str, depth: int = _SHELL_DEPTH) -> list[list[str]]:
    """Every command the verify actually runs, with ``bash -c``/``sh -c`` wrappers OPENED.

    Not a nicety — it is the difference between two clauses looking at the command and both
    of them looking at nothing. ``bash -c "./scripts/verify.sh check"`` shlex-splits to
    ``["bash", "-c", "./scripts/verify.sh check"]``: no word ends in ``verify.sh``, so
    ``runs_whole_suite`` misses it, and no word is ``pytest``, so ``pytest_segments``
    returns empty and clause A has nothing to look at either. Measured on HEAD's graph, with
    the commands byte-identical: wrapping T-130's and T-134's gates took the selector set
    from [T-130, T-134] to [], and wrapping T-087's and T-228's took the ownership set from
    [T-087, T-228] to [] — i.e. the whole gate green for four verify-field edits that change
    what nothing executes.
    """
    segments: list[list[str]] = []
    for raw in re.split(r"\s*(?:&&|\|\||;)\s*", verify):
        words = _split_words(raw)
        if not words:
            continue
        inner = _shell_script(words) if depth > 0 else None
        if inner is not None:
            segments.extend(command_segments(inner, depth - 1))
            continue
        segments.append(words)
    return segments


def pytest_segments(verify: str) -> list[list[str]]:
    """The command segments that invoke pytest directly, ``bash -c`` wrappers opened up.

    ``./scripts/verify.sh check`` and ``make verify`` run pytest too, but they carry no
    pytest token and select nothing by path, so they are not selection commands and are
    deliberately absent here.
    """
    return [
        words
        for words in command_segments(verify)
        if any(word == "pytest" or word.endswith("/pytest") for word in words)
    ]


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
    named_by = graders_named_by(tickets)
    found: list[tuple[str, str, list[str], list[str]]] = []
    for ticket in graded_population(tickets):
        own_scope = ticket.get("scope")
        for segment in pytest_segments(str(ticket["verify"])):
            for operand in selection_operands(segment):
                if is_shared_grader(operand, named_by) or scope_covers(operand, own_scope):
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
    """``verify.sh check``/``all`` or ``make verify`` — a gate that selects nothing in particular.

    Reads ``command_segments``, so a wrapper around the identical command cannot hide it.
    """
    for words in command_segments(verify):
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

    THE RESPELLING PROBES BELOW ARE NOT HYPOTHETICAL. Three escapes were measured open on
    HEAD's graph and are pinned here, each having taken this gate fully GREEN with all four
    violators' defects intact and with every command still running the same thing:

    * ``bash -c "./scripts/verify.sh check"`` — one operand word, ending in neither
      ``verify.sh`` nor ``pytest``, so clause C and clause A both looked at a command that
      was not there. Selector set [T-130, T-134] -> [].
    * ``pytest "$PWD/docs/tests/test_runbook.py"`` — the identical third-party-owned file,
      invisible to every owner's glob. Ownership set [T-087, T-228] -> [].
    * the population floor, which read 84 while the real population had grown to 94, so it
      carried ten tickets of slack and both cheap population escapes passed it again.

    All three were VERIFY-FIELD or STATUS-FIELD edits, i.e. the pre-approved amendment class,
    while the sweep's own failure message told the reader a SCOPE change was required. A gate
    that misreports what it takes to pass is worse than one that is merely weak.
    """
    tickets = _tickets()
    assert len(tickets) >= 250, f"the graph parsed {len(tickets)} tickets; it holds ~292"

    population = graded_population(tickets)
    # RATCHET, and it is the only thing that catches two one-field escapes clause A cannot
    # see: downgrading the graded ticket's verify to the placeholder, and flipping its
    # status to "closed". Either removes it from the population rather than repairing it,
    # and either drops this count. 94 as re-measured here (45 open, 46 null, 3 "unknown").
    #
    # THE FLOOR IS EXACT, AND IT WENT SLACK ONCE ALREADY. It read 84 — the population on the
    # day it was written — while commits since had taken the real population to 94, so it
    # was carrying TEN tickets of slack and both escapes it exists to catch passed it again:
    # closing the four violators, or placeholdering their verify fields, left 90 and 90,
    # both comfortably above 84. A floor with slack is not a ratchet.
    #
    # The cost of exactness is that legitimately CLOSING a gated ticket also drops the count
    # (a closed ticket leaves `graded_population` by design), so this number has to be
    # re-baselined deliberately when that happens. An earlier comment here claimed the count
    # "only ever rises"; that was wrong, and believing it is how the slack got in.
    assert len(population) >= 94, (
        f"{len(population)} not-yet-closed tickets carry a real gate; 94 did when this floor "
        "was last measured. Either a graded ticket's gate was downgraded to the placeholder "
        "or its status was flipped to closed — both remove it from this sweep rather than "
        "repairing it — or a gated ticket was legitimately closed, in which case re-measure "
        "and re-baseline this number rather than lowering it to leave headroom."
    )

    # A SECOND ratchet on the same escape, and it is here because it is monotone where the
    # floor above is not: a ticket carrying a real gate keeps carrying one when it closes, so
    # legitimate closes never move this count and it never needs re-baselining for them.
    # Downgrading four verify fields to the placeholder takes it 153 -> 149. That means the
    # placeholder escape stays caught even if the population floor above is later slackened
    # to absorb a round of closures, which is exactly how it went slack the first time.
    gated_any_status = [t for t in tickets if not str(t.get("verify", "")).startswith("false")]
    assert len(gated_any_status) >= 153, (
        f"{len(gated_any_status)} tickets carry a real gate at any status; 153 did when this "
        "floor was last measured. A verify field was downgraded to the placeholder, which "
        "deletes a gate rather than repairing it."
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
    # Re-measured across the not-yet-closed population: 94 operand occurrences, 31 distinct
    # (44 and 19 when this was written). The distinct count is the one worth pinning low — a
    # graph that started spelling gates as `-k` selections would drop it, and clause B is
    # what turns that into a failure rather than a quiet shrink, so this floor only has to
    # catch the extractor breaking outright.
    assert len(operands) >= 30, f"the sweep extracted {len(operands)} path operands to check"
    assert len(set(operands)) >= 15, f"only {len(set(operands))} distinct operands; 31 at HEAD"

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

    # The PARAMETERISED worker form. Freeze-log amendment 22 rewrote the verify field of
    # all 130 gated tickets from a literal `PROXYSHOP_WORKER=15` to
    # `PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-N}` — a bulk edit to the exact field this
    # sweep grades, made for an unrelated and legitimate reason. Every clause has to see
    # straight through it.
    #
    # WHY IT IS SKIPPED, corrected: it is POSITION, not the `=` rule. `selection_operands`
    # advances to the `pytest` token before it starts scanning, so the environment prefix is
    # never examined at all, and the `"=" in word` clause it also carries would only matter
    # for a word AFTER `pytest`. The `=` rule is genuinely load-bearing in the sibling file's
    # `verify_operands`, which scans every word from index 0; the claim was copied across to
    # this module, where it does not apply. It is written down because "it held for reason X"
    # is only worth recording if X is the actual reason.
    _parameterised = (
        "PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-12} uv run python -m pytest "
        "docs/tests/test_runbook.py -q"
    )
    assert [selection_operands(w) for w in pytest_segments(_parameterised)] == [
        ["docs/tests/test_runbook.py"]
    ], "the parameterised worker form is leaking into the operand list"
    assert runs_whole_suite("PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-5} make verify")
    assert runs_whole_suite(
        "PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-14} ./scripts/verify.sh check"
    )
    # Position, demonstrated rather than asserted in prose: an env-prefix word placed AFTER
    # `pytest` is discarded by the `=` clause, and one placed before it is never read.
    assert selection_operands(
        shlex.split("uv run python -m pytest PROXYSHOP_WORKER=15 docs/tests/x.py -q")
    ) == ["docs/tests/x.py"]

    # THE `bash -c` WRAPPER. Measured on HEAD's graph as four verify-field edits that change
    # what nothing executes: wrapping T-130's and T-134's `verify.sh check` took the selector
    # set to [], and wrapping T-087's and T-228's pytest commands took the ownership set to
    # [] — the whole gate green with every command byte-identical.
    assert runs_whole_suite('bash -c "./scripts/verify.sh check"'), (
        "a `bash -c` wrapper hides a whole-suite gate from clause C again; shlex yields one "
        "operand word that ends in neither `verify.sh` nor `pytest`, so no clause looks"
    )
    assert runs_whole_suite("sh -c './scripts/verify.sh all'")
    assert runs_whole_suite('bash -lc "PROXYSHOP_WORKER=15 make verify"'), (
        "the combined `-lc` spelling of the flag re-opens the wrapper escape"
    )
    assert runs_whole_suite('/bin/bash -c "make verify"'), (
        "an absolute shell path re-opens the wrapper escape"
    )
    assert not runs_whole_suite('bash -c "uv run python -m pytest docs/tests/x.py -q"')
    assert [
        selection_operands(w)
        for w in pytest_segments('bash -c "uv run python -m pytest docs/tests/x.py -q"')
    ] == [["docs/tests/x.py"]], (
        "a `bash -c` wrapper hides a pytest SELECTION from clause A, which is the bigger "
        "half of the same escape: with no pytest token the ownership loop iterates nothing"
    )

    # THE RESPELT OPERAND. `$PWD/docs/tests/test_runbook.py` runs the identical file while
    # matching no owner's glob; measured, it took T-087 and T-228 straight out of the
    # ownership set. An absolute path did the same, and worse: `lstrip("./")` ate only the
    # leading slash, so the operand normalised to a path under the user's home directory.
    for respelling in (
        "$PWD/docs/tests/test_runbook.py",
        "${PWD}/docs/tests/test_runbook.py",
        "$(pwd)/docs/tests/test_runbook.py",
        f"{REPO_ROOT}/docs/tests/test_runbook.py",
    ):
        assert _normalise(respelling) == "docs/tests/test_runbook.py", (
            f"`{respelling}` no longer normalises to its repo-relative form, so respelling "
            "an operand takes it past every owner while running the same file"
        )
        assert scope_covers(respelling, ["docs/tests/**"]), (
            f"`{respelling}` is invisible to the ownership lookup again"
        )
    # An absolute path OUTSIDE the tree is left alone by the rewrite rather than being
    # re-rooted into this repo, which would invent an owner for a file the repo does not have.
    assert (
        _repo_relative("/somewhere/else/docs/tests/test_runbook.py")
        == "/somewhere/else/docs/tests/test_runbook.py"
    ), "an absolute path outside this tree is being rewritten as though it were a repo file"

    # The allowlists, in both directions.
    assert is_shared_grader(".swarm-loop/acceptance/test_e8_proofs.py")
    assert is_shared_grader("services/sim/tests/test_repro_open_tickets.py")
    assert not is_shared_grader("docs/tests/test_runbook.py")
    assert not is_shared_grader("docs/tests/test_runbook_executability.py")

    # The shared-grader COUNT, which is what separates T-087's grader from a gate file that
    # simply grades many tickets. Both numbers are read off the graph, not hard-coded.
    named_by = graders_named_by(tickets)
    copyset = _normalise("proxyshop_support/tests/test_artifact_copyset.py")
    runbook = _normalise("docs/tests/test_runbook.py")
    assert len(named_by.get(copyset, set())) >= 5, (
        f"only {len(named_by.get(copyset, set()))} tickets are graded by the artifact "
        "copy-set file; it was 7, and if it stops being shared the sweep starts calling "
        "those tickets ownership defects when they are not"
    )
    assert is_shared_grader("proxyshop_support/tests/test_artifact_copyset.py", named_by)
    assert len(named_by.get(runbook, set())) == 1, (
        "docs/tests/test_runbook.py is now named by "
        f"{len(named_by.get(runbook, set()))} tickets; T-087 was its only namer, which is "
        "exactly what made it T-087's own ungrantable grader rather than a shared gate"
    )
    assert not is_shared_grader("docs/tests/test_runbook.py", named_by)

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
        "T-262: not-yet-closed tickets graded by a file they cannot write. T-087's verify "
        "runs `docs/tests/test_runbook.py`, a path its own scope "
        "(`docs/demo/shopify-onboarding-extension.md`) forbids it to create, that sits "
        "inside open T-085's `docs/tests/**`, and that no other ticket is graded by — so "
        "T-087 cannot write its own grader and would inherit an unearned green from a test "
        "another lane wrote. The violator set is MEASURED LIVE and the failure output is "
        "the authority on it, deliberately rather than pinning a count here: it read four "
        "on this branch's base — T-087 and T-228 by ownership, T-130 and T-134 by "
        "whole-suite — and five on main a few commits later, freeze-log amendment 26 having "
        "repointed T-158's gate off a `test_repro_*.py` path that the shared-grader "
        "allowlist exempts and onto `apps/exchange/tests/test_t158_acceptance_claim.py`, "
        "which closed T-030, T-031, T-033 and T-036 own. Remove this marker with the scope "
        "amendment"
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
    is OWNERSHIP, never existence, and every variant of the defect stays red:

    * the file gets created -> red (nothing here calls ``exists()``);
    * the verify is repointed at ``docs/tests/test_runbook_executability.py``, which does
      exist -> red, because it is inside the same foreign glob;
    * the verify is respelt ``pytest docs/tests -q`` -> red, because ``docs/tests/**``
      matches the bare directory, and a ``-k``-only respelling is caught by clause B;
    * the operand is respelt ``"$PWD/docs/tests/test_runbook.py"``, ``"${PWD}/…"``,
      ``"$(pwd)/…"`` or as this tree's absolute path -> red, because ``_repo_relative``
      folds all four back to the repo-relative spelling before the ownership lookup;
    * the whole command is wrapped in ``bash -c "…"`` (or ``sh``/``zsh``, or ``-lc``) -> red,
      because ``command_segments`` opens the wrapper rather than seeing one opaque word.

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

    WHAT SEPARATES THIS DEFECT FROM ITS LOOK-ALIKE, and it took a wrong answer to find.
    Widening the population past ``status == "open"`` pulled in seven tickets — T-298 to
    T-301, T-304, T-313, T-314 — whose gates all run
    ``proxyshop_support/tests/test_artifact_copyset.py``, a file none of them owns. That
    looked like six more T-087s. It is not, and calling it one would have been the gate
    asserting something false:

    * a third party writing the grader is the LADDER'S DESIGN, not the defect. Gate author
      and fixer are meant to be different agents.
    * the discriminating question is whether a lane can make its own gate green WITHOUT
      fixing the defect. For those seven it cannot: the gate file is outside every one of
      their scopes (``apps/merchant/Dockerfile``, ``services/ingest/``, and so on), so a
      lane can change only its own subject, and the one edit it may make to the gate file —
      removing a strict-xfail marker — turns a still-failing test into a FAILURE, never a
      pass.
    * measured, rather than reasoned by analogy: each of those gates selects exactly one
      test and that test passes (``-k test_t304`` -> ``1 passed, 9 deselected``). An earlier
      draft of this docstring claimed they "select nothing"; that was wrong, and it was
      wrong because the ids appear in those function NAMES in the ``test_t304`` spelling
      rather than as ``T-304`` string literals, which is a fact about a scan, not about the
      graph.

    So the rule now asks the shared-ness question directly, and the numbers do the
    separating: ``docs/tests/test_runbook.py`` is named by exactly ONE ticket's gate and
    lives in another ticket's scope, while ``test_artifact_copyset.py`` is named by SEVEN
    and is a shared gate of the same kind as the ``test_repro_*.py`` files. Counting
    distinct namers is a better allowlist than the basename convention it generalises, and
    unlike an existence test it cannot be satisfied by the defect completing.

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
    population ratchet, 94 -> 90); rewriting the owner's glob as ``docs/{tests}/**`` or as
    ``docs/tests/** (runbook tests)``, neither of which changes ownership for the harness
    that enforces it; and replacing the gate with ``make verify`` or ``verify.sh check``,
    which left clause A nothing to look at.

    THEN THE REAL TEST ARRIVED, unaimed. The review that found those escapes observed that
    the cheapest of them were VERIFY-FIELD edits, i.e. the pre-approved amendment class —
    and freeze-log amendment 22 subsequently made exactly that edit to all 130 gated
    tickets at once, rewriting every ``PROXYSHOP_WORKER=15`` into
    ``PROXYSHOP_WORKER=${PROXYSHOP_GATE_WORKER:-N}`` so concurrent lanes stop sharing one
    Postgres database and one Redis logical DB. A bulk rewrite of the exact field this
    sweep grades, by the orchestrator, for a good reason unrelated to this gate.

    Re-measured across that merge the violator set is UNCHANGED — T-087 and T-228 by
    ownership, T-130 and T-134 by whole-suite. It held for a narrow, nameable reason rather
    than by luck, and the reason recorded here was the WRONG ONE: it said "every operand word
    containing ``=`` is discarded before anything is parsed". Not in this module.
    ``selection_operands`` starts scanning only AFTER the ``pytest`` token, so an environment
    prefix is never examined at all — it is POSITION that discards it, and the ``=`` clause
    would only bite a word placed after ``pytest``. The ``=`` rule is load-bearing in the
    sibling file's ``verify_operands``, which scans every word from index 0; the sentence was
    copied across to a file where it does not apply. "It held for reason X" is worth
    recording only if X is the actual reason, so both spellings are now pinned in the arming
    test rather than argued in prose.

    THREE MORE ESCAPES WERE THEN MEASURED OPEN and are closed here, all three cheaper than
    anything above because none of them changes what any command runs:

    * ``bash -c "./scripts/verify.sh check"`` in place of the bare command. One shlex token,
      ending in neither ``verify.sh`` nor ``pytest``, so clause C never recognised the
      whole-suite run AND clause A found no pytest segment to inspect. Wrapping T-130's and
      T-134's gates took the selector set to []; wrapping T-087's and T-228's took the
      ownership set to []. ``command_segments`` now opens ``bash``/``sh``/``zsh`` ``-c``
      arguments, including the combined ``-lc``/``-ec`` spellings and an absolute shell path.
    * ``pytest "$PWD/docs/tests/test_runbook.py"`` in place of the repo-relative operand.
      The same third-party-owned file, run by the same command, matching no owner's glob —
      ownership set [T-087, T-228] -> []. An absolute path did it too, and normalised into
      the user's home directory because ``lstrip("./")`` eats only the leading slash.
      ``_repo_relative`` now folds ``$PWD``/``${PWD}``/``$(pwd)`` and this tree's absolute
      prefix back to the repo-relative spelling before any ownership lookup.
    * the population floor, which had gone SLACK. It read 84 — exactly the population on the
      day it was written, which is why it caught both cheap population escapes then — while
      commits since had taken the real population to 94. With ten tickets of headroom,
      flipping the four violators to ``status: closed`` left 90 and flipping their verify
      fields to the placeholder left 90, and both passed the floor and took the gate GREEN.
      The floor is re-baselined to the measured 94, and a second ratchet on tickets carrying
      a real gate AT ANY STATUS (153) now catches the placeholder escape independently,
      because that count is monotone under legitimate closes where the population is not.

    Taken together those three were the gate lying about what it takes to pass: the failure
    message below says a SCOPE-field change is required and is not in the pre-approved
    verify-field amendment class, while four verify-field edits satisfied it.
    """
    tickets = _tickets()
    ownership = ownership_violations(tickets)
    selector = selector_violations(tickets)

    report = [
        f"  {ticket_id}: its gate runs `{operand}`, which its own scope {own} does not "
        f"grant, and which is owned by {', '.join(holders)}"
        for ticket_id, operand, own, holders in ownership
    ] + [f"  {ticket_id}: {command}" for ticket_id, command in selector]

    assert not report, (
        f"{len(ownership) + len(selector)} open ticket(s) are graded by a file they cannot "
        "write, so their lane cannot create its own grader and a third party's test would "
        "decide whether they pass:\n" + "\n".join(report) + "\n\nRepair: narrow the owning "
        "ticket's scope, widen the graded ticket's scope to cover the file its gate runs, "
        "or repoint the gate at a path no ticket owns. The first two are SCOPE-field "
        "changes and are not in the pre-approved verify-field amendment class; the third is "
        "a verify-field edit, and it is honest only when the new path genuinely grades the "
        "ticket — respelling the SAME path as `$PWD/...` or wrapping the SAME command in "
        "`bash -c` is not a repair and no longer reads as one."
    )
