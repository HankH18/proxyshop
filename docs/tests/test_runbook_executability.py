"""Does the demo runbook's procedure actually *run*? — an executability gate.

``.swarm-loop/acceptance/test_e8_proofs.py`` certifies ``docs/demo/starting-slice.md`` by
checking that the ``make`` targets it names are **defined in the Makefile**. Definition is
shape, not behaviour: ``e2e-live:`` is defined, and running it prints
``bash: docs/demo/e2e_live.sh: No such file or directory``. So E8 reads 8/8 and acceptance
reads 120/120 while the document an operator would follow is broken at two of its steps.

This file grades the other thing — **resolvability**. For every command the runbook tells an
operator to type, it asks whether the thing that command names is really there:

* ``make <target>``  — the target is defined, a non-empty recipe was actually parsed for it,
  every ``.sh``/``.py`` the recipe invokes exists (and carries ``+x`` when the recipe execs it
  directly rather than handing it to an interpreter), every ``-m <module>`` it runs is
  importable, and every prerequisite and ``$(MAKE)`` recursion is checked the same way.
* ``pytest <path>``  — the path exists, is a file when it names one, **and** a real
  ``pytest --collect-only`` subprocess collects at least one test from it.
* ``python <path>`` / ``python -m <module>``  — the script exists, or the module resolves.
* every ``*.sh`` named **anywhere**, prose included — it exists, ``bash -n`` parses it, and it
  carries at least one line that is not a shebang, a comment or blank.
* ``cp <src> <dst>``  — ``<src>`` exists.
* ``docker compose --profile <p> ... <service>``  — the merged compose config defines
  ``<service>`` with that profile.

The command list is parsed out of the documents, never hardcoded, and every markdown file under
``docs/demo/`` is swept rather than one named document — the runbook repeatedly promises an
"extension runbook", and that page must be graded the day it lands.

**Two independent parses, cross-checked — because enumerating markdown does not work.**
An adversarial review defeated three successive drafts of this file, and the lesson each time
was the same: every rule that enumerated a *shape* a document may take became simultaneously a
bypass and a false positive. Bounding fence indentation at three spaces (CommonMark measures it
relative to the containing block) both hid a fence indented to column 4 inside a list item and
silently dropped legitimate nested fences. Allow-listing fence languages both sanctioned hiding
the procedure under ```` ```text ```` and hard-failed on ```` ```plaintext ````.

So the arming pin is no longer a guess about markdown. ``test_the_parse_covers_the_raw_text``
runs a second, deliberately stupid, vocabulary-free scan over the raw bytes: every line
mentioning a ``make`` target the **Makefile** actually defines, every ``*.sh`` token and every
``dir/file.py`` token must have produced a graded command **on that line**. A fence this parser
cannot read is then a loud mismatch instead of a silent zero, and no fence-indent rule or
language list has to be right for the gate to hold. Unknown fence languages are simply skipped
now, and the cross-check is what makes that safe.

**Failing OPEN is the enemy.** Three sweeps in this repo were caught going quiet rather than red
(T-229 6→0 of 8, T-281 70→0 of 79, T-241 48→0 of 66). Both guard tests are deliberately **not**
xfail. Beyond the cross-check they require: at least one runbook; every shell block yielding at
least one command; a floor on commands naming something *resolvable* (not the raw count, which
``VAR=value`` lines would pad); and no command naming a repository artefact grading INFO.
A ``pytest`` invocation with no path grades FAIL, not INFO — letting it through was the cheapest
way found to erase the ``e2e/test_s1_flow.py`` defect. A ``make`` target whose recipe parses to
nothing grades FAIL, because an empty recipe is otherwise indistinguishable from a clean one:
``e2e-live: preflight ; @bash …``, ``e2e-live:: ; @bash …`` and ``@bash $(E2E)`` each turned the
broken target green in an earlier draft with the recipe still visibly intact in the file.

**What this gate deliberately does NOT grade: semantics.** ``docs/demo/e2e_live.sh`` containing
only ``exit 0`` resolves, and this file will pass it. That is the correct boundary — what that
script must *do* is the specification of the ticket that writes it, and a gate that guessed
would be grading its own opinion. The same holds for ``collects >= 1``: this proves pytest can
reach the module, not that the module proves S1. Read a green here as "the runbook's steps
reach real code", never as "the demo works".

What this file enforces is what the **repository** controls. Whether ``docker``, ``uv`` or Node
is installed on the operator's laptop is INFO, not failure; ``.venv/`` paths are skipped because
``make bootstrap`` is what creates them.

**This gate does not replace the frozen E8 check.** E8 pins the runbook's *shape* — that it
still names ``make e2e-live``. This pins *executability* — that what it names resolves. A
runbook edited to stop naming a broken command satisfies this file and is caught by E8; a
command named but broken satisfies E8 and is caught here.

Mechanism, both directions (the ``test_repro_open_tickets.py`` idiom):

* a normal run reports ``xfailed`` and exits 0, so ``make verify`` stays green;
* the ticket's gate runs this file with ``--runxfail`` and gets a real ``1 failed``, whose
  message names the broken step rather than only going red;
* ``strict=True`` turns the eventual repair into an XPASS *failure*, so whoever writes
  ``docs/demo/e2e_live.sh`` and ``e2e/test_s1_flow.py`` must delete the marker.

This file writes the gate and nothing else — a lane that repairs the defect it was asked to
reproduce destroys the gate that would have graded the repair. No ticket id is minted for this
work yet (the graph tops out at T-315), so the tests are named for the lane rather than
squatting an id that may be minted for something else.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

#: Repo root, reached from this file rather than from a hard-coded string so a moved test
#: cannot silently start scanning nothing.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every runbook, not one named document. The frozen E8 suite globs the same directory.
RUNBOOK_DIR = REPO_ROOT / "docs" / "demo"

MAKEFILE = REPO_ROOT / "Makefile"

ROOT_COMPOSE = REPO_ROOT / "docker-compose.yml"

#: Floor for commands that name something resolvable — deliberately NOT the raw count, which
#: three ``VAR=value`` lines would inflate. Today's measurement is 20 raw, 14 resolvable.
MIN_RESOLVABLE_COMMANDS = 5

#: Fence languages carrying an operator procedure. Anything else is skipped rather than
#: rejected: the raw-text cross-check, not this list, is what stops a command hiding in one.
SHELL_LANGS = {
    "",
    "bash",
    "sh",
    "shell",
    "zsh",
    "console",
    "shell-session",
    "shellsession",
    "console-session",
    "sh-session",
    "terminal",
}

#: Tokens meaning "the next path is handed to an interpreter", so it must exist but needs no +x.
_INTERPRETERS = ("bash", "sh", "zsh", "python", "python3", "source", ".", "uv", "pytest", "node")

#: Command words that only wrap another command; strip them and re-classify what is left.
_WRAPPERS = {"env", "sudo", "command", "exec", "time", "nohup"}

#: Flags whose following token is a value, not an operand. Separate vocabularies, because
#: applying pytest's set to ``make`` turned ``make -C services check`` into a bogus FAIL.
_PYTEST_VALUE_FLAGS = {
    "-k", "-m", "-p", "-n", "-o", "-c", "-W",
    "--timeout", "--maxfail", "--rootdir", "--junitxml",
    "--deselect", "--ignore", "--override-ini", "--import-mode",
}  # fmt: skip
_MAKE_VALUE_FLAGS = {"-C", "-f", "-I", "-j", "-l", "-O", "-W", "--directory", "--file", "--jobs"}

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INFO = "INFO"

#: Categories whose commands name a repository artefact. The guard requires that none of these
#: ever grades INFO: "nothing to check here" is the failure being hunted, not a way past it.
RESOLVABLE_CATEGORIES = {
    "make target",
    "pytest invocation",
    "python module",
    "python script",
    "shell script",
    "shell script referenced in the runbook",
    "cp source",
    "docker compose service",
}

#: A command carrying an unexpanded ``$VAR`` cannot be resolved by static inspection, and saying
#: so is honest. The guard asserts this category is EMPTY today, so the first runbook step that
#: needs it forces a human look rather than quietly widening the exemption.
UNRESOLVABLE_CATEGORY = "unexpanded shell variable"


class RunbookParseError(AssertionError):
    """The document is shaped in a way this parser cannot honestly grade."""


# =====================================================================================
# Parsing the runbook
# =====================================================================================


@dataclass
class Command:
    """One thing a runbook tells an operator to type, or one file it names."""

    text: str
    doc: str
    lineno: int
    kind: str  # "shell" | "script-reference" | "inline-make"

    @property
    def where(self) -> str:
        return f"{self.doc}:{self.lineno}"


@dataclass
class Result:
    command: Command
    verdict: str
    category: str
    reason: str
    details: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = f"  [{self.verdict}] {self.command.where}  `{self.command.text}`"
        body = f"\n        {self.category}: {self.reason}"
        extra = "".join(f"\n            {d}" for d in self.details)
        return head + body + extra


#: Indentation is deliberately unbounded. CommonMark measures a fence's indent relative to its
#: containing block, so any absolute bound is wrong: bounding it at three spaces hid a fence at
#: column 4 inside a bullet (a valid, correctly-rendering block) from an earlier draft.
_FENCE_OPEN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,})(?P<info>[^`]*)$"
    r"|^(?P<indent2>[ \t]*)(?P<fence2>~{3,})(?P<info2>.*)$"
)

#: An inline span that is *exactly* a make invocation. Deliberately narrow: the runbook is full
#: of inline spans naming types, paths and event kinds, and a generous pattern would drag
#: `CheckoutProvider` and `orders/paid` in. ``\s`` matches a newline, so a span broken across two
#: prose lines (`` `make⏎bootstrap` ``) is still found.
_INLINE_MAKE = re.compile(r"`(make\s+[A-Za-z0-9][A-Za-z0-9_.-]*)`")

#: Any shell-script path named anywhere, prose included. The trailing guard stops ``x.sh.bak``
#: from being read as ``x.sh``.
_SCRIPT_REF = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.sh)(?![\w.])")

#: A python path, for the raw-text cross-check. A directory component is required so that prose
#: mentioning a bare ``foo.py`` is not treated as a repo artefact.
_RAW_PY = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.py)(?![\w.])")

#: ``make <target>`` in raw text, for the cross-check. The Makefile supplies the vocabulary, so
#: this needs no list of English words to exclude.
_RAW_MAKE = re.compile(r"(?<![\w-])make\s+([A-Za-z0-9][\w.-]*)")


@dataclass
class Block:
    open_lineno: int
    lang: str
    lines: list[tuple[int, str]]


def _fenced_blocks(text: str, doc: str) -> list[Block]:
    """Every fenced code block in ``text``.

    An unclosed fence raises rather than being dropped: silently dropping it was how a single
    stray ```` ```` ```` reduced the whole runbook to three graded commands in an earlier draft.
    """
    blocks: list[Block] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        indent = m.group("indent") or m.group("indent2") or ""
        fence = m.group("fence") or m.group("fence2") or ""
        info = m.group("info") if m.group("fence") else (m.group("info2") or "")
        lang = (info or "").strip().split()[0].lower() if (info or "").strip() else ""
        open_lineno = i + 1

        body: list[tuple[int, str]] = []
        j = i + 1
        closed = False
        while j < len(lines):
            stripped = lines[j].strip()
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                closed = True
                break
            raw = lines[j]
            body.append((j + 1, raw[len(indent) :] if raw.startswith(indent) else raw.lstrip()))
            j += 1
        if not closed:
            raise RunbookParseError(
                f"{doc}:{open_lineno} opens a `{fence}` code fence that is never closed. Every "
                f"line to the end of the document is being swallowed into it, so the commands "
                f"below it would go ungraded — refusing to grade this document."
            )
        blocks.append(Block(open_lineno=open_lineno, lang=lang, lines=body))
        i = j + 1
    return blocks


def _mask_fences(text: str, blocks: list[Block]) -> str:
    """``text`` with every fenced block blanked, offsets preserved.

    Lets the inline-``make`` scan run over the whole prose at once — so a span broken across two
    lines is still matched — without the code blocks being scanned twice.
    """
    lines = text.splitlines(keepends=True)
    masked = list(lines)
    for block in blocks:
        lo = block.open_lineno - 1
        hi = (block.lines[-1][0] if block.lines else block.open_lineno) + 1
        for idx in range(lo, min(hi, len(masked))):
            nl = masked[idx].endswith("\n")
            masked[idx] = " " * (len(masked[idx]) - (1 if nl else 0)) + ("\n" if nl else "")
    return "".join(masked)


def _strip_comment(line: str) -> str:
    """Drop a trailing ``# comment``, respecting quotes."""
    out: list[str] = []
    quote: str | None = None
    prev_space = True
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            prev_space = False
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            prev_space = False
            continue
        if ch == "#" and prev_space:
            break
        out.append(ch)
        prev_space = ch.isspace()
    return "".join(out).strip()


