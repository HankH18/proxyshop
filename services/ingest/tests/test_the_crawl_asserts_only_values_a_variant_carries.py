"""An option value the crawl writes must be one a variant of that product actually carries.

Run::

    PROXYSHOP_WORKER=1 ./.venv/bin/python -m pytest \
        services/ingest/tests/test_the_crawl_asserts_only_values_a_variant_carries.py -q

What was wrong
--------------
:func:`~ingest.adapters.mapping.option_attributes` read a Shopify product's top-level
``options[]`` block — ``{name, position, values[]}`` — and wrote **every** listed value as an
``AttributeValue``. But ``options[].values`` is the *picker's domain*: the set of choices the
option control can display for the whole option, not the set this product's variants were
built from. A merchant who publishes one option block across a family of products, or who
retires a variant without pruning the picker, leaves values in it that this product does not
have.

The witness in ``fixtures/real-catalogs-demo`` is
``Astro™ Non-Insulated Lightweight Sleeping Pad`` (``nemoequipment.com``). Its option block
declares ``Insulation: ["Insulated", "Non-Insulated"]``; its only two variants are
``Non-Insulated / Regular`` and ``Non-Insulated / Long Wide``; the product's own title says
Non-Insulated. Before this file, the crawl wrote ``insulation = "Insulated"`` for it.

Measured before the fix, over all nineteen recorded storefronts: **83 of 21,750 readings
(0.38%) on 35 products named a value no variant of that product carries** — capacity 40,
gender 22, color 11, width 4, silhouette 2, length 2, insulation 2. Every one of them is on
one host today, which is a property of *this corpus's composition* and not evidence the
surface is safe.

What it cost, driven
--------------------
``HardCriterion(field="insulation", op="eq", value="Insulated").decide(...)`` answered
``satisfied=True`` against that pad's readings, and ``.pushdown().as_parameter()`` yielded
``value_string="insulated"`` — exactly the string ``_FILTER_AND_RETURN``'s
``a.canonical_value_string = f.value_string`` matches on — so ``candidate_products`` retrieved
the **non-insulated** pad for a shopper whose hard constraint was "insulated".

That is R19 (a hard constraint may not be satisfied by an unverified fact) and D55 (the
platform must not introduce a fact it has not checked), in the one place where the platform is
speaking for a merchant.

Why the POSITIONAL join and not the title one
---------------------------------------------
Shopify states a variant's option values twice: positionally in ``option1``/``option2``/
``option3`` against ``options[]`` order, and again in ``title`` as those values joined with
``" / "``. Measured over the same nineteen storefronts (28,134 variant records):

* ``option1/2/3``: **0** variants state none of them; **0** options declare a ``position``
  that disagrees with their index in ``options[]``; **0** products publish more than three
  options; **0** option slots are declared by a product but stated by none of its variants.
* ``title``: **2,560 of 28,134 variant titles do not decompose** into the number of values the
  variant states, because a value may itself contain ``" / "`` — and a title join would drop
  **2,418** readings on 112 products, of which 2,335 are TRUE readings the positional join
  keeps. Where the title *does* decompose it agrees with the positional slots 28,134 times out
  of 28,134 and disagrees 0 times.

So the positional join is the reliable one on real data and the title is its confirmation, not
its source. Where the positional join cannot be made — no such slot stated, a value no variant
carries — nothing is written, which is the correct answer under D55: a dropped true attribute
costs a retrieval, an asserted false one costs a shopper the wrong product.

The cost of the join, and a check on it that does not use the join
------------------------------------------------------------------
It refuses 83 of 21,750 readings and keeps 21,667, and it drops **0** readings any variant
carries — the product count (3,443), the distinct ``AttributeValue`` id count (4,090) and the
key count (147) are all unchanged, because every refused value is genuinely carried by some
*other* product, which is exactly why it looked plausible on the one it was not.

"0 true readings dropped" measured against the variants is circular on its own, so here is a
witness that never touches them: the product's own TITLE. Across the 35 affected products, a
refused value appears as a whole word in the title **0** times out of 83, while a kept value
appears **35** times out of 144. (A naive word match scores the Astro pad's ``Insulated``
against ``Non-Insulated`` — the title is agreeing with the join there, not against it.)
"""

