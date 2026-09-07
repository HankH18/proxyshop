"""Which attribute names a catalogue can actually carry, and the guard that uses it.

R19 makes a hard constraint an eligibility **filter** decided against *verified supporting
facts*. Read literally, that has a consequence this package used to ignore: a filter naming
an attribute no catalogue carries can never be supported by anything, so it is not a strict
filter. It is an empty shortlist wearing one.

That is not a hypothetical. Measured on this branch before this module existed, ``POST
/buyer/intent/clarify`` with the three turns ``e2e/support/s1/run.json`` ships as the
shopper's own words produced::

    "hard_constraints": [{"field": "brew_method", "op": "eq", "value": "espresso"}]

Nothing in this tree carries a ``brew_method`` attribute — not
``fixtures/catalog/coffee.json``, not a store agent's catalogue, not a bid, not a catalog
snapshot. Driven against the exchange's own ``POST /auctions`` and
``GET /auctions/{id}/shortlist`` with the S1 roster, catalogues and trust snapshot, that
intent produced **0 slots**. The same auction with a constraint the catalogue does speak
(``list_price lte 500``) produced 2, and narrowing it to ``lte 400`` produced 1 and to
``lte 100`` produced 0 — so the pipeline underneath was never broken. The vocabulary was.

Two failure shapes, and the second is the reason this file is a guard rather than a lexicon
edit. A store agent looks every hard-constraint field up in its own catalogue
(``store_agent.runtime.bidding`` calls ``hooks.get_product_fact(product_ref,
constraint.field)`` and rejects the product when the catalogue has no such key), so it
**declines to bid at all**; and the exchange decides the constraint only from a verified
claim (``exchange.ranking.filters.verified_attributes``), so even a bid that arrives is
excluded ``hard_constraint_unsatisfied``. Both read to a shopper as "no stores matched" and
both actually mean "the question was unanswerable".

Where the names come from
-------------------------
``fixtures/catalog/coffee.json`` is the seed catalogue config the parameterised generator
reads, and its ``attribute_template`` keys are exactly the attribute names a generated
coffee product carries. It is referenced by ``fixtures/manifest.json`` with a sha256, so
those bytes are human-approved ground truth — which is why this module quotes them rather
than inventing a vocabulary of its own, and why the fix belongs at THIS end. The clarifier
is where an attribute name is minted; the catalogue is where one is approved.

What this guard deliberately does NOT do
----------------------------------------
**It only refuses what it can prove.** ``coffee`` is the only ``seed_category`` in this
tree, so it is the only vocabulary declared here, and a category this module holds no
vocabulary for is left completely alone — a footwear shopper's ``size`` and ``waterproof``
are not purged on the guess that no footwear catalogue would carry them. A guard that
emptied ``hard_constraints`` for everyone would make every shortlist non-empty by admitting
everything, which is a worse bug than the one it replaced.

And it refuses a constraint by **recording** it (:class:`UnsatisfiableConstraint`, surfaced
on ``ClarifyOutcome.unsatisfiable`` and in the ``/clarify`` response), never by dropping it
in silence. "I could not turn 'espresso' into a filter, because no catalogue carries a
brew_method attribute" is an answer; an empty shortlist is not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CATALOGUE_ATTRIBUTES",
    "INTENT_AXES",
    "PLATFORM_ATTRIBUTES",
    "UnsatisfiableConstraint",
    "catalogue_speaks",
    "unspeakable_reason",
]

#: Attribute names every product in the network carries regardless of category, because the
#: platform rather than the catalogue config supplies them: ``store_agent.runtime.bidding``
#: reads ``LIST_PRICE_KEY = "list_price"`` off the catalogue for every bid and
#: ``IN_STOCK_KEY = "in_stock"`` off the live-state feed, and ``e2e/support/s1/run.json``'s
#: store rows carry both.
PLATFORM_ATTRIBUTES: frozenset[str] = frozenset({"list_price", "in_stock", "units_left"})

#: Axes of the intent itself rather than facts about a product, so a catalogue is not what
#: makes them decidable and their absence from one proves nothing.
#:
#: ``price_usd`` is the buyer's own budget ceiling. It is here rather than in a category
#: vocabulary, and that is a **stated exception with a measurement behind it**: no store
#: claims ``price_usd`` today, so ``price_usd lte 500`` is excluded
#: ``hard_constraint_unsatisfied`` by the exchange exactly like ``brew_method`` was (driven,
#: 0 slots). But the fix for that is not here — the exchange already HOLDS the offer's
#: price, so a price constraint is decidable from the bid it received without any claim at
#: all, and the catalogue's own price lives under ``list_price`` (the store's list price,
#: which is not the same quantity as the offer's). Renaming it here would trade one wrong
#: filter for another and silently move the buyer's ceiling off the price they pay.
INTENT_AXES: frozenset[str] = frozenset({"price_usd"})

#: Attribute names a catalogue carries, by ``Intent.category``.
#:
#: ``coffee`` is transcribed from the ``attribute_template`` keys of every product family in
#: ``fixtures/catalog/coffee.json`` — whole_bean, espresso_machine, grinder and accessory.
#: A category absent from this mapping is one this service has no catalogue for, and
#: :func:`catalogue_speaks` answers ``True`` for every field in it.
CATALOGUE_ATTRIBUTES: Mapping[str, frozenset[str]] = {
    "coffee": frozenset(
        {
            # whole_bean
            "origin",
            "roast_level",
            "process",
            "ingredients",
            "caffeine_mg_per_serving",
            "certifications",
            # espresso_machine
            "boiler_type",
            "pump_pressure_bar",
            "water_tank_l",
            "voltage",
            "compatible_with",
            "warranty_months",
            # grinder
            "burr_material",
            "burr_diameter_mm",
            "grind_settings",
            # accessory
            "material",
            "dishwasher_safe",
        }
    ),
}


@dataclass(frozen=True)
class UnsatisfiableConstraint:
    """A filter the clarifier declined to emit, and the reason it declined.

    Carried on the outcome and rendered in the ``/clarify`` response so the refusal is
    visible to whoever is looking at an unexpectedly short shortlist. The buyer's own words
    are not lost: the phrase that produced it is still in ``Intent.query``, and where it also
    named a category (``espresso`` does) that category still routes the auction.
    """

    field: str
    op: str
    value: Any
    reason: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.field, self.op)

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "op": self.op,
            "value": self.value,
            "reason": self.reason,
        }


def _normalised(field: Any) -> str:
    return " ".join(str(field).split()).casefold()


def catalogue_speaks(category: Any, field: Any) -> bool:
    """Can a ``category`` catalogue carry an attribute called ``field``?

    ``True`` when the category has no declared vocabulary here, because "this service does
    not know" and "no catalogue carries it" are different answers and only the second one
    justifies refusing a buyer's must-have.
    """
    name = _normalised(field)
    if not name:
        return False
    if name in INTENT_AXES or name in PLATFORM_ATTRIBUTES:
        return True
    vocabulary = CATALOGUE_ATTRIBUTES.get(_normalised(category))
    if vocabulary is None:
        return True
    return name in vocabulary


def unspeakable_reason(category: Any, field: Any) -> str | None:
    """Why this field cannot be a filter for this category, or ``None`` when it can.

    The message names the field, the category and the consequence, because the whole point
    of recording it is that "no stores matched" stops being the only thing anyone sees.
    """
    if catalogue_speaks(category, field):
        return None
    name = _normalised(field) or "<unnamed>"
    known = sorted(CATALOGUE_ATTRIBUTES.get(_normalised(category), frozenset()))
    return (
        f"no {_normalised(category)!r} catalogue in this network carries an attribute called "
        f"{name!r}, so no verified fact could ever support it; R19 makes a hard constraint an "
        f"eligibility filter decided on verified evidence, so this one would exclude every "
        f"candidate and empty the shortlist rather than narrow it. The attributes that "
        f"catalogue does carry are {known}."
    )
