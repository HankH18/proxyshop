"""A lint that refuses a `Claim` built anywhere but the minting site (R8, acceptance 3).

`enforce_hook_provenance` is the runtime half of R8: a claim that no hook emitted is thrown out
at the bid boundary. This module is the *static* half, and the two catch different mistakes.

The runtime guard sees only what reaches the boundary in one code path on one run. A hosted
module that constructs `Claim(key=..., provenance=Provenance(source="owner_statement", ...))`
on a branch nothing exercised is invisible to it until the day that branch runs and a bid is
silently refused — or, worse, until the claim is threaded past the guard entirely. The static
check reads the whole hosted tree instead of one execution, and it fires at `pytest`, before any
of it ships.

**The rule.** On the hosted path — every module under `packages/store-agent/src/` except the
minting site itself and `src/external/` — the classes `Claim` and `Provenance` may be imported
and annotated with, but never *called*. Building one means calling `mint_claim` /
`mint_provenance` in :mod:`.provenance`, which stamps the hook's own source class and the
published authority rank and cannot be talked into stamping another hook's.

It is a rule about the *object*, not about the token. A checker that matched the callee's
spelling would be evaded by ``getattr(protocol, 'Claim')(**row)``, by ``MODELS['Claim'](**row)``,
by ``Fact = contracts.Claim`` and by ``p.Claim.model_validate(row)`` — four ways an ordinary
author reaches the identical class, none of them written to be sneaky, all of them landing a
forged claim on the hosted path with the lint silent. So the callee is *resolved* rather than
matched: see :func:`_resolves_to_guarded`.

**Why `src/external/` is exempt, and why that is not a hole.** The external door (T-044) receives
Tier-2 submissions from agents this platform does not run. Their claims are `seller_asserted` —
the one source in `contracts.NON_HOOK_PROVENANCE_SOURCES`, and the one the hosted path rejects
outright. Reconstructing an inbound claim there is not smuggling; it is parsing untrusted input
that is *labelled* untrusted and disciplined downstream by extraction and verification instead of
by this harness. That is DESIGN's dual-path decision, not an exception carved out for
convenience. The exemption is scoped by directory and stated in :data:`EXEMPT_DIRECTORIES` so it
cannot be widened by a comment in the file being checked.

The checker is a plain function over a directory so it can be run against a fixture tree, which
is how its own test proves it refuses something real rather than passing vacuously.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

#: Protocol objects that only the minting site may CALL. Importing them for a type annotation is
#: fine — annotations do not put a fact into a bid.
GUARDED_CONSTRUCTORS: frozenset[str] = frozenset({"Claim", "Provenance"})

#: Class methods that build a guarded object as surely as calling the class does.
#: ``Claim(key=..., provenance=...)`` and ``Claim.model_validate({"key": ..., ...})`` differ only
#: in where the field names are written; a rule that caught the first and not the second would be
#: a rule about punctuation. Only flagged when the receiver is itself a guarded name, so an
#: ordinary ``Envelope.model_validate(row)`` — or any other model's — is untouched.
CONSTRUCTOR_METHODS: frozenset[str] = frozenset(
    {"model_construct", "model_validate", "model_validate_json", "construct", "parse_obj"}
)

#: The one module allowed to call them: every hook goes through its `mint_claim`.
MINTING_SITE = "hooks/provenance.py"

#: Directories under the store-agent source root the rule does not span. See the module docstring
#: — `external/` is the Tier-2 path, whose claims are `seller_asserted` by construction.
EXEMPT_DIRECTORIES: tuple[str, ...] = ("external",)


@dataclass(frozen=True)
class Offence:
    """One forbidden construction: where it is, and what it called."""

    path: str
    line: int
    name: str

    def __str__(self) -> str:
        return (
            f"{self.path}:{self.line}: {self.name}(...) constructed outside {MINTING_SITE} — "
            f"call hooks.provenance.mint_{self.name.lower()}() instead (R8)"
        )


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Every local name bound to a string literal. ``WANTED = 'Claim'; MODELS[WANTED](...)``."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings[target.id] = node.value.value
    return bindings


def _guarded_names(tree: ast.Module) -> dict[str, str]:
    """Every local name in `tree` bound to a guarded class, mapped to which class it is.

    Matching the bare callee name alone makes the rule about the token `Claim` rather than about
    the object: ``from contracts import Claim as Fact`` and ``P = Provenance`` both bind a guarded
    class to a name the token test never sees, and neither is an exotic spelling. Resolving the
    bindings first is what turns this into a rule about constructing the thing.

    Three binding forms are followed, because all three are ordinary Python: an aliased import,
    a rebinding from another local name, and a rebinding from a module attribute
    (``Fact = contracts.Claim``). Assignments are resolved to a fixed point rather than in source
    order, so a rebinding written above its own source (inside a function defined before the
    module-level alias, say) is still followed. Modules are small; this converges in a pass or two.
    """
    names = {name: name for name in GUARDED_CONSTRUCTORS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name in GUARDED_CONSTRUCTORS and imported.asname:
                    names[imported.asname] = imported.name

    assignments: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if isinstance(node.value, ast.Name):
            source = node.value.id
        elif isinstance(node.value, ast.Attribute) and node.value.attr in GUARDED_CONSTRUCTORS:
            source = node.value.attr
        else:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                assignments.append((target.id, source))

    changed = True
    while changed:
        changed = False
        for bound, source in assignments:
            guarded = names.get(source)
            if guarded is not None and names.get(bound) != guarded:
                names[bound] = guarded
                changed = True
    return names


def _named_string(node: ast.expr, strings: dict[str, str]) -> str | None:
    """The string a node denotes: a literal, or a local name bound to one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return strings.get(node.id)
    return None