from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from exchange.retrieval.criteria import HardCriterion
from ingest.adapters.mapping import option_attributes
from ingest.graph.model import canonical_text, slug

#: services/ingest/tests/… -> tests -> ingest -> services -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The nineteen recorded storefronts the demo deployment is built from. Real bytes, recorded
#: once; nothing here opens a socket.
CORPUS = REPO_ROOT / "fixtures" / "real-catalogs-demo" / "stores"

#: The witness, named so a corpus edit that removes it fails loudly instead of quietly making
#: this file prove nothing.
ASTRO_HOST = "nemoequipment.com"
ASTRO_TITLE = "Astro™ Non-Insulated Lightweight Sleeping Pad"

#: What the corpus held when this was measured. Pinned so that a shrinking corpus cannot turn
#: the sweep below into a vacuous pass.
CORPUS_PRODUCTS = 4903
CORPUS_STORES = 19

#: Readings ``option_attributes`` emitted before the join, and after it. The difference is the
#: 83 the join refuses; every one of them is a value no variant carries, so the join's cost in
#: TRUE readings is 0. Both numbers are pinned: a join that started dropping true readings
#: would show up here as a fall in the second number.
READINGS_BEFORE_THE_JOIN = 21750
READINGS_AFTER_THE_JOIN = 21667


def _corpus_products() -> list[tuple[str, dict[str, Any]]]:
    """Every recorded product, as its storefront published it. ``(host, entry)``."""
    out: list[tuple[str, dict[str, Any]]] = []
    paths = sorted(CORPUS.glob("*.products.jsonl.gz"))
    assert len(paths) == CORPUS_STORES, f"expected {CORPUS_STORES} recorded stores, found {paths}"
    for path in paths:
        host = path.name.removesuffix(".products.jsonl.gz")
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    out.append((host, json.loads(line)))
    assert len(out) == CORPUS_PRODUCTS, f"expected {CORPUS_PRODUCTS} products, read {len(out)}"
    return out


def _variant_slot_values(entry: dict[str, Any]) -> dict[int, set[str]]:
    """What the product's variants ACTUALLY carry, per option slot, canonically folded.

    Deliberately independent of the implementation under test: this reads ``option1/2/3``
    off the raw record, so it stays a check on the mapping rather than a copy of it.
    """
    carried: dict[int, set[str]] = {}
    for variant in entry.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        for slot in range(1, 4):
            raw = variant.get(f"option{slot}")
            if raw is None:
                continue
            text = str(raw).strip()
            if text:
                carried.setdefault(slot, set()).add(canonical_text(text))
    return carried


