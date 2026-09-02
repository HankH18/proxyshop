"""A mechanical check that nothing outside this package mints a discount code.

T-036 exists to make "checkout goes through the port" an *architectural* fact rather than a
convention. A convention is a comment; the thing that keeps holding a year later is a check
that fails the build. So: :func:`code_minting_call_sites` walks a source tree with :mod:`ast`
and reports every place a discount code is actually minted, and the test that owns this
asserts every such place is inside ``apps/exchange/src/checkout/``.

Two kinds of name, and they need different rules
------------------------------------------------

:data:`MINTING_METHODS` — ``mint_code``, ``create_code``
    These *are* the mint. There is no legitimate reason for code outside this package to
    hold one, so **any reference counts**, not only a call. That is what closes the whole
    family of aliasing escapes: ``fn = creator.create_code`` and ``fn = mint_code`` are
    plain attribute and name loads, invisible to a check that only looks at ``Call.func``,
    and the call that follows is through a name the check has never heard of. A check that
    matched three literal callee spellings was defeated by a rename.

:data:`MINTING_CLIENTS` — ``code_creator``
    This is the injected merchant client, and **holding it is legitimate** — ``accept()``
    receives it and forwards it, and the service reads it off ``app.state`` to put on a
    request. Only *invoking* it mints. So a reference is not a site and a call is. This is
    a deliberate narrowing of the old rule, which flagged ``getattr(state, "code_creator")``
    — a read, not a mint — while missing ``creator.create_code`` assigned to a variable.

What counts as a minting **call site**, and what does not:

* ``mint_code(...)`` / ``x.mint_code(...)``            -> a call site
* ``creator.create_code(...)`` / ``create_code(...)``  -> a call site
* ``fn = creator.create_code``                         -> a call site (attribute alias)
* ``fn = mint_code``                                   -> a call site (name alias)
* ``from .codes import mint_code as m``                -> a call site, and ``m`` is tracked
* ``getattr(creator, "create_code")``                  -> a call site (the getattr escape)
* ``attrgetter("create_code")(creator)``               -> a call site (same escape, stdlib)
* ``code_creator(...)``                                -> a call site (the client, invoked)
* ``getattr(state, "code_creator", None)(...)``        -> a call site (invoked indirectly)
* ``def accept(auction, bid_id, code_creator, mode)``  -> **not** a call site
* ``CheckoutRequest(code_creator=creator)``            -> **not** a call site
* ``getattr(state, "code_creator", None)``             -> **not** a call site (a read)

The negatives matter as much as the positives: ``accept()`` legitimately receives and
forwards the merchant client without ever invoking it, and a lint that flagged the
parameter name would push authors to rename the thing rather than to stop calling it. The
check is about minting, not about vocabulary.

What it still cannot see, stated so nobody mistakes this for a proof: a name computed at
runtime (``getattr(creator, "create_" + "code")``), a lookup through ``vars()`` or
``__dict__``, or an ``exec``. Those are not defeats of the check so much as evidence in
themselves — they are unreviewable in a code review too.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CHECKOUT_REQUEST",
    "INDIRECT_LOOKUPS",
    "MINTING_CALLEES",
    "MINTING_CLIENTS",
    "MINTING_METHODS",
    "TRUSTED_DOMAIN_KEYWORD",
    "MintingCallSite",
    "UnboundCheckoutRequest",
    "code_minting_call_sites",
    "unbound_checkout_requests",
]

#: The request record the domain guard reads its trusted half from.
CHECKOUT_REQUEST = "CheckoutRequest"

#: The keyword that decides whether that guard is real or decorative.
TRUSTED_DOMAIN_KEYWORD = "registered_domains"

#: The mint itself. Referencing one of these at all — calling it, aliasing it, importing it,
#: fetching it by string — is a minting site.
MINTING_METHODS: frozenset[str] = frozenset({"mint_code", "create_code"})

#: The injected merchant client. Holding and forwarding it is legitimate; calling it mints.
MINTING_CLIENTS: frozenset[str] = frozenset({"code_creator"})

#: Every name that mints when called. Kept for the published name it has always had.
MINTING_CALLEES: frozenset[str] = MINTING_METHODS | MINTING_CLIENTS

#: Indirect attribute lookups, and which argument carries the attribute name. These are the
#: string-shaped way to reach a method a name-based check would otherwise catch.
INDIRECT_LOOKUPS: dict[str, int] = {"getattr": 1, "attrgetter": 0}


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


def _indirect_lookup_of(node: ast.expr) -> str | None:
    """If ``node`` is ``getattr(x, "name")`` or ``attrgetter("name")``, return ``name``."""
    if not isinstance(node, ast.Call):
        return None
    lookup = _callee_name(node.func)
    index = INDIRECT_LOOKUPS.get(lookup or "")
    if index is None or len(node.args) <= index:
        return None
    argument = node.args[index]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    return None


def _aliases(tree: ast.AST) -> set[str]:
    """Local names bound to a minting method by an import — ``import mint_code as m``."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name.rsplit(".", 1)[-1] in MINTING_METHODS:
                bound.add(alias.asname or alias.name)
    return bound


