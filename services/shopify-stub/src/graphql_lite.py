"""A very small GraphQL request parser — enough to route, not a GraphQL server.

The stub answers exactly four root fields. It does not need a schema, an executor, or a
selection-set resolver, and pulling in a GraphQL engine to get one would be a dependency
this repo's manifest does not carry (and workers may not add). What it *does* need is to
tell those four root fields apart honestly, so that:

* a caller that asks for a field the stub does not implement gets Shopify's ``undefinedField``
  error rather than a plausible-looking success for the wrong operation, and
* a caller that inlines its arguments (``orders(first: 5)``) works as well as one that passes
  them as variables (``orders(first: $first)``), because both are legal GraphQL and a stub
  that only supports one of them silently constrains its consumers.

What this module deliberately does **not** do is honour the selection set: the stub returns
the whole documented node shape regardless of which fields were asked for. That is a
declared limitation, not an oversight — a consumer that reads a field it did not select
will still work against real Shopify's *response*, it just would not have received it.
Contract tests here therefore assert the stub's shape is a **superset** of the recorded
real-API shape, never that it is byte-identical to a narrowed selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_NAME_START = set("_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_NAME_CHARS = _NAME_START | set("0123456789")
_OPERATION_KEYWORDS = {"query", "mutation", "subscription"}


class GraphQLSyntaxError(ValueError):
    """The document is not parseable as a GraphQL operation."""


@dataclass
class ParsedOperation:
    """The parts of a request the stub actually routes on.

    Attributes:
        operation: ``"query"`` or ``"mutation"`` (``"query"`` for the shorthand ``{ ... }``).
        field_name: the first root field's name, with any alias resolved away.
        alias: the alias the caller used, or ``None``.
        arguments: the root field's inline arguments, with ``$var`` references already
            resolved against the request's ``variables``.
    """

    operation: str
    field_name: str
    alias: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def response_key(self) -> str:
        """The key this field's result must appear under in ``data``."""
        return self.alias or self.field_name


class _Scanner:
    """Character scanner with GraphQL's comment and comma rules."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def skip_ignored(self) -> None:
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char in " \t\r\n,﻿":
                self.pos += 1
            elif char == "#":
                while self.pos < len(self.text) and self.text[self.pos] != "\n":
                    self.pos += 1
            else:
                return

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise GraphQLSyntaxError(f"expected {char!r} at offset {self.pos}")
        self.pos += 1

    def read_name(self) -> str:
        self.skip_ignored()
        if self.peek() not in _NAME_START:
            raise GraphQLSyntaxError(f"expected a name at offset {self.pos}")
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] in _NAME_CHARS:
            self.pos += 1
        return self.text[start : self.pos]

    def skip_balanced(self, opener: str, closer: str) -> None:
        """Consume a balanced ``opener``…``closer`` run, respecting string literals."""
        self.skip_ignored()
        self.expect(opener)
        depth = 1
        while self.pos < len(self.text) and depth:
            char = self.text[self.pos]
            if char == '"':
                self._skip_string()
                continue
            if char == "#":
                while self.pos < len(self.text) and self.text[self.pos] != "\n":
                    self.pos += 1
                continue
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
            self.pos += 1
        if depth:
            raise GraphQLSyntaxError(f"unbalanced {opener!r}")

    def _skip_string(self) -> None:
        self.expect('"')
        if self.text[self.pos : self.pos + 2] == '""':
            self.pos += 2
            end = self.text.find('"""', self.pos)
            self.pos = len(self.text) if end < 0 else end + 3
            return
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                self.pos += 2
                continue
            self.pos += 1
            if char == '"':
                return
        raise GraphQLSyntaxError("unterminated string")

    def read_string(self) -> str:
        self.expect('"')
        if self.text[self.pos : self.pos + 2] == '""':
            self.pos += 2
            end = self.text.find('"""', self.pos)
            if end < 0:
                raise GraphQLSyntaxError("unterminated block string")
            value = self.text[self.pos : end]
            self.pos = end + 3
            return value.strip()
        out: list[str] = []
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "\\":
                nxt = self.text[self.pos + 1 : self.pos + 2]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                self.pos += 2
                continue
            self.pos += 1
            if char == '"':
                return "".join(out)
            out.append(char)
        raise GraphQLSyntaxError("unterminated string")


