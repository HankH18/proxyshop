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
minting site itself and `src/external/` — the names `Claim` and `Provenance` may be imported and
annotated with, but never *called*. Building one means calling `mint_claim` / `mint_provenance`
in :mod:`.provenance`, which stamps the hook's own source class and the published authority rank
and cannot be talked into stamping another hook's.

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


def _guarded_names(tree: ast.Module) -> dict[str, str]:
    """Every local name in `tree` bound to a guarded class, mapped to which class it is.

    Matching the bare callee name alone makes the rule about the token `Claim` rather than about
    the object: ``from contracts import Claim as Fact`` and ``P = Provenance`` both bind a guarded
    class to a name the token test never sees, and neither is an exotic spelling. Resolving the
    bindings first is what turns this into a rule about constructing the thing.

    Assignments are resolved to a fixed point rather than in source order, so a rebinding written
    above its own source (inside a function defined before the module-level alias, say) is still
    followed. Modules are small; this converges in a pass or two.
    """
    names = {name: name for name in GUARDED_CONSTRUCTORS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name in GUARDED_CONSTRUCTORS and imported.asname:
                    names[imported.asname] = imported.name

    assignments = [
        (target.id, node.value.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    changed = True
    while changed:
        changed = False
        for bound, source in assignments:
            guarded = names.get(source)
            if guarded is not None and names.get(bound) != guarded:
                names[bound] = guarded
                changed = True
    return names


def _guarded_target(node: ast.Call, names: dict[str, str]) -> str | None:
    """Which guarded class `node` builds, or `None` if it builds none.

    Three shapes reach the same object: calling a name bound to the class (`Claim(...)`, or any
    alias of it), calling it off a module (`protocol.Claim(...)`), and calling one of its
    constructor class methods (`Claim.model_validate(...)`).
    """
    func = node.func
    if isinstance(func, ast.Name):
        return names.get(func.id)
    if isinstance(func, ast.Attribute):
        if func.attr in CONSTRUCTOR_METHODS and isinstance(func.value, ast.Name):
            return names.get(func.value.id)
        return func.attr if func.attr in GUARDED_CONSTRUCTORS else None
    return None


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
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _guarded_target(node, names)
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
