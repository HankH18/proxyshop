"""Does the demo runbook's procedure actually *run*? — an executability gate.

``.swarm-loop/acceptance/test_e8_proofs.py`` certifies ``docs/demo/starting-slice.md`` by
checking that the ``make`` targets it names are **defined in the Makefile**. Definition is
shape, not behaviour: ``e2e-live:`` is defined, and running it prints
``bash: docs/demo/e2e_live.sh: No such file or directory``. So E8 reads 8/8 and acceptance
reads 120/120 while the document an operator would follow is broken at two of its steps.

This file grades the other thing — **resolvability**. For every command the runbook tells an
operator to type, it asks whether the thing that command names is really there:

* ``make <target>`` — the target is defined, it has a recipe **of its own**, that recipe is
  defined exactly once, and every ``.sh``/``.py`` it invokes exists (and carries ``+x`` when the
  recipe execs it directly rather than handing it to an interpreter). Every ``-m <module>``,
  every prerequisite and every ``$(MAKE)`` recursion is checked the same way.
* ``pytest <path>`` — the path exists, is a file when it names one, **and** a real
  ``pytest --collect-only`` subprocess collects at least one test from it.
* ``python <path>`` / ``python -m <module>`` — the script exists, or the module resolves.
* every ``*.sh`` named **anywhere**, prose included — it exists, ``bash -n`` parses it, and it
  carries at least one line that is not a shebang, a comment or blank.
* ``cp <src> <dst>`` — ``<src>`` exists.
* ``docker compose --profile <p> ... <service>`` — the merged compose config defines
  ``<service>`` with that profile.

The command list is parsed out of the documents, never hardcoded, and every markdown file under
``docs/demo/`` is swept rather than one named document — the runbook repeatedly promises an
"extension runbook", and that page must be graded the day it lands.

**Nothing may fall out of the sweep silently.** This is the failure mode this repo keeps being
bitten by — three sweeps were caught going quiet rather than red (T-229 6→0 of 8, T-281 70→0 of
79, T-241 48→0 of 66), and three successive drafts of *this file* were defeated the same way by
adversarial review. So a command is never simply skipped:

* A command whose head this parser cannot name — ``$PYTHON``, a wrapper script, a shell
  function — has its **arguments resolved anyway**. ``$PYTHON -m pytest e2e/test_s1_flow.py -q``
  is graded exactly like the ``python`` spelling. A program we cannot classify must never take
  the files it names out of the sweep with it; that was a real bypass, not a hypothetical.
* Only a command that names nothing checkable lands in the explicit ``UNCLASSIFIED`` bucket, and
  that bucket is **counted and printed** in the gate's own report. "I could not classify these
  three commands" is a fine outcome; not mentioning them is not.

**Two independent parses, cross-checked — because enumerating markdown does not work.** Every
rule that enumerated a *shape* a document may take became simultaneously a bypass and a false
positive. Bounding fence indentation at three spaces (CommonMark measures it relative to the
containing block) hid a fence at column 4 inside a list item *and* dropped legitimate nested
fences. Allow-listing fence languages sanctioned hiding the procedure under ```` ```text ````
*and* hard-failed on ```` ```plaintext ````. So fence indentation is unbounded and an unknown
language is skipped, and the arming pin is instead ``test_the_parse_covers_the_raw_text``: a
second, deliberately stupid scan of the raw bytes for every ``make`` target the **Makefile**
defines, every ``*.sh`` token and every ``dir/file.py`` token, each of which must have produced a
graded command on that line. A fence this parser cannot read is then a loud mismatch instead of a
silent zero, and no fence rule has to be right for the gate to hold.

**What this gate deliberately does NOT grade: semantics.** ``docs/demo/e2e_live.sh`` containing
only ``exit 0`` resolves, and this file passes it. That is the correct boundary — what that
script must *do* is the specification of the ticket that writes it, and a gate that guessed would
be grading its own opinion. The same holds for ``collects >= 1``: it proves pytest can reach the
module, not that the module proves S1. Read a green here as "the runbook's steps reach real
code", never as "the demo works".

What this file enforces is what the **repository** controls. Whether ``docker``, ``uv`` or Node
is installed on the operator's laptop is INFO, not failure; ``.venv/`` paths are skipped because
``make bootstrap`` is what creates them.

**This gate does not replace the frozen E8 check.** E8 pins the runbook's *shape* — that it still
names ``make e2e-live``. This pins *executability* — that what it names resolves. A runbook
edited to stop naming a broken command satisfies this file and is caught by E8; a command named
but broken satisfies E8 and is caught here.

Mechanism, both directions (the ``test_repro_open_tickets.py`` idiom):

* a normal run reports ``xfailed`` and exits 0, so ``make verify`` stays green;
* the ticket's gate runs this file with ``--runxfail`` and gets a real ``1 failed``, whose
  message names the broken step rather than only going red;
* ``strict=True`` turns the eventual repair into an XPASS *failure*, so whoever writes
  ``docs/demo/e2e_live.sh`` and ``e2e/test_s1_flow.py`` must delete the marker.

This file writes the gate and nothing else — a lane that repairs the defect it was asked to
reproduce destroys the gate that would have graded the repair. No ticket id is minted for this
work yet, so the tests are named for the lane rather than squatting an id.
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
#: three ``VAR=value`` lines would inflate. Today's measurement is 17 raw, 14 resolvable.
MIN_RESOLVABLE_COMMANDS = 5

#: Fence languages carrying an operator procedure. Anything else is skipped rather than
#: rejected: the raw-text cross-check, not this list, is what stops a command hiding in one.
SHELL_LANGS = {
    "", "bash", "sh", "shell", "zsh",
    "console", "shell-session", "shellsession", "console-session", "sh-session", "terminal",
}  # fmt: skip

#: Session/transcript fences interleave commands with their OUTPUT, so only prompt lines are
#: commands there. Parsing output as commands made `lint ok; don't rerun` an unparseable FAIL.
PROMPT_LANGS = {"console", "shell-session", "shellsession", "console-session", "sh-session", "terminal"}  # fmt: skip

#: Programs that take a script path as an argument, so the path must exist but needs no ``+x``.
#: Matched on the BASENAME, never with ``str.endswith`` — the latter made every token ending in
#: "sh" (``verify.sh``, ``publish``) an interpreter, and every token ending in "." as well.
_INTERPRETER_NAMES = {"bash", "sh", "zsh", "dash", "python", "python3", "uv", "pytest", "node"}
_SOURCE_TOKENS = {"source", "."}

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
#: A command this parser could not classify AND which names nothing checkable. Counted and
#: printed by the gate — never silently dropped, which is the whole point of having the bucket.
VERDICT_UNCLASSIFIED = "UNCLASSIFIED"

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
    "operands of an unclassified program",
}


class RunbookParseError(AssertionError):
    """The document is shaped in a way this parser cannot honestly grade."""


def _is_interpreter(token: str) -> bool:
    """Does ``token`` name a program that takes a script path as its argument?"""
    tok = token.strip("\"'").lstrip("@-")
    return tok in _SOURCE_TOKENS or Path(tok).name in _INTERPRETER_NAMES


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
    #: Every physical line this command occupies. A backslash-continued command is reported at
    #: its first line but *covers* all of them — crediting only the first made the raw-text
    #: cross-check fire on correctly-graded multi-line commands.
    span: frozenset[int] = field(default_factory=frozenset)

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

#: Any shell-script path named anywhere, prose included. ``(?!\.\w)`` stops ``x.sh.bak`` being
#: read as ``x.sh``, while a SENTENCE-ENDING period still matches — ``…e2e_live.sh.`` hid the
#: path from both parses at once when the guard was the blunter ``(?![\w.])``.
_SCRIPT_REF = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.sh)(?![\w]|\.\w)")

#: A python path, for the raw-text cross-check. A directory component is required so that prose
#: mentioning a bare ``foo.py`` is not treated as a repo artefact.
_RAW_PY = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.py)(?![\w]|\.\w)")

_BACKTICK_SPAN = re.compile(r"`[^`]*`")


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


def _logical_lines(
    body: list[tuple[int, str]], prompt_only: bool
) -> list[tuple[int, frozenset[int], str]]:
    """``(first_lineno, every_lineno_covered, command_text)``.

    Joins backslash continuations and strips comments. ``#`` starts a comment and never a root
    prompt — treating ``# `` as a prompt turned ``# don't run this twice`` into an unparseable
    command. In a session/transcript fence ``prompt_only`` is set and lines without a ``$``/``%``
    prompt are program OUTPUT, not commands.
    """
    joined: list[tuple[int, list[int], str]] = []
    pending: str | None = None
    first = 0
    covered: list[int] = []
    for lineno, raw in body:
        line = raw.rstrip()
        if pending is not None:
            pending = pending + " " + line.strip()
            covered.append(lineno)
        else:
            first, covered, pending = lineno, [lineno], line
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        joined.append((first, covered, pending))
        pending = None
    if pending is not None:
        joined.append((first, covered, pending))

    out: list[tuple[int, frozenset[int], str]] = []
    for lineno, lines, line in joined:
        text = line.strip()
        prompted = text[:2] in ("$ ", "% ")
        if prompted:
            text = text[2:]
        elif prompt_only:
            continue  # program output inside a transcript block
        if text.startswith("#"):
            continue
        text = _strip_comment(text)
        if text:
            out.append((lineno, frozenset(lines), text))
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


_BASH_C = re.compile(
    r"^(?:[\w./-]*/)?(?:bash|sh|zsh)\s+(?:-\w+\s+)*-c\s+(?P<q>['\"])(?P<script>.+)(?P=q)\s*$"
)


def _expand_inline_script(piece: str) -> list[str]:
    """``bash -c 'make check && make demo-seed'`` becomes the two commands it actually runs.

    Grading the wrapper instead of its contents left the inner commands ungraded while the raw
    text still named them, which reddened the cross-check on a perfectly legitimate line — and,
    worse, would have let a real step hide inside a quoted string.
    """
    m = _BASH_C.match(piece.strip())
    if not m:
        return [piece]
    return _split_chain(m.group("script")) or [piece]


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
        for lineno, span, line in _logical_lines(block.lines, block.lang in PROMPT_LANGS):
            for piece in _split_chain(line):
                for sub in _expand_inline_script(piece):
                    commands.append(
                        Command(text=sub, doc=doc, lineno=lineno, kind="shell", span=span)
                    )

    prose = _mask_fences(text, blocks)
    for hit in _INLINE_MAKE.finditer(prose):
        start = prose[: hit.start()].count("\n") + 1
        end = prose[: hit.end()].count("\n") + 1
        commands.append(
            Command(
                text=" ".join(hit.group(1).split()),
                doc=doc,
                lineno=start,
                kind="inline-make",
                span=frozenset(range(start, end + 1)),
            )
        )

    for hit in _SCRIPT_REF.finditer(text):
        lineno = text[: hit.start()].count("\n") + 1
        commands.append(
            Command(
                text=hit.group(1),
                doc=doc,
                lineno=lineno,
                kind="script-reference",
                span=frozenset({lineno}),
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
    #: How many separate rule lines gave this target a recipe. GNU make keeps only the LAST for
    #: a single-colon target, so more than one means the file disagrees with itself.
    recipe_definitions: int = 0
    double_colon: bool = False


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
    defs: dict[str, int] = {}
    dcolon: dict[str, bool] = {}
    variables: dict[str, str] = {}
    current: list[str] = []
    current_counted: set[str] = set()

    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("\t"):
            for name in current:
                bodies.setdefault(name, []).append(raw.strip())
                if name not in current_counted:
                    defs[name] = defs.get(name, 0) + 1
                    current_counted.add(name)
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
        current, current_counted = names, set()
        recipe = (m.group("recipe") or "").strip()
        deps = [d for d in m.group("prereqs").split() if d != "|"]
        for name in names:
            bodies.setdefault(name, [])
            prereqs.setdefault(name, []).extend(deps)
            dcolon[name] = dcolon.get(name, False) or m.group("colons") == "::"
            if recipe:
                bodies[name].append(recipe)
                defs[name] = defs.get(name, 0) + 1
                current_counted.add(name)

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
        name: Rule(
            recipe=expand("\n".join(body)),
            prereqs=prereqs.get(name, []),
            recipe_definitions=defs.get(name, 0),
            double_colon=dcolon.get(name, False),
        )
        for name, body in bodies.items()
    }


#: Any REPO-RELATIVE path a recipe names, whatever its extension. Restricting this to ``.sh``
#: and ``.py`` left every other program a recipe invokes ungraded, and six one-line recipe edits
#: — ``@node docs/demo/e2e_live.js``, ``@bash …e2e_live.sh && ./scripts/e2e_live_extra`` — turned
#: a target that dies with exit 2 into a green gate without the runbook being touched at all.
#: The lookbehind excludes absolute paths: ``/bin/bash`` is not a file in this repository.
_RECIPE_PATH = re.compile(r"(?<![\w/.\-$])((?:\./)?(?:[\w.-]+/)+[\w.-]+|\./[\w.-]+)(?![\w/])")
_RECIPE_MODULE = re.compile(r"-m\s+([A-Za-z_][\w.]*)")
_UNEXPANDED = re.compile(r"\$[({](?!MAKE[)}])(?P<name>[A-Za-z_][\w.]*)[)}]")

#: Shell syntax and builtins, which name no program to resolve.
_SHELL_WORDS = {
    "echo", "cd", "exit", "true", "false", "set", "test", "read", "printf", "shift", "local",
    "return", "eval", "trap", "unset", "export", "source", "wait", "umask", "ulimit", "times",
    "[", "]", "[[", "]]", "{", "}", "(", ")", ":", "if", "then", "else", "elif", "fi", "for",
    "while", "until", "do", "done", "case", "esac", "function", "&&", "||", "|",
}  # fmt: skip


def _recipe_chunks(recipe: str) -> list[list[str]]:
    """Each command in a recipe, as a token list, split on newlines and shell separators."""
    chunks: list[list[str]] = []
    for line in recipe.split("\n"):
        for chunk in re.split(r"&&|\|\||[;|]", line):
            toks = chunk.split()
            if toks:
                chunks.append(toks)
    return chunks


def _recipe_head(toks: list[str]) -> str:
    """The program a recipe chunk runs, past ``@``/``-`` prefixes and ``VAR=value`` settings."""
    for idx, tok in enumerate(toks):
        word = tok.lstrip("@-+") if idx == 0 else tok
        if not word or _is_assignment(word):
            continue
        return word.strip("\"'")
    return ""


def _undefined_program_vars(recipe: str) -> list[str]:
    """Undefined ``$(VAR)`` tokens sitting where a **program or script path** would go.

    The hole being closed is ``@bash $(NOPE)``: expanding an unassigned variable to the empty
    string made the path vanish, so the target passed by naming nothing. The hole NOT being
    closed is ``--category "$(SEED_CATEGORY)"`` — the runbook states plainly that ``demo-seed``
    reads that from the operator's environment, so an unassigned *data argument* is correct and
    flagging it was a false positive. Position, not presence, distinguishes them.
    """
    found: list[str] = []
    for chunk in re.split(r"[;&|]+", recipe.replace("\n", " ")):
        toks = chunk.split()
        for idx, tok in enumerate(toks):
            hits = [m.group("name") for m in _UNEXPANDED.finditer(tok)]
            if hits and (idx == 0 or _is_interpreter(toks[idx - 1])):
                found.extend(hits)
    return found


def _preceding_token(recipe: str, path_token: str) -> str:
    """The token before each occurrence of ``path_token``; an interpreter anywhere wins."""
    best = ""
    for chunk in re.split(r"[;&|]+", recipe.replace("\n", " ")):
        toks = chunk.split()
        for idx, tok in enumerate(toks):
            if tok.strip("\"'") != path_token:
                continue
            prev = toks[idx - 1] if idx else ""
            if prev and _is_interpreter(prev):
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

    # The Makefile sweep's arming pin, and it has no exemption. An earlier draft let a target
    # with no recipe pass when a PREREQUISITE had one — which is exactly how `e2e-live: preflight`
    # turned the broken target green while `make -n e2e-live` printed only ./scripts/preflight.sh.
    if not rule.recipe.strip():
        return False, [
            f"target `{target}:` is defined with NO recipe of its own — `make {target}` would run "
            f"only its prerequisites {rule.prereqs or '[]'}, so this gate cannot confirm it runs "
            f"anything the runbook's step promises"
        ]

    if rule.recipe_definitions > 1 and not rule.double_colon:
        problems.append(
            f"target `{target}:` is given a recipe {rule.recipe_definitions} times; GNU make "
            f"keeps only the LAST for a single-colon rule, so what an operator would actually run "
            f"cannot be determined from this file"
        )

    for hit in dict.fromkeys(_undefined_program_vars(rule.recipe)):
        problems.append(
            f"recipe for `{target}` runs `$({hit})` as a program or script, and the Makefile "
            f"never assigns it — so what this target executes cannot be resolved, and a missing "
            f"file behind that variable would be invisible to this gate"
        )

    for token in dict.fromkeys(_RECIPE_PATH.findall(rule.recipe)):
        rel = token[2:] if token.startswith("./") else token
        if rel.startswith(".venv/") or "/.venv/" in token:
            continue  # provisioned environment, not a repo artefact
        path = REPO_ROOT / rel
        if not path.exists():
            problems.append(
                f"recipe for `{target}` invokes {rel}, which does not exist in the repo"
            )
            continue
        if rel.endswith((".sh", ".py")) and not path.is_file():
            problems.append(f"recipe for `{target}` invokes {rel}, which is not a file")
            continue
        prev = _preceding_token(rule.recipe, token)
        if not (prev and _is_interpreter(prev)) and not os.access(path, os.X_OK):
            problems.append(
                f"recipe for `{target}` execs {rel} directly and it is not executable "
                f"(mode {oct(path.stat().st_mode & 0o777)})"
            )

    for module in dict.fromkeys(_RECIPE_MODULE.findall(rule.recipe)):
        if module != "pytest":
            ok, why = _module_resolves(module)
            if not ok:
                problems.append(f"recipe for `{target}` runs `-m {module}`, which {why}")

    # EVERY program the recipe runs, not just the two extensions this gate happens to parse.
    # Grading only `.sh`/`.py` left `@node docs/demo/e2e_live.js`, `@proxyshop-live-runner`,
    # `@docker compose run --rm no-such-service` and `${MAKE} does-not-exist` all unchecked —
    # six one-line recipe edits that turned a target dying with exit 2 into a green gate without
    # the runbook being touched, so the "E8 catches the other direction" argument cannot apply.
    services: dict[str, list[str]] | None = None
    for toks in _recipe_chunks(rule.recipe):
        head = _recipe_head(toks)
        if not head or head in _SHELL_WORDS or "/" in head:
            continue  # shell syntax, or a repo path the scan above already graded
        rest = toks[toks.index(head) + 1 :] if head in toks else toks[1:]
        if re.fullmatch(r"\$[({]MAKE[)}]", head):
            for sub in _positional_args(rest, _MAKE_VALUE_FLAGS):
                if "=" in sub:
                    continue
                ok, sub_problems = check_make_target(sub, seen)
                if not ok:
                    problems.extend(f"via `$(MAKE) {sub}`: {p}" for p in sub_problems)
            continue
        if "$" in head:
            continue  # an undefined program variable is reported by _undefined_program_vars
        if head == "docker" and rest[:1] == ["compose"]:
            if services is None:
                services = _compose_services()[0]
            named = _positional_args(
                rest[1:], {"--profile", "-f", "--file", "-p", "--project-name"}
            )
            unknown = [t for t in named if t not in services and t not in _COMPOSE_WORDS]
            if services and unknown:
                problems.append(
                    f"recipe for `{target}` runs `docker compose` against "
                    f"{', '.join(unknown)}, which the merged compose config defines as no service"
                )
            continue
        if not shutil.which(head):
            problems.append(
                f"recipe for `{target}` invokes `{head}`, which is neither a file in this "
                f"repository nor a program on PATH, so this target cannot run as written"
            )

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


def _repo_shaped(token: str) -> bool:
    """Does ``token`` look like a path into this repository rather than an option value?"""
    if token.startswith("-") or "$" in token:
        return False
    base = token.split("::")[0]
    return base.endswith(".sh") or ("/" in base and base.endswith(".py"))


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


def _check_one_path(rel: str) -> tuple[bool, str]:
    """Resolve one repo-relative operand, choosing the strongest applicable check."""
    target = REPO_ROOT / rel.split("::")[0]
    if not target.exists():
        return False, f"{rel} does not exist in the repo"
    if rel.endswith(".sh"):
        verdict, reason = _check_script(rel, must_be_executable=False)
        return verdict == VERDICT_PASS, reason
    if rel.endswith(".py") and not target.is_file():
        return False, f"{rel} exists but is a directory, not a module"
    if Path(rel).name.startswith("test_"):
        ok, why = _pytest_collects(rel)
        return (True, f"{rel} exists and pytest collects it") if ok else (False, f"{rel} {why}")
    return True, f"{rel} exists"


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


def _resolve_loose_operands(cmd: Command, toks: list[str]) -> Result | None:
    """Grade a command by its ARGUMENTS when its head cannot be classified.

    This closes a real bypass, not a hypothetical one: ``$PYTHON -m pytest e2e/test_s1_flow.py``
    kept the missing module visibly named in the runbook — so the raw-text cross-check stayed
    quiet — while the classifier dropped the whole command into an uninspected "external tool"
    bucket and the FAIL disappeared. A program this parser cannot name must never take the files
    it names out of the sweep with it. Returns ``None`` when the command names nothing checkable,
    which is the only case allowed to reach ``UNCLASSIFIED``.
    """
    if "-m" in toks:
        after = toks[toks.index("-m") + 1 :]
        if after[:1] == ["pytest"]:
            verdict, reason, details = _check_pytest_paths(after[1:])
            return Result(cmd, verdict, "pytest invocation", reason, details)

    operands = [t for t in toks[1:] if _repo_shaped(t)]
    if not operands:
        return None
    details: list[str] = []
    failed = False
    for rel in operands:
        ok, why = _check_one_path(rel)
        details.append(why)
        failed = failed or not ok
    return Result(
        cmd,
        VERDICT_FAIL if failed else VERDICT_PASS,
        "operands of an unclassified program",
        f"`{toks[0]}` is not a program this gate can classify, so it was graded by the "
        f"{len(operands)} repository path(s) it names",
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
        return Result(cmd, VERDICT_UNCLASSIFIED, "unclassified command", "nothing to resolve")

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
        return Result(cmd, VERDICT_UNCLASSIFIED, "unclassified command", "nothing to resolve")

    head = toks[0]
    # Lower-cased: this filesystem is case-insensitive, so `Make e2e-live` runs the real make.
    # Leaving it case-sensitive dropped that spelling into the uninspected `external tool`
    # bucket — the same silent-exit bypass, wearing a capital letter.
    base = Path(head).name.lower()

    if base == "make" and "$" not in head:
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

    if base.startswith("pytest") and "$" not in head:
        verdict, reason, details = _check_pytest_paths(toks[1:])
        return Result(cmd, verdict, "pytest invocation", reason, details)

    if base.startswith("python") and "$" not in head:
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
        scripts = [s for s in _positional_args(rest, set()) if "$" not in s]
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

    if base in ("bash", "sh", "zsh") and "$" not in head and "-c" not in toks[1:]:
        scripts = [s for s in _positional_args(toks[1:], set()) if "$" not in s]
        if not scripts:
            return Result(cmd, VERDICT_FAIL, "shell script", "names no script path")
        verdict, reason = _check_script(scripts[0], must_be_executable=False)
        return Result(cmd, verdict, "shell script", reason)

    if base == "cp" and "$" not in head:
        operands = _positional_args(toks[1:], set())
        if len(operands) < 2:
            return Result(cmd, VERDICT_FAIL, "cp source", "no source/destination pair to resolve")
        src = operands[0]
        if "$" in src:
            return Result(
                cmd,
                VERDICT_UNCLASSIFIED,
                "unclassified command",
                f"the source `{src}` is behind a shell variable this gate cannot expand",
            )
        if not (REPO_ROOT / src).exists():
            return Result(
                cmd, VERDICT_FAIL, "cp source", f"{src} does not exist, so this step cannot run"
            )
        return Result(cmd, VERDICT_PASS, "cp source", f"{src} exists")

    if base == "docker" and len(toks) > 1 and toks[1] == "compose" and "$" not in head:
        return _check_docker_compose(cmd, toks)

    if (head.startswith("./") or head.startswith("../")) and "$" not in head:
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

    loose = _resolve_loose_operands(cmd, toks)
    if loose is not None:
        return loose

    where = shutil.which(head) if "$" not in head else None
    if where:
        return Result(
            cmd,
            VERDICT_INFO,
            "external tool",
            f"`{head}` is on PATH at {where} and names no repository path "
            "(operator environment, not enforced by this gate)",
        )
    return Result(
        cmd,
        VERDICT_UNCLASSIFIED,
        "unclassified command",
        f"`{head}` is neither a program this gate classifies nor on PATH, and the command names "
        f"no repository path — it is REPORTED rather than skipped, because a command that falls "
        f"out of the sweep silently is how this gate would certify an ungraded step",
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


def _raw_make_mentions(line: str, targets: set[str]) -> list[str]:
    """``make <target>`` mentions on ``line`` that a reader would read as a command.

    Only a mention at the start of the line (a code block) or inside an inline ``code`` span
    counts. Without that, the English sentence "will make check pass on the first try" reads as a
    reference to the ``check`` target and the cross-check fires on prose. Flags between ``make``
    and the target are skipped, because ``make -f Makefile e2e-live`` was a bypass, and the match
    is case-insensitive because this filesystem resolves ``Make`` too.
    """
    spans = [(m.start(), m.end()) for m in _BACKTICK_SPAN.finditer(line)]
    out: list[str] = []
    for m in re.finditer(r"(?<![\w-])make\b", line, re.IGNORECASE):
        # `make` must sit where a COMMAND sits, not merely at the start of the line. Requiring
        # line-initial-or-backticked let `time make e2e-live`, `$ make e2e-live` and
        # `cd "$REPO" && make e2e-live` vanish from this scan AND from the real parser at once
        # (in a fence language the parser skips) — a full bypass. Accepting any position instead
        # reads the English sentence "will make check pass" as a command. So: the text before it
        # must be empty, a shell separator, a prompt, a backtick, or a wrapper word.
        prefix = line[: m.start()].rstrip()
        last = prefix.split()[-1] if prefix.split() else ""
        command_position = (
            not prefix
            or prefix.endswith(("`", "&", "|", ";", "(", "{", "$", "%", ">"))
            or last in _WRAPPERS
            or last in {"time", "then", "else", "do", "&&", "||"}
        )
        if not (command_position or any(a < m.start() < b for a, b in spans)):
            continue
        # Consume flags (skipping the value of a value-taking one), collect target words, and
        # STOP at the first token that is neither. Scanning to end-of-line instead read
        # "`FATAL: run 'make bootstrap' first` — `deps-up` found no virtualenv" as
        # `make deps-up`, which is a sentence, not a command.
        skip_value = False
        for tok in line[m.end() :].split():
            word = tok.strip("`'\".,;:()")
            if skip_value:
                skip_value = False
                continue
            if word.startswith("-"):
                skip_value = "=" not in word and word in _MAKE_VALUE_FLAGS
                continue
            if word in targets:
                out.append(f"make {word}")
                continue
            break
    return out


def _named_by_line(commands: list[Command]) -> tuple[dict[int, set[str]], dict[int, set[str]]]:
    """``({line: make targets graded there}, {line: repo paths graded there})``.

    The cross-check compares per MENTION, not per line, and this is what makes that possible.
    Per-line coverage was exploitable: ``make check   # then: make e2e-live`` has the comment
    stripped by the real parser, so ``e2e-live`` went ungraded while the decoy ``make check`` on
    the same line kept the line "covered" and the raw scan silent.
    """
    makes: dict[int, set[str]] = {}
    paths: dict[int, set[str]] = {}
    for cmd in commands:
        lines = cmd.span or frozenset({cmd.lineno})
        if cmd.kind == "script-reference":
            for ln in lines:
                paths.setdefault(ln, set()).add(cmd.text)
            continue
        try:
            toks = _strip_redirects(shlex.split(cmd.text))
        except ValueError:
            continue
        while toks and _is_assignment(toks[0]):
            toks = toks[1:]
        while toks and toks[0] in _WRAPPERS:
            toks = toks[1:]
        if toks[:2] == ["uv", "run"]:
            toks = toks[2:]
            while toks and toks[0].startswith("-"):
                toks = toks[1:]
        if not toks:
            continue
        if Path(toks[0]).name.lower() == "make":
            for target in _positional_args(toks[1:], _MAKE_VALUE_FLAGS):
                if "=" not in target:
                    for ln in lines:
                        makes.setdefault(ln, set()).add(target)
        for tok in toks[1:]:
            if _repo_shaped(tok):
                for ln in lines:
                    paths.setdefault(ln, set()).add(tok.split("::")[0])
    return makes, paths


def _coverage_gaps(path: Path, commands: list[Command]) -> list[str]:
    """Raw-text mentions of a repo artefact that no graded command on that line actually names.

    The second, deliberately stupid parse. It has no idea what a fence is, so no fence-indent
    rule or language list has to be right for it to work — which is the entire point: every
    attempt to enumerate the shapes a markdown document may take produced a bypass and a false
    positive at the same time. The Makefile supplies the ``make`` vocabulary, so this needs no
    list of English words to exclude either.
    """
    text = path.read_text(encoding="utf-8")
    targets = set(parse_makefile(MAKEFILE))
    graded_makes, graded_paths = _named_by_line(commands)
    gaps: list[str] = []
    for idx, raw in enumerate(text.splitlines(), start=1):
        missing: list[str] = []
        for mention in _raw_make_mentions(raw, targets):
            if mention.split(maxsplit=1)[1] not in graded_makes.get(idx, set()):
                missing.append(f"`{mention}`")
        for hit in _SCRIPT_REF.finditer(raw):
            if hit.group(1) not in graded_paths.get(idx, set()):
                missing.append(hit.group(1))
        for hit in _RAW_PY.finditer(raw):
            if hit.group(1) not in graded_paths.get(idx, set()):
                missing.append(hit.group(1))
        if missing:
            gaps.append(
                f"  {path.relative_to(REPO_ROOT)}:{idx} mentions {', '.join(sorted(set(missing)))}"
                f" but the parser graded no command naming it on that line"
                f"\n      | {raw.strip()[:110]}"
            )
    return gaps


def test_the_parse_covers_the_raw_text() -> None:
    """Everything a dumb raw scan can see must also have been graded by the real parser.

    This is the arming pin that does not have to be right about markdown. Three earlier drafts
    were defeated by edits that changed how the document *parses* without changing what it
    *says* — a fence indented past a bound, a fence relabelled to an allow-listed language, a
    fence deliberately left unclosed, a ``make`` invocation wearing a flag. Each made a real
    command invisible while the sweep still reported a healthy count.

    Reconciling two independent parses catches all of them for one reason: the raw scan cannot be
    hidden from, because it does not interpret anything.
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
    (T-229 6→0 of 8, T-281 70→0 of 79, T-241 48→0 of 66). A loop over an empty list is green, and
    green here would read as "the runbook is executable". A raw count is not enough of a pin
    either — a count-plus-categories guard was defeated three ways, each of which left the sweep
    *reporting* a healthy 15 commands while grading a decoy.
    """
    docs = runbooks()
    assert docs, f"no runbook markdown found under {RUNBOOK_DIR.relative_to(REPO_ROOT)}/"

    commands, results, per_doc = audit()

    for path, blocks, found in per_doc:
        doc = path.relative_to(REPO_ROOT)
        for block in blocks:
            if block.lang not in SHELL_LANGS or block.lang in PROMPT_LANGS:
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

    failing_open = [
        r for r in results if r.verdict == VERDICT_INFO and r.category in RESOLVABLE_CATEGORIES
    ]
    assert not failing_open, (
        "these commands name something in the repository but were graded INFO rather than "
        "PASS/FAIL — the gate is failing OPEN on them:\n"
        + "\n".join(r.render() for r in failing_open)
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

    The message is the point as much as the red is: it names the document, the line, the command
    and what is missing, so the gate reports *which step is broken*. It also prints the
    UNCLASSIFIED bucket unconditionally, including on the failing path — a command this parser
    could not grade is a hole in the sweep, and the one thing it must never be is invisible.
    """
    commands, results, _ = audit()

    assert commands, "extracted 0 commands — refusing to report PASS on an empty sweep"

    failures = [r for r in results if r.verdict == VERDICT_FAIL]
    passes = [r for r in results if r.verdict == VERDICT_PASS]
    infos = [r for r in results if r.verdict == VERDICT_INFO]
    unclassified = [r for r in results if r.verdict == VERDICT_UNCLASSIFIED]

    tally = (
        f"(swept {len(commands)} commands out of "
        f"{[str(p.relative_to(REPO_ROOT)) for p in runbooks()]}; {len(passes)} PASS, "
        f"{len(failures)} FAIL, {len(infos)} INFO, {len(unclassified)} UNCLASSIFIED)"
    )
    unclassified_report = (
        "\n\n--- UNCLASSIFIED: named nothing this gate could resolve ---\n"
        + "\n".join(r.render() for r in unclassified)
        if unclassified
        else ""
    )

    if failures:
        raise AssertionError(
            f"{len(failures)} of {len(commands)} commands the demo runbook instructs an "
            f"operator to run do not resolve:\n\n"
            + "\n".join(r.render() for r in failures)
            + f"\n\n--- the {len(passes) + len(infos)} that do ---\n"
            + "\n".join(r.render() for r in passes + infos)
            + unclassified_report
            + f"\n\n{tally}"
        )