def _unsupported(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """The readings this entry produces that no variant of it carries. Empty is the contract."""
    carried = _variant_slot_values(entry)
    declared: dict[str, set[str]] = {}
    for index, option in enumerate(entry.get("options") or []):
        if not isinstance(option, dict):
            continue
        position = option.get("position")
        slot = (
            position
            if isinstance(position, int) and not isinstance(position, bool) and 1 <= position <= 3
            else index + 1
        )
        key = slug(str(option.get("name") or ""))
        declared.setdefault(key, set()).update(carried.get(slot, set()))
    bad: list[tuple[str, str]] = []
    for reading in option_attributes(entry):
        if canonical_text(reading.value) not in declared.get(reading.key, set()):
            bad.append((reading.key, reading.value))
    return bad


@pytest.fixture(scope="module")
def astro_pad() -> dict[str, Any]:
    """The recorded ``Astro™ Non-Insulated Lightweight Sleeping Pad``, verbatim."""
    for host, entry in _corpus_products():
        if host == ASTRO_HOST and entry.get("title") == ASTRO_TITLE:
            return entry
    raise AssertionError(f"{ASTRO_TITLE!r} is no longer in {CORPUS}; this file grades nothing")


def test_the_astro_pad_is_never_written_as_insulated(astro_pad: dict[str, Any]) -> None:
    """The picker offers ``Insulated``. This product is not it, and neither is any variant."""
    variants = [str(v.get("title")) for v in astro_pad["variants"]]
    assert variants == ["Non-Insulated / Regular", "Non-Insulated / Long Wide"], variants

    readings = [(r.key, r.value) for r in option_attributes(astro_pad)]
    assert ("insulation", "Insulated") not in readings, (
        f"{ASTRO_TITLE!r} was written with insulation='Insulated'. Its option block declares "
        f"{astro_pad['options'][0]['values']} because that is the PICKER's domain; its only "
        f"variants are {variants} and its own title says Non-Insulated. Readings: {readings}"
    )
    # The control: the value it really does carry is still written, so the fix is a join and
    # not a mute button.
    assert ("insulation", "Non-Insulated") in readings, readings


def test_the_false_reading_satisfied_an_insulated_hard_constraint(
    astro_pad: dict[str, Any],
) -> None:
    """R19, driven: the constraint a shopper states must not be satisfied by a picker option.

    ``decide`` is given the readings exactly as ``ingest.graph.Candidate.attributes`` projects
    them, so this is the same comparison the retrieval layer makes, and ``pushdown`` is the
    graph-side half — ``value_string='insulated'`` is literally what
    ``a.canonical_value_string = f.value_string`` matches in ``_FILTER_AND_RETURN``.
    """
    projected = [
        {
            "key": reading.key,
            "value_string": reading.value,
            "value_number": None,
            "value_bool": None,
            "unit": None,
        }
        for reading in option_attributes(astro_pad)
    ]
    criterion = HardCriterion(field="insulation", op="eq", value="Insulated")
    verdict = criterion.decide(projected)
    assert not verdict.satisfied, (
        f"a shopper whose hard constraint is insulation='Insulated' is told the "
        f"{ASTRO_TITLE!r} satisfies it. Readings the crawl wrote: {projected}"
    )

    pushed = criterion.pushdown()
    assert pushed is not None
    parameter = pushed.as_parameter()
    assert parameter["value_string"] == "insulated", parameter
    carried = {canonical_text(row["value_string"]) for row in projected}
    assert parameter["value_string"] not in carried, (
        f"the graph-side filter's operand {parameter['value_string']!r} matches a "
        f"canonical_value_string this product's crawl wrote, so candidate_products retrieves "
        f"the non-insulated pad for an 'insulated' constraint. Wrote: {sorted(carried)}"
    )


def test_no_reading_names_a_value_no_variant_of_that_product_carries() -> None:
    """The whole corpus, not one product: 83 readings before this file, and 0 is the contract.

    Stated per key in the failure message rather than as a bare count, because the shape of
    the violation is what says whether a regression is the same defect or a new one.
    """
    per_key: Counter[str] = Counter()
    per_host: Counter[str] = Counter()
    offenders: list[str] = []
    total = 0
    for host, entry in _corpus_products():
        total += len(option_attributes(entry))
        bad = _unsupported(entry)
        if not bad:
            continue
        per_host[host] += len(bad)
        for key, value in bad:
            per_key[key] += 1
        if len(offenders) < 8:
            offenders.append(f"{host} {entry.get('title')!r}: {bad}")

    assert sum(per_key.values()) == 0, (
        f"{sum(per_key.values())} readings on {len(offenders)}+ products name an option value "
        f"no variant of that product carries — R19/D55.\n"
        f"  by key:  {per_key.most_common()}\n"
        f"  by host: {per_host.most_common()}\n"
        f"  e.g.:    " + "\n           ".join(offenders)
    )
    assert total == READINGS_AFTER_THE_JOIN, (
        f"the join now keeps {total} readings, not {READINGS_AFTER_THE_JOIN}. It refused "
        f"{READINGS_BEFORE_THE_JOIN - total} of the {READINGS_BEFORE_THE_JOIN} the unjoined "
        f"read produced; a number above "
        f"{READINGS_BEFORE_THE_JOIN - READINGS_AFTER_THE_JOIN} means true readings are being "
        f"dropped and the cost has changed"
    )


def test_a_value_is_matched_across_case_and_whitespace_but_not_across_meaning() -> None:
    """The join folds the way retrieval folds, and no further.

    ``AttributeValue.canonical_value_string`` is ``canonical_text`` — NFKC, case-folded,
    whitespace-collapsed — and that is what ``_FILTER_AND_RETURN`` compares. A join that
    matched more loosely than that would keep a reading retrieval cannot use; one that matched
    more strictly would drop a true reading over a merchant's stray capital.
    """
    entry = {
        "id": 1,
        "options": [{"name": "Color", "position": 1, "values": ["Deep  NAVY", "Chartreuse"]}],
        "variants": [{"id": 11, "title": "deep navy", "option1": " deep navy "}],
    }
    assert [(r.key, r.value) for r in option_attributes(entry)] == [("color", "Deep  NAVY")], (
        option_attributes(entry)
    )


def test_a_variant_whose_title_does_not_decompose_still_supports_its_own_values() -> None:
    """2,560 real variant titles contain ``" / "`` inside a value. The slots still decide.

    This is the case that rules the title join out: ``"Bundle / Pack / Black"`` is two values,
    not three, and only ``option1``/``option2`` say which two.
    """
    entry = {
        "id": 1,
        "options": [
            {"name": "Style", "position": 1, "values": ["Bundle / Pack", "Single"]},
            {"name": "Color", "position": 2, "values": ["Black", "Red"]},
        ],
        "variants": [
            {
                "id": 11,
                "title": "Bundle / Pack / Black",
                "option1": "Bundle / Pack",
                "option2": "Black",
            }
        ],
    }
    assert [(r.key, r.value) for r in option_attributes(entry)] == [
        ("style", "Bundle / Pack"),
        ("color", "Black"),
    ], option_attributes(entry)


def test_an_option_whose_two_statements_of_its_slot_disagree_asserts_nothing() -> None:
    """The merchant says which slot an option binds to twice. Both, or neither.

    ``options[].position`` and the entry's place in the array are the same fact stated twice,
    and over the recorded corpus they never disagree (0 of 4,903 products). When they DO,
    which variant slot holds this option's values is precisely what has just been said two
    ways, so there is nothing to check against and nothing is written — instead of checking
    ``Finish`` against the slot that holds colours and writing whatever happens to collide.
    """
    entry = {
        "id": 1,
        "options": [
            {"name": "Color", "position": 1, "values": ["Black"]},
            # Second in the array, but claims to be first. One of the two is wrong and the
            # record does not say which.
            {"name": "Finish", "position": 1, "values": ["Black"]},
        ],
        "variants": [{"id": 11, "title": "Black / Matte", "option1": "Black", "option2": "Matte"}],
    }
    assert [(r.key, r.value) for r in option_attributes(entry)] == [("color", "Black")], (
        option_attributes(entry)
    )


def test_a_fourth_option_binds_to_no_variant_slot_and_asserts_nothing() -> None:
    """A Shopify variant states three options. A fourth is unconfirmable, so it is unwritten."""
    entry = {
        "id": 1,
        "options": [
            {"name": "Size", "position": 1, "values": ["M"]},
            {"name": "Color", "position": 2, "values": ["Black"]},
            {"name": "Fit", "position": 3, "values": ["Slim"]},
            {"name": "Monogram", "position": 4, "values": ["HH"]},
        ],
        "variants": [
            {
                "id": 11,
                "title": "M / Black / Slim",
                "option1": "M",
                "option2": "Black",
                "option3": "Slim",
            }
        ],
    }
    assert [(r.key, r.value) for r in option_attributes(entry)] == [
        ("size", "M"),
        ("color", "Black"),
        ("fit", "Slim"),
    ], option_attributes(entry)


def test_a_product_that_states_no_variants_asserts_nothing() -> None:
    """No variant is no evidence. Silence is the D55 answer, not the picker's domain."""
    entry = {
        "id": 1,
        "options": [{"name": "Color", "position": 1, "values": ["Navy", "Terra Cotta"]}],
        "variants": [],
    }
    assert option_attributes(entry) == (), option_attributes(entry)
