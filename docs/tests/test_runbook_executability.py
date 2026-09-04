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
* And a *line* the parser declines to read at all — an unknown fence language, an unprompted line
  inside a transcript, a comment — lands in the ``DROPPED`` bucket, which is likewise counted and
  printed. Every non-blank line inside every fence leaves the parse as exactly one of a graded
  command or a dropped line with a reason, and ``test_the_runbook_command_sweep_is_armed``
  asserts that partition line by line. This is not decoration: the version of this file merged at
  ``e57268e`` skipped an unreadable fence with a bare ``continue``, so relabelling one fence from
  ` ```bash ` to ` ```console ` deleted a whole step from the sweep (17 commands became 16) and
  pointing that step at a compose service that does not exist produced no failure at all.

**Two independent parses, cross-checked — because enumerating markdown does not work.** Every
rule that enumerated a *shape* a document may take became simultaneously a bypass and a false
positive. Bounding fence indentation at three spaces (CommonMark measures it relative to the
containing block) hid a fence at column 4 inside a list item *and* dropped legitimate nested
fences. Allow-listing fence languages sanctioned hiding the procedure under ```` ```text ````
*and* hard-failed on ```` ```plaintext ````. So fence indentation is unbounded; a line inside an
unknown language is graded when its head names a program this gate classifies and DROPPED with a
reason otherwise; and the arming pin is ``test_the_parse_covers_the_raw_text``: a second,
deliberately stupid scan of the raw bytes for every ``make`` target the **Makefile** defines,
every ``*.sh`` token, every ``dir/file.py`` token **and every ``docker compose`` invocation**,
each of which must have produced a graded command on that line. A fence this parser cannot read
is then a loud mismatch instead of a silent zero, and no fence rule has to be right for the gate
to hold.