def parse_operation(document: str, variables: dict[str, Any] | None = None) -> ParsedOperation:
    """Parse the operation head and the first root field of ``document``.

    Args:
        document: the GraphQL document as sent in the request's ``query`` member.
        variables: the request's ``variables``, used to resolve ``$name`` arguments.

    Returns:
        The :class:`ParsedOperation`.

    Raises:
        GraphQLSyntaxError: the document is not a parseable operation. The stub answers this
            with Shopify's parse-error response rather than a 500, because a malformed query
            is a client error there too.
    """
    values = variables or {}
    scanner = _Scanner(document)
    scanner.skip_ignored()
    operation = "query"
    if scanner.peek() in _NAME_START:
        keyword = scanner.read_name()
        if keyword not in _OPERATION_KEYWORDS:
            raise GraphQLSyntaxError(f"unexpected token {keyword!r}; expected an operation")
        operation = keyword
        scanner.skip_ignored()
        if scanner.peek() in _NAME_START:
            scanner.read_name()  # the operation name; the stub does not route on it
        scanner.skip_ignored()
        if scanner.peek() == "(":
            scanner.skip_balanced("(", ")")
        scanner.skip_ignored()
        while scanner.peek() == "@":  # directives on the operation
            scanner.pos += 1
            scanner.read_name()
            scanner.skip_ignored()
            if scanner.peek() == "(":
                scanner.skip_balanced("(", ")")
            scanner.skip_ignored()
    scanner.skip_ignored()
    scanner.expect("{")
    first = scanner.read_name()
    scanner.skip_ignored()
    alias: str | None = None
    field_name = first
    if scanner.peek() == ":":
        scanner.pos += 1
        alias = first
        field_name = scanner.read_name()
        scanner.skip_ignored()
    arguments: dict[str, Any] = {}
    if scanner.peek() == "(":
        arguments = _read_arguments(scanner, values)
    return ParsedOperation(
        operation=operation, field_name=field_name, alias=alias, arguments=arguments
    )


def _read_arguments(scanner: _Scanner, variables: dict[str, Any]) -> dict[str, Any]:
    scanner.expect("(")
    out: dict[str, Any] = {}
    while True:
        scanner.skip_ignored()
        if scanner.peek() == ")":
            scanner.pos += 1
            return out
        name = scanner.read_name()
        scanner.skip_ignored()
        scanner.expect(":")
        out[name] = _read_value(scanner, variables)


def _read_value(scanner: _Scanner, variables: dict[str, Any]) -> Any:
    scanner.skip_ignored()
    char = scanner.peek()
    if char == "$":
        scanner.pos += 1
        name = scanner.read_name()
        return variables.get(name)
    if char == '"':
        return scanner.read_string()
    if char == "[":
        scanner.pos += 1
        items: list[Any] = []
        while True:
            scanner.skip_ignored()
            if scanner.peek() == "]":
                scanner.pos += 1
                return items
            items.append(_read_value(scanner, variables))
    if char == "{":
        scanner.pos += 1
        obj: dict[str, Any] = {}
        while True:
            scanner.skip_ignored()
            if scanner.peek() == "}":
                scanner.pos += 1
                return obj
            key = scanner.read_name()
            scanner.skip_ignored()
            scanner.expect(":")
            obj[key] = _read_value(scanner, variables)
    if char in "-0123456789":
        start = scanner.pos
        scanner.pos += 1
        while scanner.pos < len(scanner.text) and scanner.text[scanner.pos] in "0123456789.eE+-":
            scanner.pos += 1
        raw = scanner.text[start : scanner.pos]
        return float(raw) if any(c in raw for c in ".eE") else int(raw)
    name = scanner.read_name()
    if name == "true":
        return True
    if name == "false":
        return False
    if name == "null":
        return None
    return name  # an enum value, kept as its literal name