def _logical_lines(body: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Join backslash continuations, strip ``$``/``%`` prompts and comments, drop blanks.

    ``#`` starts a comment and never a root prompt. Treating ``# `` as a prompt in an earlier
    draft turned ``# don't run this twice`` into an unparseable command and reddened the gate on
    any apostrophe in a code-block comment.
    """
    joined: list[tuple[int, str]] = []
    pending: str | None = None
    pending_lineno = 0
    for lineno, raw in body:
        line = raw.rstrip()
        if pending is not None:
            pending = pending + " " + line.strip()
        else:
            pending_lineno = lineno
            pending = line
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        joined.append((pending_lineno, pending))
        pending = None
    if pending is not None:
        joined.append((pending_lineno, pending))

    out: list[tuple[int, str]] = []
    for lineno, line in joined:
        text = line.strip()
        if text.startswith("#"):
            continue
        if text[:2] in ("$ ", "% "):
            text = text[2:]
        text = _strip_comment(text)
        if text:
            out.append((lineno, text))
    return out


def _split_chain(text: str) -> list[str]:
    """Split a shell line on ``&&``/``||``/``;``/``|`` at top level."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if text[i : i + 2] in ("&&", "||"):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in (";", "|"):
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


_REDIRECT = re.compile(r"^\d*(?:&>>|&>|>>|>|<)&?\d*$")


def _strip_redirects(toks: list[str]) -> list[str]:
    """Drop ``>file`` / ``2>&1`` / ``&> log`` so a redirect is never read as an operand."""
    out: list[str] = []
    skip_next = False
    for tok in toks:
        if skip_next:
            skip_next = False
            continue
        if _REDIRECT.fullmatch(tok):
            skip_next = not tok.endswith(("&1", "&2", "&-"))
            continue
        if re.sub(r"^\d*(?:&>>|&>|>>|>|<)", "", tok) != tok:
            continue  # operand fused to the operator, e.g. `>out.txt`
        out.append(tok)
    return out


def extract_commands(path: Path) -> tuple[list[Command], list[Block]]:
    """Every command a runbook instructs an operator to run, derived from the document.

    Three sources: fenced code blocks (the procedure), inline spans that are exactly
    ``make <target>`` (steps referenced only in prose, e.g. Troubleshooting), and a document-wide
    sweep for ``*.sh`` paths — ``docs/demo/e2e_live.sh`` is named *only* in prose. Nothing is
    de-duplicated: the raw-text cross-check asserts coverage per LINE, so a second mention on a
    second line has to produce its own graded command.
    """
    doc = str(path.relative_to(REPO_ROOT))
    text = path.read_text(encoding="utf-8")
    blocks = _fenced_blocks(text, doc)
    commands: list[Command] = []

    for block in blocks:
        if block.lang not in SHELL_LANGS:
            continue
        for lineno, line in _logical_lines(block.lines):
            for piece in _split_chain(line):
                commands.append(Command(text=piece, doc=doc, lineno=lineno, kind="shell"))

    prose = _mask_fences(text, blocks)
    for hit in _INLINE_MAKE.finditer(prose):
        commands.append(
            Command(
                text=" ".join(hit.group(1).split()),
                doc=doc,
                lineno=prose[: hit.start()].count("\n") + 1,
                kind="inline-make",
            )
        )

    for hit in _SCRIPT_REF.finditer(text):
        commands.append(
            Command(
                text=hit.group(1),
                doc=doc,
                lineno=text[: hit.start()].count("\n") + 1,
                kind="script-reference",
            )
        )

    return commands, blocks


def runbooks() -> list[Path]:
    return sorted(p for p in RUNBOOK_DIR.rglob("*.md") if p.is_file())


# =====================================================================================
# The Makefile
# =====================================================================================

_ASSIGNMENT = re.compile(r"^\s*(?P<name>[A-Za-z_][\w.]*)\s*(?P<op>[:?+!]?=)\s*(?P<value>.*)$")
#: ``target [more] [: | ::] [prereqs] [; recipe]`` — the prerequisite list and the double-colon
#: form both silently erased this Makefile's inline recipes in an earlier draft.
_TARGET = re.compile(
    r"^(?P<names>[A-Za-z0-9_][A-Za-z0-9_.\-/ ]*?)\s*(?P<colons>::|:)(?!=)"
    r"(?P<prereqs>[^;]*)(?:;(?P<recipe>.*))?$"
)
_SPECIAL = {".PHONY", ".DEFAULT", ".SUFFIXES", ".ONESHELL", ".SILENT", ".NOTPARALLEL"}


@dataclass
class Rule:
    recipe: str
    prereqs: list[str]


def parse_makefile(path: Path) -> dict[str, Rule]:
    """``{target: Rule}`` for the ``target: ; recipe`` one-liner and tab-indented forms.

    Simple ``VAR = value`` assignments are expanded into recipes, because ``@bash $(E2E)``
    otherwise hides the path this gate exists to check. An **undefined** variable is left intact
    rather than expanded to nothing — substituting the empty string made ``@bash $(NOPE)`` pass
    by making the path disappear.
    """
    if not path.is_file():
        return {}
    bodies: dict[str, list[str]] = {}
    prereqs: dict[str, list[str]] = {}
    variables: dict[str, str] = {}
    current: list[str] = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("\t"):
            for name in current:
                bodies.setdefault(name, []).append(raw.strip())
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            current = []
            continue
        assign = _ASSIGNMENT.match(raw)
        if assign and ":" not in assign.group("name"):
            variables[assign.group("name")] = assign.group("value").strip()
            current = []
            continue
        m = _TARGET.match(stripped if raw[:1] == " " else raw)
        if not m:
            current = []
            continue
        names = [n for n in m.group("names").split() if n not in _SPECIAL]
        if not names or names[0].startswith("."):
            current = []
            continue
        current = names
        recipe = (m.group("recipe") or "").strip()
        deps = [d for d in m.group("prereqs").split() if d != "|"]
        for name in names:
            bodies.setdefault(name, [])
            prereqs.setdefault(name, []).extend(deps)
            if recipe:
                bodies[name].append(recipe)

    def expand(text: str, depth: int = 0) -> str:
        if depth > 8:
            return text
        out = re.sub(
            r"\$[({](?P<name>[A-Za-z_][\w.]*)[)}]",
            lambda mm: variables.get(mm.group("name"), mm.group(0)),
            text,
        )
        return out if out == text else expand(out, depth + 1)

    return {
        name: Rule(recipe=expand("\n".join(body)), prereqs=prereqs.get(name, []))
        for name, body in bodies.items()
    }


_RECIPE_PATH = re.compile(r"(?<![\w-])((?:\.{1,2}/)?(?:[\w.-]+/)*[\w.-]+\.(?:sh|py))(?![\w])")
_RECIPE_MODULE = re.compile(r"-m\s+([A-Za-z_][\w.]*)")
_RECIPE_SUBMAKE = re.compile(r"\$\(MAKE\)((?:\s+--?[\w-]+)*)\s+([A-Za-z0-9][\w.-]*)")
_UNEXPANDED = re.compile(r"\$[({](?!MAKE[)}])(?P<name>[A-Za-z_][\w.]*)[)}]")


def _undefined_program_vars(recipe: str) -> list[str]:
    """Undefined ``$(VAR)`` tokens sitting where a **program or script path** would go.

    The hole being closed is ``@bash $(NOPE)``: expanding an unassigned variable to the empty
    string made the path vanish, so the target passed by naming nothing. The hole NOT being
    closed is ``--category "$(SEED_CATEGORY)"`` — the runbook states plainly that ``demo-seed``
    reads that from the operator's environment, so an unassigned *data argument* is correct and
    flagging it was a false positive. Position, not presence, is what distinguishes them.
    """
    found: list[str] = []
    for chunk in re.split(r"[;&|]+", recipe.replace("\n", " ")):
        toks = chunk.split()
        for idx, tok in enumerate(toks):
            hits = [m.group("name") for m in _UNEXPANDED.finditer(tok)]
            if not hits:
                continue
            prev = toks[idx - 1].lstrip("@-") if idx else ""
            if idx == 0 or (prev and prev.endswith(_INTERPRETERS)):
                found.extend(hits)
    return found


def _preceding_token(recipe: str, path_token: str) -> str:
    """The token before each occurrence of ``path_token``; an interpreter anywhere wins.

    Tokenised rather than ``str.find``-ed: first-occurrence matching misattributed
    ``@echo scripts/x.sh && bash scripts/x.sh`` and matched inside a longer path.
    """
    best = ""
    for chunk in re.split(r"[;&|]+", recipe.replace("\n", " ")):
        toks = chunk.split()
        for idx, tok in enumerate(toks):
            if tok.strip("\"'") != path_token:
                continue
            prev = toks[idx - 1].lstrip("@-") if idx else ""
            if prev.endswith(_INTERPRETERS):
                return prev
            best = prev
    return best


def check_make_target(target: str, seen: set[str] | None = None) -> tuple[bool, list[str]]:
    """Is ``make <target>`` runnable? Definition **and** everything its recipe reaches."""
    seen = seen if seen is not None else set()
    if target in seen:
        return True, []
    seen.add(target)

    rules = parse_makefile(MAKEFILE)
    if target not in rules:
        return False, [f"no target `{target}:` is defined in Makefile"]

    rule = rules[target]
    problems: list[str] = []

    # The Makefile sweep's arming pin. An empty recipe is indistinguishable from a clean one, so
    # it FAILS rather than passes. A target with no recipe of its own is only acceptable when a
    # prerequisite supplies one — otherwise `e2e-live: preflight` would pass by delegating to
    # something that never runs the missing script.
    if not rule.recipe.strip():
        donors = [p for p in rule.prereqs if p in rules and rules[p].recipe.strip()]
        if not donors:
            return False, [
                f"target `{target}:` is defined but this gate parsed NO recipe for it, and none "
                f"of its prerequisites {rule.prereqs or '[]'} supplies one — refusing to report "
                f"it runnable on the strength of a target line alone"
            ]

    for hit in dict.fromkeys(_undefined_program_vars(rule.recipe)):
        problems.append(
            f"recipe for `{target}` runs `$({hit})` as a program or script, and the Makefile "
            f"never assigns it — so what this target actually executes cannot be resolved, and "
            f"a missing file behind that variable would be invisible to this gate"
        )

    for token in dict.fromkeys(_RECIPE_PATH.findall(rule.recipe)):
        rel = token[2:] if token.startswith("./") else token
        if rel.startswith(".venv/") or "/.venv/" in token:
            continue  # provisioned environment, not a repo artefact
        path = REPO_ROOT / rel
        if not path.is_file():
            problems.append(
                f"recipe for `{target}` invokes {rel}, which does not exist in the repo"
            )
            continue
        prev = _preceding_token(rule.recipe, token)
        if not (prev.endswith(_INTERPRETERS) if prev else False) and not os.access(path, os.X_OK):
            problems.append(
                f"recipe for `{target}` execs {rel} directly and it is not executable "
                f"(mode {oct(path.stat().st_mode & 0o777)})"
            )

    for module in dict.fromkeys(_RECIPE_MODULE.findall(rule.recipe)):
        if module != "pytest":
            ok, why = _module_resolves(module)
            if not ok:
                problems.append(f"recipe for `{target}` runs `-m {module}`, which {why}")

    for _flags, sub in _RECIPE_SUBMAKE.findall(rule.recipe):
        ok, sub_problems = check_make_target(sub, seen)
        if not ok:
            problems.extend(f"via `$(MAKE) {sub}`: {p}" for p in sub_problems)

    for prereq in dict.fromkeys(rule.prereqs):
        if prereq in rules:
            ok, sub_problems = check_make_target(prereq, seen)
            if not ok:
                problems.extend(f"via prerequisite `{prereq}`: {p}" for p in sub_problems)

    return not problems, problems


# =====================================================================================
# Resolving the things commands name
# =====================================================================================

_module_cache: dict[str, tuple[bool, str]] = {}
_collect_cache: dict[str, tuple[bool, str]] = {}
_script_cache: dict[tuple[str, bool], tuple[str, str]] = {}


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PROXYSHOP_WORKER", "0")  # D38: the session aborts without it
    env.pop("PYTEST_ADDOPTS", None)  # inherited addopts must not steer a resolvability probe
    return env


def _module_resolves(module: str) -> tuple[bool, str]:
    """Can ``python -m <module>`` find something? Answered in a subprocess."""
    if module in _module_cache:
        return _module_cache[module]
    code = (
        "import importlib.util as u, sys\n"
        f"sys.exit(0 if u.find_spec({module!r}) is not None else 1)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result = (False, f"could not be probed ({type(exc).__name__}: {exc})")
    else:
        result = (
            (True, "") if proc.returncode == 0 else (False, "is not importable from the repo root")
        )
    _module_cache[module] = result
    return result


def _pytest_collects(rel: str) -> tuple[bool, str]:
    """Does ``pytest --collect-only`` gather at least one test from ``rel``?

    Collection rather than a run: it needs no datastores and no network, finishes in seconds, and
    "pytest cannot collect this path" is a complete answer to "can an operator run this command".
    It is emphatically NOT evidence that the tests prove anything — see the module docstring.
    """
    if rel in _collect_cache:
        return _collect_cache[rel]
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", rel],
            cwd=REPO_ROOT,
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result = (False, f"collection could not be run ({type(exc).__name__}: {exc})")
    else:
        if proc.returncode == 0:
            result = (True, "")
        else:
            tail = [ln for ln in (proc.stdout + "\n" + proc.stderr).splitlines() if ln.strip()][-3:]
            hint = (
                "collected 0 tests" if proc.returncode == 5 else f"pytest exited {proc.returncode}"
            )
            result = (False, f"pytest cannot collect it ({hint}): {' / '.join(tail)}")
    _collect_cache[rel] = result
    return result


def _compose_services() -> tuple[dict[str, list[str]], str]:
    """``{service: profiles}`` merged over the root compose file and its ``include:`` list."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML ships with the repo's deps
        return {}, "PyYAML is not importable, so compose services were not resolved"
    if not ROOT_COMPOSE.is_file():
        return {}, f"{ROOT_COMPOSE.name} does not exist"
    try:
        root = yaml.safe_load(ROOT_COMPOSE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return {}, f"{ROOT_COMPOSE.name} does not parse as YAML ({exc})"

    files = [ROOT_COMPOSE]
    for entry in root.get("include") or []:
        raw = entry.get("path") if isinstance(entry, dict) else entry
        for item in raw if isinstance(raw, list) else [raw]:
            if isinstance(item, str):
                files.append(REPO_ROOT / item)

    services: dict[str, list[str]] = {}
    for path in files:
        if not path.is_file():
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        for name, body in (doc.get("services") or {}).items():
            services[name] = [str(p) for p in ((body or {}).get("profiles") or [])]
    return services, ""


# =====================================================================================
# Classification
# =====================================================================================

_COMPOSE_WORDS = {
    "up", "down", "build", "run", "start", "stop", "restart",
    "logs", "ps", "pull", "config", "exec", "create", "kill", "rm",
}  # fmt: skip


def _is_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_]\w*=", token))


def _positional_args(args: list[str], value_flags: set[str]) -> list[str]:
    """Non-flag operands, skipping the value of every value-taking flag."""
    out: list[str] = []
    skip = False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok in value_flags:
                skip = True
            continue
        out.append(tok)
    return out


def _has_variable(tokens: list[str]) -> bool:
    return any("$" in t for t in tokens)


def _check_script(rel: str, must_be_executable: bool) -> tuple[str, str]:
    """Exists, parses under ``bash -n``, and is not an empty file.

    The emptiness check is the honest limit of static grading: a script whose only line is
    ``exit 0`` passes here, and deciding what it must *do* belongs to the ticket that writes it.
    """
    key = (rel, must_be_executable)
    if key in _script_cache:
        return _script_cache[key]
    path = REPO_ROOT / rel
    if not path.is_file():
        what = "is a directory, not a script" if path.is_dir() else "does not exist in the repo"
        result = (VERDICT_FAIL, f"{rel} {what}")
    elif must_be_executable and not os.access(path, os.X_OK):
        result = (
            VERDICT_FAIL,
            f"{rel} exists but is not executable (mode {oct(path.stat().st_mode & 0o777)})",
        )
    else:
        body = [
            ln.strip()
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not body:
            result = (
                VERDICT_FAIL,
                f"{rel} exists but contains no command — only blanks, comments or a shebang",
            )
        else:
            try:
                proc = subprocess.run(
                    ["bash", "-n", str(path)], capture_output=True, text=True, timeout=120
                )
            except (OSError, subprocess.SubprocessError) as exc:
                result = (VERDICT_FAIL, f"{rel} could not be checked with `bash -n` ({exc})")
            else:
                result = (
                    (VERDICT_PASS, f"{rel} exists, is non-empty and `bash -n` parses it")
                    if proc.returncode == 0
                    else (
                        VERDICT_FAIL,
                        f"{rel} is not parseable by `bash -n`: {proc.stderr.strip()}",
                    )
                )
    _script_cache[key] = result
    return result


def _check_pytest_paths(args: list[str]) -> tuple[str, str, list[str]]:
    paths = _positional_args(args, _PYTEST_VALUE_FLAGS)
    if not paths:
        # Fails CLOSED on purpose. Letting a path-less `pytest -q` grade INFO was the cheapest
        # way found to erase the e2e/test_s1_flow.py defect from this gate's inventory.
        return (
            VERDICT_FAIL,
            "names no path, so the proof this step promises cannot be shown to exist — "
            "a runbook step must name what it runs",
            [],
        )
    details: list[str] = []
    failed = False
    for rel in paths:
        target = REPO_ROOT / rel.split("::")[0]
        if not target.exists():
            failed = True
            details.append(f"{rel} does not exist in the repo")
            continue
        if rel.endswith(".py") and not target.is_file():
            failed = True
            details.append(f"{rel} exists but is a directory, not a test module")
            continue
        ok, why = _pytest_collects(rel)
        if ok:
            details.append(f"{rel} exists and pytest collects it")
        else:
            failed = True
            details.append(f"{rel} exists but {why}")
    return VERDICT_FAIL if failed else VERDICT_PASS, f"{len(paths)} pytest path(s) named", details


def _check_docker_compose(cmd: Command, toks: list[str]) -> Result:
    services, err = _compose_services()
    if err:
        return Result(cmd, VERDICT_INFO, "docker compose", err)
    rest = toks[2:]
    profiles = [rest[i + 1] for i, t in enumerate(rest) if t == "--profile" and i + 1 < len(rest)]
    named = [t for t in rest if t in services]
    if not named:
        unknown = [
            t
            for t in rest
            if not t.startswith("-") and t not in _COMPOSE_WORDS and t not in profiles
        ]
        if unknown:
            return Result(
                cmd,
                VERDICT_FAIL,
                "docker compose service",
                f"the merged compose config defines no service named {', '.join(unknown)}",
            )
        return Result(
            cmd, VERDICT_PASS, "docker compose service", "names no service; nothing to resolve"
        )
    details: list[str] = []
    failed = False
    for svc in named:
        declared = services[svc]
        for profile in profiles:
            if declared and profile not in declared:
                failed = True
                details.append(f"service `{svc}` declares profiles {declared}, not `{profile}`")
        details.append(f"service `{svc}` is defined (profiles {declared or ['<none>']})")
    return Result(
        cmd,
        VERDICT_FAIL if failed else VERDICT_PASS,
        "docker compose service",
        f"{len(named)} service(s) named",
        details,
    )


def classify_and_check(cmd: Command) -> Result:
    """Grade one extracted command."""
    if cmd.kind == "script-reference":
        verdict, reason = _check_script(cmd.text, must_be_executable=False)
        return Result(cmd, verdict, "shell script referenced in the runbook", reason)

    try:
        toks = _strip_redirects(shlex.split(cmd.text))
    except ValueError as exc:
        return Result(cmd, VERDICT_FAIL, "shell command", f"does not parse as shell ({exc})")
    if not toks:
        return Result(cmd, VERDICT_INFO, "empty", "nothing to resolve")

    if toks[0] == "export" or all(_is_assignment(t) for t in toks):
        return Result(
            cmd, VERDICT_PASS, "environment assignment", "sets shell state; nothing to resolve"
        )
    while toks and _is_assignment(toks[0]):
        toks = toks[1:]
    while toks and toks[0] in _WRAPPERS:
        toks = toks[1:]
    if toks[:2] == ["uv", "run"]:
        toks = toks[2:]
        while toks and toks[0].startswith("-"):  # `uv run --frozen python -m pytest ...`
            toks = toks[1:]
    if not toks:
        return Result(cmd, VERDICT_INFO, "wrapper only", "nothing to resolve")

    if _has_variable(toks[1:]):
        return Result(
            cmd,
            VERDICT_INFO,
            UNRESOLVABLE_CATEGORY,
            "carries a shell variable this gate cannot expand, so what it names is unknown; "
            "the armed-sweep guard fails if this category is ever non-empty",
        )

    head = toks[0]
    base = Path(head).name

    if base == "make":
        targets = [t for t in _positional_args(toks[1:], _MAKE_VALUE_FLAGS) if "=" not in t]
        if not targets:
            return Result(
                cmd, VERDICT_FAIL, "make target", "names no target, so nothing can be resolved"
            )
        details: list[str] = []
        failed = False
        for target in targets:
            ok, problems = check_make_target(target)
            if ok:
                details.append(f"`{target}` is defined and everything its recipe invokes resolves")
            else:
                failed = True
                details.extend(problems)
        return Result(
            cmd,
            VERDICT_FAIL if failed else VERDICT_PASS,
            "make target",
            f"{len(targets)} target(s) named",
            details,
        )

    if base.startswith("pytest"):
        verdict, reason, details = _check_pytest_paths(toks[1:])
        return Result(cmd, verdict, "pytest invocation", reason, details)

    if base.startswith("python"):
        rest = toks[1:]
        if rest[:1] == ["-m"] and len(rest) >= 2:
            module = rest[1]
            if module == "pytest":
                verdict, reason, details = _check_pytest_paths(rest[2:])
                return Result(cmd, verdict, "pytest invocation", reason, details)
            ok, why = _module_resolves(module)
            return Result(
                cmd,
                VERDICT_PASS if ok else VERDICT_FAIL,
                "python module",
                f"`{module}` resolves" if ok else f"`{module}` {why}",
            )
        scripts = _positional_args(rest, set())
        if not scripts:
            return Result(
                cmd, VERDICT_FAIL, "python script", "names no script path, so nothing resolves"
            )
        missing = [s for s in scripts if not (REPO_ROOT / s).exists()]
        if missing:
            return Result(
                cmd, VERDICT_FAIL, "python script", f"{', '.join(missing)} does not exist"
            )
        return Result(cmd, VERDICT_PASS, "python script", f"{', '.join(scripts)} exists")

    if base in ("bash", "sh", "zsh"):
        if "-c" in toks[1:]:
            return Result(
                cmd,
                VERDICT_INFO,
                UNRESOLVABLE_CATEGORY,
                "runs an inline script, which names no file to resolve; the armed-sweep guard "
                "fails if this category is ever non-empty",
            )
        scripts = _positional_args(toks[1:], set())
        if not scripts:
            return Result(cmd, VERDICT_FAIL, "shell script", "names no script path")
        verdict, reason = _check_script(scripts[0], must_be_executable=False)
        return Result(cmd, verdict, "shell script", reason)

    if base == "cp":
        operands = _positional_args(toks[1:], set())
        if len(operands) < 2:
            return Result(cmd, VERDICT_FAIL, "cp source", "no source/destination pair to resolve")
        src = operands[0]
        if not (REPO_ROOT / src).exists():
            return Result(
                cmd, VERDICT_FAIL, "cp source", f"{src} does not exist, so this step cannot run"
            )
        return Result(cmd, VERDICT_PASS, "cp source", f"{src} exists")

    if base == "docker" and len(toks) > 1 and toks[1] == "compose":
        return _check_docker_compose(cmd, toks)

    if head.startswith("./") or head.startswith("../"):
        rel = head[2:] if head.startswith("./") else head
        if rel.endswith(".sh"):
            verdict, reason = _check_script(rel, must_be_executable=True)
            return Result(cmd, verdict, "shell script", reason)
        path = REPO_ROOT / rel
        if not path.is_file():
            return Result(cmd, VERDICT_FAIL, "python script", f"{rel} does not exist in the repo")
        if not os.access(path, os.X_OK):
            return Result(
                cmd,
                VERDICT_FAIL,
                "python script",
                f"{rel} is invoked directly but is not executable "
                f"(mode {oct(path.stat().st_mode & 0o777)})",
            )
        return Result(cmd, VERDICT_PASS, "python script", f"{rel} exists and is executable")

    where = shutil.which(head)
    return Result(
        cmd,
        VERDICT_INFO,
        "external tool",
        f"`{head}` is {'on PATH at ' + where if where else 'NOT on PATH'} "
        "(operator environment, not enforced by this gate)",
    )


def audit() -> tuple[list[Command], list[Result], list[tuple[Path, list[Block], list[Command]]]]:
    """Grade every runbook under ``docs/demo/``."""
    commands: list[Command] = []
    per_doc: list[tuple[Path, list[Block], list[Command]]] = []
    for path in runbooks():
        found, blocks = extract_commands(path)
        per_doc.append((path, blocks, found))
        commands.extend(found)
    return commands, [classify_and_check(c) for c in commands], per_doc


# =====================================================================================
# The arming pins — deliberately NOT xfail
# =====================================================================================


def _coverage_gaps(path: Path, commands: list[Command]) -> list[str]:
    """Raw-text mentions of a repo artefact that produced no graded command on their line.

    The second, deliberately stupid parse. It has no idea what a fence is, so no fence-indent
    rule or language list has to be right for it to work — which is the entire point: every
    attempt to enumerate the shapes a markdown document may take produced a bypass and a false
    positive at the same time. The Makefile supplies the ``make`` vocabulary, so this needs no
    list of English words to exclude either.
    """
    text = path.read_text(encoding="utf-8")
    targets = set(parse_makefile(MAKEFILE))
    graded = {c.lineno for c in commands}
    gaps: list[str] = []
    for idx, raw in enumerate(text.splitlines(), start=1):
        mentions = [f"make {m.group(1)}" for m in _RAW_MAKE.finditer(raw) if m.group(1) in targets]
        mentions += [m.group(1) for m in _SCRIPT_REF.finditer(raw)]
        mentions += [m.group(1) for m in _RAW_PY.finditer(raw)]
        if mentions and idx not in graded:
            gaps.append(
                f"  {path.relative_to(REPO_ROOT)}:{idx} mentions {', '.join(sorted(set(mentions)))}"
                f" but the parser graded NOTHING on that line\n      | {raw.strip()[:110]}"
            )
    return gaps


def test_the_parse_covers_the_raw_text() -> None:
    """Everything a dumb raw scan can see must also have been graded by the real parser.

    This is the arming pin that does not have to be right about markdown. Three earlier drafts
    were defeated by edits that changed how the document *parses* without changing what it
    *says* — a fence indented past a bound, a fence relabelled to an allow-listed language, a
    fence deliberately left unclosed. Each one made a real command invisible while the sweep
    still reported a healthy count.

    Reconciling two independent parses catches all of them for one reason: the raw scan cannot
    be hidden from, because it does not interpret anything. If it can see ``make e2e-live`` on a
    line and the real parser graded nothing there, that is a parser failure, and it is reported
    as one instead of being silently counted as zero work.
    """
    docs = runbooks()
    assert docs, f"no runbook markdown found under {RUNBOOK_DIR.relative_to(REPO_ROOT)}/"

    _, _, per_doc = audit()
    gaps: list[str] = []
    for path, _blocks, found in per_doc:
        gaps.extend(_coverage_gaps(path, found))

    assert not gaps, (
        "a raw-text scan found repository artefacts the command parser never graded — a command "
        "block is invisible to it, so the gate below would report green over an ungraded step:\n"
        + "\n".join(gaps)
    )


def test_the_runbook_command_sweep_is_armed() -> None:
    """The parser must keep finding the runbook's real commands, or the gate below is theatre.

    Three sweeps in this repo were caught passing because they iterated **zero** cases
    (T-229 6→0 of 8, T-281 70→0 of 79, T-241 48→0 of 66). A loop over an empty list is green,
    and green here would read as "the runbook is executable". A raw count is not enough of a pin
    either — an adversarial review defeated a count-plus-categories guard three ways, each of
    which left the sweep *reporting* a healthy 15 commands while grading a decoy.
    """
    docs = runbooks()
    assert docs, f"no runbook markdown found under {RUNBOOK_DIR.relative_to(REPO_ROOT)}/"

    commands, results, per_doc = audit()

    for path, blocks, found in per_doc:
        doc = path.relative_to(REPO_ROOT)
        for block in blocks:
            if block.lang not in SHELL_LANGS:
                continue
            end = block.lines[-1][0] if block.lines else block.open_lineno
            got = [c for c in found if c.kind == "shell" and block.open_lineno <= c.lineno <= end]
            assert got, (
                f"{doc}:{block.open_lineno} is a shell code fence that produced ZERO commands. "
                f"An empty sweep over a real block PASSES, which is exactly the quiet-sweep "
                f"failure this guard exists to prevent."
            )

    resolvable = [r for r in results if r.category in RESOLVABLE_CATEGORIES]
    assert len(resolvable) >= MIN_RESOLVABLE_COMMANDS, (
        f"the sweep extracted {len(commands)} command(s) but only {len(resolvable)} name anything "
        f"this gate can resolve (floor {MIN_RESOLVABLE_COMMANDS}). A sweep padded with "
        f"environment assignments grades nothing while looking healthy."
    )

    open_failures = [
        r
        for r in results
        if r.verdict == VERDICT_INFO
        and (r.category in RESOLVABLE_CATEGORIES or r.category == UNRESOLVABLE_CATEGORY)
    ]
    assert not open_failures, (
        "these commands name something in the repository, or hide it behind a shell variable, "
        "but were graded INFO rather than PASS/FAIL — the gate is failing OPEN on them:\n"
        + "\n".join(r.render() for r in open_failures)
    )

    categories = {r.category for r in results}
    for required, label in (
        ("make target", "at least one `make <target>` command"),
        ("pytest invocation", "at least one pytest invocation"),
        ("shell script referenced in the runbook", "at least one *.sh reference"),
    ):
        assert required in categories, (
            f"the sweep found no {label} under {RUNBOOK_DIR.relative_to(REPO_ROOT)}/; the parser "
            f"and the documents have drifted apart. Categories found: {sorted(categories)}"
        )


# =====================================================================================
# The executability gate
# =====================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the demo runbook's two headline commands do not resolve: "
        "`uv run python -m pytest e2e/test_s1_flow.py -q` names a module that is not in the "
        "repo (T-082, which would have added it, was rejected and its branch is unmerged), and "
        "`make e2e-live` shells out to docs/demo/e2e_live.sh, which was never written, so the "
        "target dies with `No such file or directory` (bash 127, make exit 2). The frozen E8 "
        "acceptance checks only that the Makefile DEFINES the targets, so both read green. "
        "Remove this marker once the runbook's commands actually run."
    ),
)
def test_c19_every_command_the_demo_runbook_names_resolves() -> None:
    """Every command in every ``docs/demo/`` runbook must resolve to something real.

    Asserts the behaviour that SHOULD hold — an operator can type each command in the runbook and
    have it reach real code — never the behaviour that does. A test pinning today's output would
    certify the defect.

    The failure message is the point as much as the red is: it names the document, the line, the
    command and what is missing, so the gate reports *which step is broken* rather than only that
    something is.
    """
    commands, results, _ = audit()

    assert commands, "extracted 0 commands — refusing to report PASS on an empty sweep"

    failures = [r for r in results if r.verdict == VERDICT_FAIL]
    passes = [r for r in results if r.verdict == VERDICT_PASS]
    infos = [r for r in results if r.verdict == VERDICT_INFO]

    if failures:
        report = "\n".join(r.render() for r in failures)
        context = "\n".join(r.render() for r in passes + infos)
        raise AssertionError(
            f"{len(failures)} of {len(commands)} commands the demo runbook instructs an "
            f"operator to run do not resolve:\n\n{report}\n\n"
            f"--- the {len(passes) + len(infos)} that do ---\n{context}\n\n"
            f"(swept {len(commands)} commands out of "
            f"{[str(p.relative_to(REPO_ROOT)) for p in runbooks()]}; "
            f"{len(passes)} PASS, {len(failures)} FAIL, {len(infos)} INFO)"
        )