def _resolves_to_guarded(
    node: ast.expr, names: dict[str, str], strings: dict[str, str]
) -> str | None:
    """Which guarded class the expression `node` denotes, or `None`.

    The static half exists for the hosted branch the runtime guard never executes, which means it
    has to survive being written by someone who does not want it to fire. Every spelling below
    reaches the identical class object and none of them is exotic:

    * a name bound to it, imported, aliased, or rebound — ``Claim``, ``Fact``, ``P``;
    * a module attribute — ``contracts.Claim``, ``protocol.Claim``;
    * ``getattr(module, 'Claim')`` — the same attribute access, spelled as a call;
    * a registry lookup — ``MODELS['Claim']``, or ``MODELS[WANTED]`` with ``WANTED = 'Claim'``.

    Resolution is by construction, not by regex, so widening it does not widen what counts as a
    construction: every branch still has to name a guarded class.
    """
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        return node.attr if node.attr in GUARDED_CONSTRUCTORS else None
    if isinstance(node, ast.Call):
        callee = node.func
        if isinstance(callee, ast.Name) and callee.id == "getattr" and len(node.args) >= 2:
            wanted = _named_string(node.args[1], strings)
            return wanted if wanted in GUARDED_CONSTRUCTORS else None
        return None
    if isinstance(node, ast.Subscript):
        wanted = _named_string(node.slice, strings)
        return wanted if wanted in GUARDED_CONSTRUCTORS else None
    return None


def _guarded_target(node: ast.Call, names: dict[str, str], strings: dict[str, str]) -> str | None:
    """Which guarded class `node` builds, or `None` if it builds none.

    Two questions, in this order: is the callee one of the class's constructor methods, in which
    case the *receiver* is what must resolve to a guarded class (`Claim.model_validate(...)`,
    `p.Claim.model_validate(...)`); otherwise, does the callee itself resolve to one.
    """
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in CONSTRUCTOR_METHODS:
        return _resolves_to_guarded(func.value, names, strings)
    return _resolves_to_guarded(func, names, strings)


def _is_exempt(relative: Path) -> bool:
    if relative.as_posix() == MINTING_SITE:
        return True
    return bool(relative.parts) and relative.parts[0] in EXEMPT_DIRECTORIES


def _python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def hosted_claim_construction_offenders(source_root: str | Path) -> list[Offence]:
    """Every forbidden `Claim(...)` / `Provenance(...)` call under a store-agent `src/` tree.

    Returns them rather than raising, so a caller can report all of them at once instead of
    whichever one the parser reached first.
    """
    root = Path(source_root)
    offences: list[Offence] = []
    for path in _python_files(root):
        relative = path.relative_to(root)
        if _is_exempt(relative):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = _guarded_names(tree)
        strings = _string_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _guarded_target(node, names, strings)
            if name is not None:
                offences.append(Offence(path=relative.as_posix(), line=node.lineno, name=name))
    return offences


def format_offences(offences: Iterable[Offence]) -> str:
    """A multi-line report naming every offence, for an assertion message or a CI log."""
    listed = list(offences)
    if not listed:
        return "no direct protocol-object construction on the hosted path"
    return "\n".join(
        [
            f"{len(listed)} direct claim construction(s) on the hosted store-agent path. R8: a "
            "fact enters a hosted bid only through a tool hook.",
            *(f"  {offence}" for offence in listed),
        ]
    )


__all__ = [
    "CONSTRUCTOR_METHODS",
    "EXEMPT_DIRECTORIES",
    "GUARDED_CONSTRUCTORS",
    "MINTING_SITE",
    "Offence",
    "format_offences",
    "hosted_claim_construction_offenders",
]