def _sites_in_source(source: str, path: Path) -> Iterator[MintingCallSite]:
    tree = ast.parse(source, filename=str(path))
    aliases = _aliases(tree)
    methods = MINTING_METHODS | aliases
    callees = MINTING_CALLEES | aliases

    # (line, callee) — a call to `mint_code` is found twice, once as a call and once as the
    # `Load` of its own func, and it is one site.
    found: dict[tuple[int, str], MintingCallSite] = {}

    def record(line: int, callee: str) -> None:
        found.setdefault((line, callee), MintingCallSite(path=path, line=line, callee=callee))

    for node in ast.walk(tree):
        # Importing the mint is holding the mint.
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                original = alias.name.rsplit(".", 1)[-1]
                if original in MINTING_METHODS:
                    label = original if alias.asname is None else f"{original} as {alias.asname}"
                    record(node.lineno, f"import {label}")
            continue

        if isinstance(node, ast.Call):
            name = _callee_name(node.func)
            if name in callees:
                record(node.lineno, name or "?")
            else:
                # `getattr(state, "code_creator", None)(...)` — obtained indirectly, then
                # invoked. Obtaining alone is a read; the call is what mints.
                indirect = _indirect_lookup_of(node.func)
                if indirect in callees:
                    record(node.lineno, f"{indirect}(...) via indirect lookup")

            # `getattr(creator, "create_code")` — for a *method*, obtaining it is already
            # the escape, whether or not this expression is the one that calls it.
            fetched = _indirect_lookup_of(node)
            if fetched in methods:
                record(node.lineno, f"getattr(..., {fetched!r})")
            continue

        # Any bare reference to the mint: `fn = creator.create_code`, `fn = mint_code`,
        # `handlers = [mint_code]`. This is the rule a rename cannot get around.
        if isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load):
            referenced = node.id if isinstance(node, ast.Name) else node.attr
            if referenced in methods:
                record(node.lineno, referenced)

    yield from found.values()


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
    return sorted(found, key=lambda site: (str(site.path), site.line, site.callee))


# =====================================================================================
# The second mechanical check: the domain guard must be handed something to trust
# =====================================================================================
@dataclass(frozen=True)
class UnboundCheckoutRequest:
    """A ``CheckoutRequest`` built without the platform's registered-domain lookup."""

    path: Path
    line: int
    reason: str = f"no {TRUSTED_DOMAIN_KEYWORD}="

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {CHECKOUT_REQUEST}(...) with {self.reason}"


def _unbound_requests_in_source(source: str, path: Path) -> Iterator[UnboundCheckoutRequest]:
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_name(node.func) != CHECKOUT_REQUEST:
            continue
        names = {keyword.arg for keyword in node.keywords}
        if TRUSTED_DOMAIN_KEYWORD in names:
            continue
        # `**payload` could carry it, and could equally not. "Could be fine" is not the
        # standard a guard is held to, and spelling the keyword out is one line.
        reason = (
            f"{TRUSTED_DOMAIN_KEYWORD} may or may not be in the ** splat"
            if None in names
            else f"no {TRUSTED_DOMAIN_KEYWORD}="
        )
        yield UnboundCheckoutRequest(path=path, line=node.lineno, reason=reason)


def unbound_checkout_requests(
    root: Path | str,
    *,
    skip_dirs: Iterable[str] = ("__pycache__", ".venv", "node_modules", "tests"),
) -> list[UnboundCheckoutRequest]:
    """Every ``CheckoutRequest(...)`` under ``root`` that supplies no trusted domain source.

    The defect this exists to make unrepeatable: with no
    :attr:`CheckoutRequest.registered_domains`, ``registered_domain_for`` returns
    ``request.store_domain`` — **a field the bidding store wrote** — and the exact-host
    check then compares one store-authored value against another. A store that posts
    ``store_domain: "attacker.tld"`` next to ``checkout_url: "https://attacker.tld/…"``
    agrees with itself, so the guard passes and the buyer is handed a real discount code on
    a host the platform never registered. The guard rejects only a store that contradicts
    *itself*, which no attacker does.

    The port cannot refuse that at runtime, and it is worth being exact about why: with no
    external source there is **no information** distinguishing a legitimate bid from a
    spoofed one — the two are byte-identical — so "fail closed" would mean refusing every
    checkout, including every honest one. The decision belongs to whoever builds the
    request, because only they can reach the platform's registry. That makes it a
    convention, and this function is what turns the convention into a check that fails the
    build: exactly the same move :func:`code_minting_call_sites` makes for minting.

    :mod:`.sellers` is what a call site passes — ``StaticRegisteredDomains`` for a real
    table, ``NoRegisteredDomains`` to be explicit that nothing is registered yet (which
    refuses every checkout, loudly, instead of trusting the bid quietly).
    """
    root = Path(root)
    skip = set(skip_dirs)
    found: list[UnboundCheckoutRequest] = []
    for path in sorted(root.rglob("*.py")):
        if skip & set(path.relative_to(root).parts):
            continue
        found.extend(_unbound_requests_in_source(path.read_text(encoding="utf-8"), path))
    return sorted(found, key=lambda site: (str(site.path), site.line))
