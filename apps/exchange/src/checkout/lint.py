"""A mechanical check that nothing outside this package mints a discount code.

T-036 exists to make "checkout goes through the port" an *architectural* fact rather than a
convention. A convention is a comment; the thing that keeps holding a year later is a check
that fails the build. So: :func:`code_minting_call_sites` walks a source tree with :mod:`ast`
and reports every place a discount code is actually minted, and the test that owns this
asserts every such place is inside ``apps/exchange/src/checkout/``.

What counts as a minting **call site** — and, just as importantly, what does not:

* ``mint_code(...)`` or ``x.mint_code(...)``           -> a call site
* ``creator.create_code(...)`` or ``create_code(...)`` -> a call site
* ``code_creator(...)``  (the injected client, invoked directly)  -> a call site
* ``getattr(creator, "create_code")``                  -> a call site (the alias escape)
* ``def accept(auction, bid_id, code_creator, mode)``  -> **not** a call site
* ``CheckoutRequest(code_creator=creator)``            -> **not** a call site

The last two matter: ``accept()`` legitimately *receives* and *forwards* the merchant client
without ever invoking it, and a lint that flagged the parameter name would push authors to
rename the thing rather than to stop calling it. The check is about invocation.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = ["MINTING_CALLEES", "MintingCallSite", "code_minting_call_sites"]

#: Callee names that mint, or ask someone else to mint, a discount code.
MINTING_CALLEES: frozenset[str] = frozenset({"mint_code", "create_code", "code_creator"})


@dataclass(frozen=True)
class MintingCallSite:
    """One place a discount code is minted, for a human to look at."""

    path: Path
    line: int
    callee: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.callee}(...)"


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _sites_in_source(source: str, path: Path) -> Iterator[MintingCallSite]:
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        name = _callee_name(node.func)
        if name in MINTING_CALLEES:
            yield MintingCallSite(path=path, line=node.lineno, callee=name or "?")
            continue

        # The alias escape: `create = getattr(creator, "create_code")`, then `create(...)`.
        # The second line is invisible to a name-based check, so the first is what we flag.
        if name == "getattr":
            for argument in node.args[1:2]:
                if isinstance(argument, ast.Constant) and argument.value in MINTING_CALLEES:
                    yield MintingCallSite(
                        path=path, line=node.lineno, callee=f"getattr(..., {argument.value!r})"
                    )


def code_minting_call_sites(
    root: Path | str,
    *,
    skip_dirs: Iterable[str] = ("__pycache__", ".venv", "node_modules", "tests"),
) -> list[MintingCallSite]:
    """Every discount-code minting call site under ``root``, sorted by path then line.

    ``tests`` is skipped by default: a test legitimately drives a provider directly, and a
    lint that forbade that would forbid testing the thing it is protecting.
    """
    root = Path(root)
    skip = set(skip_dirs)
    found: list[MintingCallSite] = []
    for path in sorted(root.rglob("*.py")):
        if skip & set(path.relative_to(root).parts):
            continue
        found.extend(_sites_in_source(path.read_text(encoding="utf-8"), path))
    return sorted(found, key=lambda site: (str(site.path), site.line))