The compose half of that scan is new, and its absence was the third of three measured fail-opens
in the merged rewrite: the raw backstop knew about ``make``, ``*.sh`` and ``dir/*.py`` and about
nothing else, so a re-fenced ``docker compose`` step was outside its vocabulary and the mismatch
never fired. The other two were a Makefile recipe naming a script with no directory component
(``@bash no_such_root.sh`` passed; ``@bash ./no_such_root.sh`` failed) and a command-position
guard that was an exact-case allow-list of thirteen words while the ``make`` match beside it was
``IGNORECASE`` (``Then make e2e-live``, ``run make e2e-live``, ``- make e2e-live`` all silent;
12 of 36 realistic positions were read). **All three were invisible to the exit code**, because a
new FAIL lands inside the strict-xfail gate at the bottom of this file, which is already expected
to fail — which is why the regression pins for them assert on the sweep's own output.

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

# `import pytest` used to live here for the `@pytest.mark.xfail` on the gate below. Removing
# that marker made this the file's only remaining use of the name, and ruff's F401 fails
# `make verify` on a dead import — so it goes with the marker. Nothing else here needs pytest:
# every check is a plain assertion, and the one pytest subprocess this file runs is spawned
# through `sys.executable -m pytest`.

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


def _rel(path: Path) -> str:
    """``path`` relative to the repo root, or its absolute form when it lies outside.

    The fallback is what lets the regression tests below point ``RUNBOOK_DIR`` at a ``tmp_path``
    holding a synthetic runbook. Grading a fixture document is the only honest way to pin a
    parser bypass: the alternative is editing the real runbook, which another lane owns.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


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


@dataclass
class Dropped:
    """A line inside a fenced block that the parser declined to grade, and WHY.

    The bucket exists because "neither graded nor mentioned" is the failure this gate keeps
    being defeated by. Three drafts of this file were beaten by edits that changed how the
    document parses without changing what it says, and the merged rewrite reopened it: relabelling
    one fence from ``bash`` to ``console`` made a whole step disappear from the sweep with no
    count moving that anyone would look at. A line the parser will not grade must still be
    COUNTED and PRINTED, so that "I did not grade these two lines, here they are" is a visible
    outcome and silence is not.
    """

    doc: str
    lineno: int
    text: str
    why: str
    #: ``"comment"`` | ``"transcript-output"`` | ``"unread-fence-language"``. A machine-readable
    #: reason, because the arming pin below has to treat the three differently: a comment is
    #: legitimately not a step, while the other two are lines the parser CHOSE not to read and
    #: must therefore be provably not commands.
    kind: str = "comment"
    span: frozenset[int] = field(default_factory=frozenset)

    @property
    def where(self) -> str:
        return f"{self.doc}:{self.lineno}"

    def render(self) -> str:
        return f"  [DROPPED:{self.kind}] {self.where}  `{self.text}`\n        {self.why}"


#: Program names this gate knows how to grade. A line naming one of these is a COMMAND wherever
#: it is written — inside a fence language this parser does not read, or unprompted inside a
#: transcript — and is promoted back out of the dropped bucket rather than believed to be output.
_CLASSIFIABLE_HEADS = {
    "make", "pytest", "python", "python3", "docker", "docker-compose",
    "bash", "sh", "zsh", "cp", "uv", "node",
}  # fmt: skip


def _normalized_head(text: str) -> tuple[str, list[str]] | None:
    """``(lower-cased basename of the program, remaining tokens)``, past the usual noise."""
    try:
        toks = _strip_redirects(shlex.split(text))
    except ValueError:
        return None
    while toks and _is_assignment(toks[0]):
        toks = toks[1:]
    while toks and toks[0] in _WRAPPERS:
        toks = toks[1:]
    if not toks:
        return None
    return Path(toks[0].strip("\"'")).name.lower(), toks[1:]


def _looks_like_a_command(text: str) -> bool:
    """Would this line be graded if it were written in a fence this parser reads?

    Deliberately head-driven rather than shape-driven. Grading every unprompted transcript line
    as a command was tried and is wrong — ``lint ok; don't rerun`` is program output and becomes
    an unparseable FAIL — but "this fence is a transcript" is not a licence to drop a line that
    plainly runs ``docker compose`` or ``make``. So: a recognised program, an explicit
    ``./script`` head, or any operand shaped like a repository path is a command; everything else
    is output, and lands in the counted, printed dropped bucket instead of nowhere.
    """
    head_rest = _normalized_head(text)
    if head_rest is None:
        return False
    head, rest = head_rest
    if head in _CLASSIFIABLE_HEADS:
        return True
    try:
        toks = _strip_redirects(shlex.split(text))
    except ValueError:
        return False
    if toks and (toks[0].startswith("./") or toks[0].startswith("../")):
        return True
    return any(_repo_shaped(t) for t in rest)


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
) -> tuple[list[tuple[int, frozenset[int], str]], list[tuple[int, frozenset[int], str, str, str]]]:
    """``(commands, declined)`` — and NOTHING is in neither.

    ``commands`` are ``(first_lineno, every_lineno_covered, command_text)``. ``declined`` is
    ``(first_lineno, every_lineno_covered, raw_text, why)`` for every logical line this function
    refused to turn into a command, each carrying the reason it was refused.

    Returning the refusals is the structural half of this gate, not bookkeeping. A line that is
    dropped here is dropped from the sweep, from the tally and from the report at once, and that
    is precisely how a re-fenced step vanished: ` ```bash ` relabelled to ` ```console ` turned
    ``docker compose --profile e2e up -d shopify-stub`` into "program output", 17 commands became
    16, and pointing the step at a service that does not exist produced no signal at all. Every
    caller now has to say what happened to each line.

    Joins backslash continuations and strips comments. ``#`` starts a comment and never a root
    prompt — treating ``# `` as a prompt turned ``# don't run this twice`` into an unparseable
    command. In a session/transcript fence ``prompt_only`` is set and lines without a ``$``/``%``
    prompt are program OUTPUT rather than commands — but see ``_looks_like_a_command``: a line
    that names a program this gate classifies is promoted back out of the output bucket, because
    "it is inside a transcript fence" is a claim about the fence, not about the line.
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
    declined: list[tuple[int, frozenset[int], str, str, str]] = []
    for lineno, lines, line in joined:
        span = frozenset(lines)
        text = line.strip()
        if not text:
            continue  # a blank line names nothing and is not a hiding place
        prompted = text[:2] in ("$ ", "% ")
        if prompted:
            text = text[2:]
        elif prompt_only and not _looks_like_a_command(text):
            declined.append(
                (
                    lineno,
                    span,
                    text,
                    "session/transcript fence and this line carries no `$`/`%` prompt, so it "
                    "reads as program OUTPUT; it also names no program this gate classifies",
                    "transcript-output",
                )
            )
            continue
        if text.startswith("#"):
            declined.append((lineno, span, text, "shell comment, not a step", "comment"))
            continue
        text = _strip_comment(text)
        if text:
            out.append((lineno, span, text))
        else:
            declined.append((lineno, span, line.strip(), "the whole line is a comment", "comment"))
    return out, declined


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


def extract_commands(path: Path) -> tuple[list[Command], list[Block], list[Dropped]]:
    """Every command a runbook instructs an operator to run, derived from the document.

    Three sources: fenced code blocks (the procedure), inline spans that are exactly
    ``make <target>`` (steps referenced only in prose, e.g. Troubleshooting), and a document-wide
    sweep for ``*.sh`` paths — ``docs/demo/e2e_live.sh`` is named *only* in prose. Nothing is
    de-duplicated: the raw-text cross-check asserts coverage per LINE, so a second mention on a
    second line has to produce its own graded command.

    The third return value is the **dropped bucket**, and it is the structural repair of a
    measured regression. A fence whose language this parser does not read used to be skipped with
    a bare ``continue``: the lines inside it produced no command, no INFO, no UNCLASSIFIED entry
    and no mention anywhere in the report. Relabelling ``docs/demo/starting-slice.md:63`` from
    ` ```bash ` to ` ```console ` therefore deleted the ``docker compose … shopify-stub`` step
    from the sweep — 17 commands became 16 — and pointing that step at a service the compose
    config does not define still produced NO failure. Now every non-blank line inside every fence
    leaves this function as exactly one of a graded ``Command`` or a ``Dropped`` carrying its
    reason, and ``test_the_runbook_command_sweep_is_armed`` checks that partition line by line.
    """
    doc = _rel(path)
    text = path.read_text(encoding="utf-8")
    blocks = _fenced_blocks(text, doc)
    commands: list[Command] = []
    dropped: list[Dropped] = []

    for block in blocks:
        if block.lang not in SHELL_LANGS:
            # NOT a bare `continue` any more. An unknown fence language is a statement about the
            # fence, not about the lines inside it, so a line that plainly runs a program this
            # gate classifies is graded anyway and the rest is recorded with its reason.
            for lineno, raw in block.lines:
                stripped = raw.strip()
                if not stripped:
                    continue
                if stripped[:2] in ("$ ", "% "):
                    stripped = stripped[2:].strip()  # a prompt is furniture, not the command
                if stripped.startswith("#"):
                    dropped.append(
                        Dropped(
                            doc=doc,
                            lineno=lineno,
                            text=stripped,
                            why="comment, not a step",
                            kind="comment",
                            span=frozenset({lineno}),
                        )
                    )
                    continue
                if _looks_like_a_command(stripped):
                    for piece in _split_chain(stripped):
                        for sub in _expand_inline_script(piece):
                            commands.append(
                                Command(
                                    text=sub,
                                    doc=doc,
                                    lineno=lineno,
                                    kind="shell",
                                    span=frozenset({lineno}),
                                )
                            )
                else:
                    dropped.append(
                        Dropped(
                            doc=doc,
                            lineno=lineno,
                            text=stripped,
                            why=(
                                f"fence language `{block.lang or '<none>'}` is not one this "
                                f"parser reads as a procedure, and the line names no program "
                                f"this gate classifies"
                            ),
                            kind="unread-fence-language",
                            span=frozenset({lineno}),
                        )
                    )
            continue
        graded, declined = _logical_lines(block.lines, block.lang in PROMPT_LANGS)
        for lineno, span, line in graded:
            for piece in _split_chain(line):
                for sub in _expand_inline_script(piece):
                    commands.append(
                        Command(text=sub, doc=doc, lineno=lineno, kind="shell", span=span)
                    )
        for lineno, span, line, why, kind in declined:
            dropped.append(
                Dropped(doc=doc, lineno=lineno, text=line, why=why, kind=kind, span=span)
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

    return commands, blocks, dropped


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

#: A script named WITHOUT a directory component — ``@bash no_such_root.sh``. Both alternations
#: of ``_RECIPE_PATH`` require a ``/``, so a bare name was invisible to the path scan, and the
#: only other net (``shutil.which(head)``) inspects the HEAD, which in that recipe is ``bash``
#: and is of course on PATH. Measured against the merged rewrite: ``@bash no_such_root.sh`` and
#: ``@python no_such_root.py`` both left the gate at its baseline 13 PASS / 4 FAIL, while
#: ``@bash ./no_such_root.sh`` and ``@bash docs/demo/no_such_root.sh`` were caught. A missing
#: directory component was the whole difference between red and silent.
_RECIPE_BARE_SCRIPT = re.compile(r"(?<![\w/.\-$*])([\w.-]+\.(?:sh|py))(?![\w/])")
_RECIPE_MODULE = re.compile(r"-m\s+([A-Za-z_][\w.]*)")
_UNEXPANDED = re.compile(r"\$[({](?!MAKE[)}])(?P<name>[A-Za-z_][\w.]*)[)}]")

#: Programs whose FIRST positional operand is a script this repository has to contain. Narrower
#: than ``_INTERPRETER_NAMES`` on purpose: ``uv``, ``pytest`` and ``node`` take subcommands and
#: package names in that position, so demanding a file there produces false FAILs. This closes
#: the extensionless spelling of the same hole — ``@bash no_such_root`` names no ``.sh`` for
#: ``_RECIPE_BARE_SCRIPT`` to find and is otherwise as invisible as the ``.sh`` form was.
_SCRIPT_TAKING_INTERPRETERS = {"bash", "sh", "zsh", "dash", "python", "python3"}

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

    # The same scan for a script named with NO directory component. `_RECIPE_PATH` above cannot
    # see one — both of its alternations require a `/` — and `@bash no_such_root.sh` therefore
    # passed this gate while `@bash ./no_such_root.sh` failed it, which is a rule about
    # punctuation rather than about whether the target runs.
    # EXISTENCE only, deliberately. A bare name in HEAD position is already graded by the
    # `shutil.which(head)` fallback further down (measured: `@no_such_root.sh` is caught today),
    # so demanding `+x` here would only add a way to be wrong about `@uv run seed.py`.
    for token in dict.fromkeys(_RECIPE_BARE_SCRIPT.findall(rule.recipe)):
        if not (REPO_ROOT / token).is_file():
            problems.append(
                f"recipe for `{target}` invokes {token}, which does not exist in the repo "
                f"(named without a directory component, which is not a reason to skip it)"
            )

    # And the extensionless spelling: an interpreter's first operand IS a script path, whatever
    # it is called. `_RECIPE_BARE_SCRIPT` keys on `.sh`/`.py`, so `@bash no_such_root` would
    # otherwise walk straight through both scans and the `shutil.which("bash")` fallback.
    for toks in _recipe_chunks(rule.recipe):
        head = _recipe_head(toks)
        if Path(head.strip("\"'")).name.lower() not in _SCRIPT_TAKING_INTERPRETERS:
            continue
        rest = toks[toks.index(head) + 1 :] if head in toks else toks[1:]
        if "-c" in rest or "-m" in rest:
            continue  # an inline script or a module, neither of which is a path
        operands = [t for t in _positional_args(rest, set()) if "$" not in t]
        if not operands:
            continue
        first = operands[0].strip("\"'")
        if first.endswith((".sh", ".py")) or "/" in first:
            continue  # already graded by one of the two path scans above
        if not (REPO_ROOT / first).is_file():
            problems.append(
                f"recipe for `{target}` runs `{head} {first}` and {first} is not a file in this "
                f"repository, so what that interpreter would execute does not exist"
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
        if (head == "docker" and rest[:1] == ["compose"]) or head == "docker-compose":
            if services is None:
                services = _compose_services()[0]
            after = rest[1:] if head == "docker" else rest
            named = _positional_args(after, {"--profile", "-f", "--file", "-p", "--project-name"})
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

    # `docker-compose up -d shopify-stub` is the SAME command as `docker compose up -d
    # shopify-stub`, and grading only the spaced spelling made a hyphen a bypass. Normalised to
    # the two-token form so one checker serves both.
    if base == "docker-compose" and "$" not in head:
        return _check_docker_compose(cmd, ["docker", "compose", *toks[1:]])

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


#: One graded document: its path, its fences, the commands taken from it, and the lines the
#: parser declined with the reason for each.
PerDoc = tuple[Path, list["Block"], list["Command"], list["Dropped"]]


def audit() -> tuple[list[Command], list[Result], list[PerDoc], list[Dropped]]:
    """Grade every runbook under ``docs/demo/``.

    Returns the dropped bucket alongside the results, because a caller that cannot see what was
    NOT graded cannot tell a clean sweep from an empty one.
    """
    commands: list[Command] = []
    dropped: list[Dropped] = []
    per_doc: list[PerDoc] = []
    for path in runbooks():
        found, blocks, skipped = extract_commands(path)
        per_doc.append((path, blocks, found, skipped))
        commands.extend(found)
        dropped.extend(skipped)
    return commands, [classify_and_check(c) for c in commands], per_doc, dropped


# =====================================================================================
# The arming pins — deliberately NOT xfail
# =====================================================================================


#: Words that can stand immediately before a command — in a shell line, or in the imperative
#: English a runbook is written in. Compared LOWER-CASED, which is the whole repair of one of
#: the three fail-opens: the ``make`` match is ``IGNORECASE`` while this membership test was
#: exact-case, so ``Then make e2e-live``, ``Time …``, ``Do …``, ``Else …``, ``Sudo …`` and
#: ``Env …`` all sailed past it. Measured against the merged rewrite, 45 prefixed ``make`` lines
#: hidden in a fence the parser skips: 19 caught, 26 silent.
#:
#: ``run`` is here for a different reason and the distinction was measured rather than assumed —
#: it is missed in EVERY casing, because it was never in the vocabulary at all. So this is an
#: allow-list that has to be wide, not merely case-folded. The English false positive it must
#: still exclude is the one the earlier draft was bitten by: "will make check pass on the first
#: try" — ``will`` is not a command lead, and no modal or auxiliary is.
_COMMAND_LEAD_WORDS = _WRAPPERS | {
    "time", "then", "else", "do", "done", "fi", "and", "or", "&&", "||",
    "run", "rerun", "re-run", "rerunning", "running", "try", "just", "again",
    "if", "elif", "while", "until", "for", "xargs", "watch", "also", "first", "next",
    "finally", "now", "please", "type", "invoke", "call",
}  # fmt: skip

#: Markdown furniture that is not part of the command: bullets, ordered-list numbers, block
#: quotes. ``- make e2e-live`` inside a fence this parser skips was silent, which makes an
#: ordinary bulleted procedure a bypass.
_MARKDOWN_LEAD = re.compile(r"^(?:\s*(?:[-*+>]|\d+[.)])\s*)+")


def _raw_make_mentions(line: str, targets: set[str]) -> list[str]:
    """``make <target>`` mentions on ``line`` that a reader would read as a command.

    Only a mention where a COMMAND could stand, or inside an inline ``code`` span, counts.
    Without that, the English sentence "will make check pass on the first try" reads as a
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
        # must be empty, markdown list furniture, a shell separator, a prompt, a backtick, or a
        # word from `_COMMAND_LEAD_WORDS` — matched case-insensitively.
        prefix = _MARKDOWN_LEAD.sub("", line[: m.start()].rstrip()).strip()
        words = prefix.split()
        last = words[-1].strip("`'\"").lower() if words else ""
        command_position = (
            not prefix
            or prefix.endswith(("`", "&", "|", ";", "(", "{", "$", "%", ">"))
            or last in _COMMAND_LEAD_WORDS
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


#: A ``docker compose`` invocation, seen by the dumb raw scan. It resolves a name the REPOSITORY
#: controls — the merged compose config's service list — so it belongs in the cross-check
#: alongside `make` targets and `*.sh` paths, and its absence there was the third fail-open.
#: Measured: re-fencing `docs/demo/starting-slice.md:63` from ` ```bash ` to ` ```console ` made
#: the `docker compose --profile e2e up -d shopify-stub` step vanish (17 commands became 16),
#: and pointing the same step at `no-such-service` under that fence produced rc=0 and no FAIL,
#: where the identical edit under ` ```bash ` reports
#: `[FAIL] … the merged compose config defines no service named no-such-service`.
#: Both spellings, because they are one command. Measured: the hyphenated legacy spelling in a
#: fence the parser does not read produced `swept 16 ... 1 DROPPED` — accounted for, but the
#: sweep count still fell by one with nothing failing, which is the shape of the defect.
#: The trailing `(?![\w.-])` keeps a prose mention of the FILE `docker-compose.yml` out of
#: the scan — that names a config, not a step, and flagging it would be a false positive on
#: the document this net exists to protect.
_RAW_COMPOSE = re.compile(r"(?<![\w-])docker[-\s]+compose(?![\w.-])", re.IGNORECASE)


def _named_by_line(
    commands: list[Command],
) -> tuple[dict[int, set[str]], dict[int, set[str]], set[int]]:
    """``({line: make targets graded there}, {line: repo paths graded there}, {compose lines})``.

    The cross-check compares per MENTION, not per line, and this is what makes that possible.
    Per-line coverage was exploitable: ``make check   # then: make e2e-live`` has the comment
    stripped by the real parser, so ``e2e-live`` went ungraded while the decoy ``make check`` on
    the same line kept the line "covered" and the raw scan silent.

    The third element is per LINE rather than per service name, and deliberately so: the sabotage
    that has to be caught names a service the compose config does NOT define, so a set of known
    service names would not contain it and would go quiet on exactly the input that matters.
    What the cross-check can honestly demand is that a line saying ``docker compose`` produced a
    compose command the parser graded.
    """
    makes: dict[int, set[str]] = {}
    paths: dict[int, set[str]] = {}
    compose: set[int] = set()
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
        head_name = Path(toks[0]).name.lower()
        if (head_name == "docker" and toks[1:2] == ["compose"]) or head_name == "docker-compose":
            compose |= set(lines)
        for tok in toks[1:]:
            if _repo_shaped(tok):
                for ln in lines:
                    paths.setdefault(ln, set()).add(tok.split("::")[0])
    return makes, paths, compose


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
    graded_makes, graded_paths, graded_compose = _named_by_line(commands)
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
        if _RAW_COMPOSE.search(raw) and idx not in graded_compose:
            missing.append("`docker compose`")
        if missing:
            gaps.append(
                f"  {_rel(path)}:{idx} mentions {', '.join(sorted(set(missing)))}"
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
    assert docs, f"no runbook markdown found under {_rel(RUNBOOK_DIR)}/"

    _, _, per_doc, _ = audit()
    gaps: list[str] = []
    for path, _blocks, found, _dropped in per_doc:
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
    assert docs, f"no runbook markdown found under {_rel(RUNBOOK_DIR)}/"

    commands, results, per_doc, dropped = audit()

    for path, blocks, found, skipped in per_doc:
        doc = _rel(path)
        for block in blocks:
            if block.lang in SHELL_LANGS and block.lang not in PROMPT_LANGS:
                end = block.lines[-1][0] if block.lines else block.open_lineno
                got = [
                    c for c in found if c.kind == "shell" and block.open_lineno <= c.lineno <= end
                ]
                assert got, (
                    f"{doc}:{block.open_lineno} is a shell code fence that produced ZERO "
                    f"commands. An empty sweep over a real block PASSES, which is exactly the "
                    f"quiet-sweep failure this guard exists to prevent."
                )

            # THE PARTITION, and it holds for EVERY fence whatever its language. Each non-blank
            # line inside a block is either covered by a graded command's span or present in the
            # dropped bucket with a reason. "Neither" is the state this gate keeps being defeated
            # from: relabelling one fence `bash` -> `console` moved a whole step into "neither",
            # the tally went 17 -> 16, and a step pointed at a nonexistent compose service still
            # produced rc=0. A count that only ever goes down quietly is not a count.
            covered: set[int] = set()
            for cmd in found:
                covered |= set(cmd.span or frozenset({cmd.lineno}))
            for drop in skipped:
                covered |= set(drop.span or frozenset({drop.lineno}))
            orphans = [
                (ln, raw.strip()) for ln, raw in block.lines if raw.strip() and ln not in covered
            ]
            assert not orphans, (
                f"{doc}:{block.open_lineno} (```{block.lang or ''}) has {len(orphans)} line(s) "
                f"that produced NEITHER a graded command NOR an entry in the dropped bucket, so "
                f"they left the sweep without being counted anywhere:\n"
                + "\n".join(f"    {doc}:{ln}  {text}" for ln, text in orphans)
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
            f"the sweep found no {label} under {_rel(RUNBOOK_DIR)}/; the parser "
            f"and the documents have drifted apart. Categories found: {sorted(categories)}"
        )

    # Every dropped line must carry a reason, and a line the parser DECLINED to read must not be
    # one it would have classified — otherwise `_looks_like_a_command` and the two places that
    # consult it have drifted apart, and the promotion that pulls a command back out of a
    # transcript or an unread fence is silently inert.
    unreasoned = [d for d in dropped if not (d.why and d.text)]
    assert not unreasoned, (
        "these lines were dropped with no reason recorded, which is the same as not being "
        "counted:\n" + "\n".join(d.render() for d in unreasoned)
    )
    misdropped = [
        d
        for d in dropped
        if d.kind in ("transcript-output", "unread-fence-language")
        and _looks_like_a_command(d.text)
    ]
    assert not misdropped, (
        "these lines name a program this gate classifies but were dropped as output or as an "
        "unreadable fence language — a command hiding behind a fence label is the exact bypass "
        "the promotion rule exists to close:\n" + "\n".join(d.render() for d in misdropped)
    )


# =====================================================================================
# Regression pins for three fail-opens this file shipped with
#
# Each of the three was measured on two disjoint copies of the repo, and each was INVISIBLE TO
# THE EXIT CODE: a new FAIL lands inside the strict-xfail gate below, which is already expected
# to fail, so the sweep could lose a whole step and the run still reported `1 failed` either way.
# The tests here therefore assert on the sweep's own output rather than on the run's exit status,
# and they grade FIXTURE documents under `tmp_path` — the real runbook belongs to another lane,
# and a regression pin that needs the production document edited is a pin nobody can run.
# =====================================================================================


def _fixture_runbook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """Point the sweep at a synthetic runbook and return its path."""
    doc = tmp_path / "fixture-runbook.md"
    doc.write_text(body, encoding="utf-8")
    monkeypatch.setitem(globals(), "RUNBOOK_DIR", tmp_path)
    return doc


@pytest.mark.parametrize("spelling", ["docker compose", "docker-compose"])
@pytest.mark.parametrize("lang", ["console", "shell-session", "text", "plaintext", "output"])
def test_a_step_does_not_vanish_when_its_fence_is_relabelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lang: str, spelling: str
) -> None:
    """FAIL-OPEN 1. Re-fencing a step used to delete it from the sweep, silently.

    Measured against the merged rewrite, on `docs/demo/starting-slice.md:63`: changing ` ```bash `
    to ` ```console ` took the sweep from 17 commands to 16 and the `docker compose … shopify-stub`
    step simply stopped existing. Pointed at a service the merged compose config does not define,
    that same re-fenced step produced `rc=0` and NO failure line, where the identical edit under
    ` ```bash ` reports `[FAIL] … the merged compose config defines no service named …`. A
    ```` ```text ```` fence behaved the same way, and neither left an UNCLASSIFIED entry, a
    dropped line, or any count a reader would notice.

    Two independent repairs are pinned here. The command is PROMOTED out of the unread fence
    because its head is a program this gate classifies; and, separately,
    `test_the_compose_cross_check_fires_when_a_compose_step_is_ungraded` pins the raw-text
    backstop that catches it even if the promotion list is ever wrong.

    Both spellings, because a hyphen was a bypass of its own: measured, `docker-compose …` inside
    a ```text fence produced `swept 16 … 1 DROPPED` — accounted for, but the sweep count still
    fell by one with no test failing, which is the shape of the defect wearing a different name.
    """
    doc = _fixture_runbook(
        tmp_path,
        monkeypatch,
        "# Fixture\n\nBring the stub up:\n\n"
        f"```{lang}\n"
        f"{spelling} --profile e2e up -d no-such-service\n"
        "```\n",
    )
    commands, results, per_doc, dropped = audit()

    compose = [r for r in results if r.category == "docker compose service"]
    assert compose, (
        f"a ```{lang} fence naming `docker compose` produced no graded compose command at all. "
        f"swept={[c.text for c in commands]} dropped={[d.text for d in dropped]}"
    )
    assert [r.verdict for r in compose] == [VERDICT_FAIL], (
        f"the step names `no-such-service`, which the merged compose config does not define, and "
        f"must be graded FAIL under a ```{lang} fence exactly as under ```bash: "
        f"{[r.render() for r in compose]}"
    )
    assert "defines no service named no-such-service" in compose[0].reason, compose[0].render()

    # And it must not ALSO be sitting in the dropped bucket, which would mean the two halves of
    # the partition disagree about the same line.
    assert not [d for d in dropped if "docker compose" in d.text], (
        f"the compose step was graded AND dropped: {[d.render() for d in dropped]}"
    )
    # The raw cross-check must be quiet, because the line was graded.
    assert not _coverage_gaps(doc, per_doc[0][2]), _coverage_gaps(doc, per_doc[0][2])


def test_the_compose_cross_check_fires_when_a_compose_step_is_ungraded(tmp_path: Path) -> None:
    """FAIL-OPEN 1, second net. The dumb raw scan now knows about `docker compose`.

    The rewrite's stated defence against a re-fenced step is `test_the_parse_covers_the_raw_text`
    — "a fence this parser cannot read is a loud mismatch instead of a silent zero". It scanned
    for `make` targets, `*.sh` tokens and `dir/file.py` tokens, and for nothing else, so a
    `docker compose` step was outside its vocabulary and the mismatch never fired. That gap is
    what made re-fencing work at all.

    Graded with an EMPTY command list, which is the state the parser is in when a fence defeats
    it. A service name is deliberately not required to be one the config defines: the sabotage
    that matters names a service that does NOT exist, so a check keyed on known service names
    would go quiet on precisely the input it is for.
    """
    doc = tmp_path / "fixture-runbook.md"
    doc.write_text(
        "```console\ndocker compose --profile e2e up -d no-such-service\n```\n", encoding="utf-8"
    )
    gaps = _coverage_gaps(doc, [])
    assert len(gaps) == 1 and "docker compose" in gaps[0], (
        f"a raw line running `docker compose` with no graded command on it must be reported as a "
        f"coverage gap; got {gaps!r}"
    )
    # And it must stay quiet when the line IS graded, or the net is just noise.
    doc2 = tmp_path / "graded.md"
    doc2.write_text(
        "```bash\ndocker compose --profile e2e up -d no-such-service\n```\n", encoding="utf-8"
    )
    graded, _blocks, _dropped = extract_commands(doc2)
    assert not _coverage_gaps(doc2, graded), _coverage_gaps(doc2, graded)


def test_a_recipe_script_named_without_a_directory_is_still_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAIL-OPEN 2. A missing DIRECTORY COMPONENT used to be the difference between red and mute.

    Measured against the merged rewrite, adding one chunk to the recipe of a target the runbook
    names: `@bash ./no_such_root.sh` and `@bash docs/demo/no_such_root.sh` were caught (13 PASS /
    4 FAIL became 12 PASS / 5 FAIL), while `@bash no_such_root.sh`, `@python no_such_root.py` and
    `@uv run no_such_root.py` left the tally EXACTLY at baseline. Both alternations of
    `_RECIPE_PATH` require a `/`, and the only other net inspects the recipe's HEAD — which in
    those three spellings is `bash`, `python` or `uv`, all of them on PATH.

    The control cases matter as much as the sabotages: a bare script that EXISTS must still pass,
    or this pin would be satisfied by a gate that fails on everything.
    """
    (tmp_path / "ok_root.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    (tmp_path / "ok_root.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)
    monkeypatch.setitem(globals(), "MAKEFILE", tmp_path / "Makefile")

    caught = {
        "bare .sh behind an interpreter": "@bash no_such_root.sh",
        "bare .py behind an interpreter": "@python no_such_root.py",
        "bare .py behind python3": "@python3 no_such_root.py",
        "bare .py behind uv run": "@uv run no_such_root.py",
        "extensionless behind an interpreter": "@bash no_such_root",
        "dot-slash (already caught before)": "@bash ./no_such_root.sh",
        "with a directory (already caught before)": "@bash docs/demo/no_such_root.sh",
        "head position (already caught before)": "@no_such_root.sh",
    }
    passes = {
        "an existing bare .sh": "@bash ok_root.sh",
        "an existing bare .py": "@python ok_root.py",
        "an existing bare .py behind uv run": "@uv run ok_root.py",
        "a shell builtin": "@echo nothing to resolve here",
    }
    for label, recipe in {**caught, **passes}.items():
        (tmp_path / "Makefile").write_text(f"probe: ; {recipe}\n", encoding="utf-8")
        ok, problems = check_make_target("probe")
        if label in caught:
            assert not ok, (
                f"{label}: `probe: ; {recipe}` names a script this repository does not contain, "
                f"and the gate reported it runnable. A name without a `/` in it is not a reason "
                f"to stop looking — that spelling alone took a broken recipe past this gate."
            )
            assert any("no_such_root" in p for p in problems), (
                f"{label}: the refusal must NAME the missing script, or the gate reports that "
                f"something is wrong without saying what: {problems}"
            )
        else:
            assert ok, f"{label}: `probe: ; {recipe}` is runnable and must pass: {problems}"


def test_the_raw_make_cross_check_reads_a_command_wherever_a_command_can_stand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAIL-OPEN 3. The raw scan's command-position guard was an exact-case allow-list of ~13 words.

    The `make` match is `IGNORECASE` — deliberately, because this filesystem resolves `Make` —
    while the membership test that decides whether `make` sits where a COMMAND sits was not. So
    `Then make e2e-live`, `Time …`, `Do …`, `Else …`, `Sudo …` and `Env …` were all read as
    English. Measured on 45 prefixed `make e2e-live` lines hidden inside a fence the parser skips:
    19 caught, 26 silent, with the sweep's tally byte-identical to baseline in every case.

    Two corrections to the original report, both measured: `run make X` is missed in EVERY casing
    because "run" was never in the vocabulary at all, not because of case; and the largest holes
    were not casing but ordinary markdown furniture — `- make e2e-live`, `* make e2e-live`,
    `1. make e2e-live` — plus the shell control words `if`, `while`, `until`, `xargs`, `watch`.

    The false positive this guard must still refuse is the one an earlier draft was bitten by:
    the English sentence "will make check pass on the first try" is not an instruction to run
    `make check`.
    """
    targets = {"check", "e2e-live", "bootstrap"}

    must_be_read_as_commands = [
        "make e2e-live",
        "then make e2e-live",
        "Then make e2e-live",
        "THEN make e2e-live",
        "time make e2e-live",
        "Time make e2e-live",
        "do make e2e-live",
        "Do make e2e-live",
        "else make e2e-live",
        "Else make e2e-live",
        "sudo make e2e-live",
        "Sudo make e2e-live",
        "env make e2e-live",
        "Env make e2e-live",
        "run make e2e-live",
        "Run make e2e-live",
        "rerun make e2e-live",
        "if make e2e-live",
        "while make e2e-live",
        "until make e2e-live",
        "xargs make e2e-live",
        "watch make e2e-live",
        "just make e2e-live",
        "and make e2e-live",
        "now make e2e-live",
        "- make e2e-live",
        "* make e2e-live",
        "+ make e2e-live",
        "1. make e2e-live",
        "12) make e2e-live",
        "> make e2e-live",
        "  - make e2e-live",
        "$ make e2e-live",
        'cd "$REPO" && make e2e-live',
        "make -f Makefile e2e-live",
        "Make e2e-live",
    ]
    for line in must_be_read_as_commands:
        assert _raw_make_mentions(line, targets) == ["make e2e-live"], (
            f"{line!r} puts `make e2e-live` exactly where a command stands, and the raw-text "
            f"cross-check must see it. This scan is the only net left when a fence language "
            f"defeats the real parser, and every line it cannot read is a step that can be "
            f"hidden: got {_raw_make_mentions(line, targets)!r}"
        )

    must_not_be_read_as_commands = [
        "will make check pass on the first try",
        "that would make check unnecessary",
        "to make check faster, cache the venv",
        "we can make check run in parallel",
    ]
    for line in must_not_be_read_as_commands:
        assert _raw_make_mentions(line, targets) == [], (
            f"{line!r} is English prose using the verb 'make', and reading it as a command turns "
            f"the cross-check into a false positive on the document it is meant to protect: got "
            f"{_raw_make_mentions(line, targets)!r}"
        )

    # End to end, through the sweep: a `make` step hidden in a fence the parser does not read is
    # either graded or reported, never both silent.
    doc = _fixture_runbook(
        tmp_path,
        monkeypatch,
        "# Fixture\n\nFollow the steps:\n\n```text\n- Then make e2e-live\n```\n",
    )
    _, _, per_doc, _dropped = audit()
    graded = [c.text for c in per_doc[0][2]]
    gaps = _coverage_gaps(doc, per_doc[0][2])
    assert "make e2e-live" in graded or gaps, (
        "a `make e2e-live` step written as `- Then make e2e-live` inside a ```text fence was "
        "neither graded by the parser nor reported by the raw cross-check, which is the state "
        "this whole file exists to make impossible"
    )


def test_every_line_inside_an_unread_fence_lands_in_the_counted_dropped_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line the parser declines must be counted and printed, never merely absent.

    The old code skipped a fence whose language it did not read with a bare `continue`. That is
    the shape of every quiet-sweep failure in this repo: the work simply does not happen, and
    nothing in the report changes except a number nobody is diffing. Program OUTPUT is a real
    category and must stay droppable — grading `lint ok; don't rerun` as a command produced an
    unparseable FAIL — so the fix is not "grade everything", it is "say what you did with it".
    """
    _fixture_runbook(
        tmp_path,
        monkeypatch,
        "# Fixture\n\n```text\nlint ok; don't rerun\nall green\n```\n\n"
        "```console\n$ make check\nlint ok; don't rerun\n```\n",
    )
    commands, results, _per_doc, dropped = audit()

    assert [c.text for c in commands] == ["make check"], (
        f"the prompted `make check` is the only command here: {[c.text for c in commands]}"
    )
    assert len(dropped) == 3, (
        f"three non-blank lines were declined and all three must be recorded with a reason; "
        f"got {[d.render() for d in dropped]}"
    )
    assert {d.kind for d in dropped} == {"unread-fence-language", "transcript-output"}, (
        f"each dropped line must say WHY it was dropped: {[(d.kind, d.text) for d in dropped]}"
    )
    for drop in dropped:
        rendered = drop.render()
        assert drop.why and drop.text and str(drop.lineno) in rendered and drop.text in rendered, (
            f"a dropped line must render with its location, its text and its reason, so the "
            f"gate's report can print it: {rendered!r}"
        )
    assert all(r.verdict != VERDICT_UNCLASSIFIED for r in results), (
        f"nothing here should reach UNCLASSIFIED: {[r.render() for r in results]}"
    )


# =====================================================================================
# The executability gate
# =====================================================================================


# This test carried `@pytest.mark.xfail(strict=True)` when it landed, because the runbook's
# two headline commands did not resolve: `uv run python -m pytest e2e/test_s1_flow.py -q`
# named a file that was not in the repo (T-082, which would have added it, was rejected and
# its branch left unmerged), and `make e2e-live` shelled out to `docs/demo/e2e_live.sh`, which
# had never been written, so the target died with `No such file or directory` — bash 127, make
# exit 2. The sweep read 17 commands, 13 PASS / 4 FAIL / 0 UNCLASSIFIED.
#
# ESC-018 step (b) closed both. `docs/demo/e2e_live.sh` now exists as the live-run preflight
# (it always exits non-zero; the runbook says so), and `e2e/test_s1_flow.py` exists and
# collects. The sweep now reads 17 commands, 17 PASS / 0 FAIL / 0 UNCLASSIFIED, so the marker
# is gone: leaving it would turn this file red on XPASS, which is the same defect in the other
# direction. Put it back only alongside a measurement showing a command that does not resolve.
def test_c19_every_command_the_demo_runbook_names_resolves() -> None:
    """Every command in every ``docs/demo/`` runbook must resolve to something real.

    Asserts the behaviour that SHOULD hold — an operator can type each command in the runbook and
    have it reach real code — never the behaviour that does. A test pinning today's output would
    certify the defect.

    The message is the point as much as the red is: it names the document, the line, the command
    and what is missing, so the gate reports *which step is broken*. It also prints the
    UNCLASSIFIED and DROPPED buckets unconditionally, including on the failing path — a command
    this parser could not grade, and a line it declined to read at all, are both holes in the
    sweep, and the one thing they must never be is invisible.
    """
    commands, results, _, dropped = audit()

    assert commands, "extracted 0 commands — refusing to report PASS on an empty sweep"

    failures = [r for r in results if r.verdict == VERDICT_FAIL]
    passes = [r for r in results if r.verdict == VERDICT_PASS]
    infos = [r for r in results if r.verdict == VERDICT_INFO]
    unclassified = [r for r in results if r.verdict == VERDICT_UNCLASSIFIED]

    tally = (
        f"(swept {len(commands)} commands out of "
        f"{[_rel(p) for p in runbooks()]}; {len(passes)} PASS, "
        f"{len(failures)} FAIL, {len(infos)} INFO, {len(unclassified)} UNCLASSIFIED, "
        f"{len(dropped)} DROPPED)"
    )
    unclassified_report = (
        "\n\n--- UNCLASSIFIED: named nothing this gate could resolve ---\n"
        + "\n".join(r.render() for r in unclassified)
        if unclassified
        else ""
    )
    # The DROPPED bucket is printed for the same reason UNCLASSIFIED is, and it is the newer of
    # the two lessons: a fence relabelled `bash` -> `console` used to remove a step from the
    # sweep leaving nothing but a count one lower than yesterday's, which nobody was comparing.
    dropped_report = (
        "\n\n--- DROPPED: lines inside a fence that the parser declined to grade ---\n"
        + "\n".join(d.render() for d in dropped)
        if dropped
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
            + dropped_report
            + f"\n\n{tally}"
        )
