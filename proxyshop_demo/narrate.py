"""Plain-text narration for the demo driver.

Deliberately dumb: no colour, no cursor control, no progress spinners. The output of this
driver is read three ways — over a shoulder in a room, scrolled back in a terminal, and
asserted on by ``docs/tests/test_demo_driver.py`` — and ANSI escapes serve exactly one of
those three. Everything here writes plain ASCII to a stream the caller passes in.

The one rule this module exists to enforce: **a beat that did not run says so.**
:meth:`Narrator.gap` renders a labelled ``DOES NOT RUN YET`` block, and it is the reason the
narrator is a class rather than a handful of ``print`` calls — a driver that could only print
successes would quietly become a demo of the parts that work, which is the failure this whole
lane is a response to.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from typing import Any, TextIO

__all__ = ["Narrator", "money"]

#: The rendered width. 80 so a projector and a `git log` pager both fit it.
WIDTH = 80


def money(amount: Any, currency: str = "USD") -> str:
    """``389.0`` -> ``"$389.00"``. Anything unreadable renders verbatim, never as ``$0.00``."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return f"<unreadable price {amount!r}>"
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{value:,.2f}"


class Narrator:
    """Writes the journey as prose plus the real data behind it."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._out = stream if stream is not None else sys.stdout
        self._beats = 0

    # -- structure ----------------------------------------------------------------
    def title(self, text: str, subtitle: str = "") -> None:
        self.line("=" * WIDTH)
        self.line(f"  {text}")
        if subtitle:
            self.line(f"  {subtitle}")
        self.line("=" * WIDTH)

    def beat(self, number: int, text: str) -> None:
        self._beats += 1
        self.line("")
        self.line("-" * WIDTH)
        self.line(f"  BEAT {number}.  {text}")
        self.line("-" * WIDTH)

    def section(self, text: str) -> None:
        self.line("")
        self.line(f"  {text}")

    # -- content ------------------------------------------------------------------
    def say(self, text: str) -> None:
        """A paragraph of plain English, wrapped by hand at the source rather than at runtime."""
        for paragraph in text.strip("\n").split("\n"):
            self.line(f"  {paragraph.strip()}" if paragraph.strip() else "")

    def wire(self, method: str, url: str, status: int | str, note: str = "") -> None:
        """One real HTTP round trip, named so a viewer can see it is not a simulation."""
        tail = f"   {note}" if note else ""
        self.line(f"  >> {method:<5} {url}   -> {status}{tail}")

    def fact(self, label: str, value: Any) -> None:
        self.line(f"      {label:<26} {value}")

    def bullet(self, text: str, marker: str = "-") -> None:
        self.line(f"      {marker} {text}")

    def numbered(self, items: Iterable[str]) -> None:
        for index, item in enumerate(sorted_or_given(items), start=1):
            self.line(f"      {index}. {item}")

    def quote(self, text: str) -> None:
        self.line(f'      "{text}"')

    def gap(self, headline: str, body: str) -> None:
        """A beat the product cannot do yet, printed rather than skipped.

        Skipping it would make this driver a demo of the parts that work, which is the same
        lie as a green board over a broken system.
        """
        self.line("")
        self.line(f"  !! DOES NOT RUN YET: {headline}")
        for paragraph in body.strip("\n").split("\n"):
            self.line(f"     {paragraph.strip()}" if paragraph.strip() else "")

    def kv_block(self, rows: Mapping[str, Any]) -> None:
        for label, value in rows.items():
            self.fact(label, value)

    def line(self, text: str = "") -> None:
        print(text, file=self._out)

    def blank(self) -> None:
        self.line("")


def sorted_or_given(items: Iterable[str]) -> list[str]:
    """The items as a list, in the order given. Named so call sites read as a decision.

    Order is never sorted here: the clarifier's questions and the buyer's turns are sequences
    whose order IS the content, and a narrator that reordered them would be telling a
    different story from the one the run produced.
    """
    return list(items)
